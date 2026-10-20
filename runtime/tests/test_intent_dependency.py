"""P0-1 attack-chain dependency gate tests (batch: P0-1).

Locks three layers:

- **dispatcher gate** (`_intent_dependencies_satisfied`): OFF keeps the legacy
  pass-through (an intent dispatches regardless of its depends_on); ON blocks
  an intent whose depends_on facts are not all present, and lets everything
  else through (no depends_on, or all satisfied).
- **server create**: `depends_on` may reference facts that do not exist yet
  (that is the whole point — a chain step waiting on its predecessor's
  evidence), while `from` keeps its existing existence check untouched. The
  goal itself can never be a prerequisite. Round-trips through the project
  detail endpoint.
- Legacy requests without `depends_on` behave exactly as before (empty list).

The loop is built via DispatcherLoop.__new__ with only the attributes each
method touches (same style as test_bootstrap_stall.py). Server tests use a
temp DB + TestClient.
"""

from __future__ import annotations

import types

import pytest

from sharp.dispatcher.scheduler.loop import DispatcherLoop
from sharp.server import db
from sharp.server.models import Fact, Intent, ProjectDetail, ProjectMeta

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


def _create_project(client) -> str:
    r = client.post(
        "/projects",
        json={"title": "t", "origin": "http://target.example", "goal": "prove it"},
        headers=_h(),
    )
    assert r.status_code == 201, r.text
    return r.json()["project"]["id"]


def _intent(**over) -> Intent:
    base = dict(
        id="i001",
        project_id="p1",
        from_=["origin"],
        to_fact_id=None,
        description="d",
        creator="dispatcher.reason",
        worker=None,
        created_at=TS,
    )
    base.update(over)
    return Intent(**base)


def _detail(fact_ids=("origin", "goal"), intents=()) -> ProjectDetail:
    meta = ProjectMeta(id="p1", title="p1", status="active", created_at=TS)
    return ProjectDetail(
        project=meta,
        facts=[Fact(id=fid, description=fid) for fid in fact_ids],
        intents=list(intents),
        hints=[],
    )


def _make_loop(gate: bool) -> DispatcherLoop:
    loop = DispatcherLoop.__new__(DispatcherLoop)
    loop.config = types.SimpleNamespace(
        runtime=types.SimpleNamespace(intent_dependency_gate=gate)
    )
    # Watchdog state (audit fix B3) used by the satisfied/blocked paths.
    loop._dep_blocked = {}
    loop._dep_hinted = set()
    loop.client = HintRecordingClient()
    return loop


# ── dispatcher gate: OFF keeps legacy pass-through ────────────────────────

def test_dependency_gate_disabled_passes_through_missing_deps():
    loop = _make_loop(gate=False)
    intent = _intent(depends_on=["f999"])  # dependency does not exist
    assert loop._intent_dependencies_satisfied(_detail(), intent) is True


# ── dispatcher gate: ON semantics ─────────────────────────────────────────

def test_dependency_gate_enabled_no_depends_on_passes():
    loop = _make_loop(gate=True)
    assert loop._intent_dependencies_satisfied(_detail(), _intent()) is True


def test_dependency_gate_enabled_all_satisfied_passes():
    loop = _make_loop(gate=True)
    intent = _intent(depends_on=["f001"])
    detail = _detail(fact_ids=("origin", "goal", "f001"))
    assert loop._intent_dependencies_satisfied(detail, intent) is True


def test_dependency_gate_enabled_missing_blocks():
    loop = _make_loop(gate=True)
    intent = _intent(depends_on=["f999"])
    assert loop._intent_dependencies_satisfied(_detail(), intent) is False


def test_dependency_gate_enabled_partial_missing_blocks():
    loop = _make_loop(gate=True)
    intent = _intent(depends_on=["f001", "f999"])
    detail = _detail(fact_ids=("origin", "goal", "f001"))
    assert loop._intent_dependencies_satisfied(detail, intent) is False


