from __future__ import annotations

import socket
import threading
import time
from pathlib import Path

import uvicorn

from sharp.server import db


def run_window(
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
    db_path: Path = db.DEFAULT_DB,
    title: str = "Sharp",
    width: int = 1440,
    height: int = 920,
) -> None:
    try:
        import webview
    except ImportError as exc:
        raise RuntimeError(
            "Desktop window support requires pywebview. "
            "Install dependencies with `uv sync --project runtime --extra desktop` "
            "or `uv add --project runtime pywebview`."
        ) from exc

    selected_port = _free_port(host) if port == 0 else port
    if port != 0:
        _ensure_port_available(host, selected_port)
    db.configure(db_path)

    from sharp.server.app import app

    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=host,
            port=selected_port,
            log_level="warning",
            access_log=False,
        )
    )
    thread = threading.Thread(target=server.run, name="sharp-window-server", daemon=True)
    thread.start()
    _wait_for_server(host, selected_port)

    url = f"http://{host}:{selected_port}"
    webview.create_window(title, url, width=width, height=height)
    try:
        webview.start()
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def _free_port(host: str) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def _ensure_port_available(host: str, port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((host, port))
        except OSError as exc:
            raise RuntimeError(
                f"{host}:{port} is already in use. Stop the existing service or start Sharp with `--port 0`."
            ) from exc


def _wait_for_server(host: str, port: int, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    last_error: OSError | None = None
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.25):
                return
        except OSError as exc:
            last_error = exc
            time.sleep(0.1)
    raise RuntimeError(f"Sharp server did not start on {host}:{port}") from last_error
