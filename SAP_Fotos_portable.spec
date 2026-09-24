# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

from PyInstaller.building.datastruct import Tree
from PyInstaller.utils.hooks import collect_all, copy_metadata

project = Path(SPECPATH)

datas = [
    (str(project / "app.py"), "."),
    (str(project / "config.json"), "."),
    (str(project / ".streamlit" / "config.toml"), ".streamlit"),
    (str(project / "BASE_LEVANTAMENTO_ATUALIZADA.xlsx"), "."),
]
binaries = []
hiddenimports = [
    "app",
    "modules.config",
    "modules.excel_utils",
    "modules.sap_bot",
    "modules.vision",
    "modules.window_control",
    "modules.word_report",
    "streamlit.web.cli",
    "openpyxl",
    "docx",
    "docx.oxml",
    "pytesseract",
    "pyautogui",
    "pygetwindow",
    "rapidfuzz",
    "cv2",
    "PIL",
]

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

# Na versão portátil, o OCR é copiado para a pasta ao lado do EXE depois do build.

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
    [],
    exclude_binaries=True,
    name="SAP_Fotos_Portatil",
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

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="SAP_Fotos_Portatil",
)
