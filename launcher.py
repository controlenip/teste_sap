from __future__ import annotations

# IMPORTANTE:
# Estas variaveis precisam ser definidas ANTES de qualquer import do Streamlit.
import os

os.environ["STREAMLIT_GLOBAL_DEVELOPMENT_MODE"] = "false"
os.environ["STREAMLIT_SERVER_HEADLESS"] = "true"
os.environ["STREAMLIT_BROWSER_GATHER_USAGE_STATS"] = "false"
os.environ["STREAMLIT_SERVER_FILE_WATCHER_TYPE"] = "none"

import multiprocessing
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path


def bundle_root() -> Path:
    """Retorna a pasta onde os arquivos empacotados estao disponiveis."""
    if hasattr(sys, "_MEIPASS"):
        return Path(getattr(sys, "_MEIPASS"))
    return Path(__file__).resolve().parent


def find_free_port(start: int = 8501, end: int = 8525) -> int:
    """Procura uma porta local livre para o Streamlit."""
    for port in range(start, end + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue

    raise RuntimeError(
        f"Nao foi encontrada uma porta livre entre {start} e {end} para iniciar o Streamlit."
    )


def open_browser_when_ready(port: int) -> None:
    """Abre o navegador somente quando o servidor estiver respondendo."""
    url = f"http://127.0.0.1:{port}"

    # Aproximadamente 30 segundos de espera maxima.
    for _ in range(120):
        try:
            with socket.create_connection(
                ("127.0.0.1", port),
                timeout=0.25
            ):
                webbrowser.open(url, new=1)
                return

        except OSError:
            time.sleep(0.25)

    # Se nao detectou o servidor a tempo,
    # ainda tenta abrir o navegador.
    webbrowser.open(url, new=1)


def main() -> int:
    multiprocessing.freeze_support()

    root = bundle_root()
    app = root / "app.py"

    if not app.exists():
        raise FileNotFoundError(
            f"Arquivo interno app.py nao encontrado em: {app}"
        )

    # Garante que modules/, app.py, .streamlit/
    # e outros recursos empacotados sejam encontrados.
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    # Faz o Streamlit localizar o
    # .streamlit/config.toml empacotado.
    try:
        os.chdir(root)
    except OSError:
        pass

    port = find_free_port()

    # Reforca as configuracoes antes
    # de importar qualquer modulo do Streamlit.
    os.environ["STREAMLIT_GLOBAL_DEVELOPMENT_MODE"] = "false"
    os.environ["STREAMLIT_SERVER_HEADLESS"] = "true"
    os.environ["STREAMLIT_SERVER_ADDRESS"] = "127.0.0.1"
    os.environ["STREAMLIT_SERVER_PORT"] = str(port)
    os.environ["STREAMLIT_SERVER_FILE_WATCHER_TYPE"] = "none"
    os.environ["STREAMLIT_BROWSER_GATHER_USAGE_STATS"] = "false"

    # Abre o navegador quando o servidor estiver pronto.
    threading.Thread(
        target=open_browser_when_ready,
        args=(port,),
        daemon=True,
    ).start()

    # Importa Streamlit somente depois
    # de todas as configuracoes acima.
    from streamlit.web import cli as stcli

    # developmentMode=false e passado explicitamente
    # para impedir o erro:
    #
    # server.port does not work when
    # global.developmentMode is true
    sys.argv = [
        "streamlit",
        "run",
        str(app),

        "--global.developmentMode=false",

        "--server.headless=true",
        "--server.address=127.0.0.1",
        f"--server.port={port}",
        "--server.fileWatcherType=none",
        "--server.runOnSave=false",

        "--browser.gatherUsageStats=false",
    ]

    result = stcli.main()

    return int(result or 0)


if __name__ == "__main__":
    raise SystemExit(main())
