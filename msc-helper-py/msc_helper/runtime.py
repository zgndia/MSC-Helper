"""Frozen-exe detection and optional Rust GUI launch."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from msc_helper.config import BASE_DIR

def is_frozen_exe() -> bool:
    return bool(getattr(sys, "frozen", False))


def resource_dir() -> Path:
    """Directory that holds bundled assets / sidecar binaries.

    When frozen (PyInstaller onefile) this is ``sys._MEIPASS``.
    Otherwise it is the source tree next to ``main.py``.
    """
    if is_frozen_exe() and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)  # type: ignore[attr-defined]
    return BASE_DIR


def current_executable_path() -> Path | None:
    if is_frozen_exe():
        return Path(sys.executable).resolve()
    # Development / source run – look for a nearby built exe.
    for candidate in (BASE_DIR / "dist" / "MSCHelper.exe", BASE_DIR / "MSCHelper.exe"):
        if candidate.is_file():
            return candidate.resolve()
    return None


def find_rust_gui_binary() -> Path | None:
    """Locate the optional Rust primary-GUI sidecar."""
    names = ("msc-helper-gui.exe", "msc_helper_gui.exe", "MSCHelperGui.exe")
    search_roots = [
        resource_dir(),
        resource_dir() / "bin",
        BASE_DIR,
        BASE_DIR / "msc-helper-gui" / "target" / "release",
        BASE_DIR / "dist",
        Path(sys.executable).resolve().parent if is_frozen_exe() else BASE_DIR,
    ]
    seen: set[Path] = set()
    for root in search_roots:
        try:
            root = root.resolve()
        except Exception:
            continue
        if root in seen:
            continue
        seen.add(root)
        for name in names:
            candidate = root / name
            if candidate.is_file():
                return candidate
    return None


def launch_rust_gui() -> subprocess.Popen | None:
    """Start the Rust primary GUI as a child process. Returns the Popen handle or None."""
    binary = find_rust_gui_binary()
    if binary is None:
        return None
    try:
        # Keep as a tracked child so we can terminate it on exit and detect close.
        # Do NOT detach — that caused both UIs to stay visible / lose process tracking.
        proc = subprocess.Popen(
            [str(binary)],
            cwd=str(binary.parent),
            close_fds=True,
        )
        return proc
    except Exception:
        return None


