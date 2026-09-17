"""Android App 对话式渗透测试 — REST 端点。"""

from __future__ import annotations

import json
from typing import AsyncIterator

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator

from sharp.server.android import analyze_apk_path, build_static_ai_analysis_context
from sharp.server.repository import android_chat as android_chat_repo
from sharp.server.repository import facts as facts_repo
from sharp.server.repository import hints as hints_repo
from sharp.server.repository import projects as projects_repo
from sharp.server.android_chat import (
    MAX_HISTORY_MESSAGES,
    build_system_prompt,
    get_llm_config,
    new_session_id,
    stream_llm,
    utcnow_str,
)
from sharp.server.android_dynamic import get_manager as get_dynamic_manager
from sharp.server.db import get_conn

router = APIRouter(prefix="/android/chat", tags=["android-chat"])


# ─── Request / Response models ────────────────────────────────────────────────

class CreateSessionRequest(BaseModel):
    apk_path: str = Field(min_length=1, max_length=4096)

    @field_validator("apk_path")
    @classmethod
    def clean(cls, v: str) -> str:
        return v.strip()


class SendMessageRequest(BaseModel):
    content: str = Field(min_length=1, max_length=32_000)


class PushFactRequest(BaseModel):
    project_id: str = Field(min_length=1, max_length=128)
    content: str = Field(min_length=1, max_length=32_000)
    kind: str = Field(default="hint")  # 'hint' | 'fact'

    @field_validator("kind")
    @classmethod
    def validate_kind(cls, v: str) -> str:
        if v not in ("hint", "fact"):
            raise ValueError("kind must be 'hint' or 'fact'")
        return v


class DynamicStartRequest(BaseModel):
    """开启动态调试会话：可附一个引导 prompt（缺省给通用开场）。"""
    prompt: str = Field(default="", max_length=32_000)
    device_info: str = Field(default="", max_length=512)


class DynamicMessageRequest(BaseModel):
    content: str = Field(min_length=1, max_length=32_000)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _get_session_or_404(conn, session_id: str) -> dict:
    row = android_chat_repo.get_session(conn, session_id)
    if not row:
        raise HTTPException(404, f"会话不存在：{session_id}")
    return dict(row)


def _get_history(conn, session_id: str, limit: int = MAX_HISTORY_MESSAGES) -> list[dict]:
    rows = android_chat_repo.list_history(conn, session_id, limit)
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


# ─── Endpoints ────────────────────────────────────────────────────────────────

@router.get("/sessions")
def list_sessions():
    """列出所有历史 chat 会话，按最后活跃时间倒序。"""
    with get_conn() as conn:
        rows = android_chat_repo.list_sessions(conn)
    return {"sessions": [dict(r) for r in rows]}


@router.post("/sessions", status_code=201)
def create_session(body: CreateSessionRequest):
    """分析 APK 并创建一个对话会话，返回 session_id。"""
    try:
        result = analyze_apk_path(body.apk_path)
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(404, f"文件不存在：{exc}") from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    apk_context = build_static_ai_analysis_context(result, body.apk_path)
    session_id = new_session_id()
    now = utcnow_str()

    with get_conn() as conn:
        android_chat_repo.insert_session(
            conn,
            session_id,
            body.apk_path,
            result.get("filename", ""),
            apk_context,
            now,
        )
        # 会话数量留存（P2-E），与通用聊天同一口径
        from sharp.server.chat_retention import prune_android_chat_sessions

        prune_android_chat_sessions(conn)

    return {
        "session_id": session_id,
        "apk_filename": result.get("filename", ""),
        "apk_path": body.apk_path,
        "analysis": {
            "format": result.get("format", ""),
            "frameworks": result.get("frameworks", []),
            "package": result.get("package", {}),
            "domain_count": len(result.get("domains") or []),
            "endpoint_count": len(result.get("endpoints") or []),
            "native_lib_count": len(result.get("native_libs") or []),
            "secrets_count": len(result.get("secrets") or []),
        },
        "static_result": {
            "domains": result.get("domains", []),
            "endpoints": result.get("endpoints", []),
            "native_libs": result.get("native_libs", []),
            "secrets_count": len(result.get("secrets") or []),
            "notes": result.get("notes", []),
        },
    }


@router.get("/sessions/{session_id}/messages")
def get_messages(session_id: str):
    """拉取会话历史消息（刷新页面后恢复用）。"""
    with get_conn() as conn:
        _get_session_or_404(conn, session_id)
        rows = android_chat_repo.list_messages(conn, session_id)
    return {"messages": [dict(r) for r in rows]}


