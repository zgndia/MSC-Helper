from __future__ import annotations

import ctypes
import ctypes.wintypes
import json
import logging
import logging.handlers
import os
import ntpath
import queue
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any

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
    from native_keyboard_inputs import InputController, KEY_NAMES
    from native_keyboard_inputs.keys import KEY_ALIASES
except ImportError as exc:
    inputs = None
    KEY_NAMES = ()
    KEY_ALIASES = {}
    INPUT_IMPORT_ERROR = exc
else:
    # Use the built-in Windows SendInput backend with physical scan codes.
    # Keep the key registry imported even on non-Windows development hosts.
    try:
        inputs = InputController("windows", keyboard_mode="scancode")
    except Exception as exc:
        inputs = None
        INPUT_IMPORT_ERROR = exc
    else:
        INPUT_IMPORT_ERROR = None

try:
    from detector import BarDetector, BarReading, StatReading
except ImportError as exc:
    BarDetector = None
    BarReading = None
    StatReading = None
    DETECTOR_IMPORT_ERROR = exc
else:
    DETECTOR_IMPORT_ERROR = None

APP_NAME = "MSC Helper"
APP_VERSION = "0.2.1"
BASE_DIR = Path(__file__).resolve().parent
CONFIG_DIR = Path(os.environ.get("APPDATA", Path.home())) / "MSCHelper"
CONFIG_PATH = CONFIG_DIR / "settings.json"
CAPTURE_PATH = CONFIG_DIR / "last_capture.json"
LOGS_DIR = CONFIG_DIR / "logs"
VERSION_MARK_PATH = CONFIG_DIR / "VERSION"
GITHUB_VERSION_URL = "https://raw.githubusercontent.com/zgndia/MSC-Helper/main/VERSION"
GITHUB_RELEASES_API = "https://api.github.com/repos/zgndia/MSC-Helper/releases/latest"
GITHUB_REPO = "zgndia/MSC-Helper"

_file_logger: logging.Logger | None = None


def setup_file_logging() -> logging.Logger:
    """Configure rotating file logs under %APPDATA%/MSCHelper/logs."""
    global _file_logger
    if _file_logger is not None:
        return _file_logger
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("msc_helper")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    # Dated rotating log.
    log_path = LOGS_DIR / f"msc_helper_{datetime.now().strftime('%Y%m%d')}.log"
    dated = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=2_000_000, backupCount=5, encoding="utf-8"
    )
    dated.setFormatter(fmt)
    logger.addHandler(dated)
    # Always-on latest.log (truncated each run, single active session file).
    latest_path = LOGS_DIR / "latest.log"
    try:
        # Truncate so each session starts clean.
        latest_path.write_text("", encoding="utf-8")
    except OSError:
        pass
    latest = logging.FileHandler(latest_path, mode="a", encoding="utf-8")
    latest.setFormatter(fmt)
    logger.addHandler(latest)
    logger.propagate = False
    _file_logger = logger
    logger.info("MSC Helper %s started", APP_VERSION)
    return logger


def get_logger() -> logging.Logger:
    return _file_logger or setup_file_logging()


@dataclass(frozen=True)
class Settings:
    """User-configurable options. Field names describe the job they control."""
    window_title: str = "MapleStory"
    max_hp: int = 652
    max_mp: int = 516
    # Potion trigger: how much HP/MP must be missing (or % remaining) before pressing the key.
    hp_threshold: float = 100
    mp_threshold: float = 200
    hp_threshold_mode: str = "points"       # "points" (missing) | "percent" (remaining)
    mp_threshold_mode: str = "points"
    hp_random_offset: int = 75
    mp_random_offset: int = 75
    hp_enabled: bool = True
    mp_enabled: bool = True
    hp_key: str = "v"
    mp_key: str = "t"
    potion_cooldown_ms: int = 1000
    # How often the HUD region is captured while the game is focused (ms).
    # Default is responsive; the capture path itself is cheap (ROI + local match).
    hud_scan_interval_ms: int = 500
    key_hold_ms: int = 75
    window_id: int = 0
    # When True, Tesseract OCR is used for exact current/max readings and max-stat detection.
    auto_detect_max_stats: bool = True
    hud_monitoring_enabled: bool = True
    # When True, sample HP/MP while MapleStory is visible but not focused.
    # Off by default — background reads can under-report fill and force potions on focus.
    passive_hud_reading: bool = False
    # Require the game to stay focused this long before any key is injected.
    min_focus_hold_ms: int = 1000
    ocr_interval_ms: int = 1800
    ability_spam_enabled: bool = False
    ability_keys: str = "q, e, r"
    movement_keys: str = "w, a, s, d"
    ability_interval_ms: int = 250
    ability_set_delay_ms: int = 800
    movement_hold_ms: int = 1250
    pause_spam_while_typing: bool = True
    overlay_enabled: bool = True
    overlay_x_offset: int = 18
    overlay_y_offset: int = 42
    overlay_width: int = 390
    overlay_height: int = 315
    auto_update_enabled: bool = True
    last_window_title: str = ""
    last_window_pid: int = 0
    last_window_hwnd: int = 0


_USER32 = None
if sys.platform == "win32":
    try:
        _USER32 = ctypes.WinDLL("user32", use_last_error=True)
        _USER32.GetForegroundWindow.restype = ctypes.c_void_p
        _USER32.GetWindowTextLengthW.argtypes = (ctypes.c_void_p,)
        _USER32.GetWindowTextLengthW.restype = ctypes.c_int
        _USER32.GetWindowTextW.argtypes = (ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int)
        _USER32.GetWindowTextW.restype = ctypes.c_int
        _USER32.GetWindowThreadProcessId.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong))
        _USER32.GetWindowThreadProcessId.restype = ctypes.c_ulong
        _USER32.GetAsyncKeyState.argtypes = (ctypes.c_int,)
        _USER32.GetAsyncKeyState.restype = ctypes.c_short
        _USER32.GetWindowRect.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.wintypes.RECT))
        _USER32.GetWindowRect.restype = ctypes.c_int
        _USER32.IsIconic.argtypes = (ctypes.c_void_p,)
        _USER32.IsIconic.restype = ctypes.c_int
        _USER32.IsWindow.argtypes = (ctypes.c_void_p,)
        _USER32.IsWindow.restype = ctypes.c_int
        _KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except Exception:
        _USER32 = None
        _KERNEL32 = None
else:
    _KERNEL32 = None


def get_window_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    """Return a live window rectangle (left, top, right, bottom)."""
    if sys.platform != "win32" or _USER32 is None or not hwnd:
        return None
    rect = ctypes.wintypes.RECT()
    try:
        if not _USER32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(rect)):
            return None
        return int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)
    except Exception:
        return None


def _list_helper_overlay_hwnds() -> list[int]:
    """Visible top-level windows owned by this process (the live status overlay).

    The overlay is WS_EX_NOACTIVATE and owned by the game, so desktop capture of
    the game client includes its pixels whenever it covers the HUD. Callers hide
    these briefly around an mss grab so HP/MP template matching sees the real bars.
    """
    if sys.platform != "win32" or _USER32 is None:
        return []
    found: list[int] = []
    enum_windows = _USER32.EnumWindows
    enum_windows.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
    enum_windows.restype = ctypes.c_int
    is_visible = _USER32.IsWindowVisible
    is_visible.argtypes = (ctypes.c_void_p,)
    is_visible.restype = ctypes.c_int
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def visit(hwnd, _lparam):
        if not is_visible(hwnd):
            return True
        pid = ctypes.c_ulong()
        _USER32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if int(pid.value) != OUR_PID:
            return True
        # Prefer toolwindow / noactivate overlays; skip the main Tk frame if possible
        # by requiring an empty or short caption (overrideredirect overlays).
        length = _USER32.GetWindowTextLengthW(hwnd)
        if length > 0:
            buffer = ctypes.create_unicode_buffer(length + 1)
            _USER32.GetWindowTextW(hwnd, buffer, len(buffer))
            title = buffer.value or ""
            # Main app title contains "MSC Helper"; the overlay is captionless.
            if is_helper_title(title):
                return True
        found.append(int(hwnd))
        return True

    enum_windows(callback_type(visit), 0)
    return found


def _hide_helper_overlays() -> list[int]:
    """Hide our overlay windows for a clean desktop capture. Returns hwnds to restore."""
    if sys.platform != "win32" or _USER32 is None:
        return []
    hidden: list[int] = []
    try:
        show = _USER32.ShowWindow
        show.argtypes = (ctypes.c_void_p, ctypes.c_int)
        show.restype = ctypes.c_int
        for hwnd in _list_helper_overlay_hwnds():
            # SW_HIDE = 0
            show(ctypes.c_void_p(hwnd), 0)
            hidden.append(hwnd)
    except Exception:
        pass
    return hidden


def _restore_helper_overlays(hwnds: list[int]) -> None:
    """Show overlay windows again without activating them (SW_SHOWNOACTIVATE = 4)."""
    if sys.platform != "win32" or _USER32 is None or not hwnds:
        return
    try:
        show = _USER32.ShowWindow
        show.argtypes = (ctypes.c_void_p, ctypes.c_int)
        show.restype = ctypes.c_int
        for hwnd in hwnds:
            show(ctypes.c_void_p(hwnd), 4)
    except Exception:
        pass


def is_helper_under_cursor() -> bool:
    """True when the mouse is over any window belonging to this process.

    The live overlay is WS_EX_NOACTIVATE and owned by MapleStory, so clicking or
    resting on it does not change the OS foreground window — the game stays
    "focused" and monitoring would keep capturing. Treating helper-under-cursor
    as not game-focused stops HP/MP tracking while the user is on the overlay
    or the main GUI.

    While the user is actively dragging/resizing the overlay we temporarily
    ignore this check so status stays "GAME ACTIVE" and HUD reads continue.
    """
    if _OVERLAY_INTERACTING:
        return False
    if sys.platform != "win32" or _USER32 is None:
        return False
    try:
        _USER32.GetCursorPos.argtypes = (ctypes.POINTER(ctypes.wintypes.POINT),)
        _USER32.GetCursorPos.restype = ctypes.c_int
        _USER32.WindowFromPoint.argtypes = (ctypes.wintypes.POINT,)
        _USER32.WindowFromPoint.restype = ctypes.c_void_p
        point = ctypes.wintypes.POINT()
        if not _USER32.GetCursorPos(ctypes.byref(point)):
            return False
        hwnd = _USER32.WindowFromPoint(point)
        if not hwnd:
            return False
        # Walk up the parent chain; WindowFromPoint can hit a child control.
        get_parent = _USER32.GetParent
        get_parent.argtypes = (ctypes.c_void_p,)
        get_parent.restype = ctypes.c_void_p
        current = int(hwnd)
        for _ in range(8):
            if not current:
                break
            pid = ctypes.c_ulong()
            _USER32.GetWindowThreadProcessId(ctypes.c_void_p(current), ctypes.byref(pid))
            if int(pid.value) == OUR_PID:
                return True
            parent = get_parent(ctypes.c_void_p(current))
            if not parent or int(parent) == current:
                # Also check owner (GWLP_HWNDPARENT) for owned popups.
                break
            current = int(parent)
        # Owned overlay: parent walk may stop; check root owner via GetAncestor.
        try:
            get_ancestor = _USER32.GetAncestor
            get_ancestor.argtypes = (ctypes.c_void_p, ctypes.c_uint)
            get_ancestor.restype = ctypes.c_void_p
            GA_ROOT = 2
            root = get_ancestor(hwnd, GA_ROOT)
            if root:
                pid = ctypes.c_ulong()
                _USER32.GetWindowThreadProcessId(ctypes.c_void_p(int(root)), ctypes.byref(pid))
                if int(pid.value) == OUR_PID:
                    return True
        except Exception:
            pass
        return False
    except Exception:
        return False


