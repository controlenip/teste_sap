from __future__ import annotations

import os
import re
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Sequence, Tuple

import cv2
import numpy as np
import pandas as pd
import pytesseract
from PIL import Image, ImageEnhance, ImageFilter, ImageOps
from rapidfuzz import fuzz

from .config import norm_region_to_abs
from .window_control import norm_region_in_window_to_abs, screenshot_window_contains


@dataclass
class OCRLine:
    text: str
    left: int
    top: int
    width: int
    height: int
    score: float = 0.0

    @property
    def center(self) -> Tuple[int, int]:
        return self.left + self.width // 2, self.top + self.height // 2


def normalize_text(value: str) -> str:
    s = unicodedata.normalize("NFKD", str(value or ""))
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.upper()
    return re.sub(r"[^A-Z0-9]+", "", s)


def configure_tesseract(cmd: str) -> None:
    if cmd:
        pytesseract.pytesseract.tesseract_cmd = cmd
        tessdata = Path(cmd).resolve().parent / "tessdata"
        if tessdata.exists():
            os.environ["TESSDATA_PREFIX"] = str(tessdata)


def screenshot_full(window_title: str = "") -> Image.Image:
    """Captura a tela inteira ou, quando informado, somente a janela remota."""
    import pyautogui
    if str(window_title or "").strip():
        image, _ = screenshot_window_contains(str(window_title), content_only=False)
        return image
    return pyautogui.screenshot()


def screenshot_norm_region(
    region: Iterable[float],
    *,
    window_title: str = "",
) -> tuple[Image.Image, tuple[int, int, int, int]]:
    """Captura uma região normalizada.

    Se ``window_title`` for informado, as coordenadas normalizadas são relativas
    somente à janela RDP, nunca ao monitor inteiro.
    """
    import pyautogui
    if str(window_title or "").strip():
        abs_region = norm_region_in_window_to_abs(
            str(window_title),
            region,
            content_only=False,
        )
    else:
        sw, sh = pyautogui.size()
        abs_region = norm_region_to_abs(region, (sw, sh))
    return pyautogui.screenshot(region=abs_region), abs_region


def _image_to_data(image: Image.Image, lang: str = "eng", psm: int = 6) -> pd.DataFrame:
    try:
        df = pytesseract.image_to_data(
            image,
            lang=lang,
            config=f"--psm {psm}",
            output_type=pytesseract.Output.DATAFRAME,
        )
    except pytesseract.TesseractError:
        # Fallback para instalação sem pacote de idioma selecionado.
        df = pytesseract.image_to_data(
            image,
            lang="eng",
            config=f"--psm {psm}",
            output_type=pytesseract.Output.DATAFRAME,
        )
    if df is None or df.empty:
        return pd.DataFrame()
    return df.dropna(subset=["text"]).copy()


def ocr_lines(image: Image.Image, lang: str = "eng", psm: int = 6) -> list[OCRLine]:
    df = _image_to_data(image, lang=lang, psm=psm)
    if df.empty:
        return []
    lines: list[OCRLine] = []
    keys = ["block_num", "par_num", "line_num"]
    for _, grp in df.groupby(keys, sort=False):
        words = [str(t).strip() for t in grp["text"].tolist() if str(t).strip()]
        if not words:
            continue
        left = int(grp["left"].min())
        top = int(grp["top"].min())
        right = int((grp["left"] + grp["width"]).max())
        bottom = int((grp["top"] + grp["height"]).max())
        lines.append(OCRLine(" ".join(words), left, top, right - left, bottom - top))
    return lines


def match_line(lines: Sequence[OCRLine], targets: Sequence[str], threshold: float = 70) -> Optional[OCRLine]:
    best: Optional[OCRLine] = None
    best_score = -1.0
    norm_targets = [(t, normalize_text(t)) for t in targets if str(t).strip()]
    for line in lines:
        ln = normalize_text(line.text)
        if not ln:
            continue
        for raw, tn in norm_targets:
            if not tn:
                continue
            # Evita falso positivo do partial_ratio em lixo OCR muito curto (ex.: "o-").
            min_line_len = max(4, min(10, int(len(tn) * 0.45)))
            if len(ln) < min_line_len:
                continue
            if tn in ln:
                score = 100.0
            else:
                score = max(
                    fuzz.partial_ratio(tn, ln),
                    fuzz.ratio(tn, ln),
                )
            if score > best_score:
                best_score = float(score)
                best = OCRLine(line.text, line.left, line.top, line.width, line.height, float(score))
    if best is not None and best_score >= threshold:
        return best
    return None


