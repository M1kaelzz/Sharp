from __future__ import annotations

import logging
import time

from sharp.dispatcher.config import DispatchConfig, WorkerConfig
from sharp.dispatcher.contracts import (
    parse_json_output,
    validate_bootstrap_conclude_payload,
    validate_bootstrap_execute_payload,
)
from sharp.dispatcher.prompting import format_env_baseline, format_hints, load_prompt, render_prompt, scoring_note
from sharp.dispatcher.scheduler.provider_health import provider_error_status
from sharp.dispatcher.protocol.client import SharpClient
from sharp.dispatcher.runtime.cancellation import TaskCancellation
from sharp.dispatcher.runtime.containers import ContainerManager
from sharp.dispatcher.runtime.heartbeat import HeartbeatLease
from sharp.dispatcher.tasks.common import (
    ApkInjectionError,
    best_effort_release,
    cancel_reason,
    did_timeout,
    ensure_apk_injected,
    inject_mcp_flags,
    log_payload_keys,
    preflight_mcp,
    project_allows_conclude_fallback,
    preview,
    run_healthcheck,
    run_worker_process,
    write_conclude_result_with_fact_id,
)
from sharp.dispatcher.workers.registry import get_driver
from sharp.server.models import Intent, ProjectDetail

LOG = logging.getLogger(__name__)


