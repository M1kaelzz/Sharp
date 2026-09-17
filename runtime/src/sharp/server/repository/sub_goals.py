"""SQL for the ``sub_goals`` table (batch C).

Sub goals are phase-level objectives attached to a project: the planner
(reason) proposes them, marks them done or abandons them, and humans can do the
same from the UI. They make staged progress explicit — the graph keeps a single
terminal goal, while sub goals describe the road there.

Functions take an open connection and return rows / rowcounts; callers own the
transaction and HTTP error handling.
"""

from __future__ import annotations

import sqlite3


def insert(
    conn: sqlite3.Connection,
    sub_goal_id: str,
    project_id: str,
    title: str,
    *,
    created_by: str,
    created_at: str,
) -> None:
    conn.execute(
        "INSERT INTO sub_goals (id, project_id, title, status, note, created_by, created_at) "
        "VALUES (?, ?, ?, 'pending', '', ?, ?)",
        (sub_goal_id, project_id, title, created_by, created_at),
    )


def list_for_project(
    conn: sqlite3.Connection, project_id: str
) -> list[sqlite3.Row]:
    """All sub goals, oldest first (creation order = the intended sequence)."""
    return conn.execute(
        "SELECT * FROM sub_goals WHERE project_id = ? ORDER BY created_at, id",
        (project_id,),
    ).fetchall()


def fetch(
    conn: sqlite3.Connection, sub_goal_id: str, project_id: str
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM sub_goals WHERE id = ? AND project_id = ?",
        (sub_goal_id, project_id),
    ).fetchone()


def update_status(
    conn: sqlite3.Connection,
    sub_goal_id: str,
    project_id: str,
    status: str,
    note: str,
    now: str,
) -> int:
    """Set a sub goal's status. ``concluded_at`` is stamped for terminal states."""
    concluded_at = now if status in ("done", "abandoned") else None
    cur = conn.execute(
        "UPDATE sub_goals SET status = ?, note = ?, concluded_at = ? "
        "WHERE id = ? AND project_id = ?",
        (status, note, concluded_at, sub_goal_id, project_id),
    )
    return cur.rowcount


def counts(conn: sqlite3.Connection, project_id: str) -> dict[str, int]:
    """{status: count} for one project (used by UI progress + acceptance checks)."""
    rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM sub_goals WHERE project_id = ? GROUP BY status",
        (project_id,),
    ).fetchall()
    return {r["status"]: int(r["n"]) for r in rows}
