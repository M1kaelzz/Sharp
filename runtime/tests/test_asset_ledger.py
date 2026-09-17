"""接口台账闭环（P1-B）：worker 看得见、也写得回。

**为什么这一批必须做**：`asset_endpoints.status` / `last_assessed_at` 此前
**全代码库无人写入** —— 状态机只有"创建"没有"推进"，所有行永远停在 `discovered`。
后果不是"少个字段"，而是刚交付的覆盖报告里那份「未验证接口」盲区清单**只增不减**：
明明测过的接口会一直挂在盲区里，越用越吵，最后没人看。

这条链路补齐后：worker 每轮读到「已发现待评估 / 已验证 / 已排除」三组，
并把本轮的评估写回台账，盲区才真正随覆盖推进而收敛。
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


def _project(client, origin: str = "目标地址：https://app.example.com/\n授权范围：仅该主域") -> str:
    r = client.post("/projects", json={"title": "t", "origin": origin, "goal": "g"}, headers=_h())
    assert r.status_code == 201, r.text
    return r.json()["project"]["id"]


def _register(client, pid: str, description: str) -> None:
    """走真实登记路径：结论里的完整 URL 自动进台账。"""
    with db.get_conn() as conn:
        from sharp.server.asset_endpoints import register_endpoints_from_fact

        register_endpoints_from_fact(
            conn, project_id=pid, fact_id="origin", description=description,
            now="2026-09-15T00:00:00Z",
        )


# ── 读取：三组分明 ─────────────────────────────────────────────────────────

def test_ledger_groups_pending_first(client):
    pid = _project(client)
    _register(client, pid, "https://app.example.com/api/v1/users 与 https://app.example.com/api/v1/orders 均被发现")
    data = client.get(f"/projects/{pid}/asset-endpoints", headers=_h()).json()
    assert data["asset_ref"] == "app.example.com"
    assert data["counts"]["total"] == 2 and data["counts"]["todo"] == 2
    assert "已发现但尚未评估（2）" in data["block"]
    assert data["block"].index("尚未评估") < data["block"].index("已验证") if "已验证" in data["block"] else True


def test_project_without_asset_ref_returns_empty(client):
    pid = _project(client, origin="本地路径：/Users/x/app.apk")
    data = client.get(f"/projects/{pid}/asset-endpoints", headers=_h()).json()
    assert data["asset_ref"] == ""
    assert data["counts"]["total"] == 0 and data["items"] == []


# ── 写入：状态机推进（这一条是整批的核心）──────────────────────────────────

def test_assessment_moves_endpoint_out_of_blind_spots(client):
    """评估之后，该接口必须从"未验证"里消失 —— 否则盲区清单永远不收敛。"""
    pid = _project(client)
    _register(client, pid, "https://app.example.com/api/v1/users 未授权可读")

    before = client.get(f"/projects/{pid}/coverage", headers=_h()).json()
    assert before["assets"]["todo"] == 1

    r = client.post(f"/projects/{pid}/endpoint-assessments", headers=_h(), json={
        "items": [{"method": "GET", "path": "/api/v1/users", "status": "verified",
                   "note": "未授权可读，返回用户列表"}],
    })
    assert r.status_code == 200 and r.json()["updated"] == 1

    after = client.get(f"/projects/{pid}/coverage", headers=_h()).json()
    assert after["assets"]["todo"] == 0 and after["assets"]["verified"] == 1
    assert not [b for b in after["blind_spots"] if b["kind"] == "unverified_endpoint"]


def test_assessment_matches_row_with_empty_method(client):
    """提取阶段只记 path（method 留空），worker 报的是带方法的请求。

    严格按 (method, path) 匹配会永远打不中历史行 —— 于是评估看起来"成功了"，
    状态却还是 discovered（这个 bug 只在真实数据上才看得出来）。
    """
    pid = _project(client)
    _register(client, pid, "https://app.example.com/api/v1/users 被发现")
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT method FROM asset_endpoints WHERE path = '/api/v1/users'"
        ).fetchone()
        assert row["method"] == "", "前提：提取阶段 method 为空"

    client.post(f"/projects/{pid}/endpoint-assessments", headers=_h(), json={
        "items": [{"method": "GET", "path": "/api/v1/users", "status": "verified"}],
    })
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT method, status, last_assessed_at FROM asset_endpoints "
            "WHERE path = '/api/v1/users'"
        ).fetchone()
    assert row["status"] == "verified"
    assert row["method"] == "GET", "命中空 method 的历史行时应顺带补上方法"
    assert row["last_assessed_at"], "评估时间必须落库"


def test_path_normalization_avoids_duplicate_rows(client):
    """带查询串/尾斜杠的写法要落到同一行，否则同一接口会出现两行、评估打不中。"""
    pid = _project(client)
    _register(client, pid, "https://app.example.com/api/v1/users 被发现")
    client.post(f"/projects/{pid}/endpoint-assessments", headers=_h(), json={
        "items": [{"method": "GET", "path": "/api/v1/users/?page=2", "status": "dismissed"}],
    })
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT path, status FROM asset_endpoints WHERE asset_ref = 'app.example.com'"
        ).fetchall()
    assert len(rows) == 1, f"不该产生第二行：{[dict(r) for r in rows]}"
    assert rows[0]["status"] == "dismissed"


def test_unknown_endpoint_is_created_as_assessed(client):
    """worker 真的测过但台账没登记过的接口 → 新建并记为已评估（这本身就是覆盖证据）。"""
    pid = _project(client)
    r = client.post(f"/projects/{pid}/endpoint-assessments", headers=_h(), json={
        "items": [{"method": "POST", "path": "/api/v1/login", "status": "verified",
                   "note": "弱口令均失败"}],
    })
    assert r.json()["created"] == 1
    data = client.get(f"/projects/{pid}/asset-endpoints", headers=_h()).json()
    assert data["counts"]["verified"] == 1 and data["counts"]["todo"] == 0


def test_discovered_is_not_accepted_from_worker(client):
    """`discovered` 是创建时的初始态，不让 worker 把已评估的条目退回未评估 ——
    那会让盲区清单重新变脏。"""
    pid = _project(client)
    _register(client, pid, "https://app.example.com/api/v1/users 被发现")
    r = client.post(f"/projects/{pid}/endpoint-assessments", headers=_h(), json={
        "items": [{"method": "GET", "path": "/api/v1/users", "status": "discovered"}],
    })
    assert r.status_code == 422


def test_assessment_requires_asset_ref(client):
    pid = _project(client, origin="本地路径：/Users/x/app.apk")
    r = client.post(f"/projects/{pid}/endpoint-assessments", headers=_h(), json={
        "items": [{"path": "/x", "status": "verified"}],
    })
    assert r.status_code == 409


# ── dispatcher 侧 ─────────────────────────────────────────────────────────

def test_extract_endpoint_assessments_filters_and_bounds():
    from sharp.dispatcher.tasks.explore import _extract_endpoint_assessments

    payload = {"data": {"endpoint_tests": [
        {"method": "get", "path": "/a", "status": "VERIFIED", "note": "n"},
        {"method": "GET", "path": "/b", "status": "discovered"},   # 初始态：拒
        {"method": "GET", "path": "", "status": "verified"},       # 无路径：拒
        "不是对象",                                                  # 形状错：跳过
        {"path": "/c", "status": "dismissed"},
    ]}}
    items = _extract_endpoint_assessments(payload)
    assert [(i["method"], i["path"], i["status"]) for i in items] == [
        ("GET", "/a", "verified"), ("", "/c", "dismissed"),
    ]


def test_extract_endpoint_assessments_accepts_alt_key_and_caps():
    from sharp.dispatcher.tasks.explore import _extract_endpoint_assessments

    assert _extract_endpoint_assessments({"endpoint_assessments": [{"path": "/x", "status": "verified"}]})
    many = {"data": {"endpoint_tests": [{"path": f"/p{i}", "status": "verified"} for i in range(80)]}}
    assert len(_extract_endpoint_assessments(many)) == 50


def test_ledger_block_says_when_unavailable():
    from sharp.dispatcher.tasks.explore import _asset_ledger_block

    class _Client:
        def fetch_asset_ledger(self, project_id):
            return None

    assert "台账不可用" in _asset_ledger_block(_Client(), "p1")


def test_ledger_block_prompts_when_empty():
    from sharp.dispatcher.tasks.explore import _asset_ledger_block

    class _Client:
        def fetch_asset_ledger(self, project_id):
            return {"counts": {"total": 0}, "block": ""}

    assert "还是空的" in _asset_ledger_block(_Client(), "p1")


def test_reason_and_explore_prompts_have_ledger_placeholder():
    from sharp.dispatcher.prompting import load_prompt

    for name in ("reason.md", "explore.md"):
        text = load_prompt("default", name)
        assert "{asset_ledger}" in text, f"{name} 缺台账占位符"
        assert "endpoint_tests" in text or "未评估" in text or "un-assessed" in text.lower()
