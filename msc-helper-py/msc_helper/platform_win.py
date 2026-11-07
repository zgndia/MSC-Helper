"""Windows window / process helpers used by the monitor worker."""
from __future__ import annotations

import ctypes
import ctypes.wintypes
import ntpath
import os
import sys
from pathlib import Path

from msc_helper.config import APP_NAME, BASE_DIR

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

