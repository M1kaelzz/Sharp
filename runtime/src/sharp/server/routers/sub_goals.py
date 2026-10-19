"""Sub goal HTTP surface (batch C).

Phase-level objectives the planner (reason) or a human can add, progress
(`active`), complete (`done`) or retire (`abandoned`). They give staged tasks a
visible roadmap and let the planner express "this phase is finished".
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, HTTPException

from sharp.server.db import get_conn
from sharp.server.events import publish
from sharp.server.models import CreateSubGoalRequest, SubGoal, UpdateSubGoalRequest
from sharp.server.repository import sub_goals as sub_goals_repo
from sharp.server.services import get_project_or_404, next_sub_goal_id, utcnow

router = APIRouter(tags=["sub-goals"])


def _to_model(row: sqlite3.Row) -> SubGoal:
    return SubGoal(
        id=row["id"],
        project_id=row["project_id"],
        title=row["title"],
        status=row["status"],
        note=row["note"],
        created_by=row["created_by"],
        created_at=row["created_at"],
        concluded_at=row["concluded_at"],
    )


@router.get("/projects/{project_id}/sub-goals", response_model=list[SubGoal])
def list_sub_goals(project_id: str):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        return [_to_model(r) for r in sub_goals_repo.list_for_project(conn, project_id)]


@router.post("/projects/{project_id}/sub-goals", response_model=SubGoal, status_code=201)
def create_sub_goal(project_id: str, body: CreateSubGoalRequest):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        now = utcnow()
        sgid = next_sub_goal_id(conn, project_id)
        sub_goals_repo.insert(
            conn, sgid, project_id, body.title, created_by=body.created_by, created_at=now
        )
        row = sub_goals_repo.fetch(conn, sgid, project_id)
        assert row is not None
        result = _to_model(row)
    publish(project_id, "sub_goal_created", {"id": result.id, "title": result.title})
    return result


@router.patch("/projects/{project_id}/sub-goals/{sub_goal_id}", response_model=SubGoal)
def update_sub_goal(project_id: str, sub_goal_id: str, body: UpdateSubGoalRequest):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        row = sub_goals_repo.fetch(conn, sub_goal_id, project_id)
        if row is None:
            raise HTTPException(404, f"Sub goal not found: {sub_goal_id}")
        if sub_goals_repo.update_status(
            conn, sub_goal_id, project_id, body.status, body.note, utcnow()
        ) != 1:
            raise HTTPException(409, "Sub goal update lost race")
        updated = sub_goals_repo.fetch(conn, sub_goal_id, project_id)
        assert updated is not None
        result = _to_model(updated)
    publish(project_id, "sub_goal_updated", {"id": result.id, "status": result.status})
    return result
