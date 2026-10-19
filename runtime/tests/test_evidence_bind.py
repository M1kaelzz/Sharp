"""P1-1 evidence auto-bind tests (batch: P1-1).

Locks:

- EvidenceBuffer: enabled accumulates live output (bounded); disabled stores
  nothing; feed/snapshot round-trips; the oldest half is dropped past the cap.
- extract_evidence_snippet: pulls lines matching the exact URL, then falls
  back to the same host; returns "" (never fabricated) when nothing matches.
- _best_effort_create_vulns with an EvidenceBuffer: a finding with no
  `evidence` gets it auto-filled from the buffer for its URL; a finding that
  already has evidence keeps it; without a buffer behaviour is unchanged.

No docker daemon, no live server.
"""

from __future__ import annotations

import types

from sharp.dispatcher.tasks.common import EvidenceBuffer, extract_evidence_snippet
from sharp.dispatcher.tasks.explore import _best_effort_create_vulns


# ── EvidenceBuffer ─────────────────────────────────────────────────────────

def test_evidence_buffer_enabled_collects_and_snapshots():
    buf = EvidenceBuffer(enabled=True)
    buf.feed("line one\n")
    buf.feed("GET http://target.example/ HTTP/1.1 200 OK\n")
    assert "200 OK" in buf.snapshot()
    assert "line one" in buf.snapshot()


def test_evidence_buffer_disabled_stores_nothing():
    buf = EvidenceBuffer(enabled=False)
    buf.feed("GET http://target.example/ 200 OK\n")
    assert buf.snapshot() == ""


def test_evidence_buffer_bounded_drops_oldest():
    buf = EvidenceBuffer(enabled=True, max_chars=50)
    for i in range(20):
        buf.feed(f"chunk-{i:04d}\n" * 5)  # ~50 bytes each
    assert len(buf.snapshot()) <= 100  # bounded (oldest half dropped)


# ── extract_evidence_snippet ───────────────────────────────────────────────

def test_extract_exact_url_match():
    buf = "line1\nGET http://target.example/api HTTP/1.1 200 OK\nline2\n"
    assert "GET http://target.example/api" in extract_evidence_snippet(buf, "http://target.example/api")
    assert "line1" not in extract_evidence_snippet(buf, "http://target.example/api")


def test_extract_fallback_to_host():
    buf = "curl http://target.example/login -o /dev/null\n"
    out = extract_evidence_snippet(buf, "http://target.example/login")
    assert "curl" in out  # host fallback catches it


def test_extract_no_match_empty():
    assert extract_evidence_snippet("unrelated text", "http://other.example/x") == ""
    assert extract_evidence_snippet("", "http://target.example/x") == ""


# ── _best_effort_create_vulns auto-bind ───────────────────────────────────

class RecordingVulnClient:
    def __init__(self):
        self.records: list[dict] = []

    def create_vulnerability(self, project_id, fact_id, title, **kwargs):
        self.records.append({"title": title, **kwargs})
        return types.SimpleNamespace(ok=True, status_code=200, data={}, text="")


def _findings(evidence: str = ""):
    return [{"title": "SQLi", "url": "http://target.example/api", "evidence": evidence}]


def test_auto_bind_fills_blank_evidence():
    client = RecordingVulnClient()
    buf = EvidenceBuffer(enabled=True)
    buf.feed("SQL injection probe\nGET http://target.example/api?id=1 HTTP/1.1 500\n")
    _best_effort_create_vulns(
        client, "p1", "f1", "i1", "w1", _findings(evidence=""), buf
    )
    assert "GET http://target.example/api?id=1" in client.records[0]["evidence"]


def test_auto_bind_keeps_existing_evidence():
    client = RecordingVulnClient()
    buf = EvidenceBuffer(enabled=True)
    buf.feed("probe output\n")
    _best_effort_create_vulns(
        client, "p1", "f1", "i1", "w1", _findings(evidence="official curl cmd"), buf
    )
    assert client.records[0]["evidence"] == "official curl cmd"


def test_no_buffer_keeps_legacy_blank_evidence():
    client = RecordingVulnClient()
    _best_effort_create_vulns(
        client, "p1", "f1", "i1", "w1", _findings(evidence=""), None
    )
    assert client.records[0]["evidence"] == ""


def test_auto_bind_no_url_match_keeps_blank():
    client = RecordingVulnClient()
    buf = EvidenceBuffer(enabled=True)
    buf.feed("unrelated stuff\n")
    _best_effort_create_vulns(
        client, "p1", "f1", "i1", "w1", _findings(evidence=""), buf
    )
    assert client.records[0]["evidence"] == ""