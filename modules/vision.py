from __future__ import annotations

import os
import re
import time
import unicodedata

from dataclasses import dataclass
from pathlib import Path
from typing import (
    Iterable,
    Optional,
    Sequence,
    Tuple,
)

import cv2
import numpy as np
import pandas as pd
import pytesseract

from PIL import Image
from rapidfuzz import fuzz

from .config import norm_region_to_abs

from .window_control import (
    norm_region_in_window_to_abs,
    screenshot_window_contains,
)


@dataclass
class OCRLine:
    text: str
    left: int
    top: int
    width: int
    height: int
    score: float = 0.0

    @property
    def center(
        self,
    ) -> Tuple[int, int]:
        return (
            self.left + self.width // 2,
            self.top + self.height // 2,
        )


def normalize_text(
    value: str,
) -> str:

    s = unicodedata.normalize(
        "NFKD",
        str(value or ""),
    )

    s = "".join(
        c
        for c in s
        if not unicodedata.combining(c)
    )

    s = s.upper()

    return re.sub(
        r"[^A-Z0-9]+",
        "",
        s,
    )


def configure_tesseract(
    cmd: str,
) -> None:

    if cmd:
        pytesseract.pytesseract.tesseract_cmd = cmd

        tessdata = (
            Path(cmd)
            .resolve()
            .parent
            / "tessdata"
        )

        if tessdata.exists():
            os.environ[
                "TESSDATA_PREFIX"
            ] = str(tessdata)


def screenshot_full(
    window_title: str = "",
) -> Image.Image:
    """
    Captura a tela inteira ou,
    quando informado,
    somente a janela remota.
    """

    import pyautogui

    if str(
        window_title or ""
    ).strip():

        image, _ = (
            screenshot_window_contains(
                str(window_title),
                content_only=False,
            )
        )

        return image

    return pyautogui.screenshot()


def screenshot_norm_region(
    region: Iterable[float],
    *,
    window_title: str = "",
) -> tuple[
    Image.Image,
    tuple[int, int, int, int],
]:
    """
    Captura uma região normalizada.

    Se ``window_title`` for informado,
    as coordenadas normalizadas
    são relativas somente à janela RDP,
    nunca ao monitor inteiro.
    """

    import pyautogui

    if str(
        window_title or ""
    ).strip():

        abs_region = (
            norm_region_in_window_to_abs(
                str(window_title),
                region,
                content_only=False,
            )
        )

    else:
        sw, sh = pyautogui.size()

        abs_region = norm_region_to_abs(
            region,
            (sw, sh),
        )

    return (
        pyautogui.screenshot(
            region=abs_region
        ),
        abs_region,
    )


def _image_to_data(
    image: Image.Image,
    lang: str = "eng",
    psm: int = 6,
) -> pd.DataFrame:

    try:
        df = pytesseract.image_to_data(
            image,
            lang=lang,
            config=f"--psm {psm}",
            output_type=(
                pytesseract
                .Output
                .DATAFRAME
            ),
        )

    except pytesseract.TesseractError:
        # Fallback para instalação
        # sem pacote de idioma selecionado.
        df = pytesseract.image_to_data(
            image,
            lang="eng",
            config=f"--psm {psm}",
            output_type=(
                pytesseract
                .Output
                .DATAFRAME
            ),
        )

    if df is None or df.empty:
        return pd.DataFrame()

    return df.dropna(
        subset=["text"]
    ).copy()


def ocr_lines(
    image: Image.Image,
    lang: str = "eng",
    psm: int = 6,
) -> list[OCRLine]:

    df = _image_to_data(
        image,
        lang=lang,
        psm=psm,
    )

    if df.empty:
        return []

    lines: list[OCRLine] = []

    keys = [
        "block_num",
        "par_num",
        "line_num",
    ]

    for _, grp in df.groupby(
        keys,
        sort=False,
    ):

        words = [
            str(t).strip()
            for t in grp["text"].tolist()
            if str(t).strip()
        ]

        if not words:
            continue

        left = int(
            grp["left"].min()
        )

        top = int(
            grp["top"].min()
        )

        right = int(
            (
                grp["left"]
                + grp["width"]
            ).max()
        )

        bottom = int(
            (
                grp["top"]
                + grp["height"]
            ).max()
        )

        lines.append(
            OCRLine(
                " ".join(words),
                left,
                top,
                right - left,
                bottom - top,
            )
        )

    return lines


