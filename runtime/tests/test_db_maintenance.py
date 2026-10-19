"""DB health tests (batch DB-health): orphan approval-event cleanup on project
delete, and gated startup vacuum (cadence + freelist trigger)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from sharp.server import db
from sharp.server import db_maintenance as mnt
from sharp.server.repository import projects as projects_repo
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
            "INSERT INTO facts (id, project_id, description) VALUES ('origin','p1','o')",
        )
        conn.execute(
            "INSERT INTO intents (id, project_id, description, creator, created_at) "
            "VALUES ('i1','p1','x','w',?)",
            (utcnow(),),
        )
        conn.execute(
            "INSERT INTO approval_events (intent_id, project_id, action, note, created_at) "
            "VALUES ('i1','p1','approved','',?)",
            (utcnow(),),
        )
    yield tmp_path / "sharp.db"


def test_delete_project_purges_orphan_approval_events(temp_db):
    with db.get_conn() as conn:
        assert conn.execute(
            "SELECT COUNT(*) c FROM approval_events WHERE project_id='p1'"
        ).fetchone()["c"] == 1
        projects_repo.delete(conn, "p1")
    with db.get_conn() as conn:
        assert conn.execute(
            "SELECT COUNT(*) c FROM approval_events WHERE project_id='p1'"
        ).fetchone()["c"] == 0
        # cascade still works for the FK-backed tables
        assert conn.execute(
            "SELECT COUNT(*) c FROM facts WHERE project_id='p1'"
        ).fetchone()["c"] == 0
        assert conn.execute(
            "SELECT COUNT(*) c FROM intents WHERE project_id='p1'"
        ).fetchone()["c"] == 0


def _state(conn) -> str | None:
    row = conn.execute(
        "SELECT value FROM maintenance_state WHERE key='last_vacuum_at'"
    ).fetchone()
    return row["value"] if row else None


def test_vacuum_runs_when_never_run(temp_db):
    result = mnt.run_startup_maintenance(temp_db)
    assert result["vacuumed"] is True
    with db.get_conn() as conn:
        assert _state(conn) is not None


def test_vacuum_skipped_within_cadence(temp_db):
    mnt.run_startup_maintenance(temp_db)
    again = mnt.run_startup_maintenance(temp_db)
    assert again["vacuumed"] is False
    assert again["reason"].startswith("not_due")


def test_vacuum_runs_again_after_cadence(temp_db):
    mnt.run_startup_maintenance(temp_db)
    old = (datetime.now(timezone.utc) - timedelta(days=40)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with db.get_conn() as conn:
        conn.execute("UPDATE maintenance_state SET value = ? WHERE key = 'last_vacuum_at'", (old,))
    result = mnt.run_startup_maintenance(temp_db)
    assert result["vacuumed"] is True


def test_vacuum_triggered_by_freelist_threshold(temp_db, monkeypatch):
    mnt.run_startup_maintenance(temp_db)  # first run, state now fresh
    # Manufacture a freelist: bulk-insert then delete rows (no VACUUM yet).
    with db.get_conn() as conn:
        for i in range(4000):
            conn.execute(
                "INSERT INTO facts (id, project_id, description) VALUES (?, 'p1', 'x')",
                (f"bulk{i}",),
            )
        conn.execute("DELETE FROM facts WHERE id LIKE 'bulk%'")
    monkeypatch.setattr(mnt, "_VACUUM_INTERVAL_SECONDS", 10 ** 9)  # not cadence-due
    monkeypatch.setattr(mnt, "_FREELIST_TRIGGER_BYTES", 4096)      # tiny threshold
    result = mnt.run_startup_maintenance(temp_db)
    assert result["vacuumed"] is True  # freelist threshold overrides cadence


def test_maintenance_never_raises_on_missing_file(tmp_path):
    result = mnt.run_startup_maintenance(tmp_path / "nope.db")
    assert result["vacuumed"] is False
    assert "error" in result["reason"]