def get_client_rect_screen(hwnd: int) -> tuple[int, int, int, int] | None:
    """Return the target window's client rectangle in desktop coordinates."""
    if sys.platform != "win32" or _USER32 is None or not hwnd:
        return None
    try:
        client = ctypes.wintypes.RECT()
        if not _USER32.GetClientRect(ctypes.c_void_p(hwnd), ctypes.byref(client)):
            return None
        point = ctypes.wintypes.POINT(0, 0)
        _USER32.ClientToScreen.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.wintypes.POINT))
        _USER32.ClientToScreen.restype = ctypes.c_int
        if not _USER32.ClientToScreen(ctypes.c_void_p(hwnd), ctypes.byref(point)):
            return None
        left, top = int(point.x), int(point.y)
        return left, top, left + int(client.right), top + int(client.bottom)
    except Exception:
        return None


def is_window_minimized(hwnd: int) -> bool:
    if sys.platform != "win32" or _USER32 is None or not hwnd:
        return False
    try:
        return bool(_USER32.IsIconic(ctypes.c_void_p(hwnd)))
    except Exception:
        return False


TARGET_EXECUTABLES = {
    "maplestory.exe",
    "maplestory_classic.exe",  # MapleStory Classic client
}
# Never treat our own process (or typical helper hosts) as the game target.
OUR_PID = os.getpid()
HELPER_EXECUTABLES = {
    "mschelper.exe",
    "python.exe",
    "pythonw.exe",
    "py.exe",
}
# Titles that must never be treated as the game (case-insensitive substring).
HELPER_TITLE_MARKERS = (
    "msc helper",
    "mschelper",
)

# Process image paths are immutable for the lifetime of a PID. Cache them briefly
# so foreground checks do not repeatedly open/query the same process.
_PROCESS_EXE_CACHE: dict[int, tuple[float, str]] = {}


def is_helper_title(title: str) -> bool:
    """True when the window caption belongs to this helper, not MapleStory."""
    if not title:
        return False
    lowered = title.casefold()
    return any(marker in lowered for marker in HELPER_TITLE_MARKERS)


def is_helper_process(process_id: int) -> bool:
    """True for this app's PID or any known helper-host executable."""
    if not process_id:
        return False
    if int(process_id) == OUR_PID:
        return True
    executable = ntpath.basename(get_process_executable(int(process_id))).casefold()
    return bool(executable) and executable in HELPER_EXECUTABLES


def is_maplestory_process(process_id: int) -> bool:
    """True only when the process image is a MapleStory client and not this helper."""
    if not process_id or is_helper_process(process_id):
        return False
    executable = ntpath.basename(get_process_executable(int(process_id))).casefold()
    if not executable or executable in HELPER_EXECUTABLES:
        return False
    return executable in TARGET_EXECUTABLES


VK_MOVEMENT_KEYS = (0x57, 0x41, 0x53, 0x44, 0x20)  # W / A / S / D / Space
# Poll printable keys and common text-editing keys only. No keyboard hook is
# installed and key values/text are never read or stored.
VK_TYPING_KEYS = (
    *range(0x30, 0x3A),  # top-row digits
    *(vk for vk in range(0x41, 0x5B) if vk not in VK_MOVEMENT_KEYS),  # letters except movement keys
    *range(0x60, 0x70),  # numpad digits/operators
    0x08, 0x09, 0x0D,    # Backspace, Tab, Enter
    0xBA, 0xBB, 0xBC, 0xBD, 0xBE, 0xBF, 0xC0,
    0xDB, 0xDC, 0xDD, 0xDE, 0xE2,  # OEM punctuation
)


def get_foreground_window_info() -> tuple[str, int, int]:
    """Return (title, process_id, HWND) for the current foreground window."""
    if sys.platform != "win32" or _USER32 is None:
        return "", 0, 0
    hwnd = _USER32.GetForegroundWindow()
    if not hwnd:
        return "", 0, 0
    length = _USER32.GetWindowTextLengthW(hwnd)
    buffer = ctypes.create_unicode_buffer(max(1, length + 1))
    _USER32.GetWindowTextW(hwnd, buffer, len(buffer))
    process_id = ctypes.c_ulong()
    _USER32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
    return buffer.value, int(process_id.value), int(hwnd)


def get_foreground_window_title() -> str:
    # Compatibility helper retained for callers that only need the title.
    return get_foreground_window_info()[0]


def get_process_executable(process_id: int) -> str:
    """Return the executable path for a process, or an empty string on failure."""
    if sys.platform != "win32" or not process_id:
        return ""
    now = time.monotonic()
    cached = _PROCESS_EXE_CACHE.get(int(process_id))
    if cached is not None and now < cached[0]:
        return cached[1]

    kernel32 = getattr(ctypes, "windll", None)
    kernel32 = kernel32.kernel32 if kernel32 is not None else _KERNEL32
    if kernel32 is None:
        return ""
    kernel32.OpenProcess.argtypes = (ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.QueryFullProcessImageNameW.argtypes = (ctypes.c_void_p, ctypes.c_ulong, ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_ulong))
    kernel32.QueryFullProcessImageNameW.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
    kernel32.CloseHandle.restype = ctypes.c_int
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, process_id)
    if not handle:
        _PROCESS_EXE_CACHE[int(process_id)] = (now + 0.5, "")
        return ""
    try:
        size = ctypes.c_ulong(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            result = ""
        else:
            result = buffer.value
    finally:
        kernel32.CloseHandle(handle)
    _PROCESS_EXE_CACHE[int(process_id)] = (now + 2.0, result)
    return result


def find_target_windows(window_title_fragment: str) -> list[tuple[int, str, int]]:
    """Find visible top-level windows owned by MapleStory.exe.

    The process image is authoritative. The configured title is only a
    preference, not a requirement, because game builds/launchers can change
    the caption while keeping the same executable.
    """
    if sys.platform != "win32" or _USER32 is None:
        return []

    matches: list[tuple[int, str, int]] = []
    enum_windows = _USER32.EnumWindows
    enum_windows.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
    enum_windows.restype = ctypes.c_int
    is_visible = _USER32.IsWindowVisible
    is_visible.argtypes = (ctypes.c_void_p,)
    is_visible.restype = ctypes.c_int
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def visit(hwnd, _lparam):
        if not is_visible(hwnd):
            return True
        length = _USER32.GetWindowTextLengthW(hwnd)
        buffer = ctypes.create_unicode_buffer(max(1, length + 1))
        _USER32.GetWindowTextW(hwnd, buffer, len(buffer))
        title = buffer.value
        if is_helper_title(title):
            return True
        pid = ctypes.c_ulong()
        _USER32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        pid_int = int(pid.value)
        if is_helper_process(pid_int):
            return True
        if is_maplestory_process(pid_int):
            matches.append((int(hwnd), title, pid_int))
        return True

    callback = callback_type(visit)
    enum_windows(callback, 0)
    wanted = (window_title_fragment or "").casefold().strip()
    if wanted:
        matches.sort(key=lambda item: 0 if wanted in item[1].casefold() else 1)
    return matches


# Short-lived cache for focus checks so the hot loop does not re-open the
# process handle on every 80–250 ms tick. Keep TTL short so a helper↔game
# focus switch is noticed quickly; input paths always invalidate + re-query.
_FOCUS_CACHE: tuple[float, bool, str] = (0.0, False, "")
_FOCUS_CACHE_TTL = 0.05  # seconds
# Set while the user is dragging or resizing the live overlay so the
# under-cursor check does not force "game in background" or block HUD reads.
_OVERLAY_INTERACTING = False


def is_target_focused(window_title_fragment: str, window_id: int = 0) -> tuple[bool, str]:
    """Accept input only when the foreground HWND belongs to a MapleStory client.

    window_id is retained for settings-file compatibility but ignored because
    process IDs and window handles change between launches. MSC Helper's own
    windows (by PID, executable, or title) are never treated as the game target.
    """
    global _FOCUS_CACHE
    now = time.monotonic()
    cached_at, cached_ok, cached_title = _FOCUS_CACHE
    if now - cached_at < _FOCUS_CACHE_TTL:
        # Even a fresh cache entry can be wrong across a fast helper↔game
        # focus switch. Reject known helper titles without waiting for TTL.
        if cached_ok and is_helper_title(cached_title):
            _FOCUS_CACHE = (now, False, cached_title)
            return False, cached_title
        return cached_ok, cached_title

    title, foreground_pid, hwnd = get_foreground_window_info()
    if not hwnd or not foreground_pid:
        _FOCUS_CACHE = (now, False, title)
        return False, title
    if sys.platform != "win32":
        _FOCUS_CACHE = (now, False, title)
        return False, title
    # Process image is the primary identity check. Also reject this helper by
    # title / under-cursor so the NOACTIVATE overlay cannot leave the game
    # "focused" while the user is on MSC Helper UI.
    if (
        is_helper_title(title)
        or is_helper_process(foreground_pid)
        or is_helper_under_cursor()
    ):
        ok = False
    else:
        ok = is_maplestory_process(foreground_pid)
    _FOCUS_CACHE = (now, ok, title)
    return ok, title


def invalidate_focus_cache() -> None:
    global _FOCUS_CACHE
    _FOCUS_CACHE = (0.0, False, "")


def seed_focus_cache(ok: bool, title: str = "") -> None:
    """Force the focus cache to a known state (used after a fresh foreground query)."""
    global _FOCUS_CACHE
    _FOCUS_CACHE = (time.monotonic(), bool(ok), title or "")


MOVEMENT_VK_NAMES = {
    "space": 0x20, "tab": 0x09, "enter": 0x0D, "escape": 0x1B,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "left_shift": 0xA0, "right_shift": 0xA1,
    "left_ctrl": 0xA2, "right_ctrl": 0xA3,
    "left_alt": 0xA4, "right_alt": 0xA5,
    "caps_lock": 0x14, "backspace": 0x08,
}


def key_to_virtual_key(name: str) -> int:
    """Map a supported physical key name to its Windows virtual-key code."""
    key = canonical_key(name)
    if len(key) == 1 and key.isalnum():
        return ord(key.upper())
    if key in MOVEMENT_VK_NAMES:
        return MOVEMENT_VK_NAMES[key]
    if key.startswith("f") and key[1:].isdigit() and 1 <= int(key[1:]) <= 24:
        return 0x70 + int(key[1:]) - 1
    raise ValueError(f"{name!r} cannot be monitored as a physical key. Use letters, digits, arrows, Space, Shift, Ctrl, Alt, or F1-F24.")


def parse_monitor_keys(value: str) -> list[tuple[str, int]]:
    raw_items = [item.strip() for item in value.split(",") if item.strip()]
    if not raw_items:
        raise ValueError("Enter at least one movement/listener key, separated by commas.")
    result: list[tuple[str, int]] = []
    seen: set[str] = set()
    for item in raw_items:
        key = canonical_key(item)
        if key not in seen:
            result.append((key, key_to_virtual_key(key)))
            seen.add(key)
    return result


def _held_virtual_keys(codes: list[int] | tuple[int, ...] | set[int]) -> bool:
    if sys.platform != "win32" or _USER32 is None:
        return False
    return any(_USER32.GetAsyncKeyState(vk) & 0x8000 for vk in codes)


def is_movement_key_held(movement_keys: str = "w, a, s, d") -> bool:
    """Return whether a configured movement/listener key is physically held."""
    try:
        codes = [vk for _name, vk in parse_monitor_keys(movement_keys)]
    except ValueError:
        codes = list(VK_MOVEMENT_KEYS[:4])
    return _held_virtual_keys(codes)


def is_space_key_held() -> bool:
    return _held_virtual_keys((0x20,))


def is_typing_key_held(movement_keys: str = "w, a, s, d") -> bool:
    """Detect likely in-game typing without installing a hook or reading text."""
    try:
        ignored = {vk for _name, vk in parse_monitor_keys(movement_keys)} | {0x20}
    except ValueError:
        ignored = set(VK_MOVEMENT_KEYS)
    return _held_virtual_keys(tuple(vk for vk in VK_TYPING_KEYS if vk not in ignored))


def canonical_key(name: str) -> str:
    value = re.sub(r"[\s-]+", "_", name.strip().lower())
    canonical = KEY_ALIASES.get(value, value)
    if canonical not in KEY_NAMES:
        raise ValueError(f"Unknown key {name!r}. Use a supported key name from Native keyboard inputs.")
    return canonical


def parse_key_list(value: str) -> list[str]:
    raw_items = [item.strip() for item in value.split(",") if item.strip()]
    keys: list[str] = []
    for raw in raw_items:
        key = canonical_key(raw)
        if key not in keys:
            keys.append(key)
    if not keys:
        raise ValueError("Enter at least one ability/pickup key, separated by commas.")
    return keys


def load_settings() -> Settings:
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return Settings()
        # Migrate legacy field names from older builds.
        legacy_map = {
            "hp_missing": "hp_threshold",
            "mp_missing": "mp_threshold",
            "cooldown_ms": "potion_cooldown_ms",
            "scan_interval_ms": "hud_scan_interval_ms",
            "key_press_ms": "key_hold_ms",
            "auto_detect_stats": "auto_detect_max_stats",
        }
        for old, new in legacy_map.items():
            if old in data and new not in data:
                data[new] = data.pop(old)
            else:
                data.pop(old, None)
        allowed = Settings.__dataclass_fields__.keys()
        values = {k: v for k, v in data.items() if k in allowed}
        # Floor extremely aggressive intervals so a bad settings file cannot starve the UI/game.
        ocr = values.get("ocr_interval_ms")
        if isinstance(ocr, (int, float)) and ocr < 800:
            values["ocr_interval_ms"] = 1800
        scan = values.get("hud_scan_interval_ms")
        if isinstance(scan, (int, float)) and scan < 200:
            values["hud_scan_interval_ms"] = 500
        return Settings(**values)
    except (OSError, ValueError, TypeError):
        return Settings()


def save_settings(settings: Settings) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(asdict(settings), indent=2), encoding="utf-8")