@router.post("/sessions/{session_id}/stream")
async def stream_message(session_id: str, body: SendMessageRequest):
    """发送消息，以 SSE 流式返回 AI 回复。"""
    with get_conn() as conn:
        session = _get_session_or_404(conn, session_id)
        history = _get_history(conn, session_id)
        # 保存用户消息
        now = utcnow_str()
        android_chat_repo.add_message(conn, session_id, "user", body.content, now)
        android_chat_repo.touch_session(conn, session_id, now)

    messages = history + [{"role": "user", "content": body.content}]
    system_prompt = build_system_prompt(session["apk_context"])
    config = get_llm_config()

    if not config.get("auth_token"):
        raise HTTPException(400, "未配置 API Token，请在设置页填写 SHARP_ANTHROPIC_AUTH_TOKEN")

    async def generate() -> AsyncIterator[bytes]:
        full_reply: list[str] = []
        try:
            async for chunk in stream_llm(messages, system_prompt, config):
                full_reply.append(chunk)
                yield f"data: {json.dumps({'type': 'delta', 'text': chunk}, ensure_ascii=False)}\n\n".encode()
        except Exception as exc:
            err_msg = str(exc)
            yield f"data: {json.dumps({'type': 'error', 'message': err_msg}, ensure_ascii=False)}\n\n".encode()
            # 即使出错也保存已收到的部分回复
            if full_reply:
                reply_text = "".join(full_reply)
                with get_conn() as conn:
                    android_chat_repo.add_message(
                        conn, session_id, "assistant", reply_text, utcnow_str()
                    )
            return
        except GeneratorExit:
            # 客户端断连：保存已收到的部分回复，避免用户消息有去无回
            if full_reply:
                reply_text = "".join(full_reply)
                with get_conn() as conn:
                    android_chat_repo.add_message(
                        conn, session_id, "assistant", reply_text, utcnow_str()
                    )
            raise

        yield b"data: {\"type\": \"done\"}\n\n"

        # 保存完整 AI 回复到历史
        if full_reply:
            reply_text = "".join(full_reply)
            with get_conn() as conn:
                android_chat_repo.add_message(
                    conn, session_id, "assistant", reply_text, utcnow_str()
                )

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )



# ── 动态调试（dynamic mode）─────────────────────────────────────────────
# mode=dynamic 的会话绑定一个常驻 worker 容器，在容器里跑 claude-code
# （bash + adb/frida），逐轮续接同一 claude session，让 AI 真实连真机/hook。
# 与 /stream（无状态 LLM 问答）互斥：开 dynamic 后走 /dynamic/stream。

DEFAULT_DYNAMIC_OPENING = (
    "你是 Sharp 动态调试会话中的渗透 agent。目标 APK 已按上下文分析过。\n"
    "现在你在一个带 adb + frida 的容器里。第一步：\n"
    "1) 执行 adb devices 确认设备连通（或按 ADB_SERVER_SOCKET 提示连真机）；\n"
    "2) 简要说明你打算测什么（脱壳 / hook / 抓包），并先做只读侦察；\n"
    "3) 输出要简洁，含实际命令与结果摘要。"
)


