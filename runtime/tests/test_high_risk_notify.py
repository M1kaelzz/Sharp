"""P2 high-risk IM notify tests (batch: P2).

Locks the outbound high-risk webhook notifier in ``server/notifier.py``:

- **sanitized summary only**: the alert message carries project title/id,
  severity, vuln title, kind, URL host and fact id — NEVER the evidence /
  reproduction / description / impact bodies (they may leak credentials or
  inject markdown into the IM).
- **plain text robot shape**: the payload is ``{"msgtype":"text","text":
  {"content": ...}}`` so the IM renders text, not markdown.
- **server-side env authority**: enabled/webhook/severities come from operator
  env, so workers cannot redirect or disable alerts.
- **best-effort, non-blocking**: disabled / unconfigured / ineligible severities
  return False without sending; a webhook failure never raises.
- **mutant gate**: dropping the severity-eligibility check must fail the
  ineligible test.

Pure function + mocked-requests test; no live server, no real webhook.
"""

from __future__ import annotations

import sharp.server.notifier as notifier
from sharp.server.notifier import build_alert_message, send_high_risk_alert


def _patch_env(monkeypatch, *, enabled=True, webhook="http://hook.invalid/x",
               severities=("critical", "high")):
    monkeypatch.setattr(notifier, "ALERT_ENABLED", enabled)
    monkeypatch.setattr(notifier, "ALERT_WEBHOOK", webhook)
    monkeypatch.setattr(notifier, "ALERT_SEVERITIES", frozenset(severities))


# ── build_alert_message ────────────────────────────────────────────────────

def test_alert_message_is_sanitized_summary_only():
    body = {
        "title": "RCE via unserialize",
        "severity": "critical",
        "kind": "vuln",
        "fact_id": "f7",
        "url": "http://target.example:8080/admin?token=SECRET",
        # Forbidden fields must never leak into the message.
        "evidence": "curl -X POST ... password=SECRET",
        "description": "P0wn3d: token=SECRET",
        "reproduction": "step1: send token=SECRET",
        "impact": "full compromise",
    }
    msg = build_alert_message("My Project", "proj_1", body)
    # Safe identifying fields present.
    assert "My Project (proj_1)" in msg
    assert "critical" in msg
    assert "RCE via unserialize" in msg
    assert "f7" in msg
    assert "target.example" in msg  # host only, not the full URL/query.
    # Forbidden bodies / secrets must NOT appear.
    assert "SECRET" not in msg
    assert "P0wn3d" not in msg
    assert "full compromise" not in msg
    assert "password=" not in msg


def test_alert_message_host_extraction_strips_query_and_path():
    body = {
        "title": "SQLi",
        "severity": "high",
        "kind": "vuln",
        "fact_id": "f9",
        "url": "https://admin.example.com/x?a=1&b=2",
    }
    msg = build_alert_message("Proj", "p2", body)
    assert "admin.example.com" in msg
    assert "a=1" not in msg
    assert "b=2" not in msg


def test_alert_message_unparsable_url_omits_host():
    body = {"title": "x", "severity": "high", "url": "not a url"}
    msg = build_alert_message("P", "p3", body)
    assert "目标" not in msg  # no host appended


def test_alert_message_control_chars_stripped_from_title():
    body = {"title": "a\x07b **markdown**", "severity": "high"}
    msg = build_alert_message("P", "p4", body)
    assert "\x07" not in msg
    assert "markdown" in msg


# ── eligibility ────────────────────────────────────────────────────────────

def test_alert_eligible_severities(monkeypatch):
    _patch_env(monkeypatch, severities=("critical", "high"))
    assert notifier._alert_eligible("critical") is True
    assert notifier._alert_eligible("high") is True
    assert notifier._alert_eligible("medium") is False
    assert notifier._alert_eligible("") is False


# ── send_high_risk_alert (best-effort, non-blocking) ──────────────────────

def test_send_disabled_returns_false_no_post(monkeypatch):
    _patch_env(monkeypatch, enabled=False)
    calls = []
    monkeypatch.setattr(notifier.requests, "post",
                        lambda *a, **k: calls.append(1) or None)
    assert send_high_risk_alert("proj", {"severity": "critical", "title": "x"}) is False
    assert calls == []


def test_send_unconfigured_returns_false_no_post(monkeypatch):
    _patch_env(monkeypatch, enabled=True, webhook="")
    calls = []
    monkeypatch.setattr(notifier.requests, "post",
                        lambda *a, **k: calls.append(1) or None)
    assert send_high_risk_alert("proj", {"severity": "critical", "title": "x"}) is False
    assert calls == []


def test_send_ineligible_severity_returns_false(monkeypatch):
    _patch_env(monkeypatch)
    calls = []
    monkeypatch.setattr(notifier.requests, "post",
                        lambda *a, **k: calls.append(1) or None)
    assert send_high_risk_alert("proj", {"severity": "low", "title": "x"}) is False
    assert calls == []


def test_send_high_risk_posts_plaintext_robot_shape(monkeypatch):
    _patch_env(monkeypatch)
    captured = {}

    def fake_post(url, *, json, timeout, headers):
        captured["url"] = url
        captured["json"] = json
        captured["timeout"] = timeout
        captured["headers"] = headers
        return None

    monkeypatch.setattr(notifier.requests, "post", fake_post)
    ok = send_high_risk_alert(
        "proj_9",
        {"project_title": "Proj Nine", "title": "RCE", "severity": "critical",
         "kind": "vuln", "fact_id": "f1", "url": "http://t.example/"},
    )
    assert ok is True
    assert captured["url"] == "http://hook.invalid/x"
    assert captured["json"]["msgtype"] == "text"
    content = captured["json"]["text"]["content"]
    assert "Proj Nine" in content
    assert "critical" in content
    assert "f1" in content


def test_send_webhook_failure_does_not_raise(monkeypatch):
    _patch_env(monkeypatch)

    def boom(url, **kwargs):
        raise RuntimeError("network down")

    monkeypatch.setattr(notifier.requests, "post", boom)
    # Must not raise.
    assert send_high_risk_alert("proj", {"severity": "high", "title": "x"}) is True


def test_send_eligible_custom_severity(monkeypatch):
    _patch_env(monkeypatch, severities=("critical",))
    calls = []
    monkeypatch.setattr(notifier.requests, "post",
                        lambda *a, **k: calls.append(1) or None)
    assert send_high_risk_alert("proj", {"severity": "critical", "title": "x"}) is True
    assert len(calls) == 1
    # high not in custom set -> no post.
    assert send_high_risk_alert("proj", {"severity": "high", "title": "x"}) is False
    assert len(calls) == 1