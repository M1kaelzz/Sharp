"""Entry point for the packaged (PyInstaller) Sharp binary.

One executable plays several roles depending on argv[0..]:

  <exe>                 launcher: license gate -> docker bootstrap -> run server +
                        dispatcher (each spawned as a child of this same exe) ->
                        supervise -> open browser
  <exe> _serve ...      in-process: run the API server (internal, self-invoked)
  <exe> _dispatch ...   in-process: run the dispatcher (internal, self-invoked)
  <exe> doctor          environment diagnostics
  <exe> license         show the current license status

Unlike the dev launcher (which shells out to `uv run`), the frozen build
re-invokes itself: sys.executable is the exe, so children are `<exe> _serve`.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
import urllib.request
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

# --- PyInstaller collection anchors ------------------------------------------
# Several runtime modules are imported lazily (licensing's cryptography, the
# server/dispatcher apps pulled in by the self-invoked `_serve`/`_dispatch`
# roles) — PyInstaller's static analysis misses them and produces a frozen
# binary missing most third-party deps. Importing them at the entry module
# makes PyInstaller walk the real dependency graph (incl. native hooks for
# cryptography/fastapi/uvicorn).
import sharp.server.app as _pkg_server_app  # noqa: F401  (web API runtime chain)
import sharp.dispatcher.scheduler.loop as _pkg_dispatch_loop  # noqa: F401  (dispatcher chain)
import sharp.licensing as _pkg_licensing  # noqa: F401  (license gate + cryptography)


IS_WINDOWS = os.name == "nt"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
DEFAULT_WORKER_IMAGE = "sharp-worker:latest"


def worker_image() -> str:
    """The worker image the dispatcher will actually use. Priority:
    SHARP_WORKER_IMAGE env > container.image in dispatch.yaml > full default.
    Reading dispatch.yaml keeps the exe's pre-flight check in sync with what the
    dispatcher runs (dispatch.yaml normally points at sharp-worker:latest)."""
    env = os.environ.get("SHARP_WORKER_IMAGE")
    if env:
        return env
    try:
        text = config_path().read_text(encoding="utf-8")
        in_container = False
        for raw in text.splitlines():
            line = raw.rstrip()
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if not line.startswith((" ", "\t")) and stripped == "container:":
                in_container = True
                continue
            if in_container and not line.startswith((" ", "\t")):
                in_container = False
            if in_container and stripped.startswith("image:"):
                value = stripped.split(":", 1)[1].strip().strip('"\'')
                if value:
                    return value
    except Exception:
        pass
    return DEFAULT_WORKER_IMAGE


def app_dir() -> Path:
    """Directory the user runs from — next to the exe when frozen, else cwd-ish."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(os.environ.get("SHARP_APP_DIR", Path.cwd())).resolve()


def data_dir() -> Path:
    d = app_dir() / "datas" / "sharp"
    d.mkdir(parents=True, exist_ok=True)
    return d


def license_path() -> Path:
    return Path(os.environ.get("SHARP_LICENSE_FILE", app_dir() / "license.key"))


def config_path() -> Path:
    return Path(os.environ.get("SHARP_DISPATCH_CONFIG", app_dir() / "dispatch.yaml"))


# ----------------------------------------------------------------------------- license


def require_license() -> None:
    from sharp.licensing import LicenseError, verify_license_file

    try:
        lic = verify_license_file(license_path())
    except LicenseError as exc:
        sys.stderr.write(f"\n[license] {exc}\n")
        sys.exit(2)
    print(f"[license] valid for {lic.subject}; {lic.days_remaining} day(s) remaining "
          f"(expires {lic.expires_at:%Y-%m-%d})", flush=True)


def print_license() -> int:
    from sharp.licensing import LicenseError, verify_license_file

    try:
        lic = verify_license_file(license_path())
    except LicenseError as exc:
        print(f"license: INVALID — {exc}")
        return 2
    print(f"license file: {license_path()}")
    print(f"subject:      {lic.subject}")
    print(f"issued_at:    {lic.issued_at:%Y-%m-%d}")
    print(f"expires_at:   {lic.expires_at:%Y-%m-%d}")
    print(f"remaining:    {lic.days_remaining} day(s)")
    return 0


def _require_model_token() -> None:
    """Fail early with a clear message if the model token is not configured,
    instead of letting the dispatcher child crash on ${SHARP_ANTHROPIC_AUTH_TOKEN}
    expansion in dispatch.yaml."""
    from sharp.secrets import load_secrets_file

    # Load datas/sharp/secrets.env into the environment (respects SHARP_ROOT set
    # by the launcher) so both this check and the child processes see the token.
    load_secrets_file()
    token = os.environ.get("SHARP_ANTHROPIC_AUTH_TOKEN", "")
    lowered = token.lower()
    if not token or "your-token" in lowered or "your_token" in lowered:
        secrets_file = data_dir() / "secrets.env"
        sys.stderr.write(
            "\n[config] model API token is not configured.\n"
            f"  Edit: {secrets_file}\n"
            '  Set:  SHARP_ANTHROPIC_AUTH_TOKEN="sk-..."\n'
            "  (copy config.example.env into datas/sharp/secrets.env if it does not exist)\n"
        )
        sys.exit(3)


# ----------------------------------------------------------------------------- docker


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, **kw)


