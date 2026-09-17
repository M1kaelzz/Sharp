"""阶段估算（P2-C）：**只用结构化信号，不从散文里猜**。

**为什么改**（2026-09-09 实测推翻旧实现）：旧版把 `未授权 / 绕过 / ssrf / 越权 / 漏洞` 这类词
在结论里出现当作"已有漏洞面线索"。真实语料上它必然误报，因为**工人描述"测过什么"和"发现了什么"
用的是同一套词**：

| 真实结论原话 | 旧实现读成 | 实际 |
|---|---|---|
| `SSRF 探测（i009）完成：…不存在未认证 SSRF` | 有 SSRF 线索 | 否定结论 |
| `因此 IDOR/越权/业务逻辑测试…` | 有越权线索 | 待测计划 |
| `旧 nginx 未认证 DoS/RCE，影响 ≤8` | 有 RCE 线索 | 复述 CVE 描述 |

后果不轻：这条指导**每轮注入 reason**，且在零漏洞项目上写着"图上已有漏洞面线索…**少开新面**" ——
实测 `proj_002`（结论全为否定）与 `proj_004` 都被判成"验证阶段"。
"""

from __future__ import annotations

import pytest

from sharp.server import db
from sharp.server.services import compute_project_phase


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_db_path", None, raising=False)
    db.configure(tmp_path / "sharp.db")
    with db.get_conn() as c:
        yield c


def _project(conn, *, origin="目标地址：https://app.example.com/", title="t") -> str:
    from sharp.server.models import CreateProjectRequest
    from sharp.server.routers.projects import create_project

    detail = create_project(CreateProjectRequest(title=title, origin=origin, goal="g"))
    return detail.project.id


def _fact(conn, pid: str, text: str, fid: str = "f001") -> None:
    conn.execute(
        "INSERT INTO facts (id, project_id, description) VALUES (?, ?, ?)", (fid, pid, text)
    )


def _vuln(
    conn, pid: str, *, severity="high", status="pending",
    evidence="GET /api/v1/users → 200，返回 3 个用户对象（含手机号）",
    reproduction="1. 直接 GET /api/v1/users（不带任何认证头）\n2. 观察 200 与完整用户列表",
    impact="未授权读取用户隐私数据，可批量遍历",
) -> None:
    conn.execute(
        "INSERT INTO vulnerabilities (id, project_id, fact_id, title, severity, status, url, "
        "description, evidence, reproduction, impact, created_at) "
        "VALUES ('v001', ?, 'origin', 'T', ?, ?, 'https://app.example.com/x', 'd', ?, ?, ?, 'T')",
        (pid, severity, status, evidence, reproduction, impact),
    )


def _endpoint(conn, pid: str, asset_ref: str, path: str, status="discovered") -> None:
    conn.execute(
        "INSERT INTO asset_endpoints (asset_ref, method, path, source_project_id, source_fact_id, "
        "first_seen, last_seen, status) VALUES (?, '', ?, ?, 'origin', 'T', 'T', ?)",
        (asset_ref, path, pid, status),
    )


# ── 核心回归：否定/计划/CVE 陈述都不该触发"验证阶段" ────────────────────────

def test_negated_findings_do_not_trigger_verify(conn):
    """真实事故：结论里写"SSRF 探测完成，不存在未认证 SSRF"曾被读成"有 SSRF 线索"。”

    这条测试用的是 proj_002 的**真实措辞**（Peplink 那次，全部结论为否定）。
    """
    pid = _project(conn)
    _fact(conn, pid, "SSRF 探测（i009）完成：该 web 管理接口不存在未认证 SSRF；"
                     "因此 IDOR/越权/业务逻辑测试均未发现可确认漏洞；403 不可绕过。")
    phase = compute_project_phase(conn, pid)
    assert phase["phase"] != "verify", f"否定措辞被当成漏洞线索：{phase}"
    assert "少开新面" not in phase["guidance"]


def test_cve_recital_does_not_trigger_verify(conn):
    """复述 CVE 描述（"旧 nginx 未认证 DoS/RCE，影响 ≤8"）不是本目标的发现。"""
    pid = _project(conn)
    _fact(conn, pid, "CVE 情报检索完成：旧 nginx 未认证 DoS/RCE，影响 ≤8.6；本目标未受影响。")
    assert compute_project_phase(conn, pid)["phase"] != "verify"


