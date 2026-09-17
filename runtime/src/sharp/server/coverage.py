"""项目覆盖报告（P1-A）：结项时回答"打到了什么 / 漏了什么 / 哪些已验证不通"。

**为什么需要**：结项目前只留一句完成说明，加上一张**纯负面**的盘点清单（`acceptance-check`：
未了结行动、待审批、不可信证据、未完成阶段）。它回答的是"还有什么没了结"，
但回答不了交付时最常被问的两件事：

1. **打到了什么**（结论、严重度、证据是否够用、拿到了哪些访问面）
2. **漏了什么**（哪块面根本没碰过 —— 这才是"说得清"的核心，也是最容易被含糊过去的地方）

**盲区从哪来**：不靠猜。只用**有据可查的缺口**：

- `asset_endpoints.status = 'discovered'` —— 已登记的接口但从未评估过（结构化台账）
- `intents.abandoned_at` 且带 `abandon_reason` —— 计划过却放弃的步骤（含放弃原因）
- 未结算假设、未完成阶段、开放行动、待审批行动
- 被人工标注不可信的证据（结论的可信度打折）

**刻意不做的两件事**：

- **不假装知道"本该测什么"**：授权范围写在 origin/goal 的自然语言里，没有机器可读的清单。
  报告只列**有据可查的缺口**，并明说"此处不构成完整覆盖声明" —— 编一份看起来完整的覆盖面清单
  比不写更危险。
- **不把凭据明文写进报告**：知识库里 `credential` 条目的 title 就是凭据本身。
  报告只给**条数**，不给值（要看得去知识库页面）。其余类型（接口/指纹/死胡同）按标题列出。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any

from sharp.server.finding_quality import assess_finding
from sharp.server.hostkey import canonical_key, key_from_sources

# 否定结论措辞（与 knowledge.py 的 dead_end 判定同源，这里用于统计结论文本里的死胡同）
_DEAD_END_MARKERS = (
    "不适用", "不可行", "无法利用", "试过不通", "走不通", "行不通", "无效",
    "未发现", "不存在", "均失败", "全部失败", "无果", "全部 404", "均返回 404",
    "全部返回 404", "无泄露", "未泄露", "无配置文件", "均无响应", "无响应", "无法绕过",
    "not applicable", "not vulnerable", "ineffective", "dead end", "no effect",
    # 实测补入：工人真的会写这些中文措辞（真实结论里出现过"本 intent 为死胡同"、
    # "结论为负"、"未获取有效会话"），缺了它们就是**漏报** —— 明明写清了不通，
    # 却因为词表里只有英文 "dead end" 而没被记下来。
    "死胡同", "结论为负", "结果为负", "未获取有效会话", "无可确认漏洞", "未确认漏洞",
)

# 盲区的类型标签（前端与报告共用同一份文案，避免两处各写一套）
BLIND_SPOT_LABELS = {
    "unverified_endpoint": "已发现但从未验证的接口",
    "abandoned_intent": "计划过却放弃的行动",
    "open_intent": "未了结的行动",
    "pending_approval": "等待人工审批的行动",
    "open_hypothesis": "未结算的假设",
    "unfinished_stage": "未完成的阶段目标",
    "untrusted_fact": "被标注不可信的结论",
    "unconfirmed_high_vuln": "未确认的高危/严重结论",
}


# 否定标记只在**结论头部**才算数。实测教训：按全文匹配时，goal 文本里的
# "不输出的**无效**发现"命中了标记，于是目标描述被当成"已验证不通"列进报告；
# 前缀事实（"目标已确认为 Peplink 路由器…"）也会因为后面某句话带否定词而误入。
# 工人写结论的习惯是"开头就下结论"，所以头部命中既保精度又不丢召回。
_HEADLINE_WINDOW = 300


def _headline(text: str, limit: int = 170) -> str:
    """取结论的"标题句"：首个非空行 → 截到句读边界 → 超长才硬切。

    之前直接 `[:160]` 会在半句中间断开（实测出现 `…请（来源：结论 f003）` 这种交付观感很差的截断）。
    """
    body = (text or "").strip()
    if not body:
        return ""
    first_line = next((line.strip() for line in body.splitlines() if line.strip()), "")
    if len(first_line) <= limit:
        return first_line
    for sep in ("。", "；", "！", "？", ". ", "; "):
        idx = first_line.find(sep)
        if 0 < idx <= limit:
            return first_line[: idx + len(sep)].rstrip()
    return first_line[:limit].rstrip() + "…"


def _looks_like_dead_end(text: str) -> bool:
    """否定结论判定（只看头部窗口，见 `_HEADLINE_WINDOW`）。"""
    lowered = (text or "").lower()[:_HEADLINE_WINDOW]
    return any(marker in lowered for marker in _DEAD_END_MARKERS)


@dataclass(slots=True)
class BlindSpot:
    """一处**有据可查**的覆盖缺口。"""

    kind: str
    label: str
    detail: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "label": self.label, "detail": self.detail}


@dataclass(slots=True)
class CoverageReport:
    project_id: str
    title: str
    status: str
    task_mode: str
    started_at: str
    last_activity_at: str
    asset_ref: str
    product: str
    key: str | None
    findings: dict[str, Any] = field(default_factory=dict)
    assets: dict[str, Any] = field(default_factory=dict)
    knowledge: dict[str, Any] = field(default_factory=dict)
    dead_ends: list[dict[str, str]] = field(default_factory=list)
    blind_spots: list[BlindSpot] = field(default_factory=list)
    effort: dict[str, Any] = field(default_factory=dict)

    @property
    def blind_spot_count(self) -> int:
        return len(self.blind_spots)

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "title": self.title,
            "status": self.status,
            "task_mode": self.task_mode,
            "started_at": self.started_at,
            "last_activity_at": self.last_activity_at,
            "asset_ref": self.asset_ref,
            "product": self.product,
            "key": self.key,
            "findings": self.findings,
            "assets": self.assets,
            "knowledge": self.knowledge,
            "dead_ends": self.dead_ends,
            "blind_spots": [b.to_dict() for b in self.blind_spots],
            "blind_spot_count": self.blind_spot_count,
            "effort": self.effort,
        }


def _last_activity(conn: sqlite3.Connection, project_id: str, fallback: str) -> str:
    """最后活动时间：取**有时间的**痕迹里最新的一个（行动结论 / 漏洞 / 知识沉淀）。

    **两个如实约束**：

    - `facts` 表只有 `(id, project_id, description, trusted)`，**没有任何时间戳**，
      所以证据无法参与计算；
    - `projects` 表没有 `completed_at`，拿不到真正的结项时刻。

    因此报告里写的是"首次活动 → 最后活动"并明确标注**非结项时刻** ——
    不编一个看起来精确的时间。
    """
    row = conn.execute(
        """
        SELECT MAX(t) AS last FROM (
            SELECT MAX(concluded_at) t FROM intents WHERE project_id = ?
            UNION ALL SELECT MAX(created_at) FROM vulnerabilities WHERE project_id = ?
            UNION ALL SELECT MAX(updated_at) FROM knowledge_base WHERE source_project_id = ?
        )
        """,
        (project_id, project_id, project_id),
    ).fetchone()
    return (row["last"] if row and row["last"] else fallback) or fallback


def build_coverage(conn: sqlite3.Connection, project_id: str) -> CoverageReport:
    """聚合一个项目的覆盖报告。纯读取，不写库。"""
    from sharp.server.services import get_project_or_404

    project = get_project_or_404(conn, project_id)
    started_at = project["created_at"]
    asset_ref = project["asset_ref"] if "asset_ref" in project.keys() else ""
    product = project["product"] if "product" in project.keys() else ""

    origin_row = conn.execute(
        "SELECT description FROM facts WHERE project_id = ? AND id = 'origin'",
        (project_id,),
    ).fetchone()
    key = key_from_sources(origin_row["description"]) if origin_row else None
    if key is None and asset_ref:
        key = canonical_key(asset_ref)

    report = CoverageReport(
        project_id=project_id,
        title=project["title"],
        status=project["status"],
        task_mode=project["task_mode"] if "task_mode" in project.keys() else "pentest",
        started_at=started_at,
        last_activity_at=_last_activity(conn, project_id, started_at),
        asset_ref=asset_ref,
        product=product,
        key=key,
    )

    # ── 打到了什么 ──────────────────────────────────────────────────────────
    vulns = conn.execute(
        "SELECT id, title, severity, status, url, description, evidence, reproduction, impact, kind "
        "FROM vulnerabilities WHERE project_id = ? ORDER BY created_at",
        (project_id,),
    ).fetchall()
    by_severity: dict[str, int] = {}
    incomplete: list[dict[str, str]] = []
    for row in vulns:
        by_severity[row["severity"]] = by_severity.get(row["severity"], 0) + 1
        quality = assess_finding(
            kind=row["kind"] if "kind" in row.keys() else "vuln",
            url=row["url"],
            evidence=row["evidence"],
            reproduction=row["reproduction"],
            impact=row["impact"],
        )
        if quality.applicable and not quality.complete:
            incomplete.append({
                "id": row["id"],
                "title": row["title"],
                "missing": "、".join(quality.missing_labels),
            })
    report.findings = {
        "total": len(vulns),
        "by_severity": by_severity,
        "confirmed": sum(1 for r in vulns if r["status"] == "confirmed"),
        "incomplete_count": len(incomplete),
        "incomplete": incomplete,
    }

    # ── 资产面：登记过的接口 + 从未评估的（= 结构化盲区）────────────────────
    assets: dict[str, Any] = {"asset_ref": asset_ref, "total": 0, "verified": 0, "todo": 0,
                              "dismissed": 0, "todo_items": []}
    if asset_ref:
        rows = conn.execute(
            "SELECT method, path, status FROM asset_endpoints WHERE asset_ref = ? "
            "ORDER BY (status = 'discovered') DESC, path",
            (asset_ref,),
        ).fetchall()
        assets["total"] = len(rows)
        for row in rows:
            state = row["status"]
            if state == "discovered":
                assets["todo"] += 1
                assets["todo_items"].append(
                    f"{(row['method'] or 'ANY').upper()} {row['path']}"
                )
            elif state == "verified":
                assets["verified"] += 1
            elif state == "dismissed":
                assets["dismissed"] += 1
    report.assets = assets

    # ── 知识沉淀（同目标键）：条数按类型，凭据只给条数不给值 ─────────────────
    knowledge: dict[str, Any] = {"key": key, "by_kind": {}, "product_scope": product}
    if key:
        for row in conn.execute(
            "SELECT kind, COUNT(*) c FROM knowledge_base WHERE root_domain = ? GROUP BY kind",
            (key,),
        ).fetchall():
            knowledge["by_kind"][row["kind"]] = int(row["c"])
    report.knowledge = knowledge

    # ── 已验证不通：知识库 dead_end + 结论里的否定措辞 ──────────────────────
    if key:
        for row in conn.execute(
            "SELECT title, content FROM knowledge_base WHERE root_domain = ? AND kind = 'dead_end' "
            "ORDER BY updated_at DESC LIMIT 20",
            (key,),
        ).fetchall():
            report.dead_ends.append({
                "title": _headline(row["title"] or row["content"]),
                "source": "知识库",
            })
    seen_titles = {d["title"] for d in report.dead_ends}
    for row in conn.execute(
        # origin / goal 是**结构事实**不是结论：目标文本里出现"无效发现"这类词完全正常，
        # 把目标描述列成"已验证不通"是错的（实测发生过）。
        "SELECT id, description FROM facts WHERE project_id = ? "
        "AND id NOT IN ('origin', 'goal') ORDER BY id",
        (project_id,),
    ).fetchall():
        if not _looks_like_dead_end(row["description"]):
            continue
        headline = _headline(row["description"])
        if not headline or headline in seen_titles:
            continue
        seen_titles.add(headline)
        report.dead_ends.append({"title": headline, "source": f"结论 {row['id']}"})

    # ── 盲区：只列有据可查的 ────────────────────────────────────────────────
    spots: list[BlindSpot] = []
    for item in assets["todo_items"][:20]:
        spots.append(BlindSpot("unverified_endpoint", item, "资产台账中状态为 discovered"))
    if assets["todo"] > 20:
        spots.append(BlindSpot("unverified_endpoint", f"…另有 {assets['todo'] - 20} 个未验证接口",
                               "资产台账"))

    for row in conn.execute(
        "SELECT id, description, abandon_reason FROM intents "
        "WHERE project_id = ? AND abandoned_at IS NOT NULL ORDER BY abandoned_at",
        (project_id,),
    ).fetchall():
        spots.append(BlindSpot(
            "abandoned_intent",
            f"[{row['id']}] {(row['description'] or '')[:120]}",
            f"放弃原因：{row['abandon_reason'] or '未填写'}",
        ))
    for row in conn.execute(
        "SELECT id, description FROM intents WHERE project_id = ? AND to_fact_id IS NULL "
        "AND approval_status != 'pending' ORDER BY created_at",
        (project_id,),
    ).fetchall():
        spots.append(BlindSpot("open_intent", f"[{row['id']}] {(row['description'] or '')[:120]}"))
    for row in conn.execute(
        "SELECT id, description FROM intents WHERE project_id = ? AND approval_status = 'pending'",
        (project_id,),
    ).fetchall():
        spots.append(BlindSpot("pending_approval", f"[{row['id']}] {(row['description'] or '')[:120]}"))
    for row in conn.execute(
        "SELECT id, statement FROM hypotheses WHERE project_id = ? AND status IN ('open', 'testing')",
        (project_id,),
    ).fetchall():
        spots.append(BlindSpot("open_hypothesis", f"[{row['id']}] {(row['statement'] or '')[:120]}"))
    for row in conn.execute(
        "SELECT id, title FROM sub_goals WHERE project_id = ? AND status IN ('pending', 'active')",
        (project_id,),
    ).fetchall():
        spots.append(BlindSpot("unfinished_stage", f"[{row['id']}] {(row['title'] or '')[:120]}"))
    for row in conn.execute(
        "SELECT id, description FROM facts WHERE project_id = ? AND trusted = 0 ORDER BY id",
        (project_id,),
    ).fetchall():
        spots.append(BlindSpot("untrusted_fact", f"[{row['id']}] {(row['description'] or '')[:120]}",
                               "人工标注为不可信"))
    for row in conn.execute(
        "SELECT id, title, severity FROM vulnerabilities WHERE project_id = ? "
        "AND severity IN ('critical', 'high') AND status NOT IN ('dismissed', 'confirmed')",
        (project_id,),
    ).fetchall():
        spots.append(BlindSpot("unconfirmed_high_vuln", f"[{row['id']}] {row['title'][:110]}",
                               f"严重度 {row['severity']}，尚未确认"))
    report.blind_spots = spots

    # ── 成本 ────────────────────────────────────────────────────────────────
    facts_total = conn.execute(
        "SELECT COUNT(*) c FROM facts WHERE project_id = ?", (project_id,)
    ).fetchone()["c"]
    report.effort = {
        "task_count": project["task_count"] if "task_count" in project.keys() else 0,
        "task_budget": project["task_budget"] if "task_budget" in project.keys() else 0,
        "facts": int(facts_total),
    }
    return report


def coverage_markdown(report: CoverageReport) -> str:
    """渲染成给人和模型看的 Markdown。

    结构固定为「打到了什么 → 已验证不通 → 盲区 → 成本」，因为交付沟通里
    被问的顺序就是这个顺序。
    """
    lines: list[str] = [f"## 覆盖报告 · {report.title}（{report.project_id}）", ""]
    lines.append(f"- 状态：{report.status}｜模式：{report.task_mode}")
    lines.append(f"- 活动区间：{report.started_at or '—'} → {report.last_activity_at or '—'}（首次/最后活动，非结项时刻）")
    if report.key:
        lines.append(f"- 目标键：`{report.key}`" + (f"｜产品：{report.product}" if report.product else ""))

    lines += ["", "### 打到了什么", ""]
    findings = report.findings
    if findings.get("total"):
        severity = "、".join(f"{k} {v}" for k, v in (findings.get("by_severity") or {}).items())
        lines.append(f"- 结论 {findings['total']} 条（{severity}），其中已确认 {findings.get('confirmed', 0)} 条")
        if findings.get("incomplete_count"):
            lines.append(f"- ⚠ 其中 **{findings['incomplete_count']} 条证据不足**，交付前需补齐：")
            for item in (findings.get("incomplete") or [])[:10]:
                lines.append(f"  - {item['title'][:80]}（缺 {item['missing']}）")
    else:
        lines.append("- 未登记结构化结论（漏洞库为空）")
    assets = report.assets
    lines.append(
        f"- 接口台账：登记 {assets.get('total', 0)} 个（已验证 {assets.get('verified', 0)}、"
        f"从未验证 {assets.get('todo', 0)}、已排除 {assets.get('dismissed', 0)}）"
    )
    kinds = report.knowledge.get("by_kind") or {}
    if kinds:
        # 凭据只报条数：知识库里 credential 的 title 就是凭据本身，写进报告等于到处散密钥
        shown = "、".join(
            f"{k} {v}" for k, v in sorted(kinds.items())
        )
        lines.append(f"- 沉淀知识（同目标）：{shown}（凭据仅计数，值见知识库页面）")

    lines += ["", "### 已验证不通（换同类目标时最省时间的一条）", ""]
    if report.dead_ends:
        for item in report.dead_ends[:15]:
            lines.append(f"- {item['title']}（来源：{item['source']}）")
        if len(report.dead_ends) > 15:
            lines.append(f"- …另有 {len(report.dead_ends) - 15} 条，见知识库 `{report.key}`")
    else:
        lines.append("- 无（本次没有留下否定结论）")

    lines += ["", f"### 盲区（{report.blind_spot_count} 项）", ""]
    if report.blind_spots:
        grouped: dict[str, list[BlindSpot]] = {}
        for spot in report.blind_spots:
            grouped.setdefault(spot.kind, []).append(spot)
        for kind, items in grouped.items():
            lines.append(f"**{BLIND_SPOT_LABELS.get(kind, kind)}（{len(items)}）**")
            for spot in items[:12]:
                suffix = f" —— {spot.detail}" if spot.detail else ""
                lines.append(f"- {spot.label}{suffix}")
            if len(items) > 12:
                lines.append(f"- …另有 {len(items) - 12} 项")
            lines.append("")
    else:
        lines.append("- 无有据可查的缺口")
        lines.append("")
    lines.append(
        "> 以上盲区**仅列出有凭据的缺口**（资产台账未验证接口、放弃的行动、未结算假设等）。"
        "授权范围本身写在 Origin/Goal 的自然语言里，没有机器可读清单，因此**本报告不构成"
        "完整覆盖声明** —— 未列出的面不等于已覆盖。"
    )

    effort = report.effort
    lines += ["", "### 成本", ""]
    budget = effort.get("task_budget") or 0
    lines.append(
        f"- 任务数 {effort.get('task_count', 0)}"
        + (f" / 预算 {budget}" if budget else "（无预算上限）")
        + f"｜证据 {effort.get('facts', 0)} 条"
    )
    return "\n".join(lines)
