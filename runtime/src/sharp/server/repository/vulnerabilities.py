"""SQL for the ``vulnerabilities`` table.

Same pattern as the other repository modules: functions take an open
connection, return rows/None, and the caller owns the transaction.
"""

from __future__ import annotations

import sqlite3

_COLUMNS = (
    "id, project_id, fact_id, intent_id, title, severity, status, "
    "url, description, evidence, reproduction, impact, recommendation, "
    "created_at, verified_at, kind, score"
)


def insert(
    conn: sqlite3.Connection,
    vuln_id: str,
    project_id: str,
    *,
    fact_id: str,
    intent_id: str | None = None,
    title: str,
    severity: str = "info",
    status: str = "pending",
    url: str = "",
    description: str = "",
    evidence: str = "",
    reproduction: str = "",
    impact: str = "",
    recommendation: str = "",
    created_at: str,
    verified_at: str | None = None,
    kind: str = "vuln",
    score: int = 0,
) -> None:
    conn.execute(
        f"INSERT INTO vulnerabilities ({_COLUMNS}) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            vuln_id, project_id, fact_id, intent_id,
            title, severity, status, url,
            description, evidence, reproduction, impact, recommendation,
            created_at, verified_at, kind, score,
        ),
    )


def fetch(
    conn: sqlite3.Connection, vuln_id: str, project_id: str
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM vulnerabilities WHERE id = ? AND project_id = ?",
        (vuln_id, project_id),
    ).fetchone()


def list_for_project(
    conn: sqlite3.Connection, project_id: str
) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM vulnerabilities WHERE project_id = ? ORDER BY created_at DESC",
        (project_id,),
    ).fetchall()


def list_all(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM vulnerabilities ORDER BY created_at DESC",
    ).fetchall()


def delete(conn: sqlite3.Connection, vuln_id: str, project_id: str) -> None:
    conn.execute(
        "DELETE FROM vulnerabilities WHERE id = ? AND project_id = ?",
        (vuln_id, project_id),
    )


def update_status(
    conn: sqlite3.Connection,
    vuln_id: str,
    project_id: str,
    status: str,
    verified_at: str | None = None,
) -> int:
    cursor = conn.execute(
        "UPDATE vulnerabilities SET status = ?, verified_at = ? WHERE id = ? AND project_id = ?",
        (status, verified_at, vuln_id, project_id),
    )
    return cursor.rowcount


def count_for_project(
    conn: sqlite3.Connection, project_id: str
) -> dict[str, int]:
    """Return severity → count for a project, including a 'total' key."""
    rows = conn.execute(
        "SELECT severity, COUNT(*) AS n FROM vulnerabilities "
        "WHERE project_id = ? AND status != 'dismissed' GROUP BY severity",
        (project_id,),
    ).fetchall()
    result = {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0, "total": 0}
    for r in rows:
        sev = r["severity"]
        if sev in result:
            result[sev] = r["n"]
        result["total"] += r["n"]
    return result


def count_all(conn: sqlite3.Connection) -> dict[str, int]:
    """Return severity → count across all projects (excludes dismissed),
    plus a 'confirmed' count for confirmed vulnerabilities."""
    rows = conn.execute(
        "SELECT severity, status, COUNT(*) AS n FROM vulnerabilities "
        "WHERE status != 'dismissed' GROUP BY severity, status"
    ).fetchall()
    result = {
        "critical": 0, "high": 0, "medium": 0,
        "low": 0, "info": 0, "confirmed": 0, "total": 0,
    }
    for r in rows:
        sev = r["severity"]
        if sev in result:
            result[sev] += r["n"]
        if r["status"] == "confirmed":
            result["confirmed"] += r["n"]
        result["total"] += r["n"]
    return result


def daily_counts(conn: sqlite3.Connection, days: int = 14) -> list[dict]:
    """Return [{date, count}] for the last *days* days (excludes dismissed).

    Dates are ISO 'YYYY-MM-DD'. Uses date(created_at) for bucketing.
    """
    rows = conn.execute(
        "SELECT date(created_at) AS d, COUNT(*) AS n "
        "FROM vulnerabilities WHERE status != 'dismissed' "
        "AND date(created_at) >= date('now', ?) "
        "GROUP BY date(created_at) ORDER BY d",
        (f"-{days} days",),
    ).fetchall()
    return [{"date": r["d"], "count": r["n"]} for r in rows]