def run_bootstrap_task(
    config: DispatchConfig,
    client: SharpClient,
    container_manager: ContainerManager,
    project: ProjectDetail,
    intent: Intent,
    worker: WorkerConfig,
    cancellation: TaskCancellation,
) -> str:
    driver = get_driver(worker.type)
    task_started = time.perf_counter()
    healthcheck_timeout = config.runtime.healthcheck_timeout
    lease = HeartbeatLease.for_intent(client, project.project.id, intent.id, worker.name, config.runtime.interval)
    lease.start()
    try:
        container_name = container_manager.ensure_running(project.project.id)

        try:
            ensure_apk_injected(container_manager, container_name, project)
        except ApkInjectionError as exc:
            LOG.warning(
                "bootstrap apk injection failed project=%s intent=%s worker=%s reason=%s",
                project.project.id,
                intent.id,
                worker.name,
                exc,
            )
            client.conclude(
                project.project.id,
                intent.id,
                worker.name,
                f"APK 注入容器失败，无法进行反编译分析：{exc}",
            )
            best_effort_release(client, project.project.id, intent.id, worker.name)
            return "failed"

        LOG.info(
            "starting container exec project=%s intent=%s worker=%s phase=bootstrap_healthcheck timeout=%ss",
            project.project.id,
            intent.id,
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
                "bootstrap cancelled during healthcheck project=%s intent=%s worker=%s reason=%s",
                project.project.id,
                intent.id,
                worker.name,
                cancelled,
            )
            best_effort_release(client, project.project.id, intent.id, worker.name)
            return "cancelled"
        if lease.failure is not None:
            LOG.warning(
                "heartbeat lost during bootstrap healthcheck project=%s intent=%s worker=%s status=%s",
                project.project.id,
                intent.id,
                worker.name,
                lease.failure.status_code,
            )
            best_effort_release(client, project.project.id, intent.id, worker.name)
            return "failed"
        if healthcheck.result.returncode != 0:
            LOG.warning(
                "worker unhealthy project=%s intent=%s worker=%s healthcheck_ms=%s stderr=%s",
                project.project.id,
                intent.id,
                worker.name,
                healthcheck.duration_ms,
                preview(healthcheck.result.stderr),
            )
            best_effort_release(client, project.project.id, intent.id, worker.name)
            return "unhealthy"

        prompt = render_prompt(
            load_prompt(config.runtime.prompt_group, "bootstrap.md"),
            _bootstrap_prompt_replacements(project, client),
        )

        session = driver.prepare_session()
        execute = driver.build_execute(worker, prompt, session)
        session = execute.session
        mcp_path = preflight_mcp(config, container_manager, container_name)
        execute_argv = inject_mcp_flags(execute.argv, mcp_path)
        execute_started = time.perf_counter()
        first = run_worker_process(
            container_manager,
            container_name,
            worker,
            execute_argv,
            phase="bootstrap",
            timeout_seconds=config.tasks.bootstrap.timeout,
            lease=lease,
            cancellation=cancellation,
            stdout_callback=lambda text: client.push_live_output(
                project.project.id, intent.id, worker.name, text
            ),
        )
        execute_ms = int((time.perf_counter() - execute_started) * 1000)
        session = driver.extract_session(session, first.stdout, first.stderr)
        cancelled = cancel_reason(first, cancellation)
        if cancelled is not None:
            LOG.info(
                "bootstrap cancelled project=%s intent=%s worker=%s reason=%s execute_ms=%s",
                project.project.id,
                intent.id,
                worker.name,
                cancelled,
                execute_ms,
            )
            best_effort_release(client, project.project.id, intent.id, worker.name)
            return "cancelled"
        if lease.failure is not None:
            LOG.warning(
                "heartbeat lost during bootstrap project=%s intent=%s worker=%s status=%s execute_ms=%s",
                project.project.id,
                intent.id,
                worker.name,
                lease.failure.status_code,
                execute_ms,
            )
            best_effort_release(client, project.project.id, intent.id, worker.name)
            return "failed"
        if not did_timeout(first) and first.returncode == 0:
            try:
                model_output = driver.extract_response_text(first.stdout, first.stderr)
                payload = parse_json_output(model_output)
                log_payload_keys(LOG, "bootstrap", payload)
                kind, data = validate_bootstrap_execute_payload(payload)
            except Exception as exc:
                LOG.warning(
                    "bootstrap parse failed project=%s intent=%s worker=%s error=%s execute_ms=%s total_ms=%s stdout_preview=%s stderr_preview=%s",
                    project.project.id,
                    intent.id,
                    worker.name,
                    exc,
                    execute_ms,
                    int((time.perf_counter() - task_started) * 1000),
                    preview(first.stdout),
                    preview(first.stderr),
                )
                return _try_conclude_fallback(
                    config,
                    client,
                    container_manager,
                    container_name,
                    worker,
                    driver,
                    project,
                    intent,
                    session,
                    lease,
                    cancellation,
                )
            if kind == "rejected":
                LOG.warning(
                    "bootstrap rejected project=%s intent=%s worker=%s execute_ms=%s total_ms=%s stdout_preview=%s",
                    project.project.id,
                    intent.id,
                    worker.name,
                    execute_ms,
                    int((time.perf_counter() - task_started) * 1000),
                    preview(first.stdout),
                )
                best_effort_release(client, project.project.id, intent.id, worker.name)
                return "rejected"
            return _write_bootstrap_complete_result(
                client,
                project.project.id,
                intent.id,
                worker.name,
                data["fact_description"],
                data["complete_description"],
                source="bootstrap",
                findings=data.get("findings"),
                env_facts=data.get("env_facts"),
                product=data.get("product"),
                endpoint_tests=data.get("endpoint_tests"),
                phase_ms=execute_ms,
                total_ms=int((time.perf_counter() - task_started) * 1000),
            )
        if did_timeout(first):
            LOG.warning(
                "bootstrap timed out project=%s intent=%s worker=%s execute_ms=%s total_ms=%s stdout_preview=%s stderr_preview=%s",
                project.project.id,
                intent.id,
                worker.name,
                execute_ms,
                int((time.perf_counter() - task_started) * 1000),
                preview(first.stdout),
                preview(first.stderr),
            )
            return _try_conclude_fallback(
                config,
                client,
                container_manager,
                container_name,
                worker,
                driver,
                project,
                intent,
                session,
                lease,
                cancellation,
            )
        LOG.warning(
            "bootstrap command failed project=%s intent=%s worker=%s code=%s execute_ms=%s total_ms=%s stdout_preview=%s stderr_preview=%s",
            project.project.id,
            intent.id,
            worker.name,
            first.returncode,
            execute_ms,
            int((time.perf_counter() - task_started) * 1000),
            preview(first.stdout),
            preview(first.stderr),
        )
        best_effort_release(client, project.project.id, intent.id, worker.name)
        provider_failure = provider_error_status(first.stderr)
        if provider_failure:
            LOG.warning(
                "bootstrap provider failure project=%s worker=%s status=%s stderr_preview=%s",
                project.project.id, worker.name, provider_failure, preview(first.stderr),
            )
            return provider_failure
        return "failed"
    except Exception:
        LOG.exception("bootstrap task crashed project=%s intent=%s worker=%s", project.project.id, intent.id, worker.name)
        best_effort_release(client, project.project.id, intent.id, worker.name)
        return "failed"
    finally:
        lease.stop()


