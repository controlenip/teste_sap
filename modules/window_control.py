from __future__ import annotations

import ctypes
import time
from typing import Iterable, Optional, Tuple

import pyautogui
import pygetwindow as gw


SW_RESTORE = 9
SW_SHOW = 5
SW_MAXIMIZE = 3
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_SHOWWINDOW = 0x0040
HWND_TOP = 0


def _window_title(window) -> str:
    try:
        return str(getattr(window, "title", "") or "").strip()
    except Exception:
        return ""


def _window_hwnd(window) -> Optional[int]:
    try:
        hwnd = getattr(window, "_hWnd", None)
        return int(hwnd) if hwnd else None
    except Exception:
        return None


def _window_area(window) -> int:
    try:
        width = max(0, int(getattr(window, "width", 0)))
        height = max(0, int(getattr(window, "height", 0)))
        return width * height
    except Exception:
        return 0


def _active_hwnd() -> Optional[int]:
    try:
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        return int(hwnd) if hwnd else None
    except Exception:
        return None


def _is_window_foreground(window) -> bool:
    hwnd = _window_hwnd(window)
    if hwnd and _active_hwnd() == hwnd:
        return True

    try:
        active = gw.getActiveWindow()
        if active is None:
            return False
        active_hwnd = _window_hwnd(active)
        if hwnd and active_hwnd:
            return hwnd == active_hwnd
        return _window_title(active) == _window_title(window)
    except Exception:
        return False


def all_window_titles() -> list[str]:
    titles: list[str] = []
    try:
        windows = gw.getAllWindows()
    except Exception:
        windows = []

    for window in windows:
        title = _window_title(window)
        if title:
            titles.append(title)
    return titles


def find_window_contains(text: str):
    needle = str(text or "").strip().lower()
    if not needle:
        return None

    try:
        windows = gw.getAllWindows()
    except Exception:
        windows = []

    candidates = []
    for window in windows:
        title = _window_title(window)
        if title and needle in title.lower():
            candidates.append(window)

    if not candidates:
        return None

    # Se houver mais de uma janela com o mesmo texto, a maior tende a ser o RDP principal.
    candidates.sort(key=_window_area, reverse=True)
    return candidates[0]


def _restore_window_native(window) -> None:
    hwnd = _window_hwnd(window)
    if not hwnd:
        return
    try:
        user32 = ctypes.windll.user32
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
        else:
            user32.ShowWindow(hwnd, SW_SHOW)
    except Exception:
        pass


def _activate_native(window) -> bool:
    hwnd = _window_hwnd(window)
    if not hwnd:
        return False

    try:
        user32 = ctypes.windll.user32
        user32.ShowWindow(hwnd, SW_RESTORE)
        user32.SetWindowPos(
            hwnd,
            HWND_TOP,
            0,
            0,
            0,
            0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW,
        )
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        time.sleep(0.25)
        return _is_window_foreground(window)
    except Exception:
        return False


def _activate_pygetwindow(window) -> bool:
    try:
        if getattr(window, "isMinimized", False):
            try:
                window.restore()
                time.sleep(0.2)
            except Exception:
                pass

        try:
            window.activate()
        except Exception as exc:
            # pygetwindow pode lançar este erro mesmo quando o Windows retorna sucesso.
            message = str(exc).lower()
            harmless = (
                "error code from windows: 0",
                "a operação foi concluída com êxito",
                "a operacao foi concluida com exito",
                "the operation completed successfully",
            )
            if not any(token in message for token in harmless):
                pass

        time.sleep(0.3)
        return _is_window_foreground(window)
    except Exception:
        return False


