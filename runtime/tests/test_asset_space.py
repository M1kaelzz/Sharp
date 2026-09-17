"""目标空间详情（P2-A）：一个资产键下**跨项目**的整体状态。

为什么要有这个视角：点开资产芯片此前只等于"过滤项目列表"，看不到**跨项目共享的那部分** ——
而接口台账本来就是按资产键共享的。它现在才真正有内容可看：台账状态机（P1-B）、
证据完备度（P0-1）、死胡同（P0-知识复用）都是这一轮才补上的。
"""

from __future__ import annotations

import pytest

from sharp.server import db


@pytest.fixture
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from sharp.server.app import app

    monkeypatch.setattr(db, "_db_path", None, raising=False)
    monkeypatch.setenv("SHARP_SERVER_TOKEN", "server-token-xyz")
    db.configure(tmp_path / "sharp.db")
    c = TestClient(app)
    yield c
    c.close()


def _h() -> dict[str, str]:
    return {"Authorization": "Bearer server-token-xyz"}


def _project(client, title: str, host: str = "app.example.com") -> str:
    r = client.post("/projects", json={
        "title": title,
        "origin": f"目标地址：https://{host}/\n授权范围：仅该主域",
        "goal": "g",
    }, headers=_h())
    assert r.status_code == 201, r.text
    return r.json()["project"]["id"]


def _endpoint(conn, pid: str, ref: str, path: str, status: str = "discovered", note: str = "") -> None:
    conn.execute(
        "INSERT INTO asset_endpoints (asset_ref, method, path, source_project_id, source_fact_id, "
        "first_seen, last_seen, status, note) VALUES (?, 'GET', ?, ?, 'origin', 'T', 'T', ?, ?)",
        (ref, path, pid, status, note),
    )


def _vuln(conn, pid: str, *, severity="high", status="pending", evidence="", reproduction="", impact="") -> None:
    conn.execute(
        "INSERT INTO vulnerabilities (id, project_id, fact_id, title, severity, status, url, "
        "description, evidence, reproduction, impact, created_at) "
        "VALUES (?, ?, 'origin', 'T', ?, ?, 'https://app.example.com/x', 'd', ?, ?, ?, 'T')",
        (f"v_{pid}_{severity}", pid, severity, status, evidence, reproduction, impact),
    )


# ── 基本结构 ───────────────────────────────────────────────────────────────

def test_unknown_asset_is_404(client):
    assert client.get("/asset-spaces/nope.example", headers=_h()).status_code == 404


def test_space_aggregates_projects_across_runs(client):
    a = _project(client, "第一轮", "app.example.com")
    b = _project(client, "第二轮", "app.example.com")
    data = client.get("/asset-spaces/app.example.com", headers=_h()).json()
    assert data["asset_ref"] == "app.example.com"
    assert data["project_count"] == 2
    assert {p["id"] for p in data["projects"]} == {a, b}
    assert data["target_kinds"] == ["web"]
    assert data["activity"]["first_project_at"] <= data["activity"]["last_project_at"]


def test_other_assets_do_not_leak_in(client):
    _project(client, "本资产", "app.example.com")
    _project(client, "别的资产", "other.example.org")
    data = client.get("/asset-spaces/app.example.com", headers=_h()).json()
    assert data["project_count"] == 1
    assert {p["title"] for p in data["projects"]} == {"本资产"}


# ── 覆盖状态：接口台账 ─────────────────────────────────────────────────────

def test_endpoints_grouped_by_status_with_source(client):
    pid = _project(client, "第一轮")
    with db.get_conn() as c:
        _endpoint(c, pid, "app.example.com", "/api/v1/users", "discovered")
        _endpoint(c, pid, "app.example.com", "/api/v1/orders", "verified", note="未授权可读")
        _endpoint(c, pid, "app.example.com", "/admin", "dismissed", note="403 无法绕过")
    data = client.get("/asset-spaces/app.example.com", headers=_h()).json()
    ep = data["endpoints"]
    assert (ep["total"], ep["discovered"], ep["verified"], ep["dismissed"]) == (3, 1, 1, 1)
    # 未验证的排最前（"接着测什么"一眼可见）
    assert ep["items"][0]["path"] == "/api/v1/users"
    assert ep["items"][0]["status"] == "discovered"
    # 带来源项目标题，便于判断是哪个项目测的
    assert all(i["source_project_title"] == "第一轮" for i in ep["items"])
    assert any(i["note"] == "未授权可读" for i in ep["items"])


# ── 历史发现 ───────────────────────────────────────────────────────────────

