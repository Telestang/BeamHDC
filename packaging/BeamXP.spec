# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path


ROOT = Path(SPECPATH).parent

a = Analysis(
    [str(ROOT / "beamng_hand_drive_tool.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[
        (str(ROOT / "blender_preview_backend.py"), "."),
        (str(ROOT / "BeamXP_icon.ico"), "."),
        # Composited onto generated config previews; hdc_sticker_path() looks
        # for it in sys._MEIPASS in frozen builds.
        (str(ROOT / "hdc_sticker.png"), "."),
    ],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="BeamXP",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ROOT / "BeamXP_icon.ico"),
)
