from __future__ import annotations

import logging
import time

from sharp.dispatcher.config import DispatchConfig, WorkerConfig
from sharp.dispatcher.contracts import parse_json_output, validate_reason_payload
from sharp.dispatcher.scheduler.provider_health import provider_error_status
from sharp.dispatcher.prompting import (
    format_fact_ids,
    format_hints,
    format_open_intents,
    load_prompt,
    render_prompt,
)
from sharp.dispatcher.protocol.client import SharpClient
from sharp.dispatcher.runtime.cancellation import TaskCancellation
from sharp.dispatcher.runtime.containers import ContainerManager
from sharp.dispatcher.runtime.heartbeat import HeartbeatLease
from sharp.dispatcher.tasks.explore import _asset_ledger_block
from sharp.dispatcher.tasks.common import (
    best_effort_release_reason,
    cancel_reason,
    cleanup_graph_snapshots,
    did_timeout,
    inject_mcp_flags,
    log_payload_keys,
    preflight_mcp,
    preview,
    run_healthcheck,
    run_worker_process,
    summarize_graph_yaml,
    write_graph_snapshot,
)
from sharp.dispatcher.workers.registry import get_driver
from sharp.dispatcher.protocol.client import ApiResult
from sharp.server.models import ProjectDetail
import uuid

LOG = logging.getLogger(__name__)
def _hypotheses_block(client: SharpClient, project_id: str) -> str:
    """未结算假设（P0-3）——规划器的主职责是"消灭未验证假设"。"""
    items = client.list_hypotheses(project_id, only_open=True)
    if not items:
        return (
            "（当前没有未验证假设。若某项判断还是猜想而非结论，用 hypotheses.add 记下来——"
            "假设必须可证伪，写成『若 X 则 Y』。）"
        )
    lines = []
    for h in items:
        premise = f" 依据 {h.get('premise_fact_ids')}" if h.get("premise_fact_ids") else ""
        lines.append(f"- [{h.get('id')}] ({h.get('status')}) {h.get('statement')}{premise}")
    return "\n".join(lines)


def _hints_block(project: ProjectDetail) -> str:
    """人工线索：操作员随时可以补的方向性情报。

    为什么 reason 每轮都要渲染，而不是只在建项时的 bootstrap 注入：
    操作员的中途纠偏恰恰发生在项目跑起来之后（看到 worker 走错方向才补线索），
    而 bootstrap 每个项目只跑一次——只在 bootstrap 渲染等于把运行中新增的线索
    变成只写不读的死文本。这里每轮重新取，操作员补线索当轮即可生效。
    """
    if not project.hints:
        return "(No operator hints yet for this project.)"
    hints = [
        {
            "id": hint.id,
            "content": hint.content,
            "creator": hint.creator,
            "created_at": hint.created_at,
        }
        for hint in project.hints
    ]
    return format_hints(hints)


def _knowledge_block(client: SharpClient, project_id: str) -> str:
    """跨项目知识（同目标 + 同产品）。

    为什么要在 reason 阶段拉而不是只靠建项时的 hint：建项时注入是**一次性**的，
    项目跑到一半新积累的知识对当前项目永远不可见。这里每轮重新取。
    """
    data = client.fetch_knowledge(project_id)
    if not data:
        return "（无历史知识可用——本项目是这条线上的第一次尝试，结论会被沉淀下来供后续项目复用。）"

    count = data.get("count") or 0
    if not count:
        key = data.get("key")
        return (
            f"（同目标 {key} 与同产品下暂无历史知识。"
            "本项目验证出的结论（包括『这条路不通』）会被沉淀，供后续同类目标复用。）"
        )

    parts: list[str] = []
    if data.get("block"):
        parts.append(data["block"])
    if data.get("product_block"):
        parts.append(
            "以下是**同类产品**（不同目标）上的已有经验，可信度低于同目标经验，"
            "需要用本目标的证据复核：\n" + data["product_block"]
        )
    return "\n\n".join(parts)


