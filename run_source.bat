@echo off
REM Launch MSC Helper with the Rust GUI.
REM 1. Starts the Python backend in headless-main-window mode.
REM 2. Starts the Rust GUI (must be built first: cargo build --release).

set MSC_RUST_GUI=1

start "" /B pythonw msc-helper-py\main.py --rust-gui
timeout /t 1 /nobreak >nul

if exist "msc-helper-gui\target\release\msc-helper-gui.exe" (
    start "" "msc-helper-gui\target\release\msc-helper-gui.exe"
) else if exist "msc-helper-gui.exe" (
    start "" "msc-helper-gui.exe"
) else (
    echo.
    echo  msc-helper-gui.exe not found.
    echo  Build it first:
    echo    cd msc-helper-gui
    echo    cargo build --release
    echo.
    pause
)
