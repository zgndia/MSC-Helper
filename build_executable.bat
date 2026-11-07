@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

echo Building MSC Helper for Windows (windowed, administrator manifest, app icon)...
echo.

rem ── Locate / build the Rust primary GUI ────────────────────────────────────
set "RUST_GUI_EXE="

if exist "msc-helper-gui\Cargo.toml" (
    where cargo >nul 2>&1
    if not errorlevel 1 (
        echo Building Rust primary GUI ^(msc-helper-gui^)...
        pushd msc-helper-gui
        cargo build --release
        if errorlevel 1 (
            echo WARNING: Rust GUI build failed – package will require a pre-built msc-helper-gui.exe at runtime.
        )
        popd
    ) else (
        echo cargo not found – skipping Rust GUI build.
        echo Place a pre-built msc-helper-gui.exe next to this script to bundle it.
    )
)

rem Prefer the crate's release output, then common fallback locations.
if exist "%~dp0msc-helper-gui\target\release\msc-helper-gui.exe" (
    set "RUST_GUI_EXE=%~dp0msc-helper-gui\target\release\msc-helper-gui.exe"
) else if exist "%~dp0msc-helper-gui.exe" (
    set "RUST_GUI_EXE=%~dp0msc-helper-gui.exe"
) else if exist "%~dp0dist\msc-helper-gui.exe" (
    set "RUST_GUI_EXE=%~dp0dist\msc-helper-gui.exe"
) else if exist "%~dp0target\release\msc-helper-gui.exe" (
    set "RUST_GUI_EXE=%~dp0target\release\msc-helper-gui.exe"
)

if defined RUST_GUI_EXE (
    echo Rust GUI ready: !RUST_GUI_EXE!
) else (
    echo No Rust GUI binary found – build or place msc-helper-gui.exe before running.
)

if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

python -m pip install --upgrade pip
if errorlevel 1 goto :failed
python -m pip install -r requirements.txt pyinstaller
if errorlevel 1 goto :failed

rem native-keyboard-inputs is distributed separately from requirements.txt.
python -c "import native_keyboard_inputs"
if errorlevel 1 (
    echo.
    echo ERROR: Install native-keyboard-inputs 0.3.0+ into this Python environment first.
    echo Example: python -m pip install -e C:\path\to\native-keyboard-inputs
    goto :failed
)

rem ── PyInstaller ────────────────────────────────────────────────────────────
if defined RUST_GUI_EXE (
    echo Bundling Rust GUI sidecar: !RUST_GUI_EXE!
    python -m PyInstaller --noconfirm --clean --onefile --windowed --uac-admin ^
        --name MSCHelper ^
        --icon "icon.ico" ^
        --optimize 2 ^
        --add-data "assets;assets" ^
        --add-data "VERSION;." ^
        --add-data "icon.ico;." ^
        --add-data "msc-helper-py\control_server.py;." ^
        --add-binary "!RUST_GUI_EXE!;." ^
        --collect-all mss ^
        --collect-all native_keyboard_inputs ^
        --collect-all pytesseract ^
        --hidden-import mss ^
        --hidden-import cv2 ^
        --hidden-import numpy ^
        --hidden-import control_server ^
        --hidden-import msc_helper ^
        --hidden-import msc_helper.config ^
        --hidden-import msc_helper.logging_util ^
        --hidden-import msc_helper.platform_win ^
        --hidden-import msc_helper.keys ^
        --hidden-import msc_helper.movement ^
        --hidden-import msc_helper.worker_thresholds ^
        --hidden-import msc_helper.worker ^
        --hidden-import msc_helper.overlay ^
        --hidden-import msc_helper.updates ^
        --hidden-import msc_helper.runtime ^
        --hidden-import msc_helper.app_ui ^
        --hidden-import msc_helper.app_updates ^
        --hidden-import msc_helper.app ^
        --hidden-import msc_helper.detector ^
        msc-helper-py\main.py
) else (
    python -m PyInstaller --noconfirm --clean --onefile --windowed --uac-admin ^
        --name MSCHelper ^
        --icon "icon.ico" ^
        --optimize 2 ^
        --add-data "assets;assets" ^
        --add-data "VERSION;." ^
        --add-data "icon.ico;." ^
        --add-data "msc-helper-py\control_server.py;." ^
        --collect-all mss ^
        --collect-all native_keyboard_inputs ^
        --collect-all pytesseract ^
        --hidden-import mss ^
        --hidden-import cv2 ^
        --hidden-import numpy ^
        --hidden-import control_server ^
        --hidden-import msc_helper ^
        --hidden-import msc_helper.config ^
        --hidden-import msc_helper.logging_util ^
        --hidden-import msc_helper.platform_win ^
        --hidden-import msc_helper.keys ^
        --hidden-import msc_helper.movement ^
        --hidden-import msc_helper.worker_thresholds ^
        --hidden-import msc_helper.worker ^
        --hidden-import msc_helper.overlay ^
        --hidden-import msc_helper.updates ^
        --hidden-import msc_helper.runtime ^
        --hidden-import msc_helper.app_ui ^
        --hidden-import msc_helper.app_updates ^
        --hidden-import msc_helper.app ^
        --hidden-import msc_helper.detector ^
        msc-helper-py\main.py
)
if errorlevel 1 goto :failed

echo.
echo Build complete: dist\MSCHelper.exe
if defined RUST_GUI_EXE (
    echo Included: msc-helper-gui.exe ^(Rust primary GUI sidecar^)
    echo On launch the Rust GUI starts automatically; Python stays headless ^(overlay + backend^).
) else (
    echo WARNING: No Rust GUI binary was found. MSC Helper requires msc-helper-gui.exe at runtime.
    echo Build it with: cd msc-helper-gui ^&^& cargo build --release
    echo Then re-run this script, or place msc-helper-gui.exe next to the packaged exe.
)
echo Keep the source repository alongside the executable for debugging and tests.
echo Install Tesseract OCR separately for exact live HP/MP text recognition.
exit /b 0

:failed
echo.
echo Build failed. Read the error above and check README.md for requirements.
exit /b 1
