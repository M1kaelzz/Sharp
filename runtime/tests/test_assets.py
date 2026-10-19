"""Asset-centre tests (batch 11.4): target_kind/asset_ref population, web host
extraction, and the /assets grouping endpoint."""

from __future__ import annotations

import pytest

from sharp.server import db
from sharp.server.models import CreateProjectRequest
from sharp.server.routers import projects
from sharp.server.services import extract_web_asset_ref, utcnow


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_db_path", None, raising=False)
    db.configure(tmp_path / "sharp.db")
    with db.get_conn() as conn:
        # two projects sharing one host asset, one standalone
        for pid, origin in (
            ("p1", "https://www.example.com"),
            ("p2", "https://example.com/"),
            ("p3", "http://other.example.net:8080"),
            ("p4", "一句话目标没有域名"),  # uncategorized
        ):
            conn.execute(
                "INSERT INTO projects (id, title, status, created_at, target_kind, asset_ref) "
                "VALUES (?, ?, 'active', ?, 'web', ?)",
                (pid, pid, utcnow(), extract_web_asset_ref(origin)),
            )
            conn.execute(
                "INSERT INTO facts (id, project_id, description) VALUES ('origin', ?, ?)",
                (pid, origin),
            )
    yield


def _row(project_id: str) -> dict:
    with db.get_conn() as conn:
        r = conn.execute(
            "SELECT * FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
    return dict(r)


def test_extract_web_asset_ref_normalizes_hosts():
    assert extract_web_asset_ref("https://www.example.com/a?b=1") == "www.example.com"
    assert extract_web_asset_ref("http://example.com:8080/path") == "example.com"
    assert extract_web_asset_ref("target.example.org") == "target.example.org"
    # not parseable as a web asset → empty
    assert extract_web_asset_ref("一句话目标没有域名") == ""
    assert extract_web_asset_ref("") == ""


def test_bare_ip_is_a_valid_asset_ref():
    """裸 IP 现在算合法资产键（此前被拒）。

    旧口径自相矛盾：裸 `192.168.1.1` 返回空串，但 `http://192.168.1.1:8099/` 经 URL
    分支却放行 —— 库里就存在 `asset_ref='127.0.0.1'` 的行。而实测这次真实授权项目
    **全是 IP 目标**（10.0.172.232/233/234、10.0.100.58），拒绝 IP 等于让最需要
    资产台账的场景反而没有台账。
    """
    assert extract_web_asset_ref("192.168.1.1") == "192.168.1.1"
    assert extract_web_asset_ref("http://192.168.1.1:8099/") == "192.168.1.1"
    # 句子形式的 origin（真实格式）也要解析出主机，否则 asset_ref 为空、覆盖提示静默失效
    sentence = "目标地址：http://host.docker.internal:8099/\n授权范围：仅本机自建靶机"
    assert extract_web_asset_ref(sentence) == "host.docker.internal"


def test_create_project_defaults_web_and_derives_asset_ref(temp_db):
    # bare call: kind web + ref extracted from origin
    r = projects.create_project(
        CreateProjectRequest(title="t", origin="https://a.example.com:8443/admin", goal="g")
    )
    row = _row(r.project.id)
    assert row["target_kind"] == "web"
    assert row["asset_ref"] == "a.example.com"
    assert r.project.target_kind == "web"
    assert r.project.asset_ref == "a.example.com"


def test_create_project_explicit_kind_and_ref_win(temp_db):
    r = projects.create_project(
        CreateProjectRequest(
            title="t", origin="whatever", goal="g",
            target_kind="miniprogram", asset_ref="wx1234567890abcdef",
        )
    )
    row = _row(r.project.id)
    assert row["target_kind"] == "miniprogram"
    assert row["asset_ref"] == "wx1234567890abcdef"


def test_assets_grouping_and_counts(temp_db):
    groups = projects.list_assets()
    by_ref = {g.asset_ref: g for g in groups}
    # www.example.com and example.com are different strings but same org — the
    # grouping is by exact extracted host, so they stay separate (p1, p2 seeded
    # with different origins above: p1 → www.example.com, p2 → example.com).
    assert "www.example.com" in by_ref
    assert by_ref["www.example.com"].project_count == 1
    # uncategorized bucket exists for non-domain origins
    assert "" in by_ref
    assert by_ref[""].project_count == 1
    # every project lands exactly once
    total = sum(g.project_count for g in groups)
    assert total == 4


def test_seed_style_insert_carries_asset_metadata(temp_db):
    """miniprogram/android seed inserts go through projects_repo.insert with
    target_kind/asset_ref — exercise the repo path the routers now use."""
    from pathlib import Path

    import tempfile

    from sharp.server.repository import projects as projects_repo

    tmp = Path(tempfile.mkdtemp())
    with db.get_conn() as conn:
        projects_repo.insert(conn, "mp1", "小程序静态安全分析 - wxAAA", utcnow(),
                             target_kind="miniprogram", asset_ref="wxAAA")
        projects_repo.insert(conn, "ap1", "安卓 App 静态安全分析 - com.x", utcnow(),
                             target_kind="android", asset_ref="com.x")
    row = _row("mp1")
    assert row["target_kind"] == "miniprogram" and row["asset_ref"] == "wxAAA"
    assert _row("ap1")["target_kind"] == "android" and _row("ap1")["asset_ref"] == "com.x"
