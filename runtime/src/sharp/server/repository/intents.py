"""SQL for the ``intents`` and ``intent_sources`` tables.

Functions take an open connection and return rows / None; callers own the
transaction and any HTTP error handling. Row->model conversion stays with the
callers (see ``services.intent_to_model``).
"""

from __future__ import annotations

import sqlite3

_COLUMNS = (
    "id, project_id, to_fact_id, description, creator, worker, "
    "last_heartbeat_at, created_at, concluded_at, idempotency_key, "
    "risk_level, risk_reason, approval_status, approval_note, approval_decided_at"
)


def insert(
    conn: sqlite3.Connection,
    intent_id: str,
    project_id: str,
    *,
    to_fact_id: str | None,
    description: str,
    creator: str | None,
    worker: str | None,
    last_heartbeat_at: str | None,
    created_at: str,
    concluded_at: str | None,
    idempotency_key: str | None = None,
    risk_level: str = "low",
    risk_reason: str = "",
    approval_status: str = "none",
    approval_note: str = "",
    approval_decided_at: str | None = None,
) -> None:
    conn.execute(
        f"INSERT INTO intents ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            intent_id,
            project_id,
            to_fact_id,
            description,
            creator,
            worker,
            last_heartbeat_at,
            created_at,
            concluded_at,
            idempotency_key,
            risk_level,
            risk_reason,
            approval_status,
            approval_note,
            approval_decided_at,
        ),
    )


def add_source(
    conn: sqlite3.Connection, intent_id: str, project_id: str, fact_id: str
) -> None:
    conn.execute(
        "INSERT INTO intent_sources (intent_id, project_id, fact_id) VALUES (?, ?, ?)",
        (intent_id, project_id, fact_id),
    )


def fetch(
    conn: sqlite3.Connection, intent_id: str, project_id: str
) -> sqlite3.Row | None:
    """One intent by id within a project, or None."""
    return conn.execute(
        "SELECT * FROM intents WHERE id = ? AND project_id = ?",
        (intent_id, project_id),
    ).fetchone()


def list_for_project(
    conn: sqlite3.Connection, project_id: str
) -> list[sqlite3.Row]:
    """All intents for a project, oldest first."""
    return conn.execute(
        "SELECT * FROM intents WHERE project_id = ? ORDER BY created_at",
        (project_id,),
    ).fetchall()


def list_source_fact_ids(
    conn: sqlite3.Connection, intent_id: str, project_id: str
) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT fact_id FROM intent_sources "
        "WHERE intent_id = ? AND project_id = ? ORDER BY rowid",
        (intent_id, project_id),
    ).fetchall()


def delete(conn: sqlite3.Connection, intent_id: str, project_id: str) -> None:
    conn.execute(
        "DELETE FROM intents WHERE id = ? AND project_id = ?",
        (intent_id, project_id),
    )


def release_workers_for_project(conn: sqlite3.Connection, project_id: str) -> None:
    conn.execute(
        "UPDATE intents SET worker = NULL "
        "WHERE project_id = ? AND concluded_at IS NULL",
        (project_id,),
    )


def find_by_idempotency_key(
    conn: sqlite3.Connection, project_id: str, idempotency_key: str
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM intents WHERE project_id = ? AND idempotency_key = ?",
        (project_id, idempotency_key),
    ).fetchone()


def claim(
    conn: sqlite3.Connection,
    intent_id: str,
    project_id: str,
    worker: str | None,
    last_heartbeat_at: str,
) -> int:
    """Claim/refresh an open intent for ``worker``. Returns rows affected: 1 on
    success, 0 if already concluded or held by another worker (caller decides
    the 409)."""
    cursor = conn.execute(
        """
        UPDATE intents
        SET worker = ?, last_heartbeat_at = ?
        WHERE id = ?
          AND project_id = ?
          AND to_fact_id IS NULL
          AND (worker IS NULL OR worker = ?)
        """,
        (worker, last_heartbeat_at, intent_id, project_id, worker),
    )
    return cursor.rowcount


def release_worker(
    conn: sqlite3.Connection, intent_id: str, project_id: str
) -> None:
    conn.execute(
        "UPDATE intents SET worker = NULL WHERE id = ? AND project_id = ?",
        (intent_id, project_id),
    )


def conclude(
    conn: sqlite3.Connection,
    intent_id: str,
    project_id: str,
    to_fact_id: str,
    worker: str | None,
    concluded_at: str,
) -> int:
    """Conclude an open intent, pointing it at ``to_fact_id``. Returns rows
    affected: 1 on success, 0 if it lost the race (already concluded or claimed
    by another worker)."""
    cursor = conn.execute(
        """
        UPDATE intents
        SET to_fact_id = ?, worker = ?, last_heartbeat_at = ?, concluded_at = ?
        WHERE id = ?
          AND project_id = ?
          AND to_fact_id IS NULL
          AND (worker IS NULL OR worker = ?)
        """,
        (to_fact_id, worker, concluded_at, concluded_at, intent_id, project_id, worker),
    )
    return cursor.rowcount


def abandon(
    conn: sqlite3.Connection, intent_id: str, project_id: str, reason: str, now: str
) -> int:
    """Mark an open intent abandoned (planner decision, batch A). Returns rowcount."""
    cur = conn.execute(
        "UPDATE intents SET abandoned_at = ?, abandon_reason = ? "
        "WHERE id = ? AND project_id = ? AND to_fact_id IS NULL AND abandoned_at IS NULL",
        (now, reason, intent_id, project_id),
    )
    return cur.rowcount


def set_priority(
    conn: sqlite3.Connection, intent_id: str, project_id: str, priority: int
) -> int:
    """Set an open intent's scheduling priority (higher = dispatched first)."""
    cur = conn.execute(
        "UPDATE intents SET priority = ? "
        "WHERE id = ? AND project_id = ? AND to_fact_id IS NULL",
        (priority, intent_id, project_id),
    )
    return cur.rowcount
