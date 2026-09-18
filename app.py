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

st.markdown("# 📷 Robô SAP — Fotos e Coordenadas")
st.caption("Automação local para controlar uma sessão RDP/SAP já aberta, localizar fotos específicas, capturar as imagens e extrair latitude/longitude por OCR.")

if not tesseract_cmd:
    st.error("Motor OCR não foi encontrado. Na versão EXE ele deve estar embutido; se esta mensagem aparecer, use a versão portátil/EXE gerada pelo build ou informe um caminho externo na aba Configuração.")

st.warning(
    "**Antes de executar:** deixe a Área Remota e o SAP abertos. Não use mouse/teclado durante o processamento. "
    "Para interromper imediatamente o PyAutoGUI, mova o mouse para o **canto superior esquerdo da tela**."
)

exec_tab, calib_tab, diag_tab, cfg_tab = st.tabs(["🚀 Executar", "🎯 Calibração", "🧪 Diagnóstico", "⚙️ Configuração"])

with exec_tab:
    c1, c2, c3 = st.columns([1.2, 1, 1])
    with c1:
        st.subheader("Obras")
        obras_text = st.text_area(
            "Cole um ou vários números de obra (um por linha ou separados por texto):",
            height=150,
            placeholder="1114882266\n1114307028",
        )
    with c2:
        st.subheader("Importar lista")
        up = st.file_uploader("CSV ou Excel (opcional)", type=["csv", "xlsx", "xls"])
        df_up, cols_up = read_uploaded_obras(up) if up else (pd.DataFrame(), [])
        selected_col = None
        if cols_up:
            preferred = next((c for c in cols_up if any(k in c.upper() for k in ["OBRA", "NOTA", "PROTOCOLO"])), cols_up[0])
            selected_col = st.selectbox("Coluna com os números", cols_up, index=cols_up.index(preferred))
            st.dataframe(df_up.head(10), use_container_width=True, height=220)
    with c3:
        st.subheader("Pré-checagem")
        remote_title = cfg["remote"].get("window_title_contains", "")
        rw = find_window_contains(remote_title) if remote_title else None
        st.write("Área Remota:", "🟢 detectada" if rw else "🔴 não detectada")
        st.write("OCR:", "🟢 disponível" if tesseract_cmd else "🔴 indisponível")
        st.write("Resolução local:", f"{pyautogui.size().width} × {pyautogui.size().height}")
        st.write("Saída:", str(cfg["output"].get("root_folder", "saida")))

    obras = parse_obras_text(obras_text)
    if selected_col and not df_up.empty:
        for v in df_up[selected_col].fillna("").astype(str):
            obras.extend(parse_obras_text(v))
    # dedupe mantendo ordem
    obras = list(dict.fromkeys(obras))

    if obras:
        st.info(f"{len(obras)} obra(s) preparada(s): {', '.join(obras[:8])}{'...' if len(obras) > 8 else ''}")

    run_col, folder_col = st.columns([1, 1])
    start = run_col.button("🚀 Iniciar processamento", type="primary", use_container_width=True, disabled=(not obras or not tesseract_cmd))
    if folder_col.button("📁 Abrir pasta de saída", use_container_width=True):
        out = Path(cfg["output"].get("root_folder", "saida"))
        if not out.is_absolute():
            out = APP_DIR / out
        out.mkdir(parents=True, exist_ok=True)
        try:
            if os.name == "nt":
                os.startfile(out)  # type: ignore[attr-defined]
            else:
                st.info(str(out))
        except Exception as e:
            st.error(f"Não foi possível abrir a pasta: {e}")

    if start:
        if not find_window_contains(cfg["remote"].get("window_title_contains", "")):
            st.error("A janela da Área Remota configurada não foi encontrada. Abra o RDP ou ajuste o título em Configuração.")
            st.stop()

        progress_bar = st.progress(0.0)
        status_box = st.status("Iniciando automação...", expanded=True)
        log_placeholder = st.empty()
        logs = []

        def callback(level, message, progress=None):
            icon = {"success": "✅", "warning": "⚠️", "error": "❌", "info": "ℹ️"}.get(level, "•")
            logs.append(f"{icon} {message}")
            log_placeholder.code("\n".join(logs[-18:]), language=None)
            if progress is not None:
                progress_bar.progress(max(0.0, min(1.0, float(progress))))

        try:
            bot = SAPPhotoBot(cfg, callback=callback)
            results = bot.process_many(obras)
            st.session_state.last_results = results
            progress_bar.progress(1.0)
            ok = sum(1 for r in results if r.get("ok"))
            fail = len(results) - ok
            status_box.update(label=f"Processamento finalizado: {ok} concluída(s), {fail} com erro", state="complete" if fail == 0 else "error")
        except Exception as exc:
            status_box.update(label=f"Execução interrompida: {exc}", state="error")
            st.exception(exc)

    results = st.session_state.get("last_results", [])
    if results:
        st.markdown("### Resultado da última execução")
        rows = []
        for r in results:
            rows.append({
                "OBRA": r.get("obra"),
                "STATUS": "OK" if r.get("ok") else "ERRO",
                "PASTA": str(r.get("folder", "")),
                "EXCEL": str(r.get("excel", "")),
                "ERRO": r.get("error", ""),
            })
        st.dataframe(pd.DataFrame(rows), use_container_width=True)

        for r in results:
            if not r.get("ok"):
                continue
            obra = r.get("obra")
            with st.expander(f"📦 Arquivos da obra {obra}"):
                recs = pd.DataFrame(r.get("records", []))
                if not recs.empty:
                    cols = [c for c in ["TIPO_FOTO", "LATITUDE", "LONGITUDE", "STATUS_LINK", "STATUS_COORDENADA", "ARQUIVO_FOTO"] if c in recs.columns]
                    st.dataframe(recs[cols], use_container_width=True)
                excel = Path(r.get("excel")) if r.get("excel") else None
                zip_path = Path(r.get("zip")) if r.get("zip") else None
                d1, d2 = st.columns(2)
                if excel and excel.exists():
                    d1.download_button(
                        "📊 Baixar Excel",
                        data=excel.read_bytes(),
                        file_name=excel.name,
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        key=f"excel_{obra}",
                        use_container_width=True,
                    )
                if zip_path and zip_path.exists():
                    d2.download_button(
                        "📦 Baixar pasta ZIP",
                        data=zip_path.read_bytes(),
                        file_name=zip_path.name,
                        mime="application/zip",
                        key=f"zip_{obra}",
                        use_container_width=True,
                    )

