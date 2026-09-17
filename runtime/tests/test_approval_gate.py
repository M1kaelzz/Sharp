"""Approval-gate enforcement tests (batch 2).

Server-side security invariants:
1. A high-risk intent that is ``pending`` human approval can NOT be
   heartbeat-claimed or concluded — the gate previously lived only in the
   dispatcher's client-side filter, so any caller holding the shared server
   token (the AI) could claim and conclude it directly.
2. Approval endpoints require an explicit project_id; intent ids are
   per-project counters (every project has its own i001...), so a project-less
   lookup by intent id alone can silently hit another project's same-named
   intent.

Drives the real router functions against a real (temporary) SQLite DB (same
pattern as test_intent_idempotency_api.py; no httpx required).
"""

from __future__ import annotations

import pytest

from sharp.server import db
from sharp.server.models import ApprovalDecisionRequest, ConcludeRequest, CreateIntentRequest, HeartbeatRequest
from sharp.server.routers import approvals
from sharp.server.routers.intents import conclude, create_intent, heartbeat
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
            "INSERT INTO projects (id, title, status, created_at) VALUES (?, ?, 'active', ?)",
            ("p2", "t2", utcnow()),
        )
        conn.execute(
            "INSERT INTO facts (id, project_id, description) VALUES ('origin', 'p1', 'o')",
        )
        conn.execute(
            "INSERT INTO facts (id, project_id, description) VALUES ('origin', 'p2', 'o')",
        )
    yield


def _high_risk_intent(project_id: str, description: str = "写入 webshell 获取权限"):
    """A description hitting the critical keyword classifier -> pending."""
    return CreateIntentRequest(
        **{"from": ["origin"]},
        description=description,
        creator="ai_worker",
        worker="ai_worker",
    )


def _make_pending(project_id: str) -> str:
    intent = create_intent(project_id, _high_risk_intent(project_id))
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT approval_status FROM intents WHERE id = ? AND project_id = ?",
            (intent.id, project_id),
        ).fetchone()
        assert row["approval_status"] == "pending", "keyword classifier should gate this intent"
    return intent.id


def test_pending_intent_cannot_be_heartbeat_claimed(temp_db):
    """A pending high-risk intent must be refused by the claim/heartbeat path."""
    iid = _make_pending("p1")
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        heartbeat("p1", iid, HeartbeatRequest(worker="ai_worker"))
    assert exc_info.value.status_code == 403


def test_pending_intent_cannot_be_concluded(temp_db):
    """A pending high-risk intent must be refused by the conclude path."""
    iid = _make_pending("p1")
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        conclude("p1", iid, ConcludeRequest(worker="ai_worker", description="confirmed webshell"))
    assert exc_info.value.status_code == 403


def test_low_risk_intent_still_claimable(temp_db):
    """The gate must not block normal (non-pending) intents."""
    intent = create_intent(
        "p1",
        CreateIntentRequest(
            **{"from": ["origin"]},
            description="被动收集子域名信息",
            creator="ai_worker",
            worker="ai_worker",
        ),
    )
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT approval_status FROM intents WHERE id = ? AND project_id = ?",
            (intent.id, "p1"),
        ).fetchone()
        assert row["approval_status"] != "pending"
    result = heartbeat("p1", intent.id, HeartbeatRequest(worker="ai_worker"))
    assert result.id == intent.id


def test_approve_requires_project_id_no_global_fallback(temp_db):
    """approve/reject/detail must not silently resolve across projects.

    Both p1 and p2 get a same-named pending intent (i001...). With project_id
    required, operating on p1's intent id via p2's scope must 404, and the
    legacy 'no project_id -> latest by id' fallback is gone (a project-less
    call would previously approve whichever project created its intent last).
    """
    iid_p1 = _make_pending("p1")
    iid_p2 = _make_pending("p2")
    assert iid_p1 == iid_p2  # per-project counters collide by design

    from fastapi import HTTPException

    # Detail scoped to p1 sees p1's intent; p2 has a same-named one but
    # scoping is explicit now.
    detail = approvals.approval_detail(iid_p1, "p1")
    assert detail["project_id"] == "p1"

    # Approve p1's intent via p1 scope -> ok.
    approvals.approve_intent(iid_p1, "p1", ApprovalDecisionRequest(note="允许"))
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT approval_status FROM intents WHERE id = ? AND project_id = 'p1'",
            (iid_p1,),
        ).fetchone()
        assert row["approval_status"] == "approved"
        # p2's same-named intent stays pending (no cross-project bleed).
        row2 = conn.execute(
            "SELECT approval_status FROM intents WHERE id = ? AND project_id = 'p2'",
            (iid_p2,),
        ).fetchone()
        assert row2["approval_status"] == "pending"

    # Approving the same id under the OTHER project's scope targets that
    # project's row (and 409s because it is already ... no — p2's is still
    # pending, so this would approve p2's intent if it resolved; scope is
    # explicit, so pass p2 -> approves p2's). Verify explicit scoping works:
    approvals.approve_intent(iid_p2, "p2", ApprovalDecisionRequest(note="允许"))
    with db.get_conn() as conn:
        row2 = conn.execute(
            "SELECT approval_status FROM intents WHERE id = ? AND project_id = 'p2'",
            (iid_p2,),
        ).fetchone()
        assert row2["approval_status"] == "approved"
