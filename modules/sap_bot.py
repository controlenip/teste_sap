from __future__ import annotations

import ctypes
import json
import os
import re
import shutil
import subprocess
import time
import webbrowser
from ctypes import wintypes
from io import BytesIO
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest
from datetime import datetime
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path
from typing import Callable, Dict, Iterable, Optional

import pyautogui
import pyperclip
import pytesseract
from PIL import Image, ImageEnhance, ImageFilter, ImageOps

from .config import APP_DIR, resolve_output_root
from .excel_utils import save_work_excel, update_consolidated
from .vision import (
    click_text,
    extract_coordinates_from_photo,
    extract_largest_photo_from_screen,
    locate_text_on_screen,
    locate_link_filename_on_screen,
    locate_link_row_robust,
    locate_link_row_strict,
    locate_link_row_fullscreen,
    detect_link_row_centers,
    save_debug_image,
    wait_for_text,
)
from .window_control import (
    activate_window_contains,
    maximize_window_contains,
    norm_point_in_window_to_abs,
    get_window_region_contains,
    screenshot_window_contains,
    active_window_title,
)

ProgressCallback = Callable[[str, str, Optional[float]], None]


# ---------------------------------------------------------------------------
# Clipboard do Windows
# ---------------------------------------------------------------------------
# A Area de Trabalho Remota precisa estar com o redirecionamento de clipboard
# habilitado. Assim, o Ctrl+C feito dentro do SAP chega ao clipboard do PC local.
CF_UNICODETEXT = 13


def _clear_windows_clipboard() -> None:
    user32 = ctypes.windll.user32
    if user32.OpenClipboard(None):
        try:
            user32.EmptyClipboard()
        finally:
            user32.CloseClipboard()


def _read_windows_clipboard_text() -> str:
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = wintypes.HANDLE
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]

    if not user32.OpenClipboard(None):
        return ""

    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return ""
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return ""
        try:
            return ctypes.wstring_at(ptr)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def _extract_http_url(text: str) -> str:
    """Extrai a primeira URL HTTP/HTTPS do texto copiado do SAP."""
    raw = str(text or "").replace("\r", " ").replace("\n", " ").strip()
    if not raw:
        return ""
    match = re.search(r"https?://[^\s\t]+", raw, flags=re.IGNORECASE)
    if not match:
        return ""
    url = match.group(0).strip().strip('"').strip("'")
    return url.rstrip(";,)")


# A coordenada deve ser extraida SOMENTE da foto de fachada.
FACADE_ALIASES = (
    "FACHADADOIMOVEL",
    "FACHADAIMOVEL",
    "FACHADA DO IMOVEL",
)


