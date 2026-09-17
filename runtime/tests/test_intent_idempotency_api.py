"""Server-side gate tests for IDEMP-1: intent creation idempotency key.

Drives the real intents router function against a real (temporary) SQLite DB,
so the DB migration, the partial unique index, the fast-path lookup, and the
IntegrityError race fallback are all exercised end to end. Avoids TestClient so
no httpx dependency is required.
"""

from __future__ import annotations

import threading

import pytest

from sharp.server import db
from sharp.server.models import CreateIntentRequest
from sharp.server.routers.intents import create_intent
from sharp.server.services import utcnow


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    # Reset the module-global path guard so configure() rebuilds against a fresh
    # per-test database, then seed one active project with an 'origin' fact.
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
    yield


def _count_intents() -> int:
    with db.get_conn() as conn:
        return conn.execute("SELECT COUNT(*) c FROM intents WHERE project_id = 'p1'").fetchone()["c"]


def _req(key):
    return CreateIntentRequest(
        **{"from": ["origin"]}, description="probe web root", creator="w1", idempotency_key=key
    )


def test_migration_added_column_and_index(temp_db):
    with db.get_conn() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(intents)")}
        assert "idempotency_key" in cols
        idx = {r["name"] for r in conn.execute("PRAGMA index_list(intents)")}
        assert "ux_intents_idempotency" in idx


def test_same_key_returns_existing_no_duplicate(temp_db):
    first = create_intent("p1", _req("key-abc"))
    second = create_intent("p1", _req("key-abc"))
    assert first.id == second.id
    assert _count_intents() == 1  # retry did not create a duplicate


def test_different_keys_create_distinct_intents(temp_db):
    a = create_intent("p1", _req("key-a"))
    b = create_intent("p1", _req("key-b"))
    assert a.id != b.id
    assert _count_intents() == 2


def test_no_key_preserves_legacy_non_idempotent_behavior(temp_db):
    # Backward compat: without a key, each call still creates a new intent.
    create_intent("p1", _req(None))
    create_intent("p1", _req(None))
    assert _count_intents() == 2


def test_returned_intent_has_sources(temp_db):
    intent = create_intent("p1", _req("key-src"))
    assert intent.from_ == ["origin"]


def test_concurrent_same_key_single_row(temp_db):
    # Two threads racing the same key must collapse to one row (fast-path miss +
    # IntegrityError fallback on the unique index).
    barrier = threading.Barrier(2)
    results: list = []
    errors: list = []

    def worker():
        try:
            barrier.wait()
            results.append(create_intent("p1", _req("key-race")))
        except Exception as exc:  # noqa: BLE001 - surface any race failure
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    assert len(results) == 2
    assert results[0].id == results[1].id
    assert _count_intents() == 1
