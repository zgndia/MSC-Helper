# MSC Helper

**Release:** `0.2.0`  
**MapleStory Classic Helper** — configurable input automation macro for MapleStory Classic

## Features

- Auto-potting - automatically using hp and mp pots when necessary.
- Auto-casting - automatically casting any abilities that you want to cast when it's off cooldown such as Nimble Feet and Recovery. Works with the pick-up keybind but isn't recommended.
- Game overlay to see toggled commands, HUD monitoring, etc. (Can be toggled off)
- Settings for commands and general settings for maximum configurability.
- Monitors the in-game HUD to track your current and max Mana and Health.
- Offers auto-updates and logging.

## Requirements

- Windows 10/11
- Python 3.10+ (for source runs)
- Tesseract OCR (recommended, for best accuracy)
- Dependencies in `requirements.txt` plus [native-inputs](https://github.com/zgndia/native-inputs) (for running from source)

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

## Running (from source)

```cmd
python -m pip install -r requirements.txt
python -m pip install native-inputs
```

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