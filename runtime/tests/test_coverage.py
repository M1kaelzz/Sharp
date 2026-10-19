"""覆盖报告（P1-A）：结项时"打到了什么 / 漏了什么 / 哪些已验证不通"。

**为什么需要这一层**：`acceptance-check` 是**纯负面**清单（还有什么没了结），
回答不了交付时被问的两个问题：打到了什么、**哪块面根本没碰过**。
而"没碰过的面"正是最容易被含糊过去的地方 —— 所以这里只列**有据可查**的缺口，
并明确写出"本报告不构成完整覆盖声明"。
"""

from __future__ import annotations

import pytest

from sharp.server import db
from sharp.server.coverage import build_coverage, coverage_markdown


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_db_path", None, raising=False)
    db.configure(tmp_path / "sharp.db")
    with db.get_conn() as c:
        yield c


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
    resp = client.post(
        "/projects",
        json={"title": "覆盖报告验证", "origin": origin, "goal": "g"},
        headers=_h(),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["project"]["id"]


def _extract(client, pid: str, description: str) -> None:
    client.post(
        "/knowledge/extract",
        json={"project_id": pid, "description": description},
        headers=_h(),
    )


# ── 目标键与基本结构 ───────────────────────────────────────────────────────

def test_report_has_all_sections(client):
    pid = _project(client)
    data = client.get(f"/projects/{pid}/coverage", headers=_h()).json()
    assert data["key"] == "domain:example.com"
    for field in ("findings", "assets", "knowledge", "dead_ends", "blind_spots", "effort", "markdown"):
        assert field in data, f"缺字段 {field}"


def test_empty_project_reports_zero_not_crash(client):
    pid = _project(client)
    data = client.get(f"/projects/{pid}/coverage", headers=_h()).json()
    assert data["findings"]["total"] == 0
    assert data["blind_spot_count"] == 0
    assert "未登记结构化结论" in data["markdown"]
    assert "无有据可查的缺口" in data["markdown"]


# ── 盲区只列有据可查者 ─────────────────────────────────────────────────────

def test_unverified_endpoints_become_blind_spots(client):
    """资产台账里 status=discovered 的接口 = 结构化盲区（发现了却没测）。"""
    pid = _project(client)
    with db.get_conn() as conn:
        from sharp.server.asset_endpoints import register_endpoints_from_fact

        register_endpoints_from_fact(
            conn, project_id=pid, fact_id="origin",
            description="结论里出现了完整接口 https://app.example.com/api/v1/users",
            now="2026-09-15T00:00:00Z",
        )
    data = client.get(f"/projects/{pid}/coverage", headers=_h()).json()
    assert data["assets"]["todo"] == 1
    kinds = {b["kind"] for b in data["blind_spots"]}
    assert "unverified_endpoint" in kinds
    assert any("/api/v1/users" in b["label"] for b in data["blind_spots"])


def test_open_hypothesis_and_unfinished_stage_are_blind_spots(client):
    pid = _project(client)
    client.post(
        f"/projects/{pid}/hypotheses",
        json={"statement": "若 /admin 返回 403 则存在路径但未授权"},
        headers=_h(),
    )
    client.post(
        f"/projects/{pid}/sub-goals",
        json={"title": "获取管理员会话"},
        headers=_h(),
    )
    data = client.get(f"/projects/{pid}/coverage", headers=_h()).json()
    kinds = {b["kind"] for b in data["blind_spots"]}
    assert {"open_hypothesis", "unfinished_stage"} <= kinds


def test_abandoned_intent_carries_reason(client):
    """放弃的行动必须带上原因 —— 否则"为什么没做"这件事没人说得清。"""
    pid = _project(client)
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO intents (id, project_id, description, to_fact_id, creator, created_at, "
            "abandoned_at, abandon_reason, approval_status, priority, risk_level, risk_reason) "
            "VALUES ('i009', ?, '对 /upload 做文件上传绕过', NULL, '人工操作员', '2026-09-15T00:00:00Z', "
            "'2026-09-15T01:00:00Z', '授权窗口不够，留待下一轮', 'none', 0, 'low', '')",
            (pid,),
        )
    data = client.get(f"/projects/{pid}/coverage", headers=_h()).json()
    spot = next(b for b in data["blind_spots"] if b["kind"] == "abandoned_intent")
    assert "授权窗口不够" in spot["detail"]


# ── 已验证不通 ─────────────────────────────────────────────────────────────

def test_dead_ends_appear_from_knowledge_and_facts(client):
    pid = _project(client)
    _extract(client, pid, "https://app.example.com/admin 的 403 绕过测试：路径变体与头注入均返回 404，无法绕过。")
    data = client.get(f"/projects/{pid}/coverage", headers=_h()).json()
    assert data["dead_ends"], "否定结论必须出现在覆盖报告里"
    assert "已验证不通" in data["markdown"]


def test_positive_result_is_not_a_dead_end(client):
    pid = _project(client)
    _extract(client, pid, "https://app.example.com/api/v1/users 未授权可读，返回用户列表。")
    data = client.get(f"/projects/{pid}/coverage", headers=_h()).json()
    assert not any("api/v1/users" in d["title"] for d in data["dead_ends"])


# ── 证据完备度与凭据保护 ───────────────────────────────────────────────────

def test_incomplete_findings_are_flagged(client):
    pid = _project(client)
    client.post(
        f"/projects/{pid}/vulnerabilities",
        json={"fact_id": "origin", "title": "信息泄露", "severity": "low", "url": "https://app.example.com/x"},
        headers=_h(),
    )
    data = client.get(f"/projects/{pid}/coverage", headers=_h()).json()
    assert data["findings"]["total"] == 1
    assert data["findings"]["incomplete_count"] == 1
    assert "证据不足" in data["markdown"]


def test_credentials_are_counted_but_never_echoed(client):
    """知识库里 credential 的 title 就是凭据本身 —— 报告只给条数，不给值。"""
    pid = _project(client)
    _extract(client, pid, "发现凭据 token=SUPERSECRETVALUE12345 可用于 /admin 接口")
    data = client.get(f"/projects/{pid}/coverage", headers=_h()).json()
    assert data["knowledge"]["by_kind"].get("credential", 0) >= 1
    assert "SUPERSECRETVALUE12345" not in data["markdown"], "凭据值不得进报告"
    assert "凭据仅计数" in data["markdown"]


# ── 诚实性：不构成完整覆盖声明 ─────────────────────────────────────────────

def test_markdown_disclaims_completeness(client):
    """授权范围是自然语言，没有机器可读清单 —— 不能假装知道"本该测什么"。"""
    pid = _project(client)
    md = client.get(f"/projects/{pid}/coverage", headers=_h()).json()["markdown"]
    assert "不构成" in md and "完整覆盖声明" in md
    assert "未列出的面不等于已覆盖" in md


def test_missing_project_is_404(client):
    assert client.get("/projects/proj_nope/coverage", headers=_h()).status_code == 404


# ── 渲染稳定性 ─────────────────────────────────────────────────────────────

def test_markdown_section_order(client):
    """交付沟通里被问的顺序就是 打到了什么 → 已验证不通 → 盲区 → 成本。"""
    pid = _project(client)
    md = client.get(f"/projects/{pid}/coverage", headers=_h()).json()["markdown"]
    order = [md.index(h) for h in ("### 打到了什么", "### 已验证不通", "### 盲区", "### 成本")]
    assert order == sorted(order)


def test_build_coverage_is_read_only(client):
    pid = _project(client)
    with db.get_conn() as conn:
        before = conn.execute("SELECT COUNT(*) c FROM knowledge_base").fetchone()["c"]
        build_coverage(conn, pid)
        after = conn.execute("SELECT COUNT(*) c FROM knowledge_base").fetchone()["c"]
    assert before == after


def test_coverage_markdown_renders_for_empty_report():
    """空报告也要渲染得出来（结项时什么都没测的项目同样要能出报告）。"""
    from sharp.server.coverage import CoverageReport

    report = CoverageReport(
        project_id="p", title="t", status="completed", task_mode="pentest",
        started_at="", last_activity_at="", asset_ref="", product="", key=None,
    )
    md = coverage_markdown(report)
    assert "覆盖报告" in md
    assert "无（本次没有留下否定结论）" in md


def test_blind_spot_labels_cover_every_emitted_kind():
    """盲区类型与文案表必须一一对应 —— 否则报告里会出现英文 kind 漏给客户。"""
    from sharp.server.coverage import BLIND_SPOT_LABELS, BlindSpot

    emitted = {
        "unverified_endpoint", "abandoned_intent", "open_intent", "pending_approval",
        "open_hypothesis", "unfinished_stage", "untrusted_fact", "unconfirmed_high_vuln",
    }
    assert emitted <= set(BLIND_SPOT_LABELS), f"缺文案：{emitted - set(BLIND_SPOT_LABELS)}"
    assert BlindSpot("open_intent", "x").to_dict() == {
        "kind": "open_intent", "label": "x", "detail": "",
    }


# ── 精度：不要把正常描述当成"已验证不通" ──────────────────────────────────

def test_structural_facts_are_never_dead_ends(client):
    """真实事故：`goal` 目标文本里含"不输出的**无效**发现"，被当成"已验证不通"列进报告。

    origin / goal 是**结构事实**不是结论 —— 无论里面出现什么词都不该进这一栏。
    """
    pid = client.post(
        "/projects",
        json={
            "title": "t",
            "origin": "目标地址：https://app.example.com/",
            "goal": "尽量发现漏洞；不输出的无效发现：反射型 XSS、缺少安全头。",
        },
        headers=_h(),
    ).json()["project"]["id"]
    data = client.get(f"/projects/{pid}/coverage", headers=_h()).json()
    assert data["dead_ends"] == [], "目标/起点文本不是结论"
    assert "无效发现" not in data["markdown"]


def test_marker_must_be_in_headline_region(client):
    """否定词出现在正文深处（而非结论头部）时不算死胡同。

    精度优先：一条假的 dead_end 会被注入到后续项目的提示词里，写着"不要重复尝试" ——
    这比漏报更糟，它会劝退本来是有效的重试。
    """
    pid = _project(client)
    # 否定词放在 300 字窗口**之外**（窗口内的命中仍算数：工人常在开头就下结论）
    body = "指纹与服务版本已确认：" + "该项已逐条核对。" * 40 + "该路径均返回 404。"
    assert body.index("均返回 404") > 300
    _extract(client, pid, body)
    data = client.get(f"/projects/{pid}/coverage", headers=_h()).json()
    assert data["dead_ends"] == []


def test_headline_cuts_at_sentence_boundary():
    """交付观感：**超长**时才截断，且要落在句读处 —— 不在半句中间硬切。

    （短文本原样返回：硬切会白丢信息。）
    """
    from sharp.server.coverage import _headline

    short = "接口未授权可读"
    assert _headline(short) == short, "短文本不该被切"
    assert _headline("") == ""

    # 超过 limit(170)：应在第一个句读处收住
    long_line = "对 login 接口做了完整输入验证测试，结论为负（无 confirmed 漏洞），" + "补充说明若干。" * 40
    cut = _headline(long_line)
    assert len(cut) <= 175 and cut.endswith("。"), cut

    # 句读来得太晚（超 limit）→ 退化为硬切 + 省略号
    assert _headline("x" * 400).endswith("…")


# ── 注入报告上下文 ─────────────────────────────────────────────────────────

def test_report_context_includes_coverage_and_disclaimer_rule(client):
    """生成的报告必须带着覆盖与盲区，并被明确禁止越界声称已覆盖。

    只靠模型自由发挥时，"覆盖范围"这一节最容易被写成听起来完整、实则没依据的清单 ——
    这正是要防的。
    """
    from sharp.server.reports import build_engineered_report_context

    pid = _project(client)
    _extract(client, pid, "https://app.example.com/admin 的 403 绕过测试：路径变体均返回 404，无法绕过。")
    with db.get_conn() as conn:
        ctx = build_engineered_report_context(conn, pid)
    assert '"coverage"' in ctx
    assert "blind_spot_count" in ctx
    assert "不得声称覆盖了清单之外的面" in ctx
    assert "已验证不通" in ctx