def match_line(
    lines: Sequence[OCRLine],
    targets: Sequence[str],
    threshold: float = 70,
) -> Optional[OCRLine]:

    best: Optional[OCRLine] = None
    best_score = -1.0

    norm_targets = [
        (
            t,
            normalize_text(t),
        )
        for t in targets
        if str(t).strip()
    ]

    for line in lines:
        ln = normalize_text(
            line.text
        )

        if not ln:
            continue

        for raw, tn in norm_targets:
            if not tn:
                continue

            min_line_len = max(
                4,
                min(
                    10,
                    int(
                        len(tn)
                        * 0.45
                    ),
                ),
            )

            if len(ln) < min_line_len:
                continue

            if tn in ln:
                score = 100.0

            else:
                score = max(
                    fuzz.partial_ratio(
                        tn,
                        ln,
                    ),
                    fuzz.ratio(
                        tn,
                        ln,
                    ),
                )

            if score > best_score:
                best_score = float(
                    score
                )

                best = OCRLine(
                    line.text,
                    line.left,
                    line.top,
                    line.width,
                    line.height,
                    float(score),
                )

    if (
        best is not None
        and best_score >= threshold
    ):
        return best

    return None


def find_text_span(
    image: Image.Image,
    targets: Sequence[str],
    lang: str = "eng",
    threshold: float = 70,
    psm: int = 6,
) -> Optional[OCRLine]:
    """
    Localiza a menor sequência de tokens OCR
    que melhor representa o texto-alvo.

    Diferente de usar o centro da linha inteira,
    isto permite clicar exatamente em abas como
    "Imagens de Campo" mesmo quando o OCR juntou
    várias abas na mesma linha.
    """

    df = _image_to_data(
        image,
        lang=lang,
        psm=psm,
    )

    if df.empty:
        return None

    norm_targets = [
        (
            str(t),
            normalize_text(
                str(t)
            ),
        )
        for t in targets
        if str(t).strip()
    ]

    best = None

    best_rank = (
        -1.0,
        -(10**9),
    )

    keys = [
        "block_num",
        "par_num",
        "line_num",
    ]

    for _, grp in df.groupby(
        keys,
        sort=False,
    ):

        grp = (
            grp
            .sort_values(
                [
                    "left",
                    "top",
                ]
            )
            .reset_index(
                drop=True
            )
        )

        rows = [
            r
            for _, r in grp.iterrows()
            if str(
                r.get(
                    "text",
                    "",
                )
            ).strip()
        ]

        n = len(rows)

        for i in range(n):

            # A maior parte dos alvos
            # cabe em até 5 tokens.
            # 7 cobre OCR fragmentado.
            for j in range(
                i,
                min(
                    n,
                    i + 7,
                ),
            ):

                chunk = rows[
                    i : j + 1
                ]

                text = " ".join(
                    str(
                        r["text"]
                    ).strip()
                    for r in chunk
                )

                cn = normalize_text(
                    text
                )

                if not cn:
                    continue

                left = int(
                    min(
                        int(
                            r["left"]
                        )
                        for r in chunk
                    )
                )

                top = int(
                    min(
                        int(
                            r["top"]
                        )
                        for r in chunk
                    )
                )

                right = int(
                    max(
                        int(
                            r["left"]
                        )
                        + int(
                            r["width"]
                        )
                        for r in chunk
                    )
                )

                bottom = int(
                    max(
                        int(
                            r["top"]
                        )
                        + int(
                            r["height"]
                        )
                        for r in chunk
                    )
                )

                for raw, tn in norm_targets:

                    if not tn:
                        continue

                    min_len = max(
                        3,
                        min(
                            8,
                            int(
                                len(tn)
                                * 0.35
                            ),
                        ),
                    )

                    if len(cn) < min_len:
                        continue

                    if tn in cn:
                        score = 100.0

                    else:
                        score = max(
                            fuzz.ratio(
                                tn,
                                cn,
                            ),
                            fuzz.partial_ratio(
                                tn,
                                cn,
                            ),
                        )

                    if score < threshold:
                        continue

                    # Em empate,
                    # prefere trecho menor/
                    # mais específico.
                    specificity = -abs(
                        len(cn)
                        - len(tn)
                    )

                    rank = (
                        float(score),
                        specificity,
                    )

                    if rank > best_rank:

                        best_rank = rank

                        best = OCRLine(
                            text,
                            left,
                            top,
                            right - left,
                            bottom - top,
                            float(score),
                        )

    return best


