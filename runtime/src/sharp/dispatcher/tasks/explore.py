from __future__ import annotations

import logging
import time

from sharp.dispatcher.config import DispatchConfig, WorkerConfig
from sharp.dispatcher.contracts import parse_json_output, validate_explore_payload
from sharp.dispatcher.prompting import format_env_baseline, load_prompt, render_prompt, scoring_note
from sharp.dispatcher.scheduler.provider_health import provider_error_status
from sharp.dispatcher.protocol.client import SharpClient
from sharp.dispatcher.runtime.cancellation import TaskCancellation
from sharp.dispatcher.runtime.containers import ContainerManager
from sharp.dispatcher.runtime.heartbeat import HeartbeatLease
from sharp.dispatcher.tasks.common import (
    ApkInjectionError,
    ConcludeWriteResult,
    best_effort_release,
    cancel_reason,
    cleanup_graph_snapshots,
    did_timeout,
    ensure_apk_injected,
    inject_mcp_flags,
    log_payload_keys,
    preflight_mcp,
    project_allows_conclude_fallback,
    preview,
    run_healthcheck,
    run_worker_process,
    summarize_graph_yaml,
    write_conclude_result_with_fact_id,
    write_graph_snapshot,
)
from sharp.dispatcher.workers.registry import get_driver
from sharp.server.models import Intent, ProjectDetail

LOG = logging.getLogger(__name__)


