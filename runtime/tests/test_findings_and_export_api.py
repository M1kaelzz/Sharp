"""产物（Finding）与项目导出 API 测试（P1 覆盖率补强）。

覆盖两条"数据完整性 / 数据外流"路径：

**产物**（`routers/vulnerabilities.py`，原 61%）——漏洞、旗帜、其他发现共用一张表，
  语义靠 `kind` 区分。要固定的是：默认仍是漏洞（老流程不受影响）、旗帜带分值、
  非法 kind 必须拒绝、指向不存在的证据必须 404（否则会出现"无来源的产物"）。
  记分板（`scoreboard`）是评分类任务的成绩视图，聚合口径必须稳定。

**导出**（`routers/export.py`，原 16%）——YAML / 时间线是**数据离开系统**的通道，
  必须：未知格式被拒（避免意外输出内部结构）、未知项目 404、内容确实包含图数据。

产物创建用 `origin` 作来源证据：项目创建时必然存在，无需额外造证据。
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
        json={"title": "target", "origin": "http://target.example", "goal": "prove it"},
        headers=_h(),
    )
    assert r.status_code == 201, r.text
    return r.json()["project"]["id"]


def _create_finding(client, pid: str, **overrides) -> dict:
    body = {
        "fact_id": "origin",
        "title": "t",
        "severity": "high",
        "kind": "vuln",
        "score": 0,
    }
    body.update(overrides)
    r = client.post(f"/projects/{pid}/vulnerabilities", json=body, headers=_h())
    assert r.status_code == 201, r.text
    return r.json()


# ── 产物创建与查询 ────────────────────────────────────────────────────────────

def test_create_default_finding_is_vuln_with_zero_score(client, project):
    v = _create_finding(client, project, title="SQL 注入")
    assert v["kind"] == "vuln"
    assert v["score"] == 0
    assert v["status"] == "pending"


def test_create_flag_with_score(client, project):
    v = _create_finding(client, project, title="a-05 通关旗帜", kind="flag", score=100, severity="info")
    assert v["kind"] == "flag"
    assert v["score"] == 100


def test_invalid_kind_rejected(client, project):
    r = client.post(
        f"/projects/{project}/vulnerabilities",
        json={"fact_id": "origin", "title": "x", "kind": "whatever"},
        headers=_h(),
    )
    assert r.status_code == 422


def test_finding_requires_existing_fact(client, project):
    """指向不存在的证据必须 404 —— 否则库里会出现无法溯源的产物。"""
    r = client.post(
        f"/projects/{project}/vulnerabilities",
        json={"fact_id": "f999", "title": "x"},
        headers=_h(),
    )
    assert r.status_code == 404


def test_list_project_findings(client, project):
    _create_finding(client, project, title="v1")
    _create_finding(client, project, title="v2", kind="flag", score=500)
    r = client.get(f"/projects/{project}/vulnerabilities", headers=_h())
    assert r.status_code == 200
    assert len(r.json()) == 2


def test_cross_project_listing_and_stats(client, project):
    _create_finding(client, project, title="v1", severity="critical")
    all_vulns = client.get("/vulnerabilities", headers=_h())
    assert all_vulns.status_code == 200
    assert len(all_vulns.json()) == 1
    stats = client.get("/vulnerabilities/stats", headers=_h())
    assert stats.status_code == 200
    assert isinstance(stats.json(), dict)


def test_trend_returns_series(client, project):
    _create_finding(client, project)
    r = client.get("/vulnerabilities/trend?days=7", headers=_h())
    assert r.status_code == 200
    assert isinstance(r.json(), list)


# ── 状态流转与删除 ────────────────────────────────────────────────────────────

def test_confirm_sets_verified_at(client, project):
    v = _create_finding(client, project)
    r = client.patch(
        f"/projects/{project}/vulnerabilities/{v['id']}",
        json={"status": "confirmed"},
        headers=_h(),
    )
    assert r.status_code == 200
    assert r.json()["status"] == "confirmed"
    assert r.json()["verified_at"]


def test_dismiss_does_not_set_verified_at(client, project):
    v = _create_finding(client, project)
    r = client.patch(
        f"/projects/{project}/vulnerabilities/{v['id']}",
        json={"status": "dismissed"},
        headers=_h(),
    )
    assert r.status_code == 200
    assert r.json()["verified_at"] in (None, "")


def test_invalid_status_rejected(client, project):
    v = _create_finding(client, project)
    r = client.patch(
        f"/projects/{project}/vulnerabilities/{v['id']}",
        json={"status": "fixed"},
        headers=_h(),
    )
    assert r.status_code == 422


def test_delete_then_missing(client, project):
    v = _create_finding(client, project)
    assert client.delete(f"/projects/{project}/vulnerabilities/{v['id']}", headers=_h()).status_code == 204
    assert client.patch(
        f"/projects/{project}/vulnerabilities/{v['id']}",
        json={"status": "confirmed"},
        headers=_h(),
    ).status_code == 404


def test_patch_unknown_finding_404(client, project):
    r = client.patch(
        f"/projects/{project}/vulnerabilities/v999",
        json={"status": "confirmed"},
        headers=_h(),
    )
    assert r.status_code == 404


# ── 记分板 ────────────────────────────────────────────────────────────────────

def test_scoreboard_aggregates_by_kind(client, project):
    _create_finding(client, project, title="v1", kind="vuln", score=0)
    _create_finding(client, project, title="f1", kind="flag", score=100)
    _create_finding(client, project, title="f2", kind="flag", score=500)
    _create_finding(client, project, title="other", kind="finding", score=0)
    r = client.get(f"/projects/{project}/scoreboard", headers=_h())
    assert r.status_code == 200
    data = r.json()
    assert data["flag_count"] == 2
    assert data["flag_score"] == 600
    assert data["by_kind"]["vuln"]["count"] == 1
    assert data["by_kind"]["finding"]["count"] == 1


def test_scoreboard_empty_project(client, project):
    data = client.get(f"/projects/{project}/scoreboard", headers=_h()).json()
    assert data["flag_count"] == 0
    assert data["flag_score"] == 0
    assert data["total_score"] == 0


def test_scoreboard_unknown_project_404(client):
    assert client.get("/projects/proj_nope/scoreboard", headers=_h()).status_code == 404


# ── 导出 ──────────────────────────────────────────────────────────────────────

def test_export_yaml_contains_graph(client, project):
    r = client.get(f"/projects/{project}/export?format=yaml", headers=_h())
    assert r.status_code == 200
    assert "text/plain" in r.headers["content-type"]
    assert "target.example" in r.text


def test_export_timeline_non_empty(client, project):
    r = client.get(f"/projects/{project}/export?format=timeline", headers=_h())
    assert r.status_code == 200
    assert r.text.strip()


def test_export_default_format_is_yaml(client, project):
    r = client.get(f"/projects/{project}/export", headers=_h())
    assert r.status_code == 200
    assert r.text.strip()


@pytest.mark.parametrize("bad", ["json", "csv", "yaml2", "..", ""])
def test_export_rejects_unknown_format(client, project, bad):
    """格式白名单：不接受额外格式，避免意外输出内部结构。"""
    r = client.get(f"/projects/{project}/export?format={bad}", headers=_h())
    assert r.status_code == 400


def test_export_unknown_project_404(client):
    assert client.get("/projects/proj_nope/export?format=yaml", headers=_h()).status_code == 404
