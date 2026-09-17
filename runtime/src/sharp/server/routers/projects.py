import time
from datetime import datetime
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from sharp.server.db import get_conn
from sharp.server.events import close_project, publish, subscribe
from sharp.server.repository import facts as facts_repo
from sharp.server.repository import hints as hints_repo
from sharp.server.repository import intents as intents_repo
from sharp.server.repository import projects as projects_repo
from sharp.server.models import (
    UpdateProjectDeadlineRequest,
    UpdateProjectTaskModeRequest,
    AcceptanceCheck,
    AcceptanceItem,
    AssetGroup,
    CompleteRequest,
    CreateProjectRequest,
    Fact,
    Hint,
    HeartbeatRequest,
    Intent,
    ProjectDetail,
    ProjectMeta,
    ProjectSummary,
    ReopenRequest,
    ReopenResponse,
    ReasonClaimRequest,
    UpdateProjectBudgetRequest,
    UpdateProjectPausedRequest,
    UpdateProjectTitleRequest,
    UpdateProjectStatusRequest,
)
from sharp.server.asset_endpoints import endpoint_counts_by_asset
from sharp.server.services import (
    build_intents,
    check_project_completed,
    check_project_active,
    clear_project_reason,
    expire_pending_approvals,
    expire_project_emergency,
    expire_reason_leases,
    expire_workers,
    get_completion_intent_or_409,
    get_project_or_404,
    intent_to_model,
    next_fact_id,
    next_hint_id,
    next_intent_id,
    next_project_id,
    project_meta_from_row,
    project_reason_from_row,
    utcnow,
    validate_facts_exist,
    validate_goal_not_in_sources,
)

router = APIRouter(tags=["projects"])


def _project_summaries(conn) -> list[ProjectSummary]:
    """Rows → ProjectSummary models (shared by /projects and /assets)."""
    rows = projects_repo.list_summaries(conn)
    return [
        ProjectSummary(
            id=row["id"],
            title=row["title"],
            status=row["status"],
            created_at=row["created_at"],
            reason=project_reason_from_row(row),
            paused=bool(row["paused"]) if "paused" in row.keys() else False,
            task_budget=row["task_budget"] if "task_budget" in row.keys() else 0,
            task_count=row["task_count"] if "task_count" in row.keys() else 0,
            target_kind=row["target_kind"] if "target_kind" in row.keys() else "web",
            asset_ref=row["asset_ref"] if "asset_ref" in row.keys() else "",
            deadline_at=row["deadline_at"] if "deadline_at" in row.keys() else None,
            task_mode=row["task_mode"] if "task_mode" in row.keys() else "pentest",
            fact_count=row["fact_count"],
            intent_count=row["intent_count"],
            working_intent_count=row["working_intent_count"],
            unclaimed_intent_count=row["unclaimed_intent_count"],
            pending_approval_count=row["pending_approval_count"],
            hint_count=row["hint_count"],
            engineered_report_status=(
                "generating" if row["has_generating_report"]
                else "done" if row["has_done_report"]
                else None
            ),
            vuln_count=row["vuln_count"] if "vuln_count" in row.keys() else 0,
            vuln_high_count=row["vuln_high_count"] if "vuln_high_count" in row.keys() else 0,
        )
        for row in rows
    ]


_LAST_EXPIRE_TS = 0.0
_EXPIRE_CADENCE_SECONDS = 30.0


def _expire_leases(conn) -> None:
    """Expire stale leases at most once per cadence (batch B3).

    Read paths (GET /projects, GET /assets) used to run four full-table UPDATEs
    on every request. Write paths (heartbeat/claim/conclude/complete/approvals)
    already expire per project, so a 30 s cadence here keeps list views
    consistent at near-zero write amplification. First call always runs."""
    global _LAST_EXPIRE_TS
    now = time.monotonic()
    if now - _LAST_EXPIRE_TS < _EXPIRE_CADENCE_SECONDS:
        return
    _LAST_EXPIRE_TS = now
    expire_workers(conn)
    expire_reason_leases(conn)
    expire_project_emergency(conn)
    expire_pending_approvals(conn)


