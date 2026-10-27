# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the portable Windows GUI build.
# Used by build.bat:  pyinstaller BunkrDownloaderPortableGUI.spec

a = Analysis(
    ["gui.py"],
    pathex=[],
    binaries=[],
    # resource_path() reads the window icons from sys._MEIPASS at runtime.
    datas=[("misc/gui/icons", "misc/gui/icons")],
    # gui.py imports downloader only at runtime (--gui-runner / --cli modes),
    # so name it explicitly to guarantee it is bundled.
    hiddenimports=["downloader"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="BunkrDownloaderPortableGUI",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,  # GUI app: launch without a console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=["misc/gui/icons/icon.ico"],
)