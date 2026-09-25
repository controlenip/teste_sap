from __future__ import annotations

import io
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pandas as pd
import pyautogui
import pytesseract
import streamlit as st

from modules.config import (
    APP_DIR,
    CONFIG_PATH,
    abs_point_to_norm,
    find_tesseract,
    load_config,
    save_config,
)
def _load_sap_photo_bot():
    """
    Carrega SAPPhotoBot diretamente do arquivo-fonte empacotado.

    Isso evita a colisao de importacao que pode ocorrer entre Streamlit e
    o arquivo PYZ do PyInstaller no modo onefile.
    """
    bundle_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))

    candidates = [
        bundle_root / "bot_source" / "sap_bot.py",
        Path(__file__).resolve().parent / "bot_source" / "sap_bot.py",
        Path(__file__).resolve().parent / "modules" / "sap_bot.py",
    ]

    source_path = next((p for p in candidates if p.exists()), None)

    if source_path is None:
        checked = "\n".join(str(p) for p in candidates)
        raise ImportError(
            "Nao foi possivel localizar o codigo do SAPPhotoBot.\n"
            "Caminhos verificados:\n" + checked
        )

    module_name = "modules._sap_bot_embedded"
    spec = importlib.util.spec_from_file_location(module_name, str(source_path))

    if spec is None or spec.loader is None:
        raise ImportError(
            f"Nao foi possivel criar o carregador para: {source_path}"
        )

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module

    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise ImportError(
            f"Falha carregando SAPPhotoBot de {source_path}: "
            f"{type(exc).__name__}: {exc}"
        ) from exc

    bot_class = getattr(module, "SAPPhotoBot", None)

    if bot_class is None:
        public_names = ", ".join(
            name for name in dir(module) if not name.startswith("_")
        )
        raise ImportError(
            f"O arquivo {source_path} foi carregado, mas nao contem "
            f"SAPPhotoBot. Nomes encontrados: {public_names}"
        )

    return bot_class


SAPPhotoBot = _load_sap_photo_bot()

from modules.word_report import BASE_FILENAME, find_base_workbook
from modules.vision import (
    configure_tesseract,
    extract_coordinates_from_photo,
    extract_largest_photo_from_screen,
    locate_text_on_screen,
    screenshot_full,
)
from modules.window_control import all_window_titles, find_window_contains

st.set_page_config(page_title="Captura SAP - Fotos e Coordenadas", page_icon="📷", layout="wide")
