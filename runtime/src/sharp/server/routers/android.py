from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator

from sharp.server.android import (
    APK_SOURCE_FACT_ID,
    analyze_apk_path,
    build_apk_source_fact,
    build_static_ai_analysis_context,
    build_static_ai_intent_description,
    build_static_ai_project_seed,
    list_directory,
)
from sharp.server.db import get_conn
from sharp.server.repository import intents as intents_repo
from sharp.server.services import (
    check_project_active,
    intent_to_model,
    next_fact_id,
    next_intent_id,
    next_project_id,
    project_meta_from_row,
    utcnow,
)

router = APIRouter(prefix="/android", tags=["android"])

# APK/XAPK 上传大小上限（600MB，与 upload_apk 的 413 文案一致）。此前遗漏定义，
# 导致带 content-length 的真实浏览器上传在 upload_apk 首行即 NameError → 500。
MAX_UPLOAD_BYTES = 600 * 1024 * 1024

# 文件名清洁：非 [字母数字._-] 一律替换为下划线，防路径穿越。
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]")


class AnalyzePathRequest(BaseModel):
    path: str = Field(min_length=1, max_length=4096)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("path must not be empty")
        return text


class DispatchStaticAnalysisRequest(AnalyzePathRequest):
    project_id: str = Field(default="", max_length=128)
    creator: str = Field(default="android.static", max_length=128)

    @field_validator("project_id", "creator")
    @classmethod
    def validate_optional_text(cls, value: str) -> str:
        return value.strip()


def _analyze_or_http(path: str) -> dict:
    try:
        return analyze_apk_path(path)
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(404, f"文件不存在：{exc}") from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/fs/list")
def fs_list(path: str = Query(default="", max_length=4096)):
    """Browse directories and APK/XAPK files within the user's home dir."""
    try:
        return list_directory(path)
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(404, f"目录不存在：{exc}") from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/apk/analyze-path")
def analyze_apk(body: AnalyzePathRequest):
    result = _analyze_or_http(body.path)
    # Return a compact view; the full context blob is only built at dispatch time.
    return {
        "filename": result.get("filename", ""),
        "source_path": result.get("source_path", ""),
        "format": result.get("format", ""),
        "frameworks": result.get("frameworks", []),
        "package": result.get("package", {}),
        "domains": result.get("domains", []),
        "endpoints": result.get("endpoints", []),
        "native_libs": result.get("native_libs", []),
        "secrets_count": len(result.get("secrets") or []),
        "secrets": result.get("secrets", []),
        "counts": {
            "domains": len(result.get("domains") or []),
            "endpoints": len(result.get("endpoints") or []),
            "urls": len(result.get("urls") or []),
            "native_libs": len(result.get("native_libs") or []),
            "files": len(result.get("files") or []),
        },
        "notes": result.get("notes", []),
    }


@router.post("/apk/dispatch-static-analysis", status_code=201)
def dispatch_static_analysis(body: DispatchStaticAnalysisRequest):
    result = _analyze_or_http(body.path)

    creator = body.creator or "android.static"
    fact_description = build_static_ai_analysis_context(result, result.get("source_path", body.path))
    intent_description = build_static_ai_intent_description(result)
    seed = build_static_ai_project_seed(result, result.get("source_path", body.path))

    with get_conn() as conn:
        now = utcnow()
        created_project = False
        if body.project_id:
            project_id = body.project_id
            project_row = check_project_active(conn, project_id)
        else:
            created_project = True
            project_id = next_project_id(conn)
            # Asset-centre metadata (batch 11.4): android targets group by
            # application package name.
            from sharp.server.repository import projects as projects_repo

            android_pkg = str((result.get("package") or {}).get("package_name") or "")
            projects_repo.insert(conn, project_id, seed["title"], now,
                                 target_kind="android", asset_ref=android_pkg)
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

        # Record the APK's server-side path in a fixed-id fact so the dispatcher
        # can inject the binary into the worker container before decompilation.
        # INSERT OR REPLACE: re-dispatching into an existing project refreshes it.
        apk_source_description = build_apk_source_fact(result, result.get("source_path", body.path))
        conn.execute(
            "INSERT OR REPLACE INTO facts (id, project_id, description) VALUES (?, ?, ?)",
            (APK_SOURCE_FACT_ID, project_id, apk_source_description),
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
            "analysis": {
                "filename": result.get("filename", ""),
                "format": result.get("format", ""),
                "package": result.get("package", {}),
                "frameworks": result.get("frameworks", []),
                "domain_count": len(result.get("domains") or []),
                "endpoint_count": len(result.get("endpoints") or []),
                "native_lib_count": len(result.get("native_libs") or []),
                "suspected_secret_count": len(result.get("secrets") or []),
            },
        }


