from __future__ import annotations

import ctypes
import time
from typing import Optional

import pyautogui
import pygetwindow as gw


# ============================================================
# CONSTANTES DA API DO WINDOWS
# ============================================================

SW_RESTORE = 9
SW_SHOW = 5

SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_SHOWWINDOW = 0x0040

HWND_TOP = 0


# ============================================================
# FUNÇÕES AUXILIARES
# ============================================================

def _window_title(window) -> str:
    """
    Retorna o título de uma janela com segurança.
    """
    try:
        return str(getattr(window, "title", "") or "").strip()
    except Exception:
        return ""


def _window_hwnd(window) -> Optional[int]:
    """
    Obtém o identificador nativo HWND da janela.
    """
    try:
        hwnd = getattr(window, "_hWnd", None)

        if hwnd:
            return int(hwnd)

    except Exception:
        pass

    return None


def _window_area(window) -> int:
    """
    Calcula a área aproximada da janela.

    É usada para preferir a janela principal do RDP em vez
    de alguma janela auxiliar com título semelhante.
    """
    try:
        width = max(0, int(getattr(window, "width", 0)))
        height = max(0, int(getattr(window, "height", 0)))

        return width * height

    except Exception:
        return 0


def _is_same_window(window_a, window_b) -> bool:
    """
    Compara duas janelas preferencialmente pelo HWND.
    """
    if window_a is None or window_b is None:
        return False

    hwnd_a = _window_hwnd(window_a)
    hwnd_b = _window_hwnd(window_b)

    if hwnd_a and hwnd_b:
        return hwnd_a == hwnd_b

    try:
        return _window_title(window_a) == _window_title(window_b)
    except Exception:
        return False


def _active_hwnd() -> Optional[int]:
    """
    Obtém o HWND atualmente em primeiro plano.
    """
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()

        if hwnd:
            return int(hwnd)

    except Exception:
        pass

    return None


def _is_window_foreground(window) -> bool:
    """
    Verifica se a janela informada está atualmente em primeiro plano.
    """
    target_hwnd = _window_hwnd(window)

    if target_hwnd:
        foreground_hwnd = _active_hwnd()

        if foreground_hwnd == target_hwnd:
            return True

    try:
        active = gw.getActiveWindow()

        if _is_same_window(window, active):
            return True

    except Exception:
        pass

    return False


# ============================================================
# LISTAGEM E LOCALIZAÇÃO DE JANELAS
# ============================================================

def all_window_titles() -> list[str]:
    """
    Retorna os títulos de todas as janelas visíveis para o Windows.
    """
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
    """
    Procura uma janela cujo título contenha determinado texto.

    Quando várias janelas forem encontradas, prefere a de maior área,
    o que normalmente corresponde à janela principal da Área Remota.
    """
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

        if not title:
            continue

        if needle in title.lower():
            candidates.append(window)

    if not candidates:
        return None

    candidates.sort(
        key=_window_area,
        reverse=True
    )

    return candidates[0]


# ============================================================
# CONTROLE NATIVO DO WINDOWS
# ============================================================

def _restore_window_native(window) -> None:
    """
    Restaura/exibe a janela usando a API nativa do Windows.
    """
    hwnd = _window_hwnd(window)

    if not hwnd:
        return

    try:
        user32 = ctypes.windll.user32

        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
        else:
            user32.ShowWindow(hwnd, SW_SHOW)

        time.sleep(0.15)

    except Exception:
        pass


def _activate_native_simple(window) -> bool:
    """
    Primeira tentativa para trazer a janela para frente utilizando
    apenas chamadas simples da API do Windows.
    """
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
            SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW
        )

        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)

        time.sleep(0.30)

        return _is_window_foreground(window)

    except Exception:
        return False