# ── server: create with depends_on referencing a future fact ──────────────

def test_create_intent_with_depends_on_future_fact(client):
    pid = _create_project(client)
    r = client.post(
        f"/projects/{pid}/intents",
        json={
            "from": ["origin"],
            "description": "verify chain step on f003",
            "creator": "dispatcher.reason",
            "depends_on": ["f003"],  # not produced yet — must be allowed
        },
        headers=_h(),
    )
    assert r.status_code == 201, r.text
    assert r.json()["depends_on"] == ["f003"]


def test_create_intent_without_depends_on_defaults_empty(client):
    pid = _create_project(client)
    r = client.post(
        f"/projects/{pid}/intents",
        json={"from": ["origin"], "description": "plain", "creator": "dispatcher.reason"},
        headers=_h(),
    )
    assert r.status_code == 201, r.text
    assert r.json()["depends_on"] == []


def test_create_intent_depends_on_goal_rejected(client):
    pid = _create_project(client)
    r = client.post(
        f"/projects/{pid}/intents",
        json={
            "from": ["origin"],
            "description": "x",
            "creator": "dispatcher.reason",
            "depends_on": ["goal"],  # goal can never be a prerequisite
        },
        headers=_h(),
    )
    assert r.status_code == 400


def test_depends_on_roundtrip_in_project_detail(client):
    pid = _create_project(client)
    r = client.post(
        f"/projects/{pid}/intents",
        json={
            "from": ["origin"],
            "description": "chain step",
            "creator": "dispatcher.reason",
            "depends_on": ["f999", "f001"],
        },
        headers=_h(),
    )
    assert r.status_code == 201, r.text
    detail = client.get(f"/projects/{pid}", headers=_h()).json()
    matches = [
        i for i in detail["intents"]
        if i["description"] == "chain step" and i["depends_on"] == ["f999", "f001"]
    ]
    assert matches, f"chain step intent not round-tripped: {detail['intents']}"


# ── server: from still enforces existence (unchanged baseline) ────────────

def test_create_intent_from_missing_fact_still_rejected(client):
    pid = _create_project(client)
    r = client.post(
        f"/projects/{pid}/intents",
        json={
            "from": ["f404"],  # does not exist -> must stay rejected (baseline)
            "description": "x",
            "creator": "dispatcher.reason",
        },
        headers=_h(),
    )
    assert r.status_code == 404

# ── B3 (audit fix): dependency-block watchdog ──────────────────────────────

class HintRecordingClient:
    """Fake SharpClient recording create_hint calls."""

    def __init__(self):
        self.hints: list[tuple[str, str, str]] = []

    def create_hint(self, project_id, content, creator):
        self.hints.append((project_id, content, creator))
        return types.SimpleNamespace(ok=True)

    def create_hint_failing(self, project_id, content, creator):
        raise RuntimeError("server down")

    def create_hint_rejected(self, project_id, content, creator):
        return types.SimpleNamespace(ok=False)


def _watch_loop(gate: bool, client) -> DispatcherLoop:
    loop = DispatcherLoop.__new__(DispatcherLoop)
    loop.config = types.SimpleNamespace(
        runtime=types.SimpleNamespace(intent_dependency_gate=gate)
    )
    loop.client = client
    loop._dep_blocked = {}
    loop._dep_hinted = set()
    return loop


def test_watchdog_arms_then_hints_once_after_timeout(monkeypatch):
    from sharp.dispatcher.scheduler import loop as loop_mod

    client = HintRecordingClient()
    lp = _watch_loop(True, client)
    intent = _intent(depends_on=["f999"])
    det = _detail()  # f999 does not exist

    # First block: arms the timer, no hint yet.
    assert lp._intent_dependencies_satisfied(det, intent) is False
    assert client.hints == []

    # Same block after the watchdog window: one hint, exactly once.
    monkeypatch.setattr(loop_mod, "DEP_WATCHDOG_SECONDS", 0)
    assert lp._intent_dependencies_satisfied(det, intent) is False
    assert len(client.hints) == 1
    pid, content, creator = client.hints[0]
    assert pid == "p1"
    assert "f999" in content and intent.id in content
    assert creator == "dispatcher.stall_detector"

    # Further ticks: one-shot, no duplicates.
    assert lp._intent_dependencies_satisfied(det, intent) is False
    assert len(client.hints) == 1


