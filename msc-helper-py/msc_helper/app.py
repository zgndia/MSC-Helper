"""Main MapleHelperApp — wires worker, overlay, settings, and UI mixins."""
from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any

from msc_helper.app_ui import AppUIMixin
from msc_helper.app_updates import AppUpdatesMixin
from msc_helper.config import (
    APP_NAME,
    APP_VERSION,
    BASE_DIR,
    CAPTURE_PATH,
    CONFIG_DIR,
    Settings,
    load_settings,
    save_settings,
)
from msc_helper.logging_util import get_logger, setup_file_logging
from msc_helper.overlay import GameOverlay
from msc_helper.runtime import find_rust_gui_binary, launch_rust_gui
from msc_helper.keys import parse_key_list
from msc_helper.worker import MonitorWorker

try:
    from native_keyboard_inputs import InputController, KEY_NAMES
    from native_keyboard_inputs.keys import KEY_ALIASES
    try:
        inputs = InputController("windows", keyboard_mode="scancode")
        INPUT_IMPORT_ERROR = None
    except Exception as exc:
        inputs = None
        INPUT_IMPORT_ERROR = exc
except ImportError as exc:
    inputs = None
    KEY_NAMES = ()
    KEY_ALIASES = {}
    INPUT_IMPORT_ERROR = exc

try:
    import mss
except ImportError as exc:
    mss = None
    DEPENDENCY_ERROR = exc
else:
    DEPENDENCY_ERROR = None

try:
    from msc_helper.detector import BarDetector
except ImportError as exc:
    BarDetector = None
    DETECTOR_IMPORT_ERROR = exc
else:
    DETECTOR_IMPORT_ERROR = None


