"""P1-4 worker process-level trace tests (batch: P1-4).

Locks three layers:

- **dispatcher archive** (`_reap_futures` → `_record_task_trace`): with
  runtime.task_trace_archive OFF the reap path is a no-op for traces (legacy
  behavior); ON it archives exactly one row per finished task round. A failed
  archive write is swallowed (must never disturb the dispatch loop).
- **reason prompt injection** (`_task_traces_block`): disabled -> placeholder;
  enabled with no records -> placeholder; enabled with records -> one line per
  trace with outcome/worker/duration, failures marked ⚠.
- **server**: POST /traces persists rows; GET /projects/{id}/traces returns
  newest-first with a bounded limit.

The loop is built via DispatcherLoop.__new__ with only the attributes each
method touches (same style as test_bootstrap_stall.py). Server tests use a
temp DB + TestClient.
"""

from __future__ import annotations

import types
from concurrent.futures import Future

import pytest

from sharp.dispatcher.models import RunningTask
from sharp.dispatcher.scheduler.loop import DispatcherLoop
from sharp.dispatcher.tasks import reason
from sharp.server import db

SERVER_TOKEN = "server-token-xyz"
TS = "2026-09-01T00:00:00Z"


@pytest.fixture
def env_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_db_path", None, raising=False)
    monkeypatch.setenv("SHARP_SERVER_TOKEN", SERVER_TOKEN)
    db.configure(tmp_path / "sharp.db")


@pytest.fixture
def client(env_db):
    from fastapi.testclient import TestClient
    from sharp.server.app import app

    c = TestClient(app)
    yield c
    c.close()


def _h() -> dict[str, str]:
    return {"Authorization": f"Bearer {SERVER_TOKEN}"}


class TraceRecordingClient:
    def __init__(self):
        self.trace_records: list[dict] = []
        self.fetched: list[list[dict]] = []

    def record_task_traces(self, records: list[dict]):
        self.trace_records.extend(records)
        return types.SimpleNamespace(ok=True, status_code=200)

    def fetch_task_traces(self, project_id: str, limit: int = 5):
        self.fetched.append([project_id, limit])
        return [
            {
                "project_id": project_id,
                "intent_id": "i1",
                "worker_name": "w1",
                "task_type": "explore",
                "outcome": "failed",
                "started_at": 0.0,
                "duration_ms": 1234,
            }
        ]


class FailingTraceClient(TraceRecordingClient):
    def record_task_traces(self, records: list[dict]):
        raise RuntimeError("server down")


def _make_loop(archive: bool, trace_client: TraceRecordingClient) -> DispatcherLoop:
    loop = DispatcherLoop.__new__(DispatcherLoop)
    loop.config = types.SimpleNamespace(
        runtime=types.SimpleNamespace(task_trace_archive=archive)
    )
    loop.client = trace_client
    loop.futures = {}
    loop.bootstrap_fail_counts = {}
    loop.bootstrap_stalled = {}
    loop.bootstrap_stall_hint_counts = {}
    loop.reason_checkpoints = {}
    loop.reason_stall_counts = {}
    loop.reason_stalled_hint_counts = {}
    loop.worker_unhealthy_until = {}
    loop.worker_rejected_until = {}
    loop._cleanup_pending = set()
    loop._budget_hint_written = set()
    loop._deadline_hint_written = set()
    loop._log_state = {}
    loop.project_cursor = 0
    loop.runtime_project_ids = set()
    return loop


def _done_future(outcome: str = "success") -> tuple[Future, RunningTask]:
    fut: Future = Future()
    fut.set_result(outcome)
    task = RunningTask(
        project_id="p1",
        task_type="explore",
        worker_name="w1",
        cancellation=types.SimpleNamespace(cancel=lambda reason: None),
        start_time=1000.0,
        started_at_wall=1_800_000_000.0,
        intent_id="i1",
    )
    return fut, task


# ── dispatcher: gate OFF keeps legacy no-archive behavior ─────────────────

def test_reap_with_archive_disabled_does_not_record():
    tc = TraceRecordingClient()
    loop = _make_loop(archive=False, trace_client=tc)
    fut, task = _done_future()
    loop.futures[fut] = task
    loop._reap_futures()
    assert tc.trace_records == []


# ── dispatcher: gate ON archives one row per finished round ───────────────

def test_reap_with_archive_enabled_records_one_row():
    tc = TraceRecordingClient()
    loop = _make_loop(archive=True, trace_client=tc)
    fut, task = _done_future()
    loop.futures[fut] = task
    loop._reap_futures()
    assert len(tc.trace_records) == 1
    r = tc.trace_records[0]
    assert r["project_id"] == "p1"
    assert r["intent_id"] == "i1"
    assert r["worker_name"] == "w1"
    assert r["task_type"] == "explore"
    assert r["outcome"] == "success"
    assert r["started_at"] == task.started_at_wall


