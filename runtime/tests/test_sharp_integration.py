"""场景 A 集成测试：Sharp 工具主流程 + 今次 bug 修复验证

覆盖：
  1. 主流程：创建项目 → 创建 intent → conclude fact → 项目完成
  2. Bug1 修复：publish() 不再是死代码，SSE 事件确实被推送
  3. Bug2 修复：events.publish() 线程安全（从子线程调用不崩溃）
  4. Settings：report_instructions 读写 + 旧库迁移
  5. project ID gap-filling（删除后编号重用）
  6. DB 旧库迁移（无 report_instructions 列的旧库自动升级）
"""
from __future__ import annotations

import asyncio
import sqlite3
import threading
import time

import pytest

from sharp.server import db
from sharp.server.events import publish, set_event_loop, subscribe
from sharp.server.models import (
    CompleteRequest,
    ConcludeRequest,
    CreateIntentRequest,
)
from sharp.server.routers.intents import conclude, create_intent
from sharp.server.routers.projects import complete_project, create_project
from sharp.server.models import CreateProjectRequest
from sharp.server.services import next_project_id, utcnow


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """每个测试使用独立的全新 SQLite 数据库。"""
    monkeypatch.setattr(db, "_db_path", None, raising=False)
    db.configure(tmp_path / "sharp.db")
    yield


@pytest.fixture
def seeded_db(fresh_db):
    """预置一个 active 项目 p1 + origin/goal facts。"""
    with db.get_conn() as conn:
        now = utcnow()
        conn.execute(
            "INSERT INTO projects (id, title, status, created_at) VALUES (?, ?, 'active', ?)",
            ("p1", "测试项目", now),
        )
        conn.execute("INSERT INTO facts (id, project_id, description) VALUES ('origin', 'p1', '起点')")
        conn.execute("INSERT INTO facts (id, project_id, description) VALUES ('goal', 'p1', '目标')")
    yield


# ── 1. 主流程 ─────────────────────────────────────────────────────────────────

def test_create_project_returns_correct_id(fresh_db):
    """创建项目返回 proj_001，包含 origin/goal。"""
    req = CreateProjectRequest(title="Web渗透", origin="http://target.com", goal="拿到flag")
    result = create_project(req)
    assert result.project.id == "proj_001"
    assert result.project.title == "Web渗透"
    assert result.project.status == "active"
    assert any(f.id == "origin" for f in result.facts)
    assert any(f.id == "goal" for f in result.facts)


def test_create_intent_and_conclude_full_flow(seeded_db):
    """主流程：create_intent → conclude → fact 存在于 DB。"""
    # 创建 intent
    req = CreateIntentRequest(**{"from": ["origin"]}, description="扫描端口", creator="scanner")
    intent = create_intent("p1", req)
    assert intent.id == "i001"
    assert intent.to is None

    # conclude：生成新 fact
    conclude_req = ConcludeRequest(
        description="开放端口：80, 443, 8080",
        worker="scanner",
    )
    resp = conclude("p1", "i001", conclude_req)
    assert resp.fact.id == "f001"
    assert "80" in resp.fact.description
    assert resp.intent.concluded_at is not None

    # 验证 fact 写入 DB
    with db.get_conn() as conn:
        row = conn.execute("SELECT description FROM facts WHERE id='f001' AND project_id='p1'").fetchone()
    assert row is not None
    assert "80" in row["description"]


def test_complete_project_changes_status(seeded_db):
    """完成项目后状态变为 completed。"""
    req = CompleteRequest(**{"from": ["origin"]}, description="获得 shell", worker="attacker")
    result = complete_project("p1", req)
    assert result.to == "goal"
    assert result.concluded_at is not None

    with db.get_conn() as conn:
        row = conn.execute("SELECT status FROM projects WHERE id='p1'").fetchone()
    assert row["status"] == "completed"


# ── 2. Bug1 修复：publish() 不再是死代码 ──────────────────────────────────────

