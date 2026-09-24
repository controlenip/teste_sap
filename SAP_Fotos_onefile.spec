# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

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


# ============================================================
# COMPONENTES DINÂMICOS
# ============================================================

# Streamlit, Altair e PyDeck carregam vários arquivos
# dinamicamente durante a execução.
for package_name in [
    "streamlit",
    "altair",
    "pydeck",
]:

    try:
        d, b, h = collect_all(
            package_name
        )

        datas += d
        binaries += b
        hiddenimports += h

    except Exception:
        pass

    try:
        datas += copy_metadata(
            package_name
        )

    except Exception:
        pass


# ============================================================
# TESSERACT PORTÁTIL
# ============================================================

# IMPORTANTE:
#
# Não utilizar:
#
#     Tree(...)
#
# dentro de "datas".
#
# O PyInstaller atual espera que cada item de datas seja:
#
#     (origem, destino)
#
# O Tree() pode produzir estruturas internas com mais valores
# e gerar:
#
# ValueError: too many values to unpack (expected 2)
#
# Por isso percorremos os arquivos individualmente.

tess_dir = (
    project
    / "vendor"
    / "tesseract"
)

if (
    tess_dir.exists()
    and
    (
        tess_dir
        / "tesseract.exe"
    ).exists()
):

    for file_path in tess_dir.rglob("*"):

        if not file_path.is_file():
            continue

        relative_parent = (
            file_path
            .parent
            .relative_to(
                tess_dir
            )
        )

        if str(
            relative_parent
        ) == ".":

            target_dir = "tesseract"

        else:

            target_dir = str(
                Path(
                    "tesseract"
                )
                / relative_parent
            ).replace(
                "\\",
                "/",
            )

        datas.append(
            (
                str(
                    file_path
                ),
                target_dir,
            )
        )


# ============================================================
# ANALYSIS
# ============================================================

a = Analysis(
    [
        str(
            project
            / "launcher.py"
        )
    ],

    pathex=[
        str(
            project
        )
    ],

    binaries=binaries,

    datas=datas,

    hiddenimports=hiddenimports,

    hookspath=[],

    hooksconfig={},

    runtime_hooks=[],

    excludes=[],

    noarchive=False,
)


# ============================================================
# PYZ
# ============================================================

pyz = PYZ(
    a.pure
)


# ============================================================
# EXE
# ============================================================

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
