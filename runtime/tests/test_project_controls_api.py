"""项目控制面 API 测试（P1 覆盖率补强）。

`routers/projects.py` 此前 49% 覆盖，缺的正是这些"改变项目调度行为"的端点：
标题 / 状态 / 暂停 / 预算 / 截止时间 / 规划租约（reason claim-heartbeat-release）。

它们共同的安全与数据完整性语义：
- 截止时间必须是合法 ISO-8601，非法输入不能悄悄写库
- 规划租约是**排他**的：别人持有的租约不能被抢占，未持有者不能心跳/释放
- 预算不能为负

写这些测试是因为它们"错了不会立刻炸"，但会让调度行为与人工预期不符
（例如第二个规划器抢走租约导致重复派生行动）。
"""

from __future__ import annotations

import pytest

from sharp.server import db

SERVER_TOKEN = "server-token-xyz"


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


@pytest.fixture
def project(client) -> str:
    r = client.post(
        "/projects",
        json={"title": "t", "origin": "http://target.example", "goal": "prove it"},
        headers=_h(),
    )
    assert r.status_code == 201, r.text
    return r.json()["project"]["id"]


# ── 标题 / 状态 ───────────────────────────────────────────────────────────────

def test_update_title(client, project):
    r = client.put(f"/projects/{project}/title", json={"title": "改名后的项目"}, headers=_h())
    assert r.status_code == 200
    assert r.json()["title"] == "改名后的项目"


def test_update_title_rejects_blank(client, project):
    r = client.put(f"/projects/{project}/title", json={"title": "   "}, headers=_h())
    assert r.status_code == 422


def test_update_status_stops_project(client, project):
    r = client.put(f"/projects/{project}/status", json={"status": "stopped"}, headers=_h())
    assert r.status_code == 200
    assert r.json()["status"] == "stopped"


def test_update_status_rejects_unknown_value(client, project):
    r = client.put(f"/projects/{project}/status", json={"status": "paused"}, headers=_h())
    assert r.status_code == 422


# ── 暂停 / 预算 ───────────────────────────────────────────────────────────────

def test_pause_and_resume(client, project):
    assert client.put(f"/projects/{project}/paused", json={"paused": True}, headers=_h()).status_code == 200
    assert client.put(f"/projects/{project}/paused", json={"paused": False}, headers=_h()).status_code == 200


def test_budget_accepts_zero_and_rejects_negative(client, project):
    r = client.put(f"/projects/{project}/budget", json={"task_budget": 0}, headers=_h())
    assert r.status_code == 200
    assert client.put(f"/projects/{project}/budget", json={"task_budget": -1}, headers=_h()).status_code == 422


# ── 截止时间 ──────────────────────────────────────────────────────────────────

def test_set_deadline_accepts_iso8601(client, project):
    r = client.put(
        f"/projects/{project}/deadline",
        json={"deadline_at": "2026-09-10T09:00:00Z"},
        headers=_h(),
    )
    assert r.status_code == 200
    assert r.json()["deadline_at"].startswith("2026-09-10T09:00")


def test_set_deadline_rejects_garbage(client, project):
    """非法时间不能被静默接受 —— 否则"到期停止派发"会永远不触发。"""
    r = client.put(f"/projects/{project}/deadline", json={"deadline_at": "下周"}, headers=_h())
    assert r.status_code == 422
    assert "ISO-8601" in r.json()["detail"]


def test_deadline_can_be_cleared(client, project):
    client.put(f"/projects/{project}/deadline", json={"deadline_at": "2026-09-10T09:00:00Z"}, headers=_h())
    r = client.put(f"/projects/{project}/deadline", json={"deadline_at": None}, headers=_h())
    assert r.status_code == 200
    assert r.json()["deadline_at"] in (None, "")


# ── 规划租约（reason claim / heartbeat / release）──────────────────────────────

def test_reason_claim_then_heartbeat_and_release(client, project):
    claim = client.post(
        f"/projects/{project}/reason/claim",
        json={"worker": "dispatcher-a", "trigger": "tick"},
        headers=_h(),
    )
    assert claim.status_code == 200
    assert client.post(
        f"/projects/{project}/reason/heartbeat",
        json={"worker": "dispatcher-a"},
        headers=_h(),
    ).status_code == 200
    assert client.post(
        f"/projects/{project}/reason/release",
        json={"worker": "dispatcher-a"},
        headers=_h(),
    ).status_code == 200


def test_reason_claim_is_exclusive(client, project):
    """第二个 worker 不能抢走别人持有的租约（否则会重复派生行动）。"""
    assert client.post(
        f"/projects/{project}/reason/claim",
        json={"worker": "dispatcher-a", "trigger": "tick"},
        headers=_h(),
    ).status_code == 200
    r = client.post(
        f"/projects/{project}/reason/claim",
        json={"worker": "dispatcher-b", "trigger": "tick"},
        headers=_h(),
    )
    assert r.status_code == 409
    assert "dispatcher-a" in r.json()["detail"]


def test_heartbeat_without_claim_is_conflict(client, project):
    r = client.post(
        f"/projects/{project}/reason/heartbeat",
        json={"worker": "dispatcher-a"},
        headers=_h(),
    )
    assert r.status_code == 409
    assert "not currently claimed" in r.json()["detail"]


def test_heartbeat_by_other_worker_is_conflict(client, project):
    client.post(
        f"/projects/{project}/reason/claim",
        json={"worker": "dispatcher-a", "trigger": "tick"},
        headers=_h(),
    )
    r = client.post(
        f"/projects/{project}/reason/heartbeat",
        json={"worker": "dispatcher-b"},
        headers=_h(),
    )
    assert r.status_code == 409


def test_release_by_other_worker_is_conflict(client, project):
    client.post(
        f"/projects/{project}/reason/claim",
        json={"worker": "dispatcher-a", "trigger": "tick"},
        headers=_h(),
    )
    r = client.post(
        f"/projects/{project}/reason/release",
        json={"worker": "dispatcher-b"},
        headers=_h(),
    )
    assert r.status_code == 409


def test_release_without_claim_is_idempotent(client, project):
    """没有租约时释放应是无害的（重复调用不该报错）。"""
    assert client.post(
        f"/projects/{project}/reason/release",
        json={"worker": "dispatcher-a"},
        headers=_h(),
    ).status_code == 200


# ── 未知项目 ──────────────────────────────────────────────────────────────────

def test_unknown_project_returns_404(client):
    assert client.put("/projects/proj_nope/title", json={"title": "x"}, headers=_h()).status_code == 404
    assert client.put("/projects/proj_nope/budget", json={"task_budget": 1}, headers=_h()).status_code == 404