def find_text_span(image: Image.Image, targets: Sequence[str], lang: str = "eng", threshold: float = 70, psm: int = 6) -> Optional[OCRLine]:
    """Localiza a menor sequência de tokens OCR que melhor representa o texto-alvo.

    Diferente de usar o centro da linha inteira, isto permite clicar exatamente em
    abas como "Imagens de Campo" mesmo quando o OCR juntou várias abas na mesma linha.
    """
    df = _image_to_data(image, lang=lang, psm=psm)
    if df.empty:
        return None
    norm_targets = [(str(t), normalize_text(str(t))) for t in targets if str(t).strip()]
    best = None
    best_rank = (-1.0, -10**9)
    keys = ["block_num", "par_num", "line_num"]
    for _, grp in df.groupby(keys, sort=False):
        grp = grp.sort_values(["left", "top"]).reset_index(drop=True)
        rows = [r for _, r in grp.iterrows() if str(r.get("text", "")).strip()]
        n = len(rows)
        for i in range(n):
            # A maior parte dos alvos cabe em até 5 tokens; 7 cobre OCR fragmentado.
            for j in range(i, min(n, i + 7)):
                chunk = rows[i:j+1]
                text = " ".join(str(r["text"]).strip() for r in chunk)
                cn = normalize_text(text)
                if not cn:
                    continue
                left = int(min(int(r["left"]) for r in chunk))
                top = int(min(int(r["top"]) for r in chunk))
                right = int(max(int(r["left"]) + int(r["width"]) for r in chunk))
                bottom = int(max(int(r["top"]) + int(r["height"]) for r in chunk))
                for raw, tn in norm_targets:
                    if not tn:
                        continue
                    min_len = max(3, min(8, int(len(tn) * 0.35)))
                    if len(cn) < min_len:
                        continue
                    if tn in cn:
                        score = 100.0
                    else:
                        score = max(fuzz.ratio(tn, cn), fuzz.partial_ratio(tn, cn))
                    if score < threshold:
                        continue
                    # Em empate, prefere trecho menor/mais específico.
                    specificity = -abs(len(cn) - len(tn))
                    rank = (float(score), specificity)
                    if rank > best_rank:
                        best_rank = rank
                        best = OCRLine(text, left, top, right-left, bottom-top, float(score))
    return best

def locate_text_on_screen(
    targets: Sequence[str],
    region_norm: Iterable[float],
    *,
    lang: str = "eng",
    threshold: float = 70,
    psm: int = 6,
    window_title: str = "",
) -> tuple[Optional[OCRLine], Image.Image, tuple[int, int, int, int]]:
    """Localiza texto na tela ou, quando ``window_title`` for informado,
    exclusivamente dentro da janela RDP.

    Em sessões RDP redimensionadas os textos do SAP ficam muito pequenos.
    Para melhorar o OCR, a região capturada é ampliada antes do Tesseract e
    as coordenadas encontradas são convertidas de volta para o tamanho real.
    """
    image, region_abs = screenshot_norm_region(
        region_norm,
        window_title=window_title,
    )

    # O SAP em meia tela pode deixar links/abas com fonte de poucos pixels.
    # Upscale melhora bastante o reconhecimento sem alterar a posição real.
    scale = 1.0
    if str(window_title or "").strip():
        if image.width < 900:
            scale = 3.0
        elif image.width < 1300:
            scale = 2.25
        else:
            scale = 1.6

    ocr_image = image
    if scale > 1.0:
        ocr_image = image.resize(
            (
                max(1, int(round(image.width * scale))),
                max(1, int(round(image.height * scale))),
            ),
            Image.Resampling.LANCZOS,
        )

    found = find_text_span(
        ocr_image,
        targets,
        lang=lang,
        threshold=threshold,
        psm=psm,
    )

    if found:
        if scale > 1.0:
            found.left = int(round(found.left / scale))
            found.top = int(round(found.top / scale))
            found.width = max(1, int(round(found.width / scale)))
            found.height = max(1, int(round(found.height / scale)))

        found.left += region_abs[0]
        found.top += region_abs[1]

    # Retorna a captura ORIGINAL, útil para debug e para manter as coordenadas
    # consistentes com a tela real.
    return found, image, region_abs


def wait_for_text(
    targets: Sequence[str],
    region_norm: Iterable[float],
    *,
    lang: str,
    threshold: float,
    timeout: float,
    poll_interval: float = 0.45,
    window_title: str = "",
) -> Optional[OCRLine]:
    end = time.time() + timeout
    while time.time() < end:
        found, _, _ = locate_text_on_screen(
            targets,
            region_norm,
            lang=lang,
            threshold=threshold,
            psm=6,
            window_title=window_title,
        )
        if found:
            return found
        time.sleep(poll_interval)
    return None


def click_text(
    targets: Sequence[str],
    region_norm: Iterable[float],
    *,
    lang: str,
    threshold: float,
    click_offset: tuple[int, int] = (0, 0),
    window_title: str = "",
) -> tuple[bool, str]:
    found, _, _ = locate_text_on_screen(
        targets,
        region_norm,
        lang=lang,
        threshold=threshold,
        psm=6,
        window_title=window_title,
    )
    if not found:
        return False, ""
    import pyautogui
    x, y = found.center
    pyautogui.click(x + click_offset[0], y + click_offset[1])
    return True, found.text



