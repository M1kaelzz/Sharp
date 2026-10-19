"""环境基线测试（P0-2）。

基线 = 已确认的**作业前提**（连通性、凭据有效性、可达性、工具可用性），跨会话复用。
它要解决的是实测出来的浪费：同一条网络预检在一次长任务里被重复执行了 144 次——每轮
行动都是独立会话，上下文从零构建，"已经确认过的事"被反复支付。

覆盖三件事：API 的写入/幂等/边界、提示词渲染、以及**占位符不许漏进 prompt**（漏了
worker 会读到 `{env_baseline}` 字面量，属于静默故障）。
"""

from __future__ import annotations

import pytest

from sharp.dispatcher.prompting import format_env_baseline, load_prompt, render_prompt
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


# ── API ──────────────────────────────────────────────────────────────────────

def test_empty_baseline_initially(client, project):
    r = client.get(f"/projects/{project}/baseline", headers=_h())
    assert r.status_code == 200 and r.json() == []


def test_upsert_and_list(client, project):
    r = client.put(f"/projects/{project}/baseline", headers=_h(), json={"entries": [
        {"key": "network.vpn", "value": "ok", "note": "10.0.100.58 → status:ok"},
        {"key": "platform.token", "value": "valid"},
    ]})
    assert r.status_code == 200, r.text
    keys = {e["key"] for e in r.json()}
    assert keys == {"network.vpn", "platform.token"}


def test_upsert_is_idempotent_and_updates(client, project):
    client.put(f"/projects/{project}/baseline", headers=_h(),
               json={"entries": [{"key": "network.vpn", "value": "unreachable"}]})
    client.put(f"/projects/{project}/baseline", headers=_h(),
               json={"entries": [{"key": "network.vpn", "value": "ok", "note": "重连后恢复"}]})
    rows = client.get(f"/projects/{project}/baseline", headers=_h()).json()
    assert len(rows) == 1
    assert rows[0]["value"] == "ok" and rows[0]["note"] == "重连后恢复"


def test_blank_key_rejected(client, project):
    r = client.put(f"/projects/{project}/baseline", headers=_h(),
                   json={"entries": [{"key": "   ", "value": "x"}]})
    assert r.status_code == 422


def test_oversized_value_rejected(client, project):
    r = client.put(f"/projects/{project}/baseline", headers=_h(),
                   json={"entries": [{"key": "k", "value": "x" * 5000}]})
    assert r.status_code == 422


def test_delete_entry(client, project):
    client.put(f"/projects/{project}/baseline", headers=_h(),
               json={"entries": [{"key": "tmp", "value": "1"}]})
    assert client.delete(f"/projects/{project}/baseline/tmp", headers=_h()).status_code == 204
    assert client.get(f"/projects/{project}/baseline", headers=_h()).json() == []


def test_unknown_project_404(client):
    assert client.get("/projects/proj_nope/baseline", headers=_h()).status_code == 404


def test_baseline_is_per_project(client, project):
    other = client.post("/projects", json={"title": "o", "origin": "http://o.example", "goal": "g"},
                        headers=_h()).json()["project"]["id"]
    client.put(f"/projects/{project}/baseline", headers=_h(),
               json={"entries": [{"key": "k", "value": "v"}]})
    assert client.get(f"/projects/{other}/baseline", headers=_h()).json() == []


# ── 提示词渲染 ────────────────────────────────────────────────────────────────

def test_empty_baseline_prompts_to_create_one():
    text = format_env_baseline([])
    assert "尚未建立环境基线" in text
    assert "env_facts" in text, "空基线必须告诉 worker 怎么上报（随结论输出 env_facts）"
    assert "不要尝试自己调用 Sharp API" in text, "worker 无法直连 API，必须明确禁止"


def test_baseline_rendered_with_note():
    text = format_env_baseline([{"key": "network.vpn", "value": "ok", "note": "已连通", "source": "worker", "updated_at": ""}])
    assert "- network.vpn: ok" in text and "已连通" in text


@pytest.mark.parametrize("name", ["explore.md", "bootstrap.md"])
def test_prompt_templates_use_placeholder(name):
    template = load_prompt("default", name)
    assert "{env_baseline}" in template, f"{name} 应包含 env_baseline 占位符"


@pytest.mark.parametrize("name", ["explore.md", "bootstrap.md"])
def test_render_leaves_no_placeholder(name):
    """漏注入会让 worker 读到 `{env_baseline}` 字面量——渲染后必须无残留。"""
    text = render_prompt(load_prompt("default", name), {
        "env_baseline": format_env_baseline([{"key": "k", "value": "v", "note": "", "source": "worker", "updated_at": ""}]),
        "graph_yaml": "{}", "intent_id": "i1", "intent_description": "d",
        "goal": "g", "origin": "o", "hints": "[]",
    })
    assert "{env_baseline}" not in text