def test_planned_tests_do_not_trigger_verify(conn):
    pid = _project(conn)
    _fact(conn, pid, "下一步计划：对 login 接口做弱口令测试与越权差分。")
    assert compute_project_phase(conn, pid)["phase"] not in ("verify", "credential")


# ── 结构化信号驱动判定 ─────────────────────────────────────────────────────

def test_high_unconfirmed_is_verify(conn):
    pid = _project(conn)
    _vuln(conn, pid, severity="high", status="pending")
    assert compute_project_phase(conn, pid)["phase"] == "verify"


def test_high_confirmed_is_report(conn):
    pid = _project(conn)
    _vuln(conn, pid, severity="critical", status="confirmed")
    assert compute_project_phase(conn, pid)["phase"] == "report"


def test_incomplete_evidence_outranks_backlog(conn):
    """证据不足是交付阻塞项，优先于"接着测新接口"。"""
    pid = _project(conn)
    _vuln(conn, pid, severity="low", status="pending", evidence="", reproduction="", impact="")
    _endpoint(conn, pid, "app.example.com", "/api/v1/users")
    phase = compute_project_phase(conn, pid)
    assert phase["phase"] == "evidence"
    assert phase["signals"]["incomplete_evidence"] == 1


def test_endpoint_backlog_is_specific_work(conn):
    pid = _project(conn)
    _endpoint(conn, pid, "app.example.com", "/api/v1/users")
    _endpoint(conn, pid, "app.example.com", "/api/v1/orders")
    phase = compute_project_phase(conn, pid)
    assert phase["phase"] == "test_backlog"
    assert "2 个" in phase["guidance"]


def test_credential_knowledge_drives_credential_phase(conn):
    pid = _project(conn)
    conn.execute(
        "INSERT INTO knowledge_base (root_domain, kind, title, content, source_project_id, "
        "product, confidence, created_at, updated_at) "
        "VALUES ('domain:example.com', 'credential', 'token', 'x', ?, '', 'high', 'T', 'T')",
        (pid,),
    )
    assert compute_project_phase(conn, pid)["phase"] == "credential"


def test_dead_ends_without_findings_ask_for_new_surface(conn):
    pid = _project(conn)
    for i, text in enumerate([
        "对 login 参数做完整输入验证测试，结论为负，本 intent 为死胡同。",
        "nginx 反代/备份文件暴露探测未发现可确认漏洞。",
        "非 443 端口/第二入口枚举结果为负。",
    ]):
        _fact(conn, pid, text, fid=f"f{i:03d}")
    phase = compute_project_phase(conn, pid)
    assert phase["phase"] == "explore"
    assert "换面" in phase["guidance"] and "3 条" in phase["guidance"]


def test_dead_ends_counted_from_facts_when_knowledge_is_empty(conn):
    """真实项目 proj_002 的知识是在 `dead_end` 这个 kind 出现**之前**沉淀的。

    只看知识库会把 9 条已验证不通当成 0 条 —— 首次跑就踩到了。
    """
    pid = _project(conn)
    for i in range(4):
        _fact(conn, pid, f"第 {i} 条：均返回 404，无法绕过。", fid=f"f{i:03d}")
    phase = compute_project_phase(conn, pid)
    assert phase["signals"]["dead_ends_from_knowledge"] == 0
    assert phase["signals"]["dead_ends_from_facts"] == 4
    assert phase["phase"] == "explore"


def test_complete_finding_is_concluded_not_recon(conn):
    """有漏洞但证据完备时既不该说"无漏洞"，也不该催收敛。"""
    pid = _project(conn)
    _vuln(conn, pid, severity="low", status="pending")
    phase = compute_project_phase(conn, pid)
    assert phase["phase"] == "concluded"
    assert "无漏洞" not in phase["guidance"]


def test_empty_project_is_recon_and_says_so(conn):
    pid = _project(conn)
    phase = compute_project_phase(conn, pid)
    assert phase["phase"] == "recon"
    assert "尚无结构化信号" in phase["guidance"]


