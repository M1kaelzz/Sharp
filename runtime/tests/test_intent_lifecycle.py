"""Intent lifecycle tests (batch A): planner-driven abandon / priority, project
deadline, and the reason-side planning actions."""

from __future__ import annotations

import pytest

from sharp.server import db
from sharp.server.models import (
    AbandonIntentRequest,
    CreateIntentRequest,
    UpdateIntentPriorityRequest,
    UpdateProjectDeadlineRequest,
)
from sharp.server.routers import intents as intents_router
from sharp.server.routers import projects as projects_router
from sharp.server.services import utcnow


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_db_path", None, raising=False)
    db.configure(tmp_path / "sharp.db")
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO projects (id, title, status, created_at) VALUES ('p1','t','active',?)",
            (utcnow(),),
        )
        conn.execute(
            "INSERT INTO facts (id, project_id, description) VALUES ('origin','p1','o')",
        )
    yield


def _open_intent(desc: str = "probe endpoint") -> str:
    m = intents_router.create_intent(
        "p1", CreateIntentRequest(**{"from": ["origin"]}, description=desc,
                                 creator="w1", worker=None)
    )
    return m.id


def _row(intent_id: str) -> dict:
    with db.get_conn() as conn:
        r = conn.execute(
            "SELECT * FROM intents WHERE project_id='p1' AND id=?", (intent_id,)
        ).fetchone()
    return dict(r)


def test_abandon_open_intent(temp_db):
    iid = _open_intent()
    out = intents_router.abandon_intent("p1", iid, AbandonIntentRequest(reason="dead end"))
    assert out.abandoned_at is not None
    assert out.abandon_reason == "dead end"
    assert _row(iid)["abandoned_at"] is not None


def test_abandon_twice_conflicts(temp_db):
    iid = _open_intent()
    intents_router.abandon_intent("p1", iid, AbandonIntentRequest(reason="x"))
    with pytest.raises(Exception) as exc:
        intents_router.abandon_intent("p1", iid, AbandonIntentRequest(reason="y"))
    assert exc.value.status_code == 409


def test_cannot_abandon_claimed_intent(temp_db):
    iid = _open_intent()
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE intents SET worker='w1', last_heartbeat_at=? WHERE id=? AND project_id='p1'",
            (utcnow(), iid),
        )
    with pytest.raises(Exception) as exc:
        intents_router.abandon_intent("p1", iid, AbandonIntentRequest(reason="x"))
    assert exc.value.status_code == 409


def test_abandoned_intent_not_counted_as_unclaimed(temp_db):
    _open_intent("keep me")
    iid2 = _open_intent("retire me")
    intents_router.abandon_intent("p1", iid2, AbandonIntentRequest(reason="low value"))
    summaries = projects_router.list_projects()
    assert summaries[0].unclaimed_intent_count == 1  # only the kept one


def test_set_priority_and_conflict_after_conclusion(temp_db):
    iid = _open_intent()
    out = intents_router.set_intent_priority("p1", iid, UpdateIntentPriorityRequest(priority=25))
    assert out.priority == 25
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE intents SET to_fact_id='origin' WHERE id=? AND project_id='p1'", (iid,)
        )
    with pytest.raises(Exception) as exc:
        intents_router.set_intent_priority("p1", iid, UpdateIntentPriorityRequest(priority=5))
    assert exc.value.status_code == 409


def test_project_deadline_set_and_cleared(temp_db):
    meta = projects_router.update_project_deadline(
        "p1", UpdateProjectDeadlineRequest(deadline_at="2030-01-01T00:00:00Z")
    )
    assert meta.deadline_at == "2030-01-01T00:00:00Z"
    cleared = projects_router.update_project_deadline(
        "p1", UpdateProjectDeadlineRequest(deadline_at=None)
    )
    assert cleared.deadline_at is None


def test_project_deadline_rejects_bad_format(temp_db):
    with pytest.raises(Exception) as exc:
        projects_router.update_project_deadline(
            "p1", UpdateProjectDeadlineRequest(deadline_at="not-a-date")
        )
    assert exc.value.status_code == 422


def test_deadline_passed_helper():
    from sharp.dispatcher.scheduler.loop import _deadline_passed

    assert _deadline_passed("2000-01-01T00:00:00Z") is True
    assert _deadline_passed("2099-01-01T00:00:00Z") is False
    assert _deadline_passed("garbage") is False  # never block dispatch on bad input


def test_planning_actions_call_client():
    from sharp.dispatcher.tasks.reason import _apply_planning_actions

    calls: list[tuple] = []

    class FakeClient:
        def abandon_intent(self, pid, iid, reason):
            calls.append(("abandon", pid, iid, reason))
            return type("R", (), {"ok": True, "status_code": 200})()

        def set_intent_priority(self, pid, iid, priority):
            calls.append(("priority", pid, iid, priority))
            return type("R", (), {"ok": True, "status_code": 200})()

    payload = {
        "accepted": True,
        "data": {
            "intents": [],
            "abandon": [{"id": "i003", "reason": "dead end"}, {"no_id": True}],
            "prioritize": [{"id": "i002", "priority": 20}, {"id": "i004", "priority": "bad"}],
        },
    }
    _apply_planning_actions(FakeClient(), "p1", "w1", payload)
    assert ("abandon", "p1", "i003", "dead end") in calls
    assert ("priority", "p1", "i002", 20) in calls
    # malformed entries are skipped, not raised
    assert len(calls) == 2
