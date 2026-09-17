import sqlite3

from fastapi import APIRouter, HTTPException

from sharp.server.db import get_conn
from sharp.server.events import publish
from sharp.server.repository import intents as intents_repo
from sharp.server.models import (
    AbandonIntentRequest,
    UpdateIntentPriorityRequest,
    ConcludeRequest,
    ConcludeResponse,
    CreateIntentRequest,
    Fact,
    HeartbeatRequest,
    Intent,
)
from sharp.server.services import (
    check_project_active,
    expire_workers,
    get_intent_or_404,
    get_project_or_404,
    get_releasable_open_intent_or_404,
    intent_to_model,
    next_fact_id,
    next_intent_id,
    utcnow,
    validate_facts_exist,
    validate_intent_creator_worker,
    validate_goal_not_in_sources,
)
from sharp.server.reports import is_engineered_report_intent_description
from sharp.server import risk as risk_mod

router = APIRouter(tags=["intents"])


@router.post(
    "/projects/{project_id}/intents",
    response_model=Intent,
    status_code=201,
)
def create_intent(project_id: str, body: CreateIntentRequest):
    with get_conn() as conn:
        check_project_active(conn, project_id)
        validate_facts_exist(conn, project_id, body.from_)
        validate_goal_not_in_sources(body.from_)
        validate_intent_creator_worker(body.creator, body.worker)

        # Idempotency fast path: a retry with the same key returns the existing
        # intent instead of creating a duplicate.
        if body.idempotency_key is not None:
            existing = _find_intent_by_idempotency_key(conn, project_id, body.idempotency_key)
            if existing is not None:
                return intent_to_model(conn, existing, project_id)

        # --- Risk classification & approval gate (server authority) -------
        kw_level, kw_reason = risk_mod.classify(body.description)
        final_level = risk_mod.combine(body.risk_level, kw_level)
        # Enrolled for human approval unless the project is in emergency mode
        # (JWT-opened escape hatch) — in which case it is auto-approved with an
        # auditable note.
        project = get_project_or_404(conn, project_id)
        in_emergency = _project_in_emergency(conn, project)
        emergency_released = bool(in_emergency and risk_mod.needs_approval(final_level))
        if risk_mod.needs_approval(final_level) and not in_emergency:
            approval_status = "pending"
            approval_note = ""
            approval_decided_at = None
        else:
            approval_status = "none"
            approval_note = "紧急模式自动放行" if emergency_released else ""
            approval_decided_at = None
        risk_reason = body.risk_reason if body.risk_reason else kw_reason
        # ----------------------------------------------------------------

        now = utcnow()
        iid = next_intent_id(conn, project_id)
        claimed = body.worker is not None
        try:
            intents_repo.insert(
                conn,
                iid,
                project_id,
                to_fact_id=None,
                description=body.description,
                creator=body.creator,
                worker=body.worker,
                last_heartbeat_at=now if claimed else None,
                created_at=now,
                concluded_at=None,
                idempotency_key=body.idempotency_key,
                risk_level=final_level,
                risk_reason=risk_reason,
                approval_status=approval_status,
                approval_note=approval_note,
                approval_decided_at=approval_decided_at,
            )
        except sqlite3.IntegrityError:
            # Concurrent request with the same idempotency_key won the unique
            # index. Roll back our snapshot (and the wasted id) so the requery
            # sees the committed winner, then return it.
            if body.idempotency_key is None:
                raise
            conn.rollback()
            winner = _find_intent_by_idempotency_key(conn, project_id, body.idempotency_key)
            if winner is None:
                raise
            return intent_to_model(conn, winner, project_id)
        for fid in body.from_:
            intents_repo.add_source(conn, iid, project_id, fid)

        if approval_status == "pending":
            conn.execute(
                "INSERT INTO approval_events (intent_id, project_id, action, note, created_at) VALUES (?, ?, 'submitted', ?, ?)",
                (iid, project_id, f"风险等级：{final_level}，等待人工审批", now),
            )
        elif emergency_released:
            # 紧急模式自动放行也要逐条留痕——ADR-0011 的承诺是"旁路的是人工确认，
            # 不是记录"。此前只有 intent 上的一条 note，approval_events 里查不到，
            # 事后复核只能靠筛字段。
            until = project["emergency_until"] or ""
            reason = project["emergency_reason"] or ""
            conn.execute(
                "INSERT INTO approval_events (intent_id, project_id, action, note, created_at) VALUES (?, ?, 'emergency_release', ?, ?)",
                (iid, project_id,
                 f"紧急模式自动放行：风险等级 {final_level}；窗口至 {until}；原因：{reason}",
                 now),
            )

        result = Intent(
            id=iid,
            **{"from": body.from_},
            to=None,
            description=body.description,
            creator=body.creator,
            worker=body.worker,
            last_heartbeat_at=now if claimed else None,
            created_at=now,
            concluded_at=None,
            risk_level=final_level,
            risk_reason=risk_reason,
            approval_status=approval_status,
            approval_note=approval_note,
            approval_decided_at=approval_decided_at,
        )
    # Bug fix: publish AFTER with-block so DB is committed before SSE fires
    publish(project_id, "intent_created", {"id": iid, "worker": body.worker})
    if approval_status == "pending":
        publish(project_id, "approval_pending", {"id": iid, "risk_level": final_level})
    return result


