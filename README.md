# MSC Helper

**Release:** `0.2.0`  
**MapleStory Classic Helper** — an input automation tool for MapleStory Classic.

MSC Helper offers automatic potion use and ordered ability casting with a live in-game overlay.

## Installation

1. Install the latest release from the [Releases](https://github.com/zgndia/MSC-Helper/releases) page.
2. Install [Tesseract OCR](https://github.com/tesseract-ocr/tesseract) for best accuracy (recommended).
3. Run this one-line command to add Tesseract OCR to your path in PowerShell:

```cmd
[Environment]::SetEnvironmentVariable("Path", $env:Path + ";C:\Program Files\Tesseract-OCR", "User")
```

4. Restart PowerShell and run `tesseract --version` to verify.
5. Launch `MSCHelper.exe` and MapleStory Classic.

Administrator elevation is required for reliable input delivery.

## Features

- Detects HP and MP HUD bars via template matching; estimates fill when OCR is unavailable.
- Optional Tesseract OCR for exact `current/max` values (preferred when available).
- Auto-detects maximum HP/MP from the HUD.
- Locates visible top-level windows owned by `MapleStory.exe` without requiring an exact title match.
- Passive desktop capture; does not steal focus.
- Cached HUD region after first successful scan for lower CPU use.
- Auto-pot with threshold (missing points or remaining %) plus random offset.
- Configurable minimum focus-hold before any input; optional passive background HUD reading (off by default).
- Auto-Casting: presses ability keys in sequence, with separate delays between keys and after a full set.
- Live status overlay (resizable).
- Automatic update checks (can be disabled under Settings).
- Activity log in the UI and file logs under `%APPDATA%\MSCHelper\logs\` (`latest.log` + dated rotating logs).

## Requirements

- Windows 10/11
- Python 3.10+ (for source runs)
- Tesseract OCR (recommended, for best accuracy)
- Dependencies in `requirements.txt` plus `native-keyboard-inputs` 0.3.0+

```cmd
python -m pip install -r requirements.txt
python -m pip install native-keyboard-inputs
```

## Running (from source)

```cmd
python autopot.py
```

Or use `run_autopot.bat` (launches the built exe if present, otherwise pythonw).

Administrator elevation is required for reliable input delivery.

## Building a release executable

```cmd
build_windows.bat
```

Produces `dist\MSCHelper.exe` (one-file, windowed, admin manifest, bundled icon and assets).

## Configuration

Settings, the last HP/MP snapshot, logs, and update marks are stored under:

```
%APPDATA%\MSCHelper\
```

Use **Settings → Go to file location** inside the app, or open that folder manually.

Most options ship with safe defaults. Prefer leaving cooldown, scan interval, and key-hold values at their recommended defaults unless you have a specific reason to change them.

### Auto-cast delays

- **Delay between each cast** — pause between individual keys in the ability list (e.g. after Q before E).
- **Delay after ability set** — longer pause after the full list has been pressed once before the cycle restarts.

### Updates

When auto-update is enabled (default), the helper compares its local version with the `VERSION` file on GitHub and, if newer, downloads the latest release executable and restarts to apply it. You can disable this under the **Settings** tab.

## Project layout

```text
assets/                 HUD reference images + info icon
autopot.py              Main application and worker
detector.py             HP/MP HUD detection and OCR
main.py                 Compatibility entry point
requirements.txt        Python runtime dependencies
build_windows.bat       Windows PyInstaller build script
MSCHelper.spec          PyInstaller spec
VERSION                 Release version
```

## Known limitations

- Windows-only; expects process name `MapleStory.exe`.
- Exact numeric readings need Tesseract and a HUD layout similar to the supplied reference assets.
- Desktop capture only sees pixels Windows exposes; minimized or exclusive-fullscreen content may not be readable.
- External overlays may not appear in exclusive-fullscreen modes — use windowed or borderless.
- No anti-cheat bypass is attempted. Review the game’s rules before using input automation.