@router.get("/projects", response_model=list[ProjectSummary])
def list_projects():
    with get_conn() as conn:
        _expire_leases(conn)
        return _project_summaries(conn)


@router.get("/assets", response_model=list[AssetGroup])
def list_assets():
    """Asset-centre view (batch 11.4): group every project under its canonical
    asset key (web host / wx AppID / android package), newest group first.
    Projects with no parseable asset key land in the '' (uncategorized) group.
    """
    with get_conn() as conn:
        _expire_leases(conn)
        summaries = _project_summaries(conn)

        grouped: dict[tuple[str, str], list[ProjectSummary]] = {}
        for summary in summaries:
            key = (summary.asset_ref or "", summary.target_kind or "web")
            grouped.setdefault(key, []).append(summary)

        assets: list[AssetGroup] = []
        for (ref, kind), projects_in_asset in grouped.items():
            projects_in_asset.sort(key=lambda s: s.created_at, reverse=True)
            counts = endpoint_counts_by_asset(conn, ref) if ref else {"total": 0, "todo": 0}
            assets.append(
                AssetGroup(
                    asset_ref=ref,
                    target_kind=kind,
                    project_count=len(projects_in_asset),
                    vuln_high_total=sum(
                        p.vuln_high_count for p in projects_in_asset
                    ),
                    pending_approval_total=sum(
                        p.pending_approval_count for p in projects_in_asset
                    ),
                    latest_created_at=projects_in_asset[0].created_at,
                    endpoint_total=int(counts["total"]),
                    endpoint_todo=int(counts["todo"]),
                    projects=projects_in_asset,
                )
            )
    assets.sort(key=lambda g: g.latest_created_at, reverse=True)
    return assets


@router.post("/projects", response_model=ProjectDetail, status_code=201)
def create_project(body: CreateProjectRequest):
    with get_conn() as conn:
        pid = next_project_id(conn)
        now = utcnow()

        # Asset-centre metadata (batch 11.4): explicit ref wins; web projects
        # derive the canonical host from the origin fact when not supplied.
        kind = body.target_kind or "web"
        asset_ref = body.asset_ref or ""
        if kind == "web" and not asset_ref:
            from sharp.server.services import extract_web_asset_ref

            asset_ref = extract_web_asset_ref(body.origin)

        projects_repo.insert(
            conn, pid, body.title, now,
            target_kind=kind, asset_ref=asset_ref,
            task_mode=body.task_mode or "pentest",
        )
        facts_repo.insert(conn, "origin", pid, body.origin)
        facts_repo.insert(conn, "goal", pid, body.goal)

        hints = []
        if body.hints:
            for h in body.hints:
                hid = next_hint_id(conn, pid)
                hints_repo.insert(conn, hid, pid, h.content, h.creator, now)
                hints.append(Hint(id=hid, content=h.content, creator=h.creator, created_at=now))

        # P3-1: inject cross-project knowledge as hints. The knowledge router
        # resolves root_domain from the origin fact and looks up matching
        # entries in the global knowledge_base.
        from sharp.server.routers.knowledge import inject_knowledge_hints
        kb_hints = inject_knowledge_hints(conn, pid, body.origin, now)
        for kh in kb_hints:
            hid = next_hint_id(conn, pid)
            hints_repo.insert(conn, hid, pid, kh["content"], kh["creator"], now)
            hints.append(Hint(id=hid, content=kh["content"], creator=kh["creator"], created_at=now))

        # Batch A1: if this asset already has endpoints on the shared ledger,
        # inject a coverage summary + still-unassessed inventory as a hint, so
        # the fresh campaign starts from structured prior work, not luck.
        from sharp.server.asset_endpoints import endpoint_counts_by_asset
        from sharp.server.routers.assets import build_asset_coverage_hints

        coverage = build_asset_coverage_hints(conn, asset_ref, now)
        for ch in coverage:
            hid = next_hint_id(conn, pid)
            hints_repo.insert(conn, hid, pid, ch["content"], ch["creator"], now)
            hints.append(Hint(id=hid, content=ch["content"], creator=ch["creator"], created_at=now))

        # 读回而不是手写构造：手写会在新增项目字段时静默漏字段
        # （task_mode 就是这么漏的），读回保证响应与库内一致。
        row = projects_repo.fetch(conn, pid)
        return ProjectDetail(
            project=project_meta_from_row(row),
            facts=[
                Fact(id="origin", description=body.origin),
                Fact(id="goal", description=body.goal),
            ],
            intents=[],
            hints=hints,
        )


