from __future__ import annotations

import concurrent.futures
import io
import logging
from pathlib import Path, PurePosixPath
import re
import tarfile
import threading
import uuid

import docker
from docker.errors import APIError, DockerException, NotFound
from docker.models.containers import Container

from sharp.dispatcher.config import ContainerConfig
from sharp.dispatcher.runtime.process import ManagedProcess, run_docker_ctl

LOG = logging.getLogger(__name__)

# Directory inside the worker container where each exec records its session id so
# it can be signalled by session (see build_exec_process / ManagedProcess.kill).
EXEC_SESSION_ROOT = "/tmp/sharp-exec"


class ContainerManager:
    _PREFIX = "sharp-dispatch-"
    _STARTUP_PREFIX = "sharp-startup-healthcheck-"

    def __init__(self, config: ContainerConfig, client=None):
        """Wrap the docker SDK for one dispatcher.

        ``client`` is injectable (defaults to ``docker.from_env()``) so tests
        can drive the container lifecycle against a fake docker client without
        a daemon; it only needs the subset of the docker SDK surface used here
        (``containers.run/get/list`` and per-container ``reload/start/stop/
        remove/exec_run/put_archive``) plus ``close()``.
        """
        self._config = config
        self._client = client if client is not None else docker.from_env()
        self._ensure_running_locks: dict[str, threading.Lock] = {}
        self._ensure_running_locks_guard = threading.Lock()

    @staticmethod
    def _validate_container_name(name: str) -> None:
        """Validate container name to prevent injection attacks.

        Docker container names must match: [a-zA-Z0-9][a-zA-Z0-9._-]*
        Raises ValueError if the name is invalid.
        """
        if not re.match(r'^[a-zA-Z0-9][a-zA-Z0-9._-]*$', name):
            raise ValueError(f"invalid container name: {name}")

    def close(self) -> None:
        self._client.close()

    def container_name(self, project_id: str) -> str:
        sanitized = self._sanitize_container_name(project_id)
        return f"{self._PREFIX}{sanitized}"

    def _sanitize_container_name(self, project_id: str) -> str:
        """Sanitize project_id for safe use in container names.

        Docker container names must match [a-zA-Z0-9][a-zA-Z0-9_.-]*
        This prevents injection attacks and ensures valid container names.
        """
        # Replace any character that's not alphanumeric, underscore, dot, or hyphen
        sanitized = re.sub(r'[^a-zA-Z0-9_.-]', '-', project_id)
        # Ensure it starts with alphanumeric (Docker requirement)
        sanitized = re.sub(r'^[^a-zA-Z0-9]+', '', sanitized)
        # Limit length to 200 chars (Docker max is 255, leaving room for prefix)
        sanitized = sanitized[:200]
        # Ensure we have something left after sanitization
        if not sanitized:
            sanitized = "project"
        return sanitized

    def ensure_running(self, project_id: str) -> str:
        name = self.container_name(project_id)
        with self._ensure_running_lock(name):
            return self._ensure_running_locked(project_id, name)

    def _ensure_running_locked(self, project_id: str, name: str) -> str:
        state = self.inspect_state(name)
        if state == "running":
            LOG.debug("container already running project=%s container=%s", project_id, name)
            return name
        if state is not None:
            LOG.info("starting existing container project=%s container=%s state=%s", project_id, name, state)
            self._start_existing(name)
            return name
        LOG.info("creating container project=%s container=%s image=%s", project_id, name, self._config.image)
        try:
            self._client.containers.run(
                self._config.image,
                ["sleep", "infinity"],
                detach=True,
                name=name,
                platform=self._config.platform,
                network_mode=self._config.network_mode,
                cap_add=self._config.cap_add or None,
                extra_hosts={"host.docker.internal": "host-gateway"},
                **self._resource_kwargs(),
            )
            LOG.info("created container project=%s container=%s", project_id, name)
            return name
        except APIError as exc:
            if not self._is_name_conflict(exc):
                raise RuntimeError(f"failed to create container {name}: {exc}") from exc
        LOG.info("container name conflict, reusing existing container project=%s container=%s", project_id, name)
        state = self.inspect_state(name)
        if state == "running":
            return name
        if state is not None:
            LOG.info("starting conflicted existing container project=%s container=%s state=%s", project_id, name, state)
            self._start_existing(name)
            return name
        raise RuntimeError(f"failed to create container {name}")

    def _resource_kwargs(self) -> dict:
        """Build Docker resource-limit kwargs from config, omitting keys that
        are None so containers.run() uses the Docker daemon defaults."""
        kwargs: dict = {}
        if self._config.memory is not None:
            kwargs["mem_limit"] = self._config.memory
            # Disable swap to prevent memory spillover
            kwargs["memswap_limit"] = self._config.memory
        if self._config.cpu_count is not None:
            # Docker SDK accepts nano_cpus (integer nanoseconds per second).
            # 1 CPU = 1_000_000_000 nano_cpus.
            kwargs["nano_cpus"] = int(self._config.cpu_count * 1_000_000_000)
        # Add process limit to prevent fork bombs
        kwargs["pids_limit"] = 512
        # Disable OOM killer to prevent the container from being killed silently
        kwargs["oom_kill_disable"] = False
        return kwargs

    def _ensure_running_lock(self, name: str) -> threading.Lock:
        with self._ensure_running_locks_guard:
            lock = self._ensure_running_locks.get(name)
            if lock is None:
                lock = threading.Lock()
                self._ensure_running_locks[name] = lock
            return lock

    def create_startup_container(self) -> str:
        name = f"{self._STARTUP_PREFIX}{uuid.uuid4().hex[:12]}"
        LOG.debug("creating startup healthcheck container container=%s image=%s", name, self._config.image)
        try:
            self._client.containers.run(
                self._config.image,
                ["sleep", "infinity"],
                detach=True,
                name=name,
                platform=self._config.platform,
                network_mode=self._config.network_mode,
                cap_add=self._config.cap_add or None,
                extra_hosts={"host.docker.internal": "host-gateway"},
                **self._resource_kwargs(),
            )
        except DockerException as exc:
            raise RuntimeError(f"failed to create startup container {name}: {exc}") from exc
        return name

    def inspect_state(self, name: str) -> str | None:
        """Container state in exactly ONE docker GET.

        ``containers.get()`` inspects the daemon and populates attrs fresh, so
        the extra ``container.reload()`` this method used to issue was a second,
        redundant GET on every call (batch B2 — 11 call sites, all halved)."""
        container = self._get_container(name)
        if container is None:
            return None
        state = container.attrs.get("State", {}).get("Status")
        return str(state) if state else None

    def cleanup_completed(self, project_id: str) -> bool:
        name = self.container_name(project_id)
        state = self.inspect_state(name)
        if state is None:
            return True
        container = self._require_container(name)
        if self._config.completed_action == "remove":
            LOG.info("removing completed project container project=%s container=%s", project_id, name)
            return self._remove_container_with_retry(container, name)
        elif state == "running":
            LOG.info("stopping completed project container project=%s container=%s", project_id, name)
            try:
                container.stop(timeout=1)
            except NotFound:
                return True
            except DockerException as exc:
                LOG.warning("failed to stop container=%s error=%s", name, exc)
                return False
            return self.inspect_state(name) != "running"
        return True

    def cleanup_stopped(self, project_id: str) -> bool:
        name = self.container_name(project_id)
        state = self.inspect_state(name)
        if state != "running":
            return True
        LOG.info("stopping stopped project container project=%s container=%s", project_id, name)
        container = self._require_container(name)
        try:
            container.stop(timeout=1)
        except NotFound:
            return True
        except DockerException as exc:
            LOG.warning("failed to stop stopped project container=%s error=%s", name, exc)
            return False
        return self.inspect_state(name) != "running"

    def cleanup_orphan(self, name: str) -> bool:
        state = self.inspect_state(name)
        if state is None:
            return True
        LOG.info("removing orphan project container container=%s state=%s", name, state)
        container = self._require_container(name)
        return self._remove_container_with_retry(container, name)

    def managed_container_names(self) -> list[str]:
        try:
            containers = self._client.containers.list(all=True)
        except DockerException as exc:
            LOG.warning("failed to list managed containers error=%s", exc)
            return []
        return sorted(container.name for container in containers if container.name.startswith(self._PREFIX))

    def needs_completed_cleanup(self, project_id: str) -> bool:
        name = self.container_name(project_id)
        state = self.inspect_state(name)
        if state is None:
            return False
        if self._config.completed_action == "remove":
            return True
        return state == "running"

    def needs_orphan_cleanup(self, name: str) -> bool:
        return self.inspect_state(name) is not None

    def needs_stopped_cleanup(self, project_id: str) -> bool:
        return self.inspect_state(self.container_name(project_id)) == "running"

    def build_exec_process(
        self,
        container_name: str,
        env: dict[str, str],
        command: list[str],
        timeout_seconds: int | None = None,
        kill_after_seconds: int = 5,
        stdout_callback=None,
    ) -> ManagedProcess:
        container = self._require_container(container_name)
        inner: list[str] = []
        if timeout_seconds is not None:
            inner.extend(
                [
                    "timeout",
                    "-k",
                    f"{kill_after_seconds}s",
                    f"{timeout_seconds}s",
                ]
            )
        inner.extend(command)

        # Run the workload under a fresh session so it can be terminated reliably.
        # docker's exec Pid is in the host PID namespace, which cannot be used to
        # kill the process from inside the container; instead `setsid -w` makes the
        # shell a session leader (and waits, so the exec stays attached and
        # propagates the exit code), the shell records its own pid == session id,
        # and ManagedProcess.kill() signals the whole session via `pkill -s`.
        # `timeout` places its child in its own process group, so killing by
        # session — not process group — is required to reap the whole tree.
        session_file = f"{EXEC_SESSION_ROOT}/{uuid.uuid4().hex}.sid"
        script = 'mkdir -p "$1"; echo $$ > "$2"; sf="$2"; shift 2; "$@"; rc=$?; rm -f "$sf"; exit $rc'
        argv = ["setsid", "-w", "sh", "-c", script, "sh", EXEC_SESSION_ROOT, session_file, *inner]
        return ManagedProcess(container, argv, env, session_file=session_file,
                              stdout_callback=stdout_callback)

    def remove_path(self, container_name: str, path: str, *, require_prefix: str) -> None:
        """Best-effort recursive removal of a path inside a container. The path
        must be absolute and sit under require_prefix, guarding against removing
        anything outside the intended scratch directory."""
        normalized = str(PurePosixPath(path))
        prefix = str(PurePosixPath(require_prefix))
        if prefix in ("", "/") or normalized in ("", "/"):
            return
        if normalized != prefix and not normalized.startswith(prefix + "/"):
            LOG.warning("refusing to remove container path outside %s path=%s", prefix, path)
            return
        container = self._get_container(container_name)
        if container is None:
            return

        def _do_remove():
            try:
                # Run as root: graph snapshots are written via put_archive, which
                # creates root-owned files, while the default exec user is unprivileged
                # and cannot remove them.
                container.exec_run(["rm", "-rf", "--", normalized], user="0")
            except DockerException as exc:
                LOG.debug("failed to remove container path=%s container=%s error=%s", normalized, container_name, exc)

        # Best-effort with wall-clock timeout via the shared pool (a wedged
        # exec_run() must not block cleanup / the dispatcher loop).
        run_docker_ctl(_do_remove, 2.0, context=f"rm {normalized} container={container_name}")

    def write_text_file(self, container_name: str, path: str, content: str) -> None:
        archive_path, archive = self._text_file_archive(path, content)
        container = self._require_container(container_name)
        try:
            ok = container.put_archive(archive_path, archive)
        except DockerException as exc:
            raise RuntimeError(f"failed to write container file {path}: {exc}") from exc
        if not ok:
            raise RuntimeError(f"failed to write container file {path}")

    def path_exists_in_container(self, container_name: str, path: str) -> bool:
        """True if `path` is an existing file inside the container. Used to make
        binary injection idempotent across container reuse."""
        container = self._get_container(container_name)
        if container is None:
            return False

        def _do_check():
            try:
                result = container.exec_run(["test", "-f", path])
                return result.exit_code == 0
            except DockerException as exc:
                LOG.debug("path_exists check failed container=%s path=%s error=%s", container_name, path, exc)
                return False

        result = run_docker_ctl(
            _do_check, 1.0, context=f"test -f {path} container={container_name}"
        )
        # Timeout / error both surface as None -> treat as non-existent.
        return result if result is not None else False

    def write_binary_file(
        self, container_name: str, path: str, local_file: str, *, max_bytes: int
    ) -> int:
        """Copy a host file into the container at `path` via put_archive.

        Enforces a size cap before reading. Returns the number of bytes copied.
        Raises FileNotFoundError if the host file is gone, ValueError if it
        exceeds max_bytes, RuntimeError on docker failure.
        """
        source = Path(local_file)
        if not source.is_file():
            raise FileNotFoundError(f"host file not found: {local_file}")
        size = source.stat().st_size
        if size > max_bytes:
            raise ValueError(
                f"file too large to inject: {size} bytes > limit {max_bytes} bytes ({local_file})"
            )
        archive_path, archive = self._binary_file_archive(path, source)
        container = self._require_container(container_name)
        try:
            ok = container.put_archive(archive_path, archive)
        except DockerException as exc:
            raise RuntimeError(f"failed to inject container file {path}: {exc}") from exc
        if not ok:
            raise RuntimeError(f"failed to inject container file {path}")
        # put_archive returning True only means the Docker API accepted the tar
        # stream, not that the bytes landed intact (a full filesystem or a
        # truncated write can still pass here). Confirm the in-container size
        # matches the source before reporting success.
        actual = self._container_file_size(container, path)
        if actual is not None and actual != size:
            raise RuntimeError(
                f"injected file size mismatch {path}: expected {size} bytes, "
                f"found {actual} bytes in container"
            )
        return size

    def _container_file_size(self, container: Container, path: str) -> int | None:
        """Return the size in bytes of `path` inside the container, or None if
        it cannot be determined (stat unavailable, unparseable, or timed out).
        None means "could not verify" — callers must not treat it as a size."""

        def _do_stat():
            try:
                result = container.exec_run(["stat", "-c", "%s", path])
            except DockerException as exc:
                LOG.debug("stat failed container=%s path=%s error=%s", container.name, path, exc)
                return None
            if result.exit_code != 0:
                return None
            raw = result.output.decode("utf-8", "replace").strip() if result.output else ""
            try:
                return int(raw)
            except ValueError:
                LOG.debug("unparseable stat output container=%s path=%s raw=%r", container.name, path, raw)
                return None

        return run_docker_ctl(
            _do_stat, 1.0, context=f"stat {path} container={container.name}"
        )

    @staticmethod
    def _binary_file_archive(path: str, source: Path) -> tuple[str, bytes]:
        target = PurePosixPath(path)
        if not target.is_absolute() or target.name in ("", ".", ".."):
            raise ValueError(f"container file path must be absolute: {path}")
        parts = target.parts[1:]
        if not parts or any(part in ("", ".", "..") for part in parts):
            raise ValueError(f"invalid container file path: {path}")
        if len(parts) == 1:
            archive_path = "/"
            archive_parts = parts
        else:
            archive_path = f"/{parts[0]}"
            archive_parts = parts[1:]

        payload = source.read_bytes()
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w") as archive:
            parent = ""
            for part in archive_parts[:-1]:
                parent = f"{parent}/{part}" if parent else part
                info = tarfile.TarInfo(parent)
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                archive.addfile(info)

            file_name = "/".join(archive_parts)
            info = tarfile.TarInfo(file_name)
            info.size = len(payload)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(payload))
        return archive_path, stream.getvalue()

    def remove_container(self, name: str, *, force: bool = True) -> None:
        self._validate_container_name(name)
        container = self._get_container(name)
        if container is None:
            return
        try:
            container.remove(force=force)
        except NotFound:
            return
        except DockerException as exc:
            LOG.warning("failed to remove container=%s error=%s", name, exc)

    def _start_existing(self, name: str) -> None:
        LOG.debug("starting container=%s", name)
        container = self._require_container(name)
        try:
            container.start()
            return
        except DockerException as exc:
            if self.inspect_state(name) == "running":
                return
            raise RuntimeError(f"failed to start container {name}: {exc}") from exc

    def _get_container(self, name: str) -> Container | None:
        self._validate_container_name(name)
        try:
            return self._client.containers.get(name)
        except NotFound:
            return None
        except DockerException as exc:
            raise RuntimeError(f"failed to get container {name}: {exc}") from exc

    def _require_container(self, name: str) -> Container:
        container = self._get_container(name)
        if container is None:
            raise RuntimeError(f"container not found: {name}")
        return container

    @staticmethod
    def _is_name_conflict(exc: APIError) -> bool:
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        explanation = str(getattr(exc, "explanation", "") or exc)
        return status_code == 409 or "is already in use" in explanation

    @staticmethod
    def _text_file_archive(path: str, content: str) -> tuple[str, bytes]:
        target = PurePosixPath(path)
        if not target.is_absolute() or target.name in ("", ".", ".."):
            raise ValueError(f"container file path must be absolute: {path}")
        parts = target.parts[1:]
        if not parts or any(part in ("", ".", "..") for part in parts):
            raise ValueError(f"invalid container file path: {path}")
        if len(parts) == 1:
            archive_path = "/"
            archive_parts = parts
        else:
            archive_path = f"/{parts[0]}"
            archive_parts = parts[1:]

        payload = content.encode("utf-8")
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w") as archive:
            parent = ""
            for part in archive_parts[:-1]:
                parent = f"{parent}/{part}" if parent else part
                info = tarfile.TarInfo(parent)
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                archive.addfile(info)

            file_name = "/".join(archive_parts)
            info = tarfile.TarInfo(file_name)
            info.size = len(payload)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(payload))
        return archive_path, stream.getvalue()

    def _remove_container_with_retry(self, container: Container, name: str, max_retries: int = 3) -> bool:
        """Remove a container with retry logic.

        Retries up to max_retries times with exponential backoff.
        Returns True if successfully removed, False otherwise.
        """
        import time

        for attempt in range(max_retries):
            try:
                container.remove(force=True)
                # Verify removal
                if self.inspect_state(name) is None:
                    if attempt > 0:
                        LOG.info("container removed after %d retries container=%s", attempt, name)
                    return True
            except NotFound:
                return True
            except DockerException as exc:
                if attempt < max_retries - 1:
                    backoff = 2 ** attempt  # 1s, 2s, 4s
                    LOG.warning(
                        "failed to remove container=%s attempt=%d/%d error=%s, retrying in %ds",
                        name, attempt + 1, max_retries, exc, backoff
                    )
                    time.sleep(backoff)
                else:
                    LOG.warning("failed to remove container=%s after %d attempts error=%s", name, max_retries, exc)
                    return False
        return False