def _activate_native_attached(window) -> bool:
    """
    Segunda tentativa.

    O Windows pode impedir que um processo diferente assuma o foco.
    AttachThreadInput permite temporariamente compartilhar o contexto
    de entrada entre as threads para tentar trazer a janela do RDP
    para primeiro plano.
    """
    hwnd = _window_hwnd(window)

    if not hwnd:
        return False

    try:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        foreground_hwnd = user32.GetForegroundWindow()

        current_thread = kernel32.GetCurrentThreadId()

        foreground_thread = 0
        target_thread = 0

        if foreground_hwnd:
            foreground_thread = user32.GetWindowThreadProcessId(
                foreground_hwnd,
                None
            )

        target_thread = user32.GetWindowThreadProcessId(
            hwnd,
            None
        )

        attached_foreground = False
        attached_target = False

        try:
            if (
                foreground_thread
                and foreground_thread != current_thread
            ):
                attached_foreground = bool(
                    user32.AttachThreadInput(
                        current_thread,
                        foreground_thread,
                        True
                    )
                )

            if (
                target_thread
                and target_thread != current_thread
            ):
                attached_target = bool(
                    user32.AttachThreadInput(
                        current_thread,
                        target_thread,
                        True
                    )
                )

            user32.ShowWindow(hwnd, SW_RESTORE)

            user32.BringWindowToTop(hwnd)

            user32.SetWindowPos(
                hwnd,
                HWND_TOP,
                0,
                0,
                0,
                0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW
            )

            user32.SetForegroundWindow(hwnd)

            try:
                user32.SetFocus(hwnd)
            except Exception:
                pass

            time.sleep(0.35)

        finally:
            if attached_target:
                try:
                    user32.AttachThreadInput(
                        current_thread,
                        target_thread,
                        False
                    )
                except Exception:
                    pass

            if attached_foreground:
                try:
                    user32.AttachThreadInput(
                        current_thread,
                        foreground_thread,
                        False
                    )
                except Exception:
                    pass

        return _is_window_foreground(window)

    except Exception:
        return False


# ============================================================
# FALLBACK COM PYGETWINDOW
# ============================================================

def _activate_pygetwindow(window) -> bool:
    """
    Tenta ativar utilizando pygetwindow.

    O pygetwindow pode gerar no Windows:

        Error code from Windows: 0
        A operação foi concluída com êxito.

    Esse erro não é tratado imediatamente como falha porque muitas vezes
    a janela foi ativada mesmo assim.
    """
    try:
        if getattr(window, "isMinimized", False):
            try:
                window.restore()
                time.sleep(0.25)
            except Exception:
                pass

        try:
            window.activate()

        except Exception as exc:
            message = str(exc).lower()

            harmless_messages = (
                "error code from windows: 0",
                "a operação foi concluída com êxito",
                "a operacao foi concluida com exito",
                "the operation completed successfully",
            )

            if not any(
                harmless in message
                for harmless in harmless_messages
            ):
                # Não interrompe aqui.
                # Ainda teremos outros métodos de ativação.
                pass

        time.sleep(0.35)

        return _is_window_foreground(window)

    except Exception:
        return False


# ============================================================
# FALLBACK VISUAL
# ============================================================

