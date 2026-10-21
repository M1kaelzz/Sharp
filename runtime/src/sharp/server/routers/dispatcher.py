from __future__ import annotations

import json
import os
import shutil
import signal
import sqlite3
import subprocess
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from sharp.server.db import get_conn
from sharp.server.models import (
    DispatcherStatusEntry,
    DispatcherStatusUpdate,
    TaskTraceRecord,
    TaskTraceRecords,
    TaskUsageRecords,
)
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


_COST_ALERT_SUBJECT = "【Sharp 成本异常】"


def _cost_alert_threshold() -> float:
    """Audit fix A1 (short-term): per-task cost threshold from operator env.

    Reuses the batch-9 alert channel — a task_usage row above the threshold
    pushes a sanitized alert (worker/model/phase/cost only), which is the early
    warning for a drained or stolen worker token (worker containers receive the
    API token via env, so a counter-RCE on the target can steal it and run up
    the bill). Unset / unparsable / <= 0 disables the check (default, legacy).
    """
    raw = os.environ.get("SHARP_ALERT_COST_PER_TASK", "").strip()
    if not raw:
        return 0.0
    try:
        return float(raw)
    except ValueError:
        return 0.0


def _alert_cost_anomaly(r, threshold: float) -> None:
    """Best-effort cost anomaly alert for one usage row; never raises."""
    from sharp.server.notifier import send_high_risk_alert

    try:
        send_high_risk_alert(
            r.project_id,
            {
                "title": (
                    f"任务成本 ${float(r.total_cost_usd):.2f} 超过阈值 ${threshold:.2f}"
                    f"（worker={r.worker_name} model={r.model} phase={r.phase}）"
                    "——疑似 token 被盗刷或超大上下文"
                ),
                "severity": "critical",  # flows the default SHARP_ALERT_SEVERITIES filter
                "kind": r.task_type or "task",
            },
            subject=_COST_ALERT_SUBJECT,
        )
    except Exception:
        pass  # never fail the usage write on an alert problem


@router.post("/usage/record", status_code=204)
def record_task_usage(body: TaskUsageRecords):
    """P1-3 real token metering: persist dispatcher-reported claude usage rows.

    Pure observability — nothing reads these rows to decide behavior, so a
    failure here must never surface to the task layer (the dispatcher client
    already treats the write as best-effort). Audit fix A1 (short-term): when
    SHARP_ALERT_COST_PER_TASK is set, rows above the threshold additionally
    push a best-effort alert through the batch-9 channel after the insert.
    """
    now = utcnow()
    with get_conn() as conn:
        conn.executemany(
            """
            INSERT INTO task_usage (
                project_id, intent_id, worker_name, task_type, phase, model,
                input_tokens, output_tokens, cache_read_input_tokens,
                cache_creation_input_tokens, total_cost_usd, duration_ms,
                outcome, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    r.project_id, r.intent_id, r.worker_name, r.task_type, r.phase, r.model,
                    r.input_tokens, r.output_tokens, r.cache_read_input_tokens,
                    r.cache_creation_input_tokens, r.total_cost_usd, r.duration_ms,
                    r.outcome, now,
                )
                for r in body.records
            ],
        )
    threshold = _cost_alert_threshold()
    if threshold > 0:
        for r in body.records:
            try:
                cost = float(r.total_cost_usd or 0.0)
            except (TypeError, ValueError):
                cost = 0.0
            if cost > threshold:
                _alert_cost_anomaly(r, threshold)
    return None


@router.post("/traces", status_code=204)
def record_task_traces(body: TaskTraceRecords):
    """P1-4 worker process-level exchange: persist dispatched task round rows.

    Written by the dispatcher at reap time (one row per task round). Reason
    reads them back per project to see failed rounds / dead directions across
    rounds. Pure observability — nothing else reads these rows.
    """
    now = utcnow()
    with get_conn() as conn:
        conn.executemany(
            """
            INSERT INTO task_traces (
                project_id, intent_id, worker_name, task_type, outcome,
                started_at, duration_ms, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    r.project_id, r.intent_id, r.worker_name, r.task_type, r.outcome,
                    r.started_at, r.duration_ms, now,
                )
                for r in body.records
            ],
        )
    return None


