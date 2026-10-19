from pathlib import Path

import click
import uvicorn

from sharp.dispatcher.logging import configure_logging
from sharp.dispatcher.scheduler.loop import DispatcherLoop
from sharp.server import db
from sharp.single_instance import SingleInstanceError, acquire_single_instance_lock

# Holds single-instance lock handles for the process lifetime. The OS releases
# the underlying flock automatically when the process exits; we only need to
# stop these handles from being garbage-collected (which would close the file
# and drop the lock) while the server/dispatcher runs.
_LOCK_HANDLES: list = []


@click.group()
def main():
    """Sharp - Fact-graph based collaborative exploration protocol."""


@main.command()
@click.option("--host", default="127.0.0.1", show_default=True, help="Bind host")
@click.option("--port", default=8000, show_default=True, help="Bind port")
@click.option(
    "--db-path",
    type=click.Path(),
    default=str(db.DEFAULT_DB),
    show_default=True,
    help="SQLite database path",
)
@click.option("--log-level", default="info", show_default=True, help="Uvicorn log level")
@click.option("--access-log/--no-access-log", default=True, show_default=True, help="Enable Uvicorn access log")
def serve(host: str, port: int, db_path: str, log_level: str, access_log: bool):
    """Start the Sharp API server."""
    try:
        _LOCK_HANDLES.append(acquire_single_instance_lock("server", detail=f"serve {host}:{port}"))
    except SingleInstanceError as exc:
        raise click.ClickException(str(exc)) from exc
    db.configure(Path(db_path))
    from sharp.server.app import app

    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level=log_level.lower(),
        access_log=access_log,
    )


@main.command()
@click.option("--host", default="127.0.0.1", show_default=True, help="Bind host")
@click.option("--port", default=8000, show_default=True, help="Bind port; 0 selects a free port")
@click.option(
    "--db-path",
    type=click.Path(),
    default=str(db.DEFAULT_DB),
    show_default=True,
    help="SQLite database path",
)
@click.option("--title", default="Sharp", show_default=True, help="Window title")
@click.option("--width", default=1440, show_default=True, help="Window width")
@click.option("--height", default=920, show_default=True, help="Window height")
def window(host: str, port: int, db_path: str, title: str, width: int, height: int):
    """Start Sharp in a desktop window."""
    try:
        _LOCK_HANDLES.append(acquire_single_instance_lock("server", detail=f"window {host}:{port}"))
    except SingleInstanceError as exc:
        raise click.ClickException(str(exc)) from exc
    from sharp.window import run_window

    try:
        run_window(
            host=host,
            port=port,
            db_path=Path(db_path),
            title=title,
            width=width,
            height=height,
        )
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc


@main.command()
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="Dispatcher config path",
)
@click.option("--once", is_flag=True, help="Run one scheduling iteration and exit")
@click.option(
    "--startup-healthcheck-only",
    is_flag=True,
    help="Run startup worker healthchecks and exit",
)
@click.option("--log-level", default="INFO", show_default=True, help="Log level")
@click.option("--server", help="Override the Sharp API server URL from the dispatch config")
def dispatch(config_path: Path, once: bool, startup_healthcheck_only: bool, log_level: str, server: str | None):
    """Run the Sharp dispatcher."""
    configure_logging(log_level, bare=startup_healthcheck_only)
    if not once and not startup_healthcheck_only:
        try:
            _LOCK_HANDLES.append(acquire_single_instance_lock("dispatch", detail=str(config_path)))
        except SingleInstanceError as exc:
            raise click.ClickException(str(exc)) from exc
    loop = DispatcherLoop(config_path, server_override=server)
    try:
        if startup_healthcheck_only:
            loop.run_startup_healthchecks_only()
            return
        loop.run(once=once)
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc
