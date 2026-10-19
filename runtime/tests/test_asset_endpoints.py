"""Asset endpoint ledger tests (batch A1): URL extraction heuristics,
registration upsert, web-project asset_ref backfill, and coverage counts."""

from __future__ import annotations

import pytest

from sharp.server import asset_endpoints as ae
from sharp.server import db
from sharp.server.services import utcnow


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_db_path", None, raising=False)
    db.configure(tmp_path / "sharp.db")
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO projects (id, title, status, created_at, target_kind, asset_ref) "
            "VALUES ('p1', 't', 'active', ?, 'web', 'a.example.com')",
            (utcnow(),),
        )
        conn.execute(
            "INSERT INTO projects (id, title, status, created_at, target_kind, asset_ref) "
            "VALUES ('p2', 't2', 'active', ?, 'web', '')",
            (utcnow(),),
        )
    yield


# ── extraction ────────────────────────────────────────────────────────────────

def test_extract_full_urls_with_paths():
    text = (
        "GET https://a.example.com/api/v1/users?id=1 返回 200；"
        "post https://a.example.com/admin/login（弱口令试探）；"
        "首页 https://a.example.com/ 与图片 https://a.example.com/logo.png 忽略"
    )
    got = ae.extract_endpoints_from_text(text)
    assert ("a.example.com", "/api/v1/users") in got
    assert ("a.example.com", "/admin/login") in got
    # bare "/" and static resources are skipped
    assert not any(p == "/" or p.endswith(".png") for _, p in got)


def test_extract_deduplicates_and_caps():
    text = " ".join(f"https://a.example.com/api/x{i}" for i in range(100))
    got = ae.extract_endpoints_from_text(text)
    assert len(got) == ae._MAX_PATHS_PER_FACT
    assert len({g for g in got}) == len(got)


def test_extract_ignores_plain_sentences():
    assert ae.extract_endpoints_from_text("登录接口无验证码，越权可读订单") == []
    assert ae.extract_endpoints_from_text("") == []


def test_extract_strips_scheme_port_and_trailing_punct():
    got = ae.extract_endpoints_from_text("看 https://A.Example.com:8080/api/me. 结果")
    assert got == [("a.example.com", "/api/me")]


# ── registration ──────────────────────────────────────────────────────────────

def test_register_upserts_and_counts(temp_db):
    now = utcnow()
    with db.get_conn() as conn:
        n = ae.register_endpoints_from_fact(
            conn, project_id="p1", fact_id="f1",
            description="发现 https://a.example.com/api/users 与 https://a.example.com/api/orders",
            now=now,
        )
        assert n == 2
        # same endpoint again → refreshed, not duplicated
        n2 = ae.register_endpoints_from_fact(
            conn, project_id="p1", fact_id="f2",
            description="复测 https://a.example.com/api/users 仍 200",
            now=now,
        )
        assert n2 == 1
    counts = _counts("a.example.com")
    assert counts["total"] == 2
    assert counts["todo"] == 2  # all discovered, none assessed


def _counts(asset_ref: str) -> dict:
    with db.get_conn() as conn:
        return ae.endpoint_counts_by_asset(conn, asset_ref)


def test_register_backfills_uncategorized_web_project(temp_db):
    with db.get_conn() as conn:
        ae.register_endpoints_from_fact(
            conn, project_id="p2", fact_id="f1",
            description="https://fresh.example.com/api/health 正常",
            now=utcnow(),
        )
        row = conn.execute(
            "SELECT asset_ref FROM projects WHERE id = 'p2'"
        ).fetchone()
    assert row["asset_ref"] == "fresh.example.com"
    assert _counts("fresh.example.com")["total"] == 1


def test_no_url_no_registration_and_no_backfill(temp_db):
    with db.get_conn() as conn:
        n = ae.register_endpoints_from_fact(
            conn, project_id="p2", fact_id="f1",
            description="结论：未发现可利用接口", now=utcnow(),
        )
        assert n == 0
        row = conn.execute("SELECT asset_ref FROM projects WHERE id = 'p2'").fetchone()
    assert row["asset_ref"] == ""  # untouched