@router.post("/sessions/{session_id}/dynamic/start")
def dynamic_start(session_id: str, body: DynamicStartRequest):
    """将会话切换到 dynamic 模式：创建（或复用）绑定容器与 claude 会话。

    幂等：已在 dynamic 模式则直接返回当前绑定。返回 container_name 与
    claude_session_id，前端据此展示设备状态。
    """
    from uuid import uuid4

    with get_conn() as conn:
        session = _get_session_or_404(conn, session_id)
        if session["mode"] == "dynamic" and session["container_name"]:
            return {
                "session_id": session_id,
                "mode": "dynamic",
                "container_name": session["container_name"],
                "claude_session_id": session["claude_session_id"],
                "device_info": session["device_info"],
                "already_running": True,
            }
        manager = get_dynamic_manager()
        container_name = manager.ensure_running(session_id)
        claude_sid = f"cdyn_{uuid4().hex[:16]}"
        now = utcnow_str()
        android_chat_repo.update_dynamic_state(
            conn, session_id,
            mode="dynamic",
            container_name=container_name,
            claude_session_id=claude_sid,
            device_info=body.device_info,
            now=now,
        )
        # 首轮引导消息落库（AI 侧）
        opening = body.prompt.strip() or DEFAULT_DYNAMIC_OPENING
        android_chat_repo.add_message(conn, session_id, "user", opening, now)

    # 触发首轮执行（同步流式：容器 exec，走线程池）
    def generate_first():
        from sharp.server.android_dynamic import get_manager as gm
        mgr = gm()
        full: list[str] = []
        try:
            for text in mgr.exec_claude_stream(container_name, opening, claude_sid, first_turn=True):
                full.append(text)
                yield f"data: {json.dumps({'type': 'delta', 'text': text}, ensure_ascii=False)}\n\n".encode()
        except Exception as exc:
            yield f"data: {json.dumps({'type': 'error', 'message': str(exc)}, ensure_ascii=False)}\n\n".encode()
        finally:
            # 落库 assistant 回复（首轮可能无完整 JSON，直接存文本）
            reply = "".join(full)
            if reply:
                with get_conn() as conn2:
                    android_chat_repo.add_message(conn2, session_id, "assistant", reply, utcnow_str())
        yield b"data: {\"type\": \"done\"}\n\n"

    return StreamingResponse(
        generate_first(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/sessions/{session_id}/dynamic/stream")
def dynamic_stream(session_id: str, body: DynamicMessageRequest):
    """在 dynamic 模式下续接容器内 claude 会话（SSE 流式）。"""
    with get_conn() as conn:
        session = _get_session_or_404(conn, session_id)
        if session["mode"] != "dynamic" or not session["container_name"]:
            raise HTTPException(409, "会话未处于动态调试模式，请先 start")
        container_name = session["container_name"]
        claude_sid = session["claude_session_id"]
        # 保存用户消息
        now = utcnow_str()
        android_chat_repo.add_message(conn, session_id, "user", body.content, now)
        android_chat_repo.touch_session(conn, session_id, now)
    mgr = get_dynamic_manager()
    mgr.touch(container_name)

    def generate():
        full: list[str] = []
        try:
            for text in mgr.exec_claude_stream(container_name, body.content, claude_sid, first_turn=False):
                full.append(text)
                yield f"data: {json.dumps({'type': 'delta', 'text': text}, ensure_ascii=False)}\n\n".encode()
        except Exception as exc:
            yield f"data: {json.dumps({'type': 'error', 'message': str(exc)}, ensure_ascii=False)}\n\n".encode()
        finally:
            reply = "".join(full)
            if reply:
                with get_conn() as conn2:
                    android_chat_repo.add_message(conn2, session_id, "assistant", reply, utcnow_str())
        yield b"data: {\"type\": \"done\"}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/sessions/{session_id}/dynamic/stop")
def dynamic_stop(session_id: str):
    """退出 dynamic 模式：销毁绑定容器（释放 adb/frida 会话），模式回 chat。"""
    with get_conn() as conn:
        session = _get_session_or_404(conn, session_id)
        container_name = session["container_name"] if session["mode"] == "dynamic" else None
        now = utcnow_str()
        android_chat_repo.clear_dynamic_state(conn, session_id, now)
    if container_name:
        mgr = get_dynamic_manager()
        try:
            mgr.remove(session_id)
        except Exception as exc:  # noqa: BLE001 - best-effort cleanup
            import logging
            logging.getLogger(__name__).warning(
                "dynamic container cleanup failed session=%s error=%s", session_id, exc
            )
    return {"session_id": session_id, "mode": "chat"}


@router.get("/sessions/{session_id}/dynamic/status")
def dynamic_status(session_id: str):
    """查询会话当前 dynamic 绑定（前端刷新恢复状态用）。"""
    with get_conn() as conn:
        session = _get_session_or_404(conn, session_id)
    return {
        "session_id": session_id,
        "mode": session["mode"],
        "container_name": session["container_name"],
        "claude_session_id": session["claude_session_id"],
        "device_info": session["device_info"],
    }


@router.delete("/sessions/{session_id}", status_code=204)
def clear_session(session_id: str):
    """清空会话消息（New Chat）。"""
    with get_conn() as conn:
        _get_session_or_404(conn, session_id)
        android_chat_repo.delete_messages(conn, session_id)
        android_chat_repo.touch_session(conn, session_id, utcnow_str())
    return None


@router.post("/sessions/{session_id}/push-fact", status_code=201)
def push_fact(session_id: str, body: PushFactRequest):
    """把 AI 回复里的发现写入项目证据图（线索 hint 或证据 fact）。"""
    with get_conn() as conn:
        _get_session_or_404(conn, session_id)

        project_row = projects_repo.fetch(conn, body.project_id)
        if not project_row:
            raise HTTPException(404, f"项目不存在：{body.project_id}")

        now = utcnow_str()
        if body.kind == "hint":
            # 写为 hint（人工提示）
            from sharp.server.services import next_hint_id  # type: ignore[attr-defined]
            try:
                hint_id = next_hint_id(conn, body.project_id)
            except AttributeError:
                import uuid as _uuid
                hint_id = f"h_{_uuid.uuid4().hex[:12]}"
            hints_repo.insert(
                conn, hint_id, body.project_id, body.content, "android.chat", now
            )
            return {"kind": "hint", "id": hint_id, "project_id": body.project_id}
        else:
            # 写为 fact
            from sharp.server.fact_hooks import after_fact_write
            from sharp.server.services import next_fact_id, utcnow

            fact_id = next_fact_id(conn, body.project_id)
            facts_repo.insert(conn, fact_id, body.project_id, body.content)
            # 与主链同一套副作用：从对话写进证据图的内容同样要进台账与知识库
            after_fact_write(
                conn, project_id=body.project_id, fact_id=fact_id,
                description=body.content, now=utcnow(),
            )
            return {"kind": "fact", "id": fact_id, "project_id": body.project_id}


