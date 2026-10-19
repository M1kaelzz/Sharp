from __future__ import annotations

import json

import logging
import time
import uuid
from dataclasses import dataclass
from urllib.parse import urlparse

from sharp.dispatcher.config import DispatchConfig, MCPServerConfig, WorkerConfig
from sharp.dispatcher.protocol.client import SharpClient
from sharp.dispatcher.runtime.cancellation import TaskCancellation
from sharp.dispatcher.runtime.containers import ContainerManager
from sharp.dispatcher.runtime.heartbeat import HeartbeatLease
from sharp.dispatcher.runtime.process import ProcessResult
from sharp.server.models import ProjectDetail
from sharp.server.reports import is_engineered_report_intent_description

HEALTHCHECK_COMMUNICATE_GRACE_SECONDS = 10
PROCESS_COMMUNICATE_GRACE_SECONDS = 15
LOG_PREVIEW_LIMIT = 1200
GRAPH_SNAPSHOT_ROOT = "/tmp/sharp-prompts"
# Summarization thresholds for incremental graph planning (P2-2).
# When non-special facts exceed FACT_THRESHOLD, older facts beyond
# KEEP_RECENT have their descriptions truncated to TRUNCATE_CHARS.
GRAPH_SUMMARY_FACT_THRESHOLD = 20
GRAPH_SUMMARY_KEEP_RECENT = 8
GRAPH_SUMMARY_TRUNCATE_CHARS = 300
# Where an APK/XAPK target is injected inside the worker container so the agent
MAX_INJECT_APK_BYTES = 500 * 1024 * 1024
LOG = logging.getLogger(__name__)


def summarize_graph_yaml(
    graph_yaml: str,
    *,
    fact_threshold: int = GRAPH_SUMMARY_FACT_THRESHOLD,
    keep_recent: int = GRAPH_SUMMARY_KEEP_RECENT,
    truncate_chars: int = GRAPH_SUMMARY_TRUNCATE_CHARS,
) -> str:
    """Compress old fact descriptions in the graph YAML to reduce token consumption.

    - origin and goal facts are always kept at full length
    - If total non-special facts exceed *fact_threshold*, the oldest facts
      (all but the most recent *keep_recent*) have their descriptions truncated
      to *truncate_chars* with a ``[truncated]`` marker appended.
    - If the YAML cannot be parsed or the fact count is below the threshold,
      the original string is returned unchanged.
    """
    import yaml

    try:
        data = yaml.safe_load(graph_yaml)
    except yaml.YAMLError:
        return graph_yaml

    if not isinstance(data, dict) or not isinstance(data.get("facts"), list):
        return graph_yaml

    facts: list = data["facts"]
    special_ids = {"origin", "goal"}
    regular_facts = [f for f in facts if isinstance(f, dict) and f.get("id") not in special_ids]

    if len(regular_facts) <= max(fact_threshold, keep_recent):
        return graph_yaml

    # Truncate older facts (those before the most recent *keep_recent*).
    cutoff = len(regular_facts) - keep_recent
    regular_index = 0
    for f in facts:
        if not isinstance(f, dict) or f.get("id") in special_ids:
            continue
        if regular_index < cutoff:
            desc = f.get("description", "")
            if isinstance(desc, str) and len(desc) > truncate_chars:
                f["description"] = desc[:truncate_chars].rstrip() + " [truncated]"
        regular_index += 1

    return yaml.dump(data, allow_unicode=True, default_flow_style=False, sort_keys=False)