def _project_in_emergency(conn, project_row) -> bool:
    """True while the project's emergency window is still open (lazily cleared
    upstream by expire_project_emergency on read paths; here a defensive check
    also treats an expired emergency_until as closed)."""
    from sharp.server.services import utcnow as _now
    until = project_row["emergency_until"]
    if not until:
        return False
    return until >= _now()


def _find_intent_by_idempotency_key(conn, project_id: str, key: str):
    return intents_repo.find_by_idempotency_key(conn, project_id, key)


@router.post(
    "/projects/{project_id}/intents/{intent_id}/heartbeat",
    response_model=Intent,
)
def heartbeat(project_id: str, intent_id: str, body: HeartbeatRequest):
    with get_conn() as conn:
        expire_workers(conn, project_id)
        row = get_intent_or_404(conn, project_id, intent_id)
        _check_intent_project_claimable(conn, project_id, row)
        _check_intent_not_pending(row)

        now = utcnow()
        if intents_repo.claim(conn, intent_id, project_id, body.worker, now) != 1:
            row = get_intent_or_404(conn, project_id, intent_id)
            if row["to_fact_id"] is not None:
                raise HTTPException(409, "Intent already concluded")
            if row["worker"] is not None and row["worker"] != body.worker:
                raise HTTPException(409, f"Intent is currently claimed by {row['worker']}")
            raise HTTPException(409, "Intent claim failed")

        updated = intents_repo.fetch(conn, intent_id, project_id)
        result = intent_to_model(conn, updated, project_id)
    publish(project_id, "intent_heartbeat", {"id": intent_id, "worker": body.worker})
    return result


@router.post(
    "/projects/{project_id}/intents/{intent_id}/release",
    response_model=Intent,
)
def release(project_id: str, intent_id: str, body: HeartbeatRequest):
    with get_conn() as conn:
        row = get_intent_or_404(conn, project_id, intent_id)
        _check_intent_project_claimable(conn, project_id, row)
        row = get_releasable_open_intent_or_404(conn, project_id, intent_id, body.worker)

        if row["worker"] == body.worker:
            intents_repo.release_worker(conn, intent_id, project_id)
            row = intents_repo.fetch(conn, intent_id, project_id)

        return intent_to_model(conn, row, project_id)


@router.post(
    "/projects/{project_id}/intents/{intent_id}/abandon",
    response_model=Intent,
)
def abandon_intent(project_id: str, intent_id: str, body: AbandonIntentRequest):
    """Abandon an open intent (batch A).

    The planning pass (reason) — or a human — may retire a step that is no
    longer worth a worker slot (dead end, superseded, low value under a
    deadline). Abandoned intents stay on the graph for traceability but are no
    longer dispatchable and do not count as unclaimed work.
    """
    with get_conn() as conn:
        row = get_intent_or_404(conn, project_id, intent_id)
        _check_intent_project_claimable(conn, project_id, row)
        if row["to_fact_id"] is not None:
            raise HTTPException(409, "已结论的行动不能放弃")
        if row["abandoned_at"] is not None:
            raise HTTPException(409, "该行动已被放弃")
        if row["worker"] is not None:
            raise HTTPException(409, f"行动正被 {row['worker']} 执行，不能放弃（先释放或等其结论）")
        now = utcnow()
        if intents_repo.abandon(conn, intent_id, project_id, body.reason or "", now) != 1:
            raise HTTPException(409, "放弃失败（状态已变化）")
        updated = intents_repo.fetch(conn, intent_id, project_id)
        result = intent_to_model(conn, updated, project_id)
    publish(project_id, "intent_abandoned", {"id": intent_id, "reason": body.reason or ""})
    return result


