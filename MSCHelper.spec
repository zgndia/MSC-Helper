# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

datas = [('assets', 'assets'), ('VERSION', '.'), ('icon.ico', '.'), ('msc-helper-py/control_server.py', '.')]
binaries = [('C:/Users/zgn/Documents/programming/python/MSC-Helper/msc-helper-gui/target/release/msc-helper-gui.exe', '.')]
hiddenimports = ['mss', 'cv2', 'numpy', 'control_server', 'msc_helper', 'msc_helper.config', 'msc_helper.logging_util', 'msc_helper.platform_win', 'msc_helper.keys', 'msc_helper.movement', 'msc_helper.worker_thresholds', 'msc_helper.worker', 'msc_helper.overlay', 'msc_helper.updates', 'msc_helper.runtime', 'msc_helper.app_ui', 'msc_helper.app_updates', 'msc_helper.app', 'msc_helper.detector']
tmp_ret = collect_all('mss')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('native_keyboard_inputs')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('pytesseract')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['msc-helper-py/main.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=2,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [('O', None, 'OPTION'), ('O', None, 'OPTION')],
    name='MSCHelper',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    uac_admin=True,
    icon=['icon.ico'],
)