def _apply_hypothesis_actions(
    client: SharpClient, project_id: str, worker_name: str, payload: dict
) -> None:
    """Apply planner hypothesis directives (P0-3), best-effort.

    Payload shape:
      "hypotheses": {"add": [{"statement": "若 X 则 Y", "premise_fact_ids": ["f001"]}],
                     "update": [{"id": "h001", "status": "confirmed",
                                 "result_fact_id": "f012", "note": "..."}]}
    """
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    if not isinstance(data, dict):
        return
    block = data.get("hypotheses")
    if not isinstance(block, dict):
        return
    for item in block.get("add") or []:
        if not isinstance(item, dict):
            continue
        statement = str(item.get("statement") or "").strip()
        if not statement:
            continue
        premise = item.get("premise_fact_ids")
        premise_ids = [str(p) for p in premise] if isinstance(premise, list) else []
        try:
            resp = client.add_hypothesis(project_id, statement, premise_ids)
            if resp.ok:
                LOG.info("reason added hypothesis project=%s worker=%s statement=%s",
                         project_id, worker_name, statement[:120])
            else:
                LOG.warning("reason hypothesis add failed project=%s status=%s",
                            project_id, resp.status_code)
        except Exception as exc:
            LOG.warning("reason hypothesis add exception project=%s error=%s", project_id, exc)
    for item in block.get("update") or []:
        if not isinstance(item, dict) or not item.get("id") or not item.get("status"):
            continue
        try:
            resp = client.update_hypothesis(
                project_id, str(item["id"]), str(item["status"]),
                note=str(item.get("note") or ""),
                result_fact_id=item.get("result_fact_id"),
            )
            if not resp.ok:
                LOG.warning("reason hypothesis update failed project=%s id=%s status=%s",
                            project_id, item["id"], resp.status_code)
        except Exception as exc:
            LOG.warning("reason hypothesis update exception project=%s error=%s", project_id, exc)


def _sub_goals_block(client: SharpClient, project_id: str) -> str:
    """Current phase objectives for the planner prompt (batch C)."""
    goals = client.list_sub_goals(project_id)
    if not goals:
        return "（尚未定义阶段目标；如任务可分阶段推进，可在 sub_goals.add 里提议）"
    lines = []
    for g in goals:
        note = f" — {g.get('note')}" if g.get("note") else ""
        lines.append(f"- [{g.get('id')}] ({g.get('status')}) {g.get('title')}{note}")
    return "\n".join(lines)


def _apply_sub_goal_actions(
    client: SharpClient, project_id: str, worker_name: str, payload: dict
) -> None:
    """Apply planner sub-goal directives (batch C), best-effort.

    Payload shape:
      "sub_goals": {"add": [{"title": "..."}],
                    "update": [{"id": "sg001", "status": "done", "note": "..."}]}
    """
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    if not isinstance(data, dict):
        return
    block = data.get("sub_goals")
    if not isinstance(block, dict):
        return
    for item in block.get("add") or []:
        title = ""
        if isinstance(item, dict):
            title = str(item.get("title") or "").strip()
        elif isinstance(item, str):
            title = item.strip()
        if not title:
            continue
        try:
            resp = client.add_sub_goal(project_id, title)
            if resp.ok:
                LOG.info("reason added sub goal project=%s worker=%s title=%s", project_id, worker_name, title[:120])
            else:
                LOG.warning("reason sub goal add failed project=%s worker=%s status=%s", project_id, worker_name, resp.status_code)
        except Exception as exc:
            LOG.warning("reason sub goal add exception project=%s error=%s", project_id, exc)
    for item in block.get("update") or []:
        if not isinstance(item, dict) or not item.get("id") or not item.get("status"):
            continue
        status = str(item["status"])
        if status not in ("pending", "active", "done", "abandoned"):
            continue
        try:
            resp = client.update_sub_goal(project_id, str(item["id"]), status, str(item.get("note") or ""))
            if resp.ok:
                LOG.info("reason updated sub goal project=%s worker=%s id=%s status=%s", project_id, worker_name, item["id"], status)
            else:
                LOG.warning("reason sub goal update failed project=%s worker=%s id=%s status=%s", project_id, worker_name, item["id"], resp.status_code)
        except Exception as exc:
            LOG.warning("reason sub goal update exception project=%s id=%s error=%s", project_id, item["id"], exc)


