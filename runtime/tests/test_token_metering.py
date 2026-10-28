"""P1-3 real token metering tests (batch: P1-3).

Locks three layers:

- **driver (claudecode)**: the SHARP_TOKEN_METERING gate. Default ON since
  2026-10-28 — unset env injects `--output-format json`; explicit "0" opts
  out and keeps the legacy argv byte-for-byte. `extract_response_text`
  unwraps the JSON envelope's `result` field back to model text and falls back
  to raw stdout on any surprise (so the parse chain is never broken).
  `extract_usage` pulls usage/cost metadata, returning None on bad output.
- **record helper (common)**: `record_task_usage_best_effort` is a no-op when
  the gate is explicitly disabled ("0"), sends exactly one record when on
  (including the unset-env default), and swallows every failure — metering
  must never affect a task outcome.
- **server**: `POST /usage/record` persists rows into task_usage.

No docker daemon; the server tests use an in-memory temp DB + TestClient.
"""

from __future__ import annotations

import json
import types

import pytest

from sharp.dispatcher.tasks.common import record_task_usage_best_effort, unpack_live_output
from sharp.dispatcher.workers.adapters.claudecode import ClaudeCodeDriver
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

LEGACY_EXECUTE_ARGV = [
    "claude",
    "--session-id",
    "sess",
    "--dangerously-skip-permissions",
    "-p",
    "--",
    "prompt",
]
LEGACY_CONCLUDE_ARGV = [
    "claude",
    "-r",
    "sess",
    "--dangerously-skip-permissions",
    "-p",
    "--",
    "prompt",
]


def _worker(env: dict | None = None, name: str = "w1"):
    return types.SimpleNamespace(name=name, env=env or {})


def _driver():
    return ClaudeCodeDriver()


def _result(stdout: str = "", returncode: int = 0, timed_out: bool = False, cancelled: bool = False):
    return types.SimpleNamespace(
        stdout=stdout, returncode=returncode, timed_out=timed_out, cancelled=cancelled
    )


class RecordingClient:
    def __init__(self):
        self.recorded: list[list[dict]] = []

    def record_task_usage(self, records: list[dict]):
        self.recorded.append(records)


# ── driver: gate off keeps legacy argv byte-for-byte ───────────────────────

def test_metering_off_keeps_legacy_execute_argv():
    d = _driver()
    r = d.build_execute(_worker(env={"SHARP_TOKEN_METERING": "0"}), "prompt", "sess")
    assert r.argv == LEGACY_EXECUTE_ARGV


def test_metering_off_keeps_legacy_conclude_argv():
    d = _driver()
    assert d.build_conclude(_worker(env={"SHARP_TOKEN_METERING": "0"}), "prompt", "sess") == LEGACY_CONCLUDE_ARGV


def test_metering_default_on_injects_json_into_execute():
    """Unset SHARP_TOKEN_METERING must keep metering ON: the default was
    flipped 2026-10-28 (opt-out via "0"). Reverting to opt-in makes this red."""
    d = _driver()
    r = d.build_execute(_worker(env={}), "prompt", "sess")
    assert "--output-format" in r.argv
    assert r.argv.index("--output-format") < r.argv.index("--")


def test_metering_default_on_injects_json_into_conclude():
    d = _driver()
    argv = d.build_conclude(_worker(env={}), "prompt", "sess")
    assert "--output-format" in argv


# ── driver: gate on injects --output-format json before the `--` separator ─

def test_metering_on_injects_json_into_execute():
    d = _driver()
    r = d.build_execute(_worker(env={"SHARP_TOKEN_METERING": "1"}), "prompt", "sess")
    assert "--output-format" in r.argv
    assert "json" in r.argv
    assert r.argv.index("--output-format") < r.argv.index("--")


def test_metering_on_injects_json_into_conclude():
    d = _driver()
    argv = d.build_conclude(_worker(env={"SHARP_TOKEN_METERING": "1"}), "prompt", "sess")
    assert "--output-format" in argv
    assert argv.index("--output-format") < argv.index("--")


# ── driver: extract_response_text unwraps the envelope, never breaks ───────

def test_extract_response_text_unwraps_json_envelope():
    d = _driver()
    envelope = json.dumps({"type": "result", "subtype": "success", "result": "{\"accepted\":true}"})
    assert d.extract_response_text(envelope, "") == "{\"accepted\":true}"


def test_extract_response_text_passthrough_on_plain_or_broken():
    d = _driver()
    assert d.extract_response_text("plain text", "") == "plain text"
    assert d.extract_response_text("not json", "") == "not json"
    assert d.extract_response_text("", "") == ""
    # JSON but no `result` field → fall back to raw stdout
    assert d.extract_response_text(json.dumps({"type": "x"}), "") == json.dumps({"type": "x"})


# ── driver: extract_usage returns metadata or None ─────────────────────────

def test_extract_usage_ok():
    d = _driver()
    data = json.dumps({
        "type": "result",
        "usage": {
            "input_tokens": 1,
            "output_tokens": 2,
            "cache_read_input_tokens": 3,
            "cache_creation_input_tokens": 4,
        },
        "total_cost_usd": 0.01,
        "model": "sonnet",
    })
    usage = d.extract_usage(data)
    assert usage["usage"]["input_tokens"] == 1
    assert usage["total_cost_usd"] == 0.01
    assert usage["model"] == "sonnet"


def test_extract_usage_none_on_bad_output():
    d = _driver()
    assert d.extract_usage("not json") is None
    assert d.extract_usage(json.dumps([1, 2, 3])) is None  # non-dict JSON
    assert d.extract_usage("") is None


