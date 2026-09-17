from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, HTTPException

from sharp.secrets import default_secrets_file, save_secrets, secret_status, tool_root
from sharp.server.db import get_conn
from sharp.server.models import (
    MCPSettingsRequest,
    MCPServerSetting,
    SecretsResponse,
    Settings,
    UpdateSecretsRequest,
)

router = APIRouter(tags=["settings"])


@router.get("/settings", response_model=Settings)
def get_settings():
    with get_conn() as conn:
        row = conn.execute("SELECT intent_timeout, reason_timeout, report_instructions FROM settings WHERE rowid = 1").fetchone()
        return Settings(
            intent_timeout=row["intent_timeout"],
            reason_timeout=row["reason_timeout"],
            report_instructions=row["report_instructions"] or "",
        )


@router.put("/settings", response_model=Settings)
def update_settings(body: Settings):
    with get_conn() as conn:
        conn.execute(
            "UPDATE settings SET intent_timeout = ?, reason_timeout = ?, report_instructions = ? WHERE rowid = 1",
            (body.intent_timeout, body.reason_timeout, body.report_instructions),
        )
        return body


@router.get("/settings/secrets", response_model=SecretsResponse)
def get_secrets():
    path = default_secrets_file()
    return SecretsResponse(path=str(path), values=secret_status(path))


@router.put("/settings/secrets", response_model=SecretsResponse)
def update_secrets(body: UpdateSecretsRequest):
    path = default_secrets_file()
    try:
        save_secrets(body.values, path)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return SecretsResponse(path=str(path), values=secret_status(path))


# ---------------------------------------------------------------------------
# MCP servers (settings page, 2026-08-30)
#
# The mcp_servers section lives in dispatch.yaml, which is full of hand-written
# comments. We therefore never re-dump the whole file: we splice only the
# mcp_servers block (replace-or-append), so every comment elsewhere survives.
# ---------------------------------------------------------------------------


def dispatch_config_path() -> Path:
    return tool_root() / "dispatch.yaml"


def _find_mcp_block(text: str) -> tuple[int, int] | None:
    """Locate the top-level `mcp_servers:` block by line scanning.

    Returns (start_line, end_line) where end_line is exclusive (the first line
    that does NOT belong to the block). None when the file has no such block.
    """
    lines = text.splitlines(keepends=True)
    start: int | None = None
    for i, line in enumerate(lines):
        if line.startswith("mcp_servers:") or line.lstrip().startswith("mcp_servers:"):
            start = i
            break
    if start is None:
        return None
    end = start + 1
    while end < len(lines):
        line = lines[end]
        # Indented lines and blank lines belong to the block; any other line
        # (top-level key, top-level comment) ends it.
        if line.startswith((" ", "\t")) or not line.strip():
            end += 1
        else:
            break
    return start, end


