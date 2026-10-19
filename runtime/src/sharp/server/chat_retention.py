"""会话级留存（P2-E）：把"聊天数据能长到多大"钉在明确的数字上。

**核对过的现状**（这条待办大部分早已实现，先记清楚，避免重复造）：

| 机制 | 状态 |
|---|---|
| 单个会话的消息条数 | ✅ 已有上限（`CHAT_RETENTION_PER_SESSION = 200`，两条聊天路径都有，追加时即裁剪） |
| 删会话是否连带删消息 | ✅ 两个 store 的消息表都带 `ON DELETE CASCADE` |
| DB 文件回收 | ✅ 已有门控 VACUUM（30 天一次，或可回收 100MB 时触发，`db_maintenance.py`） |
| 调度器状态行 | ✅ 已有 90 秒窗口清理（dispatcher 每 ~5s 心跳，超时即视为僵尸实例） |
| 会话上下文体积 | ✅ 是**有界摘要**（计数 + top-30 文件类型 + 提示语），不是全量文件清单 |
| **会话数量** | ❌ **此前没有任何上限** —— 每个会话带一份上下文，数量无界累积 |

所以这里补的只有最后一行：**保留最近 N 个会话，更老的连消息一起删**（靠级联）。

为什么按 `last_active_at` 而不是 `created_at` 排序：会话是可以长期复用的 —— 一个几周前创建、
但昨天还在聊的会话，比一个小时前创建、再没动过的更值得留。
"""

from __future__ import annotations

import logging
import sqlite3

LOG = logging.getLogger(__name__)

# 每个 store 保留的会话数。取 50 是"远大于正常使用、又足以兜住失控累积"的量级：
# 单机单用户的工具，同时活跃的会话通常个位数。
CHAT_RETENTION_SESSIONS = 50


def _prune(
    conn: sqlite3.Connection,
    *,
    store: str,
    sessions_table: str,
    keep: int,
) -> int:
    """保留最近 `keep` 个会话，其余删除（消息靠外键级联）。

    `sessions_table` 只由本模块的两个公开函数传入**字面量**，不接受外部输入 ——
    表名要拼进 SQL，这是唯一的防线，所以刻意不做成"通用任意表裁剪"。
    """
    try:
        total = conn.execute(f"SELECT COUNT(*) c FROM {sessions_table}").fetchone()["c"]
        if total <= keep:
            return 0
        cursor = conn.execute(
            f"DELETE FROM {sessions_table} WHERE id IN ("
            f"  SELECT id FROM {sessions_table} "
            f"  ORDER BY last_active_at DESC, created_at DESC LIMIT -1 OFFSET ?)",
            (keep,),
        )
        removed = cursor.rowcount or 0
        if removed:
            LOG.info("pruned chat sessions store=%s removed=%s keep=%s", store, removed, keep)
        return removed
    except sqlite3.Error as exc:
        # 留存是维护动作：失败不该影响"新建会话"这个主操作
        LOG.warning("chat session prune failed store=%s error=%s", store, exc)
        return 0


def prune_chat_sessions(conn: sqlite3.Connection, *, keep: int = CHAT_RETENTION_SESSIONS) -> int:
    """通用聊天的会话留存。"""
    return _prune(conn, store="chat", sessions_table="chat_sessions", keep=keep)


def prune_android_chat_sessions(
    conn: sqlite3.Connection, *, keep: int = CHAT_RETENTION_SESSIONS
) -> int:
    """Android 分析对话的会话留存。"""
    return _prune(conn, store="android_chat", sessions_table="android_chat_sessions", keep=keep)
