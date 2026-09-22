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
    # COLETA DE LINKS POR PRINT TEMPORARIO + OCR
    # ------------------------------------------------------------------
    # Esta versao NAO depende do clipboard do RDP. A grade Links e capturada
    # como imagem, ampliada e lida pelo Tesseract. Os prints sao temporarios e
    # apagados depois que o TXT da obra e montado com sucesso.

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

    _PHOTO_BASE_URL = (
        "https://eapspddope01.equatorial.corp/ma/barramento/EQTL_MA"
    )

    def _normalize_ocr_photo_label(self, raw: str) -> str:
        """Normaliza/corrige o nome final do JPG reconhecido pelo OCR."""
        value = re.sub(r"[^A-Z0-9]", "", str(raw or "").upper())
        if not value:
            return ""

        # Tesseract costuma confundir I/V/T/L em palavras longas. Como os
        # nomes mais comuns das fotos sao padronizados no SAP, fazemos uma
        # correcao fuzzy apenas quando a semelhanca e alta.
        best = value
        best_score = 0.0
        for candidate in self._KNOWN_PHOTO_LABELS:
            score = SequenceMatcher(None, value, candidate).ratio()
            if score > best_score:
                best = candidate
                best_score = score

        if best_score >= 0.78:
            return best
        return value

    def _ocr_grid_text_variants(self, image: Image.Image) -> list[str]:
        """Executa OCR da grade Links com mais de um tratamento visual."""
        # Ampliacao e importante: na tela SAP a fonte do link e pequena.
        scale = 3
        enlarged = image.resize(
            (image.width * scale, image.height * scale),
            Image.Resampling.LANCZOS,
        )
        gray = ImageOps.grayscale(enlarged)
        contrast = ImageEnhance.Contrast(gray).enhance(2.0)
        sharp = contrast.filter(ImageFilter.SHARPEN)

        variants = [gray, contrast, sharp]
        texts: list[str] = []
        for variant in variants:
            for psm in (6, 4):
                try:
                    text = pytesseract.image_to_string(
                        variant,
                        lang=self.lang,
                        config=f"--psm {psm}",
                    )
                except Exception:
                    text = pytesseract.image_to_string(
                        variant,
                        lang="eng",
                        config=f"--psm {psm}",
                    )
                if text and text.strip():
                    texts.append(text)
        return texts

    def _pick_common_month_and_date(self, texts: list[str]) -> tuple[str, str]:
        """Descobre pasta AAAAMM e data AAAAMMDD pela maioria dos OCRs."""
        months: list[str] = []
        dates: list[str] = []
        for text in texts:
            # Mantem os espacos entre campos numericos. Remover esses espacos
            # faria 20260717 + numero da obra virarem um numero unico.
            months.extend(re.findall(r"(?<!\d)(20\d{4})(?!\d)", text))
            dates.extend(re.findall(r"(?<!\d)(20\d{6})(?!\d)", text))

        month = Counter(months).most_common(1)[0][0] if months else ""
        date = Counter(dates).most_common(1)[0][0] if dates else ""

        if not month and date:
            month = date[:6]
        return month, date

    def _parse_ocr_link_rows(self, texts: list[str], obra: str) -> list[str]:
        """Reconstrui URLs exatas a partir das linhas OCR da grade SAP.

        O Tesseract geralmente converte '_' para espaco. Em vez de confiar na
        URL inteira reconhecida, usamos o formato padrao do repositorio de
        fotos e reconstruimos cada link com os campos visiveis da linha.
        """
        month, photo_date = self._pick_common_month_and_date(texts)
        if not month or not photo_date:
            return []

        found: dict[tuple[int, str], str] = {}

        for text in texts:
            for raw_line in text.splitlines():
                line = str(raw_line or "").strip()
                if not line:
                    continue

                upper = line.upper()
                # Ignora textos que claramente nao sao linhas de link.
                if (
                    "EQUATORIAL" not in upper
                    and "EQ FOTO" not in upper
                    and "EQ_FOTO" not in upper
                    and " FOTO " not in f" {upper} "
                ):
                    continue

                # Normaliza algumas leituras frequentes de FOTO.
                work = upper.replace("F0T0", "FOTO").replace("F0TO", "FOTO")
                work = work.replace("FOT0", "FOTO")

                # Procura o numero apos FOTO. O prefixo EQ pode ou nao ter sido
                # reconhecido pelo OCR, entao o regex e propositalmente amplo.
                matches = list(re.finditer(r"FOTO\s*[_\- ]*\s*(\d{1,3})\s+", work))
                if not matches:
                    continue
                m = matches[-1]
                photo_number = int(m.group(1))

                tail = work[m.end():]
                # Termina no .JPG quando OCR o reconheceu; senao usa a cauda.
                tail = re.split(r"\.\s*JPG|\bJPG\b", tail, maxsplit=1)[0]
                label = self._normalize_ocr_photo_label(tail)
                if not label:
                    continue

                url = (
                    f"{self._PHOTO_BASE_URL}/{month}/"
                    f"OFS_PHOTO_{photo_date}_{obra}_EQ_FOTO_{photo_number}_{label}.jpg"
                )
                found[(photo_number, label)] = url

        # Ordena pela ordem visual mais comum (numero da foto) apenas para
        # manter o TXT previsivel. Todas as URLs unicas sao preservadas.
        return [found[key] for key in sorted(found, key=lambda x: (x[0], x[1]))]

    def _capture_links_grid_temp(
        self,
        obra: str,
        page: int,
        temp_dir: Path,
        region_norm: tuple[float, float, float, float],
    ) -> tuple[Image.Image, tuple[int, int, int, int]]:
        """Tira um print TEMPORARIO somente da grade Links."""
        full, remote_abs = screenshot_window_contains(
            self.remote_title,
            content_only=False,
        )
        rw, rh = full.size
        rx, ry, rwidth, rheight = region_norm
        x = max(0, int(round(rx * rw)))
        y = max(0, int(round(ry * rh)))
        w = max(1, int(round(rwidth * rw)))
        h = max(1, int(round(rheight * rh)))
        x2 = min(rw, x + w)
        y2 = min(rh, y + h)
        crop = full.crop((x, y, x2, y2))

        temp_dir.mkdir(parents=True, exist_ok=True)
        temp_file = temp_dir / f"pagina_{page:02d}.png"
        crop.save(temp_file)

        abs_region = (
            remote_abs[0] + x,
            remote_abs[1] + y,
            crop.width,
            crop.height,
        )
        return crop, abs_region

    def _collect_all_urls_from_remote(self, obra: str, obra_dir: Path) -> list[str]:
        """Extrai TODOS os links usando prints temporarios da grade + OCR.

        Fluxo:
            1. ativa o RDP e posiciona a grade no topo;
            2. tira print temporario da grade Links;
            3. OCR + reconstrucao das URLs;
            4. rola a grade e repete ate nao aparecerem links novos;
            5. apaga os prints temporarios depois do sucesso.

        Nao usa Ctrl+C, Ctrl+Y nem clipboard do RDP.
        """
        self._activate_remote()
        time.sleep(0.50)

        # Regiao relativa a janela RDP. Abrange o quadro Links completo sem
        # pegar as abas superiores. Foi ajustada para a tela SAP 1920x1080,
        # mas continua proporcional caso o RDP tenha outro tamanho.
        region_norm = (0.015, 0.300, 0.625, 0.310)
        temp_dir = obra_dir / "_temp_links_ocr"

        # Leva a lista para o topo antes da primeira captura.
        left, top, width, height = get_window_region_contains(
            self.remote_title,
            content_only=False,
        )
        grid_x = left + int(width * (region_norm[0] + region_norm[2] * 0.50))
        grid_y = top + int(height * (region_norm[1] + region_norm[3] * 0.50))
        pyautogui.moveTo(grid_x, grid_y, duration=0.15)
        for _ in range(8):
            pyautogui.scroll(10)
            time.sleep(0.06)
        time.sleep(0.45)

        urls: list[str] = []
        seen: set[str] = set()
        max_pages = int(self.cfg.get("automation", {}).get("max_link_pages", 15))
        pages_without_new = 0

        try:
            for page in range(1, max_pages + 1):
                image, abs_region = self._capture_links_grid_temp(
                    obra,
                    page,
                    temp_dir,
                    region_norm,
                )

                texts = self._ocr_grid_text_variants(image)
                page_urls = self._parse_ocr_link_rows(texts, obra)

                before = len(urls)
                for url in page_urls:
                    if url in seen:
                        continue
                    seen.add(url)
                    urls.append(url)
                    self.emit(
                        f"Obra {obra}: link {len(urls)} identificado pelo print temporario.",
                        level="success",
                    )

                if len(urls) == before:
                    pages_without_new += 1
                else:
                    pages_without_new = 0

                self.emit(
                    f"Obra {obra}: pagina {page} da grade analisada; "
                    f"{len(page_urls)} link(s) reconhecido(s) nesta pagina."
                )

                # Duas paginas consecutivas sem novidade indicam fim da lista.
                if pages_without_new >= 2:
                    break

                # Rola somente dentro da propria grade Links.
                ax, ay, aw, ah = abs_region
                pyautogui.moveTo(ax + aw // 2, ay + ah // 2, duration=0.10)
                pyautogui.scroll(-7)
                time.sleep(0.65)

            if not urls:
                raise RuntimeError(
                    "Nenhum link foi reconhecido nos prints temporarios da grade Links. "
                    "Os prints foram mantidos na pasta _temp_links_ocr para diagnostico."
                )

            # O usuario pediu prints apenas temporarios. Mantemos ate concluir a
            # leitura e, em caso de sucesso, removemos a pasta inteira.
            try:
                shutil.rmtree(temp_dir, ignore_errors=True)
            except Exception:
                pass

            return urls

        except Exception:
            # Em erro, NAO apaga os prints: eles ajudam a diagnosticar o OCR.
            raise

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
        obra = "".join(ch for ch in str(obra).strip() if ch.isdigit())
        if not obra:
            raise ValueError("Numero de obra vazio ou invalido.")

        obra_dir = self.output_root / obra
        obra_dir.mkdir(parents=True, exist_ok=True)
        records: list[dict] = []

        try:
            self.emit(f"Obra {obra}: iniciando.", progress=0.02)
            self._prepare_remote_for_automation()
            self._enter_note(obra)
            self.emit(f"Obra {obra}: nota aberta.", progress=0.10)

            self._open_images_tab()
            self.emit(f"Obra {obra}: Imagens de Campo aberta.", progress=0.18)

            # 1) Copia TODOS os links da grade SAP remota.
            all_urls = self._collect_all_urls_from_remote(obra, obra_dir)
            self.emit(
                f"Obra {obra}: coleta concluida com {len(all_urls)} link(s).",
                level="success",
                progress=0.32,
            )

            # 2) Cria um bloco de notas com todos os links, um por linha.
            txt_path = self._create_links_notepad(obra, obra_dir, all_urls)

            # 3) O TXT passa a ser a fonte: abre um link por vez no navegador local.
            urls_from_txt = self._read_links_from_notepad_file(txt_path)
            total = len(urls_from_txt)
            facade_found = False
            facade_lat = None
            facade_lon = None

            for index, url in enumerate(urls_from_txt, start=1):
                progress = 0.32 + (0.58 * (index - 1) / max(1, total))
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
                f"Obra {obra}: processamento concluido. "
                f"{len(all_urls)} link(s), {len(records)} registro(s).",
                level="success",
                progress=1.0,
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
        obras = [str(x).strip() for x in obras if str(x).strip()]
        results = []
        total = len(obras)
        for idx, obra in enumerate(obras):
            try:
                results.append(self.process_work(obra, idx, total))
            except Exception as exc:
                self.emit(f"Obra {obra}: ERRO - {exc}", level="error")
                results.append({
                    "obra": obra,
                    "ok": False,
                    "error": str(exc),
                    "records": [],
                })
        return results