def _yaml_scalar(value: Any) -> str:
    """Render a scalar without PyYAML's document-end marker.

    Plain (unquoted) output when the string is YAML-safe; double-quoted via
    json.dumps otherwise (JSON string syntax is valid YAML). Booleans and ints
    render literally.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if not isinstance(value, str):
        value = str(value)
    if _yaml_plain_safe(value):
        return value
    import json
    return json.dumps(value, ensure_ascii=False)


def _yaml_plain_safe(value: str) -> bool:
    """True when `value` can be emitted as an unquoted YAML scalar without
    being misinterpreted (no leading specials, no ': ' / ' #' sequences, not a
    bare boolean/null literal)."""
    if not value or value != value.strip():
        return False
    if value[0] in '-?:,[]{}#&*!|>\'"`%@':
        return False
    if value.lower() in ("true", "false", "null", "yes", "no", "on", "off", "~"):
        return False
    for i, ch in enumerate(value):
        if ch == ":" and (i + 1 >= len(value) or value[i + 1] == " "):
            return False
        if ch == "#" and i > 0 and value[i - 1] == " ":
            return False
    return True


def _yaml_key(value: str) -> str:
    """Render a mapping key plain when safe, double-quoted otherwise."""
    if _yaml_plain_safe(value):
        return value
    return _yaml_scalar(value)


def _render_mcp_block(servers: list[dict[str, Any]]) -> str:
    """Render the mcp_servers block from validated server dicts."""
    if not servers:
        # 空列表必须显式渲染为 []，否则 yaml 解析为 None，dispatcher 的
        # list 字段校验会失败（Field(default_factory=list) 只在字段缺失时生效）
        return "mcp_servers: []"
    lines = ["mcp_servers:"]
    for srv in servers:
        lines.append(f"  - name: {_yaml_scalar(srv['name'])}")
        transport = srv.get("transport", "http")
        lines.append(f"    transport: {_yaml_scalar(transport)}")
        if transport == "http":
            lines.append(f"    url: {_yaml_scalar(srv.get('url'))}")
            if srv.get("headers"):
                lines.append("    headers:")
                for k, v in srv["headers"].items():
                    lines.append(f"      {_yaml_key(str(k))}: {_yaml_scalar(v)}")
        elif transport == "stdio":
            lines.append(f"    command: {_yaml_scalar(srv.get('command'))}")
            if srv.get("args"):
                lines.append("    args:")
                for a in srv["args"]:
                    lines.append(f"      - {_yaml_scalar(a)}")
            if srv.get("env"):
                lines.append("    env:")
                for k, v in srv["env"].items():
                    lines.append(f"      {_yaml_key(str(k))}: {_yaml_scalar(v)}")
        lines.append(f"    enabled: {_yaml_scalar(bool(srv.get('enabled', True)))}")
        lines.append(f"    max_tools: {_yaml_scalar(srv.get('max_tools', 100))}")
    return "\n".join(lines)


def _read_mcp_servers() -> tuple[list[dict[str, Any]], str]:
    """Read the current mcp_servers list and the raw file text."""
    path = dispatch_config_path()
    if not path.exists():
        raise HTTPException(404, f"dispatch.yaml 不存在：{path}")
    text = path.read_text(encoding="utf-8")
    bounds = _find_mcp_block(text)
    if bounds is None:
        return [], text
    start, end = bounds
    block_text = "".join(text.splitlines(keepends=True)[start:end])
    try:
        data = yaml.safe_load(block_text)
    except yaml.YAMLError as exc:
        raise HTTPException(500, f"mcp_servers 段解析失败：{exc}") from exc
    servers = (data or {}).get("mcp_servers") or []
    if not isinstance(servers, list):
        raise HTTPException(500, "mcp_servers 段格式异常：不是列表")
    return servers, text


def _splice_text(text: str, servers: list[dict[str, Any]]) -> str:
    """Pure-text splice: replace the mcp_servers block, or append at the end.
    Returns the new file content without touching the filesystem."""
    block = _render_mcp_block(servers)
    lines = text.splitlines(keepends=True)
    bounds = _find_mcp_block(text)
    if bounds is not None:
        start, end = bounds
        new_lines = lines[:start] + [block + "\n"] + lines[end:]
    else:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        new_lines = lines + ["\n", block + "\n"]
    return "".join(new_lines)


def _write_mcp_servers(text: str, servers: list[dict[str, Any]]) -> None:
    """Splice the rendered mcp_servers block into the file, preserving every
    other line byte-for-byte. Atomic write via temp file + rename."""
    new_text = _splice_text(text, servers)
    path = dispatch_config_path()
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(new_text, encoding="utf-8")
    tmp.replace(path)


@router.get("/settings/mcp", response_model=MCPSettingsRequest)
def get_mcp_settings():
    servers, _ = _read_mcp_servers()
    return MCPSettingsRequest(
        servers=[MCPServerSetting(**s) for s in servers]
    )


@router.put("/settings/mcp", response_model=MCPSettingsRequest)
def update_mcp_settings(body: MCPSettingsRequest):
    # Validate through the dispatcher's own model so the frontend gets the same
    # errors the dispatcher would raise at load time.
    from sharp.dispatcher.config import MCPServerConfig

    try:
        validated = [
            MCPServerConfig(**s.model_dump(exclude_none=True)).model_dump(
                exclude_none=True
            )
            for s in body.servers
        ]
    except ValueError as exc:
        raise HTTPException(400, f"MCP 配置校验失败：{exc}") from exc

    _, text = _read_mcp_servers()
    _write_mcp_servers(text, validated)
    return MCPSettingsRequest(servers=[MCPServerSetting(**s) for s in validated])