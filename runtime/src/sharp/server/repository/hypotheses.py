"""SQL for the ``hypotheses`` table (P0-3).

假设 = **可证伪的命题**（"若 X 则 Y"），与结论刻意分开：

- `facts` 是已确认结论；假设是**待验证的猜想**，验证后才有 fact；
- 被**否定**的假设同样要留下痕迹（`status='refuted'` + `note` 写清试过什么），
  因为"这条路试过不通"对后续同类目标是有价值的积累。

函数接收已打开的连接并返回行/rowcount，调用方负责事务与 HTTP 错误处理。
"""

from __future__ import annotations

import sqlite3

TERMINAL_STATUSES = ("confirmed", "refuted")


def insert(
    conn: sqlite3.Connection,
    hypothesis_id: str,
    project_id: str,
    statement: str,
    *,
    premise_fact_ids: list[str],
    created_by: str,
    created_at: str,
) -> None:
    conn.execute(
        "INSERT INTO hypotheses (id, project_id, statement, status, premise_fact_ids, "
        "created_by, note, created_at) VALUES (?, ?, ?, 'open', ?, ?, '', ?)",
        (hypothesis_id, project_id, statement, ",".join(premise_fact_ids), created_by, created_at),
    )


def list_for_project(conn: sqlite3.Connection, project_id: str) -> list[sqlite3.Row]:
    """All hypotheses, oldest first."""
    return conn.execute(
        "SELECT * FROM hypotheses WHERE project_id = ? ORDER BY created_at, id",
        (project_id,),
    ).fetchall()


def list_open(conn: sqlite3.Connection, project_id: str) -> list[sqlite3.Row]:
    """未结算的假设（open + testing）——规划器最需要看的就是这些。"""
    return conn.execute(
        "SELECT * FROM hypotheses WHERE project_id = ? AND status IN ('open','testing') "
        "ORDER BY created_at, id",
        (project_id,),
    ).fetchall()


def fetch(conn: sqlite3.Connection, hypothesis_id: str, project_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM hypotheses WHERE id = ? AND project_id = ?",
        (hypothesis_id, project_id),
    ).fetchone()


def update_status(
    conn: sqlite3.Connection,
    hypothesis_id: str,
    project_id: str,
    status: str,
    note: str,
    result_fact_id: str | None,
    now: str,
) -> int:
    """Set status; terminal states get ``concluded_at`` stamped."""
    concluded_at = now if status in TERMINAL_STATUSES else None
    cur = conn.execute(
        "UPDATE hypotheses SET status = ?, note = ?, result_fact_id = ?, concluded_at = ? "
        "WHERE id = ? AND project_id = ?",
        (status, note, result_fact_id, concluded_at, hypothesis_id, project_id),
    )
    return cur.rowcount


def counts(conn: sqlite3.Connection, project_id: str) -> dict[str, int]:
    rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM hypotheses WHERE project_id = ? GROUP BY status",
        (project_id,),
    ).fetchall()
    return {r["status"]: int(r["n"]) for r in rows}
