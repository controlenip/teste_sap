from __future__ import annotations

import time
from typing import Optional

import pygetwindow as gw


def all_window_titles() -> list[str]:
    titles = []
    for w in gw.getAllWindows():
        title = (getattr(w, "title", "") or "").strip()
        if title:
            titles.append(title)
    return titles


def find_window_contains(text: str):
    needle = (text or "").strip().lower()
    if not needle:
        return None
    for w in gw.getAllWindows():
        title = (getattr(w, "title", "") or "").strip()
        if needle in title.lower():
            return w
    return None


def activate_window_contains(text: str, wait: float = 0.5):
    w = find_window_contains(text)
    if w is None:
        raise RuntimeError(f"Janela contendo '{text}' não foi encontrada.")
    try:
        if getattr(w, "isMinimized", False):
            w.restore()
            time.sleep(0.2)
        w.activate()
        time.sleep(wait)
    except Exception as exc:
        raise RuntimeError(f"Não foi possível ativar a Área Remota: {exc}") from exc
    return w


def active_window_title() -> str:
    try:
        w = gw.getActiveWindow()
        return (w.title or "") if w else ""
    except Exception:
        return ""
