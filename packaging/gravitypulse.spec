# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller specification for GravityPulse (Windows portable zip & macOS .app bundle)."""

from pathlib import Path
import sys

ROOT = Path(SPECPATH).resolve().parent
IS_MACOS = sys.platform == "darwin"

datas = []
ico_file = ROOT / "assets" / "gravitypulse.ico"
icns_file = ROOT / "assets" / "gravitypulse.icns"
if ico_file.exists():
    datas.append((str(ico_file), "assets"))
if icns_file.exists():
    datas.append((str(icns_file), "assets"))

hiddenimports = ["AppKit", "Foundation", "objc"] if IS_MACOS else []
icon_path = str(icns_file if (IS_MACOS and icns_file.exists()) else ico_file)

a = Analysis(
    [str(ROOT / "build.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["matplotlib", "numpy", "scipy", "pandas", "pytest"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="GravityPulse",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=icon_path,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="GravityPulse",
)

if IS_MACOS:
    app = BUNDLE(
        coll,
        name="GravityPulse.app",
        icon=icon_path,
        bundle_identifier="com.aodilab.gravitypulse",
        info_plist={
            "CFBundleName": "GravityPulse",
            "CFBundleDisplayName": "GravityPulse",
            "CFBundleShortVersionString": "1.2.0",
            "CFBundleVersion": "1.2.0",
            "LSMinimumSystemVersion": "12.0",
            "NSHighResolutionCapable": True,
            "LSUIElement": False,
        },
    )