def locate_text_on_screen(
    targets: Sequence[str],
    region_norm: Iterable[float],
    *,
    lang: str = "eng",
    threshold: float = 70,
    psm: int = 6,
    window_title: str = "",
) -> tuple[
    Optional[OCRLine],
    Image.Image,
    tuple[int, int, int, int],
]:

    image, region_abs = (
        screenshot_norm_region(
            region_norm,
            window_title=window_title,
        )
    )

    found = find_text_span(
        image,
        targets,
        lang=lang,
        threshold=threshold,
        psm=psm,
    )

    if found:
        found.left += region_abs[0]
        found.top += region_abs[1]

    return (
        found,
        image,
        region_abs,
    )


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

        found, _, _ = (
            locate_text_on_screen(
                targets,
                region_norm,
                lang=lang,
                threshold=threshold,
                psm=6,
                window_title=window_title,
            )
        )

        if found:
            return found

        time.sleep(
            poll_interval
        )

    return None


def click_text(
    targets: Sequence[str],
    region_norm: Iterable[float],
    *,
    lang: str,
    threshold: float,
    click_offset: tuple[int, int] = (
        0,
        0,
    ),
    window_title: str = "",
) -> tuple[bool, str]:

    found, _, _ = (
        locate_text_on_screen(
            targets,
            region_norm,
            lang=lang,
            threshold=threshold,
            psm=6,
            window_title=window_title,
        )
    )

    if not found:
        return (
            False,
            "",
        )

    import pyautogui

    x, y = found.center

    pyautogui.click(
        x + click_offset[0],
        y + click_offset[1],
    )

    return (
        True,
        found.text,
    )


def _normalize_ocr_coord_text(
    text: str,
) -> str:

    s = str(
        text or ""
    )

    s = (
        s
        .replace(
            "−",
            "-",
        )
        .replace(
            "–",
            "-",
        )
        .replace(
            "—",
            "-",
        )
    )

    s = s.replace(
        ";",
        ",",
    )

    return s


def _find_coordinate_pairs(
    text: str,
) -> list[
    tuple[
        float,
        float,
        str,
    ]
]:

    s = _normalize_ocr_coord_text(
        text
    )

    patterns = [
        (
            r"(-?\d{1,2}[\.,]\d{3,8})"
            r"\s*[, ]\s*"
            r"(-?\d{2,3}[\.,]\d{3,8})"
        ),
        (
            r"(-?\d{1,2}[\.,]\d{3,8})"
            r"\s+"
            r"(-?\d{2,3}[\.,]\d{3,8})"
        ),
    ]

    out: list[
        tuple[
            float,
            float,
            str,
        ]
    ] = []

    for pattern in patterns:

        for m in re.finditer(
            pattern,
            s,
        ):
            a = (
                m.group(1)
                .replace(
                    ",",
                    ".",
                )
            )

            b = (
                m.group(2)
                .replace(
                    ",",
                    ".",
                )
            )

            try:
                lat = float(a)
                lon = float(b)

            except ValueError:
                continue

            if (
                -90 <= lat <= 90
                and
                -180 <= lon <= 180
            ):
                out.append(
                    (
                        lat,
                        lon,
                        m.group(0),
                    )
                )

    # Remove duplicatas
    # preservando ordem.
    seen = set()
    unique = []

    for item in out:

        key = (
            round(
                item[0],
                8,
            ),
            round(
                item[1],
                8,
            ),
        )

        if key not in seen:
            seen.add(key)
            unique.append(item)

    return unique


