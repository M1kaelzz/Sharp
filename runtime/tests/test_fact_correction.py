"""Fact correction loop tests (batch 11.1).

A human rewrites a fact or marks it untrusted; the server must (a) persist the
change with the trusted flag, (b) append an audit entry to fact_edits with the
before/after state, (c) refuse to rewrite the origin fact, and (d) leave
idempotent no-op requests untouched.
"""

from __future__ import annotations

import pytest

from sharp.server import db
from sharp.server.models import FactCorrectRequest
from sharp.server.routers import facts as facts_router
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
            "INSERT INTO facts (id, project_id, description) VALUES ('origin', 'p1', 'http://a.example')",
        )
        conn.execute(
            "INSERT INTO facts (id, project_id, description) VALUES ('goal', 'p1', '拿下目标')",
        )
        conn.execute(
            "INSERT INTO facts (id, project_id, description) VALUES ('f1', 'p1', '原事实描述')",
        )
    yield


def _correct(project_id: str, fact_id: str, **kw) -> dict:
    return facts_router.correct_fact(project_id, fact_id, FactCorrectRequest(**kw))


def _edits(project_id: str, fact_id: str) -> list[dict]:
    return [e.model_dump() for e in facts_router.fact_edit_history(project_id, fact_id)]


def _fact(project_id: str, fact_id: str) -> dict:
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM facts WHERE id = ? AND project_id = ?", (fact_id, project_id)
        ).fetchone()
    return dict(row)


def test_rewrite_fact_marks_trusted_and_audits(temp_db):
    out = _correct("p1", "f1", description="人工修正后的事实", note="证据链更正")
    assert out.description == "人工修正后的事实"
    assert out.trusted is True

    row = _fact("p1", "f1")
    assert row["description"] == "人工修正后的事实"
    assert row["trusted"] == 1

    trail = _edits("p1", "f1")
    assert len(trail) == 1
    assert trail[0]["prev_description"] == "原事实描述"
    assert trail[0]["new_description"] == "人工修正后的事实"
    assert trail[0]["prev_trusted"] is True and trail[0]["new_trusted"] is True
    assert trail[0]["note"] == "证据链更正"


def test_mark_untrusted_keeps_text_and_audits(temp_db):
    out = _correct("p1", "f1", untrusted=True, note="来源不可信，待复核")
    assert out.trusted is False
    assert _fact("p1", "f1")["description"] == "原事实描述"  # text untouched
    assert _fact("p1", "f1")["trusted"] == 0

    trail = _edits("p1", "f1")
    assert len(trail) == 1
    assert trail[0]["prev_trusted"] is True and trail[0]["new_trusted"] is False


def test_restore_clears_untrusted(temp_db):
    _correct("p1", "f1", untrusted=True)
    out = _correct("p1", "f1", untrusted=False, note="已复核，恢复可信")
    assert out.trusted is True
    trail = _edits("p1", "f1")
    assert len(trail) == 2
    assert trail[0]["prev_trusted"] is False and trail[0]["new_trusted"] is True


def test_rewrite_clears_previous_untrusted(temp_db):
    _correct("p1", "f1", untrusted=True)
    out = _correct("p1", "f1", description="复核后重写的内容")
    assert out.trusted is True
    trail = _edits("p1", "f1")
    assert len(trail) == 2
    assert trail[0]["prev_description"] == "原事实描述"
    assert trail[0]["new_description"] == "复核后重写的内容"
    assert trail[0]["new_trusted"] is True


def test_noop_does_not_audit(temp_db):
    out = _correct("p1", "f1", description="原事实描述", untrusted=False)
    assert out.trusted is True
    assert _edits("p1", "f1") == []


def test_origin_cannot_be_rewritten(temp_db):
    with pytest.raises(Exception) as exc:
        _correct("p1", "origin", description="http://evil.example")
    assert exc.value.status_code == 422
    assert _fact("p1", "origin")["description"] == "http://a.example"


def test_missing_fact_404(temp_db):
    with pytest.raises(Exception) as exc:
        _correct("p1", "ghost", description="x")
    assert exc.value.status_code == 404


def test_history_returns_newest_first(temp_db):
    _correct("p1", "f1", description="第一次")
    _correct("p1", "f1", untrusted=True, note="存疑")
    trail = _edits("p1", "f1")
    assert len(trail) == 2
    assert trail[0]["new_trusted"] is False  # newest first
    assert trail[1]["new_description"] == "第一次"
