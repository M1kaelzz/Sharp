"""紧急模式放行审计与事后复核测试（P1-5）。

背景：急模式下的高危行动**在创建时就自动批准**（不进入 pending），所以人工的介入点
不是"审批"而是"**事后复核**"。此前的问题是：开关有审计，但**逐条放行只有 intent 上的
一个 note 字段，`approval_events` 里查不到**——与 ADR-0011 承诺的"旁路的是人工确认，
不是记录"不完全相符。

本文件锁定三件事：
1. 自动放行**必须**写 `emergency_release` 审计事件；
2. 放行清单能列出这些动作并反映复核状态（含旧数据的兼容路径）；
3. 复核端点把"人工看过之后的判定"落成 `emergency_review` 事件。
"""

from __future__ import annotations

import pytest

from sharp.server import auth, db

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


def _server() -> dict[str, str]:
    return {"Authorization": f"Bearer {SERVER_TOKEN}"}


def _human() -> dict[str, str]:
    return {"Authorization": f"Bearer {auth.sign_token(auth.get_jwt_key())}"}


@pytest.fixture
def project(client) -> str:
    r = client.post("/projects", json={"title": "t", "origin": "http://t.example", "goal": "g"},
                    headers=_server())
    assert r.status_code == 201, r.text
    return r.json()["project"]["id"]


def _open_emergency(client, pid: str, hours: int = 24):
    r = client.post(f"/projects/{pid}/emergency-mode",
                    json={"enabled": True, "reason": "长任务连续执行，人工改为事后复核", "hours": hours},
                    headers=_human())
    assert r.status_code == 200, r.text


def _high_risk_intent(client, pid: str, description: str = "写入 webshell 获取权限") -> dict:
    r = client.post(f"/projects/{pid}/intents", headers=_server(), json={
        "from": ["origin"], "description": description,
        "creator": "ai_worker", "worker": "ai_worker",
    })
    assert r.status_code == 201, r.text
    return r.json()


# ── 审计留痕 ──────────────────────────────────────────────────────────────────

def test_emergency_release_writes_audit_event(client, project):
    _open_emergency(client, project)
    intent = _high_risk_intent(client, project)
    assert intent["approval_status"] == "none"
    assert intent["approval_note"] == "紧急模式自动放行"

    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT action, note FROM approval_events WHERE project_id = ? AND intent_id = ?",
            (project, intent["id"]),
        ).fetchall()
    actions = [r["action"] for r in rows]
    assert "emergency_release" in actions, "自动放行必须写审计事件（ADR-0011：旁路的不是记录）"
    note = next(r["note"] for r in rows if r["action"] == "emergency_release")
    assert "紧急模式自动放行" in note and "风险等级" in note


def test_normal_high_risk_intent_still_pends(client, project):
    """没开急模式时，高危行动照旧进 pending（闸门未被削弱）。"""
    intent = _high_risk_intent(client, project)
    assert intent["approval_status"] == "pending"
    with db.get_conn() as conn:
        actions = [r["action"] for r in conn.execute(
            "SELECT action FROM approval_events WHERE project_id = ? AND intent_id = ?",
            (project, intent["id"])).fetchall()]
    assert actions == ["submitted"]


def test_low_risk_intent_has_no_release_event(client, project):
    _open_emergency(client, project)
    intent = _high_risk_intent(client, project, description="收集公开信息并整理接口清单")
    assert intent["approval_status"] == "none"
    assert intent["approval_note"] == ""
    with db.get_conn() as conn:
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM approval_events WHERE project_id = ? AND action='emergency_release'",
            (project,)).fetchone()["n"]
    assert n == 0, "低危动作不写放行事件"


# ── 放行清单 ──────────────────────────────────────────────────────────────────

def test_release_list_shows_auto_released_intent(client, project):
    _open_emergency(client, project)
    intent = _high_risk_intent(client, project)
    rows = client.get(f"/projects/{project}/emergency-releases", headers=_human()).json()
    assert len(rows) == 1
    row = rows[0]
    assert row["intent_id"] == intent["id"]
    assert row["risk_level"] in ("high", "critical")
    assert row["reviewed"] is False
    assert "紧急模式自动放行" in row["release_note"]


def test_release_list_excludes_pending_intents(client, project):
    _high_risk_intent(client, project)          # pending（未开急模式）
    _open_emergency(client, project)
    _high_risk_intent(client, project, description="写入 webshell 获取权限")
    rows = client.get(f"/projects/{project}/emergency-releases", headers=_human()).json()
    assert len(rows) == 1, "只列自动放行的，不列待审批的"


def test_legacy_release_without_event_is_included(client, project):
    """兼容旧数据：只有 approval_note 标注、没有审计事件的放行动作也要列出来。"""
    _open_emergency(client, project)
    intent = _high_risk_intent(client, project)
    with db.get_conn() as conn:
        conn.execute("DELETE FROM approval_events WHERE action = 'emergency_release'")
    rows = client.get(f"/projects/{project}/emergency-releases", headers=_human()).json()
    assert [r["intent_id"] for r in rows] == [intent["id"]]
    assert "历史数据" in rows[0]["release_note"]


# ── 事后复核 ──────────────────────────────────────────────────────────────────

def test_review_marks_release_reviewed(client, project):
    _open_emergency(client, project)
    intent = _high_risk_intent(client, project)
    r = client.post(f"/projects/{project}/intents/{intent['id']}/emergency-review",
                    json={"verdict": "ok", "note": "已核对：仅验证了登录绕过，未改动数据"},
                    headers=_human())
    assert r.status_code == 200, r.text
    assert r.json()["reviewed"] is True

    rows = client.get(f"/projects/{project}/emergency-releases", headers=_human()).json()
    assert rows[0]["reviewed"] is True and rows[0]["verdict"] == "ok"


def test_unreviewed_only_filter(client, project):
    _open_emergency(client, project)
    first = _high_risk_intent(client, project)
    _high_risk_intent(client, project, description="进行 SSRF 探测内网服务")
    client.post(f"/projects/{project}/intents/{first['id']}/emergency-review",
                json={"verdict": "follow_up", "note": "需补一次影响面确认"}, headers=_human())
    all_rows = client.get(f"/projects/{project}/emergency-releases", headers=_human()).json()
    open_rows = client.get(f"/projects/{project}/emergency-releases?unreviewed_only=true",
                           headers=_human()).json()
    assert len(all_rows) == 2 and len(open_rows) == 1


def test_review_unknown_intent_404(client, project):
    r = client.post(f"/projects/{project}/intents/i999/emergency-review",
                    json={"verdict": "ok", "note": ""}, headers=_human())
    assert r.status_code == 404


# ── JWT-only（AI 不能自己复核自己的放行动作）────────────────────────────────

def test_release_endpoints_reject_server_token(client, project):
    _open_emergency(client, project)
    intent = _high_risk_intent(client, project)
    assert client.get(f"/projects/{project}/emergency-releases", headers=_server()).status_code == 403
    assert client.post(f"/projects/{project}/intents/{intent['id']}/emergency-review",
                       json={"verdict": "ok", "note": ""}, headers=_server()).status_code == 403