def locate_link_filename_on_screen(
    targets: Sequence[str],
    region_norm: Iterable[float],
    *,
    lang: str = "eng",
    threshold: float = 62,
    window_title: str = "",
) -> tuple[Optional[OCRLine], Image.Image, tuple[int, int, int, int]]:
    """Localiza o NOME DO ARQUIVO dentro da grade de links do SAP.

    Esta rotina e mais agressiva do que o OCR generico porque os links do SAP
    ficam com fonte muito pequena. Ela:
      1) captura somente a faixa onde ficam os nomes dos JPGs;
      2) amplia a imagem;
      3) aumenta o contraste;
      4) testa mais de uma binarizacao/PSM;
      5) compara a linha inteira com os aliases informados.

    O retorno usa coordenadas absolutas do monitor, mas a captura continua
    restrita a janela RDP.
    """
    image, region_abs = screenshot_norm_region(
        region_norm,
        window_title=window_title,
    )

    # Upscale forte: em RDP 1920x1080 essa faixa ainda tem fonte muito pequena.
    scale = 3.0 if image.width < 1000 else 2.4

    gray = ImageOps.grayscale(image)
    gray = ImageEnhance.Contrast(gray).enhance(2.2)
    big = gray.resize(
        (
            max(1, int(round(gray.width * scale))),
            max(1, int(round(gray.height * scale))),
        ),
        Image.Resampling.LANCZOS,
    )

    arr = np.array(big)
    # Duas variantes adicionais melhoram links sublinhados/azuis do SAP.
    _, bw180 = cv2.threshold(arr, 180, 255, cv2.THRESH_BINARY)
    _, bw205 = cv2.threshold(arr, 205, 255, cv2.THRESH_BINARY)

    variants = [
        ("gray", big),
        ("bw180", Image.fromarray(bw180)),
        ("bw205", Image.fromarray(bw205)),
    ]

    norm_targets = [normalize_text(t) for t in targets if str(t).strip()]
    best: Optional[OCRLine] = None
    best_score = -1.0

    for _, variant in variants:
        for psm in (6, 11, 12):
            df = _image_to_data(variant, lang=lang, psm=psm)
            if df.empty:
                continue

            keys = ["block_num", "par_num", "line_num"]
            for _, grp in df.groupby(keys, sort=False):
                words = [
                    str(t).strip()
                    for t in grp["text"].tolist()
                    if str(t).strip()
                ]
                if not words:
                    continue

                line_text = " ".join(words)
                line_norm = normalize_text(line_text)
                if not line_norm:
                    continue

                score = -1.0
                for target_norm in norm_targets:
                    if not target_norm:
                        continue
                    if target_norm in line_norm:
                        score = max(score, 100.0)
                    else:
                        score = max(
                            score,
                            float(fuzz.partial_ratio(target_norm, line_norm)),
                            float(fuzz.ratio(target_norm, line_norm)),
                        )

                if score < threshold or score <= best_score:
                    continue

                left = int(grp["left"].min())
                top = int(grp["top"].min())
                right = int((grp["left"] + grp["width"]).max())
                bottom = int((grp["top"] + grp["height"]).max())

                # Volta da imagem ampliada para o tamanho real da captura.
                left = int(round(left / scale))
                top = int(round(top / scale))
                right = int(round(right / scale))
                bottom = int(round(bottom / scale))

                best_score = float(score)
                best = OCRLine(
                    line_text,
                    region_abs[0] + left,
                    region_abs[1] + top,
                    max(1, right - left),
                    max(1, bottom - top),
                    float(score),
                )

    return best, image, region_abs



def locate_link_row_strict(
    targets: Sequence[str],
    *,
    window_title: str,
    lang: str = "eng",
    threshold: float = 72,
) -> tuple[Optional[OCRLine], Image.Image, tuple[int, int, int, int]]:
    """Localiza diretamente a linha do JPG desejado na grade Links.

    Esta rotina NAO clica em linhas por tentativa e NAO varre a tela. Ela
    captura somente a grade de links do SAP, faz OCR linha por linha e retorna
    a linha cujo texto contem FACHADADOIMOVEL, ADESIVOLIGACAONOVA ou
    FOTOPANORAMICA. Isso evita cliques acidentais em menus do SAP.

    As coordenadas retornadas ja sao absolutas no monitor.
    """

    regions = [
        # Grade Links na sessao RDP 1920x1080 mostrada nos testes.
        (0.012, 0.285, 0.610, 0.255),
        # Fallback um pouco mais amplo para pequenas variacoes de escala.
        (0.008, 0.270, 0.640, 0.300),
    ]

    normalized_targets = [
        normalize_text(str(t).replace(".JPG", "").replace(".jpg", ""))
        for t in targets
        if str(t).strip()
    ]

    best: Optional[OCRLine] = None
    best_score = -1.0
    best_image: Optional[Image.Image] = None
    best_region: Optional[tuple[int, int, int, int]] = None

    for region_norm in regions:
        image, region_abs = screenshot_norm_region(
            region_norm,
            window_title=window_title,
        )

        # O OCR do link funciona melhor preservando a linha inteira.
        # Testamos original e versoes ampliadas/contrastadas.
        variants: list[tuple[Image.Image, float]] = [(image, 1.0)]

        gray = ImageOps.grayscale(image)
        gray = ImageEnhance.Contrast(gray).enhance(1.8)

        for scale in (1.8, 2.6):
            big = gray.resize(
                (
                    max(1, int(round(gray.width * scale))),
                    max(1, int(round(gray.height * scale))),
                ),
                Image.Resampling.LANCZOS,
            )
            variants.append((big, scale))

        for variant, scale in variants:
            for psm in (6, 11):
                df = _image_to_data(variant, lang=lang, psm=psm)
                if df.empty:
                    continue

                keys = ["block_num", "par_num", "line_num"]
                for _, grp in df.groupby(keys, sort=False):
                    words = [
                        str(t).strip()
                        for t in grp["text"].tolist()
                        if str(t).strip()
                    ]
                    if not words:
                        continue

                    line_text = " ".join(words)
                    line_norm = normalize_text(line_text)
                    if not line_norm:
                        continue

                    score = -1.0
                    for target_norm in normalized_targets:
                        if not target_norm:
                            continue
                        if target_norm in line_norm:
                            candidate = 100.0
                        else:
                            # Fuzzy e apenas fallback. Exigimos score alto para
                            # nao confundir nomes de fotos diferentes.
                            candidate = max(
                                float(fuzz.partial_ratio(target_norm, line_norm)),
                                float(fuzz.ratio(target_norm, line_norm)),
                            )
                        score = max(score, candidate)

                    if score < threshold or score <= best_score:
                        continue

                    left = int(grp["left"].min())
                    top = int(grp["top"].min())
                    right = int((grp["left"] + grp["width"]).max())
                    bottom = int((grp["top"] + grp["height"]).max())

                    if scale != 1.0:
                        left = int(round(left / scale))
                        top = int(round(top / scale))
                        right = int(round(right / scale))
                        bottom = int(round(bottom / scale))

                    best_score = float(score)
                    best = OCRLine(
                        line_text,
                        region_abs[0] + left,
                        region_abs[1] + top,
                        max(1, right - left),
                        max(1, bottom - top),
                        float(score),
                    )
                    best_image = image
                    best_region = region_abs

        if best is not None and best.score >= 99.0:
            break

    if best is not None and best_image is not None and best_region is not None:
        return best, best_image, best_region

    fallback_image, fallback_region = screenshot_norm_region(
        regions[0],
        window_title=window_title,
    )
    return None, fallback_image, fallback_region