with calib_tab:
    st.subheader("Calibração por posição do mouse")
    st.info(
        "Use esta aba se a automação clicar fora do local esperado. Clique em **Capturar**, troque para a Área Remota, "
        "posicione o mouse exatamente sobre o elemento e aguarde 4 segundos. A posição será gravada em proporção à resolução da tela."
    )

    def capture_point_ui(point_key: str, label: str):
        current = cfg["points"][point_key]
        st.write(f"**{label}** — atual: `{current}`")
        if st.button(f"🎯 Capturar {label}", key=f"cap_{point_key}"):
            ph = st.empty()
            for sec in range(4, 0, -1):
                ph.warning(f"Vá para o RDP e posicione o mouse em **{label}**. Captura em {sec}s...")
                time.sleep(1)
            p = pyautogui.position()
            cfg["points"][point_key] = abs_point_to_norm((p.x, p.y), tuple(pyautogui.size()))
            persist_cfg(cfg)
            ph.success(f"Capturado: ({p.x}, {p.y}) → {cfg['points'][point_key]}")

    a, b = st.columns(2)
    with a:
        capture_point_ui("note_field_initial", "Campo Nota — tela inicial")
        capture_point_ui("dados_campo_2_fallback", "Aba Dados de Campo 2")
        capture_point_ui("permitir_fallback", "Botão Permitir")
    with b:
        capture_point_ui("note_field_detail", "Campo Nota — tela da nota")
        capture_point_ui("imagens_campo_fallback", "Aba Imagens de Campo")

    st.markdown("#### Regiões OCR")
    st.caption("Os valores são [x, y, largura, altura] normalizados entre 0 e 1. Os padrões foram montados com base nas telas 1920×1080 enviadas.")
    reg_edit = st.text_area("Editar regiões (JSON)", value=json.dumps(cfg["regions"], ensure_ascii=False, indent=2), height=270)
    if st.button("💾 Salvar regiões"):
        try:
            parsed = json.loads(reg_edit)
            for key in ["top_detection", "tabs", "links", "security_popup", "photo_content"]:
                if key not in parsed or len(parsed[key]) != 4:
                    raise ValueError(f"Região ausente/inválida: {key}")
            cfg["regions"] = parsed
            persist_cfg(cfg)
            st.success("Regiões salvas.")
        except Exception as e:
            st.error(f"JSON inválido: {e}")