def test_create_intent_publishes_sse_event(seeded_db):
    """Bug1 修复验证：create_intent 返回后 SSE 事件被推送到队列。

    修复前：publish() 在 return 之后（死代码），事件永远不会被推送。
    修复后：result 先存变量，with 块结束后再 publish，再 return。
    """
    loop = asyncio.new_event_loop()
    set_event_loop(loop)

    received: list[str] = []

    async def collect():
        async for line in subscribe("p1"):
            received.append(line)
            break  # 收到第一条就退出

    async def run_all():
        # 并发：先订阅，再创建 intent（触发 publish）
        task = asyncio.create_task(collect())
        await asyncio.sleep(0.05)  # 让 subscribe 先挂上队列

        def call_create():
            req = CreateIntentRequest(**{"from": ["origin"]}, description="测试", creator="tester")
            create_intent("p1", req)

        loop.run_in_executor(None, call_create)
        await asyncio.wait_for(task, timeout=3.0)

    loop.run_until_complete(run_all())
    loop.close()

    assert len(received) == 1, "应该收到 1 条 SSE 事件"
    assert "intent_created" in received[0]


def test_conclude_publishes_fact_created_event(seeded_db):
    """Bug1 修复：conclude 后触发 fact_created SSE 事件。"""
    # 先创建 intent
    create_intent("p1", CreateIntentRequest(**{"from": ["origin"]}, description="测试", creator="w1"))

    loop = asyncio.new_event_loop()
    set_event_loop(loop)
    received: list[str] = []

    async def collect():
        async for line in subscribe("p1"):
            received.append(line)
            break

    async def run_all():
        task = asyncio.create_task(collect())
        await asyncio.sleep(0.05)

        def call_conclude():
            conclude("p1", "i001", ConcludeRequest(description="发现 SQLi", worker="w1"))

        loop.run_in_executor(None, call_conclude)
        await asyncio.wait_for(task, timeout=3.0)

    loop.run_until_complete(run_all())
    loop.close()

    assert any("fact_created" in e for e in received)


def test_complete_project_publishes_completed_event(seeded_db):
    """Bug1 修复：complete_project 后触发 project_completed SSE 事件。"""
    loop = asyncio.new_event_loop()
    set_event_loop(loop)
    received: list[str] = []

    async def collect():
        async for line in subscribe("p1"):
            received.append(line)
            if "project_completed" in line:
                break

    async def run_all():
        task = asyncio.create_task(collect())
        await asyncio.sleep(0.05)

        def call_complete():
            complete_project("p1", CompleteRequest(**{"from": ["origin"]}, description="验证完成：目标条件已满足", worker="w1"))

        loop.run_in_executor(None, call_complete)
        await asyncio.wait_for(task, timeout=3.0)

    loop.run_until_complete(run_all())
    loop.close()

    assert any("project_completed" in e for e in received), f"未收到 project_completed 事件，收到：{received}"


# ── 3. Bug2 修复：events.publish 线程安全 ─────────────────────────────────────

