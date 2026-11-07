"""Background monitor: HUD capture, potions, auto-cast."""
from __future__ import annotations

import queue
import random
import sys
import threading
import time
from typing import Any

from msc_helper.config import BASE_DIR, Settings
from msc_helper.logging_util import get_logger
from msc_helper.keys import (
    is_movement_key_held,
    is_typing_key_held,
    parse_key_list,
    parse_monitor_keys,
)
from msc_helper.platform_win import (
    _hide_helper_overlays,
    _restore_helper_overlays,
    find_target_windows,
    get_client_rect_screen,
    get_foreground_window_info,
    get_window_rect,
    invalidate_focus_cache,
    is_helper_under_cursor,
    is_target_focused,
    is_window_minimized,
    seed_focus_cache,
)

try:
    import mss
    import numpy as np
except ImportError as exc:
    mss = None
    np = None
    DEPENDENCY_ERROR = exc
else:
    DEPENDENCY_ERROR = None

try:
    from native_keyboard_inputs import InputController
    try:
        inputs = InputController("windows", keyboard_mode="scancode")
        INPUT_IMPORT_ERROR = None
    except Exception as exc:
        inputs = None
        INPUT_IMPORT_ERROR = exc
except ImportError as exc:
    inputs = None
    INPUT_IMPORT_ERROR = exc

try:
    from msc_helper.detector import BarDetector, BarReading, StatReading
except ImportError as exc:
    BarDetector = None
    BarReading = None
    StatReading = None
    DETECTOR_IMPORT_ERROR = exc
else:
    DETECTOR_IMPORT_ERROR = None

from msc_helper.movement import MovementGate

from msc_helper.worker_thresholds import ThresholdMixin

