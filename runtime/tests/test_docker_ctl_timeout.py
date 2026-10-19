"""Regression tests for the docker-ctl timeout helper (P0-1 fix).

Historical bug: every docker control call created its own
``ThreadPoolExecutor`` and used ``with`` around it.  ``future.result(timeout)``
returned on timeout, but exiting the ``with`` block called
``shutdown(wait=True)``, which *joined the still-blocked docker thread forever*
— so a wedged docker daemon still hung the caller (the original dispatcher
freeze, just moved one level).  The fix: one shared process-wide pool plus
``run_docker_ctl()`` that abandons the future on timeout and never shuts the
pool down on the call path.
"""

from __future__ import annotations

import time
import types

from sharp.dispatcher.runtime.process import (
    ManagedProcess,
    SESSION_ID_READ_TIMEOUT_SECONDS,
    run_docker_ctl,
)

SESSION_FILE = "/tmp/sharp-exec/tok.sid"


class _BlockingExecRun:
    """exec_run that blocks until released — simulates a wedged docker daemon."""

    def __init__(self):
        self.released = False
        self.exit_code = None
        self.output = b""

    def exec_run(self, cmd, user=None, **kwargs):
        while not self.released:
            time.sleep(0.005)
        return self


class _RecordingContainer:
    def __init__(self, blocker: _BlockingExecRun):
        self.name = "test-container"
        self.id = "cid"
        self._exec = blocker
        self.calls: list[list[str]] = []
        self.client = types.SimpleNamespace(
            api=types.SimpleNamespace(
                exec_inspect=lambda eid: {"Running": True},
            )
        )

    def exec_run(self, cmd, user=None, **kwargs):
        self.calls.append(cmd)
        return self._exec.exec_run(cmd, user=user)


def _blocked_process() -> tuple[ManagedProcess, _BlockingExecRun]:
    blocker = _BlockingExecRun()
    proc = ManagedProcess(_RecordingContainer(blocker), ["sh"], {}, session_file=SESSION_FILE)
    return proc, blocker


def test_read_session_id_returns_promptly_when_docker_blocks():
    """A wedged exec_run must not hang _read_session_id (old code hung in the
    with-block shutdown(wait=True))."""
    proc, blocker = _blocked_process()
    start = time.monotonic()
    sid = proc._read_session_id()
    elapsed = time.monotonic() - start
    # Bounded well above the timeout (thread scheduling slack), NOT seconds of join.
    assert elapsed < SESSION_ID_READ_TIMEOUT_SECONDS + 1.0
    assert sid is None  # timeout -> treat as no session id


def test_kill_returns_promptly_when_docker_blocks():
    """A wedged pkill exec_run must not hang _kill_session."""
    proc, blocker = _blocked_process()
    start = time.monotonic()
    proc._kill_session(27)
    elapsed = time.monotonic() - start
    assert elapsed < 3.0  # KILL_SESSION_TIMEOUT_SECONDS=2.0 + slack


def test_run_docker_ctl_abandons_but_worker_thread_survives():
    """On timeout the future is abandoned (caller returns) but the blocked
    worker thread stays alive in the shared pool and completes once the
    underlying call is released — i.e. we never kill/join a wedged thread."""
    released = {"flag": False}

    def blocking_fn():
        while not released["flag"]:
            time.sleep(0.005)
        return 42

    start = time.monotonic()
    result = run_docker_ctl(blocking_fn, timeout_seconds=0.2)
    assert time.monotonic() - start < 1.0
    assert result is None  # timed out

    # Release the blocked call; the worker thread should finish on its own
    # (we must NOT have joined/killed it — thread remains in the shared pool).
    released["flag"] = True
    time.sleep(0.2)  # give the worker a moment to complete without error
    # No assertion on the value (it was abandoned); the point is nothing crashed
    # and the pool is still usable.
    assert run_docker_ctl(lambda: "still-works", timeout_seconds=2.0) == "still-works"
