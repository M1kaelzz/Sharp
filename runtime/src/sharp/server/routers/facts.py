"""Human correction loop for facts (batch 11.1).

Facts are normally produced by concluded intents (the AI). When a human reviews
the graph and finds a fact wrong or dubious, they can:
  - rewrite the description (audited, marks the fact trusted again), or
  - mark it untrusted so downstream reasoning / knowledge extraction does not
    treat it as solid, or
  - clear an earlier untrusted marking (restore).

Every actual change is appended to ``fact_edits`` (before/after, note, time)
and an SSE ``fact_corrected`` event is published so open UIs update the graph.
The ``origin`` fact is the project's defining asset and cannot be rewritten
(rewriting it would desync every intent's ``from`` reference and the knowledge
base host extraction).
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, HTTPException

from sharp.server import db
from sharp.server.events import publish
from sharp.server.models import Fact, FactCorrectRequest, FactEdit
from sharp.server.services import utcnow

router = APIRouter(tags=["facts"])


def _get_fact_row(conn: sqlite3.Connection, project_id: str, fact_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM facts WHERE id = ? AND project_id = ?",
        (fact_id, project_id),
    ).fetchone()
    if row is None:
        raise HTTPException(404, f"Fact not found: {fact_id} in project {project_id}")
    return row


def _to_fact(row: sqlite3.Row) -> Fact:
    return Fact(
        id=row["id"],
        description=row["description"],
        trusted=bool(row["trusted"]),
    )


@router.post(
    "/projects/{project_id}/facts/{fact_id}/correct",
    response_model=Fact,
)
def correct_fact(project_id: str, fact_id: str, body: FactCorrectRequest):
    with db.get_conn() as conn:
        row = _get_fact_row(conn, project_id, fact_id)
        if fact_id == "origin":
            raise HTTPException(
                422, "起点证据（origin）是项目定义，不允许改写（会脱节行动引用与知识库资产）"
            )

        prev_description = row["description"]
        prev_trusted = bool(row["trusted"])

        new_description = prev_description
        new_trusted = prev_trusted
        changed = False

        if body.description is not None and body.description != prev_description:
            # A human rewrite is itself a review: the corrected text is trusted.
            new_description = body.description
            new_trusted = True
            changed = True
        if body.untrusted is True and new_trusted is not False:
            new_trusted = False
            changed = True
        elif body.untrusted is False and new_trusted is not True:
            # Explicit restore (only meaningful when currently untrusted).
            new_trusted = True
            changed = True

        if changed:
            conn.execute(
                "UPDATE facts SET description = ?, trusted = ? WHERE id = ? AND project_id = ?",
                (new_description, int(new_trusted), fact_id, project_id),
            )
            conn.execute(
                "INSERT INTO fact_edits "
                "(project_id, fact_id, prev_description, prev_trusted, new_description, new_trusted, note, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    project_id,
                    fact_id,
                    prev_description,
                    int(prev_trusted),
                    new_description,
                    int(new_trusted),
                    body.note or "",
                    utcnow(),
                ),
            )
            updated = _to_fact(conn.execute(
                "SELECT * FROM facts WHERE id = ? AND project_id = ?",
                (fact_id, project_id),
            ).fetchone())
        else:
            updated = _to_fact(row)

    publish(project_id, "fact_corrected", {
        "id": fact_id,
        "trusted": updated.trusted,
        "preview": (updated.description or "")[:120],
        "changed": changed,
    })
    return updated


@router.get(
    "/projects/{project_id}/facts/{fact_id}/edits",
    response_model=list[FactEdit],
)
def fact_edit_history(project_id: str, fact_id: str, limit: int = 50):
    """Audit trail for one fact, newest first."""
    limit = max(1, min(limit, 200))
    with db.get_conn() as conn:
        _get_fact_row(conn, project_id, fact_id)  # 404 when the fact is gone
        rows = conn.execute(
            "SELECT * FROM fact_edits WHERE project_id = ? AND fact_id = ? "
            "ORDER BY id DESC LIMIT ?",
            (project_id, fact_id, limit),
        ).fetchall()
    return [
        FactEdit(
            id=r["id"],
            fact_id=r["fact_id"],
            prev_description=r["prev_description"],
            prev_trusted=bool(r["prev_trusted"]),
            new_description=r["new_description"],
            new_trusted=bool(r["new_trusted"]),
            note=r["note"],
            annotator=r["annotator"],
            created_at=r["created_at"],
        )
        for r in rows
    ]