class MovementGate:
    """Arms only after a movement-key group is held continuously."""

    def __init__(self) -> None:
        self.started_at: float | None = None
        self.armed = False

    def update(self, held: bool, now: float, hold_seconds: float) -> tuple[bool, bool]:
        """Return (armed_now, disarmed_now)."""
        if not held:
            was_armed = self.armed
            self.started_at = None
            self.armed = False
            return False, was_armed
        if self.started_at is None:
            self.started_at = now
        if not self.armed and now - self.started_at >= hold_seconds:
            self.armed = True
            return True, False
        return False, False


class MonitorWorker(threading.Thread):
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

    def _update_reading(self, kind: str, reading: BarReading | None, now: float) -> None:
        self._fresh_flags[kind] = reading is not None
        if reading is not None:
            self._last_reading[kind] = reading
            self._last_seen_mono[kind] = now
            self._last_seen_wall[kind] = datetime.now().strftime("%H:%M:%S")

    def _current_points(self, kind: str, maximum: int, ratio: float, now: float) -> int:
        """Prefer a recent exact OCR reading; use the bar ratio between OCR samples."""
        stat_tuple = self._last_stats.get(kind)
        if stat_tuple is not None:
            stat, captured_at = stat_tuple
            age = now - captured_at
            if stat.maximum == maximum and -0.30 <= age <= 1.5:
                return int(stat.current)
        return round(maximum * ratio)

    def _randomized_threshold(self, kind: str, settings: Settings) -> float | int:
        """Roll once per potion cycle; the threshold stays fixed until a pot is sent."""
        maximum = settings.max_hp if kind == "hp" else settings.max_mp
        base = settings.hp_threshold if kind == "hp" else settings.mp_threshold
        offset = settings.hp_random_offset if kind == "hp" else settings.mp_random_offset
        mode = settings.hp_threshold_mode if kind == "hp" else settings.mp_threshold_mode
        if mode == "percent":
            # Keep offsets configured in resource points. Convert them into a
            # percentage-point jitter so the same 75-point setting remains
            # proportionate when the player changes level/max stats.
            delta = (float(offset) / max(1, maximum)) * 100.0
            return round(max(30.0, min(100.0, random.uniform(float(base) - delta, float(base) + delta))), 2)
        rolled = int(round(float(base) + random.randint(-int(offset), int(offset))))
        # In points/missing mode, 70% missing is the latest safe trigger:
        # the pot must be used while at least 30% of the resource remains.
        latest_safe_missing = max(1, int(maximum * 0.70))
        return max(1, min(latest_safe_missing, rolled))

    def _threshold_for(self, kind: str, settings: Settings) -> float | int:
        signature = (
            settings.max_hp, settings.max_mp, settings.hp_threshold, settings.mp_threshold,
            settings.hp_threshold_mode, settings.mp_threshold_mode,
            settings.hp_random_offset, settings.mp_random_offset,
        )
        if signature != self._threshold_signature:
            self._active_thresholds = {"hp": None, "mp": None}
            self._threshold_signature = signature
        value = self._active_thresholds[kind]
        if value is None:
            value = self._randomized_threshold(kind, settings)
            self._active_thresholds[kind] = value
            self.emit_log(f"{kind.upper()} randomized potion threshold set to {value}{'%' if getattr(settings, kind + '_threshold_mode') == 'percent' else ' missing points'}.")
        return value

    @staticmethod
    def _threshold_reached(kind: str, settings: Settings, current: int, maximum: int, threshold: float | int) -> bool:
        mode = settings.hp_threshold_mode if kind == "hp" else settings.mp_threshold_mode
        if mode == "percent":
            return (current / max(1, maximum)) * 100.0 <= float(threshold)
        return (maximum - current) >= int(threshold)

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


class Meter(tk.Canvas):
    def __init__(self, master, color: str, **kwargs):
        # Allow callers to override the default height without passing it twice
        # to tkinter.Canvas.__init__.
        height = kwargs.pop("height", 13)
        super().__init__(master, height=height, highlightthickness=0, bd=0,
                         bg=kwargs.pop("bg", "#252525"), **kwargs)
        self.color = color
        self.ratio = 0.0
        self._background_id = None
        self._fill_id = None
        self._last_size = (0, 0)
        self.bind("<Configure>", self._on_configure)
        self.redraw()

    def _on_configure(self, _event=None) -> None:
        # Only redraw when the canvas actually changed size.
        width = max(1, self.winfo_width())
        height = max(1, self.winfo_height())
        if (width, height) != self._last_size:
            self.redraw()

    def set_ratio(self, ratio: float | None) -> None:
        # Ignore sub-pixel changes and missing frames. Updating two existing
        # canvas items is substantially cheaper than deleting/recreating them.
        if ratio is None:
            return
        ratio = max(0.0, min(1.0, float(ratio)))
        if abs(ratio - self.ratio) < 0.003:
            return
        self.ratio = ratio
        self.redraw()

    def redraw(self) -> None:
        width = max(1, self.winfo_width())
        height = max(1, self.winfo_height())
        if self._background_id is None:
            self._background_id = self.create_rectangle(
                0, 0, width, height, fill="#3a3a3a", outline=""
            )
            self._fill_id = self.create_rectangle(
                0, 0, 0, height, fill=self.color, outline=""
            )
        elif self._last_size != (width, height):
            self.coords(self._background_id, 0, 0, width, height)
        # Always update the fill geometry; the ratio gate already filtered noise.
        self.coords(self._fill_id, 0, 0, width * self.ratio, height)
        self._last_size = (width, height)


