"""SQL for the ``hints`` table.

Functions take an open connection and return rows / None; callers own the
transaction and any HTTP error handling. Row->model conversion stays with the
callers (``Hint(**dict(row))``).
"""

from __future__ import annotations

import sqlite3


def insert(
    conn: sqlite3.Connection,
    hint_id: str,
    project_id: str,
    content: str,
    creator: str,
    created_at: str,
) -> None:
    conn.execute(
        "INSERT INTO hints (id, project_id, content, creator, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (hint_id, project_id, content, creator, created_at),
    )


def list_for_project(
    conn: sqlite3.Connection, project_id: str
) -> list[sqlite3.Row]:
    """All hints for a project, oldest first (full row)."""
    return conn.execute(
        "SELECT * FROM hints WHERE project_id = ? ORDER BY created_at",
        (project_id,),
    ).fetchall()


def list_content_for_project(
    conn: sqlite3.Connection, project_id: str
) -> list[sqlite3.Row]:
    """Hints for a project, oldest first, narrow projection for reports/export.

    Excludes ``id`` and ``project_id`` so callers that serialize whole rows
    (e.g. ``dict(row)``) keep their existing output shape.
    """
    return conn.execute(
        "SELECT content, creator, created_at FROM hints WHERE project_id = ? ORDER BY created_at",
        (project_id,),
    ).fetchall()
