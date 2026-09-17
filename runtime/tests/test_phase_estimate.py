"""阶段估算测试：信号优先级与人工纠正指针。

估算结果注入 reason.md 的 `{phase_context}`，只是节奏参考。

**2026-09-09 契约变更**：判定从"关键词启发式"改为**只用结构化信号**
（漏洞行 / 证据完备度 / 待测接口 / 凭据知识 / 死胡同数）。原因见
`test_prose_alone_does_not_promote_phase` —— 散文里的关键词无法区分
"测过什么"与"发现了什么"，真实项目上必然误报。
优先级：report > verify > evidence > test_backlog > credential > explore/concluded > recon。
"""

from __future__ import annotations

import pytest

from sharp.server import db
from sharp.server.services import compute_project_phase, utcnow


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_db_path", None, raising=False)
    db.configure(tmp_path / "sharp.db")
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO projects (id, title, status, created_at) VALUES (?, ?, 'active', ?)",
            ("p1", "t", utcnow()),
        )
        conn.execute(
            "INSERT INTO facts (id, project_id, description) VALUES ('origin', 'p1', 'http://a.example')",
        )
        conn.execute(
            "INSERT INTO facts (id, project_id, description) VALUES ('goal', 'p1', '证明目标不可控')",
        )
    yield


def _fact(description: str, trusted: int = 1) -> None:
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO facts (id, project_id, description, trusted) VALUES (?, 'p1', ?, ?)",
            ("f" + str(id(description))[-5:], description, trusted),
        )


def _vuln(title: str, severity: str, status: str) -> None:
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO vulnerabilities (id, project_id, fact_id, title, severity, status, created_at) "
            "VALUES (?, 'p1', 'origin', ?, ?, ?, ?)",
            ("v" + str(id(title))[-5:], title, severity, status, utcnow()),
        )


def _conn():
    from sharp.server.db import get_conn

    with get_conn() as conn:
        return compute_project_phase(conn, "p1")


def test_fresh_project_is_recon(temp_db):
    assert _conn()["phase"] == "recon"


def test_prose_alone_does_not_promote_phase(temp_db):
    """**散文不再驱动阶段**（2026-09-09 契约变更）。

    这两条测试原先断言"结论里出现凭据/漏洞关键词 → 对应阶段"。真实语料证明该契约是错的：
    工人描述"测过什么"与"发现了什么"用的是同一套词，于是
    `SSRF 探测完成：不存在未认证 SSRF`、`因此 IDOR/越权测试…`、`旧 nginx 未认证 DoS/RCE`
    都会被读成"已有漏洞面线索"，在**零漏洞**的项目上每轮注入"少开新面"。
    实测 `proj_002` / `proj_004` 都被误判成"验证阶段"。

    新契约：阶段只由**结构化信号**驱动（漏洞行 / 证据完备度 / 待测接口 / 凭据知识 / 死胡同数）。
    下面两条改成**反向断言**：同样的文案，不该再把阶段推走。
    """
    _fact("发现后台登录框 http://a.example/admin/login 提示默认口令 admin/admin")
    assert _conn()["phase"] == "recon", "仅凭关键词不该进入凭据阶段"

    _fact("8080 接口疑似越权，普通账号可读他人订单")
    assert _conn()["phase"] == "recon", "仅凭关键词不该进入验证阶段"


def test_credential_knowledge_promotes_to_credential_phase(temp_db):
    """真正"拿到凭据"是**知识库里有凭据条目**（结构化），不是结论里出现了"口令"两个字。"""
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO knowledge_base (root_domain, kind, title, content, source_project_id, "
            "product, confidence, created_at, updated_at) "
            "VALUES ('domain:a.example', 'credential', 'token', 'x', 'p1', '', 'high', 'T', 'T')"
        )
    phase = _conn()
    assert phase["phase"] == "credential"
    assert "凭据" in phase["guidance"]


def test_high_vuln_row_promotes_to_verify_phase(temp_db):
    """结构性信号照旧生效：高危未确认 → 验证阶段。"""
    _vuln("8080 接口疑似越权", "high", "pending")
    phase = _conn()
    assert phase["phase"] == "verify"


def test_unconfirmed_high_vuln_prioritizes_verify(temp_db):
    _vuln("SQL 注入", "critical", "pending")
    phase = _conn()
    assert phase["phase"] == "verify"
    assert "未确认" in phase["label"] or "验证" in phase["guidance"]


def test_confirmed_high_vuln_enters_report_wrapup(temp_db):
    _vuln("SQL 注入", "critical", "confirmed")
    phase = _conn()
    assert phase["phase"] == "report"
    assert "收尾" in phase["guidance"] or "报告" in phase["label"]


def test_untrusted_and_pending_surface_as_pointers(temp_db):
    _fact("需要复核的证据", trusted=0)
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO intents (id, project_id, description, creator, created_at, to_fact_id, "
            "risk_level, approval_status) VALUES ('i9', 'p1', '高危待批', 'ai', ?, NULL, 'high', 'pending')",
            (utcnow(),),
        )
    phase = _conn()
    joined = "\n".join(phase["pointers"])
    assert "不可信" in joined
    assert "审批" in joined
    assert phase["untrusted_fact_count"] >= 1
    assert phase["pending_approval_count"] == 1


def test_no_crash_on_empty_corpus(temp_db):
    phase = _conn()
    assert phase["phase"] in {"recon", "credential", "verify", "report"}
    assert phase["label"]
