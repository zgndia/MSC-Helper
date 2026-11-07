"""Settings toggles and update-check flow for MapleHelperApp."""
from __future__ import annotations

import queue
import subprocess
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any

from msc_helper.config import APP_NAME, APP_VERSION, CONFIG_DIR, Settings, save_settings
from msc_helper.runtime import current_executable_path, is_frozen_exe
from msc_helper.updates import (
    download_file,
    fetch_latest_release_asset,
    fetch_remote_version,
    is_newer_version,
    mark_installed_version,
    read_bundled_version,
)


class AppUpdatesMixin:
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
                f"There is a new version {remote} (your version is {local}).\n\nDo you want to update?",
                parent=self.root,
            )
            result.put(bool(ok))

        self.root.after(0, _ask)
        try:
            return bool(result.get(timeout=300))
        except Exception:
            return False

    def _ask_disable_auto_update(self) -> bool:
        """Ask whether to disable automatic update checks (UI thread)."""
        result: queue.Queue = queue.Queue(maxsize=1)

        def _ask() -> None:
            ok = messagebox.askyesno(
                "Updates",
                "You declined the update.\n\n"
                "Do you want to disable automatic update checks?\n"
                "(You can still use \"Check for updates now\" anytime.)",
                parent=self.root,
            )
            result.put(bool(ok))

        self.root.after(0, _ask)
        try:
            return bool(result.get(timeout=300))
        except Exception:
            return False

    def _offer_disable_auto_update(self) -> None:
        """If the user confirms, turn off auto-update and persist the setting."""
        if not self._ask_disable_auto_update():
            return
        try:
            with self.settings_lock:
                updated = replace(self.current_settings, auto_update_enabled=False)
                self.current_settings = updated
            try:
                self.auto_update_var.set(False)
            except Exception:
                pass
            try:
                save_settings(updated)
            except OSError as exc:
                get_logger().warning("Could not save auto-update preference: %s", exc)
            self.events.put(("log", "Automatic update checks disabled."))
            self.root.after(
                0,
                lambda: messagebox.showinfo(
                    "Updates",
                    "Automatic update checks have been disabled.\n"
                    "You can re-enable them under Settings at any time.",
                    parent=self.root,
                ),
            )
        except Exception:
            get_logger().exception("Failed to disable auto-update")

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
                # For automatic (startup) prompts only: offer to turn off auto-update.
                if not manual:
                    self._offer_disable_auto_update()
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
            # Clear PyInstaller onefile env vars so the restarted process is treated
            # as a fresh parent (avoids "invalid originating onefile parent process
            # (PID not found!)" security validation failure).
            bat_lines = [
                "@echo off",
                "setlocal",
                "timeout /t 3 /nobreak >nul",
                f'copy /y "{target}" "{exe_path}"',
                "if errorlevel 1 exit /b 1",
                "rem Strip PyInstaller onefile inheritance markers so the new process",
                "rem is treated as a fresh parent (avoids PID-not-found security error).",
                'set "_MEIPASS="',
                'set "_MEIPASS2="',
                'set "_PYI_APPLICATION_HOME_DIR="',
                'set "_PYI_PARENT_PROCESS_LEVEL="',
                'set "_PYI_LINUX_PROCESS_NAME="',
                'set "_PYI_SPLASH_IPC="',
                'set "PYINSTALLER_SUPPRESS_SPLASH_SCREEN="',
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
            # Also strip the markers from the environment passed to the helper bat.
            clean_env = {
                k: v
                for k, v in os.environ.items()
                if not (
                    k.startswith("_MEI")
                    or k.startswith("_PYI")
                    or k == "PYINSTALLER_SUPPRESS_SPLASH_SCREEN"
                )
            }
            subprocess.Popen(
                ["cmd", "/c", str(replace_bat)],
                cwd=str(download_dir),
                env=clean_env,
                creationflags=flags,
                close_fds=True,
            )
            # Give the bat time to start and the copy to finish before we exit.
            self.root.after(800, self.on_close)
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