def locate_link_row_robust(
    targets: Sequence[str],
    *,
    window_title: str,
    lang: str = "eng",
    threshold: float = 44,
) -> tuple[Optional[OCRLine], Image.Image, tuple[int, int, int, int]]:
    """Localiza uma das tres fotos-alvo na grade Links do SAP.

    Esta rotina foi feita especificamente para a tela 1920x1080 enviada pelo
    usuario. Ela evita OCR da URL inteira: captura somente a parte direita da
    grade, amplia fortemente a imagem e procura o nome do JPG linha por linha.

    O retorno de ``OCRLine`` ja usa coordenadas absolutas do monitor, de modo
    que o chamador pode usar ``found.center[1]`` diretamente para clicar na
    mesma linha do hyperlink.
    """

    # Primeira regiao: parte da grade em que aparecem os nomes dos arquivos.
    # As demais sao fallback caso a escala do SAP varie alguns pixels.
    regions = [
        [0.30, 0.29, 0.32, 0.30],
        [0.28, 0.285, 0.36, 0.31],
        [0.20, 0.275, 0.44, 0.34],
    ]

    target_norms = [
        normalize_text(t)
        for t in targets
        if str(t).strip()
    ]

    best: Optional[OCRLine] = None
    best_score = -1.0
    best_image: Optional[Image.Image] = None
    best_region: Optional[tuple[int, int, int, int]] = None

    for region_norm in regions:
        image, region_abs = screenshot_norm_region(
            region_norm,
            window_title=window_title,
        )

        # O texto da grade e pequeno mesmo em 1920x1080. O upscale de 3.2x
        # funcionou nos prints reais enviados pelo usuario.
        scale = 3.2
        gray = ImageOps.grayscale(image)
        gray = ImageEnhance.Contrast(gray).enhance(2.0)
        gray = gray.filter(ImageFilter.SHARPEN)
        big = gray.resize(
            (
                max(1, int(round(gray.width * scale))),
                max(1, int(round(gray.height * scale))),
            ),
            Image.Resampling.LANCZOS,
        )

        arr = np.array(big)
        _, bw190 = cv2.threshold(arr, 190, 255, cv2.THRESH_BINARY)
        _, bw215 = cv2.threshold(arr, 215, 255, cv2.THRESH_BINARY)

        variants = [
            big,
            Image.fromarray(bw190),
            Image.fromarray(bw215),
        ]

        for variant in variants:
            for psm in (6, 11):
                df = _image_to_data(
                    variant,
                    lang=lang,
                    psm=psm,
                )

                if df.empty:
                    continue

                # Tesseract normalmente separa cada URL como uma linha.
                keys = ["block_num", "par_num", "line_num"]
                for _, grp in df.groupby(keys, sort=False):
                    words = [
                        str(t).strip()
                        for t in grp["text"].tolist()
                        if str(t).strip()
                    ]
                    if not words:
                        continue

                    line_text = " ".join(words)
                    line_norm = normalize_text(line_text)
                    if not line_norm:
                        continue

                    score = -1.0
                    for target_norm in target_norms:
                        if not target_norm:
                            continue

                        if target_norm in line_norm:
                            candidate = 100.0
                        else:
                            candidate = max(
                                float(fuzz.partial_ratio(target_norm, line_norm)),
                                float(fuzz.ratio(target_norm, line_norm)),
                            )

                        score = max(score, candidate)

                    if score < threshold or score <= best_score:
                        continue

                    left = int(grp["left"].min())
                    top = int(grp["top"].min())
                    right = int((grp["left"] + grp["width"]).max())
                    bottom = int((grp["top"] + grp["height"]).max())

                    left = int(round(left / scale))
                    top = int(round(top / scale))
                    right = int(round(right / scale))
                    bottom = int(round(bottom / scale))

                    best_score = float(score)
                    best = OCRLine(
                        line_text,
                        region_abs[0] + left,
                        region_abs[1] + top,
                        max(1, right - left),
                        max(1, bottom - top),
                        float(score),
                    )
                    best_image = image
                    best_region = region_abs

        # Se achou match exato, nao ha motivo para testar regioes maiores.
        if best is not None and best.score >= 99.0:
            break

    if best is not None and best_image is not None and best_region is not None:
        return best, best_image, best_region

    # Mantem uma imagem de debug mesmo quando nao encontrar nada.
    fallback_image, fallback_region = screenshot_norm_region(
        regions[0],
        window_title=window_title,
    )
    return None, fallback_image, fallback_region





