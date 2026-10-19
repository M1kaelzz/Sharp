"""Pre-completion acceptance inventory tests (batch 11.2)."""

from __future__ import annotations

import pytest

from sharp.server import db
from sharp.server.routers import projects
from sharp.server.services import utcnow


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_db_path", None, raising=False)
    db.configure(tmp_path / "sharp.db")
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO projects (id, title, status, created_at) VALUES (?, ?, 'active', ?)",
            ("p1", "t", utcnow()),
        )
        conn.execute(
            "INSERT INTO facts (id, project_id, description) VALUES ('origin', 'p1', 'o')",
        )
        conn.execute(
            "INSERT INTO facts (id, project_id, description, trusted) VALUES ('f1', 'p1', 'x', 0)",
        )
        conn.execute(
            "INSERT INTO intents (id, project_id, description, creator, created_at, to_fact_id) "
            "VALUES ('i1', 'p1', 'open intent', 'ai', ?, NULL)",
            (utcnow(),),
        )
        conn.execute(
            "INSERT INTO intents (id, project_id, description, creator, created_at, to_fact_id, "
            "risk_level, approval_status) VALUES ('i2', 'p1', '高危待批', 'ai', ?, NULL, 'high', 'pending')",
            (utcnow(),),
        )
        conn.execute(
            "INSERT INTO vulnerabilities (id, project_id, fact_id, title, severity, status, created_at) "
            "VALUES ('v1', 'p1', 'f1', 'SQLi', 'critical', 'pending', ?)",
            (utcnow(),),
        )
        conn.execute(
            "INSERT INTO vulnerabilities (id, project_id, fact_id, title, severity, status, created_at) "
            "VALUES ('v2', 'p1', 'f1', '已确认 RCE', 'high', 'confirmed', ?)",
            (utcnow(),),
        )
        conn.execute(
            "INSERT INTO vulnerabilities (id, project_id, fact_id, title, severity, status, created_at) "
            "VALUES ('v3', 'p1', 'f1', '已忽略', 'high', 'dismissed', ?)",
            (utcnow(),),
        )
    yield


def test_inventory_counts_open_work(temp_db):
    chk = projects.acceptance_check("p1")
    assert chk.has_open_work is True
    assert [i.id for i in chk.open_intents] == ["i1"]
    assert [i.id for i in chk.pending_approvals] == ["i2"]
    assert [i.id for i in chk.untrusted_facts] == ["f1"]
    # critical pending counts, confirmed counted separately, dismissed ignored
    assert [i.id for i in chk.unconfirmed_high_vulns] == ["v1"]
    assert chk.confirmed_high_vulns == 1


def test_inventory_clear_project(temp_db):
    # conclude the open intent, approve/clear pending, trust the fact, confirm vuln
    with db.get_conn() as conn:
        conn.execute("UPDATE facts SET trusted = 1 WHERE id = 'f1'")
        conn.execute(
            "UPDATE intents SET to_fact_id = 'f1', concluded_at = ?, worker = 'w', last_heartbeat_at = ? "
            "WHERE id = 'i1'",
            (utcnow(), utcnow()),
        )
        conn.execute(
            "UPDATE intents SET approval_status = 'approved', to_fact_id = 'f1', concluded_at = ?, "
            "worker = 'w', last_heartbeat_at = ? WHERE id = 'i2'",
            (utcnow(), utcnow()),
        )
        conn.execute("UPDATE vulnerabilities SET status = 'confirmed' WHERE id = 'v1'")
    chk = projects.acceptance_check("p1")
    assert chk.has_open_work is False
    assert chk.open_intents == []
    assert chk.pending_approvals == []
    assert chk.untrusted_facts == []
    assert chk.unconfirmed_high_vulns == []
    assert chk.confirmed_high_vulns == 2


def test_inventory_404_for_unknown_project(temp_db):
    with pytest.raises(Exception) as exc:
        projects.acceptance_check("ghost")
    assert exc.value.status_code == 404
