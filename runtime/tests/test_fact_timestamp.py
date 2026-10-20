"""P2 fact timestamp tests (batch: 10, fact created_at).

Locks three layers:

- **schema/migration**: the ``facts`` table gains a first-hand ``created_at``
  column (TEXT NOT NULL DEFAULT '');
  - a fresh DB creates it via the schema,
  - a legacy DB (facts table without the column) gets it added by
    ``_migrate`` idempotently, old rows keeping '' (unknown).
- **write points**: every server-side fact write fills ``created_at`` with the
  server's utcnow() (project seed origin/goal, intent conclude) and the model
  round-trips it (Fact.created_at defaults to '', legacy payloads stay valid).
- **consumer** (``summarize_graph_yaml`` time ordering): with
  ``time_ordering=True`` + ``fact_times`` the legacy summarizer picks the
  "most recent" facts by their real timestamps instead of YAML list order;
  untimestamped facts count as oldest (all-legacy graph degrades to exactly the
  list-order selection); ``time_ordering=False`` output is byte-identical to
  the legacy path; origin/goal always stay full length; output keeps the input
  fact order (only WHICH descriptions get truncated changes).
- **mutant gate**: removing the time_ordering branch must fail the ordering
  tests; removing the wiring in reason/explore must fail the source guards.

Server tests use a temp DB + TestClient (same style as test_intent_dependency).
No docker daemon, no live server.
"""

from __future__ import annotations

import re
import sqlite3

import pytest
import yaml

from sharp.dispatcher.tasks.common import summarize_graph_yaml
from sharp.server import db
from sharp.server.models import Fact
from sharp.server.repository import facts as facts_repo

SERVER_TOKEN = "server-token-xyz"
TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


# ── shared fixtures (temp DB + TestClient) ─────────────────────────────────

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


def _create_project(client) -> str:
    r = client.post(
        "/projects",
        json={"title": "t", "origin": "http://target.example", "goal": "prove it"},
        headers=_h(),
    )
    assert r.status_code == 201, r.text
    return r.json()["project"]["id"]


def _facts_by_id(client, pid: str) -> dict[str, dict]:
    r = client.get(f"/projects/{pid}", headers=_h())
    assert r.status_code == 200, r.text
    return {f["id"]: f for f in r.json()["facts"]}


# ── 1. schema & migration ──────────────────────────────────────────────────

def _columns(conn: sqlite3.Connection, table: str) -> dict[str, sqlite3.Row]:
    return {row["name"]: row for row in conn.execute(f"PRAGMA table_info({table})")}


def test_fresh_schema_has_created_at(env_db):
    with db.get_conn() as conn:
        cols = _columns(conn, "facts")
        assert "created_at" in cols
        assert cols["created_at"]["notnull"] == 1