def _try_conclude_fallback(
    config: DispatchConfig,
    client: SharpClient,
    container_manager: ContainerManager,
    container_name: str,
    worker: WorkerConfig,
    driver,
    project: ProjectDetail,
    intent: Intent,
    session: str | None,
    lease: HeartbeatLease,
    cancellation: TaskCancellation,
) -> str:
    if not driver.supports_conclude() or not session:
        LOG.info(
            "bootstrap conclude fallback unavailable project=%s intent=%s worker=%s supports_conclude=%s has_session=%s",
            project.project.id,
            intent.id,
            worker.name,
            driver.supports_conclude(),
            bool(session),
        )
        best_effort_release(client, project.project.id, intent.id, worker.name)
        return "failed"
    if lease.failure is not None:
        LOG.warning(
            "bootstrap conclude fallback skipped because heartbeat already lost project=%s intent=%s worker=%s",
            project.project.id,
            intent.id,
            worker.name,
        )
        best_effort_release(client, project.project.id, intent.id, worker.name)
        return "failed"
    if cancellation.is_cancelled:
        LOG.info(
            "bootstrap conclude fallback skipped because task was cancelled project=%s intent=%s worker=%s reason=%s",
            project.project.id,
            intent.id,
            worker.name,
            cancellation.reason,
        )
        best_effort_release(client, project.project.id, intent.id, worker.name)
        return "cancelled"

    if not project_allows_conclude_fallback(
        client,
        project.project.id,
        worker_name=worker.name,
        intent_id=intent.id,
    ):
        best_effort_release(client, project.project.id, intent.id, worker.name)
        return "failed"

    container_name = container_manager.ensure_running(project.project.id)

    prompt = render_prompt(
        load_prompt(config.runtime.prompt_group, "bootstrap_conclude.md"),
        _bootstrap_prompt_replacements(project, client),
    )
    conclude_argv = driver.build_conclude(worker, prompt, session)
    mcp_path = preflight_mcp(config, container_manager, container_name)
    conclude_argv = inject_mcp_flags(conclude_argv, mcp_path)
    LOG.info("starting bootstrap conclude fallback project=%s intent=%s worker=%s", project.project.id, intent.id, worker.name)
    conclude_started = time.perf_counter()
    result = run_worker_process(
        container_manager,
        container_name,
        worker,
        conclude_argv,
        phase="bootstrap_conclude",
        timeout_seconds=config.tasks.bootstrap.conclude_timeout,
        lease=lease,
        cancellation=cancellation,
    )
    conclude_ms = int((time.perf_counter() - conclude_started) * 1000)
    cancelled = cancel_reason(result, cancellation)
    if cancelled is not None:
        LOG.info(
            "bootstrap conclude cancelled project=%s intent=%s worker=%s reason=%s conclude_ms=%s",
            project.project.id,
            intent.id,
            worker.name,
            cancelled,
            conclude_ms,
        )
        best_effort_release(client, project.project.id, intent.id, worker.name)
        return "cancelled"
    if lease.failure is not None:
        best_effort_release(client, project.project.id, intent.id, worker.name)
        return "failed"
    if result.timed_out or result.returncode != 0:
        LOG.warning(
            "bootstrap conclude failed project=%s intent=%s worker=%s code=%s timed_out=%s conclude_ms=%s stdout_preview=%s stderr_preview=%s",
            project.project.id,
            intent.id,
            worker.name,
            result.returncode,
            result.timed_out,
            conclude_ms,
            preview(result.stdout),
            preview(result.stderr),
        )
        best_effort_release(client, project.project.id, intent.id, worker.name)
        return "failed"
    try:
        model_output = driver.extract_response_text(result.stdout, result.stderr)
        payload = parse_json_output(model_output)
        conclude_data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        if isinstance(conclude_data, dict) and isinstance(conclude_data.get("complete"), dict):
            LOG.warning(
                "bootstrap conclude returned unexpected complete payload project=%s intent=%s worker=%s complete_preview=%s",
                project.project.id,
                intent.id,
                worker.name,
                preview(str(conclude_data.get("complete"))),
            )
        kind, fact_description = validate_bootstrap_conclude_payload(payload)
    except Exception as exc:
        LOG.warning(
            "bootstrap conclude parse failed project=%s intent=%s worker=%s error=%s conclude_ms=%s stdout_preview=%s stderr_preview=%s",
            project.project.id,
            intent.id,
            worker.name,
            exc,
            conclude_ms,
            preview(result.stdout),
            preview(result.stderr),
        )
        best_effort_release(client, project.project.id, intent.id, worker.name)
        return "failed"
    if kind == "rejected":
        LOG.warning(
            "bootstrap conclude rejected project=%s intent=%s worker=%s conclude_ms=%s stdout_preview=%s",
            project.project.id,
            intent.id,
            worker.name,
            conclude_ms,
            preview(result.stdout),
        )
        best_effort_release(client, project.project.id, intent.id, worker.name)
        return "rejected"
    # conclude 路径同样要接住 findings / 环境基线 / 产品标注 —— 否则 worker 明明报了，
    # 却因为"这条路径没写"而丢。实测：bootstrap 跑满 400s 超时后的 conclude 回包里
    # env_facts 与 product 都在、findings 也可能在，但旧实现只写了 fact。
    # 懒导入与下方同一写法，避免模块循环引用。
    from sharp.dispatcher.contracts import conclude_findings
    from sharp.dispatcher.tasks.explore import (
        _best_effort_assess_endpoints,
        _best_effort_set_product,
        _best_effort_upsert_baseline,
    )

    # 用带 fact_id 的写入口：登记漏洞必须以"本次结论产生的那条证据"为依据，
    # 与主路径一致（写入口返回对象里带 fact_id，不必再猜）。
    concluded = write_conclude_result_with_fact_id(
        client,
        project.project.id,
        intent.id,
        worker.name,
        fact_description,
        source="bootstrap_conclude",
        phase_ms=conclude_ms,
    )
    if concluded.status != "success":
        return "failed"
    try:
        from sharp.dispatcher.tasks.explore import _best_effort_extract_knowledge

        _best_effort_extract_knowledge(client, project.project.id, fact_description)
    except Exception as exc:
        LOG.warning(
            "bootstrap conclude knowledge extract failed project=%s error=%s",
            project.project.id,
            exc,
        )
    findings = conclude_findings(payload)
    if findings and concluded.fact_id:
        from sharp.dispatcher.tasks.explore import _best_effort_create_vulns

        _best_effort_create_vulns(
            client, project.project.id, concluded.fact_id, intent.id, worker.name, findings
        )
    _best_effort_upsert_baseline(
        client, project.project.id, worker.name, payload, phase="bootstrap_conclude"
    )
    _best_effort_set_product(
        client, project.project.id, worker.name, payload, phase="bootstrap_conclude"
    )
    _best_effort_assess_endpoints(
        client, project.project.id, worker.name, payload, phase="bootstrap_conclude"
    )
    return "success"