@router.post("/apk/upload")
def _persist_and_analyze_apk(data: bytes, filename: str) -> dict:
    """Synchronous upload pipeline (batch A4): clean name, md5-dedupe write,
    shallow analysis. Runs inside asyncio.to_thread so a large upload can never
    freeze the event loop."""
    # 清洁文件名，避免路径穿越
    safe_name = _SAFE_NAME_RE.sub("_", Path(filename).name) or "upload.apk"
    if not safe_name.lower().endswith((".apk", ".xapk")):
        safe_name += ".apk"

    # 保存到 Sharp 数据目录，按类型分子目录（app / 未来的 miniprogram）。
    upload_dir = Path.home() / ".local" / "share" / "sharp" / "uploads" / "app"
    upload_dir.mkdir(parents=True, exist_ok=True)
    saved_path = upload_dir / safe_name

    # 如果同名文件内容相同则复用，内容不同则加短哈希区分（避免覆盖）
    import hashlib as _hashlib
    file_hash = _hashlib.md5(data).hexdigest()[:8]
    if saved_path.exists():
        existing_hash = _hashlib.md5(saved_path.read_bytes()).hexdigest()[:8]
        if existing_hash != file_hash:
            stem = Path(safe_name).stem
            suffix = Path(safe_name).suffix
            safe_name = f"{stem}_{file_hash}{suffix}"
            saved_path = upload_dir / safe_name
            saved_path.write_bytes(data)
        # else: 内容相同，复用已有文件，跳过写入
    else:
        saved_path.write_bytes(data)

    try:
        result = _analyze_or_http(str(saved_path))
    except HTTPException:
        saved_path.unlink(missing_ok=True)
        raise

    return {
        "saved_path": str(saved_path),
        "filename": result.get("filename", safe_name),
        "source_path": str(saved_path),
        "format": result.get("format", ""),
        "frameworks": result.get("frameworks", []),
        "package": result.get("package", {}),
        "domains": result.get("domains", []),
        "endpoints": result.get("endpoints", []),
        "native_libs": result.get("native_libs", []),
        "secrets_count": len(result.get("secrets") or []),
        "secrets": result.get("secrets", []),
        "counts": {
            "domains": len(result.get("domains") or []),
            "endpoints": len(result.get("endpoints") or []),
            "urls": len(result.get("urls") or []),
            "native_libs": len(result.get("native_libs") or []),
            "files": len(result.get("files") or []),
        },
        "notes": result.get("notes", []),
    }


async def upload_apk(
    request: Request,
    filename: str = Query(default="upload.apk", max_length=255),
):
    """上传 APK/XAPK 文件，保存到 Sharp 数据目录并返回分析结果 + 保存路径。
    保存路径可直接传给 /android/chat/sessions 用于后续 AI 对话和深度分析注入。

    Heavy work (md5 over the whole body, disk writes, shallow analysis) runs in
    a worker thread — the endpoint only consumes the request body on the loop.
    """
    import asyncio

    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "文件超过 600MB 上限")

    data = await request.body()
    if not data:
        raise HTTPException(400, "上传内容为空")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "文件超过 600MB 上限")

    return await asyncio.to_thread(_persist_and_analyze_apk, data, filename)
