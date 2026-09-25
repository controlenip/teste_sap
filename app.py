from __future__ import annotations

import io
import json
import os
import re
import subprocess
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
from modules.sap_bot import SAPPhotoBot
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


@st.cache_resource(show_spinner=False)
def _load_cfg_cached():
    return load_config()


def get_cfg():
    # Mantemos em session_state para refletir calibração sem reiniciar.
    if "cfg" not in st.session_state:
        st.session_state.cfg = load_config()
    return st.session_state.cfg


def persist_cfg(cfg):
    save_config(cfg)
    st.session_state.cfg = cfg


def configure_ocr(cfg):
    cmd = find_tesseract(cfg.get("ocr", {}).get("tesseract_cmd", ""))
    if cmd:
        cfg["ocr"]["tesseract_cmd"] = cmd
        configure_tesseract(cmd)
    return cmd


def parse_obras_text(text: str) -> list[str]:
    vals = []
    seen = set()
    for token in re.findall(r"\d{6,20}", str(text or "")):
        if token not in seen:
            seen.add(token)
            vals.append(token)
    return vals


def read_uploaded_obras(file) -> tuple[pd.DataFrame, list[str]]:
    if file is None:
        return pd.DataFrame(), []
    name = file.name.lower()
    data = file.getvalue()
    if name.endswith(".csv"):
        df = pd.read_csv(io.BytesIO(data), dtype=str)
    else:
        df = pd.read_excel(io.BytesIO(data), dtype=str)
    return df, df.columns.astype(str).tolist()


cfg = get_cfg()
tesseract_cmd = configure_ocr(cfg)

st.markdown("# 📷 Robô SAP — Fotos e Relatório Word")
st.caption("Automação local para coletar os links do SAP, salvar as fotos, extrair a coordenada da fachada e gerar um Word com uma obra por página.")

if not tesseract_cmd:
    st.error("Motor OCR não foi encontrado. Na versão EXE ele deve estar embutido; se esta mensagem aparecer, use a versão portátil/EXE gerada pelo build ou informe um caminho externo na aba Configuração.")

st.warning("**Antes de executar:** deixe a Área Remota e o SAP abertos. Não use mouse/teclado durante o processamento. Para interromper imediatamente o PyAutoGUI, mova o mouse para o **canto superior esquerdo da tela**.")

exec_tab, calib_tab, diag_tab, cfg_tab = st.tabs(["🚀 Executar", "🎯 Calibração", "🧪 Diagnóstico", "⚙️ Configuração"])