def docker_available() -> bool:
    from shutil import which

    if which("docker") is None:
        return False
    return _run(["docker", "info"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def image_exists(image: str) -> bool:
    return _run(["docker", "image", "inspect", image],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def ensure_worker_image() -> None:
    if not docker_available():
        sys.exit(
            "Docker is required but not available.\n"
            "Install Docker Desktop and make sure it is running, then relaunch Sharp.\n"
            "  Windows/macOS: https://www.docker.com/products/docker-desktop/"
        )
    image = worker_image()
    if image_exists(image):
        return
    # Sharp ships one worker image (default sharp-worker:latest) with no prebuilt
    # upstream — it must be built locally (or loaded from a vendor-provided tar).
    sys.exit(
        f"Worker image '{image}' is not present.\n"
        "Build it once from the bundled Dockerfile (needs internet, ~a few minutes):\n\n"
        f"  docker build -t {image} ./container\n\n"
        "Run that inside the folder shipped with this app, then relaunch Sharp.\n"
        "(Or load a prebuilt image the vendor gave you: docker load -i sharp-worker.tar)"
    )


# ----------------------------------------------------------------------------- process supervision


def _self_cmd(*parts: str) -> list[str]:
    # Re-invoke this same executable. When frozen, sys.executable is the exe;
    # otherwise fall back to `python -m sharp.frozen_main` for dev testing.
    if getattr(sys, "frozen", False):
        return [sys.executable, *parts]
    return [sys.executable, "-m", "sharp.frozen_main", *parts]


def _spawn(cmd: list[str]) -> subprocess.Popen:
    if IS_WINDOWS:
        return subprocess.Popen(cmd, cwd=str(app_dir()), creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    return subprocess.Popen(cmd, cwd=str(app_dir()), start_new_session=True)


def _terminate(proc: subprocess.Popen | None, name: str) -> None:
    if proc is None or proc.poll() is not None:
        return
    print(f"[sharp] stopping {name} ...", flush=True)
    if IS_WINDOWS:
        _run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            return


def _wait_for_api(url: str, proc: subprocess.Popen, timeout: float = 40.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            sys.exit(f"server exited before the API was ready (code {proc.returncode})")
        try:
            with urllib.request.urlopen(f"{url}/projects", timeout=2):
                return
        except Exception:
            time.sleep(0.5)
    sys.exit(f"API did not become ready on {url}")


def run_launcher(argv: list[str]) -> int:
    host = os.environ.get("SHARP_WEB_HOST", DEFAULT_HOST)
    port = int(os.environ.get("SHARP_WEB_PORT", DEFAULT_PORT))
    no_open = "--no-open" in argv

    require_license()
    ensure_worker_image()

    # Pin the tool root for child processes. In a frozen build there is no source
    # tree for secrets.tool_root() to discover, so it would fall back to cwd;
    # setting SHARP_ROOT makes secrets.env resolution (datas/sharp/secrets.env)
    # deterministic for the spawned server and dispatcher.
    os.environ["SHARP_ROOT"] = str(app_dir())
    _require_model_token()

    db_path = str(data_dir() / "sharp.db")
    url = f"http://{host}:{port}"

    print(f"[sharp] starting server on {url} ...", flush=True)
    server = _spawn(_self_cmd("_serve", "--host", host, "--port", str(port),
                              "--db-path", db_path, "--no-access-log"))
    _wait_for_api(url, server)

    print("[sharp] starting dispatcher ...", flush=True)
    dispatcher = _spawn(_self_cmd("_dispatch", "--config", str(config_path()), "--server", url))

    if not no_open and os.environ.get("SHARP_OPEN_BROWSER", "1") != "0":
        try:
            webbrowser.open(url)
        except Exception:
            pass

    stop = {"done": False}

    def stop_all(signum=None, frame=None):
        if stop["done"]:
            return
        stop["done"] = True
        _terminate(dispatcher, "dispatcher")
        _terminate(server, "server")
        raise SystemExit(130 if signum else 0)

    signal.signal(signal.SIGINT, stop_all)
    if not IS_WINDOWS:
        signal.signal(signal.SIGTERM, stop_all)

    try:
        while True:
            if server.poll() is not None:
                _terminate(dispatcher, "dispatcher")
                return server.returncode
            if dispatcher.poll() is not None:
                print("[sharp] dispatcher exited; server still up. Ctrl+C to stop.", file=sys.stderr, flush=True)
                dispatcher_code = dispatcher.returncode
                # keep serving; wait on server only
                server.wait()
                return dispatcher_code
            time.sleep(0.5)
    finally:
        stop_all()


# ----------------------------------------------------------------------------- internal (self-invoked)


def _run_internal(argv: list[str]) -> int:
    """Route _serve/_dispatch to the click CLI in-process (no uv, no source tree)."""
    sub = argv[0]
    rest = argv[1:]
    from sharp.cli import main as cli_main

    mapped = {"_serve": "serve", "_dispatch": "dispatch", "_window": "window"}[sub]
    # click reads sys.argv; invoke its command directly via standalone mode off
    return cli_main([mapped, *rest], standalone_mode=True) or 0


def main() -> int:
    argv = sys.argv[1:]
    if argv and argv[0] in ("_serve", "_dispatch", "_window"):
        return _run_internal(argv)
    if argv and argv[0] == "license":
        return print_license()
    if argv and argv[0] == "doctor":
        return _doctor()
    return run_launcher(argv)


def _doctor() -> int:
    import platform

    print(f"Sharp packaged build")
    print(f"Frozen: {getattr(sys, 'frozen', False)}")
    print(f"App dir: {app_dir()}")
    print(f"Platform: {platform.system()} {platform.release()} ({platform.machine()})")
    print(f"License file: {license_path()} ({'exists' if license_path().exists() else 'missing'})")
    print(f"Config: {config_path()} ({'exists' if config_path().exists() else 'missing'})")
    print(f"Docker: {'ok' if docker_available() else 'not available'}")
    if docker_available():
        img = worker_image()
        print(f"Worker image: {'yes' if image_exists(img) else 'no'} ({img})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
