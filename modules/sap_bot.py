from __future__ import annotations

import ctypes
import json
import re
import shutil
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


def _extract_jpg_url(text: str) -> str:
    """Extrai um endereco HTTP/HTTPS terminado em .jpg do texto copiado."""
    raw = str(text or "").replace("\r", " ").replace("\n", " ").strip()
    if not raw:
        return ""
    match = re.search(r"https?://.*?\.jpg", raw, flags=re.IGNORECASE)
    if not match:
        return ""
    return match.group(0).strip().strip('"').strip("'")


# Fotos obrigatorias deste fluxo. Nao dependem mais do config.json.
FIXED_TARGETS = [
    {
        "key": "FACHADADOIMOVEL",
        "aliases": [
            "FACHADADOIMOVEL",
            "FACHADAIMOVEL",
            "FACHADADOIMOVEL.JPG",
            "FACHADAIMOVEL.JPG",
        ],
        "output_suffix": "FACHADADOIMOVEL",
    },
    {
        "key": "ADESIVOLIGACAONOVA",
        "aliases": [
            "ADESIVOLIGACAONOVA",
            "ADESIVOLIGACAONOVA.JPG",
        ],
        "output_suffix": "ADESIVOLIGACAONOVA",
    },
    {
        "key": "FOTOPANORAMICA",
        "aliases": [
            "FOTOPANORAMICA",
            "FOTOPANORAMICA.JPG",
            "FOTO PANORAMICA",
        ],
        "output_suffix": "FOTOPANORAMICA",
    },
]


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

        return _extract_jpg_url(copied)

    def _collect_target_urls_from_remote(self, obra: str) -> dict[str, str]:
        """Le todas as linhas visiveis da grade e recolhe somente os 3 JPGs."""
        self._activate_remote()
        time.sleep(0.35)

        row_centers, debug_img, region_abs = detect_link_row_centers(
            window_title=self.remote_title,
            # Area real da tabela Links nas telas 1920x1080 enviadas.
            region_norm=(0.012, 0.285, 0.615, 0.245),
        )
        self._save_ocr_debug(obra, "LINKS", debug_img, "linhas_detectadas")

        # Se a deteccao visual falhar, usa a geometria regular da grade SAP.
        if len(row_centers) < 3:
            left, top, width, height = region_abs
            first_y = top + int(height * 0.10)
            spacing = max(19, int(height * 0.087))
            row_centers = [first_y + i * spacing for i in range(12)]
            self.emit(
                f"Obra {obra}: deteccao automatica das linhas foi insuficiente; "
                "usando varredura geometrica da grade.",
                level="warning",
            )

        target_by_alias: list[tuple[str, str]] = []
        for target in FIXED_TARGETS:
            key = target["key"]
            for alias in target.get("aliases", []):
                clean = str(alias).upper().replace(".JPG", "").replace(" ", "")
                target_by_alias.append((key, clean))

        found: dict[str, str] = {}
        seen_urls: set[str] = set()

        for index, y in enumerate(sorted(row_centers), start=1):
            if len(found) == len(FIXED_TARGETS):
                break

            url = self._copy_url_from_link_row(y, region_abs)
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)

            normalized = url.upper().replace("%20", " ").replace(" ", "")
            matched_key = None
            for key, alias in target_by_alias:
                if alias and alias in normalized:
                    matched_key = key
                    break

            if matched_key and matched_key not in found:
                found[matched_key] = url
                self.emit(
                    f"Obra {obra}: link {matched_key} copiado da Area Remota "
                    f"(linha {index}).",
                    level="success",
                )

        if not found:
            raise RuntimeError(
                "Nenhum link JPG conseguiu ser copiado da grade. Verifique se o "
                "redirecionamento da Area de Transferencia/Clipboard esta habilitado "
                "na conexao RDP. O robo usa Ctrl+Y + Ctrl+C no SAP e precisa que o "
                "texto copiado chegue ao PC local."
            )

        return found

    def _download_photo_local(
        self,
        obra: str,
        target: Dict,
        url: str,
        obra_dir: Path,
    ) -> dict:
        """Abre o link no navegador LOCAL, baixa a imagem e executa OCR local."""
        key = str(target.get("key", "FOTO"))
        suffix = str(target.get("output_suffix") or key)

        # URLs exibidas no SAP podem conter espacos literais.
        local_url = urlparse.quote(url, safe=":/?&=%#@+;,[]")

        self.emit(f"Obra {obra}: abrindo {key} no navegador local...")
        try:
            webbrowser.open(local_url, new=2, autoraise=True)
        except Exception:
            # Abrir visualmente e util, mas nao deve impedir o download automatico.
            pass

        req = urlrequest.Request(
            local_url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 Chrome/120 Safari/537.36"
                )
            },
        )

        raw = None
        download_error = None
        try:
            with urlrequest.urlopen(req, timeout=30) as response:
                raw = response.read()
        except Exception as exc:
            download_error = exc

        photo: Optional[Image.Image] = None
        if raw:
            try:
                photo = Image.open(BytesIO(raw))
                photo.load()
                photo = photo.convert("RGB")
            except Exception as exc:
                download_error = exc
                photo = None

        # Fallback: se o browser local conseguiu abrir, mas o HTTP do Python nao
        # (por exemplo, autenticacao/certificado corporativo), tenta capturar a
        # foto diretamente da janela local atualmente ativa.
        if photo is None:
            time.sleep(2.5)
            active = active_window_title().lower()
            if self.remote_title.lower() not in active:
                local_screen = pyautogui.screenshot()
                candidate, _, cropped = extract_largest_photo_from_screen(
                    local_screen,
                    [0.00, 0.05, 1.00, 0.90],
                    min_area_ratio=0.015,
                )
                if cropped and candidate.width >= 180 and candidate.height >= 180:
                    photo = candidate.convert("RGB")

        if photo is None:
            raise RuntimeError(
                f"O link {key} foi copiado corretamente, mas o PC local nao "
                f"conseguiu carregar a imagem. URL: {url}. Erro: {download_error}. "
                "Abra esse endereco manualmente no navegador local. Se ele nao abrir, "
                "o dominio .corp provavelmente so e acessivel dentro do ambiente remoto "
                "ou exige autenticacao/VPN local."
            )

        out_file = obra_dir / f"{obra}_{suffix}.jpg"
        photo.save(out_file, quality=95)
        if "FACHADA" in key.upper():
            photo.save(obra_dir / f"{obra}.jpg", quality=95)

        result = extract_coordinates_from_photo(
            photo,
            lang=self.lang,
            prefer_negative_lat=bool(
                self.cfg.get("ocr", {}).get("prefer_negative_latitude", True)
            ),
        )

        if self.cfg.get("ocr", {}).get("save_debug_images", True):
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            photo.save(
                self.debug_root / f"{obra}_{suffix}_local_{stamp}.jpg",
                quality=90,
            )
            meta = {
                "url_copiada": url,
                "url_local": local_url,
                "latitude": result.get("latitude"),
                "longitude": result.get("longitude"),
                "variant": result.get("variant"),
                "ocr_text": result.get("ocr_text", ""),
            }
            (self.debug_root / f"{obra}_{suffix}_local_{stamp}.json").write_text(
                json.dumps(meta, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

        # Fecha a aba local somente quando ela esta efetivamente em primeiro plano.
        try:
            active = active_window_title().lower()
            if active and self.remote_title.lower() not in active:
                pyautogui.hotkey("ctrl", "w")
                time.sleep(0.35)
        except Exception:
            pass

        self._activate_remote()

        return {
            "path": out_file,
            "url": url,
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
            raise ValueError("Número de obra vazio ou inválido.")

        obra_dir = self.output_root / obra
        obra_dir.mkdir(parents=True, exist_ok=True)
        records: list[dict] = []
        targets = [dict(item) for item in FIXED_TARGETS]
        total_steps = max(1, total_works * (3 + len(targets) * 4))
        base_step = work_index * (3 + len(targets) * 4)

        try:
            self.emit(f"Obra {obra}: iniciando.", progress=base_step / total_steps)
            self._prepare_remote_for_automation()
            self._enter_note(obra)
            self.emit(f"Obra {obra}: nota aberta.", progress=(base_step + 1) / total_steps)
            self._open_images_tab()
            self.emit(f"Obra {obra}: Imagens de Campo aberta.", progress=(base_step + 2) / total_steps)

            # Primeiro copia os links da Area Remota para o clipboard local.
            # A partir daqui as fotos sao abertas/processadas no proprio PC.
            copied_urls = self._collect_target_urls_from_remote(obra)

            for ti, target in enumerate(targets):
                tkey = target.get("key", f"FOTO_{ti + 1}")
                step0 = base_step + 3 + ti * 4
                url = copied_urls.get(tkey, "")

                if not url:
                    records.append({
                        "OBRA": obra,
                        "TIPO_FOTO": tkey,
                        "LINK_IDENTIFICADO_OCR": "",
                        "LATITUDE": "",
                        "LONGITUDE": "",
                        "ARQUIVO_FOTO": "",
                        "STATUS_LINK": "NAO COPIADO",
                        "STATUS_COORDENADA": "NAO PROCESSADA",
                        "OCR_RODAPE": "",
                        "DATA_HORA": datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
                    })
                    self.emit(
                        f"Obra {obra}: link {tkey} nao foi encontrado entre as URLs copiadas.",
                        level="warning",
                        progress=(step0 + 1) / total_steps,
                    )
                    continue

                self.emit(
                    f"Obra {obra}: processando imagem {ti + 1}/{len(targets)} no PC local...",
                    progress=step0 / total_steps,
                )

                try:
                    photo_result = self._download_photo_local(
                        obra,
                        target,
                        url,
                        obra_dir,
                    )
                    lat = photo_result.get("latitude")
                    lon = photo_result.get("longitude")
                    coord_status = (
                        "OK"
                        if lat is not None and lon is not None
                        else "COORDENADA NAO RECONHECIDA"
                    )
                    records.append({
                        "OBRA": obra,
                        "TIPO_FOTO": tkey,
                        "LINK_IDENTIFICADO_OCR": url,
                        "LATITUDE": lat if lat is not None else "",
                        "LONGITUDE": lon if lon is not None else "",
                        "ARQUIVO_FOTO": str(photo_result["path"]),
                        "STATUS_LINK": "COPIADO/LOCAL",
                        "STATUS_COORDENADA": coord_status,
                        "OCR_RODAPE": photo_result.get("ocr_text", ""),
                        "DATA_HORA": datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
                    })
                    if coord_status == "OK":
                        self.emit(
                            f"Obra {obra}: {tkey} salvo; coordenada {lat:.6f}, {lon:.6f}.",
                            level="success",
                        )
                    else:
                        self.emit(
                            f"Obra {obra}: {tkey} salvo, mas a coordenada nao foi reconhecida.",
                            level="warning",
                        )
                except Exception as exc:
                    records.append({
                        "OBRA": obra,
                        "TIPO_FOTO": tkey,
                        "LINK_IDENTIFICADO_OCR": url,
                        "LATITUDE": "",
                        "LONGITUDE": "",
                        "ARQUIVO_FOTO": "",
                        "STATUS_LINK": "ERRO LOCAL",
                        "STATUS_COORDENADA": "NAO PROCESSADA",
                        "OCR_RODAPE": "",
                        "DATA_HORA": datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
                    })
                    self.emit(
                        f"Obra {obra}: falha ao processar {tkey} no PC local: {exc}",
                        level="error",
                    )
                    if not self.cfg.get("automation", {}).get("continue_when_photo_missing", True):
                        raise

            excel_path = save_work_excel(records, obra_dir / f"{obra}_coordenadas.xlsx")
            if self.cfg.get("output", {}).get("create_consolidated_excel", True):
                update_consolidated(records, self.output_root / "resumo_geral.xlsx")

            zip_path = None
            if self.cfg.get("output", {}).get("create_zip", True):
                zip_base = obra_dir.parent / f"{obra}"
                zip_result = shutil.make_archive(str(zip_base), "zip", root_dir=obra_dir)
                zip_path = Path(zip_result)

            self.emit(
                f"Obra {obra}: processamento concluído.",
                level="success",
                progress=min(1.0, (base_step + 3 + len(targets) * 4) / total_steps),
            )
            return {
                "obra": obra,
                "folder": obra_dir,
                "excel": excel_path,
                "zip": zip_path,
                "records": records,
                "ok": True,
            }

        except pyautogui.FailSafeException as exc:
            self._error_screenshot(obra, "FAILSAFE")
            self.emit(
                f"Obra {obra}: execução interrompida pelo FAILSAFE (mouse no canto superior esquerdo).",
                level="error",
            )
            raise RuntimeError("Automação interrompida pelo usuário (FAILSAFE).") from exc
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
