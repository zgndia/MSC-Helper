"""
Minimal local control plane for the Rust GUI.

Runs on 127.0.0.1:17890 and exposes:
  GET  /status   → current telemetry + settings
  POST /settings → apply settings from the Rust GUI
  POST /reset    → reset settings to defaults

This module is deliberately tiny and does not touch detector / MonitorWorker logic.
The existing MapleHelperApp instance is passed in so we can read/write its state.

Works both from source and inside a PyInstaller onefile build.
"""

from __future__ import annotations

import json
import sys
import threading
from dataclasses import asdict, replace
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Callable
from urllib.parse import urlparse

CONTROL_HOST = "127.0.0.1"
CONTROL_PORT = 17890


def _settings_helpers():
    """Resolve Settings / save_settings without relying on the module name.

    When frozen under PyInstaller the entry point is ``__main__``, so a
    plain ``from main import …`` can fail. Prefer the already-loaded
    module that owns the live Settings class.
    """
    for name in ("main", "__main__"):
        mod = sys.modules.get(name)
        if mod is not None and hasattr(mod, "Settings") and hasattr(mod, "save_settings"):
            return mod.Settings, mod.save_settings
    # Last resort – may still work in a normal source run.
    from main import Settings, save_settings  # type: ignore
    return Settings, save_settings


class ControlHandler(BaseHTTPRequestHandler):
    # Set by ControlServer before serving
    app_ref: Any = None
    on_settings: Callable[[dict], bool] | None = None
    on_reset: Callable[[], bool] | None = None

    def log_message(self, format: str, *args) -> None:  # silence default logging
        return

    def _json_response(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path != "/status":
            self._json_response(404, {"ok": False, "error": "not found"})
            return

        app = ControlHandler.app_ref
        if app is None:
            self._json_response(503, {"ok": False, "error": "app not ready"})
            return

        try:
            with app.settings_lock:
                settings = app.current_settings
                settings_dict = asdict(settings)

            telemetry = dict(getattr(app, "_latest_telemetry", {}) or {})
            # Enrich with fields the Rust GUI expects
            telemetry.setdefault("status_text", getattr(app, "_last_status_text", "—") or "—")
            # Strip the "●  " prefix if present so the Rust side can format it.
            st = telemetry.get("status_text", "")
            if isinstance(st, str) and st.startswith("●"):
                telemetry["status_text"] = st.lstrip("● ").strip()
            telemetry.setdefault("status_color", "amber")
            telemetry.setdefault("log_lines", list(getattr(app, "_recent_log_lines", []) or [])[-40:])
            telemetry.setdefault(
                "target_info",
                "MSC Helper is searching for MapleStory.exe. Make sure MapleStory Classic is running.",
            )
            telemetry.setdefault("enabled_commands", [])

            self._json_response(
                200,
                {
                    "ok": True,
                    "telemetry": telemetry,
                    "settings": settings_dict,
                },
            )
        except Exception as exc:
            self._json_response(500, {"ok": False, "error": str(exc)})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            data = {}

        if path == "/settings":
            settings_data = data.get("settings") or data
            if ControlHandler.on_settings is None:
                self._json_response(503, {"ok": False, "error": "no handler"})
                return
            ok = ControlHandler.on_settings(settings_data)
            self._json_response(200 if ok else 400, {"ok": ok})
            return

        if path == "/reset":
            if ControlHandler.on_reset is None:
                self._json_response(503, {"ok": False, "error": "no handler"})
                return
            ok = ControlHandler.on_reset()
            self._json_response(200 if ok else 400, {"ok": ok})
            return

        self._json_response(404, {"ok": False, "error": "not found"})


class ControlServer:
    def __init__(self, app: Any) -> None:
        self.app = app
        self._httpd: HTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        ControlHandler.app_ref = self.app
        ControlHandler.on_settings = self._apply_settings
        ControlHandler.on_reset = self._reset_settings

        self._httpd = HTTPServer((CONTROL_HOST, CONTROL_PORT), ControlHandler)
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            name="ControlServer",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        if self._httpd is not None:
            try:
                self._httpd.shutdown()
            except Exception:
                pass
            self._httpd = None

    def _apply_settings(self, data: dict) -> bool:
        """Apply a partial or full settings dict coming from the Rust GUI."""
        Settings, save_settings = _settings_helpers()
        app = self.app
        try:
            with app.settings_lock:
                current = app.current_settings
                allowed = Settings.__dataclass_fields__.keys()
                updates = {k: v for k, v in data.items() if k in allowed}
                for key in (
                    "max_hp",
                    "max_mp",
                    "hp_random_offset",
                    "mp_random_offset",
                    "potion_cooldown_ms",
                    "hud_scan_interval_ms",
                    "key_hold_ms",
                    "min_focus_hold_ms",
                    "ability_interval_ms",
                    "ability_set_delay_ms",
                    "movement_hold_ms",
                ):
                    if key in updates:
                        try:
                            updates[key] = int(updates[key])
                        except (TypeError, ValueError):
                            updates.pop(key, None)
                for key in ("hp_threshold", "mp_threshold"):
                    if key in updates:
                        try:
                            updates[key] = float(updates[key])
                        except (TypeError, ValueError):
                            updates.pop(key, None)

                updated = replace(current, **updates)
                app.current_settings = updated
            save_settings(updated)
            try:
                app.events.put(("log", "Settings updated from Rust GUI"))
            except Exception:
                pass
            return True
        except Exception:
            return False

    def _reset_settings(self) -> bool:
        Settings, save_settings = _settings_helpers()
        app = self.app
        try:
            with app.settings_lock:
                defaults = Settings()
                app.current_settings = defaults
            save_settings(defaults)
            try:
                app.events.put(("log", "Settings reset to defaults (from Rust GUI)"))
            except Exception:
                pass
            return True
        except Exception:
            return False
