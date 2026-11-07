"""Live game overlay and small Tk widgets."""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Any

from msc_helper.config import APP_NAME, APP_VERSION, BASE_DIR

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



