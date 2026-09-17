"""Asset-endpoint ledger HTTP surface (batch A1).

Serves the structured "what did we already find on this asset" view:
- coverage hint builder used by create_project (new campaigns start informed)
- GET /assets/endpoints?asset_ref= → the ledger rows for one asset
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, HTTPException, Query

from sharp.server import asset_endpoints as asset_endpoints_repo
from sharp.server import asset_space
from sharp.server.asset_endpoints import endpoint_counts_by_asset
from sharp.server.db import get_conn
from sharp.server.models import AssessEndpointsRequest
from sharp.server.services import utcnow

router = APIRouter(tags=["assets"])

_MAX_COVERAGE_HINT_CHARS = 2200


def build_asset_coverage_hints(
    conn: sqlite3.Connection, asset_ref: str, now: str
) -> list[dict]:
    """Coverage summary + still-unassessed inventory as hint dicts (A1).

    Returns [] when the asset has no ledger rows. Called from create_project so
    a fresh campaign sees prior discovered endpoints with their source project —
    the structured counterpart of knowledge_base's soft hints.
    """
    if not asset_ref:
        return []
    counts = endpoint_counts_by_asset(conn, asset_ref)
    if counts["total"] == 0:
        return []
    rows = conn.execute(
        "SELECT e.method, e.path, e.status, e.source_project_id, p.title AS project_title, "
        "       e.last_seen "
        "FROM asset_endpoints e LEFT JOIN projects p ON p.id = e.source_project_id "
        "WHERE e.asset_ref = ? "
        "ORDER BY (e.status = 'discovered') DESC, e.last_seen DESC",
        (asset_ref,),
    ).fetchall()

    lines = [f"[资产覆盖] 目标 {asset_ref} 此前已登记 {counts['total']} 个接口"
             f"（待验证 {counts['todo']}）："]
    for r in rows:
        method = (r["method"] or "ANY").upper()
        src = r["project_title"] or r["source_project_id"]
        state = "未测" if r["status"] == "discovered" else r["status"]
        lines.append(f"- {method} {r['path']}  [{state}, 来自 {src}]")
    content = "\n".join(lines)
    if len(content) > _MAX_COVERAGE_HINT_CHARS:
        content = content[:_MAX_COVERAGE_HINT_CHARS] + "\n…（其余见资产端点列表）"
    return [{"content": content, "creator": "asset_coverage"}]


@router.get("/assets/endpoints")
def list_asset_endpoints(
    asset_ref: str = Query(min_length=1, description="canonical host / AppID / package"),
    status: str | None = Query(default=None, pattern="^(discovered|verified|dismissed)$"),
):
    """Ledger rows for one asset (newest first), for the asset-centre UI / future
    per-endpoint coverage views."""
    with get_conn() as conn:
        if status:
            rows = conn.execute(
                "SELECT e.*, p.title AS project_title "
                "FROM asset_endpoints e LEFT JOIN projects p ON p.id = e.source_project_id "
                "WHERE e.asset_ref = ? AND e.status = ? ORDER BY e.last_seen DESC",
                (asset_ref, status),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT e.*, p.title AS project_title "
                "FROM asset_endpoints e LEFT JOIN projects p ON p.id = e.source_project_id "
                "WHERE e.asset_ref = ? ORDER BY e.last_seen DESC",
                (asset_ref,),
            ).fetchall()
    return [dict(r) for r in rows]


# ── 项目视角的资产台账（P1-B）：让 worker 在运行中看得见、也写得回 ────────────

_LEDGER_LIMIT = 60


def _render_ledger_block(asset_ref: str, rows: list) -> str:
    """渲染成给 worker 看的文本块：**未验证的排前面**。

    分组不是装饰：`未验证` 是"待办清单"，`已排除` 是"别再浪费一轮"，
    `已验证` 是"有结论了"。三者混在一起看，等于什么都没说。
    """
    pending = [r for r in rows if r["status"] == "discovered"]
    verified = [r for r in rows if r["status"] == "verified"]
    dismissed = [r for r in rows if r["status"] == "dismissed"]

    lines: list[str] = []
    if pending:
        lines.append(f"**已发现但尚未评估（{len(pending)}）** —— 优先挑这些推进，别重复测已完成的：")
        for r in pending[:_LEDGER_LIMIT]:
            src = r["source_project_title"] or r["source_project_id"] or ""
            suffix = f"　[来自 {src}]" if src else ""
            lines.append(f"  - {(r['method'] or 'ANY').upper()} {r['path']}{suffix}")
        if len(pending) > _LEDGER_LIMIT:
            lines.append(f"  - …另有 {len(pending) - _LEDGER_LIMIT} 个")
    for label, group in (("已验证", verified), ("已排除", dismissed)):
        if not group:
            continue
        lines.append(f"**{label}（{len(group)}）** —— 不要重复测；如需复测请说明为什么先前结论不再成立：")
        for r in group[:_LEDGER_LIMIT]:
            note = f"　（{r['note']}）" if r["note"] else ""
            lines.append(f"  - {(r['method'] or 'ANY').upper()} {r['path']}{note}")
    return "\n".join(lines)


@router.get("/projects/{project_id}/asset-endpoints")
def project_asset_endpoints(project_id: str):
    """本项目目标资产的接口台账（dispatcher 运行中拉取，用于 `{asset_ledger}` 注入）。

    与**建项时**注入的资产覆盖提示互补：那个只在开工那一刻给一次，
    这里让"跑到一半新登记/新评估的接口"也能被当前项目看到。
    """
    with get_conn() as conn:
        from sharp.server.services import get_project_or_404

        project = get_project_or_404(conn, project_id)
        asset_ref = (project["asset_ref"] if "asset_ref" in project.keys() else "") or ""
        if not asset_ref:
            return {"asset_ref": "", "counts": {"total": 0, "todo": 0, "verified": 0, "dismissed": 0},
                    "items": [], "block": ""}
        rows = asset_endpoints_repo.list_for_asset(conn, asset_ref)
        counts = {
            "total": len(rows),
            "todo": sum(1 for r in rows if r["status"] == "discovered"),
            "verified": sum(1 for r in rows if r["status"] == "verified"),
            "dismissed": sum(1 for r in rows if r["status"] == "dismissed"),
        }
        return {
            "asset_ref": asset_ref,
            "counts": counts,
            "items": [dict(r) for r in rows],
            "block": _render_ledger_block(asset_ref, rows),
        }


@router.post("/projects/{project_id}/endpoint-assessments")
def assess_project_endpoints(project_id: str, body: AssessEndpointsRequest):
    """落库 worker 报来的"这个接口测过了/排除了"（P1-B）。

    这是台账状态机的推进端：没有它，`status` 永远停在 `discovered`，
    覆盖报告的盲区清单只增不减。
    """
    with get_conn() as conn:
        from sharp.server.services import get_project_or_404, utcnow

        project = get_project_or_404(conn, project_id)
        asset_ref = (project["asset_ref"] if "asset_ref" in project.keys() else "") or ""
        if not asset_ref:
            raise HTTPException(409, "本项目没有资产键（asset_ref），无法登记接口评估")

        now = utcnow()
        result = {"updated": 0, "created": 0, "skipped": 0}
        for item in body.items:
            outcome = asset_endpoints_repo.assess_endpoint(
                conn,
                asset_ref=asset_ref,
                method=item.method,
                path=item.path,
                status=item.status,
                note=item.note[:2000],
                project_id=project_id,
                fact_id=None,
                now=now,
            )
            result[outcome] = result.get(outcome, 0) + 1
        return {"asset_ref": asset_ref, **result}


@router.get("/asset-spaces/{asset_ref}")
def get_asset_space(asset_ref: str):
    """目标空间详情（P2-A）：该资产下**跨项目**的接口台账 / 历史发现 / 已沉淀知识。

    路径刻意不用 `/assets/{asset_ref}`：那会与既有的 `/assets/endpoints` 抢匹配
    （FastAPI 按注册顺序匹配，`{asset_ref}` 会把 "endpoints" 吃成参数值）。
    独立前缀最省心，也更好读。
    """
    with get_conn() as conn:
        space = asset_space.build_asset_space(conn, asset_ref)
    if space is None:
        raise HTTPException(404, f"资产不存在或没有项目：{asset_ref}")
    return space
