"""通用 AI 对话端点 — 与 Android 解耦，任何角色、无需 APK。

复用 sharp.server.android_chat 里的流式引擎（stream_llm / get_llm_config），
但会话存在独立的 chat_sessions / chat_messages 表，角色可配置（含工具向导）。
"""

from __future__ import annotations

import json
from typing import AsyncIterator

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from sharp.server.android_chat import (
    MAX_HISTORY_MESSAGES,
    build_generic_system_prompt,
    get_llm_config,
    new_session_id,
    stream_llm,
    utcnow_str,
)
from sharp.server.db import get_conn
from sharp.server.repository import facts as facts_repo
from sharp.server.repository import hints as hints_repo
from sharp.server.repository import intents as intents_repo

router = APIRouter(prefix="/chat", tags=["chat"])

# Cap per section so a project with many facts cannot blow the context window.
_MAX_PROJECT_CONTEXT_FACTS = 60
_MAX_PROJECT_CONTEXT_INTENTS = 40


# ─── Request / Response models ────────────────────────────────────────────────

class CreateSessionRequest(BaseModel):
    role: str = Field(default="assistant", max_length=64)
    context: str = Field(default="", max_length=16_000)
    project_id: str | None = Field(default=None, max_length=128)


class SendMessageRequest(BaseModel):
    content: str = Field(min_length=1, max_length=32_000)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _project_context_text(conn, project_id: str) -> str:
    """Build a compact, readable summary of a project's data for the LLM.

    Reads facts/intents/hints for the project and renders them as a structured
    text block the assistant can base answers on. Bounded per section.
    """
    facts = facts_repo.list_for_project(conn, project_id)
    intents = intents_repo.list_for_project(conn, project_id)
    hints = hints_repo.list_for_project(conn, project_id)

    def trunc(text: str, limit: int) -> str:
        return text if len(text) <= limit else text[:limit] + "…"

    parts = ["以下是该项目（%s）的证据—行动图数据，供你回答项目相关问题：" % project_id]
    parts.append("\n## Facts")
    for f in facts[:_MAX_PROJECT_CONTEXT_FACTS]:
        parts.append("- [%s] %s" % (f["id"], trunc(f["description"], 400)))
    if len(facts) > _MAX_PROJECT_CONTEXT_FACTS:
        parts.append("- ... 共 %s 条" % len(facts))
    parts.append("\n## Intents")
    for i in intents[:_MAX_PROJECT_CONTEXT_INTENTS]:
        status = "concluded" if i["concluded_at"] else "open"
        parts.append("- [%s] (%s) %s" % (i["id"], status, trunc(i["description"], 300)))
    if len(intents) > _MAX_PROJECT_CONTEXT_INTENTS:
        parts.append("- ... 共 %s 条" % len(intents))
    parts.append("\n## Hints")
    for h in hints:
        parts.append("- %s" % trunc(h["content"], 200))
    if not hints:
        parts.append("- 无")
    return "\n".join(parts)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _get_session_or_404(conn, session_id: str) -> dict:
    row = conn.execute(
        "SELECT * FROM chat_sessions WHERE id = ?", (session_id,)
    ).fetchone()
    if not row:
        raise HTTPException(404, f"会话不存在：{session_id}")
    return dict(row)


# DB retention per session (batch B1) — see repository/android_chat.py.
CHAT_RETENTION_PER_SESSION = 200


def _append_message(conn, session_id: str, role: str, content: str) -> None:
    """Insert one full chat message, then keep only the newest N per session."""
    conn.execute(
        "INSERT INTO chat_messages (session_id, role, content, created_at) "
        "VALUES (?, ?, ?, ?)",
        (session_id, role, content, utcnow_str()),
    )
    conn.execute(
        "DELETE FROM chat_messages WHERE session_id = ? AND id <= ("
        "SELECT id FROM chat_messages WHERE session_id = ? "
        "ORDER BY id DESC LIMIT 1 OFFSET ?)",
        (session_id, session_id, CHAT_RETENTION_PER_SESSION),
    )


def _get_history(conn, session_id: str, limit: int = MAX_HISTORY_MESSAGES) -> list[dict]:
    rows = conn.execute(
        "SELECT role, content FROM chat_messages "
        "WHERE session_id = ? ORDER BY id DESC LIMIT ?",
        (session_id, limit),
    ).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