def summarize_graph_yaml_by_distance(
    graph_yaml: str,
    *,
    focus_ids: list[str] | tuple[str, ...] = ("goal",),
    keep_hops: int = GRAPH_SUMMARY_KEEP_RECENT,
    truncate_chars: int = GRAPH_SUMMARY_TRUNCATE_CHARS,
) -> str:
    """Compress fact descriptions by their graph distance from the focus node.

    Unlike the time-based :func:`summarize_graph_yaml` (which keeps the most
    recent facts), this compresses by structural distance in the evidence graph:
    facts close to the *focus* node (the node the agent is actively working on)
    are kept at full length, while facts far from it are truncated. The intent
    is to preserve the evidence chain the current round depends on, not merely
    the newest facts.

    - ``origin`` / ``goal`` facts are always kept at full length.
    - ``focus_ids`` names the node(s) the distance is measured from. For a
      reason round this is the project ``goal``; for an explore round it is the
      current intent's ``from`` fact ids (the evidence it is built on).
    - Distance is measured by walking intents backwards: a fact reachable by one
      intent edge from a focus node is distance 1, etc. Facts at distance
      ``<= keep_hops`` are kept full length; farther facts are truncated to
      ``truncate_chars`` with a ``[truncated]`` marker.
    - Facts with no path to the focus node (or whose distance exceeds
      ``keep_hops``) are truncated.
    - If the YAML cannot be parsed, the original string is returned unchanged.

    The fallback output for an unparseable/unrecognizable graph is identical to
    :func:`summarize_graph_yaml` (return the input unchanged), so the feature
    degrades gracefully when the graph shape is unexpected.
    """
    import yaml

    try:
        data = yaml.safe_load(graph_yaml)
    except yaml.YAMLError:
        return graph_yaml

    if not isinstance(data, dict):
        return graph_yaml

    facts: list = data.get("facts")
    intents: list = data.get("intents")
    if not isinstance(facts, list) or not isinstance(intents, list):
        return graph_yaml

    # Build the reverse graph: produced_fact -> [source facts via an intent].
    # An intent maps its "from" facts to its "to" fact, so walking an intent
    # backwards (to -> from) moves one hop away from the produced fact.
    by_target: dict[str, list[str]] = {}
    for intent in intents:
        if not isinstance(intent, dict):
            continue
        to_fact = intent.get("to")
        from_facts = intent.get("from")
        if not isinstance(to_fact, str) or not isinstance(from_facts, list):
            continue
        by_target.setdefault(to_fact, []).extend(
            s for s in from_facts if isinstance(s, str)
        )

    focus = set(f for f in focus_ids if isinstance(f, str) and f)
    if not focus:
        return graph_yaml

    # BFS distances from the focus nodes over the reverse graph.
    distances: dict[str, int] = {f: 0 for f in focus}
    frontier = list(focus)
    current_dist = 0
    seen = set(focus)
    while frontier:
        current_dist += 1
        next_frontier: list[str] = []
        for node in frontier:
            for source in by_target.get(node, []):
                if source in seen:
                    continue
                seen.add(source)
                distances[source] = current_dist
                next_frontier.append(source)
        frontier = next_frontier

    special_ids = {"origin", "goal"}
    for f in facts:
        if not isinstance(f, dict) or f.get("id") in special_ids:
            continue
        fid = f.get("id")
        dist = distances.get(fid, float("inf"))
        if dist > keep_hops:
            desc = f.get("description", "")
            if isinstance(desc, str) and len(desc) > truncate_chars:
                f["description"] = desc[:truncate_chars].rstrip() + " [truncated]"

    return yaml.dump(data, allow_unicode=True, default_flow_style=False, sort_keys=False)


class ApkInjectionError(RuntimeError):
    """Raised when a project's APK target cannot be placed into the container.
    The task layer turns this into a task failure with a written reason, so the
    project does not spin forever on an impossible 'decompile' goal."""