def locate_link_row_fullscreen(
    targets: Sequence[str],
    *,
    lang: str = "eng",
    threshold: float = 58,
) -> tuple[Optional[OCRLine], Image.Image, tuple[int, int, int, int]]:
    """Localiza uma foto na grade Links usando a TELA INTEIRA local.

    Esta rotina foi criada para o cenário confirmado pelo usuário: o RDP está
    em tela cheia e a resolução local é 1920x1080. Assim, evitamos qualquer
    diferença de coordenadas causada pela moldura/título da janela RDP.

    O OCR é feito somente na região onde a grade Links aparece. O retorno usa
    coordenadas absolutas do monitor, prontas para clique.
    """
    import pyautogui

    sw, sh = pyautogui.size()
    full = pyautogui.screenshot()

    # Região observada nos prints reais 1920x1080. Mantemos proporcionalidade
    # caso o Windows reporte pequenas diferenças de escala/resolução.
    rx, ry, rw, rh = (0.012, 0.270, 0.615, 0.320)
    x = max(0, int(round(sw * rx)))
    y = max(0, int(round(sh * ry)))
    w = max(1, min(sw - x, int(round(sw * rw))))
    h = max(1, min(sh - y, int(round(sh * rh))))
    region_abs = (x, y, w, h)
    image = full.crop((x, y, x + w, y + h))

    target_norms = [
        normalize_text(str(t).replace(".JPG", "").replace(".jpg", ""))
        for t in targets
        if str(t).strip()
    ]

    # Variantes que funcionaram melhor nos prints enviados.
    variants: list[tuple[Image.Image, float]] = [(image, 1.0)]
    gray = ImageOps.grayscale(image)
    gray = ImageEnhance.Contrast(gray).enhance(2.0)
    gray = gray.filter(ImageFilter.SHARPEN)

    for scale in (2.5, 3.5, 4.5):
        big = gray.resize(
            (
                max(1, int(round(gray.width * scale))),
                max(1, int(round(gray.height * scale))),
            ),
            Image.Resampling.LANCZOS,
        )
        variants.append((big, scale))

    best: Optional[OCRLine] = None
    best_score = -1.0

    for variant, scale in variants:
        for psm in (6, 11):
            df = _image_to_data(variant, lang=lang, psm=psm)
            if df.empty:
                continue

            for _, grp in df.groupby(["block_num", "par_num", "line_num"], sort=False):
                words = [
                    str(t).strip()
                    for t in grp["text"].tolist()
                    if str(t).strip()
                ]
                if not words:
                    continue

                line_text = " ".join(words)
                line_norm = normalize_text(line_text)
                if not line_norm:
                    continue

                score = -1.0
                for target_norm in target_norms:
                    if not target_norm:
                        continue
                    if target_norm in line_norm:
                        candidate = 100.0
                    else:
                        candidate = max(
                            float(fuzz.partial_ratio(target_norm, line_norm)),
                            float(fuzz.ratio(target_norm, line_norm)),
                        )
                    score = max(score, candidate)

                if score < threshold or score <= best_score:
                    continue

                left = int(grp["left"].min())
                top = int(grp["top"].min())
                right = int((grp["left"] + grp["width"]).max())
                bottom = int((grp["top"] + grp["height"]).max())

                if scale != 1.0:
                    left = int(round(left / scale))
                    top = int(round(top / scale))
                    right = int(round(right / scale))
                    bottom = int(round(bottom / scale))

                best_score = float(score)
                best = OCRLine(
                    line_text,
                    x + left,
                    y + top,
                    max(1, right - left),
                    max(1, bottom - top),
                    float(score),
                )

        if best is not None and best.score >= 99.0:
            break

    return best, image, region_abs


def detect_link_row_centers(
    *,
    window_title: str,
    region_norm: Sequence[float] = (0.01, 0.29, 0.63, 0.36),
) -> tuple[list[int], Image.Image, tuple[int, int, int, int]]:
    """Detecta as linhas visiveis da grade Links sem depender de OCR.

    O SAP desenha cada URL em uma linha horizontal regular. Esta rotina usa
    apenas densidade de pixels escuros para descobrir o centro Y de cada linha.
    Isso e muito mais robusto do que tentar ler o nome pequeno do JPG na grade.

    Retorna os centros Y em coordenadas absolutas do monitor, a captura usada
    para diagnostico e a regiao absoluta da grade.
    """
    image, region_abs = screenshot_norm_region(
        region_norm,
        window_title=window_title,
    )

    gray = np.array(ImageOps.grayscale(image))
    width = gray.shape[1]

    def _bands_for_threshold(level: int) -> list[tuple[int, int, float]]:
        # Mantem principalmente texto/underline escuro e ignora grande parte
        # das linhas claras da grade.
        mask = (gray < level).astype(np.uint8)
        projection = mask.sum(axis=1).astype(np.float32)

        # Suavizacao curta para unir partes de uma mesma linha de texto.
        kernel = np.ones(3, dtype=np.float32) / 3.0
        smooth = np.convolve(projection, kernel, mode="same")
        min_pixels = max(8.0, width * 0.006)
        ys = np.where(smooth > min_pixels)[0]

        if len(ys) == 0:
            return []

        groups: list[tuple[int, int]] = []
        start = prev = int(ys[0])
        for raw_y in ys[1:]:
            y = int(raw_y)
            if y <= prev + 2:
                prev = y
            else:
                groups.append((start, prev))
                start = prev = y
        groups.append((start, prev))

        out: list[tuple[int, int, float]] = []
        for a, b in groups:
            height = b - a + 1
            if height < 2 or height > 20:
                continue
            strength = float(smooth[a:b + 1].max())
            if strength < min_pixels:
                continue
            out.append((a, b, strength))
        return out

    # Tenta limiares progressivamente mais permissivos.
    bands: list[tuple[int, int, float]] = []
    for level in (165, 180, 195):
        candidate = _bands_for_threshold(level)
        if len(candidate) >= 3:
            bands = candidate
            break
        if len(candidate) > len(bands):
            bands = candidate

    centers_local = [int(round((a + b) / 2.0)) for a, b, _ in bands]

    # Remove centros muito proximos/duplicados.
    deduped: list[int] = []
    for y in sorted(centers_local):
        if not deduped or y - deduped[-1] >= 7:
            deduped.append(y)

    centers_abs = [region_abs[1] + y for y in deduped]
    return centers_abs, image, region_abs

