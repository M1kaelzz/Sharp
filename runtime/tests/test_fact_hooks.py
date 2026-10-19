"""写 fact 之后的统一副作用（P2-D）。

**为什么需要这一层**：本项目反复出现同一类缺陷 —— **某个能力只接在一条路径上**。
`facts` 有五个写入入口（行动结论 / chat 写证据图 / 重开的外部反馈 / Android 分析 / 小程序分析），
而副作用只有主链做了一半：登记接口台账；知识提取更是只由 dispatcher 在 explore/bootstrap 之后触发。

于是"从 chat 或分析器写进去的证据"既不进台账、也不产出任何知识 —— **同一条证据，
走哪个入口决定了它有没有沉淀价值**。这里把这个口径钉住。
"""

from __future__ import annotations

import re

import pytest

from sharp.server import db


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
    r = client.post("/projects", json={"title": "t", "origin": origin, "goal": "g"}, headers=_h())
    assert r.status_code == 201, r.text
    return r.json()["project"]["id"]


# ── 结构事实不参与沉淀 ─────────────────────────────────────────────────────

def test_structural_facts_are_skipped(conn, client):
    """origin/goal 是**输入**不是产出 —— 实测教训：goal 文本里的"无效发现"曾被
    当成"已验证不通"的结论。显式排除，避免任何入口把输入当产出。"""
    from sharp.server.fact_hooks import after_fact_write

    pid = _project(client)
    for fid, text in (("origin", "目标：https://app.example.com/"), ("goal", "尽量发现漏洞")): 
        result = after_fact_write(conn, project_id=pid, fact_id=fid, description=text, now="T")
        assert result == {"endpoints": 0, "knowledge": 0}
    assert conn.execute("SELECT COUNT(*) c FROM asset_endpoints").fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM knowledge_base").fetchone()["c"] == 0


def test_empty_description_is_skipped(conn, client):
    from sharp.server.fact_hooks import after_fact_write

    pid = _project(client)
    assert after_fact_write(conn, project_id=pid, fact_id="f001", description="   ", now="T") == {
        "endpoints": 0, "knowledge": 0,
    }


# ── 两个副作用都执行 ───────────────────────────────────────────────────────

def test_hook_registers_endpoints_and_knowledge(conn, client):
    from sharp.server.fact_hooks import after_fact_write

    pid = _project(client)
    result = after_fact_write(
        conn, project_id=pid, fact_id="f001", now="2026-09-15T00:00:00Z",
        description="确认 https://app.example.com/api/v1/users 未授权可读；/admin 的 403 无法绕过。",
    )
    assert result["endpoints"] >= 1, "完整 URL 必须进接口台账"
    assert result["knowledge"] >= 1, "结论必须产出跨项目知识"
    kinds = {r["kind"] for r in conn.execute(
        "SELECT kind FROM knowledge_base WHERE root_domain = 'domain:example.com'"
    ).fetchall()}
    assert "dead_end" in kinds


def test_failures_never_break_the_write(conn, client, monkeypatch):
    """沉底必须是 best-effort：调用方在事务里，沉淀抛异常会把**证据本身**一起回滚 —— 最坏的结果。"""
    from sharp.server import fact_hooks

    pid = _project(client)

    def boom(*args, **kwargs):
        raise RuntimeError("模拟沉淀故障")

    monkeypatch.setattr("sharp.server.asset_endpoints.register_endpoints_from_fact", boom)
    monkeypatch.setattr("sharp.server.routers.knowledge.extract_knowledge_for_fact", boom)
    assert fact_hooks.after_fact_write(
        conn, project_id=pid, fact_id="f001", description="有内容", now="T"
    ) == {"endpoints": 0, "knowledge": 0}


# ── 各入口真的调用了它 ─────────────────────────────────────────────────────

def test_chat_write_persists_endpoints_and_knowledge(client):
    """chat「把对话中的发现写入证据图」—— 此前这条路径什么都不产出。

    走真实路由 `POST /android/chat/sessions/{sid}/push-fact`。

    前置条件用仓储直接建会话行（不是走 `POST /sessions`）—— 那个端点要先真分析一个 APK 文件，
    单元测试里不该为此在用户主目录里造文件。会话行本身不是被测对象，被测的是 push-fact 的副作用。
    """
    pid = _project(client)
    session_id = "s_test_hooks"
    with db.get_conn() as c:
        from sharp.server.repository import android_chat as chat_repo

        chat_repo.insert_session(c, session_id, "/tmp/app.apk", "app.apk", "{}", "2026-09-15T00:00:00Z")

    r = client.post(
        f"/android/chat/sessions/{session_id}/push-fact",
        json={"project_id": pid, "kind": "fact",
              "content": "调试确认 https://app.example.com/api/v1/debug 未授权可读；/admin 均返回 404。"},
        headers=_h(),
    )
    assert r.status_code == 201, r.text
    with db.get_conn() as c:
        assert c.execute("SELECT COUNT(*) c FROM asset_endpoints").fetchone()["c"] >= 1, \
            "chat 写进的证据也要进接口台账"
        assert c.execute("SELECT COUNT(*) c FROM knowledge_base").fetchone()["c"] >= 1, \
            "chat 写进的证据也要产出跨项目知识"


def test_every_fact_writing_module_calls_the_hook():
    """**源码级结构断言**：凡是往 `facts` 里写的模块，都必须调用统一副作用。

    行为测试覆盖不到"某个入口忘了调" —— 这正是这个缺陷能长期存在的原因。

    例外清单（每个都要有理由，不是"顺手放过"）：
    - `repository/facts.py` / `db.py`：仓储层与建表本身
    - `fact_hooks.py`：这个约定的实现处
    - `routers/reports.py`：写的是**报告生成请求的上下文**（机器生成的 JSON blob），
      不是证据 —— 拿它去抽取只会往台账与知识库里灌垃圾
    """
    from pathlib import Path

    server_root = Path(__file__).resolve().parents[1] / "src" / "sharp" / "server"
    offenders: list[str] = []
    for path in sorted(server_root.rglob("*.py")):
        rel = path.relative_to(server_root).as_posix()
        if rel in ("repository/facts.py", "db.py", "fact_hooks.py", "routers/reports.py"):
            continue
        text = path.read_text(encoding="utf-8")
        writes_facts = "facts_repo.insert" in text or "INSERT INTO facts" in text
        if not writes_facts:
            continue
        # 要求存在**调用**而不是"名字出现过"：第一版只查子串，把调用删掉、只留 import
        # （或反之）都能蒙过去 —— 守卫必须盯住真正生效的那一行。
        if not re.search(r"after_fact_write\s*\(", text):
            offenders.append(rel)
    assert not offenders, f"这些模块写了 fact 却没走统一副作用：{offenders}"


def test_hook_is_documented_in_architecture():
    """这类'收口'必须写进架构文档，否则下一个人加入口时不知道有这个约定。"""
    from pathlib import Path

    doc = (Path(__file__).resolve().parents[1].parent / "docs" / "ARCHITECTURE.md").read_text(
        encoding="utf-8"
    )
    assert "after_fact_write" in doc
