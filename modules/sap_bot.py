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
from .word_report import build_word_report, default_report_path
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
