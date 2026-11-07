"""Tk UI construction for MapleHelperApp (tabs, cards, fields)."""
from __future__ import annotations

import os
import subprocess
import sys
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any

from msc_helper.config import APP_NAME, APP_VERSION, BASE_DIR, CONFIG_DIR, Settings
from msc_helper.overlay import Meter, ToolTip

# Optional deps referenced by banner helpers
try:
    import pytesseract  # noqa: F401
except ImportError:
    pytesseract = None

try:
    from msc_helper.detector import BarDetector
except ImportError:
    BarDetector = None


class AppUIMixin:
    """Mixin providing notebook/tab construction. Expects MapleHelperApp state."""

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
            text="Go to AppData folder",
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