def test_findings_aggregate_across_projects(client):
    a = _project(client, "第一轮")
    b = _project(client, "第二轮")
    with db.get_conn() as c:
        # 证据完备（阈值：证据/复现 ≥20 字、影响 ≥10 字），否则会被算进"证据不足"
        _vuln(c, a, severity="high", status="confirmed",
              evidence="GET /api/v1/users → 200，返回 3 个用户对象（含手机号）",
              reproduction="1. 不带任何认证头 GET /api/v1/users\n2. 观察 200 与完整列表",
              impact="未授权读取用户隐私数据，可批量遍历")
        _vuln(c, b, severity="low", status="pending")
    data = client.get("/asset-spaces/app.example.com", headers=_h()).json()
    f = data["findings"]
    assert f["total"] == 2
    assert f["by_severity"] == {"high": 1, "low": 1}
    assert f["high_unconfirmed"] == 0, "已确认的高危不该算作待确认"
    assert f["incomplete_evidence"] == 1, "第二条缺证据，应被标出"


def test_incomplete_evidence_points_at_the_project(client):
    a = _project(client, "第一轮")
    with db.get_conn() as c:
        _vuln(c, a, severity="high", status="confirmed")
    data = client.get("/asset-spaces/app.example.com", headers=_h()).json()
    item = data["findings"]["incomplete_items"][0]
    assert item["project_id"] == a and item["missing"]


# ── 已沉淀知识（含凭据保护）──────────────────────────────────────────────

def test_knowledge_and_dead_ends_are_listed(client):
    pid = _project(client, "第一轮")
    client.post("/knowledge/extract", json={
        "project_id": pid,
        "description": "https://app.example.com/admin 的绕过测试均返回 404，无法绕过。",
    }, headers=_h())
    data = client.get("/asset-spaces/app.example.com", headers=_h()).json()
    assert data["knowledge"]["key"] == "domain:example.com"
    assert data["knowledge"]["by_kind"].get("dead_end", 0) >= 1
    assert data["knowledge"]["dead_ends"]


def test_credentials_are_counted_but_never_echoed(client):
    """与覆盖报告同一口径：credential 条目的 title 就是凭据本身，只给条数。"""
    pid = _project(client, "第一轮")
    client.post("/knowledge/extract", json={
        "project_id": pid,
        "description": "发现凭据 token=SUPERSECRETVALUE12345 可用于 /admin 接口",
    }, headers=_h())
    data = client.get("/asset-spaces/app.example.com", headers=_h()).json()
    assert data["knowledge"]["credential_count"] >= 1
    assert "SUPERSECRETVALUE12345" not in str(data), "凭据值不得出现在空间详情里"


# ── 路由守卫（我自己踩过这类坑：路径被参数吃掉）──────────────────────────

def test_assets_endpoints_route_is_not_shadowed(client):
    """`/assets/endpoints` 不能被 `/assets/{asset_ref}` 式路由吃掉。

    这正是我把空间详情放在 `/asset-spaces/{asset_ref}` 的原因 —— FastAPI 按注册顺序匹配，
    若用 `/assets/{asset_ref}` 且注册在前，"endpoints" 会被当成资产名。
    这条测试把"两条路由都能解析"钉住。
    """
    pid = _project(client, "第一轮")
    with db.get_conn() as c:
        _endpoint(c, pid, "app.example.com", "/api/v1/users")
    r = client.get("/assets/endpoints", params={"asset_ref": "app.example.com"}, headers=_h())
    assert r.status_code == 200, r.text
    assert [row["path"] for row in r.json()] == ["/api/v1/users"]
    assert client.get("/asset-spaces/app.example.com", headers=_h()).status_code == 200


def test_dead_ends_come_from_facts_too(client):
    """知识库与事实层**都要看**：知识沉淀在 `dead_end` 这个 kind 出现之前的老项目，
    只看知识库会把 9 条已验证不通显示成 0 条（实测 proj_002 就是这种情况）。

    与 `coverage.py` / 阶段估算同一口径 —— 三处各写一套"什么算不通"正是本项目反复出现的问题。
    """
    pid = _project(client, "老项目")
    with db.get_conn() as c:
        c.execute(
            "INSERT INTO facts (id, project_id, description) VALUES ('f001', ?, ?)",
            (pid, "对 login 参数做完整输入验证测试，结论为负，本 intent 为死胡同。"),
        )
    data = client.get("/asset-spaces/app.example.com", headers=_h()).json()
    assert data["knowledge"]["dead_ends"], "事实层的否定结论也该出现在这里"
    assert data["knowledge"]["dead_ends"][0]["source"].startswith("结论")