def _bootstrap_prompt_replacements(project: ProjectDetail, client: SharpClient | None = None) -> dict[str, str]:
    facts = {fact.id: fact.description for fact in project.facts}
    hints = [
        {
            "id": hint.id,
            "content": hint.content,
            "creator": hint.creator,
            "created_at": hint.created_at,
        }
        for hint in project.hints
    ]
    return {
        "origin": facts.get("origin", ""),
        "goal": facts.get("goal", ""),
        "hints": format_hints(hints),
        "scoring_note": scoring_note(getattr(project.project, "task_mode", "pentest")),
        "env_baseline": format_env_baseline(
            client.fetch_env_baseline(project.project.id) if client else []
        ),
    }


def _write_bootstrap_complete_result(
    client: SharpClient,
    project_id: str,
    intent_id: str,
    worker_name: str,
    fact_description: str,
    complete_description: str,
    *,
    source: str,
    findings: list[dict] | None = None,
    env_facts: list[dict] | None = None,
    product: str | None = None,
    endpoint_tests: list[dict] | None = None,
    phase_ms: int,
    total_ms: int | None = None,
) -> str:
    conclude = write_conclude_result_with_fact_id(
        client,
        project_id,
        intent_id,
        worker_name,
        fact_description,
        source=source,
        phase_ms=phase_ms,
        total_ms=total_ms,
    )
    if conclude.status != "success":
        return "failed"
    if conclude.fact_id is None:
        LOG.warning(
            "bootstrap complete deferred because conclude response omitted fact id project=%s intent=%s worker=%s source=%s",
            project_id,
            intent_id,
            worker_name,
            source,
        )
        return "success"

    # 知识提取必须在 bootstrap 也做一遍：此前它只接在 explore 上，导致"整个项目在
    # bootstrap 阶段就得出结论"的场景（实测 proj_001/003/004 三条全如此）对知识库的
    # 贡献恒为 0 —— 结论里明明有大量"已验证不通"，却一条都没沉淀下来。
    try:
        from sharp.dispatcher.tasks.explore import _best_effort_extract_knowledge

        _best_effort_extract_knowledge(client, project_id, fact_description)
    except Exception as exc:
        LOG.warning("bootstrap knowledge extract failed project=%s error=%s", project_id, exc)

    # Batch VULN-BOOTSTRAP: confirmed findings must reach the structured
    # vulnerability library even when bootstrap completes the project in one
    # shot (previously they lived only in the conclusion prose).
    if findings:
        try:
            from sharp.dispatcher.tasks.explore import _best_effort_create_vulns

            _best_effort_create_vulns(
                client, project_id, conclude.fact_id, intent_id, worker_name, findings
            )
        except Exception as exc:
            LOG.warning(
                "bootstrap vuln registration failed project=%s error=%s",
                project_id,
                exc,
            )

    # P1-B：bootstrap 也会摸到接口，评估同样要回流（否则台账状态永远停在 discovered）
    try:
        from sharp.dispatcher.tasks.explore import _best_effort_assess_endpoints

        _best_effort_assess_endpoints(
            client, project_id, worker_name,
            {"endpoint_tests": endpoint_tests}, phase="bootstrap",
        )
    except Exception as exc:
        LOG.warning("bootstrap endpoint assess failed project=%s error=%s", project_id, exc)

    # P0-2 修正版：bootstrap 也会确认环境前提（连通性、凭据、可达性），
    # 由 dispatcher 代写基线（worker 容器没有 Sharp 凭据，不该直连 API）。
    if env_facts:
        from sharp.dispatcher.tasks.explore import _extract_env_facts, _best_effort_upsert_baseline

        _best_effort_upsert_baseline(
            client, project_id, worker_name, {"env_facts": env_facts}, phase="bootstrap"
        )

    # P0-C：bootstrap 阶段通常就是识别指纹的时候 —— 产品标注在这里上报最有依据。
    if product:
        from sharp.dispatcher.tasks.explore import _best_effort_set_product

        _best_effort_set_product(
            client, project_id, worker_name, {"product": product}, phase="bootstrap"
        )

    response = client.complete(project_id, [conclude.fact_id], complete_description, worker_name)
    if response.status_code in (403, 409):
        LOG.info(
            "bootstrap complete deferred project=%s intent=%s worker=%s source=%s status=%s fact_id=%s",
            project_id,
            intent_id,
            worker_name,
            source,
            response.status_code,
            conclude.fact_id,
        )
        return "success"
    if not response.ok:
        LOG.warning(
            "bootstrap complete write failed project=%s intent=%s worker=%s source=%s fact_id=%s status=%s body=%s",
            project_id,
            intent_id,
            worker_name,
            source,
            conclude.fact_id,
            response.status_code,
            response.text,
        )
        # The fact was concluded but the project was not marked complete. Report
        # this honestly as a failure; a subsequent reason step will re-evaluate
        # the new fact and can complete the project.
        return "failed"
    if total_ms is None:
        LOG.info(
            "bootstrap completed project=%s intent=%s worker=%s source=%s from=%s phase_ms=%s",
            project_id,
            intent_id,
            worker_name,
            source,
            [conclude.fact_id],
            phase_ms,
        )
    else:
        LOG.info(
            "bootstrap completed project=%s intent=%s worker=%s source=%s from=%s phase_ms=%s total_ms=%s",
            project_id,
            intent_id,
            worker_name,
            source,
            [conclude.fact_id],
            phase_ms,
            total_ms,
        )
    return "success"
