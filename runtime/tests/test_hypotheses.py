"""未验证假设测试（P0-3）。

真实渗透推进的本来逻辑是**假设驱动**：观察 → 可证伪的猜想 → 验证 → 新猜想。
本文件锁定三件事：

1. 假设是**一等对象**（可创建、可结算、可计数、可按未结算过滤）；
2. **结算必须有依据**——`confirmed` 必须指向证据，`refuted` 必须写清试过什么
   （否定的结论同样是积累，不能空着）；
3. 提示词里 `{hypotheses}` 占位符不漏（漏了规划器会读到字面量）。
"""

from __future__ import annotations

import pytest

from sharp.dispatcher.prompting import load_prompt, render_prompt
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
    r = client.post("/projects", json={"title": "t", "origin": "http://t.example", "goal": "g"}, headers=_h())
    assert r.status_code == 201, r.text
    return r.json()["project"]["id"]


def _add(client, pid: str, statement: str = "若 /api/user 只校验 JWT 不校验归属，则可用 A 的 token 读 B 的数据", **kw) -> dict:
    body = {"statement": statement, **kw}
    r = client.post(f"/projects/{pid}/hypotheses", json=body, headers=_h())
    assert r.status_code == 201, r.text
    return r.json()


# ── 创建与列表 ────────────────────────────────────────────────────────────────

def test_create_hypothesis_defaults_to_open(client, project):
    h = _add(client, project)
    assert h["status"] == "open" and h["id"] == "h001"
    assert h["result_fact_id"] is None and h["concluded_at"] is None


def test_per_project_counter(client, project):
    assert _add(client, project)["id"] == "h001"
    assert _add(client, project)["id"] == "h002"


def test_premise_facts_roundtrip(client, project):
    h = _add(client, project, premise_fact_ids=["f001", "f002"])
    assert h["premise_fact_ids"] == ["f001", "f002"]


def test_blank_statement_rejected(client, project):
    r = client.post(f"/projects/{project}/hypotheses", json={"statement": "   "}, headers=_h())
    assert r.status_code == 422


def test_only_open_filter(client, project):
    h1 = _add(client, project)
    _add(client, project)
    client.patch(f"/projects/{project}/hypotheses/{h1['id']}",
                 json={"status": "refuted", "note": "试过 3 种编码都未命中"}, headers=_h())
    all_rows = client.get(f"/projects/{project}/hypotheses", headers=_h()).json()
    open_rows = client.get(f"/projects/{project}/hypotheses?only_open=true", headers=_h()).json()
    assert len(all_rows) == 2 and len(open_rows) == 1


def test_unknown_project_and_hypothesis(client, project):
    assert client.get("/projects/proj_nope/hypotheses", headers=_h()).status_code == 404
    assert client.patch(f"/projects/{project}/hypotheses/h999", json={"status": "open"}, headers=_h()).status_code == 404


# ── 状态流转与结算依据 ────────────────────────────────────────────────────────

def test_mark_testing(client, project):
    h = _add(client, project)
    r = client.patch(f"/projects/{project}/hypotheses/{h['id']}", json={"status": "testing"}, headers=_h())
    assert r.json()["status"] == "testing" and r.json()["concluded_at"] is None


def test_confirmed_requires_evidence(client, project):
    """成立必须有证据指向——否则"确认"只是又一次声称。"""
    h = _add(client, project)
    r = client.patch(f"/projects/{project}/hypotheses/{h['id']}", json={"status": "confirmed"}, headers=_h())
    assert r.status_code == 422
    assert "result_fact_id" in r.json()["detail"]


def test_refuted_requires_note(client, project):
    """否定必须写清试过什么——否定的结论同样是积累。"""
    h = _add(client, project)
    r = client.patch(f"/projects/{project}/hypotheses/{h['id']}", json={"status": "refuted"}, headers=_h())
    assert r.status_code == 422
    assert "note" in r.json()["detail"]


def test_confirmed_stamps_concluded_at(client, project):
    h = _add(client, project)
    r = client.patch(f"/projects/{project}/hypotheses/{h['id']}",
                     json={"status": "confirmed", "result_fact_id": "f001", "note": "已复现"},
                     headers=_h())
    assert r.status_code == 200
    assert r.json()["status"] == "confirmed" and r.json()["concluded_at"]


def test_refuted_with_note_is_accepted(client, project):
    h = _add(client, project)
    r = client.patch(f"/projects/{project}/hypotheses/{h['id']}",
                     json={"status": "refuted", "note": "参数化查询已生效，注入不成立"}, headers=_h())
    assert r.json()["status"] == "refuted" and r.json()["concluded_at"]


def test_delete_for_human_cleanup(client, project):
    h = _add(client, project)
    assert client.delete(f"/projects/{project}/hypotheses/{h['id']}", headers=_h()).status_code == 204
    assert client.get(f"/projects/{project}/hypotheses", headers=_h()).json() == []


# ── 提示词 ────────────────────────────────────────────────────────────────────

def test_reason_prompt_has_placeholder():
    template = load_prompt("default", "reason.md")
    assert "{hypotheses}" in template
    assert "falsifiable" in template, "必须要求假设可证伪"


def test_reason_render_leaves_no_placeholder():
    text = render_prompt(load_prompt("default", "reason.md"), {
        "hypotheses": "- [h001] (open) 若 X 则 Y",
        "graph_yaml": "{}", "open_intents": "[]", "hints": "[]", "phase_context": "",
        "sub_goals": "", "scoring_note": "", "deadline_context": "", "env_baseline": "",
    })
    assert "{hypotheses}" not in text