def ensure_apk_injected(
    container_manager: ContainerManager,
    container_name: str,
    project: "ProjectDetail",
) -> str | None:
    """If the project carries an apk_source fact, make sure the APK binary is
    present in the container and return its in-container path. Returns None for
    non-APK projects (no-op, so Web/miniprogram projects are unaffected).

    Idempotent: skips the copy if the file is already in the container. Raises
    ApkInjectionError on missing source file, size-limit breach, or docker
    failure — the caller must fail the task and record the reason.
    """
    # Imported here to avoid a module-level dispatcher->server import cycle.
    from sharp.server.android import (
        APK_SOURCE_FACT_ID,
        container_apk_path,
        parse_apk_source_fact,
    )

    fact = next(
        (f for f in project.facts if f.id == APK_SOURCE_FACT_ID),
        None,
    )
    if fact is None:
        return None

    info = parse_apk_source_fact(fact.description)
    if info is None:
        raise ApkInjectionError(
            "APK 源信息(apk_source fact)无法解析，无法把安装包注入容器"
        )

    source_path = info.get("host_path") or ""
    filename = info.get("filename") or "target.apk"
    # Use the shared path helper so the in-container path matches exactly what
    # the seed origin/goal told the agent (no drift between the two).
    target_path = container_apk_path(filename)

    if container_manager.path_exists_in_container(container_name, target_path):
        return target_path

    try:
        copied = container_manager.write_binary_file(
            container_name, target_path, source_path, max_bytes=MAX_INJECT_APK_BYTES
        )
    except FileNotFoundError as exc:
        raise ApkInjectionError(
            f"APK 源文件已不存在，无法注入容器：{source_path}"
        ) from exc
    except ValueError as exc:
        raise ApkInjectionError(
            f"APK 超过 {MAX_INJECT_APK_BYTES // (1024 * 1024)}MB 注入上限，已拒绝：{exc}"
        ) from exc
    except RuntimeError as exc:
        raise ApkInjectionError(f"注入 APK 到容器失败：{exc}") from exc

    LOG.info(
        "injected apk into container project=%s container=%s path=%s bytes=%s",
        project.project.id,
        container_name,
        target_path,
        copied,
    )
    return target_path


@dataclass(slots=True)
class HealthcheckRun:
    result: ProcessResult
    duration_ms: int


@dataclass(slots=True)
class ConcludeWriteResult:
    status: str
    fact_id: str | None = None


def preview(text: str, limit: int = LOG_PREVIEW_LIMIT) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[:limit] + "..."


def log_payload_keys(logger, phase: str, payload: dict) -> None:
    """记录 worker 回包的**键名**（DEBUG 级）。

    为什么专门记这个：本轮排查"假设/env_facts 机制为何没触发"时发现，容器跑完就被回收，
    原始 JSON 无处可取 —— 只能靠猜。有了这行，`--log-level DEBUG` 下就能直接看到
    worker 到底输出了哪些字段（例如 `keys=['intents']` 说明它压根没输出 `hypotheses`），
    从而区分"提示词没让模型输出"与"代码没接住"这两类完全不同的故障。
    """
    try:
        if isinstance(payload, dict):
            data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
            keys = sorted(data.keys()) if isinstance(data, dict) else []
            logger.debug("%s payload keys=%s preview=%s", phase, keys,
                         preview(json.dumps(data, ensure_ascii=False, default=str)))
        else:
            logger.debug("%s payload type=%s", phase, type(payload).__name__)
    except Exception:
        logger.debug("%s payload keys=<unserializable>", phase)


def did_timeout(result: ProcessResult) -> bool:
    return not result.cancelled and (result.timed_out or result.returncode in (124, 137))


def cancel_reason(result: ProcessResult, cancellation: TaskCancellation | None = None) -> str | None:
    if result.cancelled:
        return result.cancel_reason or "cancelled"
    if cancellation is not None:
        return cancellation.reason
    return None


def communicate_timeout(timeout_seconds: int, grace_seconds: int = PROCESS_COMMUNICATE_GRACE_SECONDS) -> int:
    return timeout_seconds + grace_seconds


@dataclass(slots=True)
class GraphSnapshot:
    reference: str
    directory: str


