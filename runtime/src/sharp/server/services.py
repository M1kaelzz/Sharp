from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from fastapi import HTTPException

from sharp.server.hostkey import asset_ref, canonical_key, key_from_sources
from sharp.server.models import Intent, ProjectMeta, ProjectReason

def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def next_project_id(conn: sqlite3.Connection) -> str:
    """找最小可用编号（填补删除项目留下的空缺），而不是单调递增。"""
    rows = conn.execute("SELECT id FROM projects WHERE id LIKE 'proj_%'").fetchall()
    used = set()
    for r in rows:
        try:
            used.add(int(r["id"].split("_")[1]))
        except (IndexError, ValueError):
            pass
    n = 1
    while n in used:
        n += 1
    return f"proj_{n:03d}"


def _next_scoped_id(
    conn: sqlite3.Connection, kind: str, prefix: str, project_id: str
) -> str:
    conn.execute(
        "INSERT OR IGNORE INTO scoped_counters (project_id, kind, value) VALUES (?, ?, 0)",
        (project_id, kind),
    )
    conn.execute(
        "UPDATE scoped_counters SET value = value + 1 WHERE project_id = ? AND kind = ?",
        (project_id, kind),
    )
    row = conn.execute(
        "SELECT value FROM scoped_counters WHERE project_id = ? AND kind = ?",
        (project_id, kind),
    ).fetchone()
    assert row is not None
    return f"{prefix}{row['value']:03d}"


def next_fact_id(conn: sqlite3.Connection, project_id: str) -> str:
    return _next_scoped_id(conn, "fact", "f", project_id)


def next_intent_id(conn: sqlite3.Connection, project_id: str) -> str:
    return _next_scoped_id(conn, "intent", "i", project_id)


def next_hint_id(conn: sqlite3.Connection, project_id: str) -> str:
    return _next_scoped_id(conn, "hint", "h", project_id)


def next_vuln_id(conn: sqlite3.Connection, project_id: str) -> str:
    return _next_scoped_id(conn, "vuln", "v", project_id)


def next_sub_goal_id(conn: sqlite3.Connection, project_id: str) -> str:
    """Per-project sub goal counter (sg001, sg002, …)."""
    return _next_scoped_id(conn, "sub_goal", "sg", project_id)


def next_hypothesis_id(conn: sqlite3.Connection, project_id: str) -> str:
    """Per-project hypothesis counter (h001, h002, …)."""
    return _next_scoped_id(conn, "hypothesis", "h", project_id)


def get_project_or_404(conn: sqlite3.Connection, project_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Project not found")
    return row


def check_project_active(conn: sqlite3.Connection, project_id: str) -> sqlite3.Row:
    row = get_project_or_404(conn, project_id)
    if row["status"] != "active":
        raise HTTPException(403, f"Project is {row['status']}")
    return row


def check_project_hint_writable(conn: sqlite3.Connection, project_id: str) -> sqlite3.Row:
    row = get_project_or_404(conn, project_id)
    if row["status"] not in ("active", "stopped", "completed"):
        raise HTTPException(403, f"Project is {row['status']}")
    return row


def check_project_completed(conn: sqlite3.Connection, project_id: str) -> sqlite3.Row:
    row = get_project_or_404(conn, project_id)
    if row["status"] != "completed":
        raise HTTPException(403, f"Project is {row['status']}")
    return row


def validate_facts_exist(
    conn: sqlite3.Connection, project_id: str, fact_ids: list[str]
) -> None:
    for fid in fact_ids:
        row = conn.execute(
            "SELECT 1 FROM facts WHERE id = ? AND project_id = ?", (fid, project_id)
        ).fetchone()
        if row is None:
            raise HTTPException(404, f"Fact {fid} not found")


def validate_goal_not_in_sources(fact_ids: list[str]) -> None:
    if "goal" in fact_ids:
        raise HTTPException(400, "goal cannot be used in from")


def validate_intent_creator_worker(creator: str, worker: str | None) -> None:
    if worker is not None and worker != creator:
        raise HTTPException(400, "worker must be null or equal to creator")


def get_intent_or_404(
    conn: sqlite3.Connection, project_id: str, intent_id: str
) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM intents WHERE id = ? AND project_id = ?",
        (intent_id, project_id),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "Intent not found")
    return row


