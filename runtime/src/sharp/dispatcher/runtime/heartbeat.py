from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from sharp.dispatcher.protocol.client import ApiResult, SharpClient
from sharp.dispatcher.runtime.process import ManagedProcess


LOG = logging.getLogger(__name__)
HEARTBEAT_FAILURE_GRACE_MULTIPLIER = 2
# Extra slack added on top of the heartbeat HTTP timeout when joining the thread
# on stop(). The join must outlast one in-flight heartbeat request so stop() does
# not return while a heartbeat is still on the wire and able to refresh a lease
# the task is already releasing.
STOP_JOIN_MARGIN_SECONDS = 5.0


@dataclass(slots=True)
class HeartbeatFailure:
    status_code: int | None
    text: str


class HeartbeatLease:
    def __init__(
        self,
        heartbeat: Callable[[], ApiResult],
        scope: str,
        worker_name: str,
        interval: int,
        stop_join_timeout: float,
    ):
        self._heartbeat = heartbeat
        self._scope = scope
        self._worker_name = worker_name
        self._interval = interval
        self._stop_join_timeout = stop_join_timeout
        self._process: ManagedProcess | None = None
        self._failure: HeartbeatFailure | None = None
        self._last_success_at = time.monotonic()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, daemon=True)

    @classmethod
    def for_intent(
        cls,
        client: SharpClient,
        project_id: str,
        intent_id: str,
        worker_name: str,
        interval: int,
    ) -> "HeartbeatLease":
        return cls(
            heartbeat=lambda: client.heartbeat(project_id, intent_id, worker_name),
            scope=f"project={project_id} intent={intent_id}",
            worker_name=worker_name,
            interval=interval,
            stop_join_timeout=client.timeout + STOP_JOIN_MARGIN_SECONDS,
        )

    @classmethod
    def for_reason(
        cls,
        client: SharpClient,
        project_id: str,
        worker_name: str,
        interval: int,
    ) -> "HeartbeatLease":
        return cls(
            heartbeat=lambda: client.reason_heartbeat(project_id, worker_name),
            scope=f"project={project_id} reason",
            worker_name=worker_name,
            interval=interval,
            stop_join_timeout=client.timeout + STOP_JOIN_MARGIN_SECONDS,
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=self._stop_join_timeout)
        if self._thread.is_alive():
            LOG.warning(
                "heartbeat thread did not stop within %.0fs scope=%s worker=%s",
                self._stop_join_timeout,
                self._scope,
                self._worker_name,
            )

    def attach_process(self, process: ManagedProcess | None) -> None:
        with self._lock:
            self._process = process

    @property
    def failure(self) -> HeartbeatFailure | None:
        return self._failure

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            if self._stop.is_set():
                return
            try:
                result = self._heartbeat()
            except Exception as exc:  # noqa: BLE001 - must never die silently
                # A non-RequestException (bad JSON, programming error, ...)
                # previously killed this daemon thread with no traceback,
                # leaving the lease silently dead while the task kept running
                # (double-execution window under multiple dispatchers). Treat
                # any unexpected error as a transient failure through the same
                # grace path below; only a real lease conflict fails fast.
                LOG.exception(
                    "heartbeat raised unexpected error scope=%s worker=%s",
                    self._scope,
                    self._worker_name,
                )
                result = ApiResult(status_code=0, text=f"unexpected heartbeat error: {exc}")
            if result.ok:
                self._last_success_at = time.monotonic()
                continue
            if result.status_code in (403, 409):
                self._fail(result.status_code, result.text)
                return
            elapsed = time.monotonic() - self._last_success_at
            grace_seconds = max(float(self._interval), float(self._interval * HEARTBEAT_FAILURE_GRACE_MULTIPLIER))
            LOG.warning(
                "heartbeat transient failure scope=%s worker=%s status=%s elapsed=%.1fs grace=%.1fs",
                self._scope,
                self._worker_name,
                result.status_code,
                elapsed,
                grace_seconds,
            )
            if elapsed < grace_seconds:
                continue
            self._fail(result.status_code or None, result.text)
            return

    def _fail(self, status_code: int | None, text: str) -> None:
        self._failure = HeartbeatFailure(status_code, text)
        LOG.warning(
            "heartbeat failed scope=%s worker=%s status=%s",
            self._scope,
            self._worker_name,
            status_code,
        )
        with self._lock:
            process = self._process
        if process is not None:
            process.kill()
