from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator

from sharp.server.db import get_conn
from sharp.server.models import ReportExportResponse
from sharp.server.reports import (
    _load_findings,
    _safe_name,
    create_engineered_report_draft_intent,
    export_latest_engineered_report,
    export_or_queue_engineered_report,
    reports_root,
)
from sharp.server.routers.export import _export_timeline, _export_yaml
from sharp.server.services import get_project_or_404

router = APIRouter(tags=["reports"])


class EngineerReportRequest(BaseModel):
    creator: str = Field(default="report.engineer", max_length=128)

    @field_validator("creator")
    @classmethod
    def validate_creator(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("creator must not be empty")
        return text


@router.post("/projects/{project_id}/reports/engineer", status_code=201)
def engineer_report(project_id: str, body: EngineerReportRequest):
    with get_conn() as conn:
        try:
            draft = create_engineered_report_draft_intent(conn, project_id, creator=body.creator)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {
            "project_id": draft.project_id,
            "fact_id": draft.fact_id,
            "intent_id": draft.intent_id,
            "context_chars": draft.context_chars,
            "intent": draft.intent,
        }


@router.post("/projects/{project_id}/reports/export-engineered")
def export_engineered_report(project_id: str, confirm_high_risk: bool = Query(default=False)):
    with get_conn() as conn:
        # Pre-check: if high-risk findings exist, require explicit confirmation
        # before writing the report file to disk.
        findings = _load_findings(conn, project_id)
        if findings and not confirm_high_risk:
            return {
                "status": "pending_confirm",
                "high_risk": True,
                "finding_count": len(findings),
                "project_id": project_id,
            }
        try:
            result = export_latest_engineered_report(conn, project_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
        return ReportExportResponse(
            project_id=result.project_id,
            report_dir=str(result.report_dir),
            report_path=str(result.report_path),
            finding_count=result.finding_count,
            packet_count=result.packet_count,
            high_risk=result.high_risk,
        )


@router.post("/projects/{project_id}/reports/ai")
def ai_report(project_id: str, body: EngineerReportRequest, regenerate: bool = Query(default=False), confirm_high_risk: bool = Query(default=False)):
    with get_conn() as conn:
        try:
            action = export_or_queue_engineered_report(conn, project_id, creator=body.creator, force_regenerate=regenerate)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        if action.status == "exported" and action.report is not None:
            result = action.report
            # High-risk gate: require explicit confirmation before returning
            # the exported report details to the caller.
            if result.high_risk and not confirm_high_risk:
                return {
                    "status": "pending_confirm",
                    "high_risk": True,
                    "finding_count": result.finding_count,
                    "project_id": result.project_id,
                }
            return {
                "status": "exported",
                "project_id": result.project_id,
                "report_dir": str(result.report_dir),
                "report_path": str(result.report_path),
                "finding_count": result.finding_count,
                "packet_count": result.packet_count,
                "high_risk": result.high_risk,
            }
        if action.status == "queued" and action.draft is not None:
            return {
                "status": "queued",
                "project_id": action.project_id,
                "fact_id": action.draft.fact_id,
                "intent_id": action.draft.intent_id,
                "context_chars": action.draft.context_chars,
            }
        return {
            "status": "generating",
            "project_id": action.project_id,
            "intent_id": action.open_intent_id,
        }


@router.get("/projects/{project_id}/reports/latest/download")
def download_latest_report(project_id: str):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
    project_dir = reports_root() / _safe_name(project_id)
    latest_path = project_dir / "engineered-report-latest.md"
    if not latest_path.exists():
        raise HTTPException(404, "尚未生成 AI 报告，请先点击「生成报告」并等待完成")
    filename = f"report-{project_id}.md"
    return FileResponse(
        latest_path,
        media_type="text/markdown; charset=utf-8",
        filename=filename,
    )


@router.post("/projects/{project_id}/reports/debug-export")
def debug_export(project_id: str):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        project_dir = reports_root() / _safe_name(project_id)
        project_dir.mkdir(parents=True, exist_ok=True)
        graph_path = project_dir / "graph.yaml"
        timeline_path = project_dir / "timeline.txt"
        graph_content = _export_yaml(conn, project_id)
        timeline_content = _export_timeline(conn, project_id)
        graph_path.write_text(graph_content, encoding="utf-8")
        timeline_path.write_text(timeline_content, encoding="utf-8")
        return {
            "project_id": project_id,
            "graph_path": str(graph_path),
            "timeline_path": str(timeline_path),
        }