class MonitorWorker(ThresholdMixin, threading.Thread):
    def __init__(self, settings_getter, events: queue.Queue, stop_event: threading.Event,
                 force_ocr_event: threading.Event, app_hwnd: int = 0):
        super().__init__(name="MonitorWorker", daemon=True)
        self.settings_getter = settings_getter
        self.events = events
        self.stop_event = stop_event
        self.force_ocr_event = force_ocr_event
        # Deliberately do not store or use an application/game HWND for focus changes.
        self.detector: BarDetector | None = None
        self._last_status = ""
        self._last_telemetry = 0.0
        self._last_hp_pot = 0.0
        self._last_mp_pot = 0.0
        self._next_ability_at = 0.0
        self._ability_cycle_idx = 0
        self._last_ocr = 0.0
        self._movement_gate = MovementGate()
        self._typing_lock = threading.Lock()
        self._last_typing_at = 0.0
        self._injection_active = threading.Event()
        self._typing_pause_active = False
        self._typing_monitor: threading.Thread | None = None
        self._last_reading: dict[str, BarReading | None] = {"hp": None, "mp": None}
        self._fresh_flags: dict[str, bool] = {"hp": False, "mp": False}
        self._last_seen_wall: dict[str, str | None] = {"hp": None, "mp": None}
        self._last_seen_mono: dict[str, float | None] = {"hp": None, "mp": None}
        self._last_stats: dict[str, tuple[StatReading, float] | None] = {"hp": None, "mp": None}
        self._ocr_warning_sent = False
        self._active_thresholds: dict[str, float | int | None] = {"hp": None, "mp": None}
        self._threshold_signature: tuple[Any, ...] | None = None
        self._last_target_state: tuple[int, bool] | None = None
        self._last_target_hwnd = 0
        self._last_target_title = ""
        self._last_target_pid = 0
        self._last_target_client_size: tuple[int, int] | None = None
        self._last_target_rect: tuple[int, int, int, int] | None = None
        self._last_target_visible = False
        self._last_discovery_at = 0.0
        self._last_display: dict[str, dict[str, Any] | None] = {"hp": None, "mp": None}
        self._hud_roi_client: tuple[int, int, int, int] | None = None
        self._hud_roi_hwnd = 0
        self._hud_roi_client_size: tuple[int, int] | None = None
        self._hud_roi_misses = 0
        self._last_background_capture = 0.0
        self.BACKGROUND_CAPTURE_INTERVAL = 5.0
        self._last_capture_mono = 0.0
        self._ocr_lock = threading.Lock()
        self._ocr_busy = False
        self._ocr_thread: threading.Thread | None = None
        # Potion keys are blocked until at least one focused HUD scan completes.
        self._hud_scan_ready = False
        # Monotonic timestamp when the target window most recently became focused.
        self._focused_since: float | None = None

    def emit_status(self, text: str) -> None:
        if text != self._last_status:
            self._last_status = text
            self.events.put(("status", text))

    def emit_log(self, text: str) -> None:
        # Drop consecutive duplicate messages from the worker loop.
        if getattr(self, "_last_emit_log", None) == text:
            return
        self._last_emit_log = text
        self.events.put(("log", text))
        try:
            get_logger().info(text)
        except Exception:
            pass

    def _send_game_key(self, kind: str, key: str, settings: Settings, detail: str = "") -> bool:
        # Always re-check focus immediately before any injection decision.
        focused, _title = is_target_focused(settings.window_title, settings.window_id)
        if not focused:
            self._focused_since = None
            self.emit_log(
                f"Skipped {kind} key {key.upper()}: target window is not in focus."
            )
            return False

        hold_ms = max(0, int(getattr(settings, "min_focus_hold_ms", 1000) or 0))
        now = time.monotonic()
        if self._focused_since is None:
            self._focused_since = now
        focused_for_ms = (now - self._focused_since) * 1000.0
        if focused_for_ms < hold_ms:
            self.emit_log(
                f"Skipped {kind} key {key.upper()}: game focused for only "
                f"{focused_for_ms:.0f} ms (need {hold_ms} ms)."
            )
            return False

        if kind == "random spam":
            if not settings.ability_spam_enabled or not is_movement_key_held(settings.movement_keys):
                return False
            if settings.pause_spam_while_typing and self._typing_was_recent():
                return False
        else:
            # Potion / non-cast keys require at least one completed focused HUD scan
            # so we never inject before monitoring has observed the game state.
            if not self._hud_scan_ready:
                self.emit_log(
                    f"Skipped {kind} key {key.upper()}: waiting for the first HUD monitor scan."
                )
                return False
        if inputs is None:
            self.emit_log("Input unavailable: native-keyboard-inputs is not installed in this Python environment.")
            return False
        # Final focus check right before SendInput — covers tabbing out after the
        # decision path above but before the key is actually delivered. Also
        # rejects the helper by title so a GUI focus immediately before the game
        # is focused cannot slip through.
        invalidate_focus_cache()
        focused_now, _ = is_target_focused(settings.window_title, settings.window_id)
        title_now, pid_now, hwnd_now = get_foreground_window_info()
        if (
            not focused_now
            or not hwnd_now
            or not pid_now
            or is_helper_title(title_now)
            or is_helper_process(pid_now)
            or not is_maplestory_process(pid_now)
            or is_helper_under_cursor()
        ):
            self._focused_since = None
            self.emit_log(
                f"Skipped {kind} key {key.upper()}: target window lost focus right before send."
            )
            return False
        # The configured value is a base hold duration. Vary each tap so key-down
        # and key-up timing is not identical on every event. Native tap() accepts
        # duration in seconds.
        base_ms = float(settings.key_hold_ms)
        duration_ms = random.uniform(max(20.0, base_ms * 0.75), min(3000.0, base_ms * 1.25))
        duration_seconds = duration_ms / 1000.0
        self._injection_active.set()
        try:
            inputs.keyboard.tap(key, duration=duration_seconds, blocking=True)
        except Exception as exc:
            self.emit_log(
                f"INPUT FAILED for {kind} key {key.upper()}: {type(exc).__name__}: {exc}. "
                "Check that Auto Pot and MapleStory run at the same privilege level; "
                "Windows UIPI can block lower-integrity SendInput calls."
            )
            return False
        finally:
            self._injection_active.clear()

        suffix = f" · {detail}" if detail else ""
        self.emit_log(
            f"Windows accepted {kind} key {key.upper()} via SendInput/scancode "
            f"({duration_ms:.0f} ms randomized hold){suffix}. Game-side handling is unconfirmed."
        )
        return True

    def _monitor_typing_activity(self) -> None:
        """Sample typing keys only while ability spam is active. ~20 Hz is enough."""
        while not self.stop_event.is_set():
            settings: Settings = self.settings_getter()
            if settings.ability_spam_enabled and settings.pause_spam_while_typing:
                focused, _ = is_target_focused(settings.window_title, settings.window_id)
                if focused and not self._injection_active.is_set() and is_typing_key_held(settings.movement_keys):
                    with self._typing_lock:
                        self._last_typing_at = time.monotonic()
                self.stop_event.wait(0.05)
            else:
                # Sleep longer when spam is off so this thread is nearly idle.
                self.stop_event.wait(0.4)

    def _typing_was_recent(self, quiet_seconds: float = 1.25) -> bool:
        with self._typing_lock:
            last_typing_at = self._last_typing_at
        return last_typing_at > 0 and time.monotonic() - last_typing_at < quiet_seconds

    def _emit_telemetry(self, settings: Settings, focused: bool, movement_detected: bool = False) -> None:
        now = time.monotonic()
        # Keep GUI/overlay updates responsive when the live overlay is on so
        # "GAME ACTIVE" / "GAME IN BACKGROUND" flips promptly on focus change.
        min_interval = 1.0 if not settings.overlay_enabled else 0.18
        if now - self._last_telemetry < min_interval:
            return
        self._last_telemetry = now
        enabled_commands = []
        if settings.hp_enabled:
            enabled_commands.append(f"HP potion ({settings.hp_key.upper()})")
        if settings.mp_enabled:
            enabled_commands.append(f"MP potion ({settings.mp_key.upper()})")
        if settings.ability_spam_enabled:
            enabled_commands.append(f"Auto-cast ({settings.ability_keys.upper()})")
        payload: dict[str, Any] = {
            "focused": focused, "max_hp": settings.max_hp, "max_mp": settings.max_mp,
            "movement_detected": bool(movement_detected),
            "ability_listening": bool(settings.ability_spam_enabled),
            "movement_keys": settings.movement_keys,
            "enabled_commands": enabled_commands,
        }
        for kind in ("hp", "mp"):
            reading = self._last_reading[kind]
            stat_tuple = self._last_stats[kind]
            maximum = settings.max_hp if kind == "hp" else settings.max_mp
            stat = None
            if stat_tuple is not None:
                candidate, captured_at = stat_tuple
                age = now - captured_at
                if candidate.maximum == maximum and (
                    not focused or age <= max(1.5, settings.ocr_interval_ms / 1000.0 * 2.5)
                ):
                    stat = candidate

            if stat is not None:
                # Exact OCR is the display authority; the ratio is derived from
                # the same current/max pair so the bar and number stay aligned.
                current = int(stat.current)
                ratio = current / max(1, maximum)
                exact = True
            elif reading is not None:
                ratio = float(reading.ratio)
                current = round(maximum * ratio)
                exact = False
            else:
                # A temporarily missing frame must not erase the last seen meter.
                cached = self._last_display[kind]
                if cached is not None:
                    ratio = cached.get("ratio")
                    current = cached.get("current")
                    exact = bool(cached.get("exact"))
                else:
                    ratio = current = None
                    exact = False

            if current is not None:
                self._last_display[kind] = {
                    "current": int(current), "maximum": int(maximum),
                    "ratio": float(ratio) if ratio is not None else None,
                    "exact": bool(exact),
                }
            payload[f"{kind}_ratio"] = ratio
            payload[f"{kind}_current"] = current
            payload[f"{kind}_max"] = maximum
            payload[f"{kind}_exact"] = exact
            payload[f"{kind}_last_seen"] = self._last_seen_wall[kind]
            age = max(0.0, now - self._last_seen_mono[kind]) if self._last_seen_mono[kind] is not None else None
            payload[f"{kind}_age"] = age
            payload[f"{kind}_fresh"] = bool(focused and self._fresh_flags[kind])
        self.events.put(("telemetry", payload))

    def _grab_target_frame(self, screen, hwnd: int):
        """Grab only the HUD region after the first full-client discovery.

        The first pass is a normal desktop capture and does not activate the
        game. Once both bars are located, subsequent passes read a tight region
        around the HUD instead of copying and processing the whole game window.
        """
        if self.detector is None or not hwnd:
            return None, None, False
        rect = get_client_rect_screen(hwnd) or get_window_rect(hwnd)
        if rect is None:
            return None, None, False
        client_width = max(1, int(rect[2] - rect[0]))
        client_height = max(1, int(rect[3] - rect[1]))
        client_size = (client_width, client_height)

        if self._hud_roi_hwnd != hwnd or self._hud_roi_client_size != client_size:
            self._hud_roi_client = None
            self._hud_roi_hwnd = hwnd
            self._hud_roi_client_size = client_size
            self._hud_roi_misses = 0
            try:
                self.detector._last_locations.clear()
            except Exception:
                pass

        use_roi = self._hud_roi_client is not None
        if use_roi:
            left, top, right, bottom = self._hud_roi_client
            left = max(0, min(left, client_width - 1))
            top = max(0, min(top, client_height - 1))
            right = max(left + 1, min(right, client_width))
            bottom = max(top + 1, min(bottom, client_height))
            capture_region = {
                "left": int(rect[0]) + left,
                "top": int(rect[1]) + top,
                "width": right - left,
                "height": bottom - top,
            }
        else:
            capture_region = {
                "left": int(rect[0]),
                "top": int(rect[1]),
                "width": client_width,
                "height": client_height,
            }

        # The live overlay is a desktop window owned by MapleStory and often sits
        # on top of the HP/MP HUD. mss captures whatever is composited on screen,
        # so without hiding the overlay first the detector sees overlay pixels,
        # reports partial/zero bars, and flips between HP and MP. Only hide when
        # an overlay actually intersects the capture region to limit flicker.
        cap_left = int(capture_region["left"])
        cap_top = int(capture_region["top"])
        cap_right = cap_left + int(capture_region["width"])
        cap_bottom = cap_top + int(capture_region["height"])
        overlays_to_hide: list[int] = []
        for oh in _list_helper_overlay_hwnds():
            orect = get_window_rect(oh)
            if orect is None:
                continue
            ol, ot, oright, ob = orect
            if ol < cap_right and oright > cap_left and ot < cap_bottom and ob > cap_top:
                overlays_to_hide.append(oh)
        hidden_overlays: list[int] = []
        if overlays_to_hide and sys.platform == "win32" and _USER32 is not None:
            try:
                show = _USER32.ShowWindow
                show.argtypes = (ctypes.c_void_p, ctypes.c_int)
                show.restype = ctypes.c_int
                for oh in overlays_to_hide:
                    show(ctypes.c_void_p(oh), 0)  # SW_HIDE
                    hidden_overlays.append(oh)
                # Give the compositor a brief moment so mss does not capture
                # residual overlay pixels (common cause of 0 HP/MP reads).
                if hidden_overlays:
                    time.sleep(0.008)
            except Exception:
                hidden_overlays = []
        try:
            # mss returns BGRA; take a view of the first 3 channels to avoid an extra copy.
            bgra = np.asarray(screen.grab(capture_region))
            frame = bgra[:, :, :3]
        except Exception as exc:
            self.emit_log(f"Screen capture failed: {type(exc).__name__}: {exc}")
            return None, rect, use_roi
        finally:
            _restore_helper_overlays(hidden_overlays)
        return frame, rect, use_roi

    def _remember_hud_roi(self, readings: dict[str, BarReading | None], frame_shape: tuple[int, ...], was_roi: bool) -> None:
        """Cache a compact client-relative capture rectangle after full-frame detection."""
        if was_roi:
            if all(readings.get(kind) is not None for kind in ("hp", "mp")):
                self._hud_roi_misses = 0
            else:
                self._hud_roi_misses += 1
                if self._hud_roi_misses >= 3:
                    self._hud_roi_client = None
                    self._hud_roi_misses = 0
                    try:
                        self.detector._last_locations.clear()
                    except Exception:
                        pass
            return

        hp, mp = readings.get("hp"), readings.get("mp")
        if hp is None or mp is None or self.detector is None or not hasattr(self.detector, "templates"):
            return
        left = frame_shape[1]
        top = frame_shape[0]
        right = bottom = 0
        for reading in (hp, mp):
            template = self.detector.templates[reading.kind]
            left = min(left, reading.x - 12)
            top = min(top, reading.y - 8)
            right = max(right, reading.x + round(template.width * reading.scale) + 10)
            bottom = max(bottom, reading.y + round(template.height * reading.scale) + 8)
        client_width = self._hud_roi_client_size[0] if self._hud_roi_client_size else frame_shape[1]
        client_height = self._hud_roi_client_size[1] if self._hud_roi_client_size else frame_shape[0]
        left = max(0, min(int(left), client_width - 1))
        top = max(0, min(int(top), client_height - 1))
        right = max(left + 1, min(int(right), client_width))
        bottom = max(top + 1, min(int(bottom), client_height))
        self._hud_roi_client = (left, top, right, bottom)
        self._hud_roi_misses = 0

    def _read_captured_frame(self, frame, settings: Settings, was_roi: bool) -> tuple[BarReading | None, BarReading | None, float]:
        hp_reading = self.detector.read(frame, "hp")
        mp_reading = self.detector.read(frame, "mp")
        self._remember_hud_roi({"hp": hp_reading, "mp": mp_reading}, frame.shape, was_roi)
        now = time.monotonic()
        self._update_reading("hp", hp_reading, now)
        self._update_reading("mp", mp_reading, now)

        # OCR is intentionally asynchronous. Tesseract can take 500ms+ and must
        # never block the monitoring loop (that was the main remaining freeze).
        # OCR only runs when HUD monitoring is on; the Tesseract toggle alone is not enough.
        ocr_due = (
            settings.hud_monitoring_enabled
            and settings.auto_detect_max_stats
            and self.detector.ocr_available
            and (
                self.force_ocr_event.is_set()
                or now - self._last_ocr >= settings.ocr_interval_ms / 1000.0
            )
        )
        if ocr_due and hp_reading is not None and mp_reading is not None:
            self._schedule_ocr(frame, hp_reading, mp_reading, settings)
        return hp_reading, mp_reading, now

    def _schedule_ocr(
        self,
        frame: np.ndarray,
        hp_reading: BarReading,
        mp_reading: BarReading,
        settings: Settings,
    ) -> None:
        """Run Tesseract on a background thread; never block the capture loop."""
        with self._ocr_lock:
            if self._ocr_busy:
                return
            self._ocr_busy = True
            self._last_ocr = time.monotonic()
            if self.force_ocr_event.is_set():
                self.force_ocr_event.clear()
            # Copy the small frame so the worker owns its data.
            frame_copy = frame.copy()
            hp_copy, mp_copy = hp_reading, mp_reading
            max_hp, max_mp = settings.max_hp, settings.max_mp

        def worker() -> None:
            try:
                try:
                    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                    kernel32.SetThreadPriority(kernel32.GetCurrentThread(), -2)  # LOWEST
                except Exception:
                    pass
                detected_stats: dict[str, int] = {}
                for kind, reading, expected_max in (
                    ("hp", hp_copy, max_hp),
                    ("mp", mp_copy, max_mp),
                ):
                    if self.stop_event.is_set() or self.detector is None:
                        break
                    try:
                        stat = self.detector.read_stats(frame_copy, kind, reading)
                    except Exception:
                        continue
                    if stat is None:
                        continue
                    self._last_stats[kind] = (stat, time.monotonic())
                    if stat.maximum != expected_max:
                        detected_stats[f"max_{kind}"] = stat.maximum
                if detected_stats and not self.stop_event.is_set():
                    self.events.put(("detected_stats", detected_stats))
                    self.emit_log(
                        "Auto-detected maximum stats: "
                        + ", ".join(
                            f"{kind.upper()} {value:,}"
                            for kind, value in detected_stats.items()
                        )
                    )
            finally:
                with self._ocr_lock:
                    self._ocr_busy = False

        self._ocr_thread = threading.Thread(
            target=worker, name="OCRWorker", daemon=True
        )
        self._ocr_thread.start()

    def _capture_target_frame(self, screen, settings: Settings, hwnd: int, title: str, pid: int) -> bool:
        """Passively capture one target frame; never activate or focus the game."""
        frame, rect, was_roi = self._grab_target_frame(screen, hwnd)
        if frame is None or rect is None:
            return False
        try:
            hp_reading, mp_reading, _now = self._read_captured_frame(frame, settings, was_roi)
        except Exception as exc:
            self.emit_log(f"Initial target capture failed: {type(exc).__name__}: {exc}")
            return False

        focused = is_target_focused(settings.window_title, settings.window_id)[0]
        state = (hwnd, focused)
        rect_tuple = tuple(int(value) for value in rect)
        size = (rect_tuple[2] - rect_tuple[0], rect_tuple[3] - rect_tuple[1])
        changed = (
            state != self._last_target_state or hwnd != self._last_target_hwnd
            or rect_tuple != self._last_target_rect or size != self._last_target_client_size
            or not self._last_target_visible
        )
        self._last_target_visible = True
        self._last_target_hwnd = hwnd
        self._last_target_title = title
        self._last_target_pid = pid
        self._last_target_state = state
        self._last_target_rect = rect_tuple
        self._last_target_client_size = size
        if changed:
            self.events.put(("target_window", {
                "hwnd": hwnd, "pid": pid, "title": title, "rect": rect,
                "focused": focused, "visible": True,
            }))

        found_both = hp_reading is not None and mp_reading is not None
        # Passive/background captures must not use the live "partially detected"
        # wording — that status is reserved for focused monitoring and was the
        # confusing message users saw after tabbing to MSC Helper.
        if focused:
            if found_both:
                self.emit_status("Captured MapleStory HUD · HP and MP found")
            elif hp_reading is None and mp_reading is None:
                self.emit_status("Game found · waiting for visible HP and MP HUD")
            else:
                missing = "MP" if mp_reading is None else "HP"
                self.emit_status(f"HUD partially detected · {missing} not found")
        else:
            if found_both:
                self.emit_status(
                    "MapleStory Classic is in the background · passive HUD sample OK"
                )
            else:
                self.emit_status(
                    "MapleStory Classic is in the background · passive capture active when visible"
                )
        self._emit_telemetry(settings, focused=focused, movement_detected=False)
        return found_both

    def _initial_target_capture(self, screen, settings: Settings) -> None:
        """Find MapleStory and optionally try a passive desktop capture without changing focus."""
        matches = find_target_windows(settings.window_title)
        if not matches:
            self.emit_status("Waiting for MapleStory.exe · no matching window found")
            self.emit_log("No visible MapleStory window was found at startup. Discovery will continue in the background.")
            return

        # Never sample the HUD until MapleStory is the focused foreground window.
        self.emit_log(
            "MapleStory window found. Waiting for game focus before sampling HP/MP "
            "(no background capture)."
        )

    def _discover_background_target(self, settings: Settings) -> tuple[int, str, int] | None:
        """Discover the target window and report visibility without changing focus."""
        now = time.monotonic()
        if now - self._last_discovery_at < 1.5:
            if self._last_target_hwnd and _USER32.IsWindow(ctypes.c_void_p(self._last_target_hwnd)):
                if is_window_minimized(self._last_target_hwnd) and self._last_target_visible:
                    self._last_target_visible = False
                    self.events.put(("target_window", {
                        "hwnd": self._last_target_hwnd, "pid": self._last_target_pid,
                        "title": self._last_target_title, "rect": self._last_target_rect,
                        "focused": False, "visible": False,
                    }))
                return self._last_target_hwnd, self._last_target_title, self._last_target_pid
            return None
        self._last_discovery_at = now
        matches = find_target_windows(settings.window_title)
        if not matches:
            if self._last_target_hwnd and self._last_target_visible:
                self._last_target_visible = False
                self._last_target_state = (self._last_target_hwnd, False)
                self.events.put(("target_window", {
                    "hwnd": self._last_target_hwnd, "pid": self._last_target_pid,
                    "title": self._last_target_title, "rect": self._last_target_rect,
                    "focused": False, "visible": False,
                }))
            return None

        hwnd, title, pid = matches[0]
        minimized = is_window_minimized(hwnd)
        if minimized:
            previous_hwnd = self._last_target_hwnd
            changed = hwnd != previous_hwnd or self._last_target_visible
            self._last_target_hwnd = hwnd
            self._last_target_title = title
            self._last_target_pid = pid
            self._last_target_visible = False
            self._last_target_state = (hwnd, False)
            if changed:
                self.events.put(("target_window", {
                    "hwnd": hwnd, "pid": pid, "title": title,
                    "rect": self._last_target_rect if hwnd == previous_hwnd else None,
                    "focused": False, "visible": False,
                }))
            return hwnd, title, pid

        rect = get_client_rect_screen(hwnd) or get_window_rect(hwnd)
        if rect is None:
            return hwnd, title, pid
        size = (max(1, rect[2] - rect[0]), max(1, rect[3] - rect[1]))
        state = (hwnd, False)
        rect_tuple = tuple(int(value) for value in rect)
        changed = (
            state != self._last_target_state or size != self._last_target_client_size
            or rect_tuple != self._last_target_rect or hwnd != self._last_target_hwnd
            or not self._last_target_visible
        )
        self._last_target_state = state
        self._last_target_client_size = size
        self._last_target_rect = rect_tuple
        self._last_target_hwnd = hwnd
        self._last_target_title = title
        self._last_target_pid = pid
        self._last_target_visible = True
        if changed:
            self.events.put(("target_window", {
                "hwnd": hwnd, "pid": pid, "title": title, "rect": rect,
                "focused": False, "visible": True,
            }))
        return hwnd, title, pid

    def run(self) -> None:
        if sys.platform != "win32":
            self.emit_status("Windows required · monitoring unavailable")
            return
        if DEPENDENCY_ERROR is not None:
            self.emit_status("Missing screen-capture dependencies · install requirements.txt")
            return
        if INPUT_IMPORT_ERROR is not None:
            self.emit_status("Native keyboard inputs unavailable · check setup")
            return
        if _USER32 is None:
            self.emit_status("Could not initialize Windows foreground/input-state APIs")
            return
        try:
            if not ctypes.windll.shell32.IsUserAnAdmin():
                self.emit_status("Administrator privileges required")
                return
        except Exception:
            self.emit_status("Could not verify administrator privileges")
            return

        # Lower ONLY this worker thread. Never demote the process priority class —
        # that starves the Tk UI thread and makes Entry widgets / buttons feel dead.
        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            handle = kernel32.GetCurrentThread()
            # THREAD_PRIORITY_BELOW_NORMAL = -1 (enough to yield to the game)
            kernel32.SetThreadPriority(handle, -1)
        except Exception:
            pass

        try:
            self.detector = BarDetector(BASE_DIR / "assets")
            if not self.detector.ocr_available:
                self.emit_log("Tesseract OCR unavailable. Live tracking falls back to the less precise bar estimate; exact current/max reads need Tesseract.")
                self._ocr_warning_sent = True
            self.emit_log("Monitoring service started. Searching for MapleStory.exe; capture never changes game focus.")
            self.emit_log("Windows input backend: SendInput/scancode.")
            self._typing_monitor = threading.Thread(
                target=self._monitor_typing_activity, name="TypingActivityMonitor", daemon=True
            )
            self._typing_monitor.start()
            with mss.mss() as screen:
                self._initial_target_capture(screen, self.settings_getter())
                while not self.stop_event.is_set():
                    started = time.monotonic()
                    settings: Settings = self.settings_getter()
                    self._active_ocr_interval = settings.ocr_interval_ms
                    # Always decide focus from a fresh foreground query — never
                    # trust the short-lived cache alone for capture/input.
                    # The overlay is owned by MapleStory and WS_EX_NOACTIVATE, so
                    # the game can remain the OS foreground while the user is on
                    # the overlay. is_helper_under_cursor() treats that as unfocused
                    # so we never capture or inject unless the game is truly active.
                    title, pid, hwnd = get_foreground_window_info()
                    focused = bool(
                        hwnd
                        and pid
                        and not is_helper_title(title)
                        and not is_helper_process(pid)
                        and is_maplestory_process(pid)
                        and not is_helper_under_cursor()
                    )
                    seed_focus_cache(focused, title or "")

                    # Force a telemetry push on any focus edge so the overlay
                    # status text updates within one poll instead of waiting
                    # for the normal min_interval throttle.
                    prev_focused = getattr(self, "_prev_focused_for_telemetry", None)
                    if prev_focused is not None and prev_focused != focused:
                        self._last_telemetry = 0.0
                    self._prev_focused_for_telemetry = focused

                    movement_detected = False
                    if focused:
                        if self._focused_since is None:
                            self._focused_since = time.monotonic()
                    else:
                        self._focused_since = None

                    if focused:
                        # Re-query geometry only when the HWND changes or a few
                        # seconds have passed; continuous GetClientRect/ClientToScreen
                        # is unnecessary and contributes to desktop contention.
                        need_geometry = (
                            hwnd != self._last_target_hwnd
                            or self._last_target_rect is None
                            or (time.monotonic() - getattr(self, "_last_geometry_at", 0.0)) > 1.5
                        )
                        if need_geometry:
                            rect = get_client_rect_screen(hwnd) or get_window_rect(hwnd)
                            self._last_geometry_at = time.monotonic()
                        else:
                            rect = self._last_target_rect
                        if rect is None:
                            self.stop_event.wait(0.08)
                            continue
                        self._last_target_hwnd = hwnd
                        self._last_target_title = title
                        self._last_target_pid = pid
                        client_size = (rect[2] - rect[0], rect[3] - rect[1])
                        state = (hwnd, True)
                        if (state != self._last_target_state or client_size != self._last_target_client_size
                                or tuple(rect) != self._last_target_rect or not self._last_target_visible):
                            self._last_target_state = state
                            self._last_target_client_size = client_size
                            self._last_target_rect = tuple(rect)
                            self._last_target_visible = True
                            self.events.put(("target_window", {"hwnd": hwnd, "pid": pid, "title": title,
                                                               "rect": rect, "focused": True, "visible": True}))

                        now = time.monotonic()

                        # --- Cheap path: key polling only (no screen capture) ---
                        # This runs every loop iteration and must stay free of mss/OpenCV.
                        movement_held = False
                        movement_detected = False
                        if settings.ability_spam_enabled:
                            movement_held = is_movement_key_held(settings.movement_keys)
                            movement_detected = movement_held or is_space_key_held()
                        typing_recent = (
                            settings.ability_spam_enabled
                            and settings.pause_spam_while_typing
                            and self._typing_was_recent()
                        )
                        gate_held = movement_held and not typing_recent
                        armed_now, disarmed_now = self._movement_gate.update(
                            gate_held, now, settings.movement_hold_ms / 1000.0
                        )
                        if typing_recent and not self._typing_pause_active:
                            self._typing_pause_active = True
                            self.emit_log("Auto-cast paused: typing detected. A fresh movement hold is required to resume.")
                        elif not typing_recent and self._typing_pause_active:
                            self._typing_pause_active = False
                            self.emit_log("Typing pause cleared. Hold a configured listener key again to re-arm auto-cast.")
                        if armed_now:
                            self.emit_log(f"Auto-cast armed after {settings.movement_hold_ms} ms of continuous configured-key hold.")
                            self._next_ability_at = 0.0
                            self._ability_cycle_idx = 0
                        if disarmed_now and not typing_recent:
                            self.emit_log("Auto-cast paused because listener keys were released or focus changed.")
                        if settings.ability_spam_enabled and self._movement_gate.armed and not typing_recent:
                            now = time.monotonic()
                            if now >= self._next_ability_at:
                                keys = parse_key_list(settings.ability_keys)
                                if not keys:
                                    self._next_ability_at = time.monotonic() + 0.25
                                else:
                                    # Cycle sequentially through the ability set.
                                    idx = getattr(self, "_ability_cycle_idx", 0) % len(keys)
                                    ability_key = keys[idx]
                                    if not self.stop_event.is_set() and self._send_game_key("random spam", ability_key, settings):
                                        next_idx = idx + 1
                                        self._ability_cycle_idx = next_idx
                                        if next_idx >= len(keys):
                                            # Finished a full set — apply set delay before restarting.
                                            self._ability_cycle_idx = 0
                                            delay = settings.ability_set_delay_ms / 1000.0
                                            delay *= random.uniform(0.85, 1.15)
                                        else:
                                            delay = settings.ability_interval_ms / 1000.0
                                            delay *= random.uniform(0.80, 1.20)
                                        self._next_ability_at = time.monotonic() + max(0.03, delay)
                                    else:
                                        self._next_ability_at = time.monotonic() + 0.10

                        # --- Expensive path: screen capture only when HUD monitoring is on ---
                        # When hud_monitoring_enabled is False, zero screen grabs occur.
                        capture_interval = max(0.20, settings.hud_scan_interval_ms / 1000.0)
                        need_hud = settings.hp_enabled or settings.mp_enabled or (
                            settings.auto_detect_max_stats and settings.hud_monitoring_enabled
                        )
                        if (
                            settings.hud_monitoring_enabled
                            and need_hud
                            and now - self._last_capture_mono >= capture_interval
                        ):
                            self._last_capture_mono = now
                            screenshot, capture_rect, was_roi = self._grab_target_frame(screen, hwnd)
                            # First completed focused HUD interval unlocks potion keys.
                            if not self._hud_scan_ready:
                                self._hud_scan_ready = True
                                self.emit_log("HUD monitor completed its first scan; potion keys are now allowed.")
                            if screenshot is not None and capture_rect is not None:
                                hp_reading, mp_reading, now = self._read_captured_frame(
                                    screenshot, settings, was_roi
                                )
                                if hp_reading is None or mp_reading is None:
                                    missing_bars = []
                                    if hp_reading is None:
                                        missing_bars.append("HP")
                                    if mp_reading is None:
                                        missing_bars.append("MP")
                                    self.emit_status("HUD partially detected · " + " / ".join(missing_bars))
                                else:
                                    self.emit_status("Tracking HP and MP · game focused")

                                cooldown = settings.potion_cooldown_ms / 1000.0
                                if settings.hp_enabled and hp_reading is not None:
                                    # Reject near-zero bar reads that are almost always
                                    # capture artifacts (overlay covering the HUD, failed
                                    # hide, or partial frame). A real 0 HP would be dead
                                    # and potion keys would not matter; false zeros were
                                    # burning entire potion stacks.
                                    if hp_reading.ratio >= 0.02 and hp_reading.confidence >= 0.70:
                                        hp_current = self._current_points(
                                            "hp", settings.max_hp, hp_reading.ratio, now
                                        )
                                        if hp_current > 0:
                                            hp_threshold = self._threshold_for("hp", settings)
                                            if (
                                                self._threshold_reached(
                                                    "hp", settings, hp_current, settings.max_hp, hp_threshold
                                                )
                                                and now - self._last_hp_pot >= cooldown
                                            ):
                                                sent = self._send_game_key(
                                                    "HP potion",
                                                    settings.hp_key,
                                                    settings,
                                                    f"current {hp_current:,}/{settings.max_hp:,} · trigger {hp_threshold}",
                                                )
                                                self._last_hp_pot = time.monotonic()
                                                if sent:
                                                    self._active_thresholds["hp"] = None
                                if settings.mp_enabled and mp_reading is not None:
                                    if mp_reading.ratio >= 0.02 and mp_reading.confidence >= 0.70:
                                        mp_current = self._current_points(
                                            "mp", settings.max_mp, mp_reading.ratio, now
                                        )
                                        if mp_current > 0:
                                            mp_threshold = self._threshold_for("mp", settings)
                                            if (
                                                self._threshold_reached(
                                                    "mp", settings, mp_current, settings.max_mp, mp_threshold
                                                )
                                                and now - self._last_mp_pot >= cooldown
                                            ):
                                                sent = self._send_game_key(
                                                    "MP potion",
                                                    settings.mp_key,
                                                    settings,
                                                    f"current {mp_current:,}/{settings.max_mp:,} · trigger {mp_threshold}",
                                                )
                                                self._last_mp_pot = time.monotonic()
                                                if sent:
                                                    self._active_thresholds["mp"] = None

                        self._emit_telemetry(settings, focused=True, movement_detected=movement_detected)
                    else:
                        self._fresh_flags = {"hp": False, "mp": False}
                        # Always drop live readings when the game is not focused.
                        # Stale / partial / zero samples from the moment of tab-out
                        # were showing as "HUD partially detected" and flipping
                        # HP/MP to 0 while MSC Helper was in the foreground.
                        # Passive mode may re-sample below; until then show nothing.
                        self._last_reading = {"hp": None, "mp": None}
                        self._last_display = {"hp": None, "mp": None}
                        self._hud_scan_ready = False
                        # Never capture the game client while it is unfocused —
                        # including passive_hud_reading. Users expect zero HP/MP
                        # scans when MapleStory is in the background or the
                        # overlay/main GUI is under the cursor.
                        target = self._discover_background_target(settings)

                        _armed_now, disarmed_now = self._movement_gate.update(
                            False, time.monotonic(), settings.movement_hold_ms / 1000.0
                        )
                        if disarmed_now:
                            self.emit_log("Auto-cast paused because the target window lost focus.")
                        self._next_ability_at = 0.0
                        self._ability_cycle_idx = 0
                        self._typing_pause_active = False
                        self._hud_scan_ready = False
                        if self._last_target_state != (self._last_target_hwnd, False):
                            self._last_target_state = (self._last_target_hwnd, False)
                            self.events.put(("target_window", {
                                "hwnd": self._last_target_hwnd, "pid": self._last_target_pid,
                                "title": self._last_target_title,
                                "rect": self._last_target_rect, "focused": False,
                                "visible": self._last_target_visible,
                            }))
                        if target:
                            self.emit_status(
                                "MapleStory Classic is in the background · waiting for focus"
                            )
                        else:
                            self.emit_status("MapleStory is not running")
                        self._emit_telemetry(settings, focused=False, movement_detected=False)

                    # Keep the loop light. Ability spam needs tighter key polling;
                    # otherwise prefer a longer sleep so the game and GUI stay smooth.
                    if settings.ability_spam_enabled:
                        delay = 0.10
                    else:
                        delay = 0.28
                    elapsed = time.monotonic() - started
                    self.stop_event.wait(max(0.0, delay - elapsed))
        except Exception as exc:
            self.emit_status(f"Monitor error: {exc}")
            self.emit_log(f"Monitor stopped after error: {type(exc).__name__}: {exc}")
        finally:
            self.stop_event.set()
            if self._typing_monitor and self._typing_monitor.is_alive():
                self._typing_monitor.join(timeout=0.5)
            try:
                if inputs is not None:
                    inputs.keyboard.release_all()
            except Exception as exc:
                self.emit_log(f"Input cleanup warning: {exc}")