def _pick_best_coordinate(
    candidates: Sequence[
        tuple[
            float,
            float,
            str,
        ]
    ],
) -> Optional[
    tuple[
        float,
        float,
        str,
    ]
]:

    if not candidates:
        return None

    # Prioriza coordenadas
    # plausíveis para Brasil.
    br = [
        c
        for c in candidates
        if (
            -35.5 <= c[0] <= 6.5
            and
            -75.5 <= c[1] <= -30.0
        )
    ]

    pool = (
        br
        or list(candidates)
    )

    return (
        pool[0]
        if pool
        else None
    )


def preprocess_footer_variants(
    photo: Image.Image,
) -> list[
    tuple[
        str,
        np.ndarray,
    ]
]:

    arr = np.array(
        photo.convert(
            "RGB"
        )
    )

    h, w = arr.shape[:2]

    # O carimbo aparece no rodapé;
    # pega faixa ampla
    # e privilegia metade direita.
    footer = arr[
        max(
            0,
            int(
                h * 0.78
            ),
        ) : h,
        max(
            0,
            int(
                w * 0.32
            ),
        ) : w,
    ]

    gray = cv2.cvtColor(
        footer,
        cv2.COLOR_RGB2GRAY,
    )

    up = cv2.resize(
        gray,
        None,
        fx=4.0,
        fy=4.0,
        interpolation=cv2.INTER_CUBIC,
    )

    clahe = (
        cv2.createCLAHE(
            clipLimit=2.5,
            tileGridSize=(8, 8),
        )
        .apply(up)
    )

    variants: list[
        tuple[
            str,
            np.ndarray,
        ]
    ] = [
        (
            "gray",
            up,
        ),
        (
            "clahe",
            clahe,
        ),
    ]

    for th in (
        160,
        180,
        200,
        220,
    ):

        _, bw = cv2.threshold(
            clahe,
            th,
            255,
            cv2.THRESH_BINARY,
        )

        variants.append(
            (
                f"bw_{th}",
                bw,
            )
        )

        variants.append(
            (
                f"inv_{th}",
                255 - bw,
            )
        )

    return variants


def extract_coordinates_from_photo(
    photo: Image.Image,
    lang: str = "eng",
    prefer_negative_lat: bool = True,
) -> dict:

    attempts = []

    all_candidates: list[
        tuple[
            float,
            float,
            str,
            str,
        ]
    ] = []

    for name, variant in (
        preprocess_footer_variants(
            photo
        )
    ):

        try:
            text = (
                pytesseract
                .image_to_string(
                    variant,
                    lang=lang,
                    config=(
                        "--psm 6 "
                        "-c "
                        "tessedit_char_whitelist="
                        "0123456789-.,:()/UTC "
                    ),
                )
            )

        except pytesseract.TesseractError:
            text = (
                pytesseract
                .image_to_string(
                    variant,
                    lang="eng",
                    config=(
                        "--psm 6 "
                        "-c "
                        "tessedit_char_whitelist="
                        "0123456789-.,:()/UTC "
                    ),
                )
            )

        candidates = (
            _find_coordinate_pairs(
                text
            )
        )

        attempts.append(
            {
                "variant": name,
                "text": text,
                "candidates": candidates,
            }
        )

        for (
            lat,
            lon,
            matched,
        ) in candidates:

            all_candidates.append(
                (
                    lat,
                    lon,
                    matched,
                    name,
                )
            )

    if all_candidates:

        # Votação entre variantes.
        from collections import Counter

        br_candidates = [
            c
            for c in all_candidates
            if (
                -35.5 <= c[0] <= 6.5
                and
                -75.5 <= c[1] <= -30.0
            )
        ]

        pool = (
            br_candidates
            or all_candidates
        )

        keys = [
            (
                round(
                    c[0],
                    5,
                ),
                round(
                    c[1],
                    5,
                ),
            )
            for c in pool
        ]

        counts = Counter(
            keys
        )

        best_key, _ = (
            counts
            .most_common(1)[0]
        )

        members = [
            c
            for c, key
            in zip(
                pool,
                keys,
            )
            if key == best_key
        ]

        best = members[0]

        lat, lon, matched, name = best

        out_lat = float(
            best_key[0]
        )

        out_lon = float(
            best_key[1]
        )

        # Fotos deste fluxo são do Maranhão.
        # O sinal negativo da latitude
        # pode ser perdido pelo OCR.
        if (
            prefer_negative_lat
            and
            0 < out_lat <= 12.0
            and
            -75.5 <= out_lon <= -30.0
        ):
            out_lat = -out_lat

        return {
            "latitude":
                out_lat,

            "longitude":
                out_lon,

            "matched_text":
                matched,

            "ocr_text":
                "\n---\n".join(
                    (
                        f"[{a['variant']}] "
                        f"{a['text'].strip()}"
                    )
                    for a in attempts
                    if a["text"].strip()
                ),

            "variant":
                f"consenso:{name}",

            "attempts":
                attempts,
        }

    return {
        "latitude":
            None,

        "longitude":
            None,

        "matched_text":
            "",

        "ocr_text":
            "\n---\n".join(
                (
                    f"[{a['variant']}] "
                    f"{a['text'].strip()}"
                )
                for a in attempts
                if a["text"].strip()
            ),

        "variant":
            "",

        "attempts":
            attempts,
    }