def _activate_by_click(window) -> bool:
    """
    Última tentativa: clicar fisicamente na barra superior da janela.

    Esse método costuma funcionar muito bem com conexões RDP porque
    equivale ao usuário clicar na própria janela.
    """
    try:
        left = int(getattr(window, "left", 0))
        top = int(getattr(window, "top", 0))
        width = int(getattr(window, "width", 0))
        height = int(getattr(window, "height", 0))

        if width <= 0 or height <= 0:
            return False

        # Evita clicar nos botões fechar/minimizar.
        #
        # Escolhemos aproximadamente o centro da barra superior.
        click_x = left + (width // 2)

        # Barra de título / região superior.
        click_y = top + 10

        # Garante coordenadas não negativas.
        click_x = max(1, click_x)
        click_y = max(1, click_y)

        pyautogui.click(
            click_x,
            click_y
        )

        time.sleep(0.50)

        return _is_window_foreground(window)

    except Exception:
        return False


# ============================================================
# FUNÇÃO PRINCIPAL UTILIZADA PELO ROBÔ
# ============================================================

def activate_window_contains(
    text: str,
    wait: float = 0.5,
    timeout: float = 10.0
):
    """
    Localiza e ativa uma janela cujo título contenha ``text``.

    Esta é a função utilizada pelo sap_bot.py.

    Estratégias utilizadas:

        1. localizar a janela;
        2. restaurar caso esteja minimizada;
        3. tentar API nativa simples do Windows;
        4. tentar pygetwindow;
        5. tentar AttachThreadInput;
        6. clicar fisicamente na janela;
        7. verificar se realmente ficou ativa.

    O comportamento evita o erro:

        Error code from Windows: 0
        A operação foi concluída com êxito.

    que pode ocorrer no pygetwindow.
    """

    needle = str(text or "").strip()

    if not needle:
        raise RuntimeError(
            "O título configurado para localizar a Área Remota está vazio."
        )

    start = time.time()

    window = None

    # --------------------------------------------------------
    # PROCURA A JANELA
    # --------------------------------------------------------

    while (time.time() - start) < timeout:

        window = find_window_contains(needle)

        if window is not None:
            break

        time.sleep(0.40)

    if window is None:
        available = all_window_titles()

        sample = available[:15]

        titles_text = "\n".join(
            f"- {title}"
            for title in sample
        )

        if not titles_text:
            titles_text = "- Nenhuma janela detectada"

        raise RuntimeError(
            f"Área Remota não encontrada.\n\n"
            f"O robô procurou por uma janela contendo:\n"
            f"'{needle}'\n\n"
            f"Janelas detectadas pelo Windows:\n"
            f"{titles_text}\n\n"
            f"Deixe a Área de Trabalho Remota aberta, "
            f"restaurada e visível antes de iniciar."
        )

    # --------------------------------------------------------
    # SE JÁ ESTIVER ATIVA, NÃO FAZ MAIS NADA
    # --------------------------------------------------------

    if _is_window_foreground(window):
        time.sleep(max(0.0, wait))
        return window

    # --------------------------------------------------------
    # RESTAURA
    # --------------------------------------------------------

    try:
        if getattr(window, "isMinimized", False):
            window.restore()
            time.sleep(0.30)
    except Exception:
        pass

    _restore_window_native(window)

    # --------------------------------------------------------
    # TENTATIVA 1 - API WINDOWS
    # --------------------------------------------------------

    if _activate_native_simple(window):
        time.sleep(max(0.0, wait))
        return window

    # --------------------------------------------------------
    # TENTATIVA 2 - PYGETWINDOW
    # --------------------------------------------------------

    if _activate_pygetwindow(window):
        time.sleep(max(0.0, wait))
        return window

    # --------------------------------------------------------
    # TENTATIVA 3 - ATTACH THREAD INPUT
    # --------------------------------------------------------

    if _activate_native_attached(window):
        time.sleep(max(0.0, wait))
        return window

    # --------------------------------------------------------
    # TENTATIVA 4 - CLIQUE FÍSICO
    # --------------------------------------------------------

    if _activate_by_click(window):
        time.sleep(max(0.0, wait))
        return window

    # --------------------------------------------------------
    # UMA ÚLTIMA VERIFICAÇÃO
    # --------------------------------------------------------

    if _is_window_foreground(window):
        time.sleep(max(0.0, wait))
        return window

    raise RuntimeError(
        "A Área Remota foi encontrada, mas o Windows não permitiu "
        "colocá-la em primeiro plano.\n\n"
        "Antes de tentar novamente:\n"
        "1. deixe a Área Remota aberta;\n"
        "2. não deixe a janela minimizada;\n"
        "3. deixe o SAP aberto dentro dela;\n"
        "4. não utilize mouse ou teclado durante o processamento."
    )


# ============================================================
# CONSULTA DA JANELA ATIVA
# ============================================================

def active_window_title() -> str:
    """
    Retorna o título da janela atualmente ativa.
    """
    try:
        window = gw.getActiveWindow()

        if window:
            return _window_title(window)

    except Exception:
        pass

    return ""


# ============================================================
# FUNÇÕES DE DIAGNÓSTICO
# ============================================================

def window_exists_contains(text: str) -> bool:
    """
    Retorna True quando uma janela contendo o texto informado existe.
    """
    return find_window_contains(text) is not None


def debug_windows() -> dict:
    """
    Retorna informações úteis para a tela de diagnóstico do Streamlit.
    """
    active_title = active_window_title()

    return {
        "active_window": active_title,
        "windows": all_window_titles(),
    }