def get_releasable_open_intent_or_404(
    conn: sqlite3.Connection, project_id: str, intent_id: str, worker: str
) -> sqlite3.Row:
    expire_workers(conn, project_id)
    row = get_intent_or_404(conn, project_id, intent_id)
    if row["to_fact_id"] is not None:
        raise HTTPException(409, "Intent already concluded")
    if row["worker"] is None:
        return row
    if row["worker"] != worker:
        raise HTTPException(409, f"Intent is currently claimed by {row['worker']}")
    return row


def get_completion_intent_or_409(conn: sqlite3.Connection, project_id: str) -> sqlite3.Row:
    rows = conn.execute(
        "SELECT * FROM intents WHERE project_id = ? AND to_fact_id = 'goal'",
        (project_id,),
    ).fetchall()
    if not rows:
        raise HTTPException(409, "Completed project is missing its completion intent")
    if len(rows) != 1:
        raise HTTPException(409, "Completed project has multiple completion intents")
    return rows[0]


def intent_to_model(conn: sqlite3.Connection, row: sqlite3.Row, project_id: str) -> Intent:
    sources = conn.execute(
        "SELECT fact_id FROM intent_sources WHERE intent_id = ? AND project_id = ? ORDER BY rowid",
        (row["id"], project_id),
    ).fetchall()
    return Intent(
        id=row["id"],
        **{"from": [s["fact_id"] for s in sources]},
        to=row["to_fact_id"],
        description=row["description"],
        creator=row["creator"],
        worker=row["worker"],
        last_heartbeat_at=row["last_heartbeat_at"],
        created_at=row["created_at"],
        concluded_at=row["concluded_at"],
        risk_level=row["risk_level"],
        risk_reason=row["risk_reason"],
        approval_status=row["approval_status"],
        approval_note=row["approval_note"],
        approval_decided_at=row["approval_decided_at"],
        priority=row["priority"] if "priority" in row.keys() else 0,
        abandoned_at=row["abandoned_at"] if "abandoned_at" in row.keys() else None,
        abandon_reason=row["abandon_reason"] if "abandon_reason" in row.keys() else "",
    )


def build_intents(conn: sqlite3.Connection, project_id: str) -> list[Intent]:
    rows = conn.execute(
        "SELECT * FROM intents WHERE project_id = ? ORDER BY created_at",
        (project_id,),
    ).fetchall()
    return [intent_to_model(conn, r, project_id) for r in rows]


def vuln_to_model(row: sqlite3.Row) -> "Vulnerability":
    from sharp.server.finding_quality import assess_finding
    from sharp.server.models import FindingQualityModel, Vulnerability

    quality = assess_finding(
        kind=row["kind"] if "kind" in row.keys() else "vuln",
        url=row["url"] or "",
        evidence=row["evidence"] or "",
        reproduction=row["reproduction"] or "",
        impact=row["impact"] or "",
    )
    return Vulnerability(
        id=row["id"],
        project_id=row["project_id"],
        fact_id=row["fact_id"],
        intent_id=row["intent_id"],
        title=row["title"],
        severity=row["severity"],
        status=row["status"],
        kind=row["kind"] if "kind" in row.keys() else "vuln",
        score=row["score"] if "score" in row.keys() else 0,
        url=row["url"],
        description=row["description"],
        evidence=row["evidence"],
        reproduction=row["reproduction"],
        impact=row["impact"],
        recommendation=row["recommendation"],
        created_at=row["created_at"],
        verified_at=row["verified_at"],
        quality=FindingQualityModel(**quality.as_dict()),
    )


