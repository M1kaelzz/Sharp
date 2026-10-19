"""Outbound high-risk notification (P2 batch: high-risk IM notify).

When a high/critical severity vulnerability is created, Sharp can push a
short, sanitized alert to an operator-configured webhook (WeCom / DingTalk /
Feishu robot or any plain-text webhook). This turns high-risk findings from
"pull" (someone must open the report page to notice) into "push" (the operator
is notified the moment the finding lands).

Security posture (mirrors ARTEX audit lessons):
- **Sanitized summary only**: the payload carries project id/title, the vuln
  title, severity, kind, URL host and the fact id — NEVER the evidence,
  reproduction, impact, recommendation or description bodies. Those may contain
  credentials, tokens or markdown that could leak secrets or inject into the
  target chat.
- **Plain text**: ``content_type = "text"`` so the message renders as text, not
  as markdown that could inject link/image tricks into the IM.
- **Server-side authority via env**: the webhook URL and the enable flag are
  read from the operator-owned environment (like ``server/risk.py``), never
  from worker-controlled data, so AI workers cannot redirect or disable alerts.
- **Best-effort, non-blocking**: a send failure must never fail or slow the
  vuln creation. Errors are logged and swallowed.

Config (operator env):
- ``SHARP_ALERT_ENABLED`` — "1"/"true"/"on" enables; default off (legacy).
- ``SHARP_ALERT_WEBHOOK`` — the robot HTTP URL to POST to.
- ``SHARP_ALERT_SEVERITIES`` — comma-separated severities that trigger
  (default ``critical,high``).

The outgoing body follows the widely-supported plain-text robot shape:

    {"msgtype": "text", "text": {"content": "<message>"}}
"""

from __future__ import annotations

import logging
import os
import urllib.parse

import requests

LOG = logging.getLogger(__name__)

# --- Operator env configuration (mirrors server/risk.py) --------------------
ALERT_ENABLED = os.environ.get("SHARP_ALERT_ENABLED", "false").strip().lower() in (
    "1", "true", "yes", "on"
)
ALERT_WEBHOOK = os.environ.get("SHARP_ALERT_WEBHOOK", "").strip()
_ALERT_SEVERITIES = [
    s.strip().lower()
    for s in os.environ.get("SHARP_ALERT_SEVERITIES", "critical,high").split(",")
    if s.strip()
]
ALERT_SEVERITIES = frozenset(_ALERT_SEVERITIES or ["critical", "high"])

WEBHOOK_TIMEOUT_SECS = 5.0

# Fields that are never included in the alert payload — they may carry
# credentials, secrets or injection payloads.
_FORBIDDEN_KEYS = ("evidence", "description", "reproduction", "impact")


def _extract_host(url: str) -> str:
    """Return just the host from a URL (or the raw string if unparsable)."""
    try:
        parsed = urllib.parse.urlparse(url)
        if parsed.hostname:
            return parsed.hostname
    except ValueError:
        pass
    return ""


def _sanitize_title(title: str) -> str:
    """Strip control chars that could confuse the chat rendering."""
    return "".join(ch for ch in (title or "") if ch >= " ")


def build_alert_message(project_title: str, project_id: str, body: dict) -> str:
    """Build a plain-text alert message from a vuln create request body.

    ``body`` mirrors :class:`CreateVulnerabilityRequest` fields. Only safe,
    identifying fields are included; bodies that may contain secrets are never
    copied into the message.
    """
    title = _sanitize_title(body.get("title", ""))
    severity = (body.get("severity") or "").lower()
    kind = _sanitize_title(body.get("kind", ""))
    fact_id = _sanitize_title(body.get("fact_id", ""))
    url_host = _extract_host(body.get("url", ""))

    parts = [
        "【Sharp 高危漏洞】",
        f"项目: {project_title} ({project_id})",
        f"严重度: {severity}",
        f"漏洞: {title}",
    ]
    if kind:
        parts.append(f"类型: {kind}")
    if url_host:
        parts.append(f"目标: {url_host}")
    if fact_id:
        parts.append(f"证据: fact {fact_id}")
    return "\n".join(parts)


def _alert_eligible(severity: str) -> bool:
    return (severity or "").strip().lower() in ALERT_SEVERITIES


def send_high_risk_alert(project_id: str, body: dict) -> bool:
    """Best-effort push of a high-risk alert to the configured webhook.

    Returns True when an alert was attempted (regardless of send result), False
    when disabled / unconfigured / ineligible. Never raises.
    """
    if not ALERT_ENABLED or not ALERT_WEBHOOK:
        return False
    if not _alert_eligible(body.get("severity", "")):
        return False

    # Locate the project title without blocking: caller provides it via
    # body.get("project_title") when available.
    project_title = _sanitize_title(body.get("project_title", ""))
    message = build_alert_message(project_title, project_id, body)
    payload = {"msgtype": "text", "text": {"content": message}}
    try:
        requests.post(
            ALERT_WEBHOOK,
            json=payload,
            timeout=WEBHOOK_TIMEOUT_SECS,
            headers={"Content-Type": "application/json"},
        )
    except Exception:
        LOG.warning("high-risk alert webhook send failed project=%s", project_id, exc_info=True)
    return True