class MapleHelperApp(AppUIMixin, AppUpdatesMixin):
    BG = "#171717"
    PANEL = "#242424"
    PANEL_ALT = "#2b2b2b"
    BORDER = "#3d3d3d"
    TEXT = "#eeeeee"
    MUTED = "#a9a9a9"
    RED = "#e87979"
    GREEN = "#7fc49a"
    AMBER = "#dfb96e"
    MANA = "#80b99d"

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(f"{APP_NAME}  v{APP_VERSION}")
        self.root.geometry("1020x720")
        self.root.minsize(900, 700)
        self.root.configure(bg=self.BG)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        # Use the bundled multi-size .ico for the main window (and later the overlay).
        icon_path = BASE_DIR / "icon.ico"
        self._app_icon_path = icon_path
        if icon_path.is_file():
            try:
                self.root.iconbitmap(default=str(icon_path))
            except Exception:
                try:
                    self.root.iconbitmap(str(icon_path))
                except Exception:
                    pass
            # Extra fallback for some Tk builds that ignore iconbitmap on the main window.
            try:
                self.root.wm_iconbitmap(str(icon_path))
            except Exception:
                pass
        self.events: queue.Queue = queue.Queue()
        self.stop_event = threading.Event()
        self.force_ocr_event = threading.Event()
        self.worker: MonitorWorker | None = None
        self.settings_lock = threading.Lock()
        self.current_settings = load_settings()
        self.capture_target: str | None = None
        self._last_status_text = ""
        self._last_status_color = self.AMBER
        self._last_snapshot_saved = 0.0
        self._latest_telemetry: dict[str, Any] = {}
        self._recent_log_lines: list[str] = []
        self._rust_gui_proc: subprocess.Popen | None = None
        # UI is the Rust GUI only. The Tk root stays hidden and only
        # drives the overlay + worker timers. The old Tk notebook UI is not shown.
        self._rust_gui_mode = True
        try:
            self.root.withdraw()
            self.root.overrideredirect(False)
            self.root.attributes("-alpha", 0.0)
        except Exception:
            try:
                self.root.withdraw()
            except Exception:
                pass
        self._build_styles()
        self._build_ui()
        self.overlay = GameOverlay(self)
        # Control plane for the Rust GUI (127.0.0.1:17890).
        try:
            from control_server import ControlServer
            self._control_server = ControlServer(self)
            self._control_server.start()
        except Exception:
            self._control_server = None
        try:
            self.root.withdraw()
        except Exception:
            pass
        # Launch the Rust GUI after a short delay so the control server is up.
        self.root.after(300, self._start_rust_gui)
        # Propagate the same icon to the overlay Toplevel when possible.
        if icon_path.is_file():
            try:
                self.overlay.window.iconbitmap(default=str(icon_path))
            except Exception:
                try:
                    self.overlay.window.iconbitmap(str(icon_path))
                except Exception:
                    pass
        self._restore_last_capture_snapshot()
        try:
            setup_file_logging()
            get_logger().info("UI initialized (v%s)", APP_VERSION)
        except Exception:
            pass
        self.root.after(80, self._process_events)
        self.root.after(250, self._start_worker)
        # Paint the overlay on a slow independent timer so it never contends with the game.
        self.root.after(1000, self._refresh_overlay)
        self.root.after(3000, self._schedule_auto_update_check)
        self._set_status("Searching for MapleStory.exe", self.AMBER)
        if DEPENDENCY_ERROR is not None:
            self._log("Missing screen-capture dependency. Run: python -m pip install -r requirements.txt", "warning")
        if INPUT_IMPORT_ERROR is not None:
            self._log("Native keyboard inputs is unavailable in this Python environment.", "warning")
        if DETECTOR_IMPORT_ERROR is not None:
            self._log("OpenCV detector is unavailable. Run: python -m pip install -r requirements.txt", "warning")
        if sys.platform != "win32":
            self._log("The game monitor currently requires Windows.", "warning")

    def _parse_settings(self) -> Settings:
        def integer(value: str, name: str, minimum: int, maximum: int) -> int:
            try:
                result = int(value.strip())
            except (ValueError, AttributeError) as exc:
                raise ValueError(f"{name} must be a whole number.") from exc
            if not minimum <= result <= maximum:
                raise ValueError(f"{name} must be between {minimum} and {maximum}.")
            return result

        def threshold_value(value: str, name: str, mode: str, maximum: int) -> float | int:
            if mode == "percent":
                try:
                    result = float(value.strip())
                except (ValueError, AttributeError) as exc:
                    raise ValueError(f"{name} must be a percentage.") from exc
                if not 30.0 <= result <= 100.0:
                    raise ValueError(f"{name} must be between 30 and 100 percent remaining.")
                return result
            return integer(value, name, 1, maximum)

        title = self.window_title_var.get().strip()
        if not title:
            raise ValueError("Window title filter cannot be empty.")
        hp_key = canonical_key(self.hp_vars["key"].get())
        mp_key = canonical_key(self.mp_vars["key"].get())
        if hp_key == mp_key:
            raise ValueError("HP and MP potion bindings must be different keys.")
        # Allow 0 while auto-detect has not filled max stats yet.
        max_hp = integer(self.hp_vars["maximum"].get(), "Maximum HP", 0, 10_000_000)
        max_mp = integer(self.mp_vars["maximum"].get(), "Maximum MP", 0, 10_000_000)
        hp_bound = max(max_hp, 1)
        mp_bound = max(max_mp, 1)
        hp_mode = self._mode_value(self.hp_mode_var.get())
        mp_mode = self._mode_value(self.mp_mode_var.get())
        hp_offset = integer(self.hp_vars["offset"].get(), "HP random offset", 0, hp_bound)
        mp_offset = integer(self.mp_vars["offset"].get(), "MP random offset", 0, mp_bound)
        movement_keys = parse_monitor_keys(self.movement_keys_var.get())
        movement_key_names = ", ".join(name for name, _vk in movement_keys)
        # Ability keys are only required when auto-cast is enabled.
        ability_enabled = bool(self.ability_enabled_var.get())
        if ability_enabled:
            ability_keys = parse_key_list(self.ability_keys_var.get())
            collisions = set(ability_keys) & {hp_key, mp_key}
            if collisions:
                raise ValueError(
                    "Random spam keys cannot also be HP/MP potion keys: "
                    + ", ".join(sorted(collisions))
                )
            ability_keys_text = ", ".join(ability_keys)
        else:
            raw_ability = self.ability_keys_var.get().strip()
            ability_keys_text = raw_ability or self.current_settings.ability_keys
        return Settings(
            window_title=title,
            max_hp=max_hp,
            max_mp=max_mp,
            hp_threshold=threshold_value(self.hp_vars["threshold"].get(), "HP threshold", hp_mode, hp_bound),
            mp_threshold=threshold_value(self.mp_vars["threshold"].get(), "MP threshold", mp_mode, mp_bound),
            hp_threshold_mode=hp_mode,
            mp_threshold_mode=mp_mode,
            hp_random_offset=hp_offset,
            mp_random_offset=mp_offset,
            hp_enabled=bool(self.hp_enabled_var.get()),
            mp_enabled=bool(self.mp_enabled_var.get()),
            hp_key=hp_key,
            mp_key=mp_key,
            potion_cooldown_ms=integer(self.cooldown_var.get(), "Potion cooldown", 100, 60_000),
            hud_scan_interval_ms=integer(self.scan_interval_var.get(), "HUD scan interval", 200, 10_000),
            key_hold_ms=integer(self.key_press_var.get(), "Base key hold", 30, 3_000),
            min_focus_hold_ms=integer(self.min_focus_hold_var.get(), "Minimum focus hold", 0, 30_000),
            auto_detect_max_stats=bool(self.auto_detect_var.get()),
            hud_monitoring_enabled=bool(self.hud_monitoring_var.get()),
            passive_hud_reading=bool(self.passive_hud_var.get()),
            ocr_interval_ms=max(800, int(self.current_settings.ocr_interval_ms or 1800)),
            ability_spam_enabled=ability_enabled,
            ability_keys=ability_keys_text,
            movement_keys=movement_key_names,
            ability_interval_ms=integer(self.ability_interval_var.get(), "Delay between each cast", 25, 10_000),
            ability_set_delay_ms=integer(self.ability_set_delay_var.get(), "Delay after ability set", 0, 60_000),
            movement_hold_ms=integer(self.movement_hold_var.get(), "Movement hold gate", 100, 30_000),
            pause_spam_while_typing=bool(self.pause_typing_var.get()),
            overlay_enabled=bool(self.overlay_enabled_var.get()),
            overlay_x_offset=self.current_settings.overlay_x_offset,
            overlay_y_offset=self.current_settings.overlay_y_offset,
            overlay_width=self.current_settings.overlay_width,
            overlay_height=self.current_settings.overlay_height,
            auto_update_enabled=bool(self.auto_update_var.get()),
            last_window_title=self.current_settings.last_window_title,
            last_window_pid=self.current_settings.last_window_pid,
            last_window_hwnd=self.current_settings.last_window_hwnd,
        )

    def apply_settings(self, show_errors: bool = True) -> bool:
        try:
            settings = self._parse_settings()
        except ValueError as exc:
            if show_errors:
                messagebox.showerror("Invalid settings", str(exc), parent=self.root)
            else:
                self._log(f"Setting not applied: {exc}", "warning")
            return False
        # Auto-pot depends on HUD monitoring. OCR/Tesseract stays user-controlled.
        if settings.hp_enabled or settings.mp_enabled:
            if not settings.hud_monitoring_enabled:
                settings = replace(settings, hud_monitoring_enabled=True)
                try:
                    self.hud_monitoring_var.set(True)
                except Exception:
                    pass
                self._log(
                    "HUD monitoring enabled because an auto-pot toggle is on.",
                    "success",
                )
        else:
            # Both auto-pot toggles off → disable HUD monitoring only.
            # Tesseract toggle is never touched; OCR is inert while HUD is off.
            if settings.hud_monitoring_enabled:
                settings = replace(settings, hud_monitoring_enabled=False)
                try:
                    self.hud_monitoring_var.set(False)
                except Exception:
                    pass
                self._log(
                    "HUD monitoring disabled because both auto-pot toggles are off.",
                    "success",
                )
        with self.settings_lock:
            self.current_settings = settings
        # Apply in-memory immediately; disk write is debounced so toggles stay snappy.
        self._schedule_settings_save()
        self._update_enabled_summary(settings)
        if show_errors:
            self._log("Settings applied.", "success")
        self._start_worker()
        return True

    def _update_enabled_summary(self, settings: Settings | None = None) -> None:
        settings = settings or self._settings_snapshot()
        commands = []
        if settings.hp_enabled:
            commands.append(f"HP potion ({settings.hp_key.upper()})")
        if settings.mp_enabled:
            commands.append(f"MP potion ({settings.mp_key.upper()})")
        if settings.ability_spam_enabled:
            commands.append(f"Auto-Cast ({settings.ability_keys.upper()})")
        self.enabled_summary.configure(text="\n".join(f"• {item}" for item in commands) if commands else "No commands enabled.")

    def reset_settings(self) -> None:
        if not messagebox.askyesno("Reset options", "Reset every option to the recommended defaults?", parent=self.root):
            return
        defaults = Settings()
        with self.settings_lock:
            self.current_settings = defaults
        self._load_settings_into_ui(defaults)
        try:
            save_settings(defaults)
        except OSError as exc:
            self._log(f"Could not save reset settings: {exc}", "warning")
        try:
            CAPTURE_PATH.unlink(missing_ok=True)
        except OSError as exc:
            self._log(f"Could not clear saved capture snapshot: {exc}", "warning")
        self._update_enabled_summary(defaults)
        if self.overlay.target_rect:
            self.overlay.show_on_target()
        self._log("All options reset to recommended defaults.", "success")

    def _load_settings_into_ui(self, s: Settings) -> None:
        self.window_title_var.set(s.window_title)
        self.cooldown_var.set(str(s.potion_cooldown_ms))
        self.scan_interval_var.set(str(s.hud_scan_interval_ms))
        self.key_press_var.set(str(s.key_hold_ms))
        self.min_focus_hold_var.set(str(s.min_focus_hold_ms))
        self.auto_detect_var.set(s.auto_detect_max_stats)
        self.passive_hud_var.set(s.passive_hud_reading)
        for variables, settings_values in (
            (self.hp_vars, (s.max_hp, s.hp_threshold, s.hp_random_offset, s.hp_key)),
            (self.mp_vars, (s.max_mp, s.mp_threshold, s.mp_random_offset, s.mp_key)),
        ):
            for key, value in zip(("maximum", "threshold", "offset", "key"), settings_values):
                variables[key].set(str(value))
        self.hp_enabled_var.set(s.hp_enabled)
        self.mp_enabled_var.set(s.mp_enabled)
        self.hp_mode_var.set(self._mode_label(s.hp_threshold_mode))
        self.mp_mode_var.set(self._mode_label(s.mp_threshold_mode))
        self.ability_enabled_var.set(s.ability_spam_enabled)
        self.movement_keys_var.set(s.movement_keys)
        self.ability_keys_var.set(s.ability_keys)
        self.ability_interval_var.set(str(s.ability_interval_ms))
        self.ability_set_delay_var.set(str(s.ability_set_delay_ms))
        self.movement_hold_var.set(str(s.movement_hold_ms))
        self.pause_typing_var.set(s.pause_spam_while_typing)
        self.overlay_enabled_var.set(s.overlay_enabled)
        self.auto_update_var.set(s.auto_update_enabled)
        self.hud_monitoring_var.set(s.hud_monitoring_enabled)

    def persist_overlay_geometry(self, x: int, y: int, width: int, height: int) -> None:
        s = replace(self._settings_snapshot(),
                    overlay_x_offset=int(x), overlay_y_offset=int(y),
                    overlay_width=max(GameOverlay.MIN_WIDTH, min(GameOverlay.MAX_WIDTH, int(width))),
                    overlay_height=max(GameOverlay.MIN_HEIGHT, min(GameOverlay.MAX_HEIGHT, int(height))))
        with self.settings_lock:
            self.current_settings = s
        self._schedule_settings_save()

    def _settings_snapshot(self) -> Settings:
        with self.settings_lock:
            return self.current_settings

    def _start_worker(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        if sys.platform != "win32":
            self._set_status("Windows required", self.RED)
            return
        if DEPENDENCY_ERROR is not None or DETECTOR_IMPORT_ERROR is not None or INPUT_IMPORT_ERROR is not None:
            self._set_status("Missing dependencies", self.RED)
            return
        self.stop_event.clear()
        self.worker = MonitorWorker(
            self._settings_snapshot, self.events, self.stop_event, self.force_ocr_event
        )
        self.worker.start()

    def request_stat_detection(self) -> None:
        if not self._settings_snapshot().auto_detect_max_stats:
            self.auto_detect_var.set(True)
            self.apply_settings()
        self.force_ocr_event.set()
        self._log("Max-stat read requested. Focus MapleStory; the values update automatically.", "success")

    def begin_capture(self, target: str) -> None:
        """Listen for one key. Always auto-expires so Entry widgets cannot stay blocked."""
        self._cancel_key_capture()
        self.capture_target = target
        self._set_status(f"Press a key for {target.upper()}", self.AMBER)
        # Do not focus_force the root — that steals caret from whatever the user is typing.
        self.root.bind("<KeyPress>", self._on_capture_key)
        self._capture_timeout_id = self.root.after(8000, self._cancel_key_capture)

    def _cancel_key_capture(self) -> None:
        self.capture_target = None
        try:
            self.root.unbind("<KeyPress>")
        except tk.TclError:
            pass
        timeout_id = getattr(self, "_capture_timeout_id", None)
        if timeout_id is not None:
            try:
                self.root.after_cancel(timeout_id)
            except Exception:
                pass
            self._capture_timeout_id = None

    def _on_capture_key(self, event) -> str | None:
        if self.capture_target is None:
            return None
        # Ignore pure modifier presses.
        if event.keysym in {"Shift_L", "Shift_R", "Control_L", "Control_R", "Alt_L", "Alt_R"}:
            return "break"
        target = self.capture_target
        self._cancel_key_capture()
        key = self._keysym_to_key(event.keysym)
        if key is None:
            self._log("That key is not supported. Type its name into the box instead.", "warning")
            return "break"
        (self.hp_vars if target == "hp" else self.mp_vars)["key"].set(key)
        self.apply_settings(show_errors=False)
        self._log(f"Captured {target.upper()} potion binding: {key.upper()}", "success")
        return "break"

    @staticmethod
    def _keysym_to_key(keysym: str) -> str | None:
        mapping = {
            "Escape": "escape", "Return": "enter", "space": "space", "Tab": "tab",
            "BackSpace": "backspace", "Delete": "delete", "Insert": "insert",
            "Home": "home", "End": "end", "Prior": "page_up", "Next": "page_down",
            "Left": "left", "Right": "right", "Up": "up", "Down": "down",
            "Shift_L": "left_shift", "Shift_R": "right_shift",
            "Control_L": "left_ctrl", "Control_R": "right_ctrl",
            "Alt_L": "left_alt", "Alt_R": "right_alt",
        }
        if keysym in mapping:
            return mapping[keysym]
        value = keysym.lower()
        if value in {f"f{i}" for i in range(1, 25)}:
            return value
        if len(value) == 1 and value in "abcdefghijklmnopqrstuvwxyz0123456789":
            return value
        punctuation = {
            "minus": "minus", "equal": "equal", "bracketleft": "left_bracket",
            "bracketright": "right_bracket", "backslash": "backslash", "semicolon": "semicolon",
            "apostrophe": "apostrophe", "grave": "grave", "comma": "comma", "period": "period", "slash": "slash",
        }
        return punctuation.get(value)

    def _process_events(self) -> None:
        """Drain the worker queue without blocking the Tk event loop.

        Coalesce to the latest packet of each kind so a burst of telemetry /
        target-window events can never pile up UI work. Never write files here.
        """
        latest_status = None
        latest_telemetry = None
        latest_target = None
        latest_stats = None
        log_batch: list[tuple[str, str]] = []
        try:
            while True:
                kind, data = self.events.get_nowait()
                if kind == "status":
                    latest_status = data
                elif kind == "log":
                    lower = data.lower()
                    tag = (
                        "warning" if ("failed" in lower or "could not" in lower or "unavailable" in lower)
                        else "success" if ("auto-detected" in lower or "armed" in lower)
                        else "event"
                    )
                    log_batch.append((data, tag))
                elif kind == "telemetry":
                    latest_telemetry = data
                elif kind == "detected_stats":
                    latest_stats = data
                elif kind == "target_window":
                    latest_target = data
        except queue.Empty:
            pass

        # Status first (cheap label update).
        if latest_status is not None:
            text = latest_status
            lowered = text.lower()
            if "tracking" in lowered or "captured" in lowered:
                color = self.GREEN
            elif (
                "error" in lowered
                or "required" in lowered
                or "unavailable" in lowered
                or "not running" in lowered
            ):
                color = self.RED
            else:
                color = self.AMBER
            self._set_status(text, color)

        # Keep the hot path light so notebook tab clicks are not starved.
        # Log lines and overlay geometry are deferred to idle time.
        if log_batch:
            pending_logs = log_batch[-4:]

            def _flush_logs(batch=pending_logs) -> None:
                for data, tag in batch:
                    self._log(data, tag)

            self.root.after_idle(_flush_logs)

        # Telemetry is pure widget updates (already gated by change-detection).
        if latest_telemetry is not None:
            self._update_telemetry(latest_telemetry)

        # Detected max-stats is rare; apply without a synchronous disk write.
        if latest_stats is not None:
            self._apply_detected_stats(latest_stats)

        # Overlay / target geometry last — Win32 SetWindowPos is the expensive bit.
        if latest_target is not None:
            self._handle_target_window(latest_target)

        # 100 ms is still responsive for status; slightly less contention than 80.
        self.root.after(100, self._process_events)

    def _handle_target_window(self, data: dict[str, Any]) -> None:
        title = data.get("title") or "MapleStory"
        pid = int(data.get("pid") or 0)
        hwnd = int(data.get("hwnd") or 0)
        visible = bool(data.get("visible", True))
        focused = bool(data.get("focused"))
        # Avoid showing a fake "Found … process ID 0" when MapleStory is not running.
        if not hwnd or not pid or not visible:
            info = "MapleStory is not running. Start MapleStory Classic to begin monitoring."
        elif focused:
            info = f"Captured: {title}  ·  process ID {pid}  ·  window handle {hwnd}"
        else:
            info = (
                f"Found: {title}  ·  process ID {pid}. HUD capture is passive while the game is visible; "
                "game input commands run only when MapleStory is focused."
            )
        try:
            self._latest_telemetry["target_info"] = info
        except Exception:
            pass
        try:
            if self.target_info.cget("text") != info:
                self.target_info.configure(text=info)
        except Exception:
            pass
        if hwnd and pid:
            current = self._settings_snapshot()
            if (current.last_window_title, current.last_window_pid, current.last_window_hwnd) != (title, pid, hwnd):
                # In-memory only — never write the settings file from this path.
                updated = replace(current, last_window_title=title, last_window_pid=pid, last_window_hwnd=hwnd)
                with self.settings_lock:
                    self.current_settings = updated
        # Defer Win32 overlay geometry work so it never blocks Entry/button input.
        self._pending_overlay_target = data
        if not getattr(self, "_overlay_target_scheduled", False):
            self._overlay_target_scheduled = True
            self.root.after(50, self._flush_overlay_target)

    def _flush_overlay_target(self) -> None:
        self._overlay_target_scheduled = False
        data = getattr(self, "_pending_overlay_target", None)
        self._pending_overlay_target = None
        if data is None:
            return
        try:
            self.overlay.set_target(data)
        except Exception:
            pass

    def _apply_detected_stats(self, data: dict[str, int]) -> None:
        with self.settings_lock:
            updated = self.current_settings
            if "max_hp" in data:
                new_max = max(1, int(data["max_hp"]))
                updated = replace(
                    updated, max_hp=new_max,
                    hp_threshold=min(updated.hp_threshold, new_max) if updated.hp_threshold_mode == "points" else updated.hp_threshold,
                    hp_random_offset=min(updated.hp_random_offset, new_max),
                )
                self.hp_vars["maximum"].set(str(new_max))
                self.hp_vars["offset"].set(str(updated.hp_random_offset))
                if updated.hp_threshold_mode == "points":
                    self.hp_vars["threshold"].set(str(updated.hp_threshold))
            if "max_mp" in data:
                new_max = max(1, int(data["max_mp"]))
                updated = replace(
                    updated, max_mp=new_max,
                    mp_threshold=min(updated.mp_threshold, new_max) if updated.mp_threshold_mode == "points" else updated.mp_threshold,
                    mp_random_offset=min(updated.mp_random_offset, new_max),
                )
                self.mp_vars["maximum"].set(str(new_max))
                self.mp_vars["offset"].set(str(updated.mp_random_offset))
                if updated.mp_threshold_mode == "points":
                    self.mp_vars["threshold"].set(str(updated.mp_threshold))
            self.current_settings = updated
        # Debounce the disk write so auto-detect never freezes the UI.
        self._schedule_settings_save()

    def _schedule_settings_save(self) -> None:
        """Write settings.json a moment later on a worker thread."""
        if getattr(self, "_settings_save_pending", False):
            return
        self._settings_save_pending = True

        def flush() -> None:
            self._settings_save_pending = False
            snapshot = self._settings_snapshot()

            def writer() -> None:
                try:
                    save_settings(snapshot)
                except OSError:
                    pass

            threading.Thread(target=writer, name="SettingsWriter", daemon=True).start()

        try:
            self.root.after(400, flush)
        except Exception:
            self._settings_save_pending = False

    def _persist_capture_snapshot(self, data: dict[str, Any]) -> None:
        """Queue a snapshot write on a background thread — never block the UI."""
        has_resource_values = any(data.get(f"{kind}_current") is not None for kind in ("hp", "mp"))
        if not data.get("focused") and not has_resource_values:
            return
        now = time.monotonic()
        if now - self._last_snapshot_saved < 5.0:
            return
        self._last_snapshot_saved = now
        settings = self._settings_snapshot()
        # Shallow copy of the telemetry payload so the worker thread owns its data.
        payload = dict(data)

        def writer() -> None:
            previous: dict[str, Any] = {}
            try:
                if CAPTURE_PATH.exists():
                    previous = json.loads(CAPTURE_PATH.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                previous = {}

            def saved_resource(kind: str, maximum: int) -> dict[str, Any]:
                incoming = {
                    "current": payload.get(f"{kind}_current"),
                    "maximum": payload.get(f"{kind}_max", maximum),
                    "ratio": payload.get(f"{kind}_ratio"),
                    "last_seen": payload.get(f"{kind}_last_seen"),
                    "exact": bool(payload.get(f"{kind}_exact")),
                }
                old = previous.get(kind) if isinstance(previous.get(kind), dict) else {}
                if incoming["current"] is None and old.get("current") is not None:
                    incoming["current"] = old.get("current")
                    incoming["maximum"] = old.get("maximum", maximum)
                    incoming["ratio"] = old.get("ratio")
                    incoming["last_seen"] = old.get("last_seen")
                    incoming["exact"] = bool(old.get("exact"))
                return incoming

            snapshot = {
                "captured_at": datetime.now().isoformat(timespec="seconds"),
                "window": {
                    "title": settings.last_window_title or previous.get("window", {}).get("title", ""),
                    "process_id": settings.last_window_pid or previous.get("window", {}).get("process_id", 0),
                    "handle": settings.last_window_hwnd or previous.get("window", {}).get("handle", 0),
                },
                "hp": saved_resource("hp", settings.max_hp),
                "mp": saved_resource("mp", settings.max_mp),
                "focused": bool(payload.get("focused")),
                "options": asdict(settings),
            }
            try:
                CONFIG_DIR.mkdir(parents=True, exist_ok=True)
                CAPTURE_PATH.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
            except OSError:
                pass

        threading.Thread(target=writer, name="SnapshotWriter", daemon=True).start()

    def _restore_last_capture_snapshot(self) -> None:
        """Show last saved HP/MP until the new session gets a live frame."""
        try:
            snapshot = json.loads(CAPTURE_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return
        if not isinstance(snapshot, dict):
            return
        settings = self._settings_snapshot()
        data: dict[str, Any] = {
            "focused": False,
            "max_hp": settings.max_hp,
            "max_mp": settings.max_mp,
            "ability_listening": settings.ability_spam_enabled,
            "movement_keys": settings.movement_keys,
            "movement_detected": False,
            "enabled_commands": [],
        }
        if settings.hp_enabled:
            data["enabled_commands"].append(f"HP potion ({settings.hp_key.upper()})")
        if settings.mp_enabled:
            data["enabled_commands"].append(f"MP potion ({settings.mp_key.upper()})")
        if settings.ability_spam_enabled:
            data["enabled_commands"].append(f"Random key spam ({settings.ability_keys.upper()})")
        for kind in ("hp", "mp"):
            resource = snapshot.get(kind) if isinstance(snapshot.get(kind), dict) else {}
            current = resource.get("current")
            maximum = int(resource.get("maximum") or data[f"max_{kind}"])
            ratio = resource.get("ratio")
            if ratio is None and current is not None and maximum > 0:
                ratio = float(current) / maximum
            data[f"{kind}_current"] = current
            data[f"{kind}_max"] = maximum
            data[f"{kind}_ratio"] = ratio
            data[f"{kind}_exact"] = bool(resource.get("exact", False))
            data[f"{kind}_last_seen"] = resource.get("last_seen")
            data[f"{kind}_fresh"] = False
        self._update_telemetry(data)
        self._log("Restored the last saved HP/MP values. They will remain visible until new HUD readings arrive.", "event")

    def _update_telemetry(self, data: dict[str, Any]) -> None:
        # Store latest data for the slow overlay refresh; do not paint the overlay here.
        self._latest_telemetry = data
        key = (
            data.get("hp_current"), data.get("hp_max"), data.get("hp_ratio"),
            data.get("mp_current"), data.get("mp_max"), data.get("mp_ratio"),
            bool(data.get("hp_fresh")), bool(data.get("mp_fresh")),
            bool(data.get("focused")), data.get("hp_last_seen"), data.get("mp_last_seen"),
            tuple(data.get("enabled_commands") or ()),
        )
        if key == getattr(self, "_last_gui_telemetry_key", None):
            return
        self._last_gui_telemetry_key = key

        for kind, meter, readout, freshness in (
            ("hp", self.hp_meter, self.hp_readout, self.hp_freshness),
            ("mp", self.mp_meter, self.mp_readout, self.mp_freshness),
        ):
            ratio = data.get(f"{kind}_ratio")
            current = data.get(f"{kind}_current")
            maximum = data.get(f"{kind}_max")
            if ratio is not None:
                meter.set_ratio(ratio)
            if current is not None and maximum is not None:
                text = f"{kind.upper()}  {current:,} / {maximum:,}"
                if readout.cget("text") != text:
                    readout.configure(text=text)
            if data.get(f"{kind}_fresh") and data.get("focused"):
                if freshness.cget("text") != f"TRACKING {kind.upper()}":
                    freshness.configure(text=f"TRACKING {kind.upper()}", foreground=self.GREEN)
            elif data.get(f"{kind}_last_seen"):
                text = f"LAST SEEN  {data[f'{kind}_last_seen']}"
                if freshness.cget("text") != text:
                    freshness.configure(text=text, foreground=self.AMBER)
            else:
                if freshness.cget("text") != "WAITING FOR HUD":
                    freshness.configure(text="WAITING FOR HUD", foreground=self.MUTED)
        commands = data.get("enabled_commands") or []
        command_signature = tuple(commands)
        if command_signature != getattr(self, "_last_summary_signature", None):
            self._last_summary_signature = command_signature
            display = [f"• {item}" for item in commands] or ["No commands enabled."]
            self.enabled_summary.configure(text="\n".join(display))
            self._update_enabled_summary()
        self._persist_capture_snapshot(data)

    def _refresh_overlay(self) -> None:
        """Low-frequency overlay paint. Completely skipped when the overlay is disabled."""
        enabled = False
        try:
            enabled = bool(self._settings_snapshot().overlay_enabled)
            if enabled:
                data = self._latest_telemetry
                if data and getattr(self.overlay, "window", None) is not None:
                    try:
                        if self.overlay.window.winfo_exists():
                            self.overlay.update_data(data)
                    except tk.TclError:
                        pass
        except Exception:
            pass
        # Poll often enough that focus status on the overlay feels instantaneous.
        self.root.after(200 if enabled else 2500, self._refresh_overlay)

    def _set_status(self, text: str, color: str) -> None:
        label = f"●  {text.upper()}"
        if getattr(self, "_last_status_text", None) == label and getattr(self, "_last_status_color", None) == color:
            return
        self._last_status_text = label
        self._last_status_color = color
        # Keep a plain copy for the Rust GUI control plane.
        try:
            color_name = "green" if color == self.GREEN else "red" if color == self.RED else "muted" if color == self.MUTED else "amber"
            self._latest_telemetry["status_text"] = text
            self._latest_telemetry["status_color"] = color_name
        except Exception:
            pass
        try:
            self.status_badge.configure(text=label, fg=color)
        except Exception:
            pass

    def _log(self, text: str, tag: str = "event") -> None:
        # Suppress consecutive identical UI messages (keeps the activity feed readable).
        if getattr(self, "_last_ui_log_text", None) == text:
            return
        self._last_ui_log_text = text
        try:
            level = logging.WARNING if tag == "warning" else logging.INFO
            get_logger().log(level, text)
        except Exception:
            pass
        timestamp = datetime.now().strftime("%H:%M:%S")
        line = f"{timestamp}  {text}"
        try:
            self._recent_log_lines.append(line)
            if len(self._recent_log_lines) > 80:
                self._recent_log_lines = self._recent_log_lines[-60:]
            self._latest_telemetry["log_lines"] = list(self._recent_log_lines[-40:])
        except Exception:
            pass
        if not hasattr(self, "log"):
            return
        widget = self.log
        widget.configure(state="normal")
        widget.insert("end", f"{timestamp}  ", "time")
        widget.insert("end", text + "\n", tag)
        # Trim only every N lines so index/delete work stays rare.
        count = getattr(self, "_log_line_count", 0) + 1
        self._log_line_count = count
        if count >= 40:
            self._log_line_count = 0
            try:
                lines = int(widget.index("end-1c").split(".")[0])
                if lines > 120:
                    widget.delete("1.0", f"{lines - 80}.0")
            except tk.TclError:
                pass
        widget.see("end")
        widget.configure(state="disabled")

    def _start_rust_gui(self) -> None:
        """Launch the bundled / nearby Rust GUI once the control plane is ready."""
        if self._rust_gui_proc is not None:
            return
        # Ensure Tk stays invisible for the whole session.
        try:
            self.root.withdraw()
            self.root.attributes("-alpha", 0.0)
        except Exception:
            pass
        proc = launch_rust_gui()
        if proc is not None:
            self._rust_gui_proc = proc
            self._log("Rust GUI started.", "success")
            # When the Rust window is closed, shut down the whole helper
            # (do not pop the Tk window back — that was the dual-GUI bug).
            self.root.after(1000, self._watch_rust_gui)
        else:
            self._log(
                "Rust GUI binary not found – UI is required. Place msc-helper-gui.exe next to the app or under msc-helper-gui/target/release/.",
                "warning",
            )
            try:
                import ctypes
                ctypes.windll.user32.MessageBoxW(
                    None,
                    "MSC Helper requires the Rust GUI (msc-helper-gui.exe).\n\n"
                    "Build it with: cd msc-helper-gui && cargo build --release\n"
                    "Or place msc-helper-gui.exe next to MSCHelper.exe.",
                    APP_NAME,
                    0x10,
                )
            except Exception:
                pass
            self.root.after(100, self.on_close)

    def _watch_rust_gui(self) -> None:
        """If the Rust GUI process exits, close the backend as well."""
        proc = self._rust_gui_proc
        if proc is None:
            return
        if proc.poll() is not None:
            self._rust_gui_proc = None
            self._log("Rust GUI closed – shutting down.", "event")
            self.on_close()
            return
        self.root.after(1000, self._watch_rust_gui)

    def on_close(self) -> None:
        self.stop_event.set()
        try:
            if self._control_server is not None:
                self._control_server.stop()
        except Exception:
            pass
        try:
            if self._rust_gui_proc is not None and self._rust_gui_proc.poll() is None:
                self._rust_gui_proc.terminate()
        except Exception:
            pass
        try:
            save_settings(self._settings_snapshot())
        except Exception:
            pass
        try:
            if inputs is not None:
                inputs.keyboard.release_all()
        except Exception:
            pass
        try:
            self.overlay.window.destroy()
        except Exception:
            pass
        self.root.destroy()


