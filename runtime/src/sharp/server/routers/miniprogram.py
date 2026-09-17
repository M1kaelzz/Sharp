from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator

from sharp.server.db import get_conn
from sharp.server.repository import intents as intents_repo
from sharp.server.repository import projects as projects_repo
from sharp.server.miniprogram import (
    analyze_package,
    analyze_wechat_package_path,
    build_dynamic_ai_analysis_context,
    build_dynamic_ai_intent_description,
    build_dynamic_ai_project_seed,
    build_static_ai_analysis_context,
    build_static_ai_intent_description,
    build_static_ai_project_seed,
    parse_har,
    scan_wechat_packages,
    unpack_wechat_package_path,
)
from sharp.server.services import (
    check_project_active,
    intent_to_model,
    next_fact_id,
    next_intent_id,
    next_project_id,
    project_meta_from_row,
    utcnow,
)


MAX_UPLOAD_BYTES = 80 * 1024 * 1024

router = APIRouter(prefix="/miniprogram", tags=["miniprogram"])

_STATIC_CONTEXT_PREFIX = "# 小程序 wxapkg 静态分析上下文"


def _project_has_static_context(project_id: str) -> bool:
    """Return True if the project already has a wxapkg static-analysis fact.

    Used to decide whether to inject the static+dynamic cross-reference section
    into the HAR dynamic-analysis context so the AI can correlate both sources.
    """
    if not project_id:
        return False
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM facts WHERE project_id = ? AND description LIKE ?",
            (project_id, f"{_STATIC_CONTEXT_PREFIX}%"),
        ).fetchone()
        return bool(row and row[0] > 0)


class AnalyzePathRequest(BaseModel):
    path: str = Field(min_length=1, max_length=4096)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("path must not be empty")
        return text


class UnpackPathRequest(AnalyzePathRequest):
    output_dir: str = Field(default="", max_length=4096)
    decrypt: bool = True
    encrypt_key: str = Field(default="", max_length=64)

    @field_validator("output_dir")
    @classmethod
    def validate_output_dir(cls, value: str) -> str:
        return value.strip()

    @field_validator("encrypt_key")
    @classmethod
    def validate_encrypt_key(cls, value: str) -> str:
        return value.strip()


class DispatchStaticAnalysisRequest(AnalyzePathRequest):
    project_id: str = Field(default="", max_length=128)
    creator: str = Field(default="miniprogram.static", max_length=128)

    @field_validator("project_id", "creator")
    @classmethod
    def validate_optional_text(cls, value: str) -> str:
        return value.strip()


class ImportTrafficRequest(BaseModel):
    project_id: str = Field(min_length=1, max_length=128)
    creator: str = Field(default="miniprogram.dynamic", max_length=128)

    @field_validator("project_id", "creator")
    @classmethod
    def validate_optional_text(cls, value: str) -> str:
        text = value.strip()
        if not text and cls is ImportTrafficRequest:
            # project_id is required for import; creator falls back below.
            pass
        return text


class DispatchDynamicAnalysisRequest(BaseModel):
    project_id: str = Field(default="", max_length=128)
    creator: str = Field(default="miniprogram.dynamic", max_length=128)

    @field_validator("project_id", "creator")
    @classmethod
    def validate_optional_text(cls, value: str) -> str:
        return value.strip()


def _primary_har_host(result: dict[str, object]) -> str:
    """First domain of a parsed HAR as the miniprogram asset key."""
    domains = result.get("domains") or []
    for d in domains:
        host = str(d.get("host") or "") if isinstance(d, dict) else ""
        if host:
            return host
    return ""


def _seed_analysis_project(
    *,
    project_id: str,
    creator: str,
    fact_description: str,
    intent_description: str,
    seed: dict[str, str],
    analysis: dict[str, object],
    target_kind: str = "miniprogram",
    asset_ref: str = "",
) -> dict[str, object]:
    """Shared project-seeding flow used by both static and dynamic dispatch.

    Creates a project (origin+goal facts) when project_id is empty, then appends
    the analysis fact and an intent sourced from it. Mirrors the original inline
    static-analysis logic so behaviour is identical for both paths.
    """
    with get_conn() as conn:
        now = utcnow()
        created_project = False
        if project_id:
            project_row = check_project_active(conn, project_id)
        else:
            created_project = True
            project_id = next_project_id(conn)
            projects_repo.insert(conn, project_id, seed["title"], now,
                                 target_kind=target_kind, asset_ref=asset_ref)
            conn.execute(
                "INSERT INTO facts (id, project_id, description) VALUES (?, ?, ?)",
                ("origin", project_id, seed["origin"]),
            )
            conn.execute(
                "INSERT INTO facts (id, project_id, description) VALUES (?, ?, ?)",
                ("goal", project_id, seed["goal"]),
            )
            project_row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
            assert project_row is not None

        fact_id = next_fact_id(conn, project_id)
        intent_id = next_intent_id(conn, project_id)
        conn.execute(
            "INSERT INTO facts (id, project_id, description) VALUES (?, ?, ?)",
            (fact_id, project_id, fact_description),
        )

        # 分析事实也是证据：与主链同一套副作用（台账 + 跨项目知识）。
        # 此前这些入口什么都不产出 —— 移动端发现的主机本可被后续项目复用。
        from sharp.server.fact_hooks import after_fact_write

        after_fact_write(
            conn, project_id=project_id, fact_id=fact_id,
            description=fact_description, now=now,
        )
        intents_repo.insert(
            conn,
            intent_id,
            project_id,
            to_fact_id=None,
            description=intent_description,
            creator=creator,
            worker=None,
            last_heartbeat_at=None,
            created_at=now,
            concluded_at=None,
        )
        intents_repo.add_source(conn, intent_id, project_id, fact_id)
        intent_row = intents_repo.fetch(conn, intent_id, project_id)
        assert intent_row is not None

        return {
            "project": project_meta_from_row(project_row),
            "project_id": project_id,
            "created_project": created_project,
            "fact": {"id": fact_id, "description_preview": fact_description[:500]},
            "intent": intent_to_model(conn, intent_row, project_id),
            "analysis": analysis,
        }


