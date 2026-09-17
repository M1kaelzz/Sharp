"""未验证假设 API（P0-3）。

规划器（reason）可从证据派生假设并结算它们；人工可同样增删改——假设被滥用成
"假设刷屏"时，人能直接清理，与阶段（sub_goals）的兜底一致。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from sharp.server.db import get_conn
from sharp.server.models import (
    CreateHypothesisRequest,
    Hypothesis,
    UpdateHypothesisRequest,
)
from sharp.server.repository import hypotheses as hypotheses_repo
from sharp.server.services import (
    get_project_or_404,
    next_hypothesis_id,
    utcnow,
)

router = APIRouter(tags=["hypotheses"])


def _to_model(row) -> Hypothesis:
    raw = row["premise_fact_ids"] or ""
    return Hypothesis(
        id=row["id"],
        project_id=row["project_id"],
        statement=row["statement"],
        status=row["status"],
        premise_fact_ids=[p for p in raw.split(",") if p] if raw else [],
        result_fact_id=row["result_fact_id"],
        created_by=row["created_by"],
        note=row["note"],
        created_at=row["created_at"],
        concluded_at=row["concluded_at"],
    )


@router.get("/projects/{project_id}/hypotheses", response_model=list[Hypothesis])
def list_hypotheses(project_id: str, only_open: bool = False):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        rows = (
            hypotheses_repo.list_open(conn, project_id)
            if only_open
            else hypotheses_repo.list_for_project(conn, project_id)
        )
        return [_to_model(r) for r in rows]


@router.post("/projects/{project_id}/hypotheses", response_model=Hypothesis, status_code=201)
def create_hypothesis(project_id: str, body: CreateHypothesisRequest):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        hid = next_hypothesis_id(conn, project_id)
        hypotheses_repo.insert(
            conn, hid, project_id, body.statement.strip(),
            premise_fact_ids=body.premise_fact_ids,
            created_by=body.created_by or "reason",
            created_at=utcnow(),
        )
        row = hypotheses_repo.fetch(conn, hid, project_id)
        return _to_model(row)


@router.patch("/projects/{project_id}/hypotheses/{hypothesis_id}", response_model=Hypothesis)
def update_hypothesis(project_id: str, hypothesis_id: str, body: UpdateHypothesisRequest):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        row = hypotheses_repo.fetch(conn, hypothesis_id, project_id)
        if row is None:
            raise HTTPException(404, "Hypothesis not found")

        status = body.status or row["status"]
        note = row["note"] if body.note is None else body.note
        result_fact_id = (
            body.result_fact_id if body.result_fact_id is not None else row["result_fact_id"]
        )
        # 结算必须有依据：成立要指向证据；不成立要写清试过什么
        if status == "confirmed" and not result_fact_id:
            raise HTTPException(422, "标记 confirmed 必须提供 result_fact_id（引用支撑证据）")
        if status == "refuted" and not (note or "").strip():
            raise HTTPException(422, "标记 refuted 必须在 note 里写清尝试过什么")

        hypotheses_repo.update_status(
            conn, hypothesis_id, project_id, status, note, result_fact_id, utcnow()
        )
        return _to_model(hypotheses_repo.fetch(conn, hypothesis_id, project_id))


@router.delete("/projects/{project_id}/hypotheses/{hypothesis_id}", status_code=204)
def delete_hypothesis(project_id: str, hypothesis_id: str):
    """人工清理（防止假设刷屏）。"""
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        conn.execute(
            "DELETE FROM hypotheses WHERE id = ? AND project_id = ?",
            (hypothesis_id, project_id),
        )