def write_graph_snapshot(
    container_manager: ContainerManager,
    container_name: str,
    graph_yaml: str,
    *,
    phase: str,
) -> GraphSnapshot:
    directory = f"{GRAPH_SNAPSHOT_ROOT}/{phase}-{uuid.uuid4().hex[:12]}"
    path = f"{directory}/graph.yaml"
    container_manager.write_text_file(container_name, path, graph_yaml)
    reference = (
        "The graph YAML snapshot is stored in this file inside the current container:\n\n"
        f"{path}\n\n"
        "Before using the graph, read the entire file and treat its contents as the YAML snapshot "
        "for this Graph section."
    )
    return GraphSnapshot(reference=reference, directory=directory)


def write_graph_snapshot_reference(
    container_manager: ContainerManager,
    container_name: str,
    graph_yaml: str,
    *,
    phase: str,
) -> str:
    return write_graph_snapshot(container_manager, container_name, graph_yaml, phase=phase).reference


def cleanup_graph_snapshots(
    container_manager: ContainerManager,
    container_name: str,
    directories: list[str],
) -> None:
    for directory in directories:
        container_manager.remove_path(container_name, directory, require_prefix=GRAPH_SNAPSHOT_ROOT)


def run_healthcheck(
    container_manager: ContainerManager,
    container_name: str,
    worker: WorkerConfig,
    command: list[str],
    *,
    timeout_seconds: int,
    lease: HeartbeatLease | None = None,
    cancellation: TaskCancellation | None = None,
) -> HealthcheckRun:
    process = container_manager.build_exec_process(
        container_name,
        dict(worker.env),
        command,
        timeout_seconds=timeout_seconds,
    )
    process.start()
    if lease is not None:
        lease.attach_process(process)
    if cancellation is not None:
        cancellation.attach_process(process)
    started = time.perf_counter()
    try:
        result = process.communicate(timeout=communicate_timeout(timeout_seconds, HEALTHCHECK_COMMUNICATE_GRACE_SECONDS))
    finally:
        if lease is not None:
            lease.attach_process(None)
        if cancellation is not None:
            cancellation.attach_process(None)
    duration_ms = int((time.perf_counter() - started) * 1000)
    return HealthcheckRun(result=result, duration_ms=duration_ms)


def run_worker_process(
    container_manager: ContainerManager,
    container_name: str,
    worker: WorkerConfig,
    argv: list[str],
    *,
    phase: str,
    timeout_seconds: int,
    lease: HeartbeatLease | None = None,
    cancellation: TaskCancellation | None = None,
    stdout_callback=None,
) -> ProcessResult:
    LOG.info(
        "starting container exec container=%s worker=%s phase=%s timeout=%ss",
        container_name,
        worker.name,
        phase,
        timeout_seconds,
    )
    process = container_manager.build_exec_process(
        container_name,
        dict(worker.env),
        argv,
        timeout_seconds=timeout_seconds,
        stdout_callback=stdout_callback,
    )
    process.start()
    if lease is not None:
        lease.attach_process(process)
    if cancellation is not None:
        cancellation.attach_process(process)
    try:
        return process.communicate(timeout=communicate_timeout(timeout_seconds))
    finally:
        if lease is not None:
            lease.attach_process(None)
        if cancellation is not None:
            cancellation.attach_process(None)


def project_allows_conclude_fallback(client: SharpClient, project_id: str, *, worker_name: str, intent_id: str) -> bool:
    project = client.get_project(project_id)
    if project.project.status == "active":
        return True
    if project.project.status == "completed":
        for intent in project.intents:
            if intent.id == intent_id and is_engineered_report_intent_description(intent.description):
                return True
    LOG.info(
        "skip conclude fallback because project is no longer active project=%s intent=%s worker=%s status=%s",
        project_id,
        intent_id,
        worker_name,
        project.project.status,
    )
    return False


def best_effort_release_reason(client: SharpClient, project_id: str, worker_name: str) -> None:
    response = client.release_reason(project_id, worker_name)
    if not response.ok and response.status_code not in (403, 409):
        LOG.warning(
            "reason release failed project=%s worker=%s status=%s",
            project_id,
            worker_name,
            response.status_code,
        )
    elif response.ok:
        LOG.info("released reason project=%s worker=%s", project_id, worker_name)
    else:
        LOG.info(
            "reason release skipped project=%s worker=%s status=%s",
            project_id,
            worker_name,
            response.status_code,
        )