def run_explore_task(
    config: DispatchConfig,
    client: SharpClient,
    container_manager: ContainerManager,
    project: ProjectDetail,
    export_yaml: str,
    intent: Intent,
    worker: WorkerConfig,
    cancellation: TaskCancellation,
) -> str:
    driver = get_driver(worker.type)
    task_started = time.perf_counter()
    healthcheck_timeout = config.runtime.healthcheck_timeout
    lease = HeartbeatLease.for_intent(client, project.project.id, intent.id, worker.name, config.runtime.interval)
    lease.start()
    snapshot_dirs: list[str] = []
    try:
        container_name = container_manager.ensure_running(project.project.id)

        try:
            ensure_apk_injected(container_manager, container_name, project)
        except ApkInjectionError as exc:
            LOG.warning(
                "explore apk injection failed project=%s intent=%s worker=%s reason=%s",
                project.project.id,
                intent.id,
                worker.name,
                exc,
            )
            client.conclude(
                project.project.id,
                intent.id,
                worker.name,
                f"APK 注入容器失败，无法进行反编译分析：{exc}。请确认 server 侧 APK 源文件仍存在且不超过 500MB。",
            )
            return "failed"

        LOG.info(
            "starting container exec project=%s intent=%s worker=%s phase=explore_healthcheck timeout=%ss",
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
                "explore cancelled during healthcheck project=%s intent=%s worker=%s reason=%s",
                project.project.id,
                intent.id,
                worker.name,
                cancelled,
            )
            best_effort_release(client, project.project.id, intent.id, worker.name)
            return "cancelled"
        if lease.failure is not None:
            LOG.warning(
                "heartbeat lost during explore healthcheck project=%s intent=%s worker=%s status=%s",
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

        snapshot = write_graph_snapshot(
            container_manager,
            container_name,
            summarize_graph_yaml(export_yaml.strip()),
            phase="explore_execute",
        )
        snapshot_dirs.append(snapshot.directory)
        prompt = render_prompt(
            load_prompt(config.runtime.prompt_group, "explore.md"),
            {
                "graph_yaml": snapshot.reference,
                "intent_id": intent.id,
                "intent_description": intent.description,
                "scoring_note": scoring_note(
                    getattr(getattr(project, "project", None), "task_mode", "pentest")
                ),
                "env_baseline": format_env_baseline(client.fetch_env_baseline(project.project.id)),
                "asset_ledger": _asset_ledger_block(client, project.project.id),
            },
        )

        session = driver.prepare_session()
        execute = driver.build_execute(worker, prompt, session)
        session = execute.session
        mcp_path = preflight_mcp(config, container_manager, container_name)
        execute_argv = inject_mcp_flags(execute.argv, mcp_path)
        execute_started = time.perf_counter()
        first = _run_process(
            container_manager,
            container_name,
            worker,
            execute_argv,
            phase="explore_execute",
            timeout=config.tasks.explore.timeout,
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
                "explore cancelled project=%s intent=%s worker=%s reason=%s execute_ms=%s",
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
                "heartbeat lost during explore project=%s intent=%s worker=%s status=%s execute_ms=%s",
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
                log_payload_keys(LOG, "explore", payload)
                kind, description, findings = validate_explore_payload(payload)
            except Exception as exc:
                LOG.warning(
                    "explore parse failed project=%s intent=%s worker=%s error=%s execute_ms=%s total_ms=%s stdout_preview=%s stderr_preview=%s",
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
                    project.project.id,
                    intent,
                    export_yaml,
                    session,
                    lease,
                    cancellation,
                    snapshot_dirs,
                )
            if kind == "rejected":
                LOG.warning(
                    "explore rejected project=%s intent=%s worker=%s execute_ms=%s total_ms=%s stdout_preview=%s",
                    project.project.id,
                    intent.id,
                    worker.name,
                    execute_ms,
                    int((time.perf_counter() - task_started) * 1000),
                    preview(first.stdout),
                )
                best_effort_release(client, project.project.id, intent.id, worker.name)
                return "rejected"
            result = write_conclude_result_with_fact_id(
                client,
                project.project.id,
                intent.id,
                worker.name,
                description,
                source="explore_execute",
                phase_ms=execute_ms,
                total_ms=int((time.perf_counter() - task_started) * 1000),
            )
            _best_effort_create_vulns(
                client, project.project.id, result.fact_id, intent.id,
                worker.name, findings,
            )
            _best_effort_extract_knowledge(
                client, project.project.id, description,
            )
            _best_effort_upsert_baseline(client, project.project.id, worker.name, payload)
            _best_effort_set_product(client, project.project.id, worker.name, payload)
            _best_effort_assess_endpoints(client, project.project.id, worker.name, payload)
            return result.status
        if did_timeout(first):
            LOG.warning(
                "explore timed out project=%s intent=%s worker=%s execute_ms=%s total_ms=%s stdout_preview=%s stderr_preview=%s",
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
                project.project.id,
                intent,
                export_yaml,
                session,
                lease,
                cancellation,
                snapshot_dirs,
            )
        LOG.warning(
            "explore command failed project=%s intent=%s worker=%s code=%s execute_ms=%s total_ms=%s stdout_preview=%s stderr_preview=%s",
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
        # Provider 级故障返回可区分状态（调度器据此长冷却该 worker 并换 provider）
        provider_failure = provider_error_status(first.stderr)
        if provider_failure:
            LOG.warning(
                "explore provider failure project=%s worker=%s status=%s stderr_preview=%s",
                project.project.id, worker.name, provider_failure, preview(first.stderr),
            )
            return provider_failure
        return "failed"
    except Exception:
        LOG.exception("explore task crashed project=%s intent=%s worker=%s", project.project.id, intent.id, worker.name)
        best_effort_release(client, project.project.id, intent.id, worker.name)
        return "failed"
    finally:
        lease.stop()
        cleanup_graph_snapshots(container_manager, container_manager.container_name(project.project.id), snapshot_dirs)


def _try_conclude_fallback(
    config: DispatchConfig,
    client: SharpClient,
    container_manager: ContainerManager,
    container_name: str,
    worker: WorkerConfig,
    driver,
    project_id: str,
    intent: Intent,
    export_yaml: str,
    session: str | None,
    lease: HeartbeatLease,
    cancellation: TaskCancellation,
    snapshot_dirs: list[str],
) -> str:
    if not driver.supports_conclude() or not session:
        LOG.info(
            "conclude fallback unavailable project=%s intent=%s worker=%s supports_conclude=%s has_session=%s",
            project_id,
            intent.id,
            worker.name,
            driver.supports_conclude(),
            bool(session),
        )
        best_effort_release(client, project_id, intent.id, worker.name)
        return "failed"
    if lease.failure is not None:
        LOG.warning("conclude fallback skipped because heartbeat already lost project=%s intent=%s worker=%s", project_id, intent.id, worker.name)
        best_effort_release(client, project_id, intent.id, worker.name)
        return "failed"
    if cancellation.is_cancelled:
        LOG.info(
            "conclude fallback skipped because task was cancelled project=%s intent=%s worker=%s reason=%s",
            project_id,
            intent.id,
            worker.name,
            cancellation.reason,
        )
        best_effort_release(client, project_id, intent.id, worker.name)
        return "cancelled"

    if not project_allows_conclude_fallback(
        client,
        project_id,
        worker_name=worker.name,
        intent_id=intent.id,
    ):
        best_effort_release(client, project_id, intent.id, worker.name)
        return "failed"

    container_name = container_manager.ensure_running(project_id)

    snapshot = write_graph_snapshot(
        container_manager,
        container_name,
        summarize_graph_yaml(export_yaml.strip()),
        phase="explore_conclude",
    )
    snapshot_dirs.append(snapshot.directory)
    prompt = render_prompt(
        load_prompt(config.runtime.prompt_group, "explore_conclude.md"),
        {
            "graph_yaml": snapshot.reference,
            "intent_id": intent.id,
            "intent_description": intent.description,
        },
    )
    conclude_argv = driver.build_conclude(worker, prompt, session)
    mcp_path = preflight_mcp(config, container_manager, container_name)
    conclude_argv = inject_mcp_flags(conclude_argv, mcp_path)
    LOG.info("starting conclude fallback project=%s intent=%s worker=%s", project_id, intent.id, worker.name)
    conclude_started = time.perf_counter()
    result = _run_process(
        container_manager,
        container_name,
        worker,
        conclude_argv,
        phase="explore_conclude",
        timeout=config.tasks.explore.conclude_timeout,
        lease=lease,
        cancellation=cancellation,
    )
    conclude_ms = int((time.perf_counter() - conclude_started) * 1000)
    cancelled = cancel_reason(result, cancellation)
    if cancelled is not None:
        LOG.info(
            "conclude cancelled project=%s intent=%s worker=%s reason=%s conclude_ms=%s",
            project_id,
            intent.id,
            worker.name,
            cancelled,
            conclude_ms,
        )
        best_effort_release(client, project_id, intent.id, worker.name)
        return "cancelled"
    if lease.failure is not None:
        best_effort_release(client, project_id, intent.id, worker.name)
        return "failed"
    if result.timed_out or result.returncode != 0:
        LOG.warning(
            "conclude failed project=%s intent=%s worker=%s code=%s timed_out=%s conclude_ms=%s stdout_preview=%s stderr_preview=%s",
            project_id,
            intent.id,
            worker.name,
            result.returncode,
            result.timed_out,
            conclude_ms,
            preview(result.stdout),
            preview(result.stderr),
        )
        best_effort_release(client, project_id, intent.id, worker.name)
        return "failed"
    try:
        model_output = driver.extract_response_text(result.stdout, result.stderr)
        payload = parse_json_output(model_output)
        kind, description, findings = validate_explore_payload(payload)
    except Exception as exc:
        LOG.warning(
            "conclude parse failed project=%s intent=%s worker=%s error=%s conclude_ms=%s stdout_preview=%s stderr_preview=%s",
            project_id,
            intent.id,
            worker.name,
            exc,
            conclude_ms,
            preview(result.stdout),
            preview(result.stderr),
        )
        best_effort_release(client, project_id, intent.id, worker.name)
        return "failed"
    if kind == "rejected":
        LOG.warning(
            "conclude rejected project=%s intent=%s worker=%s conclude_ms=%s stdout_preview=%s",
            project_id,
            intent.id,
            worker.name,
            conclude_ms,
            preview(result.stdout),
        )
        best_effort_release(client, project_id, intent.id, worker.name)
        return "rejected"
    cw_result = write_conclude_result_with_fact_id(
        client,
        project_id,
        intent.id,
        worker.name,
        description,
        source="explore_conclude",
        phase_ms=conclude_ms,
    )
    _best_effort_create_vulns(
        client, project_id, cw_result.fact_id, intent.id,
        worker.name, findings,
    )
    _best_effort_extract_knowledge(
        client, project_id, description,
    )
    return cw_result.status


def _extract_env_facts(payload: dict) -> list[dict]:
    """从行动结论里取"环境前提"（P0-2 修正版）。

    worker 无法直连 Sharp API（容器内没有凭据），所以环境事实改为随结论上报，
    由 dispatcher 代写基线。兼容两种写法：`[{"key","value","note"}]` 与 `{"key": "value"}`。
    """
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    if not isinstance(data, dict):
        return []
    raw = data.get("env_facts")
    if raw is None:
        return []
    if isinstance(raw, dict):
        return [{"key": str(k).strip(), "value": str(v), "note": ""}
                for k, v in raw.items() if str(k).strip()][:20]
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for item in raw:
        if isinstance(item, dict) and str(item.get("key") or "").strip():
            out.append({
                "key": str(item["key"]).strip(),
                "value": str(item.get("value") or ""),
                "note": str(item.get("note") or ""),
            })
        elif isinstance(item, str) and item.strip():
            out.append({"key": item.strip(), "value": "", "note": ""})
    return out[:20]


def _best_effort_upsert_baseline(
    client: SharpClient,
    project_id: str,
    worker_name: str,
    payload: dict,
    *,
    phase: str = "explore",
) -> None:
    """把结论里的环境前提写入基线（best-effort，失败不影响任务状态）。"""
    entries = _extract_env_facts(payload)
    if not entries:
        return
    try:
        resp = client.upsert_env_baseline(project_id, entries)
        if resp.ok:
            # 带上 phase：本助手定义在 explore.py，logger 名也跟着是 tasks.explore，
            # 若不带调用方标识，bootstrap 的 conclude 写入会被日志显示成 explore 写的
            # （实测把排查方向带偏过一次）。
            LOG.info("env baseline updated project=%s worker=%s phase=%s entries=%s",
                     project_id, worker_name, phase, len(entries))
        else:
            LOG.warning("env baseline update failed project=%s status=%s",
                        project_id, resp.status_code)
    except Exception as exc:
        LOG.warning("env baseline update exception project=%s error=%s", project_id, exc)


def _extract_product(payload: dict) -> str | None:
    """从结论里取"目标是什么产品"的自述（P0-C 的产品维度）。

    接受 `product` / `target_product` 两种写法，取第一个非空（最多 64 字符）。
    为什么让 worker 说而不是服务端猜：产品名是要参与**相等匹配**的键，
    猜错的代价是"跨产品复用静默失效"，而 worker 在指纹阶段本来就看得见它。
    """
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    if not isinstance(data, dict):
        return None
    for key in ("product", "target_product"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:64]
    return None


def _best_effort_set_product(
    client: SharpClient,
    project_id: str,
    worker_name: str,
    payload: dict,
    *,
    phase: str = "explore",
) -> None:
    """把 worker 自述的产品标注写到项目上（best-effort）。

    **不覆盖已有非空值**由服务端把关（`force=False`）：产品名决定跨产品匹配走向，
    不该被某一轮猜测改写，否则匹配会随轮次漂移。这里因此不需要先读后写。
    """
    product = _extract_product(payload)
    if not product:
        return
    try:
        resp = client.set_project_product(project_id, product)
        if resp.ok:
            LOG.info("product labelled project=%s worker=%s phase=%s product=%s",
                     project_id, worker_name, phase, product)
        else:
            LOG.warning("product label failed project=%s status=%s", project_id, resp.status_code)
    except Exception as exc:
        LOG.warning("product label exception project=%s error=%s", project_id, exc)


_ASSESSABLE = ("verified", "dismissed")


def _asset_ledger_block(client: SharpClient, project_id: str) -> str:
    """接口台账（P1-B）：哪些已发现待评估、哪些测过了、哪些已排除。

    为什么每轮都拉而不是只在建项时给一次（HINT 已经做过）：跑动中新登记/新评估的接口
    对当前项目才有用 —— "我还没测过哪些"是随任务推进而变的。
    """
    data = client.fetch_asset_ledger(project_id)
    if not data:
        return "（接口台账不可用；如本轮测过某些接口，照常按 `endpoint_tests` 上报。）"
    counts = data.get("counts") or {}
    if not counts.get("total"):
        return ("（本项目目标的接口台账还是空的。你每确认一个接口，结论里的完整 URL 会自动登记；"
                "测过的接口请在 `endpoint_tests` 里给出评估。）")
    block = data.get("block") or ""
    return block or "（台账为空）"


def _extract_endpoint_assessments(payload: dict) -> list[dict]:
    """从结论里取"哪些接口测过了/排除了"（P1-B）。

    接受 `endpoint_tests`（也认 `endpoint_assessments`），每项形如
    `{"method": "GET", "path": "/api/v1/users", "status": "verified", "note": "..."}`。
    只收 `verified` / `dismissed`：`discovered` 是创建时的初始态，不让 worker 把已评估的
    条目退回未评估（那会让覆盖报告的盲区清单重新变脏）。上限 50 条。
    """
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    if not isinstance(data, dict):
        return []
    raw = data.get("endpoint_tests")
    if raw is None:
        raw = data.get("endpoint_assessments")
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "").strip()
        status = str(item.get("status") or "").strip().lower()
        if not path or status not in _ASSESSABLE:
            continue
        out.append({
            "method": str(item.get("method") or "").strip().upper(),
            "path": path[:400],
            "status": status,
            "note": str(item.get("note") or "").strip()[:400],
        })
        if len(out) >= 50:
            break
    return out


def _best_effort_assess_endpoints(
    client: SharpClient,
    project_id: str,
    worker_name: str,
    payload: dict,
    *,
    phase: str = "explore",
) -> None:
    """把接口评估写回台账（best-effort，失败不影响任务状态）。"""
    items = _extract_endpoint_assessments(payload)
    if not items:
        return
    try:
        resp = client.assess_endpoints(project_id, items, worker_name)
        if resp.ok:
            data = resp.data if isinstance(resp.data, dict) else {}
            LOG.info(
                "endpoint ledger assessed project=%s worker=%s phase=%s items=%s updated=%s created=%s",
                project_id, worker_name, phase, len(items),
                data.get("updated"), data.get("created"),
            )
        else:
            LOG.warning("endpoint ledger assess failed project=%s status=%s",
                        project_id, resp.status_code)
    except Exception as exc:
        LOG.warning("endpoint ledger assess exception project=%s error=%s", project_id, exc)


def _best_effort_create_vulns(
    client: SharpClient,
    project_id: str,
    fact_id: str | None,
    intent_id: str,
    worker_name: str,
    findings: list[dict] | None,
) -> None:
    """After a fact is written, create vulnerability records for any structured
    findings the worker reported.  Best-effort: failures are logged but never
    affect the task status."""
    if not findings or not fact_id:
        return
    for f in findings:
        try:
            resp = client.create_vulnerability(
                project_id,
                fact_id,
                f.get("title", ""),
                intent_id=intent_id,
                severity=f.get("severity", "info"),
                url=f.get("url", ""),
                description=f.get("description", ""),
                evidence=f.get("evidence", ""),
                reproduction=f.get("reproduction", ""),
                impact=f.get("impact", ""),
                recommendation=f.get("recommendation", ""),
                kind=str(f.get("kind", "vuln") or "vuln"),
                score=int(f.get("score", 0) or 0),
            )
            if not resp.ok:
                LOG.warning(
                    "vuln create failed project=%s intent=%s worker=%s title=%s status=%s",
                    project_id, intent_id, worker_name, f.get("title", ""), resp.status_code,
                )
            else:
                LOG.info(
                    "vuln created project=%s intent=%s worker=%s fact=%s title=%s severity=%s",
                    project_id, intent_id, worker_name, fact_id,
                    f.get("title", ""), f.get("severity", "info"),
                )
        except Exception as exc:
            LOG.warning(
                "vuln create exception project=%s intent=%s worker=%s title=%s error=%s",
                project_id, intent_id, worker_name, f.get("title", ""), exc,
            )


def _best_effort_extract_knowledge(
    client: SharpClient,
    project_id: str,
    fact_description: str,
) -> None:
    """After a fact is written, attempt to extract cross-project knowledge
    (credentials, endpoints, fingerprints) from the fact description and store
    it in the global knowledge_base.  Best-effort: failures are logged but
    never affect the task status."""
    try:
        resp = client.extract_knowledge(project_id, fact_description)
        if resp.ok:
            data = resp.data
            count = 0
            if isinstance(data, dict):
                count = data.get("extracted_count", 0)
            if count:
                LOG.info(
                    "knowledge extracted project=%s entries=%d description_preview=%s",
                    project_id, count, fact_description[:120],
                )
    except Exception as exc:
        LOG.warning(
            "knowledge extract exception project=%s error=%s",
            project_id, exc,
        )


def _run_process(
    container_manager: ContainerManager,
    container_name: str,
    worker: WorkerConfig,
    argv: list[str],
    *,
    phase: str,
    timeout: int,
    lease: HeartbeatLease,
    cancellation: TaskCancellation,
    stdout_callback=None,
):
    return run_worker_process(
        container_manager,
        container_name,
        worker,
        argv,
        phase=phase,
        timeout_seconds=timeout,
        lease=lease,
        cancellation=cancellation,
        stdout_callback=stdout_callback,
    )