# ── record helper: gate off is a no-op ─────────────────────────────────────

def test_record_usage_best_effort_disabled_noop():
    client = RecordingClient()
    # stdout carries a VALID usage envelope — the gate (not the parser) is what
    # must suppress the record when SHARP_TOKEN_METERING is explicitly "0".
    envelope = json.dumps({"usage": {"input_tokens": 9}, "total_cost_usd": 0.01})
    record_task_usage_best_effort(
        client, _driver(), _worker(env={"SHARP_TOKEN_METERING": "0"}), _result(stdout=envelope),
        project_id="p1", intent_id="i1", task_type="explore", phase="execute", duration_ms=1,
    )
    assert client.recorded == []


def test_record_usage_best_effort_default_on_records():
    """Unset env must record (default ON since 2026-10-28)."""
    client = RecordingClient()
    envelope = json.dumps({"usage": {"input_tokens": 9}, "total_cost_usd": 0.01})
    record_task_usage_best_effort(
        client, _driver(), _worker(env={}), _result(stdout=envelope),
        project_id="p1", intent_id="i1", task_type="explore", phase="execute", duration_ms=1,
    )
    assert len(client.recorded) == 1
    assert client.recorded[0][0]["input_tokens"] == 9


# ── record helper: gate on records one row with correct fields ─────────────

def test_record_usage_best_effort_enabled_records_once():
    client = RecordingClient()
    envelope = json.dumps({
        "usage": {
            "input_tokens": 10,
            "output_tokens": 20,
            "cache_read_input_tokens": 30,
            "cache_creation_input_tokens": 40,
        },
        "total_cost_usd": 0.02,
        "model": "",
    })
    d = _driver()
    record_task_usage_best_effort(
        client, d, _worker(env={"SHARP_TOKEN_METERING": "1", "ANTHROPIC_MODEL": "sonnet"}),
        _result(stdout=envelope),
        project_id="p1", intent_id="i1", task_type="explore", phase="execute", duration_ms=150,
    )
    assert len(client.recorded) == 1
    r = client.recorded[0][0]
    assert r["input_tokens"] == 10
    assert r["output_tokens"] == 20
    assert r["cache_read_input_tokens"] == 30
    assert r["cache_creation_input_tokens"] == 40
    assert r["total_cost_usd"] == 0.02
    assert r["model"] == "sonnet"  # falls back to worker env model
    assert r["duration_ms"] == 150
    assert r["task_type"] == "explore"
    assert r["phase"] == "execute"
    assert r["outcome"] == "success"


def test_record_usage_best_effort_derives_outcome_from_result():
    client = RecordingClient()
    d = _driver()
    envelope = json.dumps({"usage": {"input_tokens": 1}, "total_cost_usd": 0})
    record_task_usage_best_effort(
        client, d, _worker(env={"SHARP_TOKEN_METERING": "1"}),
        _result(stdout=envelope, timed_out=True),
        project_id="p1", intent_id="i1", task_type="explore", phase="execute", duration_ms=1,
    )
    assert client.recorded[0][0]["outcome"] == "timed_out"


def test_record_usage_best_effort_swallows_bad_output():
    client = RecordingClient()
    d = _driver()
    # gate on but stdout is not JSON -> extract_usage None -> no record, no raise
    record_task_usage_best_effort(
        client, d, _worker(env={"SHARP_TOKEN_METERING": "1"}),
        _result(stdout="not json"),
        project_id="p1", intent_id="i1", task_type="explore", phase="execute", duration_ms=1,
    )
    assert client.recorded == []


def test_record_usage_best_effort_swallows_client_failure():
    class FailingClient:
        def record_task_usage(self, records):
            raise RuntimeError("server down")

    envelope = json.dumps({"usage": {"input_tokens": 1}, "total_cost_usd": 0})
    d = _driver()
    # must not raise even though the client write blows up
    record_task_usage_best_effort(
        FailingClient(), d, _worker(env={"SHARP_TOKEN_METERING": "1"}),
        _result(stdout=envelope),
        project_id="p1", intent_id="i1", task_type="explore", phase="execute", duration_ms=1,
    )


# ── live output unwrap ─────────────────────────────────────────────────────

def test_unpack_live_output_unwraps_envelope():
    assert unpack_live_output(json.dumps({"type": "result", "result": "hello"})) == "hello"


def test_unpack_live_output_passthrough_plain():
    assert unpack_live_output("plain chunk") == "plain chunk"
    assert unpack_live_output("") == ""


# ── server: POST /usage/record persists a row ─────────────────────────────

def test_usage_record_endpoint_persists(env_db, client):
    payload = {
        "records": [{
            "project_id": "p1",
            "intent_id": "i1",
            "worker_name": "w1",
            "task_type": "explore",
            "phase": "execute",
            "model": "sonnet",
            "input_tokens": 10,
            "output_tokens": 20,
            "cache_read_input_tokens": 30,
            "cache_creation_input_tokens": 40,
            "total_cost_usd": 0.5,
            "duration_ms": 123,
            "outcome": "success",
        }]
    }
    resp = client.post("/usage/record", json=payload, headers={"Authorization": f"Bearer {SERVER_TOKEN}"})
    assert resp.status_code == 204
    with db.get_conn() as conn:
        rows = conn.execute("SELECT * FROM task_usage").fetchall()
    assert len(rows) == 1
    assert rows[0]["input_tokens"] == 10
    assert rows[0]["task_type"] == "explore"
    assert rows[0]["outcome"] == "success"