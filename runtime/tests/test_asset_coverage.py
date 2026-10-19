"""Asset coverage loop tests (batch A1): concluded facts register endpoints,
create_project injects a coverage hint, /assets reports ledger counts, and the
ledger query endpoint returns rows."""

from __future__ import annotations

import pytest

from sharp.server import asset_endpoints as ae
from sharp.server import db
from sharp.server.models import CreateProjectRequest
from sharp.server.routers import assets as assets_router
from sharp.server.routers import projects as projects_router
from sharp.server.services import utcnow


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_db_path", None, raising=False)
    db.configure(tmp_path / "sharp.db")
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO projects (id, title, status, created_at, target_kind, asset_ref) "
            "VALUES ('p1', '老项目A', 'completed', ?, 'web', 'a.example.com')",
            (utcnow(),),
        )
        conn.execute(
            "INSERT INTO facts (id, project_id, description) VALUES ('origin','p1','https://a.example.com')",
        )
        # ledger pre-populated from the "old campaign"
        ae.register_endpoints_from_fact(
            conn, project_id="p1", fact_id="f9",
            description="发现 https://a.example.com/api/users 与 https://a.example.com/api/orders",
            now=utcnow(),
        )
    yield


def _create_new_web(client_like, origin: str) -> list:
    r = projects_router.create_project(
        CreateProjectRequest(title="新项目", origin=origin, goal="验证目标")
    )
    return r.hints


def test_conclude_registers_endpoints_through_real_flow(temp_db):
    """End-to-end-ish: the same register call the conclude hook makes."""
    with db.get_conn() as conn:
        ae.register_endpoints_from_fact(
            conn, project_id="p1", fact_id="fa",
            description="https://a.example.com/api/users 越权验证通过", now=utcnow(),
        )
    rows = assets_router.list_asset_endpoints(asset_ref="a.example.com", status=None)
    assert len(rows) >= 2
    paths = {r["path"] for r in rows}
    assert "/api/users" in paths and "/api/orders" in paths
    assert all(r["asset_ref"] == "a.example.com" for r in rows)


def test_new_campaign_gets_coverage_hint(temp_db):
    hints = _create_new_web(None, "https://a.example.com/login")
    coverage = [h for h in hints if h.creator == "asset_coverage"]
    assert len(coverage) == 1
    content = coverage[0].content
    assert "a.example.com" in content
    assert "2" in content            # 2 registered endpoints
    assert "/api/users" in content
    assert "老项目A" in content      # source project title visible


def test_new_campaign_unknown_asset_no_coverage_hint(temp_db):
    hints = _create_new_web(None, "https://other.example.net/")
    assert not [h for h in hints if h.creator == "asset_coverage"]


def test_assets_groups_carry_endpoint_counts(temp_db):
    groups = projects_router.list_assets()
    g = next(x for x in groups if x.asset_ref == "a.example.com")
    assert g.endpoint_total == 2
    assert g.endpoint_todo == 2  # nothing assessed yet