def _normalize_ocr_coord_text(text: str) -> str:
    """Normaliza caracteres que o OCR costuma confundir no carimbo GPS."""
    s = str(text or "")
    s = s.replace("−", "-").replace("–", "-").replace("—", "-")
    s = s.replace(";", ",")
    # Correcoes conservadoras somente quando aparecem junto de numeros.
    s = re.sub(r"(?<=\d)[Oo](?=\d)", "0", s)
    s = re.sub(r"(?<=\d)[Il](?=\d)", "1", s)
    return s


def _number_candidates_from_ocr_token(
    token: str,
    *,
    kind: str,
) -> list[tuple[float, bool]]:
    """Converte um token OCR em candidatos numericos.

    Retorna pares ``(valor, reconstruido)``. A reconstrucao cobre o erro mais
    comum do Tesseract no carimbo das fotos: perder o ponto decimal, por exemplo
    ``4449856`` em vez de ``44.49856``.
    """
    raw = _normalize_ocr_coord_text(token).strip()
    if not raw:
        return []

    sign = -1.0 if raw.startswith("-") else 1.0
    body = raw.lstrip("+-").replace(",", ".")
    body = re.sub(r"[^0-9.]", "", body)
    body = re.sub(r"\.{2,}", ".", body)

    out: list[tuple[float, bool]] = []

    # Leitura direta quando o decimal sobreviveu ao OCR.
    if body.count(".") == 1:
        try:
            value = sign * float(body)
            out.append((value, False))
        except ValueError:
            pass

    digits = re.sub(r"\D", "", body)
    if not digits:
        return out

    # Se o decimal sumiu, reconstrói usando a geometria esperada de latitude/
    # longitude. Para EQTL_MA, latitude normalmente possui 1 algarismo inteiro
    # e longitude 2; mantemos uma segunda opcao para casos de fronteira.
    split_positions = (1, 2) if kind == "lat" else (2, 3)
    for split in split_positions:
        if len(digits) <= split + 2:
            continue
        try:
            value = sign * float(digits[:split] + "." + digits[split:])
        except ValueError:
            continue
        out.append((value, True))

    # Remove duplicatas preservando a leitura direta primeiro.
    deduped: list[tuple[float, bool]] = []
    seen: set[float] = set()
    for value, rebuilt in out:
        key = round(value, 8)
        if key in seen:
            continue
        seen.add(key)
        deduped.append((value, rebuilt))
    return deduped


def _coordinate_score(lat: float, lon: float, *, rebuilt: bool = False) -> float:
    """Pontua candidatos para priorizar coordenadas plausiveis da operacao MA."""
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return -10_000.0

    score = 0.0

    # Brasil.
    if -35.5 <= lat <= 6.5 and -75.5 <= lon <= -30.0:
        score += 40.0

    # Maranhão - faixa deliberadamente ampla para não excluir bordas do estado.
    if -11.5 <= lat <= 0.5 and -50.0 <= lon <= -40.0:
        score += 60.0

    if lat < 0:
        score += 6.0
    if lon < 0:
        score += 8.0
    if rebuilt:
        score -= 4.0

    return score


def _find_coordinate_pairs(text: str) -> list[tuple[float, float, str, float]]:
    """Extrai pares de coordenadas, inclusive quando o OCR perde o decimal."""
    s = _normalize_ocr_coord_text(text)
    out: list[tuple[float, float, str, float]] = []

    # 1) Formato normal, ex.: -4.40731, -44.49856
    strict_patterns = [
        r"(-?\d{1,2}[\.,]\d{3,8})\s*[, ]\s*(-?\d{2,3}[\.,]\d{3,8})",
        r"(-?\d{1,2}[\.,]\d{3,8})\s+(-?\d{2,3}[\.,]\d{3,8})",
    ]
    for pattern in strict_patterns:
        for m in re.finditer(pattern, s):
            try:
                lat = float(m.group(1).replace(",", "."))
                lon = float(m.group(2).replace(",", "."))
            except ValueError:
                continue
            score = _coordinate_score(lat, lon, rebuilt=False) + 15.0
            if score > -1000:
                out.append((lat, lon, m.group(0), score))

    # 2) Formato permissivo. Aceita ponto decimal perdido em um dos lados, como
    #    "40731,-4449856" ou "40731,-44.49856".
    loose_pattern = re.compile(
        r"(?<!\d)([-+]?\d[\d\.,]{3,9})\s*[, ]\s*([-+]?\d[\d\.,]{5,12})(?!\d)"
    )
    for m in loose_pattern.finditer(s):
        lat_tokens = _number_candidates_from_ocr_token(m.group(1), kind="lat")
        lon_tokens = _number_candidates_from_ocr_token(m.group(2), kind="lon")
        for lat, lat_rebuilt in lat_tokens:
            for lon, lon_rebuilt in lon_tokens:
                # Longitude das fotos da operacao e oeste; se o OCR perdeu apenas
                # o sinal, corrigimos depois de validar a magnitude brasileira.
                if lon > 0 and 30 <= lon <= 76:
                    lon = -lon
                score = _coordinate_score(
                    lat,
                    lon,
                    rebuilt=lat_rebuilt or lon_rebuilt,
                )
                if score > 0:
                    out.append((lat, lon, m.group(0), score))

    # Remove duplicatas e mantém a maior pontuação.
    best_by_key: dict[tuple[float, float], tuple[float, float, str, float]] = {}
    for item in out:
        lat, lon, matched, score = item
        key = (round(lat, 7), round(lon, 7))
        old = best_by_key.get(key)
        if old is None or score > old[3]:
            best_by_key[key] = item

    return sorted(best_by_key.values(), key=lambda x: x[3], reverse=True)