@router.post(
    "/projects/{project_id}/intents/{intent_id}/priority",
    response_model=Intent,
)
def set_intent_priority(project_id: str, intent_id: str, body: UpdateIntentPriorityRequest):
    """Raise/lower an open intent's scheduling priority (batch A).

    Higher priority is dispatched first; equal priority keeps FIFO by creation
    time. This is how the planner steers scarce worker slots under a deadline.
    """
    with get_conn() as conn:
        row = get_intent_or_404(conn, project_id, intent_id)
        _check_intent_project_claimable(conn, project_id, row)
        if row["to_fact_id"] is not None:
            raise HTTPException(409, "已结论的行动不能调整优先级")
        if intents_repo.set_priority(conn, intent_id, project_id, body.priority) != 1:
            raise HTTPException(409, "优先级调整失败（状态已变化）")
        updated = intents_repo.fetch(conn, intent_id, project_id)
        return intent_to_model(conn, updated, project_id)


@router.post(
    "/projects/{project_id}/intents/{intent_id}/conclude",
    response_model=ConcludeResponse,
)
def conclude(project_id: str, intent_id: str, body: ConcludeRequest):
    with get_conn() as conn:
        expire_workers(conn, project_id)
        row = get_intent_or_404(conn, project_id, intent_id)
        _check_intent_project_claimable(conn, project_id, row)
        _check_intent_not_pending(row)
        if row["to_fact_id"] is not None:
            raise HTTPException(409, "Intent already concluded")
        if row["worker"] is not None and row["worker"] != body.worker:
            raise HTTPException(409, f"Intent is currently claimed by {row['worker']}")

        now = utcnow()
        fid = next_fact_id(conn, project_id)

        conn.execute(
            "INSERT INTO facts (id, project_id, description) VALUES (?, ?, ?)",
            (fid, project_id, body.description),
        )
        if intents_repo.conclude(conn, intent_id, project_id, fid, body.worker, now) != 1:
            raise HTTPException(409, "Intent conclude lost race")

        # 写 fact 之后的统一副作用（接口台账 + 跨项目知识，同一事务）。
        # 这条链路此前只做了"登记接口"，知识提取靠 dispatcher 另行触发 ——
        # 于是从 chat / 分析器写进来的证据什么都不产出（见 server/fact_hooks.py）。
        from sharp.server.fact_hooks import after_fact_write

        after_fact_write(
            conn,
            project_id=project_id,
            fact_id=fid,
            description=body.description,
            now=now,
        )

        updated = intents_repo.fetch(conn, intent_id, project_id)
        result = ConcludeResponse(
            fact=Fact(id=fid, description=body.description),
            intent=intent_to_model(conn, updated, project_id),
        )
    publish(project_id, "fact_created", {
        "id": fid,
        "intent_id": intent_id,
        "worker": body.worker,
        "preview": body.description[:120],
    })
    return result


def _check_intent_not_pending(intent_row) -> None:
    """Server-side enforcement of the approval gate on the execution path.

    ``create_intent`` marks high/critical intents ``pending`` for human
    approval.  Dispatcher normally never dispatches pending intents, but the
    gate previously lived only in the dispatcher's client-side filter — a
    caller holding the shared server token (i.e. the AI/dispatcher) could
    heartbeat-claim and conclude a pending high-risk intent directly.  Reject
    pending intents here so the gate is enforced by the server, not by client
    cooperation.  (Emergency-mode intents never reach 'pending' — create
    auto-approves them — so no emergency carve-out is needed on this path.)
    """
    if intent_row["approval_status"] == "pending":
        raise HTTPException(
            403,
            "行动处于人工审批中（pending），不可认领或执行。请先在审批中心批准后再继续。",
        )


def _check_intent_project_claimable(
    conn,
    project_id: str,
    intent_row,
) -> None:
    project = get_project_or_404(conn, project_id)
    if project["status"] == "active":
        return
    if project["status"] == "completed" and is_engineered_report_intent_description(intent_row["description"]):
        return
    raise HTTPException(403, f"Project is {project['status']}")
