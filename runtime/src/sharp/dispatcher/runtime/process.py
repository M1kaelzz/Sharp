from __future__ import annotations

from contextlib import suppress
import concurrent.futures
from dataclasses import dataclass
import logging
import threading
import time
from typing import Any, Callable

from docker.errors import APIError, DockerException
from docker.models.containers import Container

LOG = logging.getLogger(__name__)


def run_docker_ctl(fn, timeout_seconds: float, *, context: str = ""):
    """Run *fn* (a short docker control call) with a hard wall-clock timeout.

    Returns the function's value on success, or None if it timed out or
    raised.  The call runs on a **daemon** thread and the caller waits at most
    *timeout_seconds* via ``join`` — never longer.

    Why a fresh daemon thread instead of a shared ``ThreadPoolExecutor``:
    ThreadPoolExecutor workers are non-daemon, so a docker call blocked on a
    wedged daemon would (a) occupy a pool slot and (b) block process exit
    (interpreter joins non-daemon threads at shutdown).  A daemon thread that
    outlives the timeout is abandoned: it cannot block the caller (join is
    bounded) and cannot block process shutdown (daemon), and it exits on its
    own once docker responds.  These calls are rare (session-id read, session
    kill, path remove/stat), so per-call thread creation is negligible.

    *context* is a short human-readable label (e.g. ``sid=123 container=x``)
    included in timeout/error logs so call sites keep precise diagnostics
    without duplicating the timeout machinery.
    """
    suffix = f" ({context})" if context else ""
    box: list = []

    def _wrapped():
        try:
            box.append(fn())
        except Exception as exc:  # noqa: BLE001 - control calls surface many docker error types
            LOG.warning("docker control call failed%s: %s", suffix, exc)
            box.append(None)

    thread = threading.Thread(target=_wrapped, name="docker-ctl", daemon=True)
    thread.start()
    thread.join(timeout=timeout_seconds)
    if thread.is_alive():
        LOG.warning("docker control call timed out after %.1fs%s", timeout_seconds, suffix)
        return None
    return box[0] if box else None


EXEC_KILL_JOIN_TIMEOUT_SECONDS = 5.0
# How long to wait for the exec's shell to write its session id before giving up
# on an in-container kill (covers the race where kill() fires just after start()).
SESSION_ID_READ_TIMEOUT_SECONDS = 1.0
# How long to wait for pkill + rm commands in _kill_session() before giving up.
# Prevents the dispatcher loop from blocking indefinitely if Docker daemon hangs.
KILL_SESSION_TIMEOUT_SECONDS = 2.0
# Target steady-state size for a single worker exec stream. Worker agents can
# run for minutes and emit very large output; keep the head (early events such
# as the session id) and the tail (the final answer) and drop the middle instead
# of buffering without bound. This is an amortized bound, not a hard ceiling:
# memory may transiently reach a small multiple of this value during compaction
# (and a single oversized chunk is held whole until the next compaction).
MAX_STREAM_BYTES = 16 * 1024 * 1024
_TRUNCATION_MARKER = (
    "\n\n[OUTPUT TRUNCATED: the middle portion of this output was dropped to "
    "stay within the memory limit. The content above is from the beginning; "
    "the content below is from the end. If you are summarising prior work, "
    "treat the gap as unknown and rely only on what is visible here.]\n\n"
)


class _CappedBuffer:
    """Accumulates text but bounds memory to ~MAX_STREAM_BYTES, keeping the
    head and tail. Memory is allowed to grow to 2x the limit before a compaction
    trims the middle, so compaction is amortized rather than per-append."""

    __slots__ = ("_limit", "_head", "_tail", "_tail_size", "_any", "_truncated")

    def __init__(self, limit: int = MAX_STREAM_BYTES):
        self._limit = max(2, limit)
        self._head = ""
        self._tail: list[str] = []
        self._tail_size = 0
        self._any = False
        self._truncated = False

    def append(self, text: str) -> None:
        if not text:
            return
        self._any = True
        self._tail.append(text)
        self._tail_size += len(text)
        if self._head_size() + self._tail_size > 2 * self._limit:
            self._compact()

    def _head_size(self) -> int:
        return len(self._head)

    def _compact(self) -> None:
        half = self._limit // 2
        combined = self._head + "".join(self._tail)
        # Freeze the earliest bytes as the head on first compaction so that
        # early output (e.g. the session id) is preserved across compactions.
        if not self._truncated:
            self._head = combined[:half]
            self._truncated = True
        tail = combined[-half:]
        self._tail = [tail]
        self._tail_size = len(tail)

    def has_content(self) -> bool:
        return self._any

    def value(self) -> str:
        tail = "".join(self._tail)
        if self._truncated:
            return self._head + _TRUNCATION_MARKER + tail
        return tail