@router.get("/projects/{project_id}/traces", response_model=list[TaskTraceRecord])
def list_task_traces(project_id: str, limit: int = 5):
    """Recent task traces for a project, newest first (P1-4, reason reads this).

    `limit` is clamped to keep prompt injections bounded (default 5, max 20).
    """
    limit = max(1, min(limit, 20))
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT project_id, intent_id, worker_name, task_type, outcome, "
            "started_at, duration_ms FROM task_traces "
            "WHERE project_id = ? ORDER BY id DESC LIMIT ?",
            (project_id, limit),
        ).fetchall()
        return [
            TaskTraceRecord(
                project_id=row["project_id"],
                intent_id=row["intent_id"],
                worker_name=row["worker_name"],
                task_type=row["task_type"],
                outcome=row["outcome"],
                started_at=row["started_at"],
                duration_ms=row["duration_ms"],
            )
            for row in rows
        ]


# ── P1-3 usage aggregation (dashboard cost band) ──────────────────────────
# Aggregations over the task_usage rows the dispatcher reports. Read-only
# observability; an empty table simply returns zeros/empty lists so the
# frontend can hide the whole cost band when metering is off.


class _UsageRow(BaseModel):
    """One aggregated bucket (by task_type or by model)."""

    key: str
    total_cost_usd: float = 0
    input_tokens: int = 0
    output_tokens: int = 0
    calls: int = 0


class _UsageTrendPoint(BaseModel):
    day: str  # YYYY-MM-DD
    total_cost_usd: float = 0
    calls: int = 0


class _UsageTopProject(BaseModel):
    project_id: str
    total_cost_usd: float = 0
    calls: int = 0


class UsageSummary(BaseModel):
    """Global cost summary for the dashboard band (batch: cost band)."""

    today_cost_usd: float = 0
    yesterday_cost_usd: float = 0
    total_cost_usd: float = 0
    total_calls: int = 0
    by_task_type: list[_UsageRow] = []
    by_model: list[_UsageRow] = []
    trend: list[_UsageTrendPoint] = []
    top_projects: list[_UsageTopProject] = []


