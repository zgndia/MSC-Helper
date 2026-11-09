"""MSC Helper entry point.

PyInstaller and source runs start here (``main.py``).
Backend logic lives under ``msc_helper/``; UI is the Rust GUI.
"""
from __future__ import annotations

import os
import sys

# Re-exports used by control_server and external tooling
from msc_helper.config import (  # noqa: F401
    APP_NAME,
    APP_VERSION,
    BASE_DIR,
    CONFIG_DIR,
    Settings,
    load_settings,
    save_settings,
)
from msc_helper.logging_util import get_logger, setup_file_logging  # noqa: F401
from msc_helper.runtime import find_rust_gui_binary  # noqa: F401


def main() -> None:
    import ctypes
    import tkinter as tk

    from msc_helper.app import MapleHelperApp
    from msc_helper.runtime import find_rust_gui_binary as _find_rust

    def is_user_admin() -> bool:
        if sys.platform != "win32":
            return False
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False

    def relaunch_as_admin() -> bool:
        if sys.platform != "win32":
            return False
        try:
            from pathlib import Path
            import subprocess
            script = str(Path(__file__).resolve())
            executable = sys.executable
            if not getattr(sys, "frozen", False):
                candidate = Path(sys.executable).with_name("pythonw.exe")
                if candidate.is_file():
                    executable = str(candidate)
            params = subprocess.list2cmdline([script, *sys.argv[1:]])
            result = ctypes.windll.shell32.ShellExecuteW(
                None, "runas", executable, params, str(BASE_DIR), 1
            )
            return int(result) > 32
        except Exception:
            return False

    def enable_dpi_awareness() -> None:
        if sys.platform != "win32":
            return
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass

    if sys.platform == "win32" and not is_user_admin():
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
    # UI is always the Rust GUI. Tk root stays hidden (overlay + backend only).
    root = tk.Tk()
    root.withdraw()
    try:
        root.attributes("-alpha", 0.0)
    except Exception:
        pass
    app = MapleHelperApp.__new__(MapleHelperApp)
    app._rust_gui_mode = True
    app.__init__(root)
    root.mainloop()


if __name__ == "__main__":
    main()