def extract_largest_photo_from_screen(
    screenshot: Image.Image,
    region_norm: Iterable[float],
    *,
    min_area_ratio: float = 0.02,
) -> tuple[
    Image.Image,
    tuple[int, int, int, int],
    bool,
]:
    """
    Recorta a maior área não branca
    dentro da região do navegador.

    Retorna:

        imagem,
        bbox,
        encontrou_recorte

    Se não houver contorno confiável,
    retorna a própria região configurada.
    """

    sw, sh = screenshot.size

    x, y, rw, rh = norm_region_to_abs(
        region_norm,
        (sw, sh),
    )

    area = np.array(
        screenshot
        .crop(
            (
                x,
                y,
                x + rw,
                y + rh,
            )
        )
        .convert("RGB")
    )

    gray = cv2.cvtColor(
        area,
        cv2.COLOR_RGB2GRAY,
    )

    # Fundo do navegador é quase branco.
    # Inverte para transformar
    # foto em primeiro plano.
    mask = (
        gray < 245
    ).astype(
        np.uint8
    ) * 255

    open_kernel = (
        cv2.getStructuringElement(
            cv2.MORPH_RECT,
            (3, 3),
        )
    )

    mask = cv2.morphologyEx(
        mask,
        cv2.MORPH_OPEN,
        open_kernel,
        iterations=1,
    )

    close_kernel = (
        cv2.getStructuringElement(
            cv2.MORPH_RECT,
            (3, 3),
        )
    )

    mask = cv2.morphologyEx(
        mask,
        cv2.MORPH_CLOSE,
        close_kernel,
        iterations=1,
    )

    contours, _ = (
        cv2.findContours(
            mask,
            cv2.RETR_EXTERNAL,
            cv2.CHAIN_APPROX_SIMPLE,
        )
    )

    candidates = []

    min_area = max(
        2500,
        int(
            rw
            * rh
            * min_area_ratio
        ),
    )

    for cnt in contours:

        bx, by, bw, bh = (
            cv2.boundingRect(cnt)
        )

        area_px = (
            bw * bh
        )

        if area_px < min_area:
            continue

        if (
            bw < 120
            or
            bh < 160
        ):
            continue

        candidates.append(
            (
                area_px,
                bx,
                by,
                bw,
                bh,
            )
        )

    if not candidates:

        fallback = screenshot.crop(
            (
                x,
                y,
                x + rw,
                y + rh,
            )
        )

        return (
            fallback,
            (
                x,
                y,
                rw,
                rh,
            ),
            False,
        )

    (
        _,
        bx,
        by,
        bw,
        bh,
    ) = max(
        candidates,
        key=lambda t: t[0],
    )

    margin = 1

    bx2 = max(
        0,
        bx + margin,
    )

    by2 = max(
        0,
        by + margin,
    )

    ex = min(
        rw,
        bx + bw - margin,
    )

    ey = min(
        rh,
        by + bh - margin,
    )

    crop = Image.fromarray(
        area[
            by2:ey,
            bx2:ex,
        ]
    )

    return (
        crop,
        (
            x + bx2,
            y + by2,
            ex - bx2,
            ey - by2,
        ),
        True,
    )


def save_debug_image(
    image: Image.Image,
    path: Path | str,
) -> None:

    path = Path(path)

    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    image.save(path)