with diag_tab:
    st.subheader("Diagnóstico")
    st.write("Motor OCR (Tesseract embutido/externo):", tesseract_cmd or "não encontrado")
    if tesseract_cmd:
        try:
            st.code(str(pytesseract.get_tesseract_version()))
        except Exception as e:
            st.warning(str(e))

    remote_title = cfg["remote"].get("window_title_contains", "")
    st.write("Janela remota configurada:", remote_title)
    st.write("Detectada:", bool(find_window_contains(remote_title)))

    if st.button("🪟 Mostrar títulos de janelas visíveis"):
        titles = all_window_titles()
        st.code("\n".join(titles[:80]) or "Nenhuma janela com título.")

    if st.button("🔎 Testar OCR das abas (captura em 3 segundos)", disabled=not tesseract_cmd):
        ph = st.empty()
        for sec in range(3, 0, -1):
            ph.info(f"Abra/focalize a tela SAP. Captura em {sec}s...")
            time.sleep(1)
        shot = screenshot_full()
        ph.empty()
        st.image(shot, caption="Screenshot de diagnóstico", use_container_width=True)
        for targets in [["Dados de Campo 2", "DadosdeCampo2"], ["Imagens de Campo"], ["Permitir"]]:
            region_key = "security_popup" if "Permitir" in targets else "tabs"
            found, _, _ = locate_text_on_screen(
                targets,
                cfg["regions"][region_key],
                lang=cfg["ocr"].get("lang", "eng"),
                threshold=45,
                psm=6,
            )
            st.write(f"{targets[0]}:", f"✅ {found.text} @ {found.center}" if found else "❌ não encontrado")

    if st.button("🌐 Testar detecção da foto e coordenada (captura em 3 segundos)", disabled=not tesseract_cmd):
        ph = st.empty()
        for sec in range(3, 0, -1):
            ph.info(f"Deixe a foto aberta/maximizada no RDP. Captura em {sec}s...")
            time.sleep(1)
        shot = screenshot_full()
        photo, bbox, cropped = extract_largest_photo_from_screen(shot, cfg["regions"]["photo_content"])
        coords = extract_coordinates_from_photo(
            photo,
            lang=cfg["ocr"].get("lang", "eng"),
            prefer_negative_lat=bool(cfg["ocr"].get("prefer_negative_latitude", True)),
        )
        ph.empty()
        st.image(photo, caption=f"Foto detectada (recorte automático={cropped}, bbox={bbox})", width=430)
        if coords.get("latitude") is not None:
            st.success(f"Coordenada: {coords['latitude']}, {coords['longitude']} — variante OCR: {coords.get('variant')}")
        else:
            st.error("Coordenada não reconhecida.")
        st.code(coords.get("ocr_text", "") or "(OCR vazio)")

with cfg_tab:
    st.subheader("Configuração")
    remote = st.text_input("Texto que identifica a janela da Área Remota", value=cfg["remote"].get("window_title_contains", ""))
    tcmd = st.text_input("Caminho alternativo do tesseract.exe (opcional)", value=cfg["ocr"].get("tesseract_cmd", ""), help="Na versão EXE o OCR já vem embutido. Preencha somente se quiser usar outro executável.")
    lang = st.text_input("Idioma OCR", value=cfg["ocr"].get("lang", "eng"), help="Use eng para máxima compatibilidade; por+eng se os dois pacotes estiverem instalados.")
    root = st.text_input("Pasta de saída", value=cfg["output"].get("root_folder", "saida"))
    threshold = st.slider("Sensibilidade do OCR de links (%)", min_value=40, max_value=95, value=int(cfg["ocr"].get("fuzzy_threshold", 70)))
    save_debug = st.checkbox("Salvar imagens de diagnóstico", value=bool(cfg["ocr"].get("save_debug_images", True)))
    prefer_negative = st.checkbox(
        "Corrigir latitude positiva para negativa (fluxo EQTL Maranhão)",
        value=bool(cfg["ocr"].get("prefer_negative_latitude", True)),
        help="O sinal de menos é pequeno no rodapé e pode sumir no OCR. Mantenha ativo para obras no Maranhão.",
    )
    activate = st.checkbox("Ativar a janela remota automaticamente antes das etapas", value=bool(cfg["remote"].get("activate_before_each_step", True)))

    st.markdown("#### Tempos")
    tc1, tc2, tc3 = st.columns(3)
    after_note = tc1.number_input("Após ENTER da obra (s)", 0.5, 15.0, float(cfg["timing"].get("after_note_enter", 2.5)), 0.5)
    after_permit = tc2.number_input("Após Permitir (s)", 0.5, 15.0, float(cfg["timing"].get("after_permit", 2.5)), 0.5)
    timeout = tc3.number_input("Timeout por tela (s)", 5.0, 60.0, float(cfg["timing"].get("screen_timeout", 18.0)), 1.0)

    st.markdown("#### Fotos procuradas")
    targets_json = st.text_area("Targets/aliases (JSON)", value=json.dumps(cfg["automation"]["targets"], ensure_ascii=False, indent=2), height=310)

    if st.button("💾 Salvar configuração", type="primary"):
        try:
            targets = json.loads(targets_json)
            if not isinstance(targets, list) or not targets:
                raise ValueError("targets deve ser uma lista não vazia")
            cfg["remote"]["window_title_contains"] = remote.strip()
            cfg["remote"]["activate_before_each_step"] = activate
            cfg["ocr"]["tesseract_cmd"] = tcmd.strip()
            cfg["ocr"]["lang"] = lang.strip() or "eng"
            cfg["ocr"]["fuzzy_threshold"] = threshold
            cfg["ocr"]["save_debug_images"] = save_debug
            cfg["ocr"]["prefer_negative_latitude"] = prefer_negative
            cfg["output"]["root_folder"] = root.strip() or "saida"
            cfg["timing"]["after_note_enter"] = after_note
            cfg["timing"]["after_permit"] = after_permit
            cfg["timing"]["screen_timeout"] = timeout
            cfg["automation"]["targets"] = targets
            persist_cfg(cfg)
            configure_ocr(cfg)
            st.success(f"Configuração salva em {CONFIG_PATH}")
        except Exception as e:
            st.error(f"Não foi possível salvar: {e}")

st.divider()
st.caption("A automação não instala nada dentro da área remota. Na versão EXE, Python, bibliotecas e OCR ficam empacotados no próprio aplicativo e ele opera apenas a sessão RDP/SAP já aberta.")
