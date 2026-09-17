"""任务模式（pentest / scored）测试。

**定位背景**：旗帜与记分是为一次评测（TSec Benchmark）长出来的能力，不该固化成
工具的一等公民。收敛方案 = **项目级可选模式**：

- `pentest`（默认）：谈安全发现；界面无记分语义；prompt 明确禁止 `kind`/`score`
- `scored`：评测/CTF 项目；暴露旗帜与记分板；prompt 注入 flag 输出规范

本文件锁住三件事：字段与 API 行为、默认值向后兼容、以及**prompt 渲染不留占位符**
（漏注入会让 worker 读到 `{scoring_note}` 字面量，属于静默故障）。
"""

from __future__ import annotations

import pytest

from sharp.dispatcher.prompting import (
    PENTEST_SCORING_NOTE,
    SCORED_SCORING_NOTE,
    load_prompt,
    render_prompt,
    scoring_note,
)
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


def _create(client, **overrides) -> dict:
    body = {"title": "t", "origin": "http://target.example", "goal": "prove it"}
    body.update(overrides)
    r = client.post("/projects", json=body, headers=_h())
    assert r.status_code == 201, r.text
    return r.json()


# ── 默认与创建 ────────────────────────────────────────────────────────────────

def test_new_project_defaults_to_pentest(client):
    """默认必须是渗透模式 —— 绝大多数项目不是评测，不该看到记分语义。"""
    detail = _create(client)
    assert detail["project"]["task_mode"] == "pentest"


def test_can_create_scored_project(client):
    assert _create(client, task_mode="scored")["project"]["task_mode"] == "scored"


def test_create_rejects_unknown_task_mode(client):
    r = client.post(
        "/projects",
        json={"title": "t", "origin": "o", "goal": "g", "task_mode": "ctf"},
        headers=_h(),
    )
    assert r.status_code == 422


def test_task_mode_visible_in_project_list(client):
    _create(client, title="渗透的")
    _create(client, title="评测的", task_mode="scored")
    rows = client.get("/projects", headers=_h()).json()
    by_title = {r["title"]: r["task_mode"] for r in rows}
    assert by_title == {"渗透的": "pentest", "评测的": "scored"}


# ── 切换 ──────────────────────────────────────────────────────────────────────

def test_switch_task_mode(client):
    pid = _create(client)["project"]["id"]
    r = client.put(f"/projects/{pid}/task-mode", json={"task_mode": "scored"}, headers=_h())
    assert r.status_code == 200
    assert r.json()["task_mode"] == "scored"
    assert client.get(f"/projects/{pid}", headers=_h()).json()["project"]["task_mode"] == "scored"

    r = client.put(f"/projects/{pid}/task-mode", json={"task_mode": "pentest"}, headers=_h())
    assert r.json()["task_mode"] == "pentest"


def test_switch_task_mode_rejects_unknown_value(client):
    pid = _create(client)["project"]["id"]
    r = client.put(f"/projects/{pid}/task-mode", json={"task_mode": "benchmark"}, headers=_h())
    assert r.status_code == 422


def test_switch_task_mode_unknown_project_404(client):
    r = client.put("/projects/proj_nope/task-mode", json={"task_mode": "scored"}, headers=_h())
    assert r.status_code == 404


# ── 提示词渲染 ────────────────────────────────────────────────────────────────

def test_scoring_note_differs_by_mode():
    assert scoring_note("scored") == SCORED_SCORING_NOTE
    assert scoring_note("pentest") == PENTEST_SCORING_NOTE
    assert scoring_note(None) == PENTEST_SCORING_NOTE
    assert scoring_note("whatever") == PENTEST_SCORING_NOTE  # 未知值当作默认
    assert "flag" in SCORED_SCORING_NOTE
    assert "do NOT emit" in PENTEST_SCORING_NOTE


@pytest.mark.parametrize("mode", ["pentest", "scored"])
@pytest.mark.parametrize("name", ["explore.md", "bootstrap.md"])
def test_render_leaves_no_placeholder(name, mode):
    """漏注入会让 worker 读到 `{scoring_note}` 字面量 —— 渲染后必须无残留。"""
    template = load_prompt("default", name)
    assert "{scoring_note}" in template, f"{name} 应使用 scoring_note 占位符"
    text = render_prompt(template, {"scoring_note": scoring_note(mode), "graph_yaml": "{}",
                                    "intent_id": "i001", "intent_description": "d",
                                    "goal": "g", "origin": "o", "hints": "[]"})
    assert "{scoring_note}" not in text


def test_pentest_prompt_forbids_score_fields():
    text = render_prompt(load_prompt("default", "explore.md"), {"scoring_note": scoring_note("pentest")})
    assert "do NOT emit" in text
    assert "scoreboard sums them" not in text


def test_scored_prompt_includes_flag_shape():
    text = render_prompt(load_prompt("default", "explore.md"), {"scoring_note": scoring_note("scored")})
    assert '"kind": "flag"' in text
    assert "scoreboard" in text
