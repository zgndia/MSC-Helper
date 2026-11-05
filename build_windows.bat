@echo off
setlocal
cd /d "%~dp0"

echo Building MSC Helper for Windows (windowed, administrator manifest, app icon)...

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

python -m PyInstaller --noconfirm --clean --onefile --windowed --uac-admin ^
    --name MSCHelper ^
    --icon "icon.ico" ^
    --optimize 2 ^
    --add-data "assets;assets" ^
    --add-data "VERSION;." ^
    --add-data "icon.ico;." ^
    --collect-all mss ^
    --collect-all native_keyboard_inputs ^
    --collect-all pytesseract ^
    --hidden-import mss ^
    --hidden-import cv2 ^
    --hidden-import numpy ^
    autopot.py
if errorlevel 1 goto :failed

echo.
echo Build complete: dist\MSCHelper.exe
echo Keep the source repository alongside the executable for debugging and tests.
echo Install Tesseract OCR separately for exact live HP/MP text recognition.
exit /b 0

:failed
echo.
echo Build failed. Read the error above and check README.md for requirements.
exit /b 1