@router.get("/projects/{project_id}", response_model=ProjectDetail)
def get_project(project_id: str):
    with get_conn() as conn:
        expire_workers(conn, project_id)
        expire_reason_leases(conn, project_id)
        expire_project_emergency(conn, project_id)
        expire_pending_approvals(conn, project_id=project_id)
        row = get_project_or_404(conn, project_id)

        facts = facts_repo.list_for_project(conn, project_id)
        hints = hints_repo.list_for_project(conn, project_id)

        return ProjectDetail(
            project=project_meta_from_row(row),
            facts=[Fact(**dict(f)) for f in facts],
            intents=build_intents(conn, project_id),
            hints=[Hint(**dict(h)) for h in hints],
        )


def _uploaded_apk_to_reap(conn, project_id: str) -> str | None:
    """If this project references an uploaded APK that (a) lives under the
    server's uploads dir and (b) is not referenced by any OTHER project, return
    its host path so it can be deleted with the project. Otherwise None.

    Two safety valves: the shared-file check avoids deleting an APK another
    project still uses (uploads dedupe by content hash, so one file can back
    several projects), and the uploads-dir prefix check prevents deleting any
    file outside the managed uploads area (an apk_source fact could, in theory,
    point at an arbitrary host path)."""
    from pathlib import Path
    from sharp.server.android import APK_SOURCE_FACT_ID, parse_apk_source_fact

    row = facts_repo.fetch(conn, APK_SOURCE_FACT_ID, project_id)
    if row is None:
        return None
    payload = parse_apk_source_fact(row["description"])
    if not payload:
        return None
    host_path = str(payload.get("host_path") or "").strip()
    if not host_path:
        return None

    # Valve 1: must sit under ~/.local/share/sharp/uploads/ (managed area).
    uploads_root = (Path.home() / ".local" / "share" / "sharp" / "uploads").resolve()
    try:
        resolved = Path(host_path).resolve()
    except OSError:
        return None
    if uploads_root not in resolved.parents:
        return None

    # Valve 2: not referenced by any other project's apk_source fact.
    others = facts_repo.list_by_id_in_other_projects(
        conn, APK_SOURCE_FACT_ID, project_id
    )
    for other in others:
        other_payload = parse_apk_source_fact(other["description"])
        if not other_payload:
            continue
        other_path = str(other_payload.get("host_path") or "").strip()
        if other_path and Path(other_path).resolve() == resolved:
            return None  # shared with another project — keep it

    return str(resolved)


@router.delete("/projects/{project_id}", status_code=204)
def delete_project(project_id: str):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        # Resolve the uploaded APK to reap BEFORE deleting the project (its
        # apk_source fact is cascade-deleted with the project row).
        apk_to_reap = _uploaded_apk_to_reap(conn, project_id)
        projects_repo.delete(conn, project_id)
    # Delete the file outside the DB transaction; best-effort, never blocks the
    # delete if the file is already gone or unremovable.
    if apk_to_reap:
        from pathlib import Path
        try:
            Path(apk_to_reap).unlink(missing_ok=True)
        except OSError:
            pass


@router.put("/projects/{project_id}/title", response_model=ProjectMeta)
def update_project_title(project_id: str, body: UpdateProjectTitleRequest):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        projects_repo.update_title(conn, project_id, body.title)
        updated = projects_repo.fetch(conn, project_id)
        return project_meta_from_row(updated)


@router.put("/projects/{project_id}/status", response_model=ProjectMeta)
def update_project_status(project_id: str, body: UpdateProjectStatusRequest):
    with get_conn() as conn:
        expire_reason_leases(conn, project_id)
        row = get_project_or_404(conn, project_id)
        current_status = row["status"]
        if current_status == "completed":
            raise HTTPException(409, "Completed projects cannot change status")
        if current_status == body.status:
            return project_meta_from_row(row)

        projects_repo.update_status(conn, project_id, body.status)
        if body.status == "stopped":
            intents_repo.release_workers_for_project(conn, project_id)
            clear_project_reason(conn, project_id)
        updated = projects_repo.fetch(conn, project_id)
        return project_meta_from_row(updated)


