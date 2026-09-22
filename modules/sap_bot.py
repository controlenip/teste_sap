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
from pathlib import Path
from typing import Callable, Dict, Iterable, Optional

import pyautogui
import pyperclip
from PIL import Image

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

    def _copy_url_from_link_row(
        self,
        y_abs: int,
        grid_region_abs: tuple[int, int, int, int],
    ) -> str:
        """Copia uma URL da grade SAP sem abrir o link.

        Usa o modo de selecao em bloco do SAP GUI (Ctrl+Y), arrasta somente a
        linha desejada e envia Ctrl+C. Com o clipboard redirecionado pelo RDP,
        o texto passa a ficar disponivel no Windows local.
        """
        self._activate_remote()
        _clear_windows_clipboard()

        left, top, width, height = grid_region_abs
        # Evita a borda/scrollbar, mas cobre praticamente a URL inteira.
        x1 = left + max(10, int(width * 0.015))
        x2 = left + width - max(25, int(width * 0.025))
        y = int(y_abs)

        # Ctrl+Y e o atalho classico do SAP GUI para selecionar texto em bloco.
        pyautogui.hotkey("ctrl", "y")
        time.sleep(0.20)
        pyautogui.moveTo(x1, y, duration=0.15)
        pyautogui.dragTo(x2, y, duration=0.70, button="left")
        time.sleep(0.15)
        pyautogui.hotkey("ctrl", "c")

        deadline = time.time() + 2.5
        copied = ""
        while time.time() < deadline:
            copied = _read_windows_clipboard_text().strip()
            if copied:
                break
            time.sleep(0.10)

        # Sai de eventual modo de selecao sem ativar o hyperlink.
        pyautogui.press("esc")
        time.sleep(0.10)

        return _extract_http_url(copied)

    def _collect_all_urls_from_remote(self, obra: str) -> list[str]:
        """Copia TODOS os links da grade Imagens de Campo.

        O robo percorre a grade por paginas/rolagem, usa Ctrl+Y + Ctrl+C em
        cada linha e acumula URLs unicas. A Area Remota e usada somente para
        esta coleta. Depois disso o processamento ocorre no computador local.
        """
        self._activate_remote()
        time.sleep(0.40)

        region_norm = (0.012, 0.285, 0.615, 0.245)
        urls: list[str] = []
        seen: set[str] = set()

        row_centers, debug_img, region_abs = detect_link_row_centers(
            window_title=self.remote_title,
            region_norm=region_norm,
        )
        self._save_ocr_debug(obra, "LINKS", debug_img, "inicio_coleta")

        left, top, width, height = region_abs
        grid_x = left + max(40, width // 2)
        grid_y = top + max(40, height // 2)

        # Comeca no topo da lista.
        pyautogui.moveTo(grid_x, grid_y, duration=0.15)
        for _ in range(6):
            pyautogui.scroll(10)
            time.sleep(0.08)
        time.sleep(0.50)

        max_pages = int(self.cfg.get("automation", {}).get("max_link_pages", 15))
        pages_without_new = 0

        for page in range(max_pages):
            row_centers, debug_img, region_abs = detect_link_row_centers(
                window_title=self.remote_title,
                region_norm=region_norm,
            )

            if page == 0 or self.cfg.get("ocr", {}).get("save_debug_images", True):
                self._save_ocr_debug(
                    obra, "LINKS", debug_img, f"pagina_{page + 1}"
                )

            left, top, width, height = region_abs

            if len(row_centers) < 3:
                first_y = top + int(height * 0.10)
                spacing = max(19, int(height * 0.087))
                row_centers = [first_y + i * spacing for i in range(12)]

            before = len(urls)

            for y in sorted(row_centers):
                url = self._copy_url_from_link_row(y, region_abs)
                if not url:
                    continue
                url = url.strip()
                if url in seen:
                    continue
                seen.add(url)
                urls.append(url)
                self.emit(
                    f"Obra {obra}: link {len(urls)} copiado da Area Remota.",
                    level="success",
                )

            if len(urls) == before:
                pages_without_new += 1
            else:
                pages_without_new = 0

            if pages_without_new >= 2:
                break

            pyautogui.moveTo(left + width // 2, top + height // 2, duration=0.10)
            pyautogui.scroll(-7)
            time.sleep(0.65)

        if not urls:
            raise RuntimeError(
                "Nenhum link conseguiu ser copiado da grade. Verifique se o "
                "redirecionamento da Area de Transferencia/Clipboard esta habilitado no RDP."
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
            all_urls = self._collect_all_urls_from_remote(obra)
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
