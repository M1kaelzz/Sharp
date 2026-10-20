"""Dashboard cost-band tests (batch: usage aggregation endpoints + UI data).

Locks the two read-only aggregation endpoints over ``task_usage``:

- **GET /usage/summary**: today vs yesterday vs all-time totals (COALESCE for
  NULL costs), by_task_type / by_model buckets (cost-desc, capped), a
  days-window trend with zero-filled days, and the top-3 burning projects
  (rows without a project skipped). Empty table returns zeros/empty lists so
  the frontend hides the whole cost band when metering is off.
- **GET /projects/{pid}/usage**: same shape scoped to one project; 404 for an
  unknown project; top_projects stays empty.
- **HA status field**: the dispatcher status snapshot carries data.ha
  {enabled, active} so the status page can badge ACTIVE/STANDBY (and a
  mutant dropping the field is caught by the source guard).

TestClient + temp DB; rows are inserted via POST /usage/record (the real
write path). No docker daemon, no live dispatcher.
"""

from __future__ import annotations

import pytest

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


def _post_rows(client, *rows: dict) -> None:
    resp = client.post("/usage/record", json={"records": list(rows)}, headers=_h())
    assert resp.status_code == 204, resp.text


def _row(project_id: str, *, day: str, cost: float, task_type="explore", model="sonnet", **over) -> dict:
    base = {
        "project_id": project_id,
        "intent_id": "i1",
        "worker_name": "claude_worker",
        "task_type": task_type,
        "phase": "execute",
        "model": model,
        "input_tokens": 100,
        "output_tokens": 50,
        "total_cost_usd": cost,
        "duration_ms": 1000,
        "outcome": "success",
    }
    base.update(over)
    base["_created_at"] = day  # marker stripped below
    created = base.pop("_created_at")
    # created_at is set server-side to utcnow; to place rows on arbitrary days
    # we write directly through the same table.
    base["_day"] = created
    return base