def _deadline_context(project) -> str:
    """Remaining-time context for the planner (batch A). Empty when no deadline."""
    deadline = getattr(project.project, "deadline_at", None)
    if not deadline:
        return "（未设置截止时间；按价值推进，不必抢时间）"
    try:
        from datetime import datetime, timezone

        dt = datetime.fromisoformat(deadline.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        remaining = dt - datetime.now(timezone.utc)
        minutes = int(remaining.total_seconds() // 60)
        if minutes <= 0:
            return (f"⚠️ 已超过截止时间（{deadline}）：不要再提议新的大范围探索；"
                    "优先梳理已有结论、用 abandon 退休无价值行动，并考虑 complete。")
        return (f"距截止时间 {deadline} 还有约 {minutes} 分钟："
                "优先高价值/高分行动，对久攻不下或价值低的开放行动用 abandon 退休、"
                "用 prioritize 提升关键行动优先级。")
    except Exception:
        return f"（截止时间 {deadline}）"


def _render_phase_context(client, project_id: str) -> str:
    """Best-effort stage context for the reason prompt (batch 11.3).

    A phase-outage / network blip must never block reason dispatch, so any
    failure degrades to a neutral notice."""
    try:
        phase = client.fetch_project_phase(project_id)
    except Exception:
        phase = None
    if not phase or not isinstance(phase, dict):
        return "（服务端阶段估算不可用——按图本身判断节奏）"
    lines = [
        f"阶段：{phase.get('label') or phase.get('phase') or '未知'}",
        (phase.get("guidance") or "").strip(),
    ]
    pointers = phase.get("pointers") or []
    if pointers:
        lines.append("注意：" + "；".join(str(pt) for pt in pointers))
    return "\n".join(line for line in lines if line)

# Reason only re-runs when the graph changes, so a dropped intent can stall
# exploration. Retry transient server/network failures a few times with backoff.
# Retries are safe against duplicates: each logical intent carries a stable
# idempotency_key reused across attempts, so a first success whose response was
# lost (status 0) or a post-commit 5xx resolves to the same server-side intent.
# See docs/adr/0001-reason-intent-retry-non-idempotent.md (Resolved by IDEMP-1).
INTENT_WRITE_MAX_ATTEMPTS = 3
INTENT_WRITE_BACKOFF_SECONDS = 0.5


def _is_transient_status(status_code: int) -> bool:
    return status_code == 0 or status_code >= 500


def _apply_planning_actions(
    client: SharpClient, project_id: str, worker_name: str, payload: dict
) -> None:
    """Apply the planner's optional abandon/prioritize directives (batch A).

    Payload shape (both optional, both best-effort):
      "abandon":    [{"id": "i003", "reason": "dead end"}]
      "prioritize": [{"id": "i002", "priority": 20}]
    """
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    if not isinstance(data, dict):
        return
    for item in data.get("abandon") or []:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        try:
            resp = client.abandon_intent(project_id, str(item["id"]), str(item.get("reason") or ""))
            if resp.ok:
                LOG.info(
                    "reason abandoned intent project=%s worker=%s intent=%s reason=%s",
                    project_id, worker_name, item["id"], (item.get("reason") or "")[:120],
                )
            else:
                LOG.warning(
                    "reason abandon failed project=%s worker=%s intent=%s status=%s",
                    project_id, worker_name, item["id"], resp.status_code,
                )
        except Exception as exc:
            LOG.warning("reason abandon exception project=%s intent=%s error=%s", project_id, item["id"], exc)
    for item in data.get("prioritize") or []:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        try:
            priority = int(item.get("priority", 0))
        except (TypeError, ValueError):
            continue
        try:
            resp = client.set_intent_priority(project_id, str(item["id"]), priority)
            if resp.ok:
                LOG.info(
                    "reason reprioritised intent project=%s worker=%s intent=%s priority=%s",
                    project_id, worker_name, item["id"], priority,
                )
            else:
                LOG.warning(
                    "reason priority failed project=%s worker=%s intent=%s status=%s",
                    project_id, worker_name, item["id"], resp.status_code,
                )
        except Exception as exc:
            LOG.warning("reason priority exception project=%s intent=%s error=%s", project_id, item["id"], exc)


def _create_intent_with_retry(
    client: SharpClient,
    project_id: str,
    from_ids: list[str],
    description: str,
    worker_name: str,
    risk_level: str | None = None,
    risk_reason: str | None = None,
) -> ApiResult:
    # One stable key per logical intent, reused across retries so the server
    # deduplicates rather than creating duplicates.
    idempotency_key = uuid.uuid4().hex
    response = client.create_intent(project_id, from_ids, description, worker_name, idempotency_key, risk_level, risk_reason)
    attempt = 1
    while _is_transient_status(response.status_code) and attempt < INTENT_WRITE_MAX_ATTEMPTS:
        backoff = INTENT_WRITE_BACKOFF_SECONDS * attempt
        LOG.warning(
            "reason intent write transient failure project=%s worker=%s status=%s attempt=%s/%s retry_in=%.1fs",
            project_id,
            worker_name,
            response.status_code,
            attempt,
            INTENT_WRITE_MAX_ATTEMPTS,
            backoff,
        )
        time.sleep(backoff)
        response = client.create_intent(project_id, from_ids, description, worker_name, idempotency_key, risk_level, risk_reason)
        attempt += 1
    return response


def run_reason_task(
    config: DispatchConfig,
    client: SharpClient,
    container_manager: ContainerManager,
    project: ProjectDetail,
    export_yaml: str,
    worker: WorkerConfig,
    cancellation: TaskCancellation,
) -> str:
    driver = get_driver(worker.type)
    task_started = time.perf_counter()
    healthcheck_timeout = config.runtime.healthcheck_timeout
    lease = HeartbeatLease.for_reason(client, project.project.id, worker.name, config.runtime.interval)
    lease.start()
    snapshot_dirs: list[str] = []
    try:
        container_name = container_manager.ensure_running(project.project.id)

        LOG.info(
            "starting container exec project=%s worker=%s phase=reason_healthcheck timeout=%ss",
            project.project.id,
            worker.name,
            healthcheck_timeout,
        )
        healthcheck = run_healthcheck(
            container_manager,
            container_name,
            worker,
            driver.build_healthcheck(worker),
            timeout_seconds=healthcheck_timeout,
            lease=lease,
            cancellation=cancellation,
        )
        cancelled = cancel_reason(healthcheck.result, cancellation)
        if cancelled is not None:
            LOG.info(
                "reason cancelled during healthcheck project=%s worker=%s reason=%s",
                project.project.id,
                worker.name,
                cancelled,
            )
            return "cancelled"
        if lease.failure is not None:
            LOG.warning(
                "heartbeat lost during reason healthcheck project=%s worker=%s status=%s",
                project.project.id,
                worker.name,
                lease.failure.status_code,
            )
            return "failed"
        if healthcheck.result.returncode != 0:
            LOG.warning(
                "worker unhealthy project=%s worker=%s healthcheck_ms=%s stderr=%s",
                project.project.id,
                worker.name,
                healthcheck.duration_ms,
                preview(healthcheck.result.stderr),
            )
            return "unhealthy"
        open_intents = [
            {
                "id": intent.id,
                "from": intent.from_,
                "description": intent.description,
                "worker": intent.worker,
            }
            for intent in project.intents
            if intent.to is None
            and intent.approval_status != "pending"  # don't re-propose awaiting-approval intents
        ]
        allowed_fact_ids = [fact.id for fact in project.facts if fact.id != "goal"]
        LOG.debug(
            "reason context prepared project=%s worker=%s facts=%s allowed_fact_ids=%s hints=%s open_intents=%s",
            project.project.id,
            worker.name,
            len(project.facts),
            len(allowed_fact_ids),
            len(project.hints),
            len(open_intents),
        )
        snapshot = write_graph_snapshot(
            container_manager,
            container_name,
            summarize_graph_yaml(export_yaml.strip()),
            phase="reason_execute",
        )
        snapshot_dirs.append(snapshot.directory)
        phase_block = _render_phase_context(client, project.project.id)
        prompt = render_prompt(
            load_prompt(config.runtime.prompt_group, "reason.md"),
            {
                "graph_yaml": snapshot.reference,
                "fact_ids": format_fact_ids(allowed_fact_ids),
                "open_intents": format_open_intents(open_intents),
                "max_intents": str(config.tasks.reason.max_intents),
                "phase_context": phase_block,
                "deadline_context": _deadline_context(project),
                "sub_goals": _sub_goals_block(client, project.project.id),
                "hypotheses": _hypotheses_block(client, project.project.id),
                "knowledge": _knowledge_block(client, project.project.id),
                "asset_ledger": _asset_ledger_block(client, project.project.id),
                "hints": _hints_block(project),
            },
        )

        session = driver.prepare_session()
        command = driver.build_execute(worker, prompt, session)
        mcp_path = preflight_mcp(config, container_manager, container_name)
        execute_argv = inject_mcp_flags(command.argv, mcp_path)
        execute_started = time.perf_counter()
        result = run_worker_process(
            container_manager,
            container_name,
            worker,
            execute_argv,
            phase="reason_execute",
            timeout_seconds=config.tasks.reason.timeout,
            lease=lease,
            cancellation=cancellation,
        )
        execute_ms = int((time.perf_counter() - execute_started) * 1000)
        total_ms = int((time.perf_counter() - task_started) * 1000)
        session = driver.extract_session(session, result.stdout, result.stderr)
        cancelled = cancel_reason(result, cancellation)
        if cancelled is not None:
            LOG.info(
                "reason cancelled project=%s worker=%s reason=%s execute_ms=%s",
                project.project.id,
                worker.name,
                cancelled,
                execute_ms,
            )
            return "cancelled"
        if lease.failure is not None:
            LOG.warning(
                "heartbeat lost during reason project=%s worker=%s status=%s execute_ms=%s",
                project.project.id,
                worker.name,
                lease.failure.status_code,
                execute_ms,
            )
            return "failed"
        if did_timeout(result):
            LOG.warning(
                "reason timed out project=%s worker=%s execute_ms=%s total_ms=%s stdout_preview=%s stderr_preview=%s",
                project.project.id,
                worker.name,
                execute_ms,
                total_ms,
                preview(result.stdout),
                preview(result.stderr),
            )
            return "failed"
        if result.returncode != 0:
            LOG.warning(
                "reason command failed project=%s worker=%s code=%s execute_ms=%s total_ms=%s stdout_preview=%s stderr_preview=%s",
                project.project.id,
                worker.name,
                result.returncode,
                execute_ms,
                total_ms,
                preview(result.stdout),
                preview(result.stderr),
            )
            provider_failure = provider_error_status(result.stderr)
            if provider_failure:
                LOG.warning(
                    "reason provider failure project=%s worker=%s status=%s",
                    project.project.id, worker.name, provider_failure,
                )
                return provider_failure
            return "failed"
        try:
            model_output = driver.extract_response_text(result.stdout, result.stderr)
            payload = parse_json_output(model_output)
            kind, data = validate_reason_payload(
                payload, open_intents_empty=not open_intents, max_intents=config.tasks.reason.max_intents,
            )
            log_payload_keys(LOG, f"reason project={project.project.id}", payload)
        except Exception as exc:
            LOG.warning(
                "reason parse failed project=%s worker=%s error=%s execute_ms=%s total_ms=%s stdout_preview=%s stderr_preview=%s",
                project.project.id,
                worker.name,
                exc,
                execute_ms,
                total_ms,
                preview(result.stdout),
                preview(result.stderr),
            )
            return "failed"
        # Batch A: planner may also retire low-value steps or re-prioritise open
        # ones — independent of the primary kind (intents/complete/noop).
        _apply_planning_actions(client, project.project.id, worker.name, payload)
        _apply_sub_goal_actions(client, project.project.id, worker.name, payload)
        _apply_hypothesis_actions(client, project.project.id, worker.name, payload)
        if kind == "rejected":
            LOG.warning(
                "reason rejected project=%s worker=%s execute_ms=%s total_ms=%s stdout_preview=%s",
                project.project.id,
                worker.name,
                execute_ms,
                total_ms,
                preview(result.stdout),
            )
            return "rejected"
        if kind == "complete":
            response = client.complete(project.project.id, data["from"], data["description"], worker.name)
            if response.status_code == 403:
                LOG.info("project became inactive during reason complete project=%s worker=%s", project.project.id, worker.name)
                return "success"
            if not response.ok:
                LOG.warning(
                    "reason complete write failed project=%s worker=%s status=%s body=%s",
                    project.project.id,
                    worker.name,
                    response.status_code,
                    response.text,
                )
                return "failed"
            LOG.info(
                "project completed project=%s worker=%s from=%s execute_ms=%s total_ms=%s",
                project.project.id,
                worker.name,
                data["from"],
                execute_ms,
                total_ms,
            )
            return "success"
        if kind == "intents":
            created = 0
            for intent_data in data:
                response = _create_intent_with_retry(
                    client,
                    project.project.id,
                    intent_data["from"],
                    intent_data["description"],
                    worker.name,
                    intent_data.get("risk_level"),
                    intent_data.get("risk_reason"),
                )
                if response.status_code == 403:
                    LOG.info("project became inactive during reason intent create project=%s worker=%s created=%s", project.project.id, worker.name, created)
                    return "success"
                if response.status_code == 409:
                    LOG.info("reason intent lost race project=%s worker=%s from=%s", project.project.id, worker.name, intent_data["from"])
                    continue
                if not response.ok:
                    LOG.warning(
                        "reason intent write failed project=%s worker=%s status=%s body=%s",
                        project.project.id,
                        worker.name,
                        response.status_code,
                        response.text,
                    )
                    continue
                created += 1
                LOG.info(
                    "reason created intent project=%s worker=%s from=%s description=%s",
                    project.project.id,
                    worker.name,
                    intent_data["from"],
                    intent_data["description"],
                )
            LOG.info(
                "reason finished project=%s worker=%s created_intents=%s/%s execute_ms=%s total_ms=%s",
                project.project.id,
                worker.name,
                created,
                len(data),
                execute_ms,
                total_ms,
            )
            if created == 0:
                LOG.warning(
                    "reason created no intents project=%s worker=%s attempted=%s execute_ms=%s total_ms=%s",
                    project.project.id,
                    worker.name,
                    len(data),
                    execute_ms,
                    total_ms,
                )
                return "failed"
            return "success"
        LOG.info(
            "reason finished without graph change project=%s worker=%s execute_ms=%s total_ms=%s",
            project.project.id,
            worker.name,
            execute_ms,
            total_ms,
        )
        return "success"
    finally:
        lease.stop()
        best_effort_release_reason(client, project.project.id, worker.name)
        cleanup_graph_snapshots(container_manager, container_manager.container_name(project.project.id), snapshot_dirs)
