from __future__ import annotations

import json
import shutil
import time
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
    save_debug_image,
    wait_for_text,
)
from .window_control import (
    activate_window_contains,
    maximize_window_contains,
    norm_point_in_window_to_abs,
    get_window_region_contains,
    screenshot_window_contains,
)

ProgressCallback = Callable[[str, str, Optional[float]], None]


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
        """Coloca o RDP em tela maximizada antes de qualquer clique no SAP.

        O SAP reorganiza e redimensiona os controles quando a janela RDP fica em
        meia tela. Por isso o robo maximiza a janela local do RDP e so entao usa
        os pontos normalizados calibrados para a tela cheia.
        """
        self.emit("Preparando Área Remota em tela maximizada...")
        maximize_window_contains(self.remote_title, wait=1.2, timeout=6.0)
        self._activate_remote()

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

    def _locate_and_click_link(self, obra: str, target: Dict) -> tuple[bool, str]:
        """Localiza o nome do JPG e clica na mesma linha do hyperlink.

        O OCR e feito SOMENTE na parte direita da grade, onde aparecem os nomes
        FACHADADOIMOVEL / ADESIVOLIGACAONOVA / FOTOPANORAMICA. Depois de achar
        a linha, o clique e feito na parte esquerda da MESMA linha, sobre a URL.
        """
        aliases = target.get("aliases", [target.get("key", "")])
        pages = max(1, int(self.cfg.get("automation", {}).get("scroll_pages_links", 4)))

        self._activate_remote()

        # Faixa onde os nomes dos arquivos aparecem na grade em RDP maximizado.
        # Evita OCR no prefixo enorme da URL e melhora muito a leitura.
        filename_region = [0.20, 0.25, 0.43, 0.36]

        # Garante que a grade esteja no topo antes de procurar cada foto.
        win_left, win_top, win_w, win_h = get_window_region_contains(
            self.remote_title,
            content_only=False,
        )
        grid_x = win_left + int(win_w * 0.30)
        grid_y = win_top + int(win_h * 0.42)
        pyautogui.moveTo(grid_x, grid_y)
        pyautogui.scroll(20)
        time.sleep(0.5)

        last_img = None

        for page in range(pages):
            found, img, region_abs = locate_link_filename_on_screen(
                aliases,
                filename_region,
                lang=self.lang,
                threshold=58,
                window_title=self.remote_title,
            )
            last_img = img

            if found:
                # O hyperlink ocupa a linha inteira. Clica mais a esquerda, onde a
                # URL e certamente clicavel, mantendo exatamente o Y reconhecido.
                click_x = win_left + int(win_w * 0.10)
                click_y = found.center[1]

                pyautogui.moveTo(click_x, click_y, duration=0.20)
                pyautogui.click()

                self._save_ocr_debug(
                    obra,
                    target.get("key", "FOTO"),
                    img,
                    "link_encontrado",
                )

                self.emit(
                    f"Obra {obra}: {target.get('key', 'FOTO')} localizado na grade.",
                    level="success",
                )
                return True, found.text

            if page == 0 and last_img is not None:
                self._save_ocr_debug(
                    obra,
                    target.get("key", "FOTO"),
                    last_img,
                    "link_nao_encontrado",
                )

            # Rola somente dentro da grade para procurar linhas abaixo.
            pyautogui.moveTo(grid_x, grid_y)
            pyautogui.scroll(-6)
            time.sleep(0.65)

        return False, ""

    def _permit_if_needed(self):
        """Libera o popup Segurança SAPGUI.

        Primeiro tenta OCR do botao Permitir. Se o OCR falhar, usa um ponto
        normalizado conhecido do popup. Se a decisao ja estiver memorizada e o
        navegador tiver aberto direto, o clique de fallback cai em uma area
        inofensiva da pagina da foto.
        """
        time.sleep(max(1.0, float(self.cfg.get("timing", {}).get("after_link_click", 1.0))))

        clicked, _ = click_text(
            ["Permitir"],
            [0.20, 0.28, 0.50, 0.36],
            lang=self.lang,
            threshold=45,
            window_title=self.remote_title,
        )

        if not clicked:
            # Ponto do botao Permitir medido nos prints fornecidos.
            x, y = norm_point_in_window_to_abs(
                self.remote_title,
                (0.357, 0.548),
                content_only=False,
            )
            pyautogui.click(x, y)

        time.sleep(max(2.5, float(self.cfg.get("timing", {}).get("after_permit", 2.0))))
        return True

    def _maximize_photo(self):
        if not self.cfg.get("automation", {}).get("maximize_photo_window", True):
            return
        # O navegador da foto está dentro da sessão RDP. O último clique foi no SAP,
        # portanto o atalho é encaminhado à aplicação remota ativa.
        pyautogui.hotkey("alt", "space")
        time.sleep(0.25)
        pyautogui.press("x")
        time.sleep(float(self.cfg["timing"]["after_maximize"]))

    def _capture_photo_and_coordinates(self, obra: str, target: Dict, obra_dir: Path) -> dict:
        """Tira o print da foto, salva o JPG e extrai latitude/longitude."""
        # Aguarda a imagem terminar de carregar no navegador remoto.
        time.sleep(1.5)

        remote_screen, remote_abs_region = screenshot_window_contains(
            self.remote_title,
            content_only=False,
        )

        # Regiao ampla do navegador. Funciona tanto com navegador maximizado
        # quanto com a janela aberta sobre o SAP.
        photo, bbox_local, cropped = extract_largest_photo_from_screen(
            remote_screen,
            [0.00, 0.04, 1.00, 0.92],
            min_area_ratio=0.01,
        )

        bbox_abs = (
            remote_abs_region[0] + bbox_local[0],
            remote_abs_region[1] + bbox_local[1],
            bbox_local[2],
            bbox_local[3],
        )

        result = extract_coordinates_from_photo(
            photo,
            lang=self.lang,
            prefer_negative_lat=bool(
                self.cfg.get("ocr", {}).get("prefer_negative_latitude", True)
            ),
        )

        # Fallback de coordenada: se o recorte principal nao trouxe o carimbo,
        # tenta uma area grande da metade esquerda do navegador, onde a foto abre.
        if result.get("latitude") is None or result.get("longitude") is None:
            rw, rh = remote_screen.size
            fallback_photo = remote_screen.crop(
                (0, int(rh * 0.04), int(rw * 0.65), int(rh * 0.96))
            )
            fallback_result = extract_coordinates_from_photo(
                fallback_photo,
                lang=self.lang,
                prefer_negative_lat=bool(
                    self.cfg.get("ocr", {}).get("prefer_negative_latitude", True)
                ),
            )
            if (
                fallback_result.get("latitude") is not None
                and fallback_result.get("longitude") is not None
            ):
                result = fallback_result

        suffix = str(target.get("output_suffix") or target.get("key") or "FOTO")
        out_file = obra_dir / f"{obra}_{suffix}.jpg"

        # Este e o print da FOTO solicitado pelo usuario.
        photo_rgb = photo.convert("RGB")
        photo_rgb.save(out_file, quality=95)

        # A fachada tambem fica com o nome simples NUMERO_DA_OBRA.jpg.
        if "FACHADA" in str(target.get("key", "")).upper():
            photo_rgb.save(obra_dir / f"{obra}.jpg", quality=95)

        if self.cfg.get("ocr", {}).get("save_debug_images", True):
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            remote_screen.convert("RGB").save(
                self.debug_root / f"{obra}_{suffix}_tela_completa_{stamp}.jpg",
                quality=85,
            )
            photo_rgb.save(
                self.debug_root / f"{obra}_{suffix}_foto_detectada_{stamp}.jpg",
                quality=90,
            )
            meta = {
                "remote_window": self.remote_title,
                "remote_region_abs": remote_abs_region,
                "bbox_local_rdp": bbox_local,
                "bbox_abs_monitor": bbox_abs,
                "cropped": cropped,
                "latitude": result.get("latitude"),
                "longitude": result.get("longitude"),
                "variant": result.get("variant"),
                "ocr_text": result.get("ocr_text", ""),
            }
            (self.debug_root / f"{obra}_{suffix}_ocr_{stamp}.json").write_text(
                json.dumps(meta, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

        return {
            "path": out_file,
            "cropped": cropped,
            "bbox": bbox_abs,
            **result,
        }

    def _close_photo(self):
        if self.cfg.get("automation", {}).get("close_photo_with_alt_f4", True):
            pyautogui.hotkey("alt", "f4")
            time.sleep(float(self.cfg["timing"]["after_close_photo"]))

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

            for ti, target in enumerate(targets):
                tkey = target.get("key", f"FOTO_{ti + 1}")
                step0 = base_step + 3 + ti * 4

                # Mensagem genérica para evitar que o próprio nome do arquivo apareça
                # no painel local durante a etapa de OCR.
                self.emit(
                    f"Obra {obra}: analisando links da imagem {ti + 1}/{len(targets)}...",
                    progress=step0 / total_steps,
                )

                found, link_text = self._locate_and_click_link(obra, target)
                if not found:
                    records.append({
                        "OBRA": obra,
                        "TIPO_FOTO": tkey,
                        "LINK_IDENTIFICADO_OCR": "",
                        "LATITUDE": "",
                        "LONGITUDE": "",
                        "ARQUIVO_FOTO": "",
                        "STATUS_LINK": "NÃO ENCONTRADO",
                        "STATUS_COORDENADA": "NÃO PROCESSADA",
                        "OCR_RODAPE": "",
                        "DATA_HORA": datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
                    })
                    self.emit(
                        f"Obra {obra}: imagem {ti + 1} não localizada no SAP.",
                        level="warning",
                        progress=(step0 + 1) / total_steps,
                    )
                    if not self.cfg.get("automation", {}).get("continue_when_photo_missing", True):
                        raise RuntimeError(f"Link {tkey} não encontrado.")
                    continue

                self.emit(
                    f"Obra {obra}: link da imagem {ti + 1} localizado; liberando abertura...",
                    progress=(step0 + 1) / total_steps,
                )
                self._permit_if_needed()
                self._maximize_photo()
                self.emit(
                    f"Obra {obra}: capturando imagem {ti + 1} e lendo coordenada...",
                    progress=(step0 + 2) / total_steps,
                )
                photo_result = self._capture_photo_and_coordinates(obra, target, obra_dir)

                lat = photo_result.get("latitude")
                lon = photo_result.get("longitude")
                coord_status = "OK" if lat is not None and lon is not None else "COORDENADA NÃO RECONHECIDA"

                records.append({
                    "OBRA": obra,
                    "TIPO_FOTO": tkey,
                    "LINK_IDENTIFICADO_OCR": link_text,
                    "LATITUDE": lat if lat is not None else "",
                    "LONGITUDE": lon if lon is not None else "",
                    "ARQUIVO_FOTO": str(photo_result["path"]),
                    "STATUS_LINK": "OK",
                    "STATUS_COORDENADA": coord_status,
                    "OCR_RODAPE": photo_result.get("ocr_text", ""),
                    "DATA_HORA": datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
                })

                if coord_status == "OK":
                    self.emit(
                        f"Obra {obra}: coordenada da imagem {ti + 1} → {lat:.6f}, {lon:.6f}",
                        level="success",
                    )
                else:
                    self.emit(
                        f"Obra {obra}: imagem {ti + 1} salva, mas a coordenada não foi reconhecida.",
                        level="warning",
                    )
                    if not self.cfg.get("automation", {}).get("continue_when_coordinate_missing", True):
                        raise RuntimeError(f"Coordenada de {tkey} não reconhecida.")

                self._close_photo()
                self._activate_remote()
                self.emit(
                    f"Obra {obra}: imagem {ti + 1} concluída.",
                    progress=(step0 + 4) / total_steps,
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
