"""会话级留存与存储可见性（P2-E）。

**先说清这条待办的核对结果** —— 它大部分早已实现，盲改只会重复造轮子：

| 机制 | 核对结论 |
|---|---|
| 单会话消息条数 | ✅ 已有上限（200，两条聊天路径都在追加时裁剪） |
| 删会话连带删消息 | ✅ 两个 store 的消息表都带 `ON DELETE CASCADE` |
| DB 文件回收 | ✅ 门控 VACUUM（30 天一次，或可回收 100MB 时触发） |
| 调度器状态行 | ✅ 90 秒窗口清理 |
| 会话上下文体积 | ✅ 有界摘要，不是全量文件清单 |
| **会话数量** | ❌ 此前无上限 —— 本批补的就是这一格 |

另加一个存储概览端点：留存策略都是静默生效的，没有可见性时"配额"只是一种信念。
"""

from __future__ import annotations

import pytest

from sharp.server import db
from sharp.server.chat_retention import (
    CHAT_RETENTION_SESSIONS,
    prune_android_chat_sessions,
    prune_chat_sessions,
)


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


def _seed_sessions(conn, table: str, n: int, *, store: str = "chat") -> None:
    for i in range(n):
        conn.execute(
            f"INSERT INTO {table} (id, role, context, project_id, created_at, last_active_at) "
            f"VALUES (?, 'assistant', '', NULL, ?, ?)"
            if store == "chat" else
            f"INSERT INTO {table} (id, apk_path, apk_filename, apk_context, created_at, last_active_at) "
            f"VALUES (?, '/tmp/a.apk', 'a.apk', '{{}}', ?, ?)",
            (f"s{i:03d}", f"2026-09-15T00:00:{i:02d}Z", f"2026-09-15T00:00:{i:02d}Z"),
        )


# ── 会话数量留存 ───────────────────────────────────────────────────────────

def test_sessions_beyond_cap_are_pruned(conn):
    _seed_sessions(conn, "chat_sessions", CHAT_RETENTION_SESSIONS + 5)
    removed = prune_chat_sessions(conn)
    assert removed == 5
    rows = conn.execute("SELECT id FROM chat_sessions ORDER BY id").fetchall()
    assert len(rows) == CHAT_RETENTION_SESSIONS
    # 保留的是**最近活跃**的（id 越大 last_active_at 越晚）
    assert rows[0]["id"] == "s005"


def test_prune_is_noop_below_cap(conn):
    _seed_sessions(conn, "chat_sessions", 3)
    assert prune_chat_sessions(conn) == 0
    assert conn.execute("SELECT COUNT(*) c FROM chat_sessions").fetchone()["c"] == 3


def test_pruning_a_session_removes_its_messages(conn):
    """靠外键级联：会话删了、消息必须跟着走，否则消息表照样无限长。"""
    _seed_sessions(conn, "chat_sessions", CHAT_RETENTION_SESSIONS + 1)
    conn.execute(
        "INSERT INTO chat_messages (session_id, role, content, created_at) VALUES ('s000','user','x','T')"
    )
    assert conn.execute("SELECT COUNT(*) c FROM chat_messages").fetchone()["c"] == 1
    prune_chat_sessions(conn)
    assert conn.execute("SELECT COUNT(*) c FROM chat_messages").fetchone()["c"] == 0


def test_recency_is_by_last_active_not_created(conn):
    """按 `last_active_at` 排序：几周前创建但昨天还在聊的会话，比刚建就没动过的更值得留。"""
    _seed_sessions(conn, "chat_sessions", CHAT_RETENTION_SESSIONS)
    conn.execute(
        "INSERT INTO chat_sessions (id, role, context, project_id, created_at, last_active_at) "
        "VALUES ('old_but_active','assistant','',NULL,'2026-08-01T00:00:00Z','2026-09-15T09:00:00Z')"
    )
    prune_chat_sessions(conn)   # 共 51 个 → 删最老的一个活跃时间
    ids = {r["id"] for r in conn.execute("SELECT id FROM chat_sessions").fetchall()}
    assert "old_but_active" in ids, "活跃时间最新的会话不该被删"
    assert "s000" not in ids


def test_android_store_has_its_own_cap(conn):
    _seed_sessions(conn, "android_chat_sessions", CHAT_RETENTION_SESSIONS + 2, store="android")
    assert prune_android_chat_sessions(conn) == 2
    assert conn.execute("SELECT COUNT(*) c FROM android_chat_sessions").fetchone()["c"] == CHAT_RETENTION_SESSIONS


def test_prune_failure_never_breaks_the_caller(conn, monkeypatch):
    """留存是维护动作：失败不能影响"新建会话"这个主操作。"""
    import sharp.server.chat_retention as ret

    conn.execute("DROP TABLE chat_sessions")
    assert ret.prune_chat_sessions(conn) == 0


# ── 建会话时自动裁剪 ───────────────────────────────────────────────────────

def test_creating_a_session_prunes_old_ones(client):
    pid_context = "ctx"
    with db.get_conn() as c:
        _seed_sessions(c, "chat_sessions", CHAT_RETENTION_SESSIONS)
    r = client.post("/chat/sessions", json={"role": "assistant", "context": pid_context}, headers=_h())
    assert r.status_code in (200, 201), r.text
    with db.get_conn() as c:
        total = c.execute("SELECT COUNT(*) c FROM chat_sessions").fetchone()["c"]
    assert total == CHAT_RETENTION_SESSIONS, f"新建会话后应仍在上限内，实得 {total}"


# ── 存储可见性 ─────────────────────────────────────────────────────────────

def test_storage_overview_reports_growth(client):
    r = client.get("/maintenance/storage", headers=_h())
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["db_bytes"] > 0
    assert data["table_rows"]["projects"] >= 0
    assert data["chat"]["retention_messages_per_session"] == 200
    assert data["chat"]["retention_sessions_per_store"] == CHAT_RETENTION_SESSIONS
    assert "last_vacuum_at" in data
    # 行数按降序排（一眼看出哪张表在长）
    values = list(data["table_rows"].values())
    assert values == sorted(values, reverse=True)


def test_storage_overview_is_read_only(client):
    client.get("/maintenance/storage", headers=_h())
    with db.get_conn() as c:
        assert c.execute("SELECT COUNT(*) c FROM chat_sessions").fetchone()["c"] == 0