# ── 跨项目继承（同目标不重复探测）───────────────────────────────────────────

def _two_projects_same_target(client):
    """两个项目打同一个目标（origin 都是自然语言句子，这是真实格式）。"""
    a = client.post("/projects", json={
        "title": "A", "origin": "目标地址：https://app.example.com/\n授权范围：仅该主域", "goal": "g",
    }, headers=_h()).json()["project"]["id"]
    b = client.post("/projects", json={
        "title": "B", "origin": "目标地址：https://app.example.com/\n授权范围：仅该主域", "goal": "g",
    }, headers=_h()).json()["project"]["id"]
    return a, b


def test_inherited_entries_are_opt_in(client):
    """默认不带继承：既有 API 语义不变（UI/人工看到的仍是本项目自己的基线）。"""
    a, b = _two_projects_same_target(client)
    client.put(f"/projects/{a}/baseline", headers=_h(),
               json={"entries": [{"key": "network.reachable", "value": "ok"}]})
    plain = client.get(f"/projects/{b}/baseline", headers=_h()).json()
    assert plain == [], "默认不该混入别的项目的条目"
    merged = client.get(f"/projects/{b}/baseline", params={"with_inherited": "true"}, headers=_h()).json()
    assert [e["key"] for e in merged] == ["network.reachable"]
    assert merged[0]["inherited"] is True
    assert merged[0]["source_project_id"] == a
    assert merged[0]["source_project_title"] == "A"


def test_local_entry_wins_over_inherited(client):
    """本地事实永远赢：本项目自己确认过的键不被继承值覆盖。"""
    a, b = _two_projects_same_target(client)
    client.put(f"/projects/{a}/baseline", headers=_h(),
               json={"entries": [{"key": "network.reachable", "value": "ok"}]})
    client.put(f"/projects/{b}/baseline", headers=_h(),
               json={"entries": [{"key": "network.reachable", "value": "timeout", "note": "本机不通"}]})
    merged = client.get(f"/projects/{b}/baseline", params={"with_inherited": "true"}, headers=_h()).json()
    assert len(merged) == 1 and merged[0]["value"] == "timeout" and merged[0]["inherited"] is False


def test_different_target_is_not_inherited(client):
    a_pid = client.post("/projects", json={
        "title": "A", "origin": "目标地址：https://app.example.com/", "goal": "g",
    }, headers=_h()).json()["project"]["id"]
    other = client.post("/projects", json={
        "title": "C", "origin": "目标地址：https://totally-other.example.org/", "goal": "g",
    }, headers=_h()).json()["project"]["id"]
    client.put(f"/projects/{a_pid}/baseline", headers=_h(),
               json={"entries": [{"key": "network.reachable", "value": "ok"}]})
    merged = client.get(f"/projects/{other}/baseline", params={"with_inherited": "true"}, headers=_h()).json()
    assert merged == [], "不同目标不得互相继承"


def test_latest_inherited_value_wins_across_siblings(client):
    """同目标有多个兄弟项目时，取最新确认的那条。"""
    a, b = _two_projects_same_target(client)
    c = client.post("/projects", json={
        "title": "C", "origin": "目标地址：https://app.example.com/", "goal": "g",
    }, headers=_h()).json()["project"]["id"]
    client.put(f"/projects/{a}/baseline", headers=_h(),
               json={"entries": [{"key": "tool.curl", "value": "missing"}]})
    client.put(f"/projects/{c}/baseline", headers=_h(),
               json={"entries": [{"key": "tool.curl", "value": "ok"}]})
    merged = client.get(f"/projects/{b}/baseline", params={"with_inherited": "true"}, headers=_h()).json()
    assert len(merged) == 1 and merged[0]["value"] == "ok"


def test_prompt_marks_inherited_entries_separately():
    """继承条目在提示词里必须单独成段并带来源 —— 否则 worker 会把"别处确认过"
    当成"这里已经确认过"。"""
    from sharp.dispatcher.prompting import format_env_baseline

    text = format_env_baseline([
        {"key": "network.reachable", "value": "ok", "note": "本机", "inherited": False},
        {"key": "tool.curl", "value": "ok", "note": "", "inherited": True,
         "source_project_id": "proj_002", "source_project_title": "Peplink 那次"},
    ])
    assert "- network.reachable: ok" in text
    assert "同目标的其他项目" in text
    assert "[来自 Peplink 那次]" in text


def test_empty_baseline_text_still_prompts(tmp_path):
    from sharp.dispatcher.prompting import format_env_baseline

    assert "尚未建立环境基线" in format_env_baseline([])
    assert "尚未建立环境基线" in format_env_baseline(None)