# ── 指针与信号块 ───────────────────────────────────────────────────────────

def test_pointers_still_surface_human_corrections(conn):
    pid = _project(conn)
    _fact(conn, pid, "一条被人工标注不可信的结论")
    conn.execute("UPDATE facts SET trusted = 0 WHERE project_id = ? AND id = 'f001'", (pid,))
    phase = compute_project_phase(conn, pid)
    assert any("不可信" in p for p in phase["pointers"])


def test_signals_block_is_reported(conn):
    pid = _project(conn)
    signals = compute_project_phase(conn, pid)["signals"]
    assert {"vulnerabilities", "incomplete_evidence", "unverified_endpoints",
            "credential_knowledge", "dead_ends"} <= set(signals)


def test_phase_endpoint_returns_signals(client):
    """HTTP 端点也要带上信号块（dispatcher 排查"为什么是这个阶段"时唯一的依据）。"""
    r = client.get("/projects/proj_missing/phase", headers={"Authorization": "Bearer server-token-xyz"})
    assert r.status_code == 404


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


# ── 评分类任务：不做"报告收尾"，且 flag 不算漏洞 ────────────────────────────

def _scored_project(conn) -> str:
    from sharp.server.models import CreateProjectRequest
    from sharp.server.routers.projects import create_project

    detail = create_project(CreateProjectRequest(
        title="scored", origin="目标地址：https://app.example.com/", goal="g", task_mode="scored",
    ))
    return detail.project.id


def test_scored_run_with_confirmed_high_does_not_wrap_up(conn):
    """评分类任务里"已确认高危"是**取分的立足点**，不是收尾信号。

    真实事故（2026-09-16 TSec 跑分）：已确认 RCE + 3 个 flag，阶段估算却返回
    「报告收尾 · 避免再发起大开大合的开放式探索」—— 而当时还剩 **11 个 flag** 未拿。
    对渗透交付这是对的；对评分类任务是**方向相反**的指导。
    """
    pid = _scored_project(conn)
    _vuln(conn, pid, severity="critical", status="confirmed")
    phase = compute_project_phase(conn, pid)
    assert phase["phase"] != "report", f"评分类任务不该被劝收尾：{phase}"
    assert phase["phase"] == "exploit"
    assert "继续" in phase["guidance"] and "收尾" in phase["guidance"]


def test_pentest_run_still_wraps_up(conn):
    """渗透交付照旧收尾 —— 修的是评分类任务，不是把原有行为一起改掉。"""
    pid = _project(conn)
    _vuln(conn, pid, severity="critical", status="confirmed")
    assert compute_project_phase(conn, pid)["phase"] == "report"


def test_flags_are_not_counted_as_vulnerabilities(conn):
    """`kind='flag'` 是记分产物，不是安全发现。

    实测它们混进 vulns 后：漏洞数被虚增（3 flag + 3 漏洞 → 6），并参与收尾判定。
    """
    pid = _scored_project(conn)
    _vuln(conn, pid, severity="critical", status="confirmed")
    conn.execute(
        "INSERT INTO vulnerabilities (id, project_id, fact_id, title, severity, status, url, "
        "description, evidence, reproduction, impact, kind, score, created_at) "
        "VALUES ('vf', ?, 'origin', 'flag #1', 'info', 'confirmed', '', 'd', 'flag{x}', '', '', "
        "'flag', 300, 'T')",
        (pid,),
    )
    signals = compute_project_phase(conn, pid)["signals"]
    assert signals["vulnerabilities"] == 1, "flag 不该算进漏洞数"
    assert signals["flags_found"] == 1


def test_scored_run_with_only_flags_keeps_collecting(conn):
    pid = _scored_project(conn)
    conn.execute(
        "INSERT INTO vulnerabilities (id, project_id, fact_id, title, severity, status, url, "
        "description, evidence, reproduction, impact, kind, score, created_at) "
        "VALUES ('vf1', ?, 'origin', 'flag #1', 'info', 'confirmed', '', 'd', 'flag{x}', '', '', "
        "'flag', 300, 'T')",
        (pid,),
    )
    phase = compute_project_phase(conn, pid)
    assert phase["phase"] == "exploit"
    assert "拿满" in phase["guidance"]
