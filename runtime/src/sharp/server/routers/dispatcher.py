from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from sharp.server.db import get_conn
from sharp.server.models import DispatcherStatusEntry, DispatcherStatusUpdate
from sharp.server.services import utcnow

router = APIRouter(tags=["dispatcher"])


@router.get("/dispatcher/status", response_model=list[DispatcherStatusEntry])
def list_dispatcher_status():
    from datetime import datetime, timedelta, timezone

    with get_conn() as conn:
        # Prune stale rows first: dispatchers heartbeat every ~5s, so anything
        # older than 90s is a dead/zombie instance (crash or SIGKILL — graceful
        # shutdown removes its row via delete_self_status). Without this, every
        # restart accumulates a permanent ghost entry on the status page.
        try:
            cutoff = (datetime.now(timezone.utc) - timedelta(seconds=90)).strftime("%Y-%m-%dT%H:%M:%SZ")
            conn.execute(
                "DELETE FROM dispatcher_status WHERE updated_at < ?", (cutoff,)
            )
        except Exception:
            pass
        rows = conn.execute(
            "SELECT dispatcher_id, updated_at, data FROM dispatcher_status ORDER BY updated_at DESC"
        ).fetchall()
        return [
            DispatcherStatusEntry(
                dispatcher_id=row["dispatcher_id"],
                updated_at=row["updated_at"],
                data=_load_status_data(row["data"]),
            )
            for row in rows
        ]


@router.post("/dispatcher/status", response_model=DispatcherStatusEntry)
def update_dispatcher_status(body: DispatcherStatusUpdate):
    now = utcnow()
    data = json.dumps(body.data, ensure_ascii=False, separators=(",", ":"))
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO dispatcher_status (dispatcher_id, updated_at, data)
            VALUES (?, ?, ?)
            ON CONFLICT(dispatcher_id) DO UPDATE SET
                updated_at = excluded.updated_at,
                data = excluded.data
            """,
            (body.dispatcher_id, now, data),
        )
        return DispatcherStatusEntry(
            dispatcher_id=body.dispatcher_id,
            updated_at=now,
            data=body.data,
        )


@router.delete("/dispatcher/status/{dispatcher_id}", status_code=204)
def delete_dispatcher_status(dispatcher_id: str):
    """Remove a dispatcher status record (e.g. a stale/zombie dispatcher that no
    longer runs). The dispatcher_id is the value shown in the status list."""
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM dispatcher_status WHERE dispatcher_id = ?", (dispatcher_id,)
        )
        if cur.rowcount == 0:
            raise HTTPException(404, f"未找到 dispatcher：{dispatcher_id}")
    return None


def _load_status_data(raw: str) -> dict[str, Any]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {"error": "invalid dispatcher status payload"}
    return data if isinstance(data, dict) else {"value": data}


@router.post("/dispatcher/restart", status_code=200, include_in_schema=False)
def restart_dispatcher(request: Request):
    """Stop running dispatcher processes and instruct how to start a fresh one.

    Deliberately does NOT spawn a replacement: when the server was started by
    the ``./sharp`` launcher, a server-spawned dispatcher would be an orphan
    outside the launcher's supervision — the next ``./sharp`` run would then
    stack a second dispatcher (double dispatch, duplicate heartbeats, stale
    status rows). Kill + wait, and let the launcher bring the dispatcher back
    (restart ``./sharp`` once — configuration changes require a process restart
    anyway).
    """
    import time as _time

    def _pids() -> list[int]:
        try:
            result = subprocess.run(
                ["pgrep", "-f", "sharp dispatch"],
                capture_output=True, text=True, timeout=5,
            )
            return [int(p) for p in result.stdout.strip().splitlines() if p.strip().isdigit()]
        except Exception:
            return []

    signaled = 0
    for pid in _pids():
        try:
            os.kill(pid, signal.SIGTERM)
            signaled += 1
        except (ProcessLookupError, PermissionError):
            pass

    # Wait for the processes to actually exit (graceful shutdown can take
    # longer than a fixed sleep), so no old instance survives to overlap a
    # later launch and no stale heartbeat lingers.
    deadline = _time.time() + 8.0
    while _time.time() < deadline:
        if not _pids():
            break
        _time.sleep(0.3)
    remaining = _pids()

    if remaining:
        return {
            "signaled": signaled, "spawned": False,
            "message": f"已发出停止信号（{signaled} 个进程），但仍有 {len(remaining)} 个未退出"
                       "（pid " + ",".join(str(x) for x in remaining) + "）。请重启 ./sharp 完成。",
        }
    return {
        "signaled": signaled, "spawned": False, "action": "relaunch_launcher",
        "message": "Dispatcher 已停止。请重启 ./sharp 以拉起新的 Dispatcher"
                   "（配置改动需要进程重启才会加载）。",
    }


# ── live worker output ─────────────────────────────────────────────────────────

from pydantic import BaseModel as _BaseModel

class WorkerOutputRequest(_BaseModel):
    project_id: str
    intent_id: str = ""
    worker: str = ""
    text: str

@router.post("/dispatcher/worker-output", status_code=204, include_in_schema=False)
def worker_output(body: WorkerOutputRequest):
    """Called by the dispatcher to forward live container stdout to SSE subscribers."""
    if not body.project_id or not body.text.strip():
        return None
    from sharp.server.events import publish
    publish(body.project_id, "worker_output", {
        "intent_id": body.intent_id,
        "worker": body.worker,
        "text": body.text[-4096:],  # cap at 4KB per event
    })
    return None
