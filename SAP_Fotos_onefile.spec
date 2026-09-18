# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

from PyInstaller.building.datastruct import Tree
from PyInstaller.utils.hooks import collect_all, copy_metadata

project = Path(SPECPATH)

datas = [
    (str(project / "app.py"), "."),
    (str(project / "config.json"), "."),
    (str(project / ".streamlit" / "config.toml"), ".streamlit"),
]
binaries = []
hiddenimports = [
    "app",
    "modules.config",
    "modules.excel_utils",
    "modules.sap_bot",
    "modules.vision",
    "modules.window_control",
    "streamlit.web.cli",
    "openpyxl",
    "pytesseract",
    "pyautogui",
    "pygetwindow",
    "rapidfuzz",
    "cv2",
    "PIL",
]

# Streamlit carrega vários componentes de forma dinâmica e precisa de package data.
for package_name in ["streamlit", "altair", "pydeck"]:
    try:
        d, b, h = collect_all(package_name)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:
        pass
    try:
        datas += copy_metadata(package_name)
    except Exception:
        pass

# Tesseract portátil completo. O workflow de build preenche esta pasta.
tess_dir = project / "vendor" / "tesseract"
if tess_dir.exists() and (tess_dir / "tesseract.exe").exists():
    datas += Tree(str(tess_dir), prefix="tesseract")


a = Analysis(
    [str(project / "launcher.py")],
    pathex=[str(project)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
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
    name="SAP_Fotos",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