# ─── Endpoints ────────────────────────────────────────────────────────────────

@router.get("/sessions")
def list_sessions():
    """列出所有通用 chat 会话，按最后活跃时间倒序。

    每条会话附带预览标题：取首条用户消息前 50 字符，便左侧栏展示。
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, role, project_id, created_at, last_active_at "
            "FROM chat_sessions ORDER BY last_active_at DESC LIMIT 50"
        ).fetchall()
        results = []
        for r in rows:
            d = dict(r)
            preview = conn.execute(
                "SELECT content FROM chat_messages "
                "WHERE session_id = ? AND role = 'user' "
                "ORDER BY id ASC LIMIT 1",
                (d["id"],),
            ).fetchone()
            d["preview"] = preview["content"][:50] if (preview and preview["content"]) else ""
            results.append(d)
    return {"sessions": results}


@router.post("/sessions", status_code=201)
def create_session(body: CreateSessionRequest):
    """创建一个通用对话会话，返回 session_id。"""
    session_id = new_session_id()
    now = utcnow_str()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO chat_sessions (id, role, context, project_id, created_at, last_active_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, body.role, body.context, body.project_id, now, now),
        )
        # 会话数量留存（P2-E）：消息条数早有上限，会话数此前没有 —— 更老的连消息一起删
        from sharp.server.chat_retention import prune_chat_sessions

        prune_chat_sessions(conn)
    return {"session_id": session_id, "role": body.role, "project_id": body.project_id}


@router.get("/sessions/{session_id}/messages")
def get_messages(session_id: str):
    """拉取会话历史消息（刷新页面后恢复用）。"""
    with get_conn() as conn:
        session = _get_session_or_404(conn, session_id)
        rows = conn.execute(
            "SELECT id, role, content, created_at FROM chat_messages "
            "WHERE session_id = ? ORDER BY id ASC",
            (session_id,),
        ).fetchall()
    return {
        "messages": [dict(r) for r in rows],
        "role": session["role"],
        "project_id": session.get("project_id"),
    }


@router.post("/sessions/{session_id}/stream")
async def stream_message(session_id: str, body: SendMessageRequest):
    """发送消息，以 SSE 流式返回 AI 回复。"""
    with get_conn() as conn:
        session = _get_session_or_404(conn, session_id)
        history = _get_history(conn, session_id)
        now = utcnow_str()
        _append_message(conn, session_id, "user", body.content)
        conn.execute(
            "UPDATE chat_sessions SET last_active_at = ? WHERE id = ?",
            (now, session_id),
        )

    messages = history + [{"role": "user", "content": body.content}]
    system_prompt = build_generic_system_prompt(session["role"])
    # If the session is bound to a project, inject its facts/intents/hints so the
    # assistant can answer questions about that project's findings.
    project_id = session.get("project_id")
    if project_id:
        with get_conn() as conn:
            proj = conn.execute(
                "SELECT id, title, status FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
            if proj:
                system_prompt += (
                    "\n\n用户当前关注项目。项目标题：%s，状态：%s。\n"
                    % (proj["title"], proj["status"])
                    + "\n\n## 项目数据\n\n"
                    + _project_context_text(conn, project_id)
                )
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
                    _append_message(conn, session_id, "assistant", reply_text)
            return
        except GeneratorExit:
            # 客户端断连：保存已收到的部分回复，避免用户消息有去无回
            if full_reply:
                reply_text = "".join(full_reply)
                with get_conn() as conn:
                    _append_message(conn, session_id, "assistant", reply_text)
            raise
        yield b"data: {\"type\": \"done\"}\n\n"
        if full_reply:
            reply_text = "".join(full_reply)
            with get_conn() as conn:
                _append_message(conn, session_id, "assistant", reply_text)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.delete("/sessions/{session_id}", status_code=204)
def delete_session(session_id: str):
    """彻底删除一个会话：连同其所有消息一起删除。"""
    with get_conn() as conn:
        _get_session_or_404(conn, session_id)
        conn.execute(
            "DELETE FROM chat_messages WHERE session_id = ?", (session_id,)
        )
        conn.execute(
            "DELETE FROM chat_sessions WHERE id = ?", (session_id,)
        )
    return None
