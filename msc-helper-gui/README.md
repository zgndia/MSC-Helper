# MSC Helper – primary GUI (Rust / egui)

The Python process runs headless in the background (detector, auto-pot / auto-cast worker, control server, and the separate game overlay).

## Architecture

```
MSCHelper.exe  (PyInstaller onefile)  or  python msc-helper-py/main.py
 ├─ Python backend (headless)
 │    · HUD detection / OCR
 │    · MonitorWorker (pots, auto-cast)
 │    · control server on 127.0.0.1:17890
 │    · game overlay (separate lightweight window)
 └─ msc-helper-gui.exe  (this crate – primary settings / status UI)
```

The Rust GUI talks to Python over HTTP:

| Method | Path        | Purpose                          |
|--------|-------------|----------------------------------|
| GET    | `/status`   | Telemetry + current settings     |
| POST   | `/settings` | Apply settings from this UI      |
| POST   | `/reset`    | Restore recommended defaults     |

Closing this window shuts down the whole helper (Python backend exits with it).

## Build (Windows)

Requires a recent stable [Rust toolchain](https://rustup.rs/):

```bat
cd msc-helper-gui
cargo build --release
```

Binary: `target\release\msc-helper-gui.exe`

Place that exe next to `MSCHelper.exe`, or leave it under `target\release\` for
source runs — the Python entry point looks in both locations.

## Package with PyInstaller

From the project root:

```bat
build_windows.bat
```

The script builds this crate when `cargo` is available (or picks up a pre-built
`msc-helper-gui.exe`), then produces `dist\MSCHelper.exe` with the GUI bundled
as a sidecar.

A Rust binary **is required** at runtime. If it is missing, the helper shows an
error and exits — there is no alternate primary UI.

## Theme / layout

Dark theme and tabs mirror the original design (General, Auto-Pot, Auto-Cast,
Overlay, Settings). Settings auto-save on change. Under **Settings**:

- **Reset to defaults**
- **Go to AppData folder** — opens `%APPDATA%\MSCHelper`

## Dependencies

See `Cargo.toml`. Notable crates: `eframe` / `egui`, `ureq`, `serde`.