def test_migrate_adds_created_at_to_legacy_facts_table(tmp_path, monkeypatch):
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE facts (
            id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            description TEXT NOT NULL,
            trusted INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (id, project_id)
        )
        """
    )
    conn.execute("INSERT INTO facts VALUES ('f001', 'proj_1', 'old fact without time', 1)")
    conn.commit()
    conn.close()

    monkeypatch.setattr(db, "_db_path", None, raising=False)
    db.configure(path)  # must ALTER the existing table, not crash

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    cols = _columns(conn, "facts")
    assert "created_at" in cols
    # Legacy rows keep '' (unknown), never a fabricated timestamp.
    row = conn.execute("SELECT created_at FROM facts WHERE id = 'f001'").fetchone()
    assert row["created_at"] == ""
    conn.close()

    # Idempotent: configure again does not fail.
    db.configure(path)


# ── 2. write points fill created_at ────────────────────────────────────────

def test_repo_insert_with_and_without_created_at(env_db):
    with db.get_conn() as conn:
        from sharp.server.repository import projects as projects_repo

        projects_repo.insert(conn, "proj_1", "p", "2026-10-20T00:00:00Z")
        facts_repo.insert(conn, "f001", "proj_1", "with time", created_at="2026-10-20T01:02:03Z")
        facts_repo.insert(conn, "f002", "proj_1", "without time")
        rows = {r["id"]: r for r in conn.execute("SELECT * FROM facts")}
    assert rows["f001"]["created_at"] == "2026-10-20T01:02:03Z"
    assert rows["f002"]["created_at"] == ""


def test_project_seed_facts_get_created_at(client):
    pid = _create_project(client)
    facts = _facts_by_id(client, pid)
    for fid in ("origin", "goal"):
        assert facts[fid]["created_at"], f"{fid} missing created_at"
        assert TS_RE.match(facts[fid]["created_at"]), facts[fid]["created_at"]


def test_conclude_writes_fact_created_at(client):
    pid = _create_project(client)
    r = client.post(
        f"/projects/{pid}/intents",
        json={"from": ["origin"], "description": "read robots.txt", "creator": "ai_worker", "worker": "ai_worker"},
        headers=_h(),
    )
    assert r.status_code == 201, r.text
    iid = r.json()["id"]
    done = client.post(
        f"/projects/{pid}/intents/{iid}/conclude",
        json={"worker": "ai_worker", "description": "robots.txt lists no secrets"},
        headers=_h(),
    )
    assert done.status_code == 200, done.text
    facts = _facts_by_id(client, pid)
    produced = [f for f in facts.values() if f["description"] == "robots.txt lists no secrets"]
    assert produced, "concluded fact missing from project detail"
    assert TS_RE.match(produced[0]["created_at"]), produced[0]


def test_fact_model_created_at_defaults_empty_for_legacy_payload():
    fact = Fact(id="f1", description="legacy payload", trusted=True)
    assert fact.created_at == ""


# ── 3. summarize_graph_yaml time ordering ─────────────────────────────────

LONG = "x" * 50  # longer than truncate_chars=20
TIMES = {
    "f1": "2026-01-03T00:00:00Z",  # newest
    "f2": "2026-01-01T00:00:00Z",  # oldest
    "f3": "2026-01-02T00:00:00Z",  # middle
    "f4": "",                      # legacy row, unknown
}


def _graph() -> str:
    data = {
        "project": {"title": "t", "origin": "o", "goal": "g"},
        "facts": [
            {"id": "origin", "description": LONG},
            {"id": "goal", "description": LONG},
            {"id": "f1", "description": LONG},
            {"id": "f2", "description": LONG},
            {"id": "f3", "description": LONG},
            {"id": "f4", "description": LONG},
        ],
    }
    return yaml.dump(data, allow_unicode=True, default_flow_style=False, sort_keys=False)


def _desc(graph_yaml: str) -> dict[str, str]:
    return {f["id"]: f["description"] for f in yaml.safe_load(graph_yaml)["facts"]}


def _summarize(graph_yaml: str, *, time_ordering: bool = False):
    return summarize_graph_yaml(
        graph_yaml,
        fact_threshold=3,
        keep_recent=2,
        truncate_chars=20,
        time_ordering=time_ordering,
        fact_times=TIMES if time_ordering else None,
    )


def test_time_ordering_keeps_real_newest_truncates_older():
    out = _summarize(_graph(), time_ordering=True)
    d = _desc(out)
    # Newest by timestamp: f1 (01-03), f3 (01-02) — kept full length even
    # though they are early in the list order.
    assert d["f1"] == LONG
    assert d["f3"] == LONG
    # Older timestamped f2 and untimestamped f4 are truncated.
    assert d["f2"].endswith("[truncated]")
    assert d["f4"].endswith("[truncated]")


def test_race_window_fact_absent_from_times_is_newest():
    """Audit fix A3: a fact present in the YAML but ABSENT from fact_times was
    created in the race window between the project-facts fetch and the export
    fetch — it must rank as the NEWEST, never as oldest. The pre-fix code
    lumped it with legacy '' rows and truncated the graph's newest fact."""
    data = {
        "facts": [
            {"id": "origin", "description": LONG},
            {"id": "goal", "description": LONG},
            {"id": "f1", "description": LONG},   # TIMES: newest stamped (01-03)
            {"id": "f2", "description": LONG},   # TIMES: oldest stamped (01-01)
            {"id": "f4", "description": LONG},   # TIMES: legacy ''
            {"id": "f_racy", "description": LONG},  # NOT in TIMES -> race window
        ]
    }
    graph = yaml.dump(data, allow_unicode=True, default_flow_style=False, sort_keys=False)
    out = summarize_graph_yaml(
        graph,
        fact_threshold=3,
        keep_recent=2,
        truncate_chars=20,
        time_ordering=True,
        fact_times=TIMES,
    )
    d = _desc(out)
    # keep_recent=2 -> the race-window fact (newest) + f1 (newest stamped) stay.
    assert d["f_racy"] == LONG
    assert d["f1"] == LONG
    assert d["f2"].endswith("[truncated]")
    assert d["f4"].endswith("[truncated]")  # '' in map = genuinely legacy-old