def write_conclude_result(
    client: SharpClient,
    project_id: str,
    intent_id: str,
    worker_name: str,
    description: str,
    *,
    source: str,
    phase_ms: int,
    total_ms: int | None = None,
) -> str:
    return write_conclude_result_with_fact_id(
        client,
        project_id,
        intent_id,
        worker_name,
        description,
        source=source,
        phase_ms=phase_ms,
        total_ms=total_ms,
    ).status


def write_conclude_result_with_fact_id(
    client: SharpClient,
    project_id: str,
    intent_id: str,
    worker_name: str,
    description: str,
    *,
    source: str,
    phase_ms: int,
    total_ms: int | None = None,
) -> ConcludeWriteResult:
    response = client.conclude(project_id, intent_id, worker_name, description)
    if response.ok:
        fact_id: str | None = None
        if isinstance(response.data, dict):
            fact = response.data.get("fact")
            if isinstance(fact, dict):
                candidate = fact.get("id")
                if isinstance(candidate, str) and candidate:
                    fact_id = candidate
        if total_ms is None:
            LOG.info(
                "intent concluded project=%s intent=%s worker=%s source=%s phase_ms=%s",
                project_id,
                intent_id,
                worker_name,
                source,
                phase_ms,
            )
        else:
            LOG.info(
                "intent concluded project=%s intent=%s worker=%s source=%s phase_ms=%s total_ms=%s",
                project_id,
                intent_id,
                worker_name,
                source,
                phase_ms,
                total_ms,
            )
        return ConcludeWriteResult(status="success", fact_id=fact_id)
    if response.status_code == 403:
        LOG.info(
            "project became inactive during conclude project=%s intent=%s worker=%s",
            project_id,
            intent_id,
            worker_name,
        )
    else:
        LOG.warning(
            "conclude write failed project=%s intent=%s worker=%s status=%s body=%s",
            project_id,
            intent_id,
            worker_name,
            response.status_code,
            response.text,
        )
    best_effort_release(client, project_id, intent_id, worker_name)
    return ConcludeWriteResult(status="failed", fact_id=None)


def best_effort_release(client: SharpClient, project_id: str, intent_id: str, worker_name: str) -> None:
    response = client.release(project_id, intent_id, worker_name)
    if not response.ok and response.status_code not in (403, 409):
        LOG.warning(
            "release failed project=%s intent=%s worker=%s status=%s",
            project_id,
            intent_id,
            worker_name,
            response.status_code,
        )
    elif response.ok:
        LOG.info("released intent project=%s intent=%s worker=%s", project_id, intent_id, worker_name)
    else:
        LOG.info(
            "release skipped project=%s intent=%s worker=%s status=%s",
            project_id,
            intent_id,
            worker_name,
            response.status_code,
        )


def unpack_live_output(text: str) -> str:
    """Live-output unwrap for metered tasks (P1-3).

    With --output-format json the per-chunk stdout is a JSON envelope whose
    model text lives in `result`; push the readable text to the live panel
    instead of raw JSON. Non-JSON chunks (legacy text mode, error banners)
    pass through unchanged.
    """
    if not text:
        return text
    try:
        data = json.loads(text)
        if isinstance(data, dict) and isinstance(data.get("result"), str):
            return data["result"]
    except Exception:
        pass
    return text


