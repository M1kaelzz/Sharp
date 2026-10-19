"""Human-approval endpoints for high-risk intents.

Security invariant: every endpoint here is **JWT-only**. The shared
server_token (used by the dispatcher / AI workers) is explicitly rejected, so
the AI can never self-approve, self-reject, or open emergency mode. These are
human actions — the dispatcher process must not, and cannot, call them.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from sharp.server.auth import extract_token, get_jwt_key, server_token, verify_token
from sharp.server.db import get_conn
from sharp.server.events import publish
from sharp.server.repository import intents as intents_repo
from sharp.server.models import (
    ApprovalDecisionRequest,
    EmergencyModeRequest,
    EmergencyRelease,
    EmergencyReviewRequest,
)
from sharp.server.repository import hints as hints_repo
from sharp.server.services import (
    get_project_or_404,
    intent_to_model,
    next_hint_id,
    utcnow,
)

router = APIRouter(prefix="/approvals", tags=["approvals"])
# Emergency-mode lives under /projects (matches the scheme doc), is JWT-only.
emergency_router = APIRouter(tags=["approvals"])


def require_human_jwt(request: Request) -> None:
    """Dependency: reject dispatcher server_token; require a valid human JWT."""
    token = extract_token(request)
    if not token:
        raise HTTPException(401, "未授权，请先登录")
    srv = server_token()
    if srv and token == srv:
        raise HTTPException(403, "审批操作仅限登录用户，AI 不能自行审批")
    if not verify_token(token, get_jwt_key()):
        raise HTTPException(401, "未授权，请先登录")


def get_project_or_404_conn(project_id: str):
    with get_conn() as conn:
        return get_project_or_404(conn, project_id)


# ── Emergency mode (JWT-only, project-scoped) ─────────────────────────────

@emergency_router.get("/projects/emergency-status", dependencies=[Depends(require_human_jwt)])
def emergency_status():
    """List all projects currently in emergency mode."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, title, emergency_until, emergency_reason FROM projects "
            "WHERE emergency_until IS NOT NULL ORDER BY emergency_until"
        ).fetchall()
        return [dict(r) for r in rows]


@emergency_router.get(
    "/emergency-releases",
    response_model=list[EmergencyRelease],
    dependencies=[Depends(require_human_jwt)],
)
def list_all_emergency_releases_route(unreviewed_only: bool = False):
    """跨项目放行清单（见下方 list_all_emergency_releases 的说明）。"""
    return list_all_emergency_releases(unreviewed_only)


@emergency_router.get(
    "/projects/{project_id}/emergency-releases",
    response_model=list[EmergencyRelease],
    dependencies=[Depends(require_human_jwt)],
)
def list_all_emergency_releases(unreviewed_only: bool = False):
    """跨项目的急模式放行清单（审批中心用）。

    审批中心本身是跨项目视图，而放行清单天然按项目组织——这里把两者接起来：
    列出所有项目的自动放行动作（含项目名），供集中复核。
    """
    with get_conn() as conn:
        project_ids = [
            r["project_id"] for r in conn.execute(
                "SELECT DISTINCT project_id FROM approval_events WHERE action = 'emergency_release' "
                "UNION SELECT DISTINCT project_id FROM intents "
                "WHERE approval_note LIKE '%紧急模式自动放行%'"
            ).fetchall()
        ]
        titles = {
            r["id"]: r["title"]
            for r in conn.execute("SELECT id, title FROM projects").fetchall()
        }
    out: list[EmergencyRelease] = []
    for pid in project_ids:
        for release in list_emergency_releases(pid, unreviewed_only):
            release.project_id = pid
            release.project_title = titles.get(pid, "")
            out.append(release)
    out.sort(key=lambda r: r.released_at)
    return out


def list_emergency_releases(project_id: str, unreviewed_only: bool = False):
    """急模式自动放行清单（P1-5）。

    急模式不产生 pending，所以人工的介入点不是"审批"而是"**事后复核**"。
    本端点列出被自动放行的高危行动及其复核状态。

    数据源以审计事件 `emergency_release` 为准，并**兼容早期数据**（此前只有 intent 上
    的一条 `approval_note` 标注、没有事件记录）。
    """
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        rows = conn.execute(
            """
            SELECT ae.intent_id, ae.note AS release_note, ae.created_at AS released_at,
                   i.description, i.risk_level, i.to_fact_id, i.worker,
                   i.concluded_at, i.abandoned_at
            FROM approval_events ae
            LEFT JOIN intents i ON i.id = ae.intent_id AND i.project_id = ae.project_id
            WHERE ae.project_id = ? AND ae.action = 'emergency_release'
            """,
            (project_id,),
        ).fetchall()
        seen = {r["intent_id"] for r in rows}
        legacy = conn.execute(
            "SELECT id AS intent_id, description, risk_level, to_fact_id, worker, "
            "concluded_at, abandoned_at, created_at AS released_at FROM intents "
            "WHERE project_id = ? AND approval_note LIKE '%紧急模式自动放行%'",
            (project_id,),
        ).fetchall()
        reviews = {
            r["intent_id"]: (r["note"], r["created_at"])
            for r in conn.execute(
                "SELECT intent_id, note, created_at FROM approval_events "
                "WHERE project_id = ? AND action = 'emergency_review'",
                (project_id,),
            ).fetchall()
        }

        def _status(row) -> str:
            if row["abandoned_at"]:
                return "已放弃"
            if row["to_fact_id"]:
                return "已结论"
            if row["worker"]:
                return "执行中"
            return "未开始"

        def _build(row, release_note: str) -> EmergencyRelease:
            verdict, reviewed_at = reviews.get(row["intent_id"], ("", None))
            return EmergencyRelease(
                intent_id=row["intent_id"],
                description=(row["description"] or "")[:400],
                risk_level=row["risk_level"] or "low",
                released_at=row["released_at"] or "",
                release_note=release_note,
                status=_status(row),
                has_evidence=bool(row["to_fact_id"]),
                reviewed=bool(reviewed_at),
                verdict=(verdict or "").split(":")[0],
                review_note=verdict or "",
                reviewed_at=reviewed_at,
            )

        out = [_build(r, r["release_note"] or "") for r in rows]
        out += [
            _build(r, "（历史数据：仅有放行标注，无审计事件）")
            for r in legacy if r["intent_id"] not in seen
        ]
        out.sort(key=lambda r: r.released_at)
        return [r for r in out if not r.reviewed] if unreviewed_only else out


