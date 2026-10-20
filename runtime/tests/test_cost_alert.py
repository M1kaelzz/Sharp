"""Audit fix A1 (short-term): per-task cost anomaly alert tests.

``POST /usage/record`` (batch P1-3) additionally checks each usage row against
the operator env ``SHARP_ALERT_COST_PER_TASK`` — a row whose ``total_cost_usd``
exceeds the threshold pushes a best-effort alert through the batch-9 notifier
channel with its own subject (【Sharp 成本异常】) and a sanitized body (worker /
model / phase / cost only). This is the early warning for a drained or stolen
worker-token: worker containers receive the API token via env, so a
counter-RCE on the target can steal it and run up the bill.

Locks:
- threshold unset / 0 / unparsable -> disabled (legacy, no alert call).
- cost above threshold -> exactly one alert per offending row, with the cost
  and threshold in the title and subject 【Sharp 成本异常】.
- cost at/below threshold -> no alert.
- the usage row is still persisted (the alert is strictly after the insert and
  must never fail the write).

Temp DB + TestClient (same style as test_token_metering). No live webhook —
the notifier entrypoint is monkeypatched.
"""

from __future__ import annotations

import pytest

import sharp.server.notifier as notifier
from sharp.server import db

SERVER_TOKEN = "server-token-xyz"


@pytest.fixture
def env_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_db_path", None, raising=False)
    monkeypatch.setenv("SHARP_SERVER_TOKEN", SERVER_TOKEN)
    db.configure(tmp_path / "sharp.db")


@pytest.fixture
def client(env_db):
    from fastapi.testclient import TestClient
    from sharp.server.app import app

    c = TestClient(app)
    yield c
    c.close()


def _h() -> dict[str, str]:
    return {"Authorization": f"Bearer {SERVER_TOKEN}"}


def _record(cost: float = 0.5, **over) -> dict:
    base = {
        "project_id": "proj_9",
        "intent_id": "i1",
        "worker_name": "claude_worker",
        "task_type": "explore",
        "phase": "execute",
        "model": "sonnet",
        "input_tokens": 10,
        "output_tokens": 20,
        "total_cost_usd": cost,
        "duration_ms": 123,
        "outcome": "success",
    }
    base.update(over)
    return base


def _post(client, records) -> object:
    return client.post("/usage/record", json={"records": records}, headers=_h())


@pytest.fixture
def alert_spy(monkeypatch):
    """Capture notifier.send_high_risk_alert calls instead of threading out."""
    calls: list[tuple] = []

    def fake_send(project_id, body, subject="【Sharp 高危漏洞】"):
        calls.append((project_id, body, subject))
        return True

    monkeypatch.setattr(notifier, "send_high_risk_alert", fake_send)
    return calls


# ── disabled paths (legacy) ────────────────────────────────────────────────

def test_threshold_unset_no_alert(client, alert_spy, monkeypatch):
    monkeypatch.delenv("SHARP_ALERT_COST_PER_TASK", raising=False)
    resp = _post(client, [_record(cost=99.0)])
    assert resp.status_code == 204
    assert alert_spy == []


def test_threshold_zero_no_alert(client, alert_spy, monkeypatch):
    monkeypatch.setenv("SHARP_ALERT_COST_PER_TASK", "0")
    resp = _post(client, [_record(cost=99.0)])
    assert resp.status_code == 204
    assert alert_spy == []


def test_threshold_unparsable_disabled(client, alert_spy, monkeypatch):
    monkeypatch.setenv("SHARP_ALERT_COST_PER_TASK", "not-a-number")
    resp = _post(client, [_record(cost=99.0)])
    assert resp.status_code == 204
    assert alert_spy == []


def test_cost_below_threshold_no_alert(client, alert_spy, monkeypatch):
    monkeypatch.setenv("SHARP_ALERT_COST_PER_TASK", "5.0")
    resp = _post(client, [_record(cost=4.99)])
    assert resp.status_code == 204
    assert alert_spy == []


# ── alert path ─────────────────────────────────────────────────────────────

def test_cost_above_threshold_alerts_once(client, alert_spy, monkeypatch):
    monkeypatch.setenv("SHARP_ALERT_COST_PER_TASK", "5.0")
    resp = _post(client, [_record(cost=7.51)])
    assert resp.status_code == 204
    assert len(alert_spy) == 1
    project_id, body, subject = alert_spy[0]
    assert project_id == "proj_9"
    assert subject == "【Sharp 成本异常】"
    assert body["severity"] == "critical"  # passes the default severity filter
    assert "7.51" in body["title"]
    assert "5.00" in body["title"]
    assert "claude_worker" in body["title"]
    assert "sonnet" in body["title"]


def test_each_offending_row_alerts(client, alert_spy, monkeypatch):
    monkeypatch.setenv("SHARP_ALERT_COST_PER_TASK", "1.0")
    resp = _post(client, [_record(cost=0.2), _record(cost=3.3), _record(cost=9.9)])
    assert resp.status_code == 204
    assert len(alert_spy) == 2  # only the two rows above the threshold


def test_usage_row_still_persisted_with_alert(client, alert_spy, monkeypatch):
    monkeypatch.setenv("SHARP_ALERT_COST_PER_TASK", "5.0")
    resp = _post(client, [_record(cost=7.5)])
    assert resp.status_code == 204
    with db.get_conn() as conn:
        rows = conn.execute("SELECT * FROM task_usage").fetchall()
    assert len(rows) == 1
    assert rows[0]["total_cost_usd"] == 7.5


def test_alert_failure_never_fails_the_write(client, monkeypatch):
    monkeypatch.setenv("SHARP_ALERT_COST_PER_TASK", "1.0")

    def boom(pid, body, subject="x"):
        raise RuntimeError("notifier exploded")

    monkeypatch.setattr(notifier, "send_high_risk_alert", boom)
    resp = _post(client, [_record(cost=9.9)])
    assert resp.status_code == 204  # swallow: usage write never fails
    with db.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM task_usage").fetchone()["n"] == 1