def record_task_usage_best_effort(
    client: SharpClient,
    driver,
    worker: WorkerConfig,
    result: ProcessResult,
    *,
    project_id: str,
    intent_id: str,
    task_type: str,
    phase: str,
    duration_ms: int,
) -> None:
    """P1-3 real token metering: best-effort one-shot usage report.

    Enabled per worker via env `SHARP_TOKEN_METERING=1` (default off — legacy
    behavior, zero overhead). Any failure is logged and swallowed: metering
    must NEVER affect the task outcome. The outcome is derived from the
    process result itself, so the caller can record right after the process
    returns, before the task branches on success/failure.
    """
    # Gate check must survive ANY worker shape (stub workers in tests, future
    # driver changes): metering is pure observability and must never crash a
    # task. The rest of the body is already inside try/except.
    try:
        worker_env = dict(getattr(worker, "env", None) or {})
    except Exception:
        worker_env = {}
    if str(worker_env.get("SHARP_TOKEN_METERING", "")).strip() != "1":
        return
    try:
        usage = driver.extract_usage(result.stdout)
        if not usage:
            return
        u = usage.get("usage") if isinstance(usage.get("usage"), dict) else {}
        try:
            cost = float(usage.get("total_cost_usd", 0) or 0)
        except (TypeError, ValueError):
            cost = 0.0
        record = {
            "project_id": project_id,
            "intent_id": intent_id,
            "worker_name": worker.name,
            "task_type": task_type,
            "phase": phase,
            "model": str(usage.get("model") or worker.env.get("ANTHROPIC_MODEL") or ""),
            "input_tokens": int(u.get("input_tokens", 0) or 0),
            "output_tokens": int(u.get("output_tokens", 0) or 0),
            "cache_read_input_tokens": int(u.get("cache_read_input_tokens", 0) or 0),
            "cache_creation_input_tokens": int(u.get("cache_creation_input_tokens", 0) or 0),
            "total_cost_usd": cost,
            "duration_ms": int(duration_ms),
            "outcome": _usage_outcome(result),
        }
        client.record_task_usage([record])
    except Exception as exc:
        LOG.warning(
            "usage record skipped project=%s intent=%s task=%s phase=%s error=%s",
            project_id,
            intent_id,
            task_type,
            phase,
            exc,
        )


def _usage_outcome(result: ProcessResult) -> str:
    if getattr(result, "cancelled", False):
        return "cancelled"
    if getattr(result, "timed_out", False):
        return "timed_out"
    if getattr(result, "returncode", 0) != 0:
        return "failed"
    return "success"


class EvidenceBuffer:
    """P1-1 evidence auto-bind: accumulate a task's live output in memory.

    The buffer keeps the raw live chunks the task actually produced (tool
    output / HTTP probes). `_best_effort_create_vulns` uses it to auto-fill a
    vulnerability's `evidence` from real observed output when the model did
    not supply one. Bounded: when the accumulated text exceeds max_chars, the
    oldest half is dropped. When disabled (gate off) it stores nothing.
    """

    def __init__(self, enabled: bool, max_chars: int = 65536):
        self._enabled = enabled
        self._max = max_chars
        self._chunks: list[str] = []

    @property
    def enabled(self) -> bool:
        return self._enabled

    def feed(self, text: str) -> None:
        if not self._enabled or not text:
            return
        self._chunks.append(text)
        total = sum(len(c) for c in self._chunks)
        if total > self._max:
            half = len(self._chunks) // 2
            self._chunks = self._chunks[half:]

    def snapshot(self) -> str:
        return "".join(self._chunks)


def extract_evidence_snippet(buffer_text: str, url: str, limit: int = 600) -> str:
    """P1-1: pull an evidence snippet from raw task output for a given URL.

    Looks for lines mentioning the exact URL, then (fallback) the URL's host.
    Returns the most recent up-to-5 matching lines, truncated to `limit`.
    Empty when nothing matches — the caller then keeps the model-provided
    evidence (or leaves it empty). Never guesses: no synthetic evidence.
    """
    if not buffer_text or not url:
        return ""
    try:
        host = urlparse(url).hostname or ""
    except Exception:
        host = ""
    norm = url.rstrip("/")
    lines = [ln for ln in buffer_text.splitlines() if ln.strip()]
    hits = [ln for ln in lines if norm in ln]
    if not hits and host:
        hits = [ln for ln in lines if host in ln]
    if not hits:
        return ""
    return "\n".join(hits[-5:])[:limit]