def _photo_only_for_coordinate_ocr(photo: Image.Image) -> tuple[Image.Image, bool]:
    """Se ``photo`` for na verdade um print do navegador, recorta só a foto.

    O problema observado no fluxo real era o OCR receber a janela inteira do
    navegador. O carimbo ficava fora do rodapé calculado. Detectamos esse caso
    pela grande proporção de fundo quase branco e localizamos o maior bloco
    colorido da tela.
    """
    rgb = photo.convert("RGB")
    arr = np.array(rgb)
    if arr.size == 0:
        return rgb, False

    gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
    white_ratio = float(np.mean(gray >= 247))

    # Uma foto normal raramente tem mais de 38% dos pixels praticamente brancos.
    # Só tentamos o recorte automático quando há forte evidência de screenshot.
    if white_ratio < 0.38:
        return rgb, False

    hsv = cv2.cvtColor(arr, cv2.COLOR_RGB2HSV)
    saturation = hsv[:, :, 1]
    value = hsv[:, :, 2]

    mask = ((saturation > 28) & (value < 252)).astype(np.uint8) * 255
    mask = cv2.morphologyEx(
        mask,
        cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_RECT, (11, 11)),
        iterations=2,
    )
    mask = cv2.morphologyEx(
        mask,
        cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5)),
        iterations=1,
    )

    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    candidates: list[tuple[int, int, int, int, int]] = []
    h, w = arr.shape[:2]
    for cnt in contours:
        x, y, cw, ch = cv2.boundingRect(cnt)
        area = cw * ch
        if cw < max(120, int(w * 0.10)) or ch < max(160, int(h * 0.18)):
            continue
        if area < int(w * h * 0.025):
            continue
        candidates.append((area, x, y, cw, ch))

    if not candidates:
        return rgb, False

    _, x, y, cw, ch = max(candidates, key=lambda t: t[0])

    # Pequena margem para não cortar o primeiro/último pixel da fotografia.
    pad = 2
    x1 = max(0, x - pad)
    y1 = max(0, y - pad)
    x2 = min(w, x + cw + pad)
    y2 = min(h, y + ch + pad)

    cropped = rgb.crop((x1, y1, x2, y2))
    if cropped.width < 120 or cropped.height < 160:
        return rgb, False
    return cropped, True


def _coordinate_regions(photo: Image.Image) -> list[tuple[str, Image.Image]]:
    """Gera regiões progressivas onde o carimbo GPS costuma aparecer."""
    w, h = photo.size
    boxes = [
        ("rodape_direita", (int(w * 0.45), int(h * 0.72), w, h)),
        ("rodape_direita_amplo", (int(w * 0.28), int(h * 0.64), w, h)),
        ("rodape_inteiro", (0, int(h * 0.68), w, h)),
        ("metade_inferior", (0, int(h * 0.52), w, h)),
    ]
    return [(name, photo.crop(box)) for name, box in boxes]


def _ocr_coordinate_region(region: Image.Image, lang: str) -> list[dict]:
    """Executa poucas variantes fortes de OCR em uma região do carimbo."""
    arr = np.array(region.convert("RGB"))
    if arr.size == 0:
        return []
    gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)

    attempts: list[dict] = []
    # 6x preserva melhor o pequeno texto sobreposto das fotos de campo.
    up = cv2.resize(gray, None, fx=6.0, fy=6.0, interpolation=cv2.INTER_CUBIC)
    clahe = cv2.createCLAHE(clipLimit=2.2, tileGridSize=(8, 8)).apply(up)

    # Realça texto claro e sua sombra sem explodir o ruído da fotografia.
    _, bw = cv2.threshold(clahe, 175, 255, cv2.THRESH_BINARY)
    variants = [
        ("gray6x", up),
        ("clahe6x", clahe),
        ("bw175", bw),
    ]

    for variant_name, variant in variants:
        for psm in (6, 11):
            config = (
                f"--psm {psm} "
                "-c tessedit_char_whitelist=0123456789-.,:()/UTC "
            )
            try:
                text = pytesseract.image_to_string(variant, lang=lang, config=config)
            except pytesseract.TesseractError:
                text = pytesseract.image_to_string(variant, lang="eng", config=config)
            candidates = _find_coordinate_pairs(text)
            attempts.append(
                {
                    "variant": f"{variant_name}_psm{psm}",
                    "text": text,
                    "candidates": candidates,
                }
            )
    return attempts