def test_publish_from_thread_does_not_crash(seeded_db):
    """Bug2 修复：从子线程调用 publish() 不崩溃，事件正确入队。

    修复前：asyncio.Queue.put_nowait() 在非 event-loop 线程中调用 Future.set_result()
    导致竞态，可能崩溃或静默丢失事件。
    修复后：使用 call_soon_threadsafe 调度回 event-loop 线程。
    """
    loop = asyncio.new_event_loop()
    set_event_loop(loop)
    received: list[str] = []
    errors: list[str] = []

    async def collect():
        async for line in subscribe("p1"):
            received.append(line)
            break

    async def run_all():
        task = asyncio.create_task(collect())
        await asyncio.sleep(0.05)

        # 从多个子线程并发 publish，测试线程安全
        def publisher(i: int):
            try:
                publish("p1", "test_event", {"seq": i})
            except Exception as e:
                errors.append(str(e))

        threads = [threading.Thread(target=publisher, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        await asyncio.wait_for(task, timeout=3.0)

    loop.run_until_complete(run_all())
    loop.close()

    assert not errors, f"线程中调用 publish 出错：{errors}"
    assert len(received) >= 1, "至少应收到 1 条事件"


# ── 4. Settings report_instructions 读写 ────────────────────────────────────

def test_settings_report_instructions_default_empty(fresh_db):
    """新库中 report_instructions 默认为空字符串。"""
    with db.get_conn() as conn:
        row = conn.execute("SELECT report_instructions FROM settings WHERE rowid=1").fetchone()
    assert row is not None
    assert row["report_instructions"] == ""


def test_settings_report_instructions_roundtrip(fresh_db):
    """report_instructions 写入后可读回。"""
    custom = "报告使用英文；每个漏洞附 CVSS 评分"
    with db.get_conn() as conn:
        conn.execute("UPDATE settings SET report_instructions=? WHERE rowid=1", (custom,))

    with db.get_conn() as conn:
        row = conn.execute("SELECT report_instructions FROM settings WHERE rowid=1").fetchone()
    assert row["report_instructions"] == custom


def test_report_prompt_includes_custom_instructions():
    """_engineered_report_intent_description 正确拼接自定义指令。"""
    from sharp.server.reports import _engineered_report_intent_description
    d = _engineered_report_intent_description("测试项目", "报告用英文")
    assert "【自定义补充要求】报告用英文" in d

    d_empty = _engineered_report_intent_description("测试项目", "")
    assert "【自定义补充要求】" not in d_empty

    d_whitespace = _engineered_report_intent_description("测试项目", "   ")
    assert "【自定义补充要求】" not in d_whitespace


# ── 5. project ID gap-filling ────────────────────────────────────────────────

def test_project_id_starts_at_001(fresh_db):
    """空库中第一个项目应为 proj_001。"""
    with db.get_conn() as conn:
        pid = next_project_id(conn)
    assert pid == "proj_001"


def test_project_id_fills_gap_after_delete(fresh_db):
    """删除 proj_002 后，新建项目应重用 proj_002 而不是 proj_004。"""
    with db.get_conn() as conn:
        now = utcnow()
        for i in range(1, 4):
            conn.execute(
                "INSERT INTO projects (id, title, status, created_at) VALUES (?,?,?,?)",
                (f"proj_{i:03d}", f"p{i}", "active", now),
            )
        # 删除 proj_002
        conn.execute("DELETE FROM projects WHERE id='proj_002'")

    with db.get_conn() as conn:
        pid = next_project_id(conn)
    assert pid == "proj_002", f"应填补空缺 proj_002，实际得到 {pid}"


def test_project_id_skips_existing(fresh_db):
    """已存在 001/003/005 时，依次分配 002、004、006、007。"""
    with db.get_conn() as conn:
        now = utcnow()
        for n in [1, 3, 5]:
            conn.execute(
                "INSERT INTO projects (id, title, status, created_at) VALUES (?,?,?,?)",
                (f"proj_{n:03d}", f"p{n}", "active", now),
            )

    results = []
    for _ in range(4):
        with db.get_conn() as conn:
            pid = next_project_id(conn)
            conn.execute(
                "INSERT INTO projects (id, title, status, created_at) VALUES (?,?,?,?)",
                (pid, "x", "active", utcnow()),
            )
            results.append(pid)

    assert results == ["proj_002", "proj_004", "proj_006", "proj_007"]


# ── 6. DB 旧库迁移 ────────────────────────────────────────────────────────────

def test_migration_adds_report_instructions_to_old_db(tmp_path, monkeypatch):
    """旧库（无 report_instructions 列）启动后自动迁移，旧数据保留。"""
    old_db = tmp_path / "old.db"

    # 手工创建旧版 settings 表（无 report_instructions 列）
    conn = sqlite3.connect(str(old_db))
    conn.execute("CREATE TABLE settings (intent_timeout INTEGER NOT NULL DEFAULT 15, reason_timeout INTEGER NOT NULL DEFAULT 15)")
    conn.execute("INSERT INTO settings (rowid, intent_timeout, reason_timeout) VALUES (1, 20, 25)")
    conn.commit()
    conn.close()

    # 用新版 db.configure() 触发迁移
    monkeypatch.setattr(db, "_db_path", None, raising=False)
    db.configure(old_db)

    with db.get_conn() as conn:
        row = conn.execute("SELECT intent_timeout, reason_timeout, report_instructions FROM settings WHERE rowid=1").fetchone()

    assert row["intent_timeout"] == 20, "旧数据 intent_timeout 应保留"
    assert row["reason_timeout"] == 25, "旧数据 reason_timeout 应保留"
    assert row["report_instructions"] == "", "新列应默认为空字符串"


def test_migration_adds_report_instructions_column(tmp_path, monkeypatch):
    """迁移后 settings 表的列名包含 report_instructions。"""
    old_db = tmp_path / "old2.db"
    conn = sqlite3.connect(str(old_db))
    conn.execute("CREATE TABLE settings (intent_timeout INTEGER NOT NULL DEFAULT 15, reason_timeout INTEGER NOT NULL DEFAULT 15)")
    conn.execute("INSERT INTO settings (rowid, intent_timeout, reason_timeout) VALUES (1, 15, 15)")
    conn.commit()
    conn.close()

    monkeypatch.setattr(db, "_db_path", None, raising=False)
    db.configure(old_db)

    with db.get_conn() as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(settings)")]
    assert "report_instructions" in cols
