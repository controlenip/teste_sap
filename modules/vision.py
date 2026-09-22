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
from PIL import Image, ImageEnhance, ImageOps
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


def _normalize_ocr_coord_text(text: str) -> str:
    s = str(text or "")
    s = s.replace("−", "-").replace("–", "-").replace("—", "-")
    s = s.replace(";", ",")
    return s


def _find_coordinate_pairs(text: str) -> list[tuple[float, float, str]]:
    s = _normalize_ocr_coord_text(text)
    patterns = [
        r"(-?\d{1,2}[\.,]\d{3,8})\s*[, ]\s*(-?\d{2,3}[\.,]\d{3,8})",
        r"(-?\d{1,2}[\.,]\d{3,8})\s+(-?\d{2,3}[\.,]\d{3,8})",
    ]
    out: list[tuple[float, float, str]] = []
    for pattern in patterns:
        for m in re.finditer(pattern, s):
            a = m.group(1).replace(",", ".")
            b = m.group(2).replace(",", ".")
            try:
                lat = float(a)
                lon = float(b)
            except ValueError:
                continue
            if -90 <= lat <= 90 and -180 <= lon <= 180:
                out.append((lat, lon, m.group(0)))
    # Remove duplicatas preservando ordem.
    seen = set()
    unique = []
    for item in out:
        key = (round(item[0], 8), round(item[1], 8))
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def _pick_best_coordinate(candidates: Sequence[tuple[float, float, str]]) -> Optional[tuple[float, float, str]]:
    if not candidates:
        return None
    # Prioriza coordenadas plausíveis para Brasil e, em seguida, qualquer par válido.
    br = [c for c in candidates if -35.5 <= c[0] <= 6.5 and -75.5 <= c[1] <= -30.0]
    pool = br or list(candidates)
    return pool[0] if pool else None


def preprocess_footer_variants(photo: Image.Image) -> list[tuple[str, np.ndarray]]:
    arr = np.array(photo.convert("RGB"))
    h, w = arr.shape[:2]
    # O carimbo aparece no rodapé; pega faixa ampla e privilegia metade direita.
    footer = arr[max(0, int(h * 0.78)) : h, max(0, int(w * 0.32)) : w]
    gray = cv2.cvtColor(footer, cv2.COLOR_RGB2GRAY)
    up = cv2.resize(gray, None, fx=4.0, fy=4.0, interpolation=cv2.INTER_CUBIC)
    clahe = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8)).apply(up)
    variants: list[tuple[str, np.ndarray]] = [("gray", up), ("clahe", clahe)]
    for th in (160, 180, 200, 220):
        _, bw = cv2.threshold(clahe, th, 255, cv2.THRESH_BINARY)
        variants.append((f"bw_{th}", bw))
        variants.append((f"inv_{th}", 255 - bw))
    return variants


