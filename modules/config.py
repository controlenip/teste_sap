from __future__ import annotations

import json
import os
import shutil
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Iterable, Tuple


def _runtime_root() -> Path:
    """Pasta gravável usada por configuração, saídas e logs.

    No EXE, evita gravar dentro do diretório temporário do PyInstaller.
    """
    if getattr(sys, "frozen", False):
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home())
        root = base / "SAP_Fotos"
        root.mkdir(parents=True, exist_ok=True)
        return root
    return Path(__file__).resolve().parent.parent


def _bundle_root() -> Path:
    """Pasta onde os recursos empacotados pelo PyInstaller são extraídos."""
    if hasattr(sys, "_MEIPASS"):
        return Path(getattr(sys, "_MEIPASS"))
    return Path(__file__).resolve().parent.parent


APP_DIR = _runtime_root()
BUNDLE_DIR = _bundle_root()
CONFIG_PATH = APP_DIR / "config.json"

DEFAULT_CONFIG: Dict[str, Any] = {
    "remote": {
        "window_title_contains": "10.30.255.10",
        "activate_before_each_step": True,
    },
    "ocr": {
        "tesseract_cmd": "",
        "lang": "eng",
        "fuzzy_threshold": 70,
        "save_debug_images": True,
        "prefer_negative_latitude": True,
    },
    "timing": {
        "after_note_enter": 2.5,
        "after_tab_click": 1.2,
        "after_link_click": 1.0,
        "after_permit": 2.5,
        "after_maximize": 1.0,
        "after_close_photo": 1.2,
        "poll_interval": 0.45,
        "screen_timeout": 18.0,
    },
    "automation": {
        "typing_interval": 0.035,
        "scroll_pages_links": 5,
        "scroll_amount": -6,
        "maximize_photo_window": True,
        "close_photo_with_alt_f4": True,
        "continue_when_photo_missing": True,
        "continue_when_coordinate_missing": True,
        "targets": [
            {
                "key": "FACHADAIMOVEL",
                "output_suffix": "FACHADAIMOVEL",
                "aliases": [
                    "FACHADAIMOVEL",
                    "FACHADADOIMOVEL",
                    "FACHADA DO IMOVEL",
                    "FACHADA",
                ],
            },
            {
                "key": "ADESIVOLIGACAONOVA",
                "output_suffix": "ADESIVOLIGACAONOVA",
                "aliases": [
                    "ADESIVOLIGACAONOVA",
                    "ADESIVO LIGACAO NOVA",
                    "ADESIVO",
                ],
            },
            {
                "key": "FOTOPANORAMICA",
                "output_suffix": "FOTOPANORAMICA",
                "aliases": [
                    "FOTOPANORAMICA",
                    "FOTO PANORAMICA",
                    "PANORAMICA",
                ],
            },
        ],
    },
    "output": {
        "root_folder": "saida",
        "create_zip": True,
        "create_consolidated_excel": True,
    },
    "points": {
        "note_field_initial": [0.125, 0.192],
        "note_field_detail": [0.099, 0.143],
        "dados_campo_2_fallback": [0.329, 0.198],
        "imagens_campo_fallback": [0.274, 0.243],
        "permitir_fallback": [0.359, 0.550],
    },
    "regions": {
        "top_detection": [0.0, 0.04, 0.72, 0.25],
        "tabs": [0.0, 0.14, 0.76, 0.16],
        "links": [0.015, 0.255, 0.64, 0.48],
        "security_popup": [0.28, 0.27, 0.48, 0.40],
        "photo_content": [0.0, 0.045, 1.0, 0.95],
    },
}


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(path: Path | str = CONFIG_PATH) -> Dict[str, Any]:
    path = Path(path)
    if not path.exists():
        save_config(DEFAULT_CONFIG, path)
        return deepcopy(DEFAULT_CONFIG)
    try:
        user_cfg = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        backup = path.with_suffix(".json.bak")
        try:
            shutil.copy2(path, backup)
        except Exception:
            pass
        save_config(DEFAULT_CONFIG, path)
        return deepcopy(DEFAULT_CONFIG)
    return _deep_merge(DEFAULT_CONFIG, user_cfg)


def save_config(cfg: Dict[str, Any], path: Path | str = CONFIG_PATH) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def find_tesseract(explicit: str = "") -> str:
    """Localiza primeiro o OCR embutido no EXE; depois instalações externas."""
    candidates: list[str] = []
    if explicit:
        candidates.append(explicit)

    # Build one-file: recursos extraídos em sys._MEIPASS/tesseract.
    candidates += [
        str(BUNDLE_DIR / "tesseract" / "tesseract.exe"),
        str(APP_DIR / "tesseract" / "tesseract.exe"),
    ]

    which = shutil.which("tesseract")
    if which:
        candidates.append(which)

    candidates += [
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        str(Path.home() / "AppData" / "Local" / "Programs" / "Tesseract-OCR" / "tesseract.exe"),
    ]

    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return str(Path(candidate))
    return ""


def norm_point_to_abs(point: Iterable[float], screen_size: Tuple[int, int]) -> Tuple[int, int]:
    x, y = [float(v) for v in point]
    w, h = screen_size
    return int(round(x * w)), int(round(y * h))


def abs_point_to_norm(point: Tuple[int, int], screen_size: Tuple[int, int]) -> list[float]:
    x, y = point
    w, h = screen_size
    return [round(x / max(1, w), 6), round(y / max(1, h), 6)]


def norm_region_to_abs(region: Iterable[float], screen_size: Tuple[int, int]) -> Tuple[int, int, int, int]:
    x, y, rw, rh = [float(v) for v in region]
    sw, sh = screen_size
    return (
        int(round(x * sw)),
        int(round(y * sh)),
        max(1, int(round(rw * sw))),
        max(1, int(round(rh * sh))),
    )


def region_from_two_points_norm(p1: Iterable[float], p2: Iterable[float]) -> list[float]:
    x1, y1 = [float(v) for v in p1]
    x2, y2 = [float(v) for v in p2]
    left, top = min(x1, x2), min(y1, y2)
    right, bottom = max(x1, x2), max(y1, y2)
    return [left, top, max(0.001, right - left), max(0.001, bottom - top)]


def resolve_output_root(cfg: Dict[str, Any]) -> Path:
    raw = str(cfg.get("output", {}).get("root_folder", "saida")).strip() or "saida"
    p = Path(raw)
    if not p.is_absolute():
        p = APP_DIR / p
    p.mkdir(parents=True, exist_ok=True)
    return p