def _usage_summary_from_conn(conn: sqlite3.Connection, days: int) -> UsageSummary:
    """Aggregate task_usage rows. ``days`` bounds the trend/top windows; the
    totals are all-time (the operator wants "how much in total, ever")."""
    from datetime import datetime, timedelta, timezone

    def _day(dt: datetime) -> str:
        return dt.strftime("%Y-%m-%d")

    now = datetime.now(timezone.utc)
    today = _day(now)
    yesterday = _day(now - timedelta(days=1))
    window_start = _day(now - timedelta(days=days - 1))

    def _cost(expr: str) -> str:
        # COALESCE strips NULL costs (rows from gateways that return no usage).
        return f"COALESCE(SUM(COALESCE({expr}, 0)), 0)"

    scalar = conn.execute("SELECT created_at FROM task_usage ORDER BY id DESC LIMIT 1").fetchone()
    has_rows = scalar is not None

    today_cost = 0.0
    yesterday_cost = 0.0
    total_cost = 0.0
    total_calls = 0
    if has_rows:
        row = conn.execute(
            f"SELECT {_cost('total_cost_usd')} AS c FROM task_usage WHERE created_at LIKE ?",
            (f"{today}%",),
        ).fetchone()
        today_cost = float(row["c"] or 0)
        row = conn.execute(
            f"SELECT {_cost('total_cost_usd')} AS c FROM task_usage WHERE created_at LIKE ?",
            (f"{yesterday}%",),
        ).fetchone()
        yesterday_cost = float(row["c"] or 0)
        row = conn.execute(f"SELECT {_cost('total_cost_usd')} AS c, COUNT(*) AS n FROM task_usage").fetchone()
        total_cost = float(row["c"] or 0)
        total_calls = int(row["n"] or 0)

    def _buckets(group_by: str, limit: int) -> list[_UsageRow]:
        if not has_rows:
            return []
        rows = conn.execute(
            f"""
            SELECT COALESCE({group_by}, '') AS k,
                   {_cost('total_cost_usd')} AS c,
                   COALESCE(SUM(COALESCE(input_tokens, 0)), 0) AS it,
                   COALESCE(SUM(COALESCE(output_tokens, 0)), 0) AS ot,
                   COUNT(*) AS n
            FROM task_usage GROUP BY k ORDER BY c DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [
            _UsageRow(
                key=(r["k"] or "-"),
                total_cost_usd=round(float(r["c"] or 0), 4),
                input_tokens=int(r["it"] or 0),
                output_tokens=int(r["ot"] or 0),
                calls=int(r["n"] or 0),
            )
            for r in rows
        ]

    trend: list[_UsageTrendPoint] = []
    top: list[_UsageTopProject] = []
    if has_rows:
        rows = conn.execute(
            f"""
            SELECT substr(created_at, 1, 10) AS d,
                   {_cost('total_cost_usd')} AS c,
                   COUNT(*) AS n
            FROM task_usage
            WHERE substr(created_at, 1, 10) >= ?
            GROUP BY d ORDER BY d
            """,
            (window_start,),
        ).fetchall()
        by_day = {r["d"]: (float(r["c"] or 0), int(r["n"] or 0)) for r in rows}
        for i in range(days - 1, -1, -1):
            d = _day(now - timedelta(days=i))
            c, n = by_day.get(d, (0.0, 0))
            trend.append(_UsageTrendPoint(day=d, total_cost_usd=round(c, 4), calls=n))

        rows = conn.execute(
            f"""
            SELECT COALESCE(project_id, '') AS p,
                   {_cost('total_cost_usd')} AS c,
                   COUNT(*) AS n
            FROM task_usage
            WHERE substr(created_at, 1, 10) >= ? AND COALESCE(project_id, '') != ''
            GROUP BY p ORDER BY c DESC LIMIT 3
            """,
            (window_start,),
        ).fetchall()
        top = [
            _UsageTopProject(
                project_id=r["p"],
                total_cost_usd=round(float(r["c"] or 0), 4),
                calls=int(r["n"] or 0),
            )
            for r in rows
        ]

    return UsageSummary(
        today_cost_usd=round(today_cost, 4),
        yesterday_cost_usd=round(yesterday_cost, 4),
        total_cost_usd=round(total_cost, 4),
        total_calls=total_calls,
        by_task_type=_buckets("task_type", 8),
        by_model=_buckets("model", 8),
        trend=trend,
        top_projects=top,
    )


@router.get("/usage/summary", response_model=UsageSummary)
def usage_summary(days: int = 7):
    """Dashboard cost band aggregation (batch: cost band). days is clamped
    to [1, 90]; totals are all-time, trend/top use the window."""
    days = max(1, min(days, 90))
    with get_conn() as conn:
        return _usage_summary_from_conn(conn, days)


@router.get("/projects/{project_id}/usage", response_model=UsageSummary)
def project_usage_summary(project_id: str, days: int = 7):
    """Per-project cost detail (batch: cost band) — same shape as the global
    summary but restricted to one project (top_projects is meaningless here
    and stays empty)."""
    from sharp.server.services import get_project_or_404

    days = max(1, min(days, 90))
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        # Temporarily scope by project via a one-off query path: reuse the
        # aggregator against a filtered connection is complex, so aggregate
        # inline with the same helpers.
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        today = now.strftime("%Y-%m-%d")
        yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        window_start = (now - timedelta(days=days - 1)).strftime("%Y-%m-%d")

        def _cost(expr: str) -> str:
            return f"COALESCE(SUM(COALESCE({expr}, 0)), 0)"

        row = conn.execute(
            f"SELECT {_cost('total_cost_usd')} AS c, COUNT(*) AS n FROM task_usage WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        total_cost = float(row["c"] or 0)
        total_calls = int(row["n"] or 0)

        row = conn.execute(
            f"SELECT {_cost('total_cost_usd')} AS c FROM task_usage WHERE project_id = ? AND created_at LIKE ?",
            (project_id, f"{today}%"),
        ).fetchone()
        today_cost = float(row["c"] or 0)
        row = conn.execute(
            f"SELECT {_cost('total_cost_usd')} AS c FROM task_usage WHERE project_id = ? AND created_at LIKE ?",
            (project_id, f"{yesterday}%"),
        ).fetchone()
        yesterday_cost = float(row["c"] or 0)

        def _buckets(group_by: str, limit: int) -> list[_UsageRow]:
            rows = conn.execute(
                f"""
                SELECT COALESCE({group_by}, '') AS k,
                       {_cost('total_cost_usd')} AS c,
                       COALESCE(SUM(COALESCE(input_tokens, 0)), 0) AS it,
                       COALESCE(SUM(COALESCE(output_tokens, 0)), 0) AS ot,
                       COUNT(*) AS n
                FROM task_usage WHERE project_id = ?
                GROUP BY k ORDER BY c DESC LIMIT ?
                """,
                (project_id, limit),
            ).fetchall()
            return [
                _UsageRow(
                    key=(r["k"] or "-"),
                    total_cost_usd=round(float(r["c"] or 0), 4),
                    input_tokens=int(r["it"] or 0),
                    output_tokens=int(r["ot"] or 0),
                    calls=int(r["n"] or 0),
                )
                for r in rows
            ]

        trend_rows = conn.execute(
            f"""
            SELECT substr(created_at, 1, 10) AS d,
                   {_cost('total_cost_usd')} AS c,
                   COUNT(*) AS n
            FROM task_usage
            WHERE project_id = ? AND substr(created_at, 1, 10) >= ?
            GROUP BY d ORDER BY d
            """,
            (project_id, window_start),
        ).fetchall()
        by_day = {r["d"]: (float(r["c"] or 0), int(r["n"] or 0)) for r in trend_rows}
        trend = []
        for i in range(days - 1, -1, -1):
            d = (now - timedelta(days=i)).strftime("%Y-%m-%d")
            c, n = by_day.get(d, (0.0, 0))
            trend.append(_UsageTrendPoint(day=d, total_cost_usd=round(c, 4), calls=n))

        return UsageSummary(
            today_cost_usd=round(today_cost, 4),
            yesterday_cost_usd=round(yesterday_cost, 4),
            total_cost_usd=round(total_cost, 4),
            total_calls=total_calls,
            by_task_type=_buckets("task_type", 8),
            by_model=_buckets("model", 8),
            trend=trend,
            top_projects=[],
        )


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

    HA mode (a non-expired active lease holder exists, i.e. runtime.ha_standby
    is in use): blanket-killing every ``sharp dispatch`` process would also
    take down the standby, creating an outage instead of a failover. Instead
    only the ACTIVE instance (the lease holder) is stopped; its graceful
    shutdown releases the lease and the standby takes over automatically —
    no relaunch needed. The holder's local PID is parsed from its
    dispatcher_id ("hostname:pid:config"); same-machine HA keeps server and
    dispatchers on one host, so the PID is meaningful here.
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

    def _ha_holder() -> str:
        """Active (non-expired) lease holder, or '' in legacy/no-HA mode."""
        try:
            with get_conn() as conn:
                row = conn.execute(
                    "SELECT holder, expires_at FROM dispatcher_lease WHERE id = 1"
                ).fetchone()
        except Exception:
            return ""
        if row is None or not row["holder"]:
            return ""
        return row["holder"] if row["expires_at"] > utcnow() else ""

    def _holder_pid(holder: str) -> int | None:
        # dispatcher_id format: "{hostname}:{pid}:{config-name}" (loop.py
        # __init__). Second-to-last colon-separated part is the pid.
        parts = holder.split(":")
        pid_text = parts[-2] if len(parts) >= 3 else ""
        return int(pid_text) if pid_text.isdigit() else None

    holder = _ha_holder()
    if holder:
        holder_pid = _holder_pid(holder)
        if holder_pid is None:
            return {
                "signaled": 0, "spawned": False,
                "message": f"HA 模式（活跃租约 {holder}），但无法解析其本机 PID；"
                           "请手动停止该实例，standby 会自动接管。",
            }
        try:
            os.kill(holder_pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            return {
                "signaled": 0, "spawned": False,
                "message": f"HA 模式：活跃实例 pid={holder_pid} 不在本机或无法停止；"
                           "请手动处理，standby 会自动接管。",
            }
        # Bounded wait for the holder to exit (same budget as legacy mode).
        deadline = _time.time() + 8.0
        while _time.time() < deadline:
            try:
                os.kill(holder_pid, 0)
            except (ProcessLookupError, PermissionError):
                break
            _time.sleep(0.3)
        try:
            os.kill(holder_pid, 0)
            exited = False
        except (ProcessLookupError, PermissionError):
            exited = True
        if not exited:
            return {
                "signaled": 1, "spawned": False,
                "message": f"已向活跃实例 pid={holder_pid} 发出停止信号，但它仍在退出中"
                           "（正在收敛在途任务）。standby 将在它释放租约后自动接管。",
            }
        return {
            "signaled": 1, "spawned": False, "action": "ha_standby_takeover",
            "message": "HA 模式：已停止活跃实例。它优雅退出时释放活跃租约，"
                       "standby 将自动接管（无需重启 ./sharp）。",
        }

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
    # P2 dispatcher auto-respawn (2026-10-21): when the launcher runs with
    # SHARP_DISPATCH_AUTO_RESPAWN=1 (same env reaches this server process),
    # IT will bring the dispatcher back — the message must not ask for a full
    # ./sharp restart. action stays "relaunch_launcher" either way: the
    # frontend toast kind keys off it (info vs error) and the semantic "the
    # launcher brings it back" holds in both cases.
    auto_respawn = os.environ.get("SHARP_DISPATCH_AUTO_RESPAWN") == "1"
    message = (
        "Dispatcher 已停止。启动器将自动拉起新实例（SHARP_DISPATCH_AUTO_RESPAWN=1），"
        "无需手动重启。"
        if auto_respawn
        else "Dispatcher 已停止。请重启 ./sharp 以拉起新的 Dispatcher"
        "（配置改动需要进程重启才会加载）。"
    )
    return {
        "signaled": signaled, "spawned": False, "action": "relaunch_launcher",
        "message": message,
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


# ── P2 HA active-lease (batch 12) ─────────────────────────────────────────────
# Server is the sole arbiter of which dispatcher instance may dispatch. The
# single dispatcher_lease row is CAS'd: an empty/expired lease or the current
# holder (renewal) acquires; anyone else is told who holds it. When the active
# instance dies its lease expires (ha_takeover_after on the dispatcher side,
# passed as ttl_seconds) and a standby takes over automatically.

class DispatcherLeaseRequest(_BaseModel):
    dispatcher_id: str
    ttl_seconds: int = 15


def _lease_expired(row, now: str) -> bool:
    # ISO Z timestamps share one format, so lexicographic compare == time.
    return not row["holder"] or row["expires_at"] <= now


@router.post("/dispatcher/lease", include_in_schema=False)
def acquire_dispatcher_lease(body: DispatcherLeaseRequest):
    from datetime import datetime, timedelta, timezone

    if not body.dispatcher_id:
        return {"acquired": False, "holder": "", "expires_at": ""}

    now_dt = datetime.now(timezone.utc)
    now = now_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    expires_at = (now_dt + timedelta(seconds=max(1, body.ttl_seconds))).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    with get_conn() as conn:
        row = conn.execute(
            "SELECT holder, expires_at FROM dispatcher_lease WHERE id = 1"
        ).fetchone()
        if row is None:  # never expected (configure seeds it), self-heal anyway
            conn.execute(
                "INSERT OR IGNORE INTO dispatcher_lease (id, holder, expires_at) VALUES (1, '', '')"
            )
            row = {"holder": "", "expires_at": ""}
        if _lease_expired(row, now) or row["holder"] == body.dispatcher_id:
            conn.execute(
                "UPDATE dispatcher_lease SET holder = ?, expires_at = ? WHERE id = 1",
                (body.dispatcher_id, expires_at),
            )
            return {"acquired": True, "holder": body.dispatcher_id, "expires_at": expires_at}
        return {
            "acquired": False,
            "holder": row["holder"],
            "expires_at": row["expires_at"],
        }


@router.post("/dispatcher/lease/release", include_in_schema=False)
def release_dispatcher_lease(body: DispatcherLeaseRequest):
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE dispatcher_lease SET holder = '', expires_at = '' "
            "WHERE id = 1 AND holder = ?",
            (body.dispatcher_id,),
        )
        released = cur.rowcount == 1
    return {"released": released}
