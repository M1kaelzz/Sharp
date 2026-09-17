"""Sub goal tests (batch C): phase objectives CRUD, status transitions, per
project counters, and the planner-side sub_goals directives."""

from __future__ import annotations

import pytest

from sharp.server import db
from sharp.server.models import CreateSubGoalRequest, UpdateSubGoalRequest
from sharp.server.repository import sub_goals as sg_repo
from sharp.server.routers import sub_goals as sg_router
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
    yield


def test_create_list_and_ids_are_per_project(temp_db):
    a = sg_router.create_sub_goal("p1", CreateSubGoalRequest(title="拿到有效会话"))
    b = sg_router.create_sub_goal("p1", CreateSubGoalRequest(title="提权到 root"))
    assert (a.id, b.id) == ("sg001", "sg002")
    goals = sg_router.list_sub_goals("p1")
    assert [g.title for g in goals] == ["拿到有效会话", "提权到 root"]
    assert all(g.status == "pending" for g in goals)
    assert all(g.created_by == "human" for g in goals)


def test_status_transitions_stamp_concluded_at(temp_db):
    g = sg_router.create_sub_goal("p1", CreateSubGoalRequest(title="侦察"))
    active = sg_router.update_sub_goal("p1", g.id, UpdateSubGoalRequest(status="active"))
    assert active.status == "active" and active.concluded_at is None
    done = sg_router.update_sub_goal("p1", g.id, UpdateSubGoalRequest(status="done", note="资产清点完成"))
    assert done.status == "done" and done.concluded_at is not None
    assert done.note == "资产清点完成"


def test_abandon_records_note(temp_db):
    g = sg_router.create_sub_goal("p1", CreateSubGoalRequest(title="内网横向"))
    out = sg_router.update_sub_goal(
        "p1", g.id, UpdateSubGoalRequest(status="abandoned", note="无内网可达路径")
    )
    assert out.status == "abandoned"
    assert out.concluded_at is not None
    assert out.note == "无内网可达路径"


def test_unknown_sub_goal_404(temp_db):
    with pytest.raises(Exception) as exc:
        sg_router.update_sub_goal("p1", "sg999", UpdateSubGoalRequest(status="done"))
    assert exc.value.status_code == 404


def test_counts_helper(temp_db):
    a = sg_router.create_sub_goal("p1", CreateSubGoalRequest(title="A"))
    sg_router.create_sub_goal("p1", CreateSubGoalRequest(title="B"))
    sg_router.update_sub_goal("p1", a.id, UpdateSubGoalRequest(status="done"))
    with db.get_conn() as conn:
        counts = sg_repo.counts(conn, "p1")
    assert counts == {"done": 1, "pending": 1}


def test_planner_sub_goal_directives_call_client():
    from sharp.dispatcher.tasks.reason import _apply_sub_goal_actions

    calls: list[tuple] = []

    class FakeClient:
        def add_sub_goal(self, pid, title):
            calls.append(("add", pid, title))
            return type("R", (), {"ok": True, "status_code": 201})()

        def update_sub_goal(self, pid, sgid, status, note=""):
            calls.append(("update", pid, sgid, status, note))
            return type("R", (), {"ok": True, "status_code": 200})()

    payload = {
        "accepted": True,
        "data": {
            "sub_goals": {
                "add": [{"title": "拿到任意有效会话"}, {"title": "  "}, "字符串形式也算"],
                "update": [{"id": "sg001", "status": "done", "note": "ok"},
                           {"id": "sg002", "status": "bogus"},
                           {"status": "done"}],
            }
        },
    }
    _apply_sub_goal_actions(FakeClient(), "p1", "w1", payload)
    assert ("add", "p1", "拿到任意有效会话") in calls
    assert ("add", "p1", "字符串形式也算") in calls
    assert ("update", "p1", "sg001", "done", "ok") in calls
    # blank title / unknown status / missing id are skipped, never raised
    assert len([c for c in calls if c[0] == "add"]) == 2
    assert len([c for c in calls if c[0] == "update"]) == 1


def test_acceptance_check_includes_open_sub_goals(temp_db):
    """Batch C: uncompleted phases surface in the pre-completion inventory."""
    from sharp.server.routers import projects as projects_router

    g = sg_router.create_sub_goal("p1", CreateSubGoalRequest(title="拿到管理员会话"))
    chk = projects_router.acceptance_check("p1")
    assert chk.has_open_work is True
    assert [i.id for i in chk.open_sub_goals] == [g.id]
    sg_router.update_sub_goal("p1", g.id, UpdateSubGoalRequest(status="done"))
    chk2 = projects_router.acceptance_check("p1")
    assert chk2.open_sub_goals == []
    assert chk2.has_open_work is False