def test_trace_started_at_uses_wall_clock_not_monotonic():
    """Regression guard: the 1970 bug.

    start_time is a monotonic counter (small, machine-uptime-relative); if it is
    ever reported as started_at again, the UI renders "1970-01-01" because it
    parses the column as epoch seconds. The archive must carry the wall-clock
    field instead.
    """
    tc = TraceRecordingClient()
    loop = _make_loop(archive=True, trace_client=tc)
    fut, task = _done_future()
    task.start_time = 42.0  # small monotonic value, typical right after boot
    task.started_at_wall = 1_800_000_000.0
    loop.futures[fut] = task
    loop._reap_futures()
    r = tc.trace_records[0]
    assert r["started_at"] == 1_800_000_000.0
    assert r["started_at"] != task.start_time  # 1970 bug must not come back
    assert r["started_at"] > 1_000_000_000  # sanity: plausible epoch seconds


# ── dispatcher: failed archive write is swallowed ─────────────────────────

def test_reap_archive_failure_is_swallowed():
    tc = FailingTraceClient()
    loop = _make_loop(archive=True, trace_client=tc)
    fut, task = _done_future()
    loop.futures[fut] = task
    # must not raise even though the trace write blows up
    loop._reap_futures()


# ── reason prompt block ───────────────────────────────────────────────────

def test_traces_block_disabled_placeholder():
    assert reason._task_traces_block(TraceRecordingClient(), "p1", enabled=False) == "(Task trace archive disabled.)"


def test_traces_block_enabled_empty_placeholder():
    tc = types.SimpleNamespace(fetch_task_traces=lambda pid, limit=5: [])
    assert reason._task_traces_block(tc, "p1", enabled=True) == "(No task trace records yet for this project.)"


def test_traces_block_enabled_formats_rows():
    tc = types.SimpleNamespace(
        fetch_task_traces=lambda pid, limit=5: [
            {"outcome": "failed", "worker_name": "w1", "task_type": "explore", "intent_id": "i1", "duration_ms": 1234},
            {"outcome": "success", "worker_name": "w2", "task_type": "reason", "intent_id": "", "duration_ms": 500},
        ]
    )
    block = reason._task_traces_block(tc, "p1", enabled=True)
    assert "⚠ explore outcome=failed worker=w1 intent=i1 duration_ms=1234" in block
    assert "✓ reason outcome=success worker=w2" in block


def test_traces_block_fetch_failure_degrades_to_placeholder():
    class Boom:
        def fetch_task_traces(self, project_id, limit=5):
            raise RuntimeError("down")

    block = reason._task_traces_block(Boom(), "p1", enabled=True)
    assert block == "(No task trace records yet for this project.)"


# ── server: archive + read back ───────────────────────────────────────────

def test_trace_archive_roundtrip(client):
    pid = _create_project(client)
    for outcome in ("success", "failed", "timed_out"):
        r = client.post(
            "/traces",
            json={"records": [{
                "project_id": pid, "intent_id": "i1", "worker_name": "w1",
                "task_type": "explore", "outcome": outcome,
                "started_at": 1.5, "duration_ms": 999,
            }]},
            headers=_h(),
        )
        assert r.status_code == 204, r.text
    rows = client.get(f"/projects/{pid}/traces", headers=_h()).json()
    assert len(rows) == 3
    assert [r["outcome"] for r in rows] == ["timed_out", "failed", "success"]  # newest first
    assert rows[0]["duration_ms"] == 999
    assert rows[0]["worker_name"] == "w1"


def test_trace_list_limit_bounded(client):
    pid = _create_project(client)
    for i in range(5):
        client.post(
            "/traces",
            json={"records": [{
                "project_id": pid, "intent_id": "i1", "worker_name": "w1",
                "task_type": "explore", "outcome": "success",
                "started_at": float(i), "duration_ms": i,
            }]},
            headers=_h(),
        )
    rows = client.get(f"/projects/{pid}/traces?limit=2", headers=_h()).json()
    assert len(rows) == 2
    # over-requested limit is clamped, not an error
    big = client.get(f"/projects/{pid}/traces?limit=999", headers=_h()).json()
    assert len(big) == 5


def _create_project(client) -> str:
    r = client.post(
        "/projects",
        json={"title": "t", "origin": "http://target.example", "goal": "prove it"},
        headers=_h(),
    )
    assert r.status_code == 201, r.text
    return r.json()["project"]["id"]