def test_legacy_empty_ts_in_map_still_ranks_oldest():
    """'' inside the map is a genuine pre-batch-10 row: still ranked oldest
    (only ABSSENCE from the map means race-window-new, per audit fix A3)."""
    times = {"f1": "2026-01-03T00:00:00Z", "f2": ""}
    data = {
        "facts": [
            {"id": "origin", "description": LONG},
            {"id": "goal", "description": LONG},
            {"id": "f1", "description": LONG},
            {"id": "f2", "description": LONG},
        ]
    }
    graph = yaml.dump(data, allow_unicode=True, default_flow_style=False, sort_keys=False)
    out = summarize_graph_yaml(
        graph, fact_threshold=1, keep_recent=1, truncate_chars=20,
        time_ordering=True, fact_times=times,
    )
    d = _desc(out)
    assert d["f1"] == LONG
    assert d["f2"].endswith("[truncated]")


def test_time_ordering_off_is_legacy_list_order():
    out = _summarize(_graph(), time_ordering=False)
    d = _desc(out)
    # Legacy: keeps the LAST keep_regular facts by list order (f3, f4),
    # truncates the ones before them (f1, f2).
    assert d["f1"].endswith("[truncated]")
    assert d["f2"].endswith("[truncated]")
    assert d["f3"] == LONG
    assert d["f4"] == LONG


def test_time_ordering_matches_legacy_when_no_timestamps():
    global TIMES
    old = TIMES
    TIMES = {"f1": "", "f2": "", "f3": "", "f4": ""}
    try:
        time_out = _summarize(_graph(), time_ordering=True)
        legacy_out = summarize_graph_yaml(
            _graph(), fact_threshold=3, keep_recent=2, truncate_chars=20
        )
        assert _desc(time_out) == _desc(legacy_out)
    finally:
        TIMES = old


def test_time_ordering_false_byte_identical_to_plain_call():
    graph = _graph()
    kw = {"fact_threshold": 3, "keep_recent": 2, "truncate_chars": 20}
    plain = summarize_graph_yaml(graph, **kw)
    # Passing fact_times WITHOUT time_ordering must not change anything.
    explicit = summarize_graph_yaml(graph, time_ordering=False, fact_times=TIMES, **kw)
    assert plain == explicit


def test_time_ordering_keeps_origin_goal_full_and_fact_order():
    out = _summarize(_graph(), time_ordering=True)
    data = yaml.safe_load(out)
    ids = [f["id"] for f in data["facts"]]
    # Output order unchanged (only which descriptions are truncated changes).
    assert ids == ["origin", "goal", "f1", "f2", "f3", "f4"]
    d = _desc(out)
    assert d["origin"] == LONG
    assert d["goal"] == LONG


def test_time_ordering_below_threshold_returns_input():
    graph = _graph()
    out = _summarize(graph, time_ordering=True)  # 4 regular > threshold 3 → truncates
    assert out != graph
    few = yaml.dump(
        {"facts": [{"id": "origin", "description": "o"}, {"id": "goal", "description": "g"},
                   {"id": "f1", "description": LONG}]},
        allow_unicode=True,
    )
    assert _summarize(few, time_ordering=True) == few  # below threshold → unchanged


# ── 4. wiring & config default ─────────────────────────────────────────────

def test_runtime_config_flag_defaults_off():
    from sharp.dispatcher.config import RuntimeConfig

    assert RuntimeConfig.model_fields["fact_time_ordering"].default is False


def test_dispatcher_call_sites_wired():
    import inspect
    from sharp.dispatcher.tasks import explore, reason

    for module in (reason, explore):
        src = inspect.getsource(module)
        assert "config.runtime.fact_time_ordering" in src, module.__name__
        assert "time_ordering=True" in src, module.__name__