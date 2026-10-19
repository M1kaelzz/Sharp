"""SQL for the ``android_chat_sessions`` / ``android_chat_messages`` tables.

These back the Android APK chat conversations (as opposed to the generic
``chat_sessions``/``chat_messages`` in ``chat.py``). Functions take an open
connection and return rows / None; callers own the transaction and any HTTP
error handling.
"""

from __future__ import annotations

import sqlite3


def get_session(
    conn: sqlite3.Connection, session_id: str
) -> sqlite3.Row | None:
    """One Android chat session, or None. Caller decides the 404."""
    return conn.execute(
        "SELECT * FROM android_chat_sessions WHERE id = ?", (session_id,)
    ).fetchone()


def list_sessions(conn: sqlite3.Connection, limit: int = 50) -> list[sqlite3.Row]:
    """All Android chat sessions, most-recently-active first."""
    return conn.execute(
        "SELECT id, apk_path, apk_filename, created_at, last_active_at "
        "FROM android_chat_sessions ORDER BY last_active_at DESC LIMIT ?",
        (limit,),
    ).fetchall()


def insert_session(
    conn: sqlite3.Connection,
    session_id: str,
    apk_path: str,
    apk_filename: str,
    apk_context: str,
    created_at: str,
) -> None:
    conn.execute(
        "INSERT INTO android_chat_sessions "
        "(id, apk_path, apk_filename, apk_context, created_at, last_active_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (session_id, apk_path, apk_filename, apk_context, created_at, created_at),
    )


def touch_session(
    conn: sqlite3.Connection, session_id: str, now: str
) -> None:
    conn.execute(
        "UPDATE android_chat_sessions SET last_active_at = ? WHERE id = ?",
        (now, session_id),
    )


def list_messages(
    conn: sqlite3.Connection, session_id: str
) -> list[sqlite3.Row]:
    """All chat messages for a session, oldest first."""
    return conn.execute(
        "SELECT id, role, content, created_at FROM android_chat_messages "
        "WHERE session_id = ? ORDER BY id ASC",
        (session_id,),
    ).fetchall()


def list_history(
    conn: sqlite3.Connection, session_id: str, limit: int
) -> list[sqlite3.Row]:
    """Most-recent messages for building LLM history, newest first."""
    return conn.execute(
        "SELECT role, content FROM android_chat_messages "
        "WHERE session_id = ? ORDER BY id DESC LIMIT ?",
        (session_id, limit),
    ).fetchall()


# DB retention per session (batch B1): LLM context is capped at
# MAX_HISTORY_MESSAGES, but the table itself used to grow without bound. Keep
# the newest N rows per session so history replay stays possible while long
# conversations stop inflating the database forever.
CHAT_RETENTION_PER_SESSION = 200


def add_message(
    conn: sqlite3.Connection, session_id: str, role: str, content: str, created_at: str
) -> None:
    conn.execute(
        "INSERT INTO android_chat_messages (session_id, role, content, created_at) "
        "VALUES (?, ?, ?, ?)",
        (session_id, role, content, created_at),
    )
    _trim_session(conn, session_id)


def _trim_session(conn: sqlite3.Connection, session_id: str) -> None:
    """Drop rows older than the newest CHAT_RETENTION_PER_SESSION for a session."""
    conn.execute(
        "DELETE FROM android_chat_messages WHERE session_id = ? AND id <= ("
        "SELECT id FROM android_chat_messages WHERE session_id = ? "
        "ORDER BY id DESC LIMIT 1 OFFSET ?)",
        (session_id, session_id, CHAT_RETENTION_PER_SESSION),
    )


def delete_messages(conn: sqlite3.Connection, session_id: str) -> None:
    conn.execute(
        "DELETE FROM android_chat_messages WHERE session_id = ?", (session_id,)
    )


def update_dynamic_state(
    conn: sqlite3.Connection,
    session_id: str,
    *,
    mode: str,
    container_name: str,
    claude_session_id: str,
    device_info: str,
    now: str,
) -> None:
    """Switch a session into/out of dynamic mode and record its container /
    claude session binding (see android_chat_sessions columns)."""
    conn.execute(
        "UPDATE android_chat_sessions SET mode = ?, container_name = ?, "
        "claude_session_id = ?, device_info = ?, last_active_at = ? WHERE id = ?",
        (mode, container_name, claude_session_id, device_info, now, session_id),
    )


def clear_dynamic_state(
    conn: sqlite3.Connection, session_id: str, now: str
) -> None:
    """Drop the dynamic binding (used on stop / session reset)."""
    conn.execute(
        "UPDATE android_chat_sessions SET mode = 'chat', container_name = '', "
        "claude_session_id = '', device_info = '', last_active_at = ? WHERE id = ?",
        (now, session_id),
    )
