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
  vuln creation. Errors are logged and swallowed. Audit fix B1: the HTTP POST
  itself runs fire-and-forget on a daemon thread — a slow webhook endpoint
  can no longer hold an API worker thread hostage.

Config (operator env):
- ``SHARP_ALERT_ENABLED`` — "1"/"true"/"on" enables; default off (legacy).
- ``SHARP_ALERT_WEBHOOK`` — the robot HTTP URL to POST to.
- ``SHARP_ALERT_SEVERITIES`` — comma-separated severities that trigger
  (default ``critical,high``).
- ``SHARP_ALERT_COST_PER_TASK`` — per-task cost anomaly threshold in USD
  (audit fix A1 short-term; a task_usage row above it triggers an alert on
  the same channel — early warning for a drained/stolen worker token).
  Unset/0 disables (default).

The outgoing body follows the widely-supported plain-text robot shape:

    {"msgtype": "text", "text": {"content": "<message>"}}
"""

from __future__ import annotations

import logging
import os
import threading
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


def build_alert_message(
    project_title: str,
    project_id: str,
    body: dict,
    subject: str = "【Sharp 高危漏洞】",
) -> str:
    """Build a plain-text alert message from an alert body.

    ``body`` mirrors :class:`CreateVulnerabilityRequest` fields. Only safe,
    identifying fields are included; bodies that may contain secrets are never
    copied into the message. ``subject`` is the alert header line (the per-task
    cost anomaly alert reuses this channel with its own subject).
    """
    title = _sanitize_title(body.get("title", ""))
    severity = (body.get("severity") or "").lower()
    kind = _sanitize_title(body.get("kind", ""))
    fact_id = _sanitize_title(body.get("fact_id", ""))
    url_host = _extract_host(body.get("url", ""))

    parts = [
        subject,
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


def _post_webhook(url: str, payload: dict) -> None:
    """Single webhook POST. Audit fix B1: invoked on a daemon thread so a slow
    webhook endpoint cannot block API worker threads. Failures logged+swallowed
    (same non-raising contract as before)."""
    try:
        requests.post(
            url,
            json=payload,
            timeout=WEBHOOK_TIMEOUT_SECS,
            headers={"Content-Type": "application/json"},
        )
    except Exception:
        LOG.warning("high-risk alert webhook send failed", exc_info=True)


def send_high_risk_alert(
    project_id: str, body: dict, subject: str = "【Sharp 高危漏洞】"
) -> bool:
    """Best-effort push of a high-risk alert to the configured webhook.

    Audit fix B1: only the message BUILD is synchronous (pure CPU); the HTTP
    POST is dispatched fire-and-forget on a daemon thread, so even a hanging
    webhook adds zero latency to the caller (create_vuln / usage-record).
    Alert volume is naturally bounded (high-severity vuln creation / cost
    anomalies), so one short-lived thread per alert is acceptable.

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
    message = build_alert_message(project_title, project_id, body, subject=subject)
    payload = {"msgtype": "text", "text": {"content": message}}
    threading.Thread(
        target=_post_webhook,
        args=(ALERT_WEBHOOK, payload),
        name="sharp-alert-webhook",
        daemon=True,
    ).start()
    return True