@router.put("/projects/{project_id}/deadline", response_model=ProjectMeta)
def update_project_deadline(project_id: str, body: UpdateProjectDeadlineRequest):
    """Set or clear a project's hard deadline (batch A, ISO-8601 UTC).

    Once the deadline passes the dispatcher stops handing out NEW work and the
    reason prompt switches to wrap-up mode (prioritise, close out, complete).
    """
    with get_conn() as conn:
        project = get_project_or_404(conn, project_id)
        value = (body.deadline_at or "").strip() or None
        if value is not None:
            try:
                datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as exc:
                raise HTTPException(422, f"deadline_at 需为 ISO-8601 时间：{exc}") from exc
        conn.execute(
            "UPDATE projects SET deadline_at = ? WHERE id = ?", (value, project_id)
        )
        row = conn.execute(
            "SELECT * FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        assert row is not None
        return project_meta_from_row(row)


@router.put("/projects/{project_id}/task-mode", response_model=ProjectMeta)
def update_project_task_mode(project_id: str, body: UpdateProjectTaskModeRequest):
    """切换任务模式：pentest（默认，只谈安全发现） ↔ scored（评分类任务）。

    评分类模式才会暴露旗帜/记分语义（记分板、旗帜徽章、worker 的 flag 输出指引）；
    渗透项目保持干净，不出现"分数"这类与授权测试无关的概念。
    """
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        projects_repo.update_task_mode(conn, project_id, body.task_mode)
        row = projects_repo.fetch(conn, project_id)
        return project_meta_from_row(row)


@router.put("/projects/{project_id}/paused", response_model=ProjectMeta)
def update_project_paused(project_id: str, body: UpdateProjectPausedRequest):
    """Toggle pause/resume on a project. Pause is independent of status:
    the dispatcher skips paused projects for new dispatches but does not
    cancel already-running tasks. Resume clears the flag immediately."""
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        conn.execute(
            "UPDATE projects SET paused = ? WHERE id = ?",
            (1 if body.paused else 0, project_id),
        )
        updated = projects_repo.fetch(conn, project_id)
        return project_meta_from_row(updated)


@router.put("/projects/{project_id}/budget", response_model=ProjectMeta)
def update_project_budget(project_id: str, body: UpdateProjectBudgetRequest):
    """Set the task-count budget for a project. 0 means unlimited. When
    task_count >= task_budget (>0), no new tasks are dispatched."""
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        conn.execute(
            "UPDATE projects SET task_budget = ? WHERE id = ?",
            (body.task_budget, project_id),
        )
        updated = projects_repo.fetch(conn, project_id)
        return project_meta_from_row(updated)


@router.post("/projects/{project_id}/reason/claim", response_model=ProjectMeta)
def claim_project_reason(project_id: str, body: ReasonClaimRequest):
    with get_conn() as conn:
        check_project_active(conn, project_id)
        expire_reason_leases(conn, project_id)
        row = get_project_or_404(conn, project_id)
        current_worker = row["reason_worker"]
        if current_worker == body.worker:
            return project_meta_from_row(row)

        now = utcnow()
        rowcount = projects_repo.claim_reason(
            conn, project_id, body.worker, body.trigger, now
        )
        if rowcount != 1:
            updated = get_project_or_404(conn, project_id)
            current_worker = updated["reason_worker"]
            if current_worker == body.worker:
                return project_meta_from_row(updated)
            if current_worker is not None:
                raise HTTPException(409, f"Project reason is currently claimed by {current_worker}")
            raise HTTPException(409, "Project reason claim failed")
        updated = projects_repo.fetch(conn, project_id)
        return project_meta_from_row(updated)


@router.post("/projects/{project_id}/reason/heartbeat", response_model=ProjectMeta)
def heartbeat_project_reason(project_id: str, body: HeartbeatRequest):
    with get_conn() as conn:
        check_project_active(conn, project_id)
        expire_reason_leases(conn, project_id)
        row = get_project_or_404(conn, project_id)
        current_worker = row["reason_worker"]
        if current_worker is None:
            raise HTTPException(409, "Project reason is not currently claimed")
        if current_worker != body.worker:
            raise HTTPException(409, f"Project reason is currently claimed by {current_worker}")

        now = utcnow()
        projects_repo.touch_reason_heartbeat(conn, project_id, now)
        updated = projects_repo.fetch(conn, project_id)
        return project_meta_from_row(updated)


@router.post("/projects/{project_id}/reason/release", response_model=ProjectMeta)
def release_project_reason(project_id: str, body: HeartbeatRequest):
    with get_conn() as conn:
        check_project_active(conn, project_id)
        expire_reason_leases(conn, project_id)
        row = get_project_or_404(conn, project_id)
        current_worker = row["reason_worker"]
        if current_worker is None:
            return project_meta_from_row(row)
        if current_worker != body.worker:
            raise HTTPException(409, f"Project reason is currently claimed by {current_worker}")

        clear_project_reason(conn, project_id)
        updated = projects_repo.fetch(conn, project_id)
        return project_meta_from_row(updated)


@router.post("/projects/{project_id}/complete", response_model=Intent)
def complete_project(project_id: str, body: CompleteRequest):
    with get_conn() as conn:
        check_project_active(conn, project_id)
        expire_reason_leases(conn, project_id)
        validate_facts_exist(conn, project_id, body.from_)
        validate_goal_not_in_sources(body.from_)

        now = utcnow()
        iid = next_intent_id(conn, project_id)

        intents_repo.insert(
            conn,
            iid,
            project_id,
            to_fact_id="goal",
            description=body.description,
            creator=body.worker,
            worker=body.worker,
            last_heartbeat_at=now,
            created_at=now,
            concluded_at=now,
        )
        for fid in body.from_:
            intents_repo.add_source(conn, iid, project_id, fid)
        rowcount = projects_repo.complete_and_reset_reason(conn, project_id)
        if rowcount != 1:
            raise HTTPException(409, "Project is no longer active")

        result = Intent(
            id=iid,
            **{"from": body.from_},
            to="goal",
            description=body.description,
            creator=body.worker,
            worker=body.worker,
            last_heartbeat_at=now,
            created_at=now,
            concluded_at=now,
        )
    publish(project_id, "project_completed", {"project_id": project_id})
    close_project(project_id)
    return result


@router.get("/projects/{project_id}/acceptance-check", response_model=AcceptanceCheck)
def acceptance_check(project_id: str):
    """Pre-completion inventory (batch 11.2): open intents, pending approvals,
    untrusted facts, and high/critical vulnerabilities not yet confirmed.

    Purely advisory — shown to the human before marking a project completed so
    the decision is informed. Does not block completion.
    """
    with get_conn() as conn:
        project = get_project_or_404(conn, project_id)

        # Open intent count excludes intents awaiting human approval — those are
        # reported separately as pending_approvals (the gate blocks dispatch).
        open_intents = conn.execute(
            "SELECT id, description FROM intents WHERE project_id = ? AND to_fact_id IS NULL "
            "AND approval_status != 'pending' ORDER BY created_at",
            (project_id,),
        ).fetchall()
        pending = conn.execute(
            "SELECT id, description FROM intents WHERE project_id = ? "
            "AND approval_status = 'pending' ORDER BY created_at",
            (project_id,),
        ).fetchall()
        untrusted = conn.execute(
            "SELECT id, description FROM facts WHERE project_id = ? AND trusted = 0 "
            "ORDER BY id",
            (project_id,),
        ).fetchall()
        high_unconfirmed = conn.execute(
            "SELECT id, title FROM vulnerabilities WHERE project_id = ? "
            "AND severity IN ('critical', 'high') AND status NOT IN ('dismissed', 'confirmed') "
            "ORDER BY created_at",
            (project_id,),
        ).fetchall()
        confirmed_high = conn.execute(
            "SELECT COUNT(*) c FROM vulnerabilities WHERE project_id = ? "
            "AND severity IN ('critical', 'high') AND status = 'confirmed'",
            (project_id,),
        ).fetchone()["c"]
        open_sub_goals = conn.execute(
            "SELECT id, title FROM sub_goals WHERE project_id = ? "
            "AND status IN ('pending', 'active') ORDER BY created_at",
            (project_id,),
        ).fetchall()

    open_work = bool(open_intents or pending or untrusted or high_unconfirmed or open_sub_goals)
    return AcceptanceCheck(
        project_id=project_id,
        status=project["status"],
        open_intents=[AcceptanceItem(id=r["id"], label=r["description"]) for r in open_intents],
        pending_approvals=[AcceptanceItem(id=r["id"], label=r["description"]) for r in pending],
        untrusted_facts=[AcceptanceItem(id=r["id"], label=r["description"]) for r in untrusted],
        unconfirmed_high_vulns=[AcceptanceItem(id=r["id"], label=r["title"]) for r in high_unconfirmed],
        open_sub_goals=[AcceptanceItem(id=r["id"], label=r["title"]) for r in open_sub_goals],
        confirmed_high_vulns=int(confirmed_high),
        has_open_work=open_work,
    )


@router.get("/projects/{project_id}/phase")
def project_phase(project_id: str):
    """Lightweight advisory stage estimate for the reason loop (batch 11.3).

    Purely contextual — the dispatcher injects it into reason.md to pace the
    agent (recon → credential/entry → verify → report wrap-up). Never a gate.
    """
    from sharp.server.services import compute_project_phase

    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        return compute_project_phase(conn, project_id)


@router.post("/projects/{project_id}/reopen", response_model=ReopenResponse)
def reopen_project(project_id: str, body: ReopenRequest):
    with get_conn() as conn:
        expire_reason_leases(conn, project_id)
        check_project_completed(conn, project_id)
        completion = get_completion_intent_or_409(conn, project_id)

        source_rows = intents_repo.list_source_fact_ids(
            conn, completion["id"], project_id
        )
        source_ids = [row["fact_id"] for row in source_rows]
        if not source_ids:
            raise HTTPException(409, "Completion intent is missing its source facts")

        now = utcnow()
        fact_id = next_fact_id(conn, project_id)
        intent_id = next_intent_id(conn, project_id)
        description = body.description
        creator = body.creator

        intents_repo.delete(conn, completion["id"], project_id)
        facts_repo.insert(conn, fact_id, project_id, description)
        # 外部反馈也是证据：走统一副作用（台账 + 知识），否则重开后写进来的
        # 内容对后续项目毫无贡献
        from sharp.server.fact_hooks import after_fact_write

        after_fact_write(
            conn, project_id=project_id, fact_id=fact_id, description=description, now=now,
        )
        intents_repo.insert(
            conn,
            intent_id,
            project_id,
            to_fact_id=fact_id,
            description="external_feedback",
            creator=creator,
            worker=creator,
            last_heartbeat_at=now,
            created_at=now,
            concluded_at=now,
        )
        for source_id in source_ids:
            intents_repo.add_source(conn, intent_id, project_id, source_id)
        clear_project_reason(conn, project_id)
        projects_repo.set_status_active(conn, project_id)

        updated_project = projects_repo.fetch(conn, project_id)
        updated_intent = intents_repo.fetch(conn, intent_id, project_id)
        assert updated_project is not None
        assert updated_intent is not None
        return ReopenResponse(
            project=project_meta_from_row(updated_project),
            fact=Fact(id=fact_id, description=description),
            intent=intent_to_model(conn, updated_intent, project_id),
        )


@router.get("/projects/{project_id}/stream")
async def stream_project(project_id: str):
    """SSE endpoint: push real-time events for a project to the browser."""
    from sharp.server.db import get_conn as _gc
    with _gc() as conn:
        row = projects_repo.fetch(conn, project_id)
    if not row:
        from fastapi import HTTPException as _H
        raise _H(404, f"Project not found: {project_id}")

    return StreamingResponse(
        subscribe(project_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/projects/{project_id}/increment-task-count", response_model=ProjectMeta)
def increment_task_count(project_id: str):
    """Increment the task counter for budget tracking. Called by the dispatcher
    after each task is submitted."""
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        projects_repo.increment_task_count(conn, project_id)
        updated = projects_repo.fetch(conn, project_id)
        return project_meta_from_row(updated)
