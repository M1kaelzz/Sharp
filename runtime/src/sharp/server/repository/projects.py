"""SQL for the ``projects`` table.

Functions take an open connection and return rows / cursors; callers own the
transaction and any HTTP error handling. Row->model conversion stays in
``services`` (``project_meta_from_row`` / ``project_reason_from_row``).
"""

from __future__ import annotations

import sqlite3


def fetch(conn: sqlite3.Connection, project_id: str) -> sqlite3.Row | None:
    """Raw ``SELECT *`` for one project, or None. No 404 (that's services')."""
    return conn.execute(
        "SELECT * FROM projects WHERE id = ?", (project_id,)
    ).fetchone()


def list_summaries(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """All projects with the aggregate counts used by the project list view."""
    return conn.execute(
        """
        SELECT p.*,
            (SELECT COUNT(*) FROM facts WHERE project_id = p.id) AS fact_count,
            (SELECT COUNT(*) FROM intents WHERE project_id = p.id) AS intent_count,
            (SELECT COUNT(*) FROM intents WHERE project_id = p.id AND concluded_at IS NULL AND worker IS NOT NULL) AS working_intent_count,
            (SELECT COUNT(*) FROM intents
             WHERE project_id = p.id AND concluded_at IS NULL AND worker IS NULL
               AND approval_status != 'pending'
               AND abandoned_at IS NULL) AS unclaimed_intent_count,
            (SELECT COUNT(*) FROM intents
             WHERE project_id = p.id AND approval_status = 'pending') AS pending_approval_count,
            (SELECT COUNT(*) FROM hints WHERE project_id = p.id) AS hint_count,
            (SELECT 1 FROM intents
             WHERE project_id = p.id AND to_fact_id IS NULL
               AND description LIKE '生成工程化测试报告%'
             LIMIT 1) AS has_generating_report,
            (SELECT 1 FROM facts f
             JOIN intents i ON i.project_id = f.project_id AND i.to_fact_id = f.id
             WHERE f.project_id = p.id
               AND i.description LIKE '生成工程化测试报告%'
               AND ltrim(f.description) LIKE '# 工程化测试报告%'
             LIMIT 1) AS has_done_report,
            (SELECT COUNT(*) FROM vulnerabilities
             WHERE project_id = p.id AND status != 'dismissed') AS vuln_count,
            (SELECT COUNT(*) FROM vulnerabilities
             WHERE project_id = p.id AND severity IN ('critical', 'high')
               AND status != 'dismissed') AS vuln_high_count
        FROM projects p
        ORDER BY p.created_at
        """
    ).fetchall()


def increment_task_count(conn: sqlite3.Connection, project_id: str) -> None:
    conn.execute(
        "UPDATE projects SET task_count = task_count + 1 WHERE id = ?",
        (project_id,),
    )


def insert(
    conn: sqlite3.Connection,
    project_id: str,
    title: str,
    now: str,
    *,
    target_kind: str = "web",
    asset_ref: str = "",
    task_mode: str = "pentest",
) -> None:
    conn.execute(
        "INSERT INTO projects (id, title, status, created_at, target_kind, asset_ref, task_mode) "
        "VALUES (?, ?, 'active', ?, ?, ?, ?)",
        (project_id, title, now, target_kind, asset_ref, task_mode),
    )


def delete(conn: sqlite3.Connection, project_id: str) -> None:
    # approval_events has no FK/cascade (audit rows outlive intents); purge its
    # orphans in the same tx or they accumulate forever (batch DB-health).
    conn.execute(
        "DELETE FROM approval_events WHERE project_id = ?", (project_id,)
    )
    conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))


def update_task_mode(conn: sqlite3.Connection, project_id: str, task_mode: str) -> None:
    """Switch a project between 'pentest' and 'scored' (opt-in scoring)."""
    conn.execute(
        "UPDATE projects SET task_mode = ? WHERE id = ?", (task_mode, project_id)
    )


def update_title(conn: sqlite3.Connection, project_id: str, title: str) -> None:
    conn.execute(
        "UPDATE projects SET title = ? WHERE id = ?", (title, project_id)
    )


def update_status(conn: sqlite3.Connection, project_id: str, status: str) -> None:
    conn.execute(
        "UPDATE projects SET status = ? WHERE id = ?", (status, project_id)
    )


def set_status_active(conn: sqlite3.Connection, project_id: str) -> None:
    conn.execute(
        "UPDATE projects SET status = 'active' WHERE id = ?", (project_id,)
    )


def claim_reason(
    conn: sqlite3.Connection,
    project_id: str,
    worker: str,
    trigger: str,
    now: str,
) -> int:
    """Optimistic claim: only succeeds while reason_worker IS NULL.

    Returns the affected row count so the caller can detect a lost race.
    """
    cursor = conn.execute(
        """
        UPDATE projects
        SET reason_worker = ?,
            reason_trigger = ?,
            reason_started_at = ?,
            reason_last_heartbeat_at = ?
        WHERE id = ?
          AND reason_worker IS NULL
        """,
        (worker, trigger, now, now, project_id),
    )
    return cursor.rowcount


def touch_reason_heartbeat(
    conn: sqlite3.Connection, project_id: str, now: str
) -> None:
    conn.execute(
        "UPDATE projects SET reason_last_heartbeat_at = ? WHERE id = ?",
        (now, project_id),
    )


def complete_and_reset_reason(conn: sqlite3.Connection, project_id: str) -> int:
    """Optimistic completion: only succeeds while status='active'.

    Clears the reason lease in the same statement. Returns affected row count.
    """
    cursor = conn.execute(
        """
        UPDATE projects
        SET status = 'completed',
            reason_worker = NULL,
            reason_trigger = NULL,
            reason_started_at = NULL,
            reason_last_heartbeat_at = NULL
        WHERE id = ?
          AND status = 'active'
        """,
        (project_id,),
    )
    return cursor.rowcount