def extract_coordinates_from_photo(
    photo: Image.Image,
    lang: str = "eng",
    prefer_negative_lat: bool = True,
) -> dict:
    """Extrai latitude/longitude da FACHADA DO IMOVEL.

    A função agora suporta tanto a imagem JPG pura quanto um screenshot do
    navegador contendo a foto em uma pequena área. O OCR começa no canto
    inferior direito e amplia progressivamente a busca.
    """
    prepared, auto_cropped = _photo_only_for_coordinate_ocr(photo)

    all_attempts: list[dict] = []
    all_candidates: list[tuple[float, float, str, float, str]] = []

    regions = _coordinate_regions(prepared)
    region_bonus = {
        "rodape_direita": 18.0,
        "rodape_direita_amplo": 12.0,
        "rodape_inteiro": 7.0,
        "metade_inferior": 2.0,
    }

    for region_name, region in regions:
        attempts = _ocr_coordinate_region(region, lang)
        for attempt in attempts:
            attempt["region"] = region_name
            all_attempts.append(attempt)
            for lat, lon, matched, score in attempt["candidates"]:
                # O sinal de menos da latitude é minúsculo e pode desaparecer.
                if prefer_negative_lat and 0 < lat <= 12.0 and -75.5 <= lon <= -30.0:
                    lat = -lat
                final_score = score + region_bonus.get(region_name, 0.0)
                all_candidates.append(
                    (lat, lon, matched, final_score, f"{region_name}:{attempt['variant']}")
                )

        # Quando a região mais específica produz um candidato muito forte, evita
        # OCR desnecessário nas áreas maiores.
        if all_candidates and max(c[3] for c in all_candidates) >= 115.0:
            break

    if all_candidates:
        # Agrupa leituras quase iguais. Isso neutraliza pequenas diferenças entre
        # variantes (ex.: quinta casa decimal) sem misturar coordenadas distintas.
        clusters: dict[tuple[float, float], list[tuple[float, float, str, float, str]]] = {}
        for item in all_candidates:
            lat, lon = item[0], item[1]
            key = (round(lat, 3), round(lon, 3))
            clusters.setdefault(key, []).append(item)

        def cluster_rank(items):
            return (len(items), max(i[3] for i in items), sum(i[3] for i in items))

        best_cluster = max(clusters.values(), key=cluster_rank)
        best = max(best_cluster, key=lambda i: i[3])
        lat, lon, matched, _, variant = best

        # Mantém precisão lida pelo OCR. Não arredonda para 3 casas; o arredondamento
        # acima serve somente para decidir qual grupo de leituras representa o mesmo
        # carimbo.
        return {
            "latitude": float(lat),
            "longitude": float(lon),
            "matched_text": matched,
            "ocr_text": "\n---\n".join(
                f"[{a['region']}|{a['variant']}] {a['text'].strip()}"
                for a in all_attempts
                if str(a.get("text", "")).strip()
            ),
            "variant": variant,
            "attempts": all_attempts,
            "photo_auto_cropped": auto_cropped,
            "ocr_photo_size": prepared.size,
        }

    return {
        "latitude": None,
        "longitude": None,
        "matched_text": "",
        "ocr_text": "\n---\n".join(
            f"[{a['region']}|{a['variant']}] {a['text'].strip()}"
            for a in all_attempts
            if str(a.get("text", "")).strip()
        ),
        "variant": "",
        "attempts": all_attempts,
        "photo_auto_cropped": auto_cropped,
        "ocr_photo_size": prepared.size,
    }


def extract_largest_photo_from_screen(
    screenshot: Image.Image,
    region_norm: Iterable[float],
    *,
    min_area_ratio: float = 0.02,
) -> tuple[Image.Image, tuple[int, int, int, int], bool]:
    """Recorta a foto real de um navegador com grande fundo branco.

    Primeiro usa saturação/cor (mais robusto para fotos dentro de IE/Edge) e só
    depois cai no método antigo de pixels não brancos.
    """
    sw, sh = screenshot.size
    x, y, rw, rh = norm_region_to_abs(region_norm, (sw, sh))
    area_img = screenshot.crop((x, y, x + rw, y + rh)).convert("RGB")
    area = np.array(area_img)

    # Estrategia 1: maior bloco colorido. Em uma página com fundo branco, a foto
    # de campo é disparado o maior retângulo com saturação relevante.
    hsv = cv2.cvtColor(area, cv2.COLOR_RGB2HSV)
    sat = hsv[:, :, 1]
    val = hsv[:, :, 2]
    color_mask = ((sat > 28) & (val < 252)).astype(np.uint8) * 255
    color_mask = cv2.morphologyEx(
        color_mask,
        cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_RECT, (11, 11)),
        iterations=2,
    )
    color_mask = cv2.morphologyEx(
        color_mask,
        cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5)),
        iterations=1,
    )
    contours, _ = cv2.findContours(color_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    candidates = []
    min_area = max(2500, int(rw * rh * min_area_ratio))
    for cnt in contours:
        bx, by, bw, bh = cv2.boundingRect(cnt)
        if bw < 120 or bh < 160:
            continue
        if bw * bh < min_area:
            continue
        candidates.append((bw * bh, bx, by, bw, bh))

    if candidates:
        _, bx, by, bw, bh = max(candidates, key=lambda t: t[0])
        pad = 2
        bx2 = max(0, bx - pad)
        by2 = max(0, by - pad)
        ex = min(rw, bx + bw + pad)
        ey = min(rh, by + bh + pad)
        crop = area_img.crop((bx2, by2, ex, ey))
        if crop.width >= 120 and crop.height >= 160:
            return crop, (x + bx2, y + by2, ex - bx2, ey - by2), True

    # Estrategia 2: compatibilidade com fotos pouco saturadas.
    gray = cv2.cvtColor(area, cv2.COLOR_RGB2GRAY)
    mask = (gray < 245).astype(np.uint8) * 255
    mask = cv2.morphologyEx(
        mask,
        cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3)),
        iterations=1,
    )
    mask = cv2.morphologyEx(
        mask,
        cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3)),
        iterations=1,
    )
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    candidates = []
    for cnt in contours:
        bx, by, bw, bh = cv2.boundingRect(cnt)
        area_px = bw * bh
        if area_px < min_area or bw < 120 or bh < 160:
            continue
        candidates.append((area_px, bx, by, bw, bh))

    if not candidates:
        return area_img, (x, y, rw, rh), False

    _, bx, by, bw, bh = max(candidates, key=lambda t: t[0])
    margin = 1
    bx2 = max(0, bx + margin)
    by2 = max(0, by + margin)
    ex = min(rw, bx + bw - margin)
    ey = min(rh, by + bh - margin)
    crop = area_img.crop((bx2, by2, ex, ey))
    return crop, (x + bx2, y + by2, ex - bx2, ey - by2), True

def save_debug_image(image: Image.Image, path: Path | str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)