# ---------------------------------------------------------------------------
# MCP preflight (P3-2)
# ---------------------------------------------------------------------------

MCP_CONFIG_PATH = "/tmp/sharp-mcp.json"
MCP_PREFLIGHT_TIMEOUT = 10  # seconds per server probe
MCP_PREFLIGHT_CACHE_TTL = 300  # 5 minutes — avoid re-probing reachable servers

# Process-level cache for HTTP MCP probe results.
# Key: (server_name, server_url)  Value: (ok, tool_count, error, timestamp)
_mcp_probe_cache: dict[tuple[str, str], tuple[bool, int, str, float]] = {}

_NODE_MCP_PROBE_SCRIPT = r"""
const url = process.argv[2];
const headers = JSON.parse(process.argv[3] || '{}');
const TIMEOUT_MS = parseInt(process.argv[4] || '10000');

const controller = new AbortController();
const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);

(async () => {
  try {
    const initResp = await fetch(url, {
      method: 'POST',
      headers: { 'content-type': 'application/json', ...headers },
      body: JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'initialize', params: { protocolVersion: '2024-11-05', capabilities: {}, clientInfo: { name: 'sharp-preflight', version: '0.1.0' } } }),
      signal: controller.signal,
    });
    if (!initResp.ok) { process.stderr.write(`init http ${initResp.status}\n`); process.exit(1); }
    const initData = await initResp.json();
    if (initData.error) { process.stderr.write(`init error: ${JSON.stringify(initData.error)}\n`); process.exit(1); }

    const toolsResp = await fetch(url, {
      method: 'POST',
      headers: { 'content-type': 'application/json', ...headers },
      body: JSON.stringify({ jsonrpc: '2.0', id: 2, method: 'tools/list', params: {} }),
      signal: controller.signal,
    });
    if (!toolsResp.ok) { process.stderr.write(`tools/list http ${toolsResp.status}\n`); process.exit(1); }
    const toolsData = await toolsResp.json();
    if (toolsData.error) { process.stderr.write(`tools/list error: ${JSON.stringify(toolsData.error)}\n`); process.exit(1); }
    const tools = (toolsData.result && toolsData.result.tools) || [];
    process.stdout.write(JSON.stringify({ ok: true, tool_count: tools.length }));
    process.exit(0);
  } catch (e) {
    process.stderr.write(`probe failed: ${e}\n`);
    process.exit(1);
  } finally {
    clearTimeout(timer);
  }
})().catch(() => {});
""".strip()


def preflight_mcp(
    config: DispatchConfig,
    container_manager: ContainerManager,
    container_name: str,
) -> str | None:
    """Probe all enabled MCP servers, write a JSON config for the reachable
    ones, and return the in-container path. Returns None when no servers are
    configured or all probes fail (fault isolation).

    Only HTTP-transport servers are probed; stdio servers are included
    without probing (claude-code will start them itself).
    """
    servers = [s for s in config.mcp_servers if s.enabled]
    if not servers:
        return None

    import json

    mcp_config: dict[str, dict] = {}
    for srv in servers:
        if srv.transport == "http":
            assert srv.url is not None
            ok, tool_count, error = _probe_http_mcp_cached(container_manager, container_name, srv)
            if not ok:
                LOG.warning(
                    "mcp preflight: server '%s' unreachable, skipping. error=%s",
                    srv.name, error,
                )
                continue
            if tool_count > srv.max_tools:
                LOG.warning(
                    "mcp preflight: server '%s' has %d tools > max_tools %d, skipping",
                    srv.name, tool_count, srv.max_tools,
                )
                continue
            LOG.info(
                "mcp preflight: server '%s' ok, %d tools available",
                srv.name, tool_count,
            )
            mcp_config[srv.name] = {
                "type": "http",
                "url": srv.url,
                "headers": srv.headers or {},
            }
        elif srv.transport == "stdio":
            assert srv.command is not None
            mcp_config[srv.name] = {
                "type": "stdio",
                "command": srv.command,
                "args": srv.args,
                "env": srv.env or {},
            }
            LOG.info("mcp preflight: stdio server '%s' included (no probe)", srv.name)

    if not mcp_config:
        LOG.warning("mcp preflight: all servers unreachable, no MCP config written")
        return None

    json_content = json.dumps({"mcpServers": mcp_config}, indent=2)
    container_manager.write_text_file(container_name, MCP_CONFIG_PATH, json_content)
    LOG.info(
        "mcp preflight: wrote %d server(s) to %s",
        len(mcp_config), MCP_CONFIG_PATH,
    )
    return MCP_CONFIG_PATH