class SAPPhotoBot:
    def __init__(self, cfg: Dict, callback: Optional[ProgressCallback] = None):
        self.cfg = cfg
        self.callback = callback or (lambda level, msg, progress=None: None)
        self.output_root = resolve_output_root(cfg)
        self.debug_root = APP_DIR / "debug"
        self.logs_root = APP_DIR / "logs"
        self.logs_root.mkdir(parents=True, exist_ok=True)
        self.debug_root.mkdir(parents=True, exist_ok=True)
        pyautogui.FAILSAFE = True
        pyautogui.PAUSE = 0.08

    @property
    def lang(self) -> str:
        return str(self.cfg.get("ocr", {}).get("lang", "eng")) or "eng"

    @property
    def threshold(self) -> float:
        return float(self.cfg.get("ocr", {}).get("fuzzy_threshold", 70))

    @property
    def remote_title(self) -> str:
        return str(self.cfg.get("remote", {}).get("window_title_contains", "")).strip()

    def emit(self, msg: str, level: str = "info", progress: Optional[float] = None):
        self.callback(level, msg, progress)
        try:
            with (self.logs_root / "execucao.log").open("a", encoding="utf-8") as f:
                f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} | {level.upper()} | {msg}\n")
        except Exception:
            pass

    def _activate_remote(self):
        if not self.cfg.get("remote", {}).get("activate_before_each_step", True):
            return None
        if not self.remote_title:
            raise RuntimeError("O título da Área Remota não está configurado.")
        return activate_window_contains(self.remote_title, wait=0.35)

    def _prepare_remote_for_automation(self):
        """Ativa a Area Remota sem alterar o tamanho da janela.

        O usuario ja utiliza o RDP em tela cheia. Forcar maximizacao aqui pode
        alterar foco/escala desnecessariamente, portanto esta versao apenas
        garante que a sessao esteja ativa.
        """
        self.emit("Preparando Área Remota...")
        self._activate_remote()
        time.sleep(0.5)

    def _click_point(self, key: str):
        """Clica em um ponto normalizado RELATIVO À JANELA RDP."""
        point = self.cfg["points"][key]
        x, y = norm_point_in_window_to_abs(
            self.remote_title,
            point,
            content_only=False,
        )
        pyautogui.click(x, y)

    def _is_initial_note_screen(self) -> bool:
        found, _, _ = locate_text_on_screen(
            ["1a tela", "1ª tela", "1 tela", "primeira tela"],
            self.cfg["regions"]["top_detection"],
            lang=self.lang,
            threshold=55,
            psm=6,
            window_title=self.remote_title,
        )
        return found is not None

    def _enter_note(self, obra: str):
        """
        Informa o número da obra e aguarda a abertura da nota.

        A confirmação usa uma área ampla do topo da sessão RDP.
        Se o OCR não conseguir confirmar a tela, o processo não
        é abortado imediatamente: a etapa seguinte (Dados de Campo 2)
        fará uma nova validação.
        """
        self._activate_remote()

        point_key = (
            "note_field_initial"
            if self._is_initial_note_screen()
            else "note_field_detail"
        )

        self._click_point(point_key)
        time.sleep(0.25)

        pyautogui.hotkey("ctrl", "a")
        time.sleep(0.10)
        pyautogui.press("backspace")
        time.sleep(0.10)

        pyautogui.write(
            str(obra),
            interval=float(
                self.cfg.get("automation", {}).get("typing_interval", 0.035)
            ),
        )

        time.sleep(0.20)
        pyautogui.press("enter")

        self.emit(f"Obra {obra}: número informado e ENTER enviado.")

        wait_after_enter = float(
            self.cfg.get("timing", {}).get("after_note_enter", 3.0)
        )
        time.sleep(max(1.5, wait_after_enter))

        # Região ampla do topo, relativa SOMENTE à janela RDP.
        region_confirmacao = [0.00, 0.00, 1.00, 0.32]

        targets = [
            "Dados Gerais",
            "Informações Gerais",
            "Informacoes Gerais",
            "Dados de Campo",
            "Dados de Campo 2",
            "Orçamento de Conexão",
            "Orcamento de Conexao",
            "Status da nota",
            "Exibir nota de serviço",
            "Exibir nota de servico",
        ]

        ok = wait_for_text(
            targets,
            region_confirmacao,
            lang=self.lang,
            threshold=45,
            timeout=float(self.cfg.get("timing", {}).get("screen_timeout", 8.0)),
            poll_interval=float(self.cfg.get("timing", {}).get("poll_interval", 0.45)),
            window_title=self.remote_title,
        )

        if ok:
            self.emit(f"Obra {obra}: tela da nota reconhecida.")
            return

        # A nota pode estar aberta mesmo quando o OCR falhar por escala,
        # fonte pequena ou qualidade do RDP. Não aborta aqui.
        self.emit(
            (
                f"Obra {obra}: a nota aparenta ter sido aberta, "
                f"mas o OCR não conseguiu confirmar a tela. "
                f"Continuando para Dados de Campo 2..."
            ),
            level="warning",
        )

    def _open_images_tab(self):
        """Abre Dados de Campo 2 e, obrigatoriamente, Imagens de Campo.

        IMPORTANTE:
        Nesta versao os dois cliques NAO usam mais os pontos antigos do
        config.json. O bug anterior acontecia porque o config continha uma
        coordenada antiga para ``imagens_campo_fallback``; como a chave existia,
        o codigo nunca chegava ao ponto fixo correto.

        Os pontos abaixo sao relativos a janela RDP maximizada e foram obtidos
        diretamente das telas SAP enviadas pelo usuario.
        """
        self._activate_remote()

        # --------------------------------------------------------
        # 1) DADOS DE CAMPO 2
        # --------------------------------------------------------
        self.emit("Clicando em Dados de Campo 2...")

        x, y = norm_point_in_window_to_abs(
            self.remote_title,
            (0.317, 0.197),
            content_only=False,
        )
        pyautogui.moveTo(x, y, duration=0.20)
        pyautogui.click()

        # O SAP precisa de um pequeno tempo para montar as subabas.
        time.sleep(max(1.8, float(self.cfg.get("timing", {}).get("after_tab_click", 1.2))))

        # --------------------------------------------------------
        # 2) IMAGENS DE CAMPO
        # --------------------------------------------------------
        self.emit("Clicando em Imagens de Campo...")

        # Ponto fixo da subaba Imagens de Campo na sessao RDP maximizada.
        x, y = norm_point_in_window_to_abs(
            self.remote_title,
            (0.292, 0.243),
            content_only=False,
        )
        pyautogui.moveTo(x, y, duration=0.20)
        pyautogui.click()

        # Segunda tentativa no mesmo ponto caso o primeiro clique ocorra durante
        # a atualizacao visual do SAP. Nao e doubleClick; sao dois cliques com
        # intervalo para evitar abrir controles indevidos.
        time.sleep(0.55)
        pyautogui.click(x, y)

        time.sleep(max(2.0, float(self.cfg.get("timing", {}).get("after_tab_click", 1.2))))

        # --------------------------------------------------------
        # 3) VALIDACAO DA GRADE DE LINKS
        # --------------------------------------------------------
        # Essa validacao e informativa. A busca dos JPGs logo depois e a
        # validacao real, entao o fluxo nao e abortado se o OCR falhar aqui.
        links = wait_for_text(
            ["Links", "FACHADA", "ADESIVO", "PANORAMICA", "JPG"],
            [0.00, 0.20, 0.72, 0.55],
            lang=self.lang,
            threshold=32,
            timeout=5.0,
            poll_interval=0.35,
            window_title=self.remote_title,
        )

        if links:
            self.emit("Imagens de Campo aberta; grade de links detectada.")
        else:
            self.emit(
                "Imagens de Campo foi clicada. O OCR nao confirmou a grade, "
                "mas o robo seguira para procurar diretamente os tres JPGs.",
                level="warning",
            )

    def _save_ocr_debug(self, obra: str, target_key: str, image: Image.Image, label: str):
        if not self.cfg.get("ocr", {}).get("save_debug_images", True):
            return
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        save_debug_image(image, self.debug_root / f"{obra}_{target_key}_{label}_{stamp}.png")

    # ------------------------------------------------------------------
    # COLETA DE LINKS POR PRINT + OCR - V16 MULTIMONITOR
    # ------------------------------------------------------------------
    # IMPORTANTE:
    # Nesta versao NENHUM hyperlink da grade SAP e clicado.
    # A Area Remota serve somente para:
    #   1) abrir Dados de Campo 2 / Imagens de Campo;
    #   2) tirar prints da grade Links;
    #   3) rolar a grade quando houver mais linhas.
    # Depois que os links sao reconstruidos e gravados em TXT, o processamento
    # passa para o navegador padrao do COMPUTADOR LOCAL.

    _KNOWN_PHOTO_LABELS = (
        "NUMEROPOSTECONEXAOCLIENT",
        "ADESIVOLIGACAONOVA",
        "PADRAODEMEDICAO",
        "IMOVELSEMREDE",
        "FOTOPANORAMICA",
        "FACHADADOIMOVEL",
        "FACHADAIMOVEL",
        "MEDIDORVIZINHOESQUERDO",
        "MEDIDORVIZINHODIREITO",
        "NUMEROTRANSFORMADOR",
        "FORMULARIODEREJEICAO",
    )

    _PHOTO_NUMBER_BY_LABEL = {
        "FACHADADOIMOVEL": 11,
        "FACHADAIMOVEL": 11,
        "ADESIVOLIGACAONOVA": 2,
        "PADRAODEMEDICAO": 30,
        "IMOVELSEMREDE": 17,
        "FOTOPANORAMICA": 42,
        "NUMEROPOSTECONEXAOCLIENT": 26,
        "MEDIDORVIZINHOESQUERDO": 25,
        "MEDIDORVIZINHODIREITO": 24,
        "NUMEROTRANSFORMADOR": 28,
        "FORMULARIODEREJEICAO": 15,
    }

    _PHOTO_BASE_URL = (
        "https://eapspddope01.equatorial.corp/ma/barramento/EQTL_MA"
    )

    def _best_photo_label_from_row(self, raw: str) -> str:
        """Identifica o nome da foto mesmo com pequenos erros de OCR."""
        compact = re.sub(r"[^A-Z0-9]", "", str(raw or "").upper())
        compact = compact.replace("JPG", "")
        if not compact:
            return ""

        # Match direto.
        for candidate in self._KNOWN_PHOTO_LABELS:
            if candidate in compact:
                return "FACHADADOIMOVEL" if candidate == "FACHADAIMOVEL" else candidate

        # Match fuzzy em janelas do tamanho do nome conhecido.
        best = ""
        best_score = 0.0
        for candidate in self._KNOWN_PHOTO_LABELS:
            clen = len(candidate)
            scores = [SequenceMatcher(None, compact, candidate).ratio()]
            if len(compact) >= max(5, clen - 6):
                lo = max(4, clen - 6)
                hi = min(len(compact), clen + 8)
                for size in range(lo, hi + 1):
                    for pos in range(0, max(1, len(compact) - size + 1)):
                        piece = compact[pos:pos + size]
                        scores.append(SequenceMatcher(None, piece, candidate).ratio())
            score = max(scores)
            if score > best_score:
                best = candidate
                best_score = score

        if best_score >= 0.66:
            return "FACHADADOIMOVEL" if best == "FACHADAIMOVEL" else best
        return ""

    def _numeric_ocr_text(self, value: str) -> str:
        """Versao do OCR usada APENAS para procurar datas/numeros."""
        table = str.maketrans({
            "O": "0",
            "Q": "0",
            "I": "1",
            "L": "1",
            "G": "6",
        })
        return str(value or "").upper().translate(table)

    def _valid_photo_date(self, raw: str) -> str:
        digits = re.sub(r"\D", "", str(raw or ""))
        if len(digits) != 8 or not digits.startswith("20"):
            return ""
        try:
            year = int(digits[:4])
            month = int(digits[4:6])
            day = int(digits[6:8])
        except ValueError:
            return ""
        if 2020 <= year <= 2035 and 1 <= month <= 12 and 1 <= day <= 31:
            return digits
        return ""

    def _extract_date_from_row(self, row_text: str) -> str:
        """Extrai AAAAMMDD da linha OCR com tolerancia a O/I/L/G."""
        numeric = self._numeric_ocr_text(row_text)

        # Prioriza data perto de PHOTO/OFS PHOTO.
        zones = []
        for token in ("PHOTO", "PH0T0", "FOTO"):
            pos = numeric.find(token)
            if pos >= 0:
                zones.append(numeric[pos:pos + 120])
        zones.append(numeric)

        for zone in zones:
            for candidate in re.findall(r"(?<!\d)(20\d{6})(?!\d)", zone):
                valid = self._valid_photo_date(candidate)
                if valid:
                    return valid
        return ""

    def _extract_photo_number_from_row(self, row_text: str, label: str) -> Optional[int]:
        if label in self._PHOTO_NUMBER_BY_LABEL:
            return int(self._PHOTO_NUMBER_BY_LABEL[label])

        work = (
            str(row_text or "").upper()
            .replace("F0T0", "FOTO")
            .replace("F0TO", "FOTO")
            .replace("FOT0", "FOTO")
        )
        matches = list(re.finditer(r"FOTO\s*[_\-: ]*\s*(\d{1,3})\b", work))
        if matches:
            try:
                return int(matches[-1].group(1))
            except ValueError:
                return None
        return None

    def _extract_generic_photo_label(self, row_text: str) -> str:
        """Fallback para tipos de foto ainda nao cadastrados no codigo."""
        work = str(row_text or "").upper()
        work = work.replace("F0T0", "FOTO").replace("FOT0", "FOTO")
        m = re.search(
            r"FOTO\s*[_\-: ]*\s*\d{1,3}\s+([A-Z][A-Z0-9_\- ]{3,}?)\s*\.\s*J(?:P|F)?G",
            work,
        )
        if not m:
            return ""
        label = re.sub(r"[^A-Z0-9]", "", m.group(1))
        return label[:80]

    def _parse_row_to_url(self, row_text: str, obra: str) -> str:
        """Reconstrui UMA URL sem clicar no SAP."""
        if not row_text or len(str(row_text).strip()) < 8:
            return ""

        label = self._best_photo_label_from_row(row_text)
        if not label:
            label = self._extract_generic_photo_label(row_text)
        if not label:
            return ""

        photo_number = self._extract_photo_number_from_row(row_text, label)
        if photo_number is None:
            return ""

        photo_date = self._extract_date_from_row(row_text)
        if not photo_date:
            return ""

        month = photo_date[:6]
        return (
            f"{self._PHOTO_BASE_URL}/{month}/"
            f"OFS_PHOTO_{photo_date}_{obra}_EQ_FOTO_{photo_number}_{label}.jpg"
        )

    def _capture_links_candidate_area(
        self,
        page: int,
        temp_dir: Path,
    ) -> tuple[Image.Image, tuple[int, int, int, int]]:
        """Captura uma area AMPLA que sempre contem a grade Links.

        A captura e relativa somente a janela RDP. Nao clica em hyperlink.
        Funciona tanto com o RDP maximizado quanto redimensionado porque usa
        proporcoes da propria janela remota.
        """
        full, remote_abs = screenshot_window_contains(
            self.remote_title,
            content_only=False,
        )

        # Diagnostico importante: a captura precisa conter a janela RDP inteira.
        # Na versao anterior, quando o RDP estava no segundo monitor, o limite
        # baseado no monitor principal reduzia a imagem para apenas 1 pixel de
        # largura. Isso tornava qualquer OCR impossivel.
        if full.width < 500 or full.height < 400:
            raise RuntimeError(
                'Captura da Area Remota invalida para OCR: '
                f'{full.width}x{full.height} px; regiao absoluta={remote_abs}. '
                'Atualize modules/window_control.py para a versao multimonitor.'
            )

        # Regiao propositalmente ampla: pega titulo Links, todas as linhas
        # visiveis e parte vazia inferior. O detector de linhas abaixo recorta
        # cada hyperlink individualmente.
        x = int(full.width * 0.005)
        y = int(full.height * 0.16)
        w = int(full.width * 0.70)
        h = int(full.height * 0.64)
        w = max(1, min(w, full.width - x))
        h = max(1, min(h, full.height - y))

        crop = full.crop((x, y, x + w, y + h))
        temp_dir.mkdir(parents=True, exist_ok=True)
        crop.save(temp_dir / f"pagina_{page:02d}.png")

        abs_region = (
            remote_abs[0] + x,
            remote_abs[1] + y,
            crop.width,
            crop.height,
        )
        return crop, abs_region

    def _detect_link_baselines(self, image: Image.Image) -> list[int]:
        """Detecta a linha sublinhada de cada hyperlink da grade SAP.

        Os links do SAP sao sublinhados; isso cria picos horizontais muito
        fortes. Esse metodo nao depende de reconhecer a palavra 'Links'.
        """
        try:
            import numpy as np
        except Exception:
            return []

        gray = np.array(ImageOps.grayscale(image))
        # Conta pixels suficientemente escuros por linha horizontal.
        dark_counts = (gray < 220).sum(axis=1)
        threshold = max(60, int(image.width * 0.18))

        # Non-maximum suppression: escolhe picos fortes separados entre si.
        # Distancia minima acompanha a escala da captura.
        min_distance = max(8, int(image.height * 0.025))
        peaks: list[int] = []
        for yy in np.argsort(dark_counts)[::-1].tolist():
            if int(dark_counts[yy]) < threshold:
                break
            y = int(yy)
            if all(abs(y - old) >= min_distance for old in peaks):
                peaks.append(y)

        peaks.sort()

        # Mantem apenas a faixa plausivel da tabela. Linhas muito no topo
        # normalmente pertencem às abas; muito embaixo sao bordas vazias.
        result = [
            y for y in peaks
            if int(image.height * 0.06) <= y <= int(image.height * 0.82)
        ]
        return result[:40]

    def _ocr_one_link_row(self, row: Image.Image) -> str:
        """OCR dedicado para uma unica linha de hyperlink."""
        if row.width < 80 or row.height < 5:
            return ""

        # Faz a linha ter altura suficiente para o Tesseract.
        scale = max(3, min(7, int(round(90 / max(1, row.height)))))
        enlarged = row.resize(
            (row.width * scale, row.height * scale),
            Image.Resampling.LANCZOS,
        )
        gray = ImageOps.grayscale(enlarged)
        contrast = ImageEnhance.Contrast(gray).enhance(2.35)
        sharp = contrast.filter(ImageFilter.SHARPEN)

        variants = [sharp]
        try:
            # Variante binaria ajuda quando a fonte esta muito pequena.
            binary = sharp.point(lambda p: 255 if p > 195 else 0)
            variants.append(binary)
        except Exception:
            pass

        best = ""
        best_rank = (-1, -1)
        for variant in variants:
            for psm in (7, 6):
                try:
                    text = pytesseract.image_to_string(
                        variant,
                        lang=self.lang,
                        config=f"--psm {psm}",
                    ).strip()
                except Exception:
                    try:
                        text = pytesseract.image_to_string(
                            variant,
                            lang="eng",
                            config=f"--psm {psm}",
                        ).strip()
                    except Exception:
                        text = ""

                upper = text.upper()
                rank = (
                    int("FOTO" in upper or "F0T0" in upper or "PHOTO" in upper),
                    len(text),
                )
                if rank > best_rank:
                    best_rank = rank
                    best = text
        return best

    def _ocr_links_from_candidate_area(
        self,
        image: Image.Image,
        obra: str,
    ) -> tuple[list[str], list[str]]:
        """Le os hyperlinks linha por linha e retorna URLs + OCR bruto."""
        urls: list[str] = []
        debug_lines: list[str] = []
        seen: set[str] = set()

        baselines = self._detect_link_baselines(image)

        # Cada baseline e a linha sublinhada do hyperlink. Recortamos uma faixa
        # pouco acima dela, onde ficam as letras.
        estimated_gap = 0
        if len(baselines) >= 2:
            gaps = [b - a for a, b in zip(baselines, baselines[1:]) if b > a]
            if gaps:
                gaps_sorted = sorted(gaps)
                estimated_gap = gaps_sorted[len(gaps_sorted) // 2]
        if estimated_gap <= 0:
            estimated_gap = max(14, int(image.height * 0.045))

        row_up = max(10, int(estimated_gap * 0.88))
        row_down = max(3, int(estimated_gap * 0.22))

        for idx, baseline in enumerate(baselines, start=1):
            y1 = max(0, baseline - row_up)
            y2 = min(image.height, baseline + row_down)
            row = image.crop((0, y1, image.width, y2))
            text = self._ocr_one_link_row(row)
            debug_lines.append(f"ROW {idx:02d} Y={baseline}: {text}")

            url = self._parse_row_to_url(text, obra)
            if url and url not in seen:
                seen.add(url)
                urls.append(url)

        # Fallback: OCR do quadro inteiro. Serve quando uma linha sublinhada
        # nao gerou pico suficiente.
        if len(urls) < 2:
            scale = 3
            enlarged = image.resize(
                (image.width * scale, image.height * scale),
                Image.Resampling.LANCZOS,
            )
            full_ocr = ImageEnhance.Contrast(
                ImageOps.grayscale(enlarged)
            ).enhance(2.1).filter(ImageFilter.SHARPEN)
            try:
                text = pytesseract.image_to_string(
                    full_ocr,
                    lang=self.lang,
                    config="--psm 6",
                )
            except Exception:
                text = pytesseract.image_to_string(
                    full_ocr,
                    lang="eng",
                    config="--psm 6",
                )
            debug_lines.append("\nFULL OCR:\n" + str(text))
            for line in str(text or "").splitlines():
                url = self._parse_row_to_url(line, obra)
                if url and url not in seen:
                    seen.add(url)
                    urls.append(url)

        return urls, debug_lines

    def _collect_all_urls_from_remote(self, obra: str, obra_dir: Path) -> list[str]:
        """CAPTURA os links do SAP por screenshot/OCR, sem abrir nenhum deles."""
        self._activate_remote()
        time.sleep(0.50)

        temp_dir = obra_dir / "_temp_links_ocr"
        temp_dir.mkdir(parents=True, exist_ok=True)

        self.emit(
            f"Obra {obra}: capturando a grade Links por print. "
            "Nenhum hyperlink sera aberto na Area Remota."
        )

        urls: list[str] = []
        seen: set[str] = set()
        all_debug: list[str] = []
        max_pages = int(self.cfg.get("automation", {}).get("max_link_pages", 12))
        pages_without_new = 0

        # A primeira captura informa onde mover o mouse para rolar somente a
        # grade. Nao ha clique de mouse nos hyperlinks.
        first_image, first_abs = self._capture_links_candidate_area(1, temp_dir)
        fx, fy, fw, fh = first_abs
        scroll_x = fx + int(fw * 0.45)
        scroll_y = fy + int(fh * 0.48)

        # Leva a grade ao topo apenas com a roda do mouse.
        pyautogui.moveTo(scroll_x, scroll_y, duration=0.12)
        for _ in range(10):
            pyautogui.scroll(10)
            time.sleep(0.04)
        time.sleep(0.45)

        for page in range(1, max_pages + 1):
            image, abs_region = self._capture_links_candidate_area(page, temp_dir)
            page_urls, debug_lines = self._ocr_links_from_candidate_area(image, obra)
            all_debug.append(
                f"\n===== PAGINA {page:02d} =====\n" + "\n".join(debug_lines)
            )

            before = len(urls)
            for url in page_urls:
                if url in seen:
                    continue
                seen.add(url)
                urls.append(url)
                self.emit(
                    f"Obra {obra}: link {len(urls)} capturado pelo print da grade.",
                    level="success",
                )

            new_count = len(urls) - before
            self.emit(
                f"Obra {obra}: pagina {page} analisada ({image.width}x{image.height}px); "
                f"{new_count} novo(s) link(s), {len(urls)} no total."
            )

            if new_count == 0:
                pages_without_new += 1
            else:
                pages_without_new = 0

            if pages_without_new >= 2:
                break

            # Apenas rolagem; NUNCA click() na grade Links.
            ax, ay, aw, ah = abs_region
            pyautogui.moveTo(ax + int(aw * 0.45), ay + int(ah * 0.48), duration=0.10)
            pyautogui.scroll(-8)
            time.sleep(0.65)

        # Sempre preserva o OCR bruto ate o TXT ser criado. Se der erro, ele e
        # muito util para diagnostico.
        try:
            (temp_dir / "ocr_debug.txt").write_text(
                "\n".join(all_debug),
                encoding="utf-8",
            )
        except Exception:
            pass

        if not urls:
            raise RuntimeError(
                "Nenhum link foi capturado nos prints da grade Links. "
                "Nenhum hyperlink foi clicado. Os arquivos pagina_XX.png e "
                "ocr_debug.txt foram mantidos em _temp_links_ocr."
            )

        return urls

    def _create_links_notepad(self, obra: str, obra_dir: Path, urls: list[str]) -> Path:
        """Cria o bloco de notas da obra com um link por linha e o abre."""
        txt_path = obra_dir / f"{obra}_links.txt"
        txt_path.write_text("\n".join(urls) + "\n", encoding="utf-8")

        try:
            subprocess.Popen(["notepad.exe", str(txt_path)])
            time.sleep(0.8)
        except Exception as exc:
            self.emit(
                f"Obra {obra}: bloco de notas criado, mas nao abriu automaticamente: {exc}",
                level="warning",
            )

        self.emit(
            f"Obra {obra}: {len(urls)} link(s) gravado(s) em {txt_path.name}.",
            level="success",
        )

        # Os prints da grade sao apenas temporarios. So removemos depois que
        # o TXT foi realmente gravado com sucesso. Se houver erro antes daqui,
        # eles permanecem para diagnostico.
        try:
            shutil.rmtree(obra_dir / "_temp_links_ocr", ignore_errors=True)
        except Exception:
            pass

        return txt_path

    def _read_links_from_notepad_file(self, txt_path: Path) -> list[str]:
        """Le os links do TXT; este arquivo vira a fonte local do processamento."""
        lines = txt_path.read_text(encoding="utf-8").splitlines()
        out: list[str] = []
        seen: set[str] = set()
        for line in lines:
            url = line.strip()
            if not url or not url.lower().startswith(("http://", "https://")):
                continue
            if url in seen:
                continue
            seen.add(url)
            out.append(url)
        return out

    def _return_to_initial_and_load_next(
        self,
        current_obra: str,
        next_obra: Optional[str] = None,
    ) -> None:
        """
        Depois de copiar/gravar os links da obra atual:

        1. volta imediatamente para a tela inicial do SAP com ESC;
        2. seleciona o campo Nota;
        3. apaga o numero da obra atual;
        4. se houver proxima obra, digita o proximo numero e pressiona ENTER;
        5. se for a ultima obra, deixa o campo Nota vazio.

        Esta rotina roda ANTES do processamento das fotos no navegador local.
        Assim, em uma lista com varias obras, toda a coleta de links no SAP e
        feita primeiro, obra por obra, e somente depois o robo passa a baixar
        as fotos no navegador do computador.
        """
        self._activate_remote()

        self.emit(
            f"Obra {current_obra}: links copiados. Voltando para a tela inicial do SAP..."
        )

        # Pela tela enviada pelo usuario, um ESC sai da nota aberta e volta
        # diretamente para "Exibir nota de servico: 1a tela".
        pyautogui.press("esc")
        time.sleep(1.35)

        # Se o SAP ainda estiver na tela detalhada por atraso de resposta,
        # faz uma segunda tentativa segura.
        try:
            if not self._is_initial_note_screen():
                pyautogui.press("esc")
                time.sleep(1.15)
        except Exception:
            # O OCR da tela inicial pode falhar por escala; nao impede o uso
            # do ponto fixo do campo Nota.
            pass

        # Campo Nota da tela inicial. Usa a calibracao que ja vinha funcionando
        # no fluxo normal de entrada das obras.
        try:
            self._click_point("note_field_initial")
        except Exception:
            # Fallback proporcional observado na tela inicial 1920x1080.
            x, y = norm_point_in_window_to_abs(
                self.remote_title,
                (0.122, 0.193),
                content_only=False,
            )
            pyautogui.click(x, y)

        time.sleep(0.20)
        pyautogui.hotkey("ctrl", "a")
        time.sleep(0.08)
        pyautogui.press("backspace")
        time.sleep(0.12)

        next_digits = "".join(ch for ch in str(next_obra or "") if ch.isdigit())

        if not next_digits:
            self.emit(
                f"Obra {current_obra}: ultima obra da lista. Campo Nota foi limpo.",
                level="success",
            )
            return

        pyautogui.write(
            next_digits,
            interval=float(
                self.cfg.get("automation", {}).get("typing_interval", 0.035)
            ),
        )
        time.sleep(0.15)
        pyautogui.press("enter")

        self.emit(
            f"Proxima obra {next_digits}: numero informado e ENTER enviado.",
            level="success",
        )

        # Deixa a proxima nota completamente carregada para a proxima iteracao
        # da fase SAP. Nao abre Dados de Campo 2 aqui; isso ocorre no inicio da
        # iteracao seguinte.
        wait_after_enter = float(
            self.cfg.get("timing", {}).get("after_note_enter", 3.0)
        )
        time.sleep(max(1.5, wait_after_enter))

    def _process_local_links_for_obra(
        self,
        obra: str,
        obra_dir: Path,
        txt_path: Path,
        all_urls: list[str],
        progress_start: float = 0.40,
        progress_end: float = 0.96,
    ) -> dict:
        """Processa localmente os links ja coletados do SAP.

        Nesta etapa o SAP/RDP nao e usado para abrir fotos. O TXT da obra e a
        unica fonte de URLs.
        """
        records: list[dict] = []
        urls_from_txt = self._read_links_from_notepad_file(txt_path)
        total = len(urls_from_txt)
        facade_found = False
        facade_lat = None
        facade_lon = None

        for index, url in enumerate(urls_from_txt, start=1):
            progress = progress_start + (
                (progress_end - progress_start) * (index - 1) / max(1, total)
            )
            self.emit(
                f"Obra {obra}: processando link {index}/{total} do bloco de notas...",
                progress=progress,
            )

            try:
                photo_result = self._process_url_from_notepad(
                    obra, index, total, url, obra_dir
                )

                is_facade = bool(photo_result.get("is_facade"))
                path = photo_result.get("path")
                lat = photo_result.get("latitude") if is_facade else None
                lon = photo_result.get("longitude") if is_facade else None

                if is_facade:
                    facade_found = True
                    facade_lat = lat
                    facade_lon = lon

                status_link = "SALVO" if path else "ERRO AO SALVAR"
                if is_facade:
                    coord_status = (
                        "OK"
                        if lat is not None and lon is not None
                        else "COORDENADA NAO RECONHECIDA"
                    )
                else:
                    coord_status = "NAO APLICAVEL - SOMENTE FACHADA"

                records.append({
                    "OBRA": obra,
                    "ORDEM": index,
                    "TIPO_FOTO": (
                        "FACHADADOIMOVEL"
                        if is_facade
                        else photo_result.get("filename", f"FOTO_{index:03d}")
                    ),
                    "LINK_IDENTIFICADO_OCR": url,
                    "LATITUDE": lat if lat is not None else "",
                    "LONGITUDE": lon if lon is not None else "",
                    "ARQUIVO_FOTO": str(path) if path else "",
                    "STATUS_LINK": status_link,
                    "STATUS_COORDENADA": coord_status,
                    "OCR_RODAPE": photo_result.get("ocr_text", "") if is_facade else "",
                    "ARQUIVO_LINKS": str(txt_path),
                    "DATA_HORA": datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
                })

                if path:
                    self.emit(
                        f"Obra {obra}: foto {index}/{total} salva em {Path(path).name}.",
                        level="success",
                    )
                else:
                    self.emit(
                        f"Obra {obra}: link {index}/{total} abriu, mas a foto nao foi salva.",
                        level="warning",
                    )

                if is_facade:
                    if lat is not None and lon is not None:
                        self.emit(
                            f"Obra {obra}: coordenada da FACHADA DO IMOVEL = "
                            f"{lat:.6f}, {lon:.6f}.",
                            level="success",
                        )
                    else:
                        self.emit(
                            f"Obra {obra}: FACHADA DO IMOVEL salva, mas a coordenada "
                            "nao foi reconhecida.",
                            level="warning",
                        )

            except Exception as exc:
                records.append({
                    "OBRA": obra,
                    "ORDEM": index,
                    "TIPO_FOTO": "",
                    "LINK_IDENTIFICADO_OCR": url,
                    "LATITUDE": "",
                    "LONGITUDE": "",
                    "ARQUIVO_FOTO": "",
                    "STATUS_LINK": "ERRO LOCAL",
                    "STATUS_COORDENADA": "NAO PROCESSADA",
                    "OCR_RODAPE": "",
                    "ARQUIVO_LINKS": str(txt_path),
                    "DATA_HORA": datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
                })
                self.emit(
                    f"Obra {obra}: erro no link {index}/{total}: {exc}",
                    level="error",
                )
                if not self.cfg.get("automation", {}).get("continue_when_photo_missing", True):
                    raise

        if not facade_found:
            self.emit(
                f"Obra {obra}: nenhuma URL de FACHADA DO IMOVEL foi encontrada no bloco de notas.",
                level="warning",
            )

        excel_path = save_work_excel(records, obra_dir / f"{obra}_coordenadas.xlsx")
        if self.cfg.get("output", {}).get("create_consolidated_excel", True):
            update_consolidated(records, self.output_root / "resumo_geral.xlsx")

        zip_path = None
        if self.cfg.get("output", {}).get("create_zip", True):
            zip_base = obra_dir.parent / f"{obra}"
            zip_result = shutil.make_archive(str(zip_base), "zip", root_dir=obra_dir)
            zip_path = Path(zip_result)

        self.emit(
            f"Obra {obra}: processamento local concluido. "
            f"{len(all_urls)} link(s), {len(records)} registro(s).",
            level="success",
            progress=progress_end,
        )

        return {
            "obra": obra,
            "folder": obra_dir,
            "excel": excel_path,
            "links_txt": txt_path,
            "zip": zip_path,
            "records": records,
            "fachada_latitude": facade_lat,
            "fachada_longitude": facade_lon,
            "ok": True,
        }

    def _safe_photo_filename(self, url: str, index: int) -> str:
        parsed = urlparse.urlparse(url)
        raw_name = urlparse.unquote(Path(parsed.path).name).strip()
        if not raw_name:
            raw_name = f"foto_{index:03d}.jpg"
        raw_name = re.sub(r'[<>:"/\\|?*]+', "_", raw_name).strip(" .")
        if not raw_name:
            raw_name = f"foto_{index:03d}.jpg"
        if "." not in raw_name:
            raw_name += ".jpg"
        return raw_name

    def _unique_photo_path(self, obra_dir: Path, filename: str) -> Path:
        path = obra_dir / filename
        if not path.exists():
            return path
        stem = path.stem
        suffix = path.suffix or ".jpg"
        n = 2
        while True:
            candidate = obra_dir / f"{stem}_{n}{suffix}"
            if not candidate.exists():
                return candidate
            n += 1

    def _is_facade_url(self, url: str) -> bool:
        normalized = urlparse.unquote(str(url)).upper()
        compact = re.sub(r"[^A-Z0-9]", "", normalized)
        return any(re.sub(r"[^A-Z0-9]", "", a) in compact for a in FACADE_ALIASES)

    def _open_url_from_txt_in_default_browser(
        self, obra: str, index: int, total: int, url: str
    ):
        self.emit(
            f"Obra {obra}: abrindo link {index}/{total} do bloco de notas no navegador padrao..."
        )
        pyperclip.copy(url)
        try:
            webbrowser.open(url, new=2, autoraise=True)
        except Exception:
            try:
                os.startfile(url)  # type: ignore[attr-defined]
            except Exception as exc:
                raise RuntimeError(f"Nao foi possivel abrir o navegador padrao: {exc}") from exc
        time.sleep(float(self.cfg.get("timing", {}).get("browser_photo_load", 2.8)))

        # O link foi aberto pelo processo LOCAL. Se o RDP continuar em primeiro
        # plano, tenta alternar para a janela local recem-aberta. Isso nao envia
        # nenhum clique ao hyperlink do SAP; acontece somente depois que o TXT
        # ja foi criado.
        try:
            for _ in range(4):
                active = active_window_title().lower()
                if self.remote_title.lower() not in active:
                    break
                pyautogui.hotkey("alt", "tab")
                time.sleep(0.45)
        except Exception:
            pass

    def _load_photo_from_local_browser_or_http(
        self, url: str
    ) -> tuple[Optional[Image.Image], str]:
        local_url = urlparse.quote(url, safe=":/?&=%#@+;,[]")
        download_error = ""

        try:
            req = urlrequest.Request(
                local_url,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 Chrome/120 Safari/537.36"
                    )
                },
            )
            with urlrequest.urlopen(req, timeout=30) as response:
                raw = response.read()
            photo = Image.open(BytesIO(raw))
            photo.load()
            return photo.convert("RGB"), "download_http"
        except Exception as exc:
            download_error = str(exc)

        try:
            active = active_window_title().lower()
            if self.remote_title.lower() not in active:
                pyautogui.hotkey("alt", "space")
                time.sleep(0.15)
                pyautogui.press("x")
                time.sleep(0.6)

            screen = pyautogui.screenshot()
            candidate, _, cropped = extract_largest_photo_from_screen(
                screen,
                [0.00, 0.05, 1.00, 0.90],
                min_area_ratio=0.012,
            )
            if cropped and candidate.width >= 160 and candidate.height >= 160:
                return candidate.convert("RGB"), "captura_navegador"
        except Exception as exc:
            download_error = f"{download_error}; captura: {exc}"

        return None, download_error

    def _process_url_from_notepad(
        self,
        obra: str,
        index: int,
        total: int,
        url: str,
        obra_dir: Path,
    ) -> dict:
        self._open_url_from_txt_in_default_browser(obra, index, total, url)

        photo, source = self._load_photo_from_local_browser_or_http(url)
        is_facade = self._is_facade_url(url)
        filename = self._safe_photo_filename(url, index)
        out_file = self._unique_photo_path(obra_dir, filename)

        result = {
            "latitude": None,
            "longitude": None,
            "ocr_text": "",
            "variant": "",
        }

        if photo is None:
            return {
                "path": None,
                "url": url,
                "filename": filename,
                "is_facade": is_facade,
                "source": source,
                **result,
            }

        photo.save(out_file, quality=95)

        # Somente a FACHADA DO IMOVEL passa pelo OCR de coordenadas.
        if is_facade:
            result = extract_coordinates_from_photo(
                photo,
                lang=self.lang,
                prefer_negative_lat=bool(
                    self.cfg.get("ocr", {}).get("prefer_negative_latitude", True)
                ),
            )
            photo.save(obra_dir / f"{obra}_FACHADADOIMOVEL.jpg", quality=95)
            photo.save(obra_dir / f"{obra}.jpg", quality=95)

        if self.cfg.get("ocr", {}).get("save_debug_images", True):
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            try:
                photo.save(self.debug_root / f"{obra}_{index:03d}_{stamp}.jpg", quality=88)
            except Exception:
                pass

        try:
            active = active_window_title().lower()
            if active and self.remote_title.lower() not in active:
                pyautogui.hotkey("ctrl", "w")
                time.sleep(0.30)
        except Exception:
            pass

        return {
            "path": out_file,
            "url": url,
            "filename": filename,
            "is_facade": is_facade,
            "source": source,
            **result,
        }

    def _error_screenshot(self, obra: str, stage: str):
        try:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            image, _ = screenshot_window_contains(self.remote_title, content_only=False)
            image.save(self.logs_root / f"ERRO_{obra}_{stage}_{stamp}.png")
        except Exception:
            # Não deixa uma falha no screenshot esconder o erro original.
            pass

    def process_work(self, obra: str, work_index: int = 0, total_works: int = 1) -> dict:
        """Processa uma unica obra.

        Para uma obra isolada, coleta os links no SAP, volta imediatamente para
        a tela inicial deixando o campo Nota vazio e, em seguida, processa as
        fotos localmente.
        """
        obra = "".join(ch for ch in str(obra).strip() if ch.isdigit())
        if not obra:
            raise ValueError("Numero de obra vazio ou invalido.")

        obra_dir = self.output_root / obra
        obra_dir.mkdir(parents=True, exist_ok=True)

        try:
            self.emit(f"Obra {obra}: iniciando.", progress=0.02)
            self._prepare_remote_for_automation()
            self._enter_note(obra)
            self.emit(f"Obra {obra}: nota aberta.", progress=0.10)

            self._open_images_tab()
            self.emit(f"Obra {obra}: Imagens de Campo aberta.", progress=0.18)

            all_urls = self._collect_all_urls_from_remote(obra, obra_dir)
            self.emit(
                f"Obra {obra}: coleta concluida com {len(all_urls)} link(s).",
                level="success",
                progress=0.28,
            )

            txt_path = self._create_links_notepad(obra, obra_dir, all_urls)

            # NOVO: volta ao SAP inicial IMEDIATAMENTE apos copiar os links.
            self._return_to_initial_and_load_next(obra, None)

            return self._process_local_links_for_obra(
                obra,
                obra_dir,
                txt_path,
                all_urls,
                progress_start=0.34,
                progress_end=1.0,
            )

        except pyautogui.FailSafeException as exc:
            self._error_screenshot(obra, "FAILSAFE")
            self.emit(
                f"Obra {obra}: execucao interrompida pelo FAILSAFE "
                "(mouse no canto superior esquerdo).",
                level="error",
            )
            raise RuntimeError("Automacao interrompida pelo usuario (FAILSAFE).") from exc
        except Exception:
            self._error_screenshot(obra, "PROCESSAMENTO")
            raise

    def process_many(self, obras: Iterable[str]) -> list[dict]:
        """Processa varias obras em DUAS FASES.

        FASE 1 - SAP / Area Remota:
            obra 1 -> copia links -> ESC -> limpa Nota -> digita obra 2 -> ENTER
            obra 2 -> copia links -> ESC -> limpa Nota -> digita obra 3 -> ENTER
            ...
            ultima obra -> copia links -> ESC -> limpa Nota

        FASE 2 - COMPUTADOR LOCAL:
            le cada *_links.txt, abre as URLs no navegador padrao, salva as
            fotos e extrai a coordenada apenas da FACHADA DO IMOVEL.

        Assim o robo nao fica alternando entre RDP e navegador local a cada
        obra e atende exatamente ao fluxo solicitado.
        """
        normalized: list[str] = []
        for item in obras:
            digits = "".join(ch for ch in str(item).strip() if ch.isdigit())
            if digits:
                normalized.append(digits)

        if not normalized:
            return []

        total_works = len(normalized)
        remote_jobs: list[dict] = []
        results_by_obra: dict[str, dict] = {}

        # ------------------------------------------------------------
        # FASE 1: COLETA DE LINKS DE TODAS AS OBRAS NO SAP
        # ------------------------------------------------------------
        self._prepare_remote_for_automation()

        for idx, obra in enumerate(normalized):
            obra_dir = self.output_root / obra
            obra_dir.mkdir(parents=True, exist_ok=True)
            next_obra = normalized[idx + 1] if idx + 1 < total_works else None

            try:
                self.emit(
                    f"Obra {obra}: iniciando coleta SAP ({idx + 1}/{total_works}).",
                    progress=0.02 + (0.28 * idx / max(1, total_works)),
                )

                # A primeira obra ainda precisa ser digitada. As seguintes ja
                # foram digitadas + ENTER pela iteracao anterior.
                if idx == 0:
                    self._enter_note(obra)
                else:
                    self.emit(
                        f"Obra {obra}: nota ja carregada pela troca automatica da obra anterior."
                    )

                self.emit(f"Obra {obra}: nota aberta.")
                self._open_images_tab()
                self.emit(f"Obra {obra}: Imagens de Campo aberta.")

                all_urls = self._collect_all_urls_from_remote(obra, obra_dir)
                txt_path = self._create_links_notepad(obra, obra_dir, all_urls)

                remote_jobs.append({
                    "obra": obra,
                    "obra_dir": obra_dir,
                    "urls": all_urls,
                    "txt_path": txt_path,
                })

                self.emit(
                    f"Obra {obra}: {len(all_urls)} link(s) copiado(s).",
                    level="success",
                )

                # NOVO FLUXO PEDIDO PELO USUARIO:
                # imediatamente apos copiar os links, ESC -> tela inicial ->
                # apaga obra atual -> digita a proxima -> ENTER.
                self._return_to_initial_and_load_next(
                    current_obra=obra,
                    next_obra=next_obra,
                )

            except pyautogui.FailSafeException as exc:
                self._error_screenshot(obra, "FAILSAFE")
                results_by_obra[obra] = {
                    "obra": obra,
                    "ok": False,
                    "error": "Automacao interrompida pelo usuario (FAILSAFE).",
                    "records": [],
                }
                self.emit(
                    f"Obra {obra}: execucao interrompida pelo FAILSAFE.",
                    level="error",
                )
                break
            except Exception as exc:
                self._error_screenshot(obra, "COLETA_SAP")
                self.emit(f"Obra {obra}: ERRO na coleta SAP - {exc}", level="error")
                results_by_obra[obra] = {
                    "obra": obra,
                    "ok": False,
                    "error": str(exc),
                    "records": [],
                }

                # Tenta voltar para a tela inicial e carregar a proxima obra
                # mesmo quando a coleta atual falhar, para nao travar a lista.
                try:
                    self._return_to_initial_and_load_next(obra, next_obra)
                except Exception as nav_exc:
                    self.emit(
                        f"Obra {obra}: nao foi possivel preparar a proxima obra: {nav_exc}",
                        level="error",
                    )
                    break

        # ------------------------------------------------------------
        # FASE 2: PROCESSAMENTO LOCAL DOS TXTs JA COLETADOS
        # ------------------------------------------------------------
        if remote_jobs:
            self.emit(
                "Coleta no SAP concluida. Iniciando processamento das fotos no navegador local...",
                level="success",
                progress=0.34,
            )

        for local_idx, job in enumerate(remote_jobs):
            obra = job["obra"]
            try:
                start = 0.34 + (0.64 * local_idx / max(1, len(remote_jobs)))
                end = 0.34 + (0.64 * (local_idx + 1) / max(1, len(remote_jobs)))
                result = self._process_local_links_for_obra(
                    obra=obra,
                    obra_dir=job["obra_dir"],
                    txt_path=job["txt_path"],
                    all_urls=job["urls"],
                    progress_start=start,
                    progress_end=min(0.99, end),
                )
                results_by_obra[obra] = result
            except Exception as exc:
                self.emit(f"Obra {obra}: ERRO no processamento local - {exc}", level="error")
                results_by_obra[obra] = {
                    "obra": obra,
                    "ok": False,
                    "error": str(exc),
                    "records": [],
                }

        # Mantem exatamente a mesma ordem digitada/colada na ferramenta.
        return [
            results_by_obra.get(obra, {
                "obra": obra,
                "ok": False,
                "error": "Obra nao processada.",
                "records": [],
            })
            for obra in normalized
        ]