class GameOverlay:
    """Separate non-activating popup owned by MapleStory (never SetParent-ed).

    The popup stays attached to the target's normal z-order while the game is
    visible, whether or not it is the foreground window. It is not globally
    topmost and never joins the game's child-window/message hierarchy.
    """

    MIN_WIDTH, MIN_HEIGHT = 360, 300
    MAX_WIDTH, MAX_HEIGHT = 600, 500

    def __init__(self, app: "MapleHelperApp"):
        self.app = app
        self.root = app.root
        self.target_rect: tuple[int, int, int, int] | None = None
        self.target_hwnd = 0
        self._native_hwnd = 0
        self._attached = False
        self._focused = False
        self._is_shown = False
        self._client_width = 0
        self._client_height = 0
        self._child_x = 18
        self._child_y = 42
        self._width = 390
        self._height = 315
        self._drag_start: tuple[int, int, int, int] | None = None
        self._resize_start: tuple[int, int, int, int] | None = None
        self.data: dict[str, Any] = {}
        self._last_render_key: tuple[Any, ...] | None = None

        self.window = tk.Toplevel(self.root)
        self.window.withdraw()
        self.window.overrideredirect(True)
        self.window.configure(bg="#101010")
        self.window.minsize(self.MIN_WIDTH, self.MIN_HEIGHT)
        self.window.maxsize(self.MAX_WIDTH, self.MAX_HEIGHT)

        # Layout: footer first (always visible resize grip), then header, then body.
        self._border = tk.Frame(self.window, bg="#555555", bd=0, highlightthickness=1, highlightbackground="#777777")
        self._border.pack(fill="both", expand=True)

        self._footer = tk.Frame(self._border, bg="#303030", height=20)
        self._footer.pack(fill="x", side="bottom")
        self._footer.pack_propagate(False)
        self.footer_label = tk.Label(
            self._footer,
            text="Drag header to move  ·  Resize bottom-right",
            bg="#303030",
            fg="#a0a0a0",
            font=("Segoe UI", 7),
            anchor="w",
            padx=8,
        )
        self.footer_label.pack(side="left", fill="both", expand=True)
        self.grip = tk.Label(
            self._footer,
            text="◢",
            bg="#303030",
            fg="#cccccc",
            cursor="size_nw_se",
            font=("Segoe UI", 10),
            padx=6,
        )
        self.grip.pack(side="right", fill="y")
        self.grip.bind("<ButtonPress-1>", self._start_resize)
        self.grip.bind("<B1-Motion>", self._resize)
        self.grip.bind("<ButtonRelease-1>", self._end_resize)

        self._header = tk.Frame(self._border, bg="#303030", height=28)
        self._header.pack(fill="x", side="top")
        self._header.pack_propagate(False)
        self.title_label = tk.Label(
            self._header,
            text="MSC HELPER  |  LIVE STATUS",
            bg="#303030",
            fg="#eeeeee",
            font=("Segoe UI Semibold", 9),
            anchor="w",
            padx=10,
        )
        self.title_label.pack(side="left", fill="both", expand=True)
        self.connection_label = tk.Label(
            self._header,
            text="WAITING",
            bg="#303030",
            fg="#d6ad62",
            font=("Segoe UI", 8, "bold"),
            padx=8,
        )
        self.connection_label.pack(side="right", fill="y")
        for widget in (self._header, self.title_label, self.connection_label):
            widget.bind("<ButtonPress-1>", self._start_drag)
            widget.bind("<B1-Motion>", self._drag)
            widget.bind("<ButtonRelease-1>", self._end_drag)

        self._body = tk.Frame(self._border, bg="#1f1f1f", padx=10, pady=8)
        self._body.pack(fill="both", expand=True)
        self.hp_label = tk.Label(
            self._body, text="HP  |  WAITING FOR HUD", bg="#1f1f1f", fg="#f08080",
            font=("Segoe UI Semibold", 10), anchor="w",
        )
        self.hp_label.pack(fill="x", pady=(0, 2))
        self.hp_detail = tk.Label(
            self._body, text="— / —", bg="#1f1f1f", fg="#e5e5e5",
            font=("Consolas", 10), anchor="w",
        )
        self.hp_detail.pack(fill="x", pady=(0, 4))
        self.mp_label = tk.Label(
            self._body, text="MP  |  WAITING FOR HUD", bg="#1f1f1f", fg="#84c4a1",
            font=("Segoe UI Semibold", 10), anchor="w",
        )
        self.mp_label.pack(fill="x", pady=(0, 2))
        self.mp_detail = tk.Label(
            self._body, text="— / —", bg="#1f1f1f", fg="#e5e5e5",
            font=("Consolas", 10), anchor="w",
        )
        self.mp_detail.pack(fill="x", pady=(0, 4))
        self.listener_label = tk.Label(
            self._body, text="KEY LISTENER OFF", bg="#1f1f1f", fg="#a9a9a9",
            font=("Segoe UI", 9), anchor="w",
        )
        self.listener_label.pack(fill="x", pady=(2, 2))
        self.movement_label = tk.Label(
            self._body, text="", bg="#1f1f1f", fg="#e7c46d",
            font=("Segoe UI Semibold", 9), anchor="w",
        )
        self.movement_label.pack(fill="x", pady=(0, 2))
        self.commands_label = tk.Label(
            self._body,
            text="ENABLED COMMANDS\n• None",
            bg="#1f1f1f",
            fg="#dddddd",
            font=("Segoe UI", 9),
            anchor="nw",
            justify="left",
            wraplength=200,
        )
        self.commands_label.pack(fill="both", expand=True, pady=(2, 0))

        # Keep layout fixed; minimum size prevents crushed widgets.
    @staticmethod
    def _win_api():
        if sys.platform != "win32":
            return None
        return _USER32 or ctypes.WinDLL("user32", use_last_error=True)

    def _ensure_owned_popup(self, hwnd: int) -> bool:
        """Set a top-level owner relationship; never call SetParent."""
        if sys.platform != "win32" or not hwnd:
            return False
        user32 = self._win_api()
        if user32 is None:
            return False
        try:
            self.window.update_idletasks()
            self.window.winfo_id()  # force HWND creation without showing or activating it
            self._native_hwnd = int(self.window.wm_frame(), 0)
            overlay = ctypes.c_void_p(self._native_hwnd)
            GWL_STYLE, GWL_EXSTYLE, GWLP_HWNDPARENT = -16, -20, -8
            WS_POPUP = 0x80000000
            WS_CHILD = 0x40000000
            WS_CAPTION = 0x00C00000
            WS_THICKFRAME = 0x00040000
            WS_SYSMENU = 0x00080000
            WS_MINIMIZEBOX = 0x00020000
            WS_MAXIMIZEBOX = 0x00010000
            WS_EX_NOACTIVATE = 0x08000000
            WS_EX_TOOLWINDOW = 0x00000080
            WS_EX_APPWINDOW = 0x00040000

            get_long = getattr(user32, "GetWindowLongPtrW", None) or user32.GetWindowLongW
            set_long = getattr(user32, "SetWindowLongPtrW", None) or user32.SetWindowLongW
            get_long.argtypes = (ctypes.c_void_p, ctypes.c_int)
            get_long.restype = ctypes.c_ssize_t
            set_long.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t)
            set_long.restype = ctypes.c_ssize_t

            style = int(get_long(overlay, GWL_STYLE))
            style = (style | WS_POPUP) & ~(
                WS_CHILD | WS_CAPTION | WS_THICKFRAME | WS_SYSMENU | WS_MINIMIZEBOX | WS_MAXIMIZEBOX
            )
            set_long(overlay, GWL_STYLE, style)
            exstyle = int(get_long(overlay, GWL_EXSTYLE))
            exstyle = (exstyle | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW
            set_long(overlay, GWL_EXSTYLE, exstyle)

            # GWLP_HWNDPARENT is the owner field for a top-level popup.
            ctypes.set_last_error(0)
            set_long(overlay, GWLP_HWNDPARENT, int(hwnd))
            if ctypes.get_last_error():
                raise ctypes.WinError(ctypes.get_last_error())

            set_pos = user32.SetWindowPos
            set_pos.argtypes = (
                ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                ctypes.c_int, ctypes.c_int, ctypes.c_uint,
            )
            set_pos.restype = ctypes.c_int
            # Apply the style changes without changing z-order or activating.
            # The native owner relationship keeps the popup above its owner.
            SWP_NOMOVE, SWP_NOSIZE, SWP_NOACTIVATE, SWP_FRAMECHANGED, SWP_NOZORDER = (
                0x0002, 0x0001, 0x0010, 0x0020, 0x0004
            )
            if not set_pos(
                overlay, None, 0, 0, 0, 0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_FRAMECHANGED | SWP_NOZORDER,
            ):
                raise ctypes.WinError(ctypes.get_last_error())

            self.target_hwnd = int(hwnd)
            self._attached = True
            self.app._log("Overlay owner connected as a separate non-activating window.", "success")
            return True
        except Exception as exc:
            self.window.withdraw()
            self._attached = False
            self._native_hwnd = 0
            self.app._log(f"Overlay owner setup failed: {type(exc).__name__}: {exc}", "warning")
            return False

    def _set_native_geometry(self, x: int, y: int, width: int, height: int) -> None:
        self._child_x, self._child_y = int(x), int(y)
        self._width, self._height = int(width), int(height)
        if sys.platform != "win32" or not self._native_hwnd or not self.target_rect:
            return
        user32 = self._win_api()
        try:
            left, top = self.target_rect[0], self.target_rect[1]
            desktop_x, desktop_y = int(left + self._child_x), int(top + self._child_y)
            set_pos = user32.SetWindowPos
            set_pos.argtypes = (
                ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                ctypes.c_int, ctypes.c_int, ctypes.c_uint,
            )
            set_pos.restype = ctypes.c_int
            # SWP_NOREDRAW + SWP_NOCOPYBITS significantly reduces the cost of
            # moving an owned popup while a full-screen game is running.
            SWP_NOACTIVATE = 0x0010
            SWP_NOZORDER = 0x0004
            SWP_SHOWWINDOW = 0x0040
            SWP_NOREDRAW = 0x0008
            SWP_NOCOPYBITS = 0x0100
            if not set_pos(
                ctypes.c_void_p(self._native_hwnd), None,
                desktop_x, desktop_y, self._width, self._height,
                SWP_NOACTIVATE | SWP_NOZORDER | SWP_SHOWWINDOW | SWP_NOREDRAW | SWP_NOCOPYBITS,
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            self._is_shown = True
        except Exception as exc:
            self.app._log(f"Could not position game overlay: {type(exc).__name__}: {exc}", "warning")

    def set_target(self, data: dict[str, Any]) -> None:
        if not self.app._settings_snapshot().overlay_enabled:
            try:
                self.hide()
            except Exception:
                pass
            return
        # Overlay window may have been destroyed while disabled.
        try:
            if not self.window.winfo_exists():
                return
        except tk.TclError:
            return
        focused = bool(data.get("focused"))
        hwnd = int(data.get("hwnd") or 0)
        rect = data.get("rect")
        visible = bool(data.get("visible", bool(hwnd and rect)))
        new_rect = tuple(int(value) for value in rect) if rect else None
        # Skip Win32 work when the target state is unchanged.
        if (
            hwnd == self.target_hwnd
            and new_rect == self.target_rect
            and focused == self._focused
            and visible == self._is_shown
            and self._attached
        ):
            return
        self._focused = focused
        if hwnd and rect:
            new_rect = tuple(int(value) for value in rect)
            width = max(1, new_rect[2] - new_rect[0])
            height = max(1, new_rect[3] - new_rect[1])
            changed_window = hwnd != self.target_hwnd or not self._attached
            moved_or_resized = new_rect != self.target_rect
            self.target_rect = new_rect
            self._client_width, self._client_height = width, height
            if changed_window and not self._ensure_owned_popup(hwnd):
                return
            if visible:
                self.show_on_target(force=changed_window or moved_or_resized)
            else:
                self.hide()
        elif not visible or not hwnd:
            self.hide()

    def show_on_target(self, force: bool = False) -> None:
        if not self.app._settings_snapshot().overlay_enabled:
            self.hide()
            return
        if not self._attached or not self.target_rect:
            return
        settings = self.app._settings_snapshot()
        client_width, client_height = self._client_width, self._client_height
        width = min(self.MAX_WIDTH, max(self.MIN_WIDTH, int(settings.overlay_width)), max(1, client_width))
        height = min(self.MAX_HEIGHT, max(self.MIN_HEIGHT, int(settings.overlay_height)), max(1, client_height))
        x = max(0, min(int(settings.overlay_x_offset), max(0, client_width - width)))
        y = max(0, min(int(settings.overlay_y_offset), max(0, client_height - height)))
        changed = (x, y, width, height) != (self._child_x, self._child_y, self._width, self._height)
        if force or changed:
            self._set_native_geometry(x, y, width, height)
            self._is_shown = True
        elif not self._is_shown and sys.platform == "win32" and self._native_hwnd:
            try:
                # SW_SHOWNOACTIVATE leaves focus and existing owner z-order intact.
                # Only call when we actually need to become visible; repeated
                # ShowWindow calls are a common source of overlay lag.
                self._win_api().ShowWindow(ctypes.c_void_p(self._native_hwnd), 4)
                self._is_shown = True
            except Exception as exc:
                self.app._log(f"Could not show game overlay: {type(exc).__name__}: {exc}", "warning")


    def hide(self) -> None:
        if not self._is_shown and not self._attached:
            return
        if sys.platform == "win32" and self._native_hwnd:
            try:
                self._win_api().ShowWindow(ctypes.c_void_p(self._native_hwnd), 0)  # SW_HIDE
            except Exception:
                pass
        elif not self._attached:
            try:
                self.window.withdraw()
            except tk.TclError:
                pass
        self._is_shown = False

    def update_data(self, data: dict[str, Any]) -> None:
        previous = self.data
        merged = dict(data)
        for kind in ("hp", "mp"):
            if merged.get(f"{kind}_current") is None and previous.get(f"{kind}_current") is not None:
                for suffix in ("current", "max", "ratio", "exact", "last_seen"):
                    merged[f"{kind}_{suffix}"] = previous.get(f"{kind}_{suffix}")
        self.data = merged
        data = merged

        hp_current, hp_max = data.get("hp_current"), data.get("hp_max")
        mp_current, mp_max = data.get("mp_current"), data.get("mp_max")
        hp_exact, mp_exact = bool(data.get("hp_exact")), bool(data.get("mp_exact"))
        focused = bool(data.get("focused"))
        commands = data.get("enabled_commands") or []
        key = (
            focused, hp_current, hp_max, hp_exact, mp_current, mp_max, mp_exact,
            bool(data.get("ability_listening")), str(data.get("movement_keys", "w, a, s, d")),
            bool(data.get("movement_detected")), tuple(commands),
        )
        # Cheap early-out – identical visible state means zero work.
        if key == self._last_render_key:
            return
        self._last_render_key = key

        self.connection_label.configure(
            text="GAME ACTIVE" if focused else "GAME IN BACKGROUND",
            fg="#7ec89d" if focused else "#d6ad62",
        )
        hp_state = "OCR" if hp_exact else ("BAR ESTIMATE" if hp_current is not None else "WAITING FOR HUD")
        self.hp_label.configure(text=f"HP  |  {hp_state}")
        self.hp_detail.configure(
            text=f"{hp_current:,} / {hp_max:,}" if hp_current is not None and hp_max else "— / —"
        )
        mp_state = "OCR" if mp_exact else ("BAR ESTIMATE" if mp_current is not None else "WAITING FOR HUD")
        self.mp_label.configure(text=f"MP  |  {mp_state}")
        self.mp_detail.configure(
            text=f"{mp_current:,} / {mp_max:,}" if mp_current is not None and mp_max else "— / —"
        )

        enabled = bool(data.get("ability_listening"))
        keys = str(data.get("movement_keys", "w, a, s, d")).upper()
        self.listener_label.configure(
            text=f"LISTENING TO: {keys}" if enabled else "KEY LISTENER OFF",
            fg="#7ec89d" if enabled else "#a9a9a9",
        )
        self.movement_label.configure(
            text="MOVEMENT DETECTED" if enabled and data.get("movement_detected") else ("NO MOVEMENT" if enabled else ""),
            fg="#e7c46d" if data.get("movement_detected") else "#a9a9a9",
        )
        command_text = "\n".join(f"• {command}" for command in commands) if commands else "• None"
        self.commands_label.configure(text="ENABLED COMMANDS\n" + command_text)

    def _start_drag(self, event) -> None:
        global _OVERLAY_INTERACTING
        if self._attached:
            _OVERLAY_INTERACTING = True
            self._drag_start = (event.x_root, event.y_root, self._child_x, self._child_y)

    def _drag(self, event) -> None:
        if not self._drag_start or not self._attached:
            return
        sx, sy, x, y = self._drag_start
        max_x = max(0, self._client_width - self._width)
        max_y = max(0, self._client_height - self._height)
        new_x = max(0, min(x + event.x_root - sx, max_x))
        new_y = max(0, min(y + event.y_root - sy, max_y))
        if (new_x, new_y) != (self._child_x, self._child_y):
            self._set_native_geometry(new_x, new_y, self._width, self._height)

    def _end_drag(self, _event) -> None:
        global _OVERLAY_INTERACTING
        self._drag_start = None
        _OVERLAY_INTERACTING = False
        self._persist_geometry()

    def _start_resize(self, event) -> None:
        global _OVERLAY_INTERACTING
        if self._attached or self._is_shown:
            _OVERLAY_INTERACTING = True
            self._resize_start = (event.x_root, event.y_root, self._width, self._height)

    def _resize(self, event) -> None:
        if not self._resize_start:
            return
        sx, sy, width, height = self._resize_start
        new_w = max(self.MIN_WIDTH, min(self.MAX_WIDTH, width + event.x_root - sx))
        new_h = max(self.MIN_HEIGHT, min(self.MAX_HEIGHT, height + event.y_root - sy))
        new_w = min(new_w, max(self.MIN_WIDTH, self._client_width))
        new_h = min(new_h, max(self.MIN_HEIGHT, self._client_height))
        if (new_w, new_h) != (self._width, self._height):
            self._set_native_geometry(self._child_x, self._child_y, new_w, new_h)

    def _end_resize(self, _event) -> None:
        global _OVERLAY_INTERACTING
        self._resize_start = None
        _OVERLAY_INTERACTING = False
        self._persist_geometry()

    def _persist_geometry(self) -> None:
        if not self._attached:
            return
        self.app.persist_overlay_geometry(
            x=self._child_x, y=self._child_y, width=self._width, height=self._height,
        )


class ToolTip:
    """Simple hover tooltip for Tk widgets."""

    def __init__(self, widget: tk.Widget, text: str, delay_ms: int = 400) -> None:
        self.widget = widget
        self.text = text
        self.delay_ms = delay_ms
        self._after_id: str | None = None
        self._tip: tk.Toplevel | None = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _event=None) -> None:
        self._cancel()
        self._after_id = self.widget.after(self.delay_ms, self._show)

    def _cancel(self) -> None:
        if self._after_id is not None:
            try:
                self.widget.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None

    def _show(self) -> None:
        self._cancel()
        if self._tip is not None:
            return
        try:
            x = self.widget.winfo_rootx() + 12
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        except tk.TclError:
            return
        self._tip = tk.Toplevel(self.widget)
        self._tip.wm_overrideredirect(True)
        self._tip.wm_geometry(f"+{x}+{y}")
        try:
            self._tip.attributes("-topmost", True)
        except Exception:
            pass
        label = tk.Label(
            self._tip,
            text=self.text,
            justify="left",
            background="#2a2a2a",
            foreground="#eeeeee",
            relief="solid",
            borderwidth=1,
            font=("Segoe UI", 9),
            padx=8,
            pady=6,
            wraplength=320,
        )
        label.pack()

    def _hide(self, _event=None) -> None:
        self._cancel()
        if self._tip is not None:
            try:
                self._tip.destroy()
            except Exception:
                pass
            self._tip = None



def parse_version(value: str) -> tuple[int, ...]:
    """Parse a dotted version string into a comparable integer tuple."""
    parts = re.findall(r"\d+", (value or "").strip().lstrip("vV"))
    return tuple(int(part) for part in parts) if parts else (0,)


def is_newer_version(remote: str, local: str) -> bool:
    """True only when remote is strictly greater than local (semver-ish)."""
    return parse_version(remote) > parse_version(local)


def read_bundled_version() -> str:
    for candidate in (BASE_DIR / "VERSION", Path(getattr(sys, "_MEIPASS", BASE_DIR)) / "VERSION"):
        try:
            if candidate.is_file():
                return candidate.read_text(encoding="utf-8").strip() or APP_VERSION
        except OSError:
            continue
    return APP_VERSION


def fetch_remote_version(timeout: float = 8.0) -> str | None:
    try:
        req = urllib.request.Request(
            GITHUB_VERSION_URL,
            headers={"User-Agent": f"MSC-Helper/{APP_VERSION}", "Accept": "text/plain"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace").strip()
    except Exception:
        return None


def fetch_latest_release_asset(timeout: float = 12.0) -> tuple[str, str] | None:
    """Return (download_url, tag_name) for the latest .exe asset, or None."""
    try:
        req = urllib.request.Request(
            GITHUB_RELEASES_API,
            headers={
                "User-Agent": f"MSC-Helper/{APP_VERSION}",
                "Accept": "application/vnd.github+json",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        tag = str(data.get("tag_name") or data.get("name") or "").lstrip("v").strip()
        assets = data.get("assets") or []
        for asset in assets:
            name = str(asset.get("name") or "").lower()
            url = asset.get("browser_download_url")
            if url and name.endswith(".exe"):
                return str(url), tag or "latest"
        # Fallback: any asset that looks like the main binary
        for asset in assets:
            name = str(asset.get("name") or "").lower()
            url = asset.get("browser_download_url")
            if url and "msc" in name:
                return str(url), tag or "latest"
    except Exception:
        return None
    return None


def download_file(url: str, dest: Path, timeout: float = 120.0) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    req = urllib.request.Request(url, headers={"User-Agent": f"MSC-Helper/{APP_VERSION}"})
    with urllib.request.urlopen(req, timeout=timeout) as resp, open(tmp, "wb") as out:
        shutil.copyfileobj(resp, out)
    tmp.replace(dest)


def mark_installed_version(version: str) -> None:
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        VERSION_MARK_PATH.write_text(version.strip() + "\n", encoding="utf-8")
        # Remove legacy marker name if present.
        legacy = CONFIG_DIR / "installed_version.txt"
        if legacy.is_file() and legacy != VERSION_MARK_PATH:
            try:
                legacy.unlink()
            except OSError:
                pass
    except OSError:
        pass


def read_installed_version_mark() -> str | None:
    try:
        if VERSION_MARK_PATH.is_file():
            return VERSION_MARK_PATH.read_text(encoding="utf-8").strip() or None
        legacy = CONFIG_DIR / "installed_version.txt"
        if legacy.is_file():
            value = legacy.read_text(encoding="utf-8").strip() or None
            if value:
                mark_installed_version(value)
            return value
    except OSError:
        pass
    return None


def is_frozen_exe() -> bool:
    return bool(getattr(sys, "frozen", False))


def current_executable_path() -> Path | None:
    if is_frozen_exe():
        return Path(sys.executable).resolve()
    # Development / source run – look for a nearby built exe.
    for candidate in (BASE_DIR / "dist" / "MSCHelper.exe", BASE_DIR / "MSCHelper.exe"):
        if candidate.is_file():
            return candidate.resolve()
    return None


class MapleHelperApp:
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
        self._last_snapshot_saved = 0.0
        self._latest_telemetry: dict[str, Any] = {}
        self._build_styles()
        self._build_ui()
        self.overlay = GameOverlay(self)
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

    def _build_styles(self) -> None:
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("TFrame", background=self.BG)
        style.configure("Panel.TFrame", background=self.PANEL)
        style.configure("TLabel", background=self.BG, foreground=self.TEXT, font=("Segoe UI", 10))
        style.configure("Muted.TLabel", background=self.BG, foreground=self.MUTED, font=("Segoe UI", 9))
        style.configure("Panel.TLabel", background=self.PANEL, foreground=self.TEXT, font=("Segoe UI", 10))
        style.configure("PanelMuted.TLabel", background=self.PANEL, foreground=self.MUTED, font=("Segoe UI", 9))
        style.configure("PanelTitle.TLabel", background=self.PANEL, foreground=self.TEXT,
                        font=("Segoe UI Semibold", 12))
        style.configure("Header.TLabel", background=self.BG, foreground=self.TEXT,
                        font=("Segoe UI Semibold", 22))
        style.configure("TEntry", fieldbackground="#303030", foreground=self.TEXT, insertcolor=self.TEXT,
                        bordercolor=self.BORDER, lightcolor=self.BORDER, darkcolor=self.BORDER,
                        padding=(8, 6), font=("Segoe UI", 10))
        style.configure("TCombobox", fieldbackground="#303030", foreground=self.TEXT,
                        background="#3a3a3a", arrowcolor=self.TEXT, bordercolor=self.BORDER,
                        padding=(7, 5))
        style.map("TCombobox", fieldbackground=[("readonly", "#303030")],
                  selectbackground=[("readonly", "#3a3a3a")], selectforeground=[("readonly", self.TEXT)])
        style.configure("TCheckbutton", background=self.PANEL, foreground=self.TEXT, font=("Segoe UI", 9))
        style.map("TCheckbutton", background=[("active", self.PANEL)], foreground=[("disabled", self.MUTED)])
        style.configure("TButton", background="#3b3b3b", foreground=self.TEXT, borderwidth=0,
                        padding=(11, 8), font=("Segoe UI Semibold", 9))
        style.map("TButton", background=[("active", "#4a4a4a"), ("disabled", "#303030")])
        style.configure("TNotebook", background=self.BG, borderwidth=0)
        style.configure("TNotebook.Tab", background="#292929", foreground=self.MUTED,
                        padding=(16, 9), font=("Segoe UI Semibold", 9))
        # Include active + expand so tabs stay clickable under clam and do not
        # shrink the hit area when selected (common cause of "need to spam" tabs).
        style.map(
            "TNotebook.Tab",
            background=[("selected", self.PANEL), ("active", "#353535")],
            foreground=[("selected", self.TEXT), ("active", self.TEXT)],
            expand=[("selected", [1, 1, 1, 0])],
        )

    def _build_ui(self) -> None:
        shell = ttk.Frame(self.root, padding=(18, 15, 18, 15))
        shell.pack(fill="both", expand=True)
        header = ttk.Frame(shell)
        header.pack(fill="x", pady=(0, 12))
        ttk.Label(header, text="MSC HELPER", style="Header.TLabel").pack(side="left")
        self.status_badge = tk.Label(header, text="●  WAITING", bg=self.BG, fg=self.AMBER,
                                     font=("Segoe UI Semibold", 9))
        self.status_badge.pack(side="right", padx=4)
        ttk.Label(shell, text="MapleStory Classic Helper",
                  style="Muted.TLabel").pack(anchor="w", pady=(0, 12))

        self._shell = shell
        self.notebook = ttk.Notebook(shell)
        self.notebook.pack(fill="both", expand=True)
        # Keyboard traversal (Ctrl+Tab) and a reliable mouse-down select so tabs
        # still switch even when the event loop is busy with telemetry/overlay work.
        try:
            self.notebook.enable_traversal()
        except Exception:
            pass
        self.notebook.bind("<ButtonPress-1>", self._on_notebook_press, add="+")
        self.overview_tab = ttk.Frame(self.notebook, padding=14)
        self.potions_tab = ttk.Frame(self.notebook, padding=14)
        self.movement_tab = ttk.Frame(self.notebook, padding=14)
        self.overlay_tab = ttk.Frame(self.notebook, padding=14)
        self.general_tab = ttk.Frame(self.notebook, padding=14)
        self.notebook.add(self.overview_tab, text="General")
        self.notebook.add(self.potions_tab, text="Auto-Pot")
        self.notebook.add(self.movement_tab, text="Auto-Cast")
        self.notebook.add(self.overlay_tab, text="Overlay")
        self.notebook.add(self.general_tab, text="Settings")
        self._build_overview_tab()
        self._build_potions_tab()
        self._build_movement_tab()
        self._build_overlay_tab()
        self._build_general_tab()
        self._build_ocr_warning_banner(shell)

    def _is_tesseract_available(self) -> bool:
        """Return True when pytesseract and the Tesseract binary are usable."""
        if BarDetector is None:
            return False
        try:
            detector = BarDetector(BASE_DIR / "assets")
            return bool(detector.ocr_available)
        except Exception:
            return False

    def _build_ocr_warning_banner(self, parent: tk.Widget) -> None:
        """Show a dismissible warning when Tesseract OCR is not installed."""
        if self._is_tesseract_available():
            return
        banner = tk.Frame(
            parent,
            bg="#3a2a12",
            highlightthickness=1,
            highlightbackground="#c49a3c",
            bd=0,
        )
        banner.pack(fill="x", pady=(12, 0))
        inner = tk.Frame(banner, bg="#3a2a12", padx=12, pady=10)
        inner.pack(fill="x")
        message = tk.Label(
            inner,
            text=(
                "Tesseract OCR is not installed. Without it, HP/MP readings use bar-fill "
                "estimates only. Install Tesseract for better accuracy — see the GitHub README."
            ),
            bg="#3a2a12",
            fg="#f0e0b0",
            font=("Segoe UI", 9),
            wraplength=720,
            justify="left",
            anchor="w",
        )
        message.pack(side="left", fill="x", expand=True)
        buttons = tk.Frame(inner, bg="#3a2a12")
        buttons.pack(side="right", padx=(12, 0))
        ttk.Button(
            buttons,
            text="Open README",
            command=self._open_github_readme,
        ).pack(side="left", padx=(0, 6))
        ttk.Button(
            buttons,
            text="Dismiss",
            command=banner.destroy,
        ).pack(side="left")
        self._ocr_warning_banner = banner

    def _open_github_readme(self) -> None:
        url = "https://github.com/zgndia/MSC-Helper/blob/main/README.md"
        try:
            import webbrowser
            webbrowser.open(url)
            self._log(f"Opened README link: {url}", "success")
        except Exception as exc:
            self._log(f"Could not open README link: {exc}", "warning")
            messagebox.showinfo(
                "GitHub README",
                f"Open this URL in your browser:\n\n{url}",
                parent=self.root,
            )

    def _on_notebook_press(self, event) -> None:
        """Force-select the tab under the cursor on mouse-down.

        ttk.Notebook occasionally drops a click when the main thread is also
        applying telemetry / overlay updates. Selecting on ButtonPress-1 makes
        tab switches reliable without needing to spam the tab.
        """
        try:
            element = self.notebook.identify(event.x, event.y)
            if not element or "label" not in element and "tab" not in element:
                # identify can return '' or 'padding' outside tab labels
                index = self.notebook.index(f"@{event.x},{event.y}")
            else:
                index = self.notebook.index(f"@{event.x},{event.y}")
            if 0 <= int(index) < self.notebook.index("end"):
                if self.notebook.index("current") != int(index):
                    self.notebook.select(index)
        except Exception:
            pass

    def _card(self, parent, title: str) -> ttk.Frame:
        card = tk.Frame(parent, bg=self.PANEL, highlightthickness=1, highlightbackground=self.BORDER, bd=0)
        inner = ttk.Frame(card, style="Panel.TFrame", padding=14)
        inner.pack(fill="both", expand=True)
        ttk.Label(inner, text=title.upper(), style="PanelMuted.TLabel").pack(anchor="w", pady=(0, 10))
        card.inner = inner
        return card

    def _load_info_icon(self) -> tk.PhotoImage | None:
        """Load a small info icon for setting tooltips. Keep a reference to avoid GC."""
        if getattr(self, "_info_icon_image", None) is not None:
            return self._info_icon_image
        candidates = [
            BASE_DIR / "assets" / "info_icon_16.png",
            BASE_DIR / "assets" / "info_icon.png",
            BASE_DIR / "info_icon.png",
        ]
        for path in candidates:
            if path.is_file():
                try:
                    self._info_icon_image = tk.PhotoImage(file=str(path))
                    return self._info_icon_image
                except Exception:
                    continue
        self._info_icon_image = None
        return None

    def _add_info_icon(self, parent: tk.Widget, tooltip: str) -> None:
        """Place a small 'i' icon that shows a hover description (not clickable)."""
        icon = self._load_info_icon()
        if icon is not None:
            label = tk.Label(parent, image=icon, bg=self.PANEL, borderwidth=0)
            label.image = icon  # keep reference
        else:
            label = tk.Label(parent, text="ⓘ", bg=self.PANEL, fg="#6eb5ff", font=("Segoe UI", 9))
        label.pack(side="left", padx=(6, 0))
        ToolTip(label, tooltip)

    def _make_meter_card(self, parent, title: str, color: str, kind: str):
        card = self._card(parent, title)
        inner = card.inner
        readout = ttk.Label(inner, text=f"{kind.upper()}  — / —", style="PanelTitle.TLabel")
        readout.pack(anchor="w", pady=(2, 8))
        meter = Meter(inner, color, bg=self.PANEL, height=14)
        meter.pack(fill="x", pady=(0, 8))
        freshness = ttk.Label(inner, text="WAITING FOR HUD", style="PanelMuted.TLabel")
        freshness.pack(anchor="w")
        return card, meter, readout, freshness

    def _build_overview_tab(self) -> None:
        cards = ttk.Frame(self.overview_tab)
        cards.pack(fill="x", pady=(0, 8))
        cards.columnconfigure(0, weight=1, uniform="overview_meters")
        cards.columnconfigure(1, weight=1, uniform="overview_meters")
        cards.rowconfigure(0, weight=1)
        self.hp_overview, self.hp_meter, self.hp_readout, self.hp_freshness = self._make_meter_card(cards, "Health", self.RED, "HP")
        self.mp_overview, self.mp_meter, self.mp_readout, self.mp_freshness = self._make_meter_card(cards, "Mana", self.MANA, "MP")
        self.hp_overview.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        self.mp_overview.grid(row=0, column=1, sticky="nsew", padx=(6, 0))

        # Game window + enabled commands side-by-side, equal width/height like HP/MP.
        mid = ttk.Frame(self.overview_tab)
        mid.pack(fill="x", pady=(0, 8))
        mid.columnconfigure(0, weight=1, uniform="overview_mid")
        mid.columnconfigure(1, weight=1, uniform="overview_mid")
        mid.rowconfigure(0, weight=1)

        capture = self._card(mid, "Game window")
        capture.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        self.target_info = ttk.Label(
            capture.inner,
            text="MSC Helper is Searching for MapleStory.exe. Make sure MapleStory Classic is running.",
            style="Panel.TLabel", wraplength=320, justify="left",
        )
        self.target_info.pack(anchor="nw", fill="both", expand=True)

        commands = self._card(mid, "Enabled Command List")
        commands.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        self.enabled_summary = ttk.Label(
            commands.inner, text="Loading…", style="Panel.TLabel", justify="left", wraplength=320,
        )
        self.enabled_summary.pack(anchor="nw", fill="both", expand=True)
        self._update_enabled_summary()

        # Activity log lives on General to remove the extra tab and empty space.
        log_card = self._card(self.overview_tab, "Recent activity")
        log_card.pack(fill="both", expand=True)
        self.log = tk.Text(
            log_card.inner, height=12, bg="#191919", fg="#dddddd",
            insertbackground=self.TEXT, relief="flat", bd=0, wrap="word",
            font=("Consolas", 9), padx=9, pady=8, state="disabled",
            highlightthickness=1, highlightbackground=self.BORDER,
        )
        self.log.pack(fill="both", expand=True)
        self.log.tag_configure("time", foreground="#888888")
        self.log.tag_configure("event", foreground="#dddddd")
        self.log.tag_configure("warning", foreground=self.AMBER)
        self.log.tag_configure("success", foreground=self.GREEN)
        ttk.Label(
            self.overview_tab, text="No key values or typed text are recorded.",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(4, 0))

    def _build_potions_tab(self) -> None:
        ttk.Label(self.potions_tab, text="Auto-pot for mp and hp.",
                  style="Muted.TLabel", wraplength=700).pack(anchor="w", pady=(0, 12))
        row = ttk.Frame(self.potions_tab)
        row.pack(fill="both", expand=True)
        row.columnconfigure(0, weight=1)
        row.columnconfigure(1, weight=1)
        self.hp_vars = {
            "maximum": tk.StringVar(value=str(self.current_settings.max_hp)),
            "threshold": tk.StringVar(value=str(self.current_settings.hp_threshold)),
            "offset": tk.StringVar(value=str(self.current_settings.hp_random_offset)),
            "key": tk.StringVar(value=self.current_settings.hp_key),
        }
        self.mp_vars = {
            "maximum": tk.StringVar(value=str(self.current_settings.max_mp)),
            "threshold": tk.StringVar(value=str(self.current_settings.mp_threshold)),
            "offset": tk.StringVar(value=str(self.current_settings.mp_random_offset)),
            "key": tk.StringVar(value=self.current_settings.mp_key),
        }
        self.hp_enabled_var = tk.BooleanVar(value=self.current_settings.hp_enabled)
        self.mp_enabled_var = tk.BooleanVar(value=self.current_settings.mp_enabled)
        self.hp_mode_var = tk.StringVar(value=self._mode_label(self.current_settings.hp_threshold_mode))
        self.mp_mode_var = tk.StringVar(value=self._mode_label(self.current_settings.mp_threshold_mode))
        hp_card = self._card(row, "Health potion")
        mp_card = self._card(row, "Mana potion")
        hp_card.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        mp_card.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        self._build_potion_controls(hp_card.inner, "HP", self.hp_enabled_var, self.hp_vars, self.hp_mode_var)
        self._build_potion_controls(mp_card.inner, "MP", self.mp_enabled_var, self.mp_vars, self.mp_mode_var)

    @staticmethod
    def _mode_label(mode: str) -> str:
        return "Remaining percentage" if mode == "percent" else "Missing points"

    @staticmethod
    def _mode_value(label: str) -> str:
        return "percent" if label == "Remaining percentage" else "points"

    def _entry_field(self, parent, label: str, variable: tk.StringVar, width: int = 14, tooltip: str | None = None):
        box = ttk.Frame(parent, style="Panel.TFrame")
        label_row = ttk.Frame(box, style="Panel.TFrame")
        label_row.pack(anchor="w", fill="x", pady=(0, 4))
        ttk.Label(label_row, text=label, style="PanelMuted.TLabel").pack(side="left")
        if tooltip:
            self._add_info_icon(label_row, tooltip)
        entry = ttk.Entry(box, textvariable=variable, width=width)
        entry.pack(fill="x")
        entry.bind("<FocusOut>", lambda _e: self.apply_settings(show_errors=False))
        entry.bind("<Return>", lambda _e: self.apply_settings(show_errors=False))
        box.pack(fill="x", pady=(0, 8))
        return box

    def _build_potion_controls(self, parent, kind: str, enabled_var, vars_dict, mode_var):
        toggle_row = ttk.Frame(parent, style="Panel.TFrame")
        toggle_row.pack(anchor="w", fill="x", pady=(0, 12))
        ttk.Checkbutton(
            toggle_row,
            text=f"Toggle auto {kind} potion",
            variable=enabled_var,
            command=self._on_feature_toggle,
        ).pack(side="left")
        self._add_info_icon(
            toggle_row,
            f"When enabled, presses the configured key when {kind} falls below the threshold (while MapleStory is focused).",
        )

        self._entry_field(
            parent,
            f"Max {kind}",
            vars_dict["maximum"],
            tooltip=f"Maximum {kind} value used for percentage calculations and display. Auto-detected when OCR is enabled.",
        )

        threshold_label = ttk.Label(parent, text="", style="PanelMuted.TLabel")
        threshold_label.pack(anchor="w", pady=(0, 4))

        def update_threshold_label(*_args):
            threshold_label.configure(
                text="Trigger at missing points"
                if mode_var.get() == "Missing points"
                else f"Trigger at remaining {kind} (%)"
            )

        update_threshold_label()
        mode_var.trace_add("write", update_threshold_label)

        thresh_entry = ttk.Entry(parent, textvariable=vars_dict["threshold"])
        thresh_entry.pack(fill="x", pady=(0, 8))
        thresh_entry.bind("<FocusOut>", lambda _e: self.apply_settings(show_errors=False))
        thresh_entry.bind("<Return>", lambda _e: self.apply_settings(show_errors=False))

        type_row = ttk.Frame(parent, style="Panel.TFrame")
        type_row.pack(anchor="w", fill="x", pady=(0, 4))
        ttk.Label(type_row, text="Threshold type", style="PanelMuted.TLabel").pack(side="left")
        self._add_info_icon(
            type_row,
            "Missing points: pot when current is this many below max. "
            "Remaining percentage: pot when current falls to this percent of max (or lower).",
        )
        mode_box = ttk.Combobox(
            parent,
            textvariable=mode_var,
            values=("Missing points", "Remaining percentage"),
            state="readonly",
        )
        mode_box.pack(fill="x", pady=(0, 8))
        mode_box.bind("<<ComboboxSelected>>", lambda _e: self.apply_settings(show_errors=False))

        self._entry_field(
            parent,
            "Random offset",
            vars_dict["offset"],
            tooltip="Random variation added to the threshold each time so pot timing is less predictable. 0 disables randomization. Recommended: 75.",
        )

        key_row = ttk.Frame(parent, style="Panel.TFrame")
        key_row.pack(fill="x", pady=(0, 4))
        key_box = ttk.Frame(key_row, style="Panel.TFrame")
        key_box.pack(side="left", fill="x", expand=True)
        key_label_row = ttk.Frame(key_box, style="Panel.TFrame")
        key_label_row.pack(anchor="w", fill="x", pady=(0, 4))
        ttk.Label(key_label_row, text="Potion keybind", style="PanelMuted.TLabel").pack(side="left")
        self._add_info_icon(key_label_row, "Key that is pressed to use a potion. Must differ between HP and MP.")
        key_entry = ttk.Entry(key_box, textvariable=vars_dict["key"], width=12)
        key_entry.pack(fill="x")
        key_entry.bind("<FocusOut>", lambda _e: self.apply_settings(show_errors=False))
        key_entry.bind("<Return>", lambda _e: self.apply_settings(show_errors=False))
        ttk.Button(
            key_row,
            text="Capture",
            command=lambda k=kind.lower(): self.begin_capture(k),
        ).pack(side="right", padx=(7, 0), anchor="s")

    def _build_movement_tab(self) -> None:
        card = self._card(self.movement_tab, "Auto-Casting")
        card.pack(fill="both", expand=True)
        self.ability_enabled_var = tk.BooleanVar(value=self.current_settings.ability_spam_enabled)

        toggle_row = ttk.Frame(card.inner, style="Panel.TFrame")
        toggle_row.pack(anchor="w", fill="x", pady=(0, 12))
        ttk.Checkbutton(
            toggle_row,
            text="Toggle auto-casting",
            variable=self.ability_enabled_var,
            command=self._on_feature_toggle,
        ).pack(side="left")
        self._add_info_icon(
            toggle_row,
            "When enabled and movement keys are held, randomly presses ability keys at the configured interval. "
            "Only active while MapleStory is focused.",
        )

        self.movement_keys_var = tk.StringVar(value=self.current_settings.movement_keys)
        self.ability_keys_var = tk.StringVar(value=self.current_settings.ability_keys)
        self.ability_interval_var = tk.StringVar(value=str(self.current_settings.ability_interval_ms))
        self.ability_set_delay_var = tk.StringVar(value=str(self.current_settings.ability_set_delay_ms))
        self.movement_hold_var = tk.StringVar(value=str(self.current_settings.movement_hold_ms))
        self.pause_typing_var = tk.BooleanVar(value=self.current_settings.pause_spam_while_typing)

        self._entry_field(
            card.inner,
            "Physical keys that arm movement detection (comma-separated)",
            self.movement_keys_var,
            32,
            tooltip=(
                "Keys that indicate you are moving. Holding any of these for the gate duration arms auto-cast. "
                "Supports letters, digits, arrows, Space, Ctrl, Shift, Alt, and F1–F24 "
                "(e.g. w, a, s, d, space, left_ctrl). Recommended: w, a, s, d."
            ),
        )
        self._entry_field(
            card.inner,
            "Ability keys to auto-cast (comma-separated)",
            self.ability_keys_var,
            32,
            tooltip="Keys pressed at random while the movement gate is armed. Can be skills or pickup keys.",
        )
        self._entry_field(
            card.inner,
            "Delay between each cast (ms)",
            self.ability_interval_var,
            tooltip="Interval between individual ability key presses within a set. Lower = faster casting. Recommended: 50-150 ms.",
        )
        self._entry_field(
            card.inner,
            "Delay after ability set (ms)",
            self.ability_set_delay_var,
            tooltip="Pause after finishing one full pass of the ability keys (e.g. after Q, E, R) before starting the set again. Recommended: 500-1500 ms.",
        )
        self._entry_field(
            card.inner,
            "Continuous key-hold gate (ms)",
            self.movement_hold_var,
            tooltip="How long a movement key must be held continuously before auto-cast starts. Prevents accidental triggers. Recommended: 1250 ms.",
        )

        pause_row = ttk.Frame(card.inner, style="Panel.TFrame")
        pause_row.pack(anchor="w", fill="x", pady=(4, 8))
        ttk.Checkbutton(
            pause_row,
            text="Pause key spam when in-game typing is detected",
            variable=self.pause_typing_var,
            command=self._on_feature_toggle,
        ).pack(side="left")
        self._add_info_icon(
            pause_row,
            "Stops auto-cast briefly when typing keys are detected so chat and UI input are not interrupted. Recommended: enabled.",
        )

        ttk.Label(
            card.inner,
            text="Auto-casting listens for configured movement keys (W, A, S, D by default; also supports Space, "
                 "Ctrl, Shift, Alt, arrows, and function keys) to detect when you are moving. "
                 "While moving, it presses the ability keys in order, then waits the set delay before repeating. "
                 "You can also bind a pickup key to automatically collect items while walking.",
            style="PanelMuted.TLabel",
            wraplength=700,
            justify="left",
        ).pack(anchor="w", pady=(7, 0))

    def _build_overlay_tab(self) -> None:
        ttk.Label(
            self.overlay_tab,
            text="Live status overlay that floats near the MapleStory window. Drag to reposition; resize from the edges.",
            style="Muted.TLabel",
            wraplength=700,
        ).pack(anchor="w", pady=(0, 12))
        card = self._card(self.overlay_tab, "Overlay")
        card.pack(fill="x", pady=(0, 10))
        self.overlay_enabled_var = tk.BooleanVar(value=self.current_settings.overlay_enabled)
        row = ttk.Frame(card.inner, style="Panel.TFrame")
        row.pack(anchor="w", fill="x")
        ttk.Checkbutton(
            row,
            text="Enable game overlay",
            variable=self.overlay_enabled_var,
            command=self._on_overlay_toggle,
        ).pack(side="left")
        self._add_info_icon(
            row,
            "Shows a small always-on-top panel with current HP/MP and connection status near the game window. "
            "Disable if you experience compositor flicker or do not need the on-screen readout.",
        )
        ttk.Label(
            card.inner,
            text="The overlay is owned by the MapleStory window and does not steal focus. "
                 "It is automatically hidden when the game is minimized or closed. "
                 "Drag the header to move; use the bottom-right grip to resize.",
            style="PanelMuted.TLabel",
            wraplength=700,
        ).pack(anchor="w", pady=(8, 0))

    def _build_general_tab(self) -> None:
        ttk.Label(
            self.general_tab,
            text="Advanced options. Most users can leave these at their recommended defaults.",
            style="Muted.TLabel",
            wraplength=700,
        ).pack(anchor="w", pady=(0, 12))

        target = self._card(self.general_tab, "Settings")
        target.pack(fill="x", pady=(0, 10))
        self.window_title_var = tk.StringVar(value=self.current_settings.window_title)
        self.cooldown_var = tk.StringVar(value=str(self.current_settings.potion_cooldown_ms))
        self.scan_interval_var = tk.StringVar(value=str(self.current_settings.hud_scan_interval_ms))
        self.key_press_var = tk.StringVar(value=str(self.current_settings.key_hold_ms))
        self.auto_detect_var = tk.BooleanVar(value=self.current_settings.auto_detect_max_stats)
        self.hud_monitoring_var = tk.BooleanVar(value=self.current_settings.hud_monitoring_enabled)
        self.passive_hud_var = tk.BooleanVar(value=self.current_settings.passive_hud_reading)
        self.min_focus_hold_var = tk.StringVar(value=str(self.current_settings.min_focus_hold_ms))

        self._entry_field(
            target.inner,
            "Game window name",
            self.window_title_var,
            30,
            tooltip="Title fragment used to locate the MapleStory window. Leave as MapleStory unless you use a custom client title.",
        )
        self._entry_field(
            target.inner,
            "Auto-pot cooldown (ms)",
            self.cooldown_var,
            tooltip="Minimum time between potion key presses. Lower values pot faster but may look more bot-like. Recommended: 1000 ms.",
        )
        self._entry_field(
            target.inner,
            "HUD scan interval (ms)",
            self.scan_interval_var,
            tooltip="How often the HP/MP bars are sampled while the game is focused. Lower is more responsive; higher reduces CPU. Recommended: 500 ms.",
        )
        self._entry_field(
            target.inner,
            "Key hold duration (ms)",
            self.key_press_var,
            tooltip="How long each simulated key is held down. Increase if keys are occasionally missed. Recommended: 75 ms.",
        )
        self._entry_field(
            target.inner,
            "Minimum focus hold before input (ms)",
            self.min_focus_hold_var,
            tooltip="The game must stay focused for at least this long before any key is sent. Prevents inputs when briefly tabbing in/out. Recommended: 1000 ms.",
        )

        mon_row = ttk.Frame(target.inner, style="Panel.TFrame")
        mon_row.pack(anchor="w", fill="x", pady=(4, 2))
        ttk.Checkbutton(
            mon_row,
            text="Toggle HUD monitoring",
            variable=self.hud_monitoring_var,
            command=self._on_feature_toggle,
        ).pack(side="left")
        self._add_info_icon(
            mon_row,
            "Must be enabled for auto-potting and live HP/MP readouts. Disable only if you want the helper to stop reading the HUD entirely.",
        )

        ocr_row = ttk.Frame(target.inner, style="Panel.TFrame")
        ocr_row.pack(anchor="w", fill="x", pady=(2, 5))
        ttk.Checkbutton(
            ocr_row,
            text="Increase HUD monitoring accuracy using Tesseract OCR",
            variable=self.auto_detect_var,
            command=self._on_feature_toggle,
        ).pack(side="left")
        self._add_info_icon(
            ocr_row,
            "Uses Tesseract to read exact current/max numbers from the HUD. Disable if you experience lag; bar-fill estimation will still work. Recommended: enabled.",
        )

        passive_row = ttk.Frame(target.inner, style="Panel.TFrame")
        passive_row.pack(anchor="w", fill="x", pady=(2, 5))
        ttk.Checkbutton(
            passive_row,
            text="Enable passive HUD reading while game is in the background",
            variable=self.passive_hud_var,
            command=self._on_feature_toggle,
        ).pack(side="left")
        self._add_info_icon(
            passive_row,
            "Samples HP/MP while MapleStory is visible but not focused. Background reads can under-report fill and cause unwanted potions when you tab back in. Recommended: disabled.",
        )

        update_card = self._card(self.general_tab, "Updates")
        update_card.pack(fill="x", pady=(0, 10))
        self.auto_update_var = tk.BooleanVar(value=self.current_settings.auto_update_enabled)
        upd_row = ttk.Frame(update_card.inner, style="Panel.TFrame")
        upd_row.pack(anchor="w", fill="x")
        ttk.Checkbutton(
            upd_row,
            text="Automatically check for and install updates",
            variable=self.auto_update_var,
            command=self._on_feature_toggle,
        ).pack(side="left")
        self._add_info_icon(
            upd_row,
            "When enabled, MSC Helper compares the local version with the latest release on GitHub and can download a new executable. Disable if you prefer manual updates.",
        )
        ttk.Label(
            update_card.inner,
            text=f"Current version: {APP_VERSION}",
            style="PanelMuted.TLabel",
        ).pack(anchor="w", pady=(6, 0))
        ttk.Button(
            update_card.inner,
            text="Check for updates now",
            command=self._check_for_updates_manual,
        ).pack(anchor="w", pady=(8, 0))

        actions = ttk.Frame(self.general_tab)
        actions.pack(fill="x", pady=(0, 10))
        ttk.Button(
            actions,
            text="Reset all options to recommended defaults",
            command=self.reset_settings,
        ).pack(side="left")
        ttk.Button(
            actions,
            text="Go to file location",
            command=self._open_config_folder,
        ).pack(side="left", padx=(8, 0))
        ttk.Label(
            self.general_tab,
            text=f"Settings and captured HP/MP snapshots are saved automatically under {CONFIG_DIR}",
            style="Muted.TLabel",
            wraplength=700,
        ).pack(anchor="w", pady=(8, 0))

    def _open_config_folder(self) -> None:
        """Open the application data folder in the system file explorer."""
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            if sys.platform == "win32":
                os.startfile(str(CONFIG_DIR))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(CONFIG_DIR)])
            else:
                subprocess.Popen(["xdg-open", str(CONFIG_DIR)])
            self._log(f"Opened config folder: {CONFIG_DIR}", "success")
        except Exception as exc:
            self._log(f"Could not open config folder: {exc}", "warning")
            messagebox.showerror("Open folder", f"Could not open:\n{CONFIG_DIR}\n\n{exc}", parent=self.root)

    def _build_activity_tab(self) -> None:
        # Activity log was moved into the General tab in 0.2.1.
        pass

    def _on_feature_toggle(self) -> None:
        if not self.apply_settings(show_errors=False):
            current = self._settings_snapshot()
            self.hp_enabled_var.set(current.hp_enabled)
            self.mp_enabled_var.set(current.mp_enabled)
            self.ability_enabled_var.set(current.ability_spam_enabled)
            self.hud_monitoring_var.set(current.hud_monitoring_enabled)
            self.auto_detect_var.set(current.auto_detect_max_stats)
            self.passive_hud_var.set(current.passive_hud_reading)
            self.pause_typing_var.set(current.pause_spam_while_typing)

    def _on_overlay_toggle(self) -> None:
        """Immediately apply the overlay on/off switch. When disabled the overlay
        window is fully destroyed so it cannot affect the game compositor."""
        if not self.apply_settings(show_errors=False):
            self.overlay_enabled_var.set(self._settings_snapshot().overlay_enabled)
            return
        if not self._settings_snapshot().overlay_enabled:
            try:
                self.overlay.hide()
                if self.overlay.window.winfo_exists():
                    self.overlay.window.destroy()
                self.overlay._native_hwnd = 0
                self.overlay._attached = False
                self.overlay._is_shown = False
                self.overlay.target_hwnd = 0
            except Exception:
                pass
            self._log("Live status overlay fully disabled (window destroyed). Capture rate also reduced.", "success")
        else:
            # Recreate a fresh overlay window.
            try:
                if not self.overlay.window.winfo_exists():
                    self.overlay = GameOverlay(self)
                    icon_path = BASE_DIR / "icon.ico"
                    if icon_path.is_file():
                        try:
                            self.overlay.window.iconbitmap(default=str(icon_path))
                        except Exception:
                            pass
            except Exception:
                self.overlay = GameOverlay(self)
            self._log("Live status overlay enabled.", "success")
            if self.overlay.target_rect:
                self.overlay.show_on_target(force=True)

    def _check_for_updates_manual(self) -> None:
        threading.Thread(target=self._run_update_check, args=(True,), daemon=True, name="UpdateCheck").start()

    def _schedule_auto_update_check(self) -> None:
        if not self._settings_snapshot().auto_update_enabled:
            return
        self.root.after(2500, lambda: threading.Thread(
            target=self._run_update_check, args=(False,), daemon=True, name="AutoUpdate"
        ).start())

    def _ask_update_confirm(self, remote: str, local: str) -> bool:
        """Show a modal confirm dialog on the UI thread and return the answer."""
        result: queue.Queue = queue.Queue(maxsize=1)

        def _ask() -> None:
            ok = messagebox.askyesno(
                "Updates",
                f"There is a new version {remote} (you have {local}).\n\nDo you want to update?",
                parent=self.root,
            )
            result.put(bool(ok))

        self.root.after(0, _ask)
        try:
            return bool(result.get(timeout=300))
        except Exception:
            return False

    def _show_update_progress(self, title: str = "Updating MSC Helper") -> tuple[tk.Toplevel, ttk.Progressbar, tk.Label]:
        win = tk.Toplevel(self.root)
        win.title(title)
        win.geometry("420x140")
        win.resizable(False, False)
        win.transient(self.root)
        try:
            win.attributes("-topmost", True)
        except Exception:
            pass
        try:
            if self._app_icon_path.is_file():
                win.iconbitmap(str(self._app_icon_path))
        except Exception:
            pass
        frame = ttk.Frame(win, padding=16)
        frame.pack(fill="both", expand=True)
        label = ttk.Label(frame, text="Preparing update…", wraplength=380)
        label.pack(anchor="w", pady=(0, 10))
        bar = ttk.Progressbar(frame, mode="indeterminate", length=380)
        bar.pack(fill="x")
        bar.start(12)
        win.update_idletasks()
        return win, bar, label

    def _set_progress_status(self, label: tk.Label, text: str) -> None:
        def _apply() -> None:
            try:
                label.configure(text=text)
            except tk.TclError:
                pass
        self.root.after(0, _apply)

    def _run_update_check(self, manual: bool) -> None:
        progress_win: tk.Toplevel | None = None
        progress_bar: ttk.Progressbar | None = None
        progress_label: tk.Label | None = None
        try:
            local = read_bundled_version()
            remote = fetch_remote_version()
            if remote is None:
                msg = "Could not reach GitHub to check for updates."
                self.events.put(("log", msg))
                if manual:
                    self.root.after(0, lambda: messagebox.showwarning("Updates", msg, parent=self.root))
                return

            get_logger().info("Version check: local=%s remote=%s", local, remote)

            # Only treat remote as an update when it is strictly newer.
            if not is_newer_version(remote, local):
                msg = f"You are up to date (v{local})."
                if remote != local:
                    msg = f"You are up to date (v{local}). Remote reports v{remote}, which is not newer."
                self.events.put(("log", msg))
                mark_installed_version(local)
                if manual:
                    self.root.after(0, lambda m=msg: messagebox.showinfo("Updates", m, parent=self.root))
                return

            self.events.put(("log", f"Update available: v{remote} (you have v{local})."))

            # Always ask before installing — never auto-install silently.
            if not self._ask_update_confirm(remote, local):
                self.events.put(("log", "Update declined by user."))
                return

            asset = fetch_latest_release_asset()
            if not asset:
                msg = (
                    f"Version {remote} is available but no downloadable executable was found "
                    "on the release page.\n\n"
                    "Open https://github.com/zgndia/MSC-Helper/releases to download manually."
                )
                self.events.put(("log", msg.split("\n")[0]))
                self.root.after(0, lambda m=msg: messagebox.showinfo("Updates", m, parent=self.root))
                return

            url, tag = asset
            exe_path = current_executable_path()
            if exe_path is None or not is_frozen_exe():
                msg = (
                    f"A newer version (v{remote}) is available.\n"
                    f"Download it from:\n{url}\n\n"
                    "Automatic replacement only works when running the packaged MSCHelper.exe."
                )
                self.events.put(("log", f"Update available: {url}"))
                self.root.after(0, lambda m=msg: messagebox.showinfo("Updates", m, parent=self.root))
                return

            # Show progress UI on the main thread.
            ready: queue.Queue = queue.Queue(maxsize=1)

            def _open_progress() -> None:
                win, bar, label = self._show_update_progress(f"Updating to v{remote}")
                ready.put((win, bar, label))

            self.root.after(0, _open_progress)
            progress_win, progress_bar, progress_label = ready.get(timeout=30)

            download_dir = CONFIG_DIR / "updates"
            target = download_dir / f"MSCHelper_{tag}.exe"
            self.events.put(("log", f"Downloading update v{remote}…"))
            self._set_progress_status(progress_label, f"Downloading MSC Helper v{remote}…")
            download_file(url, target)

            self._set_progress_status(progress_label, "Preparing installer…")
            replace_bat = download_dir / "apply_update.bat"
            bat_lines = [
                "@echo off",
                "timeout /t 2 /nobreak >nul",
                f'copy /y "{target}" "{exe_path}"',
                "if errorlevel 1 exit /b 1",
                f'start "" "{exe_path}"',
                'del "%~f0"',
            ]
            replace_bat.write_text("\r\n".join(bat_lines) + "\r\n", encoding="utf-8")
            mark_installed_version(remote)
            self.events.put(("log", f"Update v{remote} downloaded. Restarting to apply…"))
            self._set_progress_status(progress_label, "Installing and restarting…")

            flags = 0
            if hasattr(subprocess, "CREATE_NO_WINDOW"):
                flags |= subprocess.CREATE_NO_WINDOW
            if hasattr(subprocess, "DETACHED_PROCESS"):
                flags |= subprocess.DETACHED_PROCESS
            subprocess.Popen(
                ["cmd", "/c", str(replace_bat)],
                cwd=str(download_dir),
                creationflags=flags,
                close_fds=True,
            )
            self.root.after(500, self.on_close)
        except Exception as exc:
            get_logger().exception("Update check failed")
            self.events.put(("log", f"Update check failed: {type(exc).__name__}: {exc}"))
            self.root.after(
                0,
                lambda: messagebox.showerror(
                    "Updates", f"Update check failed:\n{exc}", parent=self.root
                ),
            )
        finally:
            if progress_bar is not None:
                try:
                    self.root.after(0, progress_bar.stop)
                except Exception:
                    pass
            if progress_win is not None:
                try:
                    self.root.after(0, progress_win.destroy)
                except Exception:
                    pass


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
        if self.target_info.cget("text") != info:
            self.target_info.configure(text=info)
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
        self.status_badge.configure(text=label, fg=color)

    def _log(self, text: str, tag: str = "event") -> None:
        try:
            level = logging.WARNING if tag == "warning" else logging.INFO
            get_logger().log(level, text)
        except Exception:
            pass
        if not hasattr(self, "log"):
            return
        timestamp = datetime.now().strftime("%H:%M:%S")
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

    def on_close(self) -> None:
        self.stop_event.set()
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


def is_user_admin() -> bool:
    if sys.platform != "win32":
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_as_admin() -> bool:
    """Relaunch this script with UAC elevation; return False on cancellation/failure."""
    if sys.platform != "win32":
        return False
    try:
        script = str(Path(__file__).resolve())
        executable = sys.executable
        if not getattr(sys, "frozen", False):
            candidate = Path(sys.executable).with_name("pythonw.exe")
            if candidate.is_file():
                executable = str(candidate)
        params = subprocess.list2cmdline([script, *sys.argv[1:]])
        result = ctypes.windll.shell32.ShellExecuteW(None, "runas", executable, params, str(BASE_DIR), 1)
        return int(result) > 32
    except Exception:
        return False


def enable_dpi_awareness() -> None:
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # Per-monitor v2
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def main() -> None:
    if sys.platform != "win32":
        raise SystemExit("MSC Helper requires Windows.")
    if not is_user_admin():
        if relaunch_as_admin():
            return
        try:
            ctypes.windll.user32.MessageBoxW(
                None,
                "Administrator permission is required. The program cannot run without elevation.",
                APP_NAME,
                0x10,
            )
        except Exception:
            pass
        raise SystemExit("Administrator privileges were not granted.")
    enable_dpi_awareness()
    root = tk.Tk()
    MapleHelperApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
