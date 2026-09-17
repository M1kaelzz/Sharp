"""维护与存储概览（P2-E）。

**为什么要给人看**：留存策略（每会话 200 条消息 / 每 store 50 个会话 / 门控 VACUUM）都是**静默**生效的 ——
用户没有任何办法回答"这个库现在多大、是哪张表在长、上次回收是什么时候"。没有可见性的时候，
"配额"只是一种信念。这个端点把它变成可以随时检查的事实。
"""

from __future__ import annotations

from fastapi import APIRouter

from sharp.server.chat_retention import CHAT_RETENTION_SESSIONS
from sharp.server.db import current_path, get_conn
from sharp.server.db_maintenance import last_vacuum_at
from sharp.server.routers.chat import CHAT_RETENTION_PER_SESSION

router = APIRouter(tags=["maintenance"])

# 这些表按行数排不进"增长观察"的重点（本来就是有界的），但也不必隐藏 —— 直接全列。
_SKIP_TABLES = {"sqlite_sequence"}


@router.get("/maintenance/storage")
def storage_overview():
    """存储概览：文件大小、可回收空间、逐表行数、聊天留存口径、上次 VACUUM 时间。

    只读，不触发任何回收动作（回收由启动时的门控维护负责，见 `db_maintenance.py`）。
    """
    path = current_path()
    with get_conn() as conn:
        tables = [
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            ).fetchall()
            if row["name"] not in _SKIP_TABLES
        ]
        counts = {}
        for name in tables:
            try:
                counts[name] = int(
                    conn.execute(f"SELECT COUNT(*) c FROM {name}").fetchone()["c"]
                )
            except Exception:  # 表被并发删除等极端情况：跳过而不是整个端点失败
                continue

        page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
        freelist = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
        chat_counts = {
            key: counts.get(key, 0)
            for key in ("chat_sessions", "chat_messages",
                        "android_chat_sessions", "android_chat_messages")
        }
        vacuumed = last_vacuum_at(conn)

    def _size(suffix: str) -> int:
        candidate = path.with_name(path.name + suffix)
        return candidate.stat().st_size if candidate.exists() else 0

    return {
        "db_path": str(path),
        "db_bytes": path.stat().st_size if path.exists() else 0,
        "wal_bytes": _size("-wal"),
        "freelist_bytes": freelist * page_size,
        "table_rows": dict(sorted(counts.items(), key=lambda kv: kv[1], reverse=True)),
        "chat": {
            **chat_counts,
            "retention_messages_per_session": CHAT_RETENTION_PER_SESSION,
            "retention_sessions_per_store": CHAT_RETENTION_SESSIONS,
        },
        "last_vacuum_at": vacuumed,
    }