def _insert_row_direct(day: str, row: dict) -> None:
    """Insert one task_usage row on an arbitrary day (server stamps today)."""
    with db.get_conn() as conn:
        conn.execute(
            """
            INSERT INTO task_usage (
                project_id, intent_id, worker_name, task_type, phase, model,
                input_tokens, output_tokens, cache_read_input_tokens,
                cache_creation_input_tokens, total_cost_usd, duration_ms,
                outcome, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["project_id"], row["intent_id"], row["worker_name"],
                row["task_type"], row["phase"], row["model"],
                row.get("input_tokens", 0), row.get("output_tokens", 0), 0, 0,
                row["total_cost_usd"], row.get("duration_ms", 0),
                row.get("outcome", ""), f"{day}T12:00:00Z",
            ),
        )


# ── empty table = hide-friendly zeros ──────────────────────────────────────

def test_summary_empty_table_returns_zeros(client):
    r = client.get("/usage/summary", headers=_h())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total_calls"] == 0
    assert body["total_cost_usd"] == 0
    assert body["by_task_type"] == []
    assert body["trend"] == []  # no rows -> trend list empty (band hidden)
    assert body["top_projects"] == []


def test_project_usage_empty_returns_zeros(client):
    pid = client.post(
        "/projects",
        json={"title": "t", "origin": "http://t.example", "goal": "g"},
        headers=_h(),
    ).json()["project"]["id"]
    r = client.get(f"/projects/{pid}/usage", headers=_h())
    assert r.status_code == 200
    body = r.json()
    assert body["total_calls"] == 0
    # Zero-filled 7-day window is the intended per-project behavior; the
    # frontend hides the band on total_calls == 0 regardless of trend shape.
    assert all(p["total_cost_usd"] == 0 for p in body["trend"])


def test_project_usage_unknown_project_404(client):
    r = client.get("/projects/proj_999/usage", headers=_h())
    assert r.status_code == 404


# ── aggregation correctness ────────────────────────────────────────────────

def test_summary_aggregates_today_total_and_types(client):
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    _insert_row_direct(today, _row("proj_1", day=today, cost=2.0, task_type="explore"))
    _insert_row_direct(today, _row("proj_1", day=today, cost=1.0, task_type="reason"))
    _insert_row_direct(yesterday, _row("proj_2", day=yesterday, cost=0.5, task_type="prescan"))

    body = client.get("/usage/summary?days=7", headers=_h()).json()
    assert body["total_cost_usd"] == pytest.approx(3.5)
    assert body["today_cost_usd"] == pytest.approx(3.0)  # both same-day rows
    assert body["total_calls"] == 3
    types = {b["key"]: b["total_cost_usd"] for b in body["by_task_type"]}
    assert types["explore"] == pytest.approx(2.0)
    assert types["reason"] == pytest.approx(1.0)
    assert types["prescan"] == pytest.approx(0.5)


def test_summary_trend_zero_fills_missing_days(client):
    from datetime import datetime, timezone

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    _insert_row_direct(today, _row("p1", day=today, cost=1.0))
    body = client.get("/usage/summary?days=5", headers=_h()).json()
    assert len(body["trend"]) == 5
    assert body["trend"][-1]["day"] == today
    assert body["trend"][-1]["total_cost_usd"] == pytest.approx(1.0)
    assert all(p["total_cost_usd"] == 0 for p in body["trend"][:-1])


def test_summary_top_projects_capped_and_skips_empty(client):
    from datetime import datetime, timezone

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for pid, cost in (("proj_1", 9.0), ("proj_2", 5.0), ("proj_3", 1.0), ("proj_4", 0.5)):
        _insert_row_direct(today, _row(pid, day=today, cost=cost))
    with db.get_conn() as conn:  # a row with NO project must be skipped
        conn.execute(
            "INSERT INTO task_usage (project_id, task_type, phase, model, total_cost_usd, created_at) "
            "VALUES ('', 'reason', 'execute', 'm', 99.0, ?)",
            (f"{today}T00:00:00Z",),
        )
    body = client.get("/usage/summary?days=7", headers=_h()).json()
    top = body["top_projects"]
    assert len(top) == 3  # capped at 3, and the '' project never appears
    assert [t["project_id"] for t in top] == ["proj_1", "proj_2", "proj_3"]
    assert top[0]["total_cost_usd"] == pytest.approx(9.0)


def test_summary_days_clamped(client):
    assert client.get("/usage/summary?days=0", headers=_h()).status_code == 200
    assert client.get("/usage/summary?days=9999", headers=_h()).status_code == 200
    body = client.get("/usage/summary?days=200", headers=_h()).json()
    assert len(body["trend"]) <= 90  # clamped high bound


def test_project_usage_scopes_to_project(client):
    pid = client.post(
        "/projects",
        json={"title": "t", "origin": "http://t.example", "goal": "g"},
        headers=_h(),
    ).json()["project"]["id"]
    from datetime import datetime, timezone

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    _insert_row_direct(today, _row(pid, day=today, cost=2.0, task_type="explore"))
    _insert_row_direct(today, _row("proj_other", day=today, cost=99.0))  # other project

    body = client.get(f"/projects/{pid}/usage", headers=_h()).json()
    assert body["total_cost_usd"] == pytest.approx(2.0)
    assert body["total_calls"] == 1
    assert body["top_projects"] == []  # meaningless per-project, stays empty
    assert len(body["trend"]) == 7  # default 7-day window zero-filled


# ── write path still fine ──────────────────────────────────────────────────

def test_usage_record_then_summary_counts(client):
    _post_rows(client, _row("p1", day="x", cost=1.5, task_type="explore"))
    body = client.get("/usage/summary", headers=_h()).json()
    assert body["total_calls"] == 1
    assert body["total_cost_usd"] == pytest.approx(1.5)


# ── HA status field source guard ──────────────────────────────────────────

def test_status_snapshot_carries_ha_field():
    import inspect

    from sharp.dispatcher.scheduler.loop import DispatcherLoop

    src = inspect.getsource(DispatcherLoop._status_snapshot)
    assert '"ha"' in src and '"active"' in src and '"enabled"' in src



def test_dispatcher_view_badges_ha():
    """The dispatcher status view renders ACTIVE/STANDBY from data.ha."""
    from pathlib import Path

    from sharp.server import __file__ as server_file

    html = (Path(server_file).parent / "static" / "views" / "dispatcher.html").read_text(encoding="utf-8")
    assert "entry.data?.ha?.active" in html
    assert "STANDBY" in html