def test_watchdog_no_hint_before_timeout(monkeypatch):
    client = HintRecordingClient()
    lp = _watch_loop(True, client)
    intent = _intent(depends_on=["f999"])
    det = _detail()
    # Window still open (60s default, ticks are far apart in wall time):
    assert lp._intent_dependencies_satisfied(det, intent) is False
    # A second immediate tick is still within the window.
    monkeypatch.setattr(
        "sharp.dispatcher.scheduler.loop.DEP_WATCHDOG_SECONDS", 3600
    )
    assert lp._intent_dependencies_satisfied(det, intent) is False
    assert client.hints == []


def test_watchdog_cleared_when_deps_satisfied(monkeypatch):
    from sharp.dispatcher.scheduler import loop as loop_mod

    client = HintRecordingClient()
    lp = _watch_loop(True, client)
    intent = _intent(depends_on=["f999"])
    blocked = _detail()
    assert lp._intent_dependencies_satisfied(blocked, intent) is False

    # Producer concluded: f999 exists now -> satisfied, state cleared.
    satisfied = _detail(fact_ids=("origin", "goal", "f999"))
    assert lp._intent_dependencies_satisfied(satisfied, intent) is True
    assert lp._dep_blocked == {} and lp._dep_hinted == set()

    # Re-block after satisfaction arms a FRESH timer (no immediate hint even
    # at threshold 0 — evidence the state was really cleared).
    monkeypatch.setattr(loop_mod, "DEP_WATCHDOG_SECONDS", 0)
    assert lp._intent_dependencies_satisfied(blocked, intent) is False
    assert client.hints == []


def test_watchdog_rearms_when_missing_set_changes(monkeypatch):
    from sharp.dispatcher.scheduler import loop as loop_mod

    client = HintRecordingClient()
    lp = _watch_loop(True, client)
    monkeypatch.setattr(loop_mod, "DEP_WATCHDOG_SECONDS", 0)
    det = _detail()

    # Block on f999 -> armed -> fires.
    assert lp._intent_dependencies_satisfied(det, _intent(depends_on=["f999"])) is False
    assert lp._intent_dependencies_satisfied(det, _intent(depends_on=["f999"])) is False
    assert len(client.hints) == 1

    # Different missing set (f998): fresh timer, no instant re-fire.
    assert lp._intent_dependencies_satisfied(det, _intent(depends_on=["f998"])) is False
    assert len(client.hints) == 1


def test_watchdog_gate_off_never_touches_state():
    client = HintRecordingClient()
    lp = _watch_loop(False, client)  # legacy: gate off
    intent = _intent(depends_on=["f999"])
    assert lp._intent_dependencies_satisfied(_detail(), intent) is True
    assert client.hints == []
    assert lp._dep_blocked == {} and lp._dep_hinted == set()


def test_watchdog_hint_failure_swallowed(monkeypatch):
    from sharp.dispatcher.scheduler import loop as loop_mod

    class FailingHintClient(HintRecordingClient):
        def create_hint(self, project_id, content, creator):
            raise RuntimeError("server down")

    client = FailingHintClient()
    lp = _watch_loop(True, client)
    monkeypatch.setattr(loop_mod, "DEP_WATCHDOG_SECONDS", 0)
    det = _detail()
    intent = _intent(depends_on=["f999"])
    assert lp._intent_dependencies_satisfied(det, intent) is False  # armed
    # Firing with a failing client must not raise into the dispatch path.
    assert lp._intent_dependencies_satisfied(det, intent) is False
    # Not marked hinted (retry next window).
    assert lp._dep_hinted == set()
