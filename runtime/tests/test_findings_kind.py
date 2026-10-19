"""Finding-kind tests (batch B): flags/scored artifacts alongside vulnerabilities."""

from __future__ import annotations

import pytest

from sharp.server import db
from sharp.server.models import CreateVulnerabilityRequest
from sharp.server.routers import vulnerabilities as vulns_router
from sharp.server.services import utcnow


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_db_path", None, raising=False)
    db.configure(tmp_path / "sharp.db")
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO projects (id, title, status, created_at) VALUES ('p1','t','active',?)",
            (utcnow(),),
        )
        conn.execute(
            "INSERT INTO facts (id, project_id, description) VALUES ('f001','p1','o')",
        )
    yield


def _create(**kw):
    body = {"fact_id": "f001", "title": "x", "evidence": "poC", **kw}
    return vulns_router.create_vuln("p1", CreateVulnerabilityRequest(**body))


def test_default_kind_is_vuln(temp_db):
    v = _create(title="SQLi", severity="high")
    assert v.kind == "vuln"
    assert v.score == 0
    sb = vulns_router.project_scoreboard("p1")
    assert sb["by_kind"]["vuln"]["count"] == 1
    assert sb["flag_count"] == 0
    assert sb["flag_score"] == 0


def test_flag_entries_are_scored(temp_db):
    _create(title="flag #1", kind="flag", score=100, severity="info")
    _create(title="flag #2", kind="flag", score=250, severity="info")
    _create(title="race condition", severity="medium")
    sb = vulns_router.project_scoreboard("p1")
    assert sb["flag_count"] == 2
    assert sb["flag_score"] == 350
    assert sb["total_score"] == 350
    assert sb["by_kind"]["vuln"]["count"] == 1


def test_scoreboard_isolated_per_project(temp_db):
    _create(title="flag", kind="flag", score=50)
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO projects (id, title, status, created_at) VALUES ('p2','t2','active',?)",
            (utcnow(),),
        )
        conn.execute(
            "INSERT INTO facts (id, project_id, description) VALUES ('f001','p2','o')"
        )
    vulns_router.create_vuln(
        "p2", CreateVulnerabilityRequest(fact_id="f001", title="other", kind="flag", score=999)
    )
    assert vulns_router.project_scoreboard("p1")["flag_score"] == 50
    assert vulns_router.project_scoreboard("p2")["flag_score"] == 999


def test_create_rejects_unknown_kind(temp_db):
    with pytest.raises(Exception):
        _create(title="weird", kind="nonsense")