def _activate_by_click(window) -> bool:
    try:
        left = int(getattr(window, "left", 0))
        top = int(getattr(window, "top", 0))
        width = int(getattr(window, "width", 0))
        height = int(getattr(window, "height", 0))
        if width <= 0 or height <= 0:
            return False

        # Clica na região central da barra superior, longe dos botões de janela.
        x = max(1, left + width // 2)
        y = max(1, top + 10)
        pyautogui.click(x, y)
        time.sleep(0.4)
        return _is_window_foreground(window)
    except Exception:
        return False


def activate_window_contains(text: str, wait: float = 0.5, timeout: float = 10.0):
    needle = str(text or "").strip()
    if not needle:
        raise RuntimeError("O título configurado para a Área Remota está vazio.")

    end = time.time() + max(0.5, float(timeout))
    window = None
    while time.time() < end:
        window = find_window_contains(needle)
        if window is not None:
            break
        time.sleep(0.35)

    if window is None:
        sample = "\n".join(f"- {t}" for t in all_window_titles()[:15]) or "- Nenhuma janela detectada"
        raise RuntimeError(
            f"Área Remota não encontrada. Procurado: '{needle}'.\n"
            f"Janelas detectadas:\n{sample}"
        )

    if _is_window_foreground(window):
        time.sleep(max(0.0, wait))
        return window

    try:
        if getattr(window, "isMinimized", False):
            window.restore()
            time.sleep(0.2)
    except Exception:
        pass

    _restore_window_native(window)

    if _activate_native(window) or _activate_pygetwindow(window) or _activate_by_click(window):
        time.sleep(max(0.0, wait))
        return window

    # O Windows pode não permitir confirmar programaticamente o foco, embora a janela
    # já esteja utilizável. Faz uma tentativa final de clique no interior da janela.
    try:
        left, top, width, height = get_window_region_contains(needle, content_only=False)
        pyautogui.click(left + min(max(width // 2, 20), max(20, width - 20)), top + min(35, max(10, height - 10)))
        time.sleep(0.35)
        return window
    except Exception as exc:
        raise RuntimeError(f"Não foi possível ativar a Área Remota: {exc}") from exc



def maximize_window_contains(text: str, wait: float = 1.2, timeout: float = 5.0):
    """Maximiza a janela local do RDP antes da automacao.

    Isto e importante porque o SAP muda de escala quando o RDP fica em meia tela.
    Os pontos calibrados de Dados de Campo 2 e Imagens de Campo foram obtidos com
    a sessao remota maximizada. Ao maximizar primeiro, os cliques ficam previsiveis
    e o OCR dos links ganha resolucao suficiente para ler os nomes dos JPGs.
    """
    window = find_window_contains(text)
    if window is None:
        raise RuntimeError(f"Janela contendo '{text}' nao foi encontrada.")

    # Primeiro garante foco/restauracao.
    try:
        activate_window_contains(text, wait=0.15, timeout=timeout)
    except Exception:
        pass

    hwnd = _window_hwnd(window)
    maximized = False

    if hwnd:
        try:
            ctypes.windll.user32.ShowWindow(hwnd, SW_MAXIMIZE)
            ctypes.windll.user32.SetForegroundWindow(hwnd)
            maximized = True
        except Exception:
            pass

    if not maximized:
        try:
            window.maximize()
            maximized = True
        except Exception:
            pass

    # Aguarda a geometria estabilizar. Nao exige tamanho exato porque a barra de
    # tarefas e a moldura do RDP podem reduzir alguns pixels.
    end = time.time() + max(1.0, float(timeout))
    screen_w, screen_h = pyautogui.size()
    while time.time() < end:
        current = find_window_contains(text)
        if current is not None:
            try:
                if int(current.width) >= int(screen_w * 0.80) and int(current.height) >= int(screen_h * 0.75):
                    window = current
                    break
            except Exception:
                pass
        time.sleep(0.15)

    time.sleep(max(0.0, float(wait)))
    return find_window_contains(text) or window

def active_window_title() -> str:
    try:
        window = gw.getActiveWindow()
        return _window_title(window) if window else ""
    except Exception:
        return ""


def get_window_region_contains(text: str, content_only: bool = False) -> Tuple[int, int, int, int]:
    """Retorna (left, top, width, height) da janela local que contém ``text``.

    Por padrão usa a janela inteira. Isso mantém compatibilidade com os pontos e
    regiões normalizados que foram calibrados a partir dos prints do RDP.
    """
    window = find_window_contains(text)
    if window is None:
        raise RuntimeError(f"Janela contendo '{text}' não foi encontrada.")

    left = int(getattr(window, "left", 0))
    top = int(getattr(window, "top", 0))
    width = int(getattr(window, "width", 0))
    height = int(getattr(window, "height", 0))

    if width <= 0 or height <= 0:
        raise RuntimeError("A janela da Área Remota possui tamanho inválido.")

    if content_only:
        # Margens conservadoras para remover a moldura local do RDP quando desejado.
        margin_left = 2
        margin_top = 30
        margin_right = 2
        margin_bottom = 2
        left += margin_left
        top += margin_top
        width = max(1, width - margin_left - margin_right)
        height = max(1, height - margin_top - margin_bottom)

    return left, top, width, height


def _clamp_region_to_screen(region: Tuple[int, int, int, int]) -> Tuple[int, int, int, int]:
    left, top, width, height = region
    sw, sh = pyautogui.size()
    x1 = max(0, min(int(left), sw - 1))
    y1 = max(0, min(int(top), sh - 1))
    x2 = max(x1 + 1, min(int(left + width), sw))
    y2 = max(y1 + 1, min(int(top + height), sh))
    return x1, y1, x2 - x1, y2 - y1


def screenshot_window_contains(text: str, content_only: bool = False):
    """Captura somente a janela RDP identificada pelo título."""
    region = get_window_region_contains(text, content_only=content_only)
    region = _clamp_region_to_screen(region)
    return pyautogui.screenshot(region=region), region


def norm_point_in_window_to_abs(
    text: str,
    point: Iterable[float],
    *,
    content_only: bool = False,
) -> Tuple[int, int]:
    """Converte ponto normalizado da janela RDP em coordenada absoluta do monitor."""
    px, py = [float(v) for v in point]
    left, top, width, height = get_window_region_contains(text, content_only=content_only)
    x = left + int(round(px * width))
    y = top + int(round(py * height))
    return x, y


def norm_region_in_window_to_abs(
    text: str,
    region: Iterable[float],
    *,
    content_only: bool = False,
) -> Tuple[int, int, int, int]:
    """Converte região normalizada do RDP em região absoluta do monitor."""
    rx, ry, rw, rh = [float(v) for v in region]
    left, top, width, height = get_window_region_contains(text, content_only=content_only)
    abs_region = (
        left + int(round(rx * width)),
        top + int(round(ry * height)),
        max(1, int(round(rw * width))),
        max(1, int(round(rh * height))),
    )
    return _clamp_region_to_screen(abs_region)


def local_to_screen(x: int, y: int, region: Tuple[int, int, int, int]) -> Tuple[int, int]:
    """Converte coordenada relativa a uma captura em coordenada absoluta do monitor."""
    left, top, _, _ = region
    return left + int(x), top + int(y)


def window_exists_contains(text: str) -> bool:
    return find_window_contains(text) is not None


def debug_windows() -> dict:
    return {
        "active_window": active_window_title(),
        "windows": all_window_titles(),
    }