@router.get("/wxapkg/scan")
def scan_wxapkg(limit: int = Query(default=300, ge=1, le=500)):
    return scan_wechat_packages(limit)


@router.post("/wxapkg/analyze")
async def analyze_wxapkg(
    request: Request,
    filename: str = Query(default="upload.wxapkg", max_length=255),
    app_id: str = Query(default="", max_length=64, description="V1MMWX 加密包的 AppID（用于内置解密，可选）"),
):
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "上传文件超过 80MB")

    data = await request.body()
    if not data:
        raise HTTPException(400, "上传内容为空")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "上传文件超过 80MB")

    try:
        return analyze_package(filename, data, app_id=app_id.strip() or None)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/wxapkg/analyze-path")
def analyze_wxapkg_path(body: AnalyzePathRequest):
    try:
        return analyze_wechat_package_path(body.path)
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(404, f"文件不存在：{exc}") from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/wxapkg/unpack-path")
def unpack_wxapkg_path(body: UnpackPathRequest):
    try:
        return unpack_wechat_package_path(
            body.path,
            body.output_dir or None,
            decrypt=body.decrypt,
            encrypt_key=body.encrypt_key or None,
        )
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(404, f"文件不存在：{exc}") from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/wxapkg/dispatch-static-analysis", status_code=201)
def dispatch_static_analysis(body: DispatchStaticAnalysisRequest):
    try:
        result = analyze_wechat_package_path(body.path)
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(404, f"文件不存在：{exc}") from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    creator = body.creator or "miniprogram.static"
    return _seed_analysis_project(
        project_id=body.project_id,
        creator=creator,
        fact_description=build_static_ai_analysis_context(result, body.path),
        intent_description=build_static_ai_intent_description(result),
        seed=build_static_ai_project_seed(result, body.path),
        target_kind="miniprogram",
        asset_ref=str((result.get("package") or {}).get("app_id") or ""),
        analysis={
            "filename": result.get("filename", ""),
            "format": result.get("format", ""),
            "package": result.get("package", {}),
            "domain_count": len(result.get("domains") or []),
            "endpoint_count": len(result.get("endpoints") or []),
            "finding_count": len(result.get("findings") or []),
        },
    )


@router.post("/traffic/import", status_code=201)
async def import_har_traffic(
    request: Request,
    filename: str = Query(default="upload.har", max_length=255),
    project_id: str = Query(min_length=1, max_length=128),
    creator: str = Query(default="miniprogram.dynamic", max_length=128),
):
    """Inject a HAR capture as a new analysis fact into an existing active project.

    The HAR is parsed, an analysis fact is built and appended to the project, and
    an intent is created so the dispatcher can immediately start dynamic testing.
    """
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "上传文件超过 80MB")

    data = await request.body()
    if not data:
        raise HTTPException(400, "上传内容为空")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "上传文件超过 80MB")

    try:
        result = parse_har(data)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    source = filename
    pid = project_id.strip()
    has_static = _project_has_static_context(pid)
    return _seed_analysis_project(
        project_id=pid,
        creator=creator.strip() or "miniprogram.dynamic",
        fact_description=build_dynamic_ai_analysis_context(result, source, has_static_context=has_static),
        intent_description=build_dynamic_ai_intent_description(result),
        seed=build_dynamic_ai_project_seed(result, source),
        target_kind="miniprogram",
        asset_ref=_primary_har_host(result),
        analysis={
            "filename": source,
            "format": "har",
            "counts": result.get("counts", {}),
            "auth": result.get("auth", {}),
        },
    )


@router.post("/traffic/dispatch-dynamic-analysis", status_code=201)
async def dispatch_dynamic_analysis(
    request: Request,
    filename: str = Query(default="upload.har", max_length=255),
    project_id: str = Query(default="", max_length=128),
    creator: str = Query(default="miniprogram.dynamic", max_length=128),
):
    """Upload a HAR and create a new project (or append to an existing one) in one step.

    Equivalent to calling /traffic/import but creates the project automatically
    when project_id is omitted, so the full "upload → analyse → test" flow
    completes in a single request.
    """
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "上传文件超过 80MB")

    data = await request.body()
    if not data:
        raise HTTPException(400, "上传内容为空")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "上传文件超过 80MB")

    import asyncio

    # Heavy parse/seed work off the event loop (batch A4).
    def _run():
        try:
            result = parse_har(data)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        source = filename
        pid = project_id.strip()
        # only True if appending to existing project with wxapkg fact
        has_static = _project_has_static_context(pid)
        return _seed_analysis_project(
            project_id=pid,
            creator=creator.strip() or "miniprogram.dynamic",
            fact_description=build_dynamic_ai_analysis_context(result, source, has_static_context=has_static),
            intent_description=build_dynamic_ai_intent_description(result),
            seed=build_dynamic_ai_project_seed(result, source),
            target_kind="miniprogram",
            asset_ref=_primary_har_host(result),
            analysis={
                "filename": source,
                "format": "har",
                "counts": result.get("counts", {}),
                "auth": result.get("auth", {}),
            },
        )

    return await asyncio.to_thread(_run)
