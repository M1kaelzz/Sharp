"""P1-2 deterministic prescan (httpx / nmap / katana) for initial-state projects.

Why: bootstrap workers (LLMs) used to redo HTTP probing / port discovery / URL
enumeration by hand — the worst token burn of the first hour (measured:
millions of tokens per session). These are deterministic jobs: a tool answers
in seconds for free. When runtime.prescan_enabled is on, the dispatcher runs
the tools once BEFORE bootstrap, writes a seed fact, and the bootstrap worker
plans from that evidence instead of re-scanning.

Authorization hard-gate (scope discipline):
- the ONLY target is the host parsed from the project origin;
- scope host must pass `_valid_scope_host` (hostname or IPv4, no CIDR, no
  wildcards, no shell metacharacters — nothing an LLM could "extend" later);
- no subdomain enumeration, no CIDR expansion; katana output is filtered to
  the scope host before it ever reaches the graph.

Best-effort by design: missing tools / timeouts / failures skip that tool and
never block the bootstrap dispatch that follows.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

from sharp.dispatcher.config import DispatchConfig
from sharp.dispatcher.protocol.client import SharpClient
from sharp.dispatcher.runtime.containers import ContainerManager
from sharp.server.models import ProjectDetail

LOG = logging.getLogger(__name__)

PRESCAN_CREATOR = "dispatcher.prescan"
PRESCAN_INTENT_PREFIX = "目标确定性预扫描"

_SCOPE_HOST_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)*$")
_IPV4_RE = re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}$")
_SHELL_META = frozenset("*?|;&$`\"'()<> \t,")

MAX_OUTPUT_BYTES = 8192
MAX_KATANA_URLS = 50

PRESCAN_TOOLS = ("httpx", "nmap", "katana")
PRESCAN_TIMEOUTS = {"httpx": 30, "nmap": 60, "katana": 30}

# Tool argv, scope-locked: every target comes from origin/scope host, no
# external expansion. Flags chosen for silence + bounded output.
def _tool_argv(tool: str, origin_url: str, host: str) -> list[str]:
    if tool == "httpx":
        return ["httpx", "-u", origin_url, "-silent", "-status-code", "-title", "-tech-detect", "-no-color"]
    if tool == "nmap":
        return ["nmap", "-Pn", "-T4", "--top-ports", "100", host]
    if tool == "katana":
        return ["katana", "-u", origin_url, "-d", "1", "-silent", "-no-color"]
    raise ValueError(f"unknown prescan tool: {tool}")


def extract_scope_host(origin: str) -> str | None:
    """Parse the single authorized target host from the project origin.

    None on any parse failure — the caller must skip prescan (never guess).
    """
    if not origin or not origin.strip():
        return None
    text = origin.strip()
    try:
        parsed = urlparse(text if "://" in text else f"http://{text}")
        host = parsed.hostname
    except Exception:
        return None
    if not host:
        return None
    # urlparse is lenient (e.g. "http://a b" yields host "a b"); anything with
    # whitespace or non-token characters is NOT a scannable single host.
    if any(ch in host for ch in " \t" ):
        return None
    return host


def valid_scope_host(host: str) -> bool:
    """Scope hard-gate: hostname or IPv4 only.

    Rejects CIDR (`/`), wildcards (`*`), commas, whitespace, shell
    metacharacters, ports and anything that could be extended by an LLM into
    out-of-scope scanning.
    """
    if not host or len(host) > 253:
        return False
    if any(ch in host for ch in _SHELL_META):
        return False
    if "/" in host or ":" in host or " " in host:
        return False
    if _IPV4_RE.match(host):
        # Strict octets: reject 999.1.1.1 style.
        parts = host.split(".")
        return all(0 <= int(p) <= 255 for p in parts)
    return bool(_SCOPE_HOST_RE.match(host))


def origin_from_facts(project: ProjectDetail) -> str:
    """The project's origin target (origin fact description)."""
    for fact in project.facts:
        if fact.id == "origin":
            return fact.description or ""
    return ""