@dataclass(slots=True)
class ProcessResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False
    cancelled: bool = False
    cancel_reason: str | None = None


_LIVE_OUTPUT_INTERVAL = 3.0   # push live output every ~3 seconds if there's new content


class ManagedProcess:
    def __init__(
        self,
        container: Container,
        command: list[str],
        env: dict[str, str],
        session_file: str | None = None,
        stdout_callback: Callable[[str], None] | None = None,
    ):
        self.command = command
        self.env = env
        self._container = container
        self._api = container.client.api
        self._session_file = session_file
        self._stdout_callback = stdout_callback
        self._exec_id: str | None = None
        self._reader: threading.Thread | None = None
        self._stream: Any | None = None
        self._stdout = _CappedBuffer()
        self._stderr = _CappedBuffer()
        self._returncode: int | None = None
        self._timed_out = False
        self._cancel_reason: str | None = None
        self._read_error: str | None = None
        self._done = threading.Event()

    def start(self) -> None:
        exec_info = self._api.exec_create(
            self._container.id,
            self.command,
            stdout=True,
            stderr=True,
            stdin=False,
            tty=False,
            environment=self.env,
        )
        self._exec_id = exec_info["Id"]
        self._reader = threading.Thread(target=self._read_stream, daemon=True)
        self._reader.start()

    def communicate(self, timeout: float | None) -> ProcessResult:
        assert self._reader is not None
        self._reader.join(timeout=timeout)
        if self._reader.is_alive():
            self._timed_out = True
            # Best-effort unblock: kill the container process AND force-close the
            # docker stream. kill() may be ineffective on its own (docker's exec
            # PID is in the host namespace, not the container's), and closing the
            # stream may not interrupt a blocked read on every platform; do both,
            # then wait once so neither failure mode doubles the shutdown latency.
            self.kill()
            self._close_stream(self._stream)
            self._reader.join(timeout=EXEC_KILL_JOIN_TIMEOUT_SECONDS)
        if self._reader.is_alive():
            if self._returncode is None:
                self._returncode = 137
            self._done.set()
            # The reader is a daemon thread; it will terminate when the container
            # is later stopped/removed and the stream finally ends.
            LOG.warning("container exec reader thread did not terminate exec_id=%s", self._exec_id)
        # Wait for the reader's finally block to publish returncode/output when it
        # did terminate; bounded so a wedged reader cannot block forever.
        self._done.wait(timeout=EXEC_KILL_JOIN_TIMEOUT_SECONDS)
        if self._read_error and not self._stderr.has_content():
            self._stderr.append(self._read_error)
        return ProcessResult(
            returncode=self._returncode if self._returncode is not None else 1,
            stdout=self._stdout.value(),
            stderr=self._stderr.value(),
            timed_out=self._timed_out,
            cancelled=self._cancel_reason is not None,
            cancel_reason=self._cancel_reason,
        )

    def kill(self) -> None:
        if self._exec_id is None:
            return
        try:
            details = self._api.exec_inspect(self._exec_id)
        except DockerException as exc:
            LOG.warning("failed to inspect exec before kill exec_id=%s error=%s", self._exec_id, exc)
            return
        if not details.get("Running"):
            return
        session_id = self._read_session_id()
        if session_id is None:
            LOG.warning(
                "cannot resolve container session id for kill exec_id=%s session_file=%s; "
                "relying on container stop/removal",
                self._exec_id,
                self._session_file,
            )
            return
        self._kill_session(session_id)

    def cancel(self, reason: str) -> None:
        if self._cancel_reason is None:
            self._cancel_reason = reason
        self.kill()

    def _read_stream(self) -> None:
        assert self._exec_id is not None
        stream: Any | None = None
        _last_push = time.monotonic()
        _pending: list[str] = []
        try:
            stream = self._api.exec_start(
                self._exec_id,
                detach=False,
                tty=False,
                stream=True,
                demux=True,
            )
            self._stream = stream
            for chunk in stream:
                stdout, stderr = self._split_chunk(chunk)
                if stdout:
                    self._stdout.append(stdout)
                    if self._stdout_callback is not None:
                        _pending.append(stdout)
                if stderr:
                    self._stderr.append(stderr)
                # Push buffered output every _LIVE_OUTPUT_INTERVAL seconds
                if self._stdout_callback is not None and _pending:
                    now = time.monotonic()
                    if now - _last_push >= _LIVE_OUTPUT_INTERVAL:
                        try:
                            self._stdout_callback("".join(_pending))
                        except Exception:
                            pass
                        _pending.clear()
                        _last_push = now
        except Exception as exc:
            # Broad on purpose: when communicate() force-closes the stream to
            # unblock a stuck read, the docker/urllib3 stack can raise a variety
            # of non-DockerException errors. Any of them means the read is over;
            # record it and let the finally block finalize the exit code.
            self._read_error = str(exc)
        finally:
            # Flush remaining buffered output before closing
            if self._stdout_callback is not None and _pending:
                try:
                    self._stdout_callback("".join(_pending))
                except Exception:
                    pass
            self._close_stream(stream)
            self._returncode = self._resolve_exit_code()
            self._done.set()

    @staticmethod
    def _close_stream(stream: Any | None) -> None:
        if stream is None:
            return
        close = getattr(stream, "close", None)
        if callable(close):
            with suppress(Exception):
                close()
        response = getattr(stream, "_response", None)
        response_close = getattr(response, "close", None)
        if callable(response_close):
            with suppress(Exception):
                response_close()

    def _resolve_exit_code(self) -> int:
        assert self._exec_id is not None
        deadline = time.monotonic() + EXEC_KILL_JOIN_TIMEOUT_SECONDS
        while True:
            try:
                details = self._api.exec_inspect(self._exec_id)
            except DockerException as exc:
                if self._read_error is None:
                    self._read_error = str(exc)
                return 137 if self._timed_out else 1
            exit_code = details.get("ExitCode")
            if exit_code is not None:
                return int(exit_code)
            if time.monotonic() >= deadline:
                return 137 if self._timed_out else 1
            time.sleep(0.1)

    def _read_session_id(self) -> int | None:
        if not self._session_file:
            return None

        def _do_read():
            try:
                result = self._container.exec_run(["cat", self._session_file], user="0")
                if result is None:
                    return None
                exit_code = result.exit_code if hasattr(result, "exit_code") else None
                if exit_code != 0:
                    return None
                output = getattr(result, "output", None) or b""
                text = output.decode("utf-8", errors="replace").strip() if isinstance(output, bytes) else str(output).strip()
                if text.isdigit():
                    return int(text)
            except Exception as exc:
                LOG.debug("failed to read session file %s: %s", self._session_file, exc)
                return None
            return None

        # Wall-clock bound via the shared pool: a wedged exec_run() must not
        # hang the caller (historical dispatcher-freeze root cause).  Timeout
        # and errors both surface as None here.
        return run_docker_ctl(
            _do_read, SESSION_ID_READ_TIMEOUT_SECONDS,
            context=f"read-session-file {self._session_file}",
        )

    def _kill_session(self, session_id: int) -> None:
        """Kill a session by its session ID and clean up the session file.

        Runs under a hard wall-clock timeout via the shared docker-ctl pool so
        a wedged Docker daemon cannot hang the dispatcher loop (historically
        the freeze root cause); on timeout the kill is abandoned and the
        container stop/removal path remains the backstop.
        """
        def _do_kill():
            try:
                result = self._container.exec_run(["pkill", "-KILL", "-s", str(session_id)], user="0")
            except APIError as exc:
                LOG.warning(
                    "failed to signal container session sid=%s container=%s error=%s",
                    session_id,
                    self._container.name,
                    exc,
                )
                return
            # pkill: 0 = signalled a process, 1 = no matching process (already gone).
            # Both are fine; 2/3 indicate a usage/fatal error worth surfacing.
            exit_code = result.exit_code if hasattr(result, "exit_code") else None
            if exit_code not in (0, 1, None):
                LOG.warning(
                    "pkill returned unexpected code=%s sid=%s container=%s",
                    exit_code,
                    session_id,
                    self._container.name,
                )
            # The killed shell could not run its own cleanup; remove the session file.
            if self._session_file:
                with suppress(Exception):
                    self._container.exec_run(["rm", "-f", "--", self._session_file], user="0")

        run_docker_ctl(
            _do_kill, KILL_SESSION_TIMEOUT_SECONDS,
            context=f"kill-session sid={session_id} container={self._container.name}",
        )

    @staticmethod
    def _split_chunk(chunk: Any) -> tuple[str, str]:
        if isinstance(chunk, tuple):
            stdout, stderr = chunk
        else:
            stdout, stderr = chunk, None
        return ManagedProcess._decode(stdout), ManagedProcess._decode(stderr)

    @staticmethod
    def _decode(chunk: bytes | str | None) -> str:
        if chunk is None:
            return ""
        if isinstance(chunk, bytes):
            return chunk.decode("utf-8", errors="replace")
        return chunk
