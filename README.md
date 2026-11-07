# MSC Helper

**Release:** `0.3.0`  
**MapleStory Classic Helper** — configurable input automation for MapleStory Classic

## Features

- Auto-potting — use HP and MP pots when necessary.
- Auto-casting — cast abilities on an interval (e.g. Nimble Feet, Recovery).
- Live **game overlay** for toggled commands and HUD status (can be turned off).
- **Rust primary GUI** for settings and status (required).
- HUD monitoring for current / max HP and MP (bar fill + optional Tesseract OCR).
- Auto-updates and file logging under AppData.

## Requirements

- Windows 10/11
- Python 3.10+ (for source runs)
- Tesseract OCR (recommended, for best accuracy)
- Dependencies in `requirements.txt` plus [native-inputs](https://github.com/zgndia/native-inputs) (source runs)
- [Rust toolchain](https://rustup.rs/) to build the primary GUI, **or** a pre-built `msc-helper-gui.exe`

## Installation (release)

1. Install the latest build from [Releases](https://github.com/zgndia/MSC-Helper/releases).
2. Install [Tesseract OCR](https://github.com/tesseract-ocr/tesseract) (recommended).
3. Add Tesseract to your user `PATH` in PowerShell if needed:

```cmd
[Environment]::SetEnvironmentVariable("Path", $env:Path + ";C:\Program Files\Tesseract-OCR", "User")
```

4. Restart the shell and run `tesseract --version` to verify.
5. Launch `MSCHelper.exe` and MapleStory Classic.

Administrator elevation is required for reliable input delivery.

## Running from source

```cmd
python -m pip install -r requirements.txt
python -m pip install native-inputs
```

Build the primary GUI once:

```cmd
cd msc-helper-gui
cargo build --release
cd ..
```

Then start the helper:

```cmd
python msc-helper-py/main.py
```

Or use `run.bat` (prefers `dist\MSCHelper.exe` if present, otherwise `pythonw`).

The Python process stays **headless**: it owns the worker, control server, and
game overlay. The **Rust window** is the only primary UI. If `msc-helper-gui.exe`
cannot be found, the app reports an error and exits.

## Building a release executable

```cmd
build_windows.bat
```

Produces `dist\MSCHelper.exe` (one-file, windowed, admin manifest, icon + assets).
When `cargo` is available (or a pre-built `msc-helper-gui.exe` is present), the
Rust GUI is built/bundled as a sidecar and launched automatically.

You can also use the PyInstaller spec:

```cmd
pyinstaller --noconfirm MSCHelper.spec
```

## Configuration

Settings, the last HP/MP snapshot, logs, and update marks live under:

```
%APPDATA%\MSCHelper\
```

In the app: **Settings → Go to AppData folder**.

Most options ship with safe defaults. Prefer leaving cooldown, scan interval, and
key-hold values at their recommended defaults unless you have a specific reason
to change them.

### Updates

When auto-update is enabled (default), the helper compares its local version with
the `VERSION` file on GitHub and, if newer, downloads the latest release
executable and restarts. Disable under the **Settings** tab if you prefer manual
updates.

## Project layout

```text
msc-helper-py/main.py            Entry point (admin elevation + headless backend)
msc-helper-py/control_server.py  Local HTTP control plane for the Rust GUI
msc-helper-py/msc_helper/        Backend package
  detector.py                    HP/MP HUD detection and OCR
  config.py                      Settings model + persistence
  logging_util.py                File logging
  platform_win.py                Window / process helpers
  keys.py                        Key parsing / hold detection
  movement.py                    Movement key-hold gate
  worker.py                      Monitor worker (HUD, pots, auto-cast)
  worker_thresholds.py           Threshold helpers for the worker
  overlay.py                     Live game overlay
  updates.py                     Version / update download helpers
  runtime.py                     Frozen-exe + Rust GUI launch
  app_ui.py                      Legacy Tk construction (unused primary UI)
  app_updates.py                 In-app update flow
  app.py                         MapleHelperApp (wires backend + overlay)
msc-helper-gui/                  Primary GUI (Rust / egui) — required
assets/                          HUD reference images + info icon
build_executable.bat             Windows PyInstaller build
run_source.bat                   Launch packaged exe or source entry
MSCHelper.spec                   PyInstaller spec
VERSION                          Release version
```

## Architecture

- **Rust GUI** — only primary interface (settings, status, activity log).
- **Python** — detector, worker, settings persistence, control server, **game overlay**.

## Known limitations

- Windows-only; expects process name `MapleStory.exe`.
- Exact numeric readings need Tesseract and a HUD layout similar to the supplied reference assets.
- Desktop capture only sees pixels Windows exposes; minimized or exclusive-fullscreen content may not be readable.
- External overlays may not appear in exclusive-fullscreen modes — use windowed or borderless.
- No anti-cheat bypass is attempted. Review MapleStory Classic’s rules before using input automation.