def run_prescan_task(
    config: DispatchConfig,
    client: SharpClient,
    container_manager: ContainerManager,
    container_name: str,
    project: ProjectDetail,
) -> str:
    """Deterministic prescan for an initial-state project. Always returns
    "success" for the dispatch loop (prescan outcome must never gate the
    bootstrap that follows; failures are logged and reflected in the fact)."""
    origin = origin_from_facts(project)
    host = extract_scope_host(origin)
    if not host or not valid_scope_host(host):
        LOG.warning(
            "prescan skipped project=%s invalid/unsupported scope origin=%r host=%r",
            project.project.id,
            origin[:120],
            host,
        )
        return "success"

    intent_id = _create_prescan_intent(client, project.project.id, host)
    if intent_id is None:
        return "success"

    sections: list[str] = []
    for tool in PRESCAN_TOOLS:
        argv = _tool_argv(tool, origin, host)
        result = _run_tool(container_manager, container_name, argv, PRESCAN_TIMEOUTS[tool])
        if result is None:
            LOG.info("prescan tool unavailable project=%s tool=%s", project.project.id, tool)
            continue
        if result.timed_out:
            LOG.info("prescan tool timed out project=%s tool=%s", project.project.id, tool)
            continue
        if result.returncode != 0:
            LOG.info(
                "prescan tool failed project=%s tool=%s code=%s stderr_preview=%s",
                project.project.id,
                tool,
                result.returncode,
                _preview(result.stderr),
            )
            continue
        text = _sanitize_tool_output(tool, host, result.stdout)
        if text:
            sections.append(text)

    if sections:
        description = f"# 预扫描结果（源：确定性预扫描）\n\n" + "\n\n".join(sections)
    else:
        description = "# 预扫描结果\n\n预扫描未发现有效信息（工具不可用或目标无响应）。"
    description = description[:4000]

    try:
        resp = client.conclude(project.project.id, intent_id, PRESCAN_CREATOR, description)
        if not resp.ok:
            LOG.warning(
                "prescan conclude failed project=%s intent=%s status=%s",
                project.project.id,
                intent_id,
                resp.status_code,
            )
        else:
            LOG.info(
                "prescan fact written project=%s host=%s tools=%s",
                project.project.id,
                host,
                ",".join(tool for tool in PRESCAN_TOOLS),
            )
    except Exception as exc:
        LOG.warning("prescan conclude exception project=%s intent=%s error=%s", project.project.id, intent_id, exc)
    return "success"


def _create_prescan_intent(client: SharpClient, project_id: str, host: str) -> str | None:
    """Create the prescan intent (unclaimed, so conclude accepts any worker).

    Returns the intent id or None on failure / approval-gate pending (in which
    case the prescan is skipped this round and bootstrap proceeds untouched).
    """
    description = f"{PRESCAN_INTENT_PREFIX}：{host}"
    try:
        resp = client.create_intent(
            project_id,
            ["origin"],
            description,
            PRESCAN_CREATOR,
            idempotency_key=None,
        )
        if not resp.ok:
            LOG.warning("prescan intent create failed project=%s status=%s", project_id, resp.status_code)
            return None
        data = resp.data if isinstance(resp.data, dict) else {}
        intent_id = data.get("id")
        if not intent_id:
            return None
        if data.get("approval_status") == "pending":
            LOG.warning("prescan intent pending approval project=%s intent=%s skip", project_id, intent_id)
            return None
        return intent_id
    except Exception as exc:
        LOG.warning("prescan intent create exception project=%s error=%s", project_id, exc)
        return None


def _run_tool(container_manager: ContainerManager, container_name: str, argv: list[str], timeout_seconds: int):
    """Run one prescan tool inside the worker container. None on any failure."""
    try:
        process = container_manager.build_exec_process(
            container_name,
            {},
            argv,
            timeout_seconds=timeout_seconds,
        )
        process.start()
        return process.communicate(timeout=timeout_seconds + 15)
    except Exception as exc:
        LOG.info("prescan tool launch failed tool=%s error=%s", argv[0], exc)
        return None


def _sanitize_tool_output(tool: str, scope_host: str, raw_stdout: str) -> str:
    """Per-tool output shaping + scope enforcement.

    katana output is filtered to the scope host only (no out-of-scope URL ever
    reaches the graph). Output is truncated to MAX_OUTPUT_BYTES.
    """
    text = (raw_stdout or "").strip()
    if not text:
        return ""
    if tool == "katana":
        keep: list[str] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                host = urlparse(line).hostname or ""
            except Exception:
                host = ""
            if host and host != scope_host:
                continue  # out-of-scope URL: drop
            keep.append(line)
            if len(keep) >= MAX_KATANA_URLS:
                break
        return "\n".join(keep)[:MAX_OUTPUT_BYTES]
    return text[:MAX_OUTPUT_BYTES]


def _preview(text: str, limit: int = 200) -> str:
    return (text or "").strip().replace("\n", " ")[:limit]