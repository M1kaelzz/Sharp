"""环境基线 API（P0-2）。

worker 在确认环境事实后写入；新会话开始前读取，避免重复探测。
人工也可写入（`source=human`），例如"这条路已确认不可达"。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from sharp.server.db import get_conn
from sharp.server.models import BaselineEntry, UpsertBaselineRequest
from sharp.server.repository import baseline as baseline_repo
from sharp.server.services import utcnow

router = APIRouter(tags=["baseline"])

MAX_ENTRIES = 200
MAX_KEY_LEN = 120
MAX_VALUE_LEN = 4000


def _to_model(row, *, inherited: bool = False) -> BaselineEntry:
    keys = row.keys()
    return BaselineEntry(
        key=row["key"], value=row["value"], note=row["note"],
        source=row["source"], updated_at=row["updated_at"],
        inherited=inherited,
        source_project_id=(row["source_project_id"] if "source_project_id" in keys else None),
        source_project_title=(row["source_project_title"] if "source_project_title" in keys else ""),
    )


@router.get("/projects/{project_id}/baseline", response_model=list[BaselineEntry])
def list_baseline(project_id: str, with_inherited: bool = False):
    """项目环境基线。

    `with_inherited=true` 时追加**同目标其他项目**已确认的前提（带来源标记）——
    这是 dispatcher 拉取时用的模式：同目标开新项目不该重新探测一遍连通性/工具可用性。
    默认不带，保持既有 API 语义不变（UI 与人工看到的仍是"本项目自己的基线"）。
    """
    with get_conn() as conn:
        from sharp.server.services import (
            get_project_or_404,
            project_target_key,
            target_key_map,
        )

        get_project_or_404(conn, project_id)
        own = baseline_repo.list_for_project(conn, project_id)
        entries = [_to_model(r) for r in own]
        if not with_inherited:
            return entries

        target_key = project_target_key(conn, project_id)
        if target_key is None:
            return entries
        siblings = [
            pid for pid, key in target_key_map(conn).items()
            if key == target_key and pid != project_id
        ]
        inherited = baseline_repo.list_inherited(
            conn,
            project_id=project_id,
            sibling_ids=siblings,
            skip_keys={r["key"] for r in own},
        )
        entries.extend(_to_model(r, inherited=True) for r in inherited)
        return entries


@router.put("/projects/{project_id}/baseline", response_model=list[BaselineEntry])
def upsert_baseline(project_id: str, body: UpsertBaselineRequest):
    """批量写入/更新基线（幂等 upsert）。"""
    if len(body.entries) > MAX_ENTRIES:
        raise HTTPException(422, f"基线条目过多（上限 {MAX_ENTRIES}）")
    for entry in body.entries:
        if not entry.key.strip():
            raise HTTPException(422, "基线 key 不能为空")
        if len(entry.key) > MAX_KEY_LEN:
            raise HTTPException(422, f"基线 key 过长（上限 {MAX_KEY_LEN}）")
        if len(entry.value) > MAX_VALUE_LEN:
            raise HTTPException(422, f"基线 value 过长（上限 {MAX_VALUE_LEN}）")

    now = utcnow()
    with get_conn() as conn:
        from sharp.server.services import get_project_or_404

        get_project_or_404(conn, project_id)
        for entry in body.entries:
            baseline_repo.upsert(
                conn, project_id, entry.key.strip(), entry.value,
                entry.note, entry.source or "worker", now,
            )
        return [_to_model(r) for r in baseline_repo.list_for_project(conn, project_id)]


@router.delete("/projects/{project_id}/baseline/{key}", status_code=204)
def delete_baseline(project_id: str, key: str):
    with get_conn() as conn:
        from sharp.server.services import get_project_or_404

        get_project_or_404(conn, project_id)
        baseline_repo.delete(conn, project_id, key)