@emergency_router.post(
    "/projects/{project_id}/intents/{intent_id}/emergency-review",
    response_model=EmergencyRelease,
    dependencies=[Depends(require_human_jwt)],
)
def review_emergency_release(project_id: str, intent_id: str, body: EmergencyReviewRequest):
    """对一条自动放行的高危动作做事后复核（P1-5）。

    复核不是审批：动作已经执行过了，这里记录"人工看过之后认定无碍 / 需要跟进"。
    """
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        row = conn.execute(
            "SELECT 1 FROM intents WHERE id = ? AND project_id = ?", (intent_id, project_id)
        ).fetchone()
        if row is None:
            raise HTTPException(404, "Intent not found")
        conn.execute(
            "INSERT INTO approval_events (intent_id, project_id, action, note, created_at) "
            "VALUES (?, ?, 'emergency_review', ?, ?)",
            (intent_id, project_id, f"{body.verdict}:{body.note or ''}", utcnow()),
        )
    releases = list_emergency_releases(project_id)
    found = next((r for r in releases if r.intent_id == intent_id), None)
    if found is None:
        raise HTTPException(404, "Release record not found")
    return found


@emergency_router.post("/projects/{project_id}/emergency-mode", dependencies=[Depends(require_human_jwt)])
def set_emergency_mode(project_id: str, body: EmergencyModeRequest):
    """Open or close emergency mode for a project.

    Opening requires a reason + duration hours (default 1h). While open, the
    project's high/critical intents are auto-approved (auditable). Only a logged
    in human (JWT) can do this; the AI cannot.  ``enabled`` defaults to the
    inverse of the current state (toggle) when omitted.
    """
    now = utcnow()
    with get_conn() as conn:
        row = get_project_or_404(conn, project_id)
        currently_open = bool(row["emergency_until"])
        enable = body.enabled if body.enabled is not None else (not currently_open)
        if enable:
            if currently_open:
                raise HTTPException(409, "该项目已处于紧急模式")
            reason = body.reason
            if not reason:
                raise HTTPException(400, "开启紧急模式必须填写原因")
            from datetime import datetime, timedelta, timezone
            hours = max(1.0, body.hours)
            until = (datetime.now(timezone.utc) + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
            conn.execute(
                "UPDATE projects SET emergency_until = ?, emergency_reason = ? WHERE id = ?",
                (until, reason, project_id),
            )
            action, note = "emergency_activated", f"原因：{reason}，预计持续 {hours} 小时，至 {until}"
        else:
            if not currently_open:
                raise HTTPException(409, "项目当前未处于紧急模式")
            conn.execute(
                "UPDATE projects SET emergency_until = NULL, emergency_reason = '' WHERE id = ?",
                (project_id,),
            )
            action, note = "emergency_deactivated", "人工关闭紧急模式"
        # Audit: emergency toggles are project-wide, recorded against a pseudo
        # intent id to keep the approval_events schema (intent_id required).
        conn.execute(
            "INSERT INTO approval_events (intent_id, project_id, action, note, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("__project__", project_id, action, note, now),
        )
        updated = get_project_or_404(conn, project_id)
        result = {
            "project_id": project_id,
            "emergency_until": updated["emergency_until"],
            "emergency_reason": updated["emergency_reason"],
        }
    publish(project_id, "emergency_mode", result)
    return result


def _intent_row_or_404(conn, project_id: str, intent_id: str):
    row = intents_repo.fetch(conn, intent_id, project_id)
    if row is None:
        raise HTTPException(404, "Intent not found")
    return row


@router.get("", dependencies=[Depends(require_human_jwt)])
def list_pending(status: str = "pending"):
    """List intents by approval status (default pending), newest first."""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT i.*, p.title AS project_title
            FROM intents i
            JOIN projects p ON p.id = i.project_id
            WHERE i.approval_status = ?
            ORDER BY i.created_at DESC
            """,
            (status,),
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["project_title"] = row["project_title"]
            result.append(item)
        return result


@router.get("/stats", dependencies=[Depends(require_human_jwt)])
def approval_stats():
    """Approval dashboard counters."""
    with get_conn() as conn:
        pending = conn.execute(
            "SELECT COUNT(*) AS c FROM intents WHERE approval_status = 'pending'"
        ).fetchone()["c"]
        today_approved = conn.execute(
            "SELECT COUNT(*) AS c FROM intents WHERE approval_status = 'approved' "
            "AND approval_decided_at >= date('now')"
        ).fetchone()["c"]
        total_approved = conn.execute(
            "SELECT COUNT(*) AS c FROM intents WHERE approval_status = 'approved'"
        ).fetchone()["c"]
        total_rejected = conn.execute(
            "SELECT COUNT(*) AS c FROM intents WHERE approval_status = 'rejected'"
        ).fetchone()["c"]
    return {
        "pending": pending,
        "today_approved": today_approved,
        "total_approved": total_approved,
        "total_rejected": total_rejected,
    }


@router.get("/{intent_id}", dependencies=[Depends(require_human_jwt)])
def approval_detail(intent_id: str, project_id: str):
    """Detail for one pending/decided intent, scoped to a project.

    ``project_id`` is REQUIRED: intent ids are per-project counters (every
    project has its own i001...), so a project-less lookup by intent id alone
    can silently hit another project's same-named intent (cross-project
    approval of the wrong object)."""
    with get_conn() as conn:
        row = _intent_row_or_404(conn, project_id, intent_id)
        events = conn.execute(
            "SELECT id, action, note, created_at FROM approval_events "
            "WHERE intent_id = ? AND project_id = ? ORDER BY id",
            (intent_id, project_id),
        ).fetchall()
        project = get_project_or_404(conn, project_id)
        model = intent_to_model(conn, row, project_id)
        return {
            "intent": model.model_dump(by_alias=True),
            "project_id": project_id,
            "project_title": project["title"],
            "project_status": project["status"],
            "events": [dict(e) for e in events],
        }


@router.post("/{intent_id}/approve", dependencies=[Depends(require_human_jwt)])
def approve_intent(intent_id: str, project_id: str, body: ApprovalDecisionRequest):
    """Approve a pending intent (JWT-only). ``project_id`` is required — see
    approval_detail for why project-less lookups are rejected."""
    note = body.note
    with get_conn() as conn:
        row = _intent_row_or_404(conn, project_id, intent_id)
        if row["approval_status"] != "pending":
            raise HTTPException(409, f"Intent is {row['approval_status']}, not pending")
        now = utcnow()
        conn.execute(
            "UPDATE intents SET approval_status = 'approved', approval_note = ?, "
            "approval_decided_at = ? WHERE id = ? AND project_id = ?",
            (note, now, intent_id, project_id),
        )
        conn.execute(
            "INSERT INTO approval_events (intent_id, project_id, action, note, created_at) "
            "VALUES (?, ?, 'approved', ?, ?)",
            (intent_id, project_id, note or "已批准", now),
        )
        updated = intents_repo.fetch(conn, intent_id, project_id)
        model = intent_to_model(conn, updated, project_id)
    publish(project_id, "intent_approved", {"id": intent_id, "status": "approved"})
    return model


@router.post("/{intent_id}/reject", dependencies=[Depends(require_human_jwt)])
def reject_intent(intent_id: str, project_id: str, body: ApprovalDecisionRequest):
    """Reject a pending intent (JWT-only). ``project_id`` is required — see
    approval_detail for why project-less lookups are rejected."""
    note = body.note
    if not note:
        raise HTTPException(400, "拒绝时必须填写理由")
    with get_conn() as conn:
        row = _intent_row_or_404(conn, project_id, intent_id)
        if row["approval_status"] != "pending":
            raise HTTPException(409, f"Intent is {row['approval_status']}, not pending")
        now = utcnow()
        conn.execute(
            "UPDATE intents SET approval_status = 'rejected', approval_note = ?, "
            "approval_decided_at = ? WHERE id = ? AND project_id = ?",
            (note, now, intent_id, project_id),
        )
        conn.execute(
            "INSERT INTO approval_events (intent_id, project_id, action, note, created_at) "
            "VALUES (?, ?, 'rejected', ?, ?)",
            (intent_id, project_id, note, now),
        )
        # Feed the rejection reason back as a hint so the reason loop doesn't
        # keep proposing the same high-risk intent.
        hint_id = next_hint_id(conn, project_id)
        hint_content = (
            f"[审批拒绝] 行动 {intent_id}（{row['description'][:60]}）被拒绝，"
            f"理由：{note}。请勿重复提出同类操作。"
        )
        hints_repo.insert(
            conn, hint_id, project_id, hint_content, "approval.reject", now
        )
        updated = intents_repo.fetch(conn, intent_id, project_id)
        model = intent_to_model(conn, updated, project_id)
    publish(project_id, "intent_rejected", {"id": intent_id, "status": "rejected"})
    return model