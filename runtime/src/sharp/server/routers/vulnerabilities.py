"""Vulnerability CRUD routes.

Vulnerabilities are created by the dispatcher (on behalf of AI workers)
when an explore task produces structured finding data, or manually by
users. They can be confirmed/dismissed by users.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from sharp.server.db import get_conn
from sharp.server.events import publish
from sharp.server.models import (
    CreateVulnerabilityRequest,
    UpdateVulnStatusRequest,
    VulnSeverityStats,
    VulnTrendPoint,
    Vulnerability,
)
from sharp.server.repository import vulnerabilities as vulns_repo
from sharp.server.services import (
    get_project_or_404,
    next_vuln_id,
    utcnow,
    vuln_to_model,
)

router = APIRouter(tags=["vulnerabilities"])

_SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}


@router.get(
    "/projects/{project_id}/vulnerabilities",
    response_model=list[Vulnerability],
)
def list_vulns(project_id: str):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        rows = vulns_repo.list_for_project(conn, project_id)
        return [vuln_to_model(r) for r in rows]


@router.post(
    "/projects/{project_id}/vulnerabilities",
    response_model=Vulnerability,
    status_code=201,
)
def create_vuln(project_id: str, body: CreateVulnerabilityRequest):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        # Verify the fact exists
        fact = conn.execute(
            "SELECT 1 FROM facts WHERE id = ? AND project_id = ?",
            (body.fact_id, project_id),
        ).fetchone()
        if fact is None:
            raise HTTPException(404, f"Fact {body.fact_id} not found")

        now = utcnow()
        vid = next_vuln_id(conn, project_id)

        # Lightweight verification: if evidence contains a curl command or
        # reproduction steps, auto-mark as 'confirmed'; otherwise 'pending'.
        has_evidence = bool(
            body.evidence.strip()
            or body.reproduction.strip()
        )
        status = "confirmed" if has_evidence else "pending"
        verified_at = now if has_evidence else None

        vulns_repo.insert(
            conn,
            vid,
            project_id,
            fact_id=body.fact_id,
            intent_id=body.intent_id,
            title=body.title,
            severity=body.severity,
            status=status,
            url=body.url,
            description=body.description,
            evidence=body.evidence,
            reproduction=body.reproduction,
            impact=body.impact,
            recommendation=body.recommendation,
            created_at=now,
            kind=body.kind,
            score=body.score,
            verified_at=verified_at,
        )
        result = vuln_to_model(
            vulns_repo.fetch(conn, vid, project_id)
        )
    publish(project_id, "vulnerability_created", {
        "id": vid,
        "title": body.title,
        "severity": body.severity,
        "status": status,
    })
    return result


@router.get(
    "/projects/{project_id}/vulnerabilities/{vuln_id}",
    response_model=Vulnerability,
)
def get_vuln(project_id: str, vuln_id: str):
    with get_conn() as conn:
        row = vulns_repo.fetch(conn, vuln_id, project_id)
        if row is None:
            raise HTTPException(404, "Vulnerability not found")
        return vuln_to_model(row)


@router.patch(
    "/projects/{project_id}/vulnerabilities/{vuln_id}",
    response_model=Vulnerability,
)
def update_vuln_status(project_id: str, vuln_id: str, body: UpdateVulnStatusRequest):
    with get_conn() as conn:
        row = vulns_repo.fetch(conn, vuln_id, project_id)
        if row is None:
            raise HTTPException(404, "Vulnerability not found")
        now = utcnow() if body.status == "confirmed" else None
        vulns_repo.update_status(conn, vuln_id, project_id, body.status, now)
        row = vulns_repo.fetch(conn, vuln_id, project_id)
        return vuln_to_model(row)


@router.delete(
    "/projects/{project_id}/vulnerabilities/{vuln_id}",
    status_code=204,
)
def delete_vuln(project_id: str, vuln_id: str):
    with get_conn() as conn:
        row = vulns_repo.fetch(conn, vuln_id, project_id)
        if row is None:
            raise HTTPException(404, "Vulnerability not found")
        vulns_repo.delete(conn, vuln_id, project_id)


@router.get(
    "/vulnerabilities",
    response_model=list[Vulnerability],
)
def list_all_vulns():
    """Cross-project vulnerability listing for the dashboard."""
    with get_conn() as conn:
        rows = vulns_repo.list_all(conn)
        return [vuln_to_model(r) for r in rows]


@router.get(
    "/vulnerabilities/stats",
    response_model=VulnSeverityStats,
)
def vuln_stats():
    """Cross-project severity distribution for the dashboard ring chart."""
    with get_conn() as conn:
        return vulns_repo.count_all(conn)


@router.get(
    "/projects/{project_id}/scoreboard",
)
def project_scoreboard(project_id: str):
    """Finding scoreboard for one project (batch B).

    Aggregates findings by kind and sums the score of `flag`-kind artifacts —
    the "how are we doing" view for scored tasks (CTF / benchmarks), where the
    product of the search is a flag rather than a vulnerability.
    """
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        rows = conn.execute(
            "SELECT kind, COUNT(*) AS n, COALESCE(SUM(score), 0) AS s "
            "FROM vulnerabilities WHERE project_id = ? GROUP BY kind",
            (project_id,),
        ).fetchall()
    by_kind = {r["kind"]: {"count": int(r["n"]), "score": int(r["s"])} for r in rows}
    flag = by_kind.get("flag", {"count": 0, "score": 0})
    return {
        "project_id": project_id,
        "by_kind": by_kind,
        "flag_count": flag["count"],
        "flag_score": flag["score"],
        "total_score": sum(v["score"] for v in by_kind.values()),
    }


@router.get(
    "/vulnerabilities/trend",
    response_model=list[VulnTrendPoint],
)
def vuln_trend(days: int = 14):
    """Cross-project vulnerability discovery trend (last N days, excludes dismissed)."""
    with get_conn() as conn:
        return vulns_repo.daily_counts(conn, days)
