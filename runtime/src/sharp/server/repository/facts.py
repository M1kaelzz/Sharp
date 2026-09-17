"""SQL for the ``facts`` table.

Functions take an open connection and return rows / None; callers own the
transaction and any HTTP error handling. Row->model conversion stays with the
callers (``Fact(**dict(row))``).
"""

from __future__ import annotations

import sqlite3


def insert(
    conn: sqlite3.Connection, fact_id: str, project_id: str, description: str
) -> None:
    conn.execute(
        "INSERT INTO facts (id, project_id, description) VALUES (?, ?, ?)",
        (fact_id, project_id, description),
    )


def list_for_project(
    conn: sqlite3.Connection, project_id: str
) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM facts WHERE project_id = ?", (project_id,)
    ).fetchall()


def fetch(
    conn: sqlite3.Connection, fact_id: str, project_id: str
) -> sqlite3.Row | None:
    """One fact by id within a project, or None."""
    return conn.execute(
        "SELECT description FROM facts WHERE id = ? AND project_id = ?",
        (fact_id, project_id),
    ).fetchone()


def list_by_id_in_other_projects(
    conn: sqlite3.Connection, fact_id: str, project_id: str
) -> list[sqlite3.Row]:
    """Same fact id in every project except ``project_id`` (shared-file check)."""
    return conn.execute(
        "SELECT project_id, description FROM facts WHERE id = ? AND project_id != ?",
        (fact_id, project_id),
    ).fetchall()