def inject_mcp_flags(argv: list[str], mcp_config_path: str | None) -> list[str]:
    """Insert --mcp-config and --strict-mcp-config flags into a claude-code
    argv, right before --dangerously-skip-permissions. Returns a new list.
    If mcp_config_path is None, returns argv unchanged."""
    if not mcp_config_path:
        return argv
    flags = ["--mcp-config", mcp_config_path, "--strict-mcp-config"]
    result: list[str] = []
    injected = False
    for arg in argv:
        if not injected and arg == "--dangerously-skip-permissions":
            result.extend(flags)
            injected = True
        result.append(arg)
    if not injected:
        # Fallback: prepend flags after the program name
        result = [argv[0]] if argv else []
        result.extend(flags)
        result.extend(argv[1:] if argv else [])
    return result


def _probe_http_mcp_cached(
    container_manager: ContainerManager,
    container_name: str,
    server: MCPServerConfig,
) -> tuple[bool, int, str]:
    """Cache wrapper around _probe_http_mcp. Returns cached result if the
    previous probe for this server is still within MCP_PREFLIGHT_CACHE_TTL.
    This avoids re-spawning a node process inside the container on every
    task execution when the same HTTP MCP server is probed repeatedly."""
    assert server.url is not None
    cache_key = (server.name, server.url)
    now = time.monotonic()
    cached = _mcp_probe_cache.get(cache_key)
    if cached is not None:
        ok, tool_count, error, ts = cached
        age = now - ts
        if age < MCP_PREFLIGHT_CACHE_TTL:
            LOG.debug(
                "mcp preflight: cache hit for server '%s' (age=%.1fs, ok=%s, tools=%d)",
                server.name, age, ok, tool_count,
            )
            return ok, tool_count, error
    # Cache miss or expired — perform a real probe
    result = _probe_http_mcp(container_manager, container_name, server)
    _mcp_probe_cache[cache_key] = (*result, now)
    return result


def _probe_http_mcp(
    container_manager: ContainerManager,
    container_name: str,
    server: MCPServerConfig,
) -> tuple[bool, int, str]:
    """Probe a single HTTP MCP server from inside the container. Returns
    (ok, tool_count, error_message)."""
    assert server.url is not None
    import json

    headers_json = json.dumps(server.headers or {})
    argv = [
        "node", "-e", _NODE_MCP_PROBE_SCRIPT,
        server.url,
        headers_json,
        str(MCP_PREFLIGHT_TIMEOUT * 1000),
    ]
    process = container_manager.build_exec_process(
        container_name,
        {},
        argv,
        timeout_seconds=MCP_PREFLIGHT_TIMEOUT + 5,
    )
    process.start()
    try:
        result = process.communicate(timeout=MCP_PREFLIGHT_TIMEOUT + 10)
    except Exception as exc:
        return False, 0, f"process error: {exc}"
    if result.returncode != 0:
        stderr = result.stderr.strip()[:200] if result.stderr else "unknown"
        return False, 0, stderr
    try:
        data = json.loads(result.stdout.strip())
        return True, data.get("tool_count", 0), ""
    except (json.JSONDecodeError, ValueError) as exc:
        return False, 0, f"parse error: {exc}"