def get_intent_timeout(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT intent_timeout FROM settings WHERE rowid = 1").fetchone()
    return row["intent_timeout"]


def get_reason_timeout(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT reason_timeout FROM settings WHERE rowid = 1").fetchone()
    return row["reason_timeout"]


def project_reason_from_row(row: sqlite3.Row) -> ProjectReason | None:
    if row["reason_worker"] is None:
        return None
    return ProjectReason(
        worker=row["reason_worker"],
        trigger=row["reason_trigger"],
        started_at=row["reason_started_at"],
        last_heartbeat_at=row["reason_last_heartbeat_at"],
    )


def project_meta_from_row(row: sqlite3.Row) -> ProjectMeta:
    return ProjectMeta(
        id=row["id"],
        title=row["title"],
        status=row["status"],
        created_at=row["created_at"],
        reason=project_reason_from_row(row),
        paused=bool(row["paused"]) if "paused" in row.keys() else False,
        task_budget=row["task_budget"] if "task_budget" in row.keys() else 0,
        task_count=row["task_count"] if "task_count" in row.keys() else 0,
        target_kind=row["target_kind"] if "target_kind" in row.keys() else "web",
        asset_ref=row["asset_ref"] if "asset_ref" in row.keys() else "",
        deadline_at=row["deadline_at"] if "deadline_at" in row.keys() else None,
        task_mode=row["task_mode"] if "task_mode" in row.keys() else "pentest",
        product=row["product"] if "product" in row.keys() else "",
    )


def clear_project_reason(conn: sqlite3.Connection, project_id: str) -> None:
    conn.execute(
        """
        UPDATE projects
        SET reason_worker = NULL,
            reason_trigger = NULL,
            reason_started_at = NULL,
            reason_last_heartbeat_at = NULL
        WHERE id = ?
        """,
        (project_id,),
    )


def expire_workers(conn: sqlite3.Connection, project_id: str | None = None) -> None:
    timeout = get_intent_timeout(conn)
    now = utcnow()
    query = """
        UPDATE intents
        SET worker = NULL
        WHERE to_fact_id IS NULL
          AND worker IS NOT NULL
          AND last_heartbeat_at IS NOT NULL
          AND (julianday(?) - julianday(last_heartbeat_at)) * 86400 > ?
    """
    params: tuple = (now, timeout)
    if project_id is not None:
        query = query.replace("WHERE ", "WHERE project_id = ? AND ", 1)
        params = (project_id, now, timeout)
    conn.execute(query, params)


def expire_reason_leases(conn: sqlite3.Connection, project_id: str | None = None) -> None:
    timeout = get_reason_timeout(conn)
    now = utcnow()
    query = """
        UPDATE projects
        SET reason_worker = NULL,
            reason_trigger = NULL,
            reason_started_at = NULL,
            reason_last_heartbeat_at = NULL
        WHERE reason_worker IS NOT NULL
          AND reason_last_heartbeat_at IS NOT NULL
          AND (julianday(?) - julianday(reason_last_heartbeat_at)) * 86400 > ?
    """
    params: tuple = (now, timeout)
    if project_id is not None:
        query = query.replace("WHERE ", "WHERE id = ? AND ", 1)
        params = (project_id, now, timeout)
    conn.execute(query, params)


# Default approval timeout (hours). Server-side liveness expiry: pending
# approvals older than this are lazily marked 'expired' (= never executed) when
# the projects list / project detail is read. This is the server authority; the
# dispatcher may advertise a tighter value via its config, but the server always
# enforces its own floor so a stale config cannot keep a pending intent alive
# indefinitely. 24h default matches the proposed dispatch.yaml default.
DEFAULT_APPROVAL_TIMEOUT_HOURS = 24


def expire_pending_approvals(
    conn: sqlite3.Connection, timeout_hours: int | None = None, project_id: str | None = None
) -> None:
    """Lazily expire pending approval intents that sat too long.

    Called from the projects list/detail read paths. Expired is a *dead* end:
    it is never auto-approved and never scheduled.
    """
    timeout_hours = timeout_hours or DEFAULT_APPROVAL_TIMEOUT_HOURS
    now = utcnow()
    query = """
        UPDATE intents
        SET approval_status = 'expired',
            approval_decided_at = ?
        WHERE approval_status = 'pending'
          AND (julianday(?) - julianday(created_at)) * 24 > ?
    """
    params: tuple = (now, now, timeout_hours)
    if project_id is not None:
        query = query.replace("WHERE ", "WHERE project_id = ? AND ", 1)
        params = (project_id, now, now, timeout_hours)
    cursor = conn.execute(query, params)
    if cursor.rowcount:
        conn.execute(
            """
            INSERT INTO approval_events (intent_id, project_id, action, note, created_at)
            SELECT id, project_id, 'expired',
                   '超时未审批，自动标记过期（不执行）', ?
            FROM intents WHERE approval_status = 'expired'
              AND approval_decided_at = ?
            """,
            (now, now),
        )


def expire_project_emergency(
    conn: sqlite3.Connection, project_id: str | None = None
) -> None:
    """Lazily clear an expired emergency window so high-risk intents resume
    being gated once the emergency mode times out."""
    now = utcnow()
    query = """
        UPDATE projects
        SET emergency_until = NULL,
            emergency_reason = ''
        WHERE emergency_until IS NOT NULL
          AND julianday(?) > julianday(emergency_until)
    """
    params: tuple = (now,)
    if project_id is not None:
        query = query.replace("WHERE ", "WHERE id = ? AND ", 1)
        params = (project_id, now)
    conn.execute(query, params)


# ── 阶段感知 reason（批次 11.3）───────────────────────────────────────────────

# 凭证/入口类关键词：命中提示项目处于"获取/扩大凭证入口"阶段
def compute_project_phase(conn: sqlite3.Connection, project_id: str) -> dict:
    """项目的阶段估算 —— **只用结构化信号，不从散文里猜**。

    驱动 `{phase_context}`（reason.md 的 "Current stage (server estimate)"），
    纯节奏参考、永不是硬约束。

    **为什么不再用关键词判"有没有漏洞线索"**（2026-09-09 实测推翻）：

    旧实现把 `未授权 / 绕过 / ssrf / 越权 / 漏洞` 这类词在结论里出现当作"已有漏洞面线索"。
    真实语料上它必然误报，因为**工人描述"测过什么"和"发现了什么"用的是同一套词**：

    | 真实结论里的原话 | 旧实现读成 | 实际含义 |
    |---|---|---|
    | `SSRF 探测（i009）完成：…不存在未认证 SSRF` | 有 SSRF 线索 | **否定**结论 |
    | `因此 IDOR/越权/业务逻辑测试…` | 有越权线索 | 待测**计划** |
    | `旧 nginx 未认证 DoS/RCE，影响 ≤8` | 有 RCE 线索 | 在复述 **CVE 描述** |

    后果不是"标签不准"这么轻：这条指导**每轮都注入 reason**，且在零漏洞的项目上写着
    "图上已有漏洞面线索…**少开新面**" —— 它在劝规划器收敛到一个根本没找到东西的方向上。
    实测 `proj_002`（Peplink，全部结论为否定）与 `proj_004` 都被判成"验证阶段"。

    **结构化信号**（各表的事实，按优先级）：

    1. 高危/严重已确认 → 收尾
    2. 高危/严重未确认 → 验证
    3. 存在**证据不足**的结论（`finding_quality`）→ 补证据（交付阻塞项）
    4. 资产台账里有**已发现但从未验证**的接口 → 接着测（`asset_endpoints.todo`）
    5. 本目标键下已有 **credential 知识** → 凭据/入口面
    6. 死胡同 ≥ 3 且无任何漏洞 → **换面**（重复同一手法没意义）
    7. 其余 → 侦察（并说明"尚无结构化信号"，而不是编一个阶段）
    """
    facts = conn.execute(
        "SELECT id, description FROM facts WHERE project_id = ? "
        "AND id NOT IN ('origin', 'goal')",
        (project_id,),
    ).fetchall()
    vulns = conn.execute(
        "SELECT title, severity, status, url, evidence, reproduction, impact, kind "
        "FROM vulnerabilities WHERE project_id = ? AND status != 'dismissed'",
        (project_id,),
    ).fetchall()
    untrusted = conn.execute(
        "SELECT COUNT(*) c FROM facts WHERE project_id = ? AND trusted = 0",
        (project_id,),
    ).fetchone()["c"]
    pending = conn.execute(
        "SELECT COUNT(*) c FROM intents WHERE project_id = ? AND approval_status = 'pending'",
        (project_id,),
    ).fetchone()["c"]
    open_intents = conn.execute(
        "SELECT COUNT(*) c FROM intents WHERE project_id = ? AND to_fact_id IS NULL "
        "AND approval_status != 'pending'",
        (project_id,),
    ).fetchone()["c"]

    # ── 结构化信号 ──────────────────────────────────────────────────────────
    # `kind='flag'` 是**记分产物**，不是安全发现 —— 混进来会同时污染"漏洞数"与"收尾判定"
    # （实测：3 条 flag + 3 条漏洞被一起算成 6 条结论，进而把评分类任务判成"报告收尾"）。
    flags_found = [v for v in vulns if (v["kind"] if "kind" in v.keys() else "vuln") == "flag"]
    findings_only = [v for v in vulns if (v["kind"] if "kind" in v.keys() else "vuln") != "flag"]
    high = [v for v in findings_only if v["severity"] in ("critical", "high")]
    high_confirmed = [v for v in high if v["status"] == "confirmed"]
    high_unconfirmed = [v for v in high if v["status"] != "confirmed"]

    from sharp.server.finding_quality import assess_finding

    incomplete = [
        v for v in vulns
        if not assess_finding(
            kind=(v["kind"] if "kind" in v.keys() else "vuln") or "vuln",
            url=v["url"], evidence=v["evidence"],
            reproduction=v["reproduction"], impact=v["impact"],
        ).complete
    ]

    project_row = conn.execute(
        "SELECT asset_ref, task_mode FROM projects WHERE id = ?", (project_id,)
    ).fetchone()
    asset_ref = (project_row["asset_ref"] if project_row else "") or ""
    task_mode = (project_row["task_mode"] if project_row else "pentest") or "pentest"
    todo_endpoints = 0
    if asset_ref:
        todo_endpoints = int(conn.execute(
            "SELECT COUNT(*) c FROM asset_endpoints WHERE asset_ref = ? AND status = 'discovered'",
            (asset_ref,),
        ).fetchone()["c"])

    target_key = project_target_key(conn, project_id)
    credentials = 0
    dead_ends_in_kb = 0
    if target_key:
        credentials = int(conn.execute(
            "SELECT COUNT(*) c FROM knowledge_base WHERE root_domain = ? AND kind = 'credential'",
            (target_key,),
        ).fetchone()["c"])
        dead_ends_in_kb = int(conn.execute(
            "SELECT COUNT(*) c FROM knowledge_base WHERE root_domain = ? AND kind = 'dead_end'",
            (target_key,),
        ).fetchone()["c"])
    # 事实层也要数：知识是在「dead_end」这个 kind 出现**之前**沉淀的项目（实测 proj_002），
    # 只看知识库会把 9 条已验证不通当成 0 条。判定口径复用 coverage.py（头部窗口 + 否定措辞），
    # 保持一致 —— 覆盖报告与阶段估算不该各有一套"什么算不通"。
    from sharp.server.coverage import _looks_like_dead_end

    dead_ends_in_facts = sum(
        1 for f in facts if _looks_like_dead_end(f["description"])
    )
    dead_ends = max(dead_ends_in_kb, dead_ends_in_facts)

    # ── 指针：人工纠正优先于一切节奏建议 ────────────────────────────────────
    pointers: list[str] = []
    if untrusted:
        pointers.append(f"{untrusted} 条证据已被人工标注不可信——不要基于它们推进或下结论")
    if pending:
        pointers.append(f"{pending} 个高危行动等待人工审批——不要重复提议同类高危动作")
    if open_intents:
        pointers.append(f"{open_intents} 个开放行动在执行中——优先评估其结论，避免发散")

    # ── 判定（顺序即优先级）────────────────────────────────────────────────
    scored = task_mode == "scored"
    if high_confirmed and scored:
        # **评分类任务不能转"报告收尾"**：它的产出是分数不是报告。实测在还剩 11 个 flag 时
        # 被判成"收尾、避免再发起开放式探索" —— 这是方向相反的指导。
        # 已确认的高危恰恰是**取分的立足点**，该说的是"沿这条链继续深入"。
        phase, label = "exploit", "已获入口 · 继续取分"
        guidance = (
            f"已确认 {len(high_confirmed)} 条高危（含 RCE / 未授权类）—— 在评分类任务里这是**立足点**，"
            f"不是收尾信号：沿同一入口继续深入（内网、数据库、其他 flag 文件、横向到相邻容器），"
            f"优先拿满当前容器剩余 flag。**不要**转入报告收尾。"
            + (f"已登记 flag {len(flags_found)} 个。" if flags_found else "")
        )
    elif high_confirmed:
        phase, label = "report", "高危已确认 · 报告收尾"
        guidance = (
            "高危/严重漏洞已被人工确认，进入收尾：核对 Goal 覆盖、补足影响/修复建议，"
            "或收敛为完成结论；避免再发起大开大合的开放式探索。"
        )
    elif high_unconfirmed:
        phase, label = "verify", "高危待验证"
        guidance = (
            "存在高危/严重线索但尚未确认：下一步应优先做最小伤害的验证（读/差分证明），"
            "产出可确认结论；把探索收敛到验证链上。"
        )
    elif incomplete:
        phase, label = "evidence", "补齐证据"
        guidance = (
            f"已有 {len(incomplete)} 条结论**证据不足**（缺请求/响应、复现步骤或影响说明）——"
            "这是交付阻塞项：优先补齐可复现四件套，而不是继续扩大攻击面。"
        )
    elif todo_endpoints:
        phase, label = "test_backlog", "接口待测"
        guidance = (
            f"资产台账里有 {todo_endpoints} 个**已发现但从未验证**的接口——这是最具体的待办；"
            "优先逐个评估（测过的用 `endpoint_tests` 回报），而不是另开新面。"
        )
    elif credentials:
        phase, label = "credential", "凭据与入口"
        guidance = (
            f"本目标下已沉淀 {credentials} 条凭据类知识：优先摸清认证与权限边界"
            "（未授权读、越权差分），并注意最低伤害原则。"
        )
    elif scored and flags_found:
        phase, label = "exploit", "取分中 · 继续推进"
        guidance = (
            f"已登记 {len(flags_found)} 个 flag，目标是把每道题的 flag 数拿满："
            "继续沿已打通的入口深入（内网服务、数据库、文件系统其他位置），"
            "并优先补齐尚未拿到的题目；评分类任务不做报告收尾。"
        )
    elif vulns:
        # 有结论且证据完备（证据不足的已在上一个分支拦掉）：既不该说"无漏洞"，
        # 也不必催着收敛 —— 该给的是"别重复已验证的路径"。
        phase, label = "concluded", "已有结论 · 继续或收尾"
        guidance = (
            f"已有 {len(vulns)} 条证据完备的结论。可继续扩大面，也可准备收尾；"
            "注意不要重复已验证的路径，新面优先选未被否定的方向。"
        )
    elif dead_ends >= 3:
        phase, label = "explore", "已验证多条不通 · 需换面"
        guidance = (
            f"本目标已沉淀 {dead_ends} 条「已验证不通」结论，且尚无任何漏洞——"
            "**换面或换手法**，不要重复已被否定的路径；也可考虑收敛为「未发现可确认风险」的结论。"
        )
    else:
        phase, label = "recon", "侦察阶段"
        guidance = (
            "尚无结构化信号（没有漏洞、没有待测接口、也没有凭据知识）：仍在早期或尚未产出结论，"
            "优先扩展资产/入口/技术栈侦察（低风险信息收集）。"
        )

    return {
        "phase": phase,
        "label": label,
        "guidance": guidance,
        "pointers": pointers,
        "untrusted_fact_count": int(untrusted),
        "pending_approval_count": int(pending),
        "signals": {
            "vulnerabilities": len(findings_only),
            "flags_found": len(flags_found),
            "incomplete_evidence": len(incomplete),
            "unverified_endpoints": todo_endpoints,
            "credential_knowledge": credentials,
            "dead_ends": dead_ends,
            "dead_ends_from_knowledge": dead_ends_in_kb,
            "dead_ends_from_facts": dead_ends_in_facts,
        },
    }


def project_target_key(conn: sqlite3.Connection, project_id: str) -> str | None:
    """项目的**目标键**（`domain:x` / `ip:x` / `host:x`）—— 归属与复用的唯一口径。

    来源优先级：origin 事实（真实格式是**句子**，交给 hostkey 解析）→ `projects.asset_ref`（兜底）。

    **为什么要收口到一处**：知识库、基线继承、覆盖报告都要回答"这个项目打的是不是同一个目标"。
    此前 knowledge.py 内联一份、coverage.py 又内联一份 —— 同一件事三套实现，
    迟早出现"知识库认为同目标、基线认为不是"这种自相矛盾。
    """
    row = conn.execute(
        "SELECT description FROM facts WHERE project_id = ? AND id = 'origin'",
        (project_id,),
    ).fetchone()
    if row is not None:
        key = key_from_sources(row["description"])
        if key:
            return key
    asset = conn.execute(
        "SELECT asset_ref FROM projects WHERE id = ?", (project_id,)
    ).fetchone()
    if asset is not None and asset["asset_ref"]:
        return canonical_key(asset["asset_ref"])
    return None


def target_key_map(conn: sqlite3.Connection) -> dict[str, str]:
    """所有项目的 project_id → 目标键（一次算好，供同目标查找用）。

    键没有落库（它由 origin 事实派生），所以同目标查找必须在内存里比对 ——
    项目数量是个位数到几十，逐条解析完全够用；真到瓶颈再加冗余列。
    """
    out: dict[str, str] = {}
    for row in conn.execute("SELECT id FROM projects").fetchall():
        key = project_target_key(conn, row["id"])
        if key:
            out[row["id"]] = key
    return out


def extract_web_asset_ref(origin: str) -> str:
    """Canonical web asset key derived from an origin fact.

    URL origins → the host (scheme/port stripped); a bare plausible domain →
    itself; anything else (sentences, IPs, empty) → "".

    **2026-09-15 修正**：这里此前只认"以 `http(s)://` 开头的 URL 或裸域名"，而真实
    origin 事实是**句子**：

        目标地址：http://host.docker.internal:8099/
        授权范围：仅本机自建靶机

    于是 `asset_ref` 被算成空串 —— 与知识键解析**同一个根因**（旧注释还自称
    "Mirrors the knowledge base's host normalization"，其实并不 mirror；两份各写各的
    启发式，正是 P1-B 记的那处）。现在统一走 `hostkey`：先在原文里扫主机，再退化为
    直接解析，两个口径从此只有一份实现。

    返回格式刻意**保持裸主机**（不带 `domain:`/`ip:` 前缀），避免与已有的
    `asset_endpoints.asset_ref` 历史数据不一致而需要迁移。
    """
    text = (origin or "").strip()
    if not text:
        return ""
    return asset_ref(text) or ""


# `_is_plausible_domain` / `_host_from_urlish` 已删除：口径统一到
# `sharp.server.hostkey`（两份不一致的启发式正是"同一件事有两套真相"的来源）。