def extract_coordinates_from_photo(photo: Image.Image, lang: str = "eng", prefer_negative_lat: bool = True) -> dict:
    attempts = []
    all_candidates: list[tuple[float, float, str, str]] = []
    for name, variant in preprocess_footer_variants(photo):
        try:
            text = pytesseract.image_to_string(
                variant,
                lang=lang,
                config="--psm 6 -c tessedit_char_whitelist=0123456789-.,:()/UTC ",
            )
        except pytesseract.TesseractError:
            text = pytesseract.image_to_string(
                variant,
                lang="eng",
                config="--psm 6 -c tessedit_char_whitelist=0123456789-.,:()/UTC ",
            )
        candidates = _find_coordinate_pairs(text)
        attempts.append({"variant": name, "text": text, "candidates": candidates})
        for lat, lon, matched in candidates:
            all_candidates.append((lat, lon, matched, name))

    if all_candidates:
        # Votação entre as variantes de pré-processamento. OCR pode inserir/remover
        # um dígito em uma variante isolada; a coordenada real costuma se repetir.
        from collections import Counter

        br_candidates = [
            c for c in all_candidates
            if -35.5 <= c[0] <= 6.5 and -75.5 <= c[1] <= -30.0
        ]
        pool = br_candidates or all_candidates
        keys = [(round(c[0], 5), round(c[1], 5)) for c in pool]
        counts = Counter(keys)
        best_key, _ = counts.most_common(1)[0]
        members = [c for c, key in zip(pool, keys) if key == best_key]
        # Prefere membro com sinal negativo na latitude quando o consenso é negativo
        # e, entre eles, o menor número de casas extraídas além das 5 esperadas.
        best = members[0]
        lat, lon, matched, name = best
        out_lat = float(best_key[0])
        out_lon = float(best_key[1])
        # As fotos deste fluxo são da operação EQTL_MA. O sinal de menos da latitude
        # é muito pequeno no carimbo e é o erro de OCR mais comum. Quando configurado,
        # corrige somente latitudes baixas positivas com longitude oeste do Brasil.
        if prefer_negative_lat and 0 < out_lat <= 12.0 and -75.5 <= out_lon <= -30.0:
            out_lat = -out_lat
        return {
            "latitude": out_lat,
            "longitude": out_lon,
            "matched_text": matched,
            "ocr_text": "\n---\n".join(
                f"[{a['variant']}] {a['text'].strip()}" for a in attempts if a['text'].strip()
            ),
            "variant": f"consenso:{name}",
            "attempts": attempts,
        }

    return {
        "latitude": None,
        "longitude": None,
        "matched_text": "",
        "ocr_text": "\n---\n".join(
            f"[{a['variant']}] {a['text'].strip()}" for a in attempts if a['text'].strip()
        ),
        "variant": "",
        "attempts": attempts,
    }


def extract_largest_photo_from_screen(
    screenshot: Image.Image,
    region_norm: Iterable[float],
    *,
    min_area_ratio: float = 0.02,
) -> tuple[Image.Image, tuple[int, int, int, int], bool]:
    """Recorta a maior área não branca dentro da região do navegador.

    Retorna (imagem, bbox absoluto, encontrou_recorte). Se não houver um contorno
    confiável, retorna a própria região configurada como fallback.
    """
    sw, sh = screenshot.size
    x, y, rw, rh = norm_region_to_abs(region_norm, (sw, sh))
    area = np.array(screenshot.crop((x, y, x + rw, y + rh)).convert("RGB"))
    gray = cv2.cvtColor(area, cv2.COLOR_RGB2GRAY)
    # Fundo do navegador é quase branco. Inverte para transformar foto em primeiro plano.
    mask = (gray < 245).astype(np.uint8) * 255
    # Remove linhas finas da barra do navegador antes de fechar pequenos buracos.
    # Isso evita que uma linha horizontal da UI "cole" a foto a uma grande área branca.
    open_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, open_kernel, iterations=1)
    close_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, close_kernel, iterations=1)
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    candidates = []
    min_area = max(2500, int(rw * rh * min_area_ratio))
    for cnt in contours:
        bx, by, bw, bh = cv2.boundingRect(cnt)
        area_px = bw * bh
        if area_px < min_area:
            continue
        # Evita linhas finas/toolbar; fotos têm dimensão substancial em X e Y.
        if bw < 120 or bh < 160:
            continue
        candidates.append((area_px, bx, by, bw, bh))

    if not candidates:
        fallback = screenshot.crop((x, y, x + rw, y + rh))
        return fallback, (x, y, rw, rh), False

    _, bx, by, bw, bh = max(candidates, key=lambda t: t[0])
    # Pequena margem interna para evitar bordas do navegador.
    margin = 1
    bx2 = max(0, bx + margin)
    by2 = max(0, by + margin)
    ex = min(rw, bx + bw - margin)
    ey = min(rh, by + bh - margin)
    crop = Image.fromarray(area[by2:ey, bx2:ex])
    return crop, (x + bx2, y + by2, ex - bx2, ey - by2), True


def save_debug_image(image: Image.Image, path: Path | str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)
