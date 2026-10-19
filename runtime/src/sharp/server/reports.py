from __future__ import annotations

import os
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from sharp.server.repository import hints as hints_repo
from sharp.server.services import get_project_or_404, intent_to_model, next_fact_id, next_intent_id, utcnow


HIGH_RISK_PATTERNS = (
    "critical",
    "high risk",
    "high severity",
    "高危",
    "严重",
    "命令执行",
    "远程代码执行",
    "代码执行",
    "rce",
    "未授权",
    "权限绕过",
    "认证绕过",
    "任意文件",
    "文件读取",
    "文件上传",
    "sql注入",
    "sql injection",
    "ssrf",
    "xxe",
    "反序列化",
    "deserialization",
)
REPORT_CONTEXT_HEADING = "# Sharp 工程化报告上下文"
ENGINEERED_REPORT_HEADING = "# 工程化测试报告"
ENGINEERED_REPORT_INTENT_PREFIX = "生成工程化测试报告"
REPORT_CONTEXT_FACT_LIMIT = 80
REPORT_CONTEXT_FACT_CHARS = 2800
REPORT_CONTEXT_PACKET_LIMIT = 20
REPORT_CONTEXT_PACKET_CHARS = 5000

PACKET_FENCE_RE = re.compile(
    r"```(?P<lang>http|https|request|response|raw|packet|数据包)?\s*\n"
    r"(?P<body>.*?)(?:\n```|\Z)",
    re.IGNORECASE | re.DOTALL,
)
HTTP_START_RE = re.compile(
    r"^(?:GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS|TRACE|CONNECT)\s+\S+\s+HTTP/\d(?:\.\d)?$|^HTTP/\d(?:\.\d)?\s+\d{3}\b",
    re.IGNORECASE | re.MULTILINE,
)


@dataclass(slots=True)
class Finding:
    fact_id: str
    description: str
    intent_id: str | None
    worker: str | None
    concluded_at: str | None
    source_text: str


@dataclass(slots=True)
class Packet:
    name: str
    source_id: str
    text: str


@dataclass(slots=True)
class ReportExport:
    project_id: str
    report_dir: Path
    report_path: Path
    finding_count: int
    packet_count: int
    high_risk: bool


@dataclass(slots=True)
class ReportDraftIntent:
    project_id: str
    fact_id: str
    intent_id: str
    context_chars: int
    intent: Any


@dataclass(slots=True)
class ReportAction:
    status: str
    project_id: str
    report: ReportExport | None = None
    draft: ReportDraftIntent | None = None
    open_intent_id: str | None = None


def reports_root() -> Path:
    configured = os.environ.get("SHARP_REPORTS_DIR")
    if configured:
        return Path(configured).expanduser()
    return Path("reports")



def create_engineered_report_draft_intent(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    creator: str = "report.engineer",
) -> ReportDraftIntent:
    project = get_project_or_404(conn, project_id)
    if project["status"] not in ("completed",):
        raise ValueError("项目需要完成后才能生成 AI 报告")

    # 读取自定义报告指令
    settings_row = conn.execute("SELECT report_instructions FROM settings WHERE rowid = 1").fetchone()
    custom_instructions = (settings_row["report_instructions"] or "") if settings_row else ""

    context = build_engineered_report_context(conn, project_id)
    now = utcnow()
    fact_id = next_fact_id(conn, project_id)
    intent_id = next_intent_id(conn, project_id)
    intent_description = _engineered_report_intent_description(project["title"], custom_instructions)

    # 注意：这条 fact 是**报告生成请求的上下文**（机器生成的 JSON blob），不是证据，
    # 因此**刻意不走** `after_fact_write()` 的统一副作用 —— 把整段上下文当结论去抽取，
    # 只会往接口台账与知识库里灌垃圾。约定见 server/fact_hooks.py；
    # tests/test_fact_hooks.py 的结构断言里这是显式列出的例外。
    conn.execute(
        "INSERT INTO facts (id, project_id, description) VALUES (?, ?, ?)",
        (fact_id, project_id, context),
    )
    conn.execute(
        "INSERT INTO intents (id, project_id, to_fact_id, description, creator, worker, last_heartbeat_at, created_at, concluded_at) VALUES (?, ?, NULL, ?, ?, NULL, NULL, ?, NULL)",
        (intent_id, project_id, intent_description, creator, now),
    )
    conn.execute(
        "INSERT INTO intent_sources (intent_id, project_id, fact_id) VALUES (?, ?, ?)",
        (intent_id, project_id, fact_id),
    )
    row = conn.execute(
        "SELECT * FROM intents WHERE id = ? AND project_id = ?",
        (intent_id, project_id),
    ).fetchone()
    assert row is not None
    return ReportDraftIntent(
        project_id=project_id,
        fact_id=fact_id,
        intent_id=intent_id,
        context_chars=len(context),
        intent=intent_to_model(conn, row, project_id),
    )


def export_or_queue_engineered_report(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    creator: str = "report.engineer",
    force_regenerate: bool = False,
) -> ReportAction:
    get_project_or_404(conn, project_id)
    if not force_regenerate:
        open_intent = _latest_open_engineered_report_intent(conn, project_id)
        if open_intent is not None:
            return ReportAction(
                status="generating",
                project_id=project_id,
                open_intent_id=open_intent["id"],
            )
        if _latest_engineered_report_fact(conn, project_id) is not None:
            return ReportAction(
                status="exported",
                project_id=project_id,
                report=export_latest_engineered_report(conn, project_id),
            )
    draft = create_engineered_report_draft_intent(conn, project_id, creator=creator)
    return ReportAction(status="queued", project_id=project_id, draft=draft)


def build_engineered_report_context(conn: sqlite3.Connection, project_id: str) -> str:
    project = get_project_or_404(conn, project_id)
    facts = conn.execute(
        "SELECT id, description FROM facts WHERE project_id = ? ORDER BY CASE id WHEN 'origin' THEN 0 WHEN 'goal' THEN 1 ELSE 2 END, id",
        (project_id,),
    ).fetchall()
    hints = hints_repo.list_content_for_project(conn, project_id)
    intents = conn.execute(
        "SELECT * FROM intents WHERE project_id = ? ORDER BY created_at",
        (project_id,),
    ).fetchall()
    sources_by_intent = _intent_sources(conn, project_id)
    findings = _load_findings(conn, project_id)
    packets = _extract_packets(findings)

    fact_rows = []
    # Build ref_counts from intent_sources
    ref_counts: dict[str, int] = {}
    for row in conn.execute(
        "SELECT fact_id, COUNT(*) as cnt FROM intent_sources WHERE project_id = ? GROUP BY fact_id",
        (project_id,),
    ).fetchall():
        ref_counts[row["fact_id"]] = row["cnt"]

    # Build high-risk fact ID set
    high_risk_ids = {f.fact_id for f in findings}

    # Collect eligible facts with priority score
    scored_facts = []
    for row in facts:
        description = row["description"] or ""
        if _is_report_internal_fact(description):
            continue
        fact_id = row["id"]
        # Origin/goal filtered out as safety net
        if fact_id in ("origin", "goal"):
            continue
        score = 0
        if fact_id in high_risk_ids:
            score += 100
        ref_count = ref_counts.get(fact_id, 0)
        score += min(ref_count, 10)
        desc_len = len(description)
        if 100 <= desc_len <= 2000:
            score += 3
        elif (50 <= desc_len < 100) or (2000 < desc_len <= 5000):
            score += 1
        scored_facts.append((score, row))

    # Sort by descending score, then take the top REPORT_CONTEXT_FACT_LIMIT
    scored_facts.sort(key=lambda t: t[0], reverse=True)
    for _score, row in scored_facts[:REPORT_CONTEXT_FACT_LIMIT]:
        description = row["description"] or ""
        fact_rows.append(
            {
                "id": row["id"],
                "description": _truncate_report_text(description, REPORT_CONTEXT_FACT_CHARS),
            }
        )

    intent_rows = []
    for row in intents:
        if is_engineered_report_intent_description(row["description"]):
            continue
        intent_rows.append(
            {
                "id": row["id"],
                "from": sources_by_intent.get(row["id"], []),
                "to": row["to_fact_id"],
                "description": _truncate_report_text(row["description"] or "", 1200),
                "creator": row["creator"],
                "worker": row["worker"],
                "created_at": row["created_at"],
                "concluded_at": row["concluded_at"],
            }
        )

    # P0-1 结论可交付性：把每个产物的证据完备度带进报告上下文，让生成器明确知道
    # 哪些结论"证据不足、不可作为已确认漏洞对外提交"，而不是靠它自己猜。
    from sharp.server.finding_quality import assess_finding
    quality_by_fact: dict[str, dict] = {}
    incomplete_count = 0
    for vrow in conn.execute(
        "SELECT fact_id, kind, url, evidence, reproduction, impact "
        "FROM vulnerabilities WHERE project_id = ? AND status != 'dismissed'",
        (project_id,),
    ).fetchall():
        q = assess_finding(
            kind=vrow["kind"] if "kind" in vrow.keys() else "vuln",
            url=vrow["url"] or "",
            evidence=vrow["evidence"] or "",
            reproduction=vrow["reproduction"] or "",
            impact=vrow["impact"] or "",
        )
        if not q.applicable:
            continue
        quality_by_fact[vrow["fact_id"]] = q.as_dict()
        if not q.complete:
            incomplete_count += 1

    finding_rows = []
    for finding in findings[:40]:
        finding_rows.append(
            {
                "fact_id": finding.fact_id,
                "intent_id": finding.intent_id,
                "worker": finding.worker,
                "concluded_at": finding.concluded_at,
                "text": _truncate_report_text(finding.source_text, 2200),
                "evidence_quality": quality_by_fact.get(finding.fact_id),
            }
        )

    packet_rows = []
    for packet in packets[:REPORT_CONTEXT_PACKET_LIMIT]:
        packet_rows.append(
            {
                "name": packet.name,
                "source_id": packet.source_id,
                "text": _truncate_report_text(packet.text, REPORT_CONTEXT_PACKET_CHARS),
            }
        )

    # P1-A 覆盖报告：打到了什么 / 已验证不通 / 盲区。纯读取聚合，不写库。
    from sharp.server.coverage import build_coverage

    coverage = build_coverage(conn, project_id)

    context = {
        "project": {
            "id": project_id,
            "title": project["title"],
            "status": project["status"],
            "created_at": project["created_at"],
        },
        "report_objective": {
            "type": "engineered_security_report_draft",
            "audience": "安全测试人员、复测人员、交付审阅人员",
            "must_distinguish_status": ["verified", "suspected", "todo", "info"],
            "rule": "不要机械照抄 facts/intents。必须基于证据重组为可读报告；没有请求/响应证据时，不得编造 POC，只能写验证方法。",
            "evidence_completeness_rule": (
                "每个结论都必须标注证据完备度（字段 evidence_quality：score/total/missing_labels）。"
                "凡 complete=false 的结论，必须在报告中显式写出『⚠️ 证据不足（缺 X、Y）——待补充』，"
                "并归入『待补充验证』一类，不得表述为已确认漏洞。"
            ),
        },
        "hints": [dict(row) for row in hints],
        "facts": fact_rows,
        "intents": intent_rows,
        "high_risk_findings": finding_rows,
        "packet_samples": packet_rows,
        "packet_count": len(packets),
        "finding_quality_summary": {
            "assessed": len(quality_by_fact),
            "incomplete": incomplete_count,
            "rule": "报告中需说明『共 N 条结论，其中 M 条证据不足需补充』",
        },
        # P1-A 覆盖报告：让报告带着"打到了什么 / 已验证不通 / 盲区"三件事，
        # 而不是只复述结论。blind_spots 尤其重要 —— 它是"哪里根本没碰过"的唯一
        # 有据可查来源（资产台账未验证接口、放弃的行动、未结算假设、未完成阶段）。
        "coverage": {
            **coverage.to_dict(),
            "rule": (
                "报告必须包含『覆盖范围与盲区』一节：列出已验证不通的路径与上表的 blind_spots。"
                "**不得声称覆盖了清单之外的面** —— 授权范围写在 Origin/Goal 的自然语言里，"
                "没有机器可读清单，未列出的面不等于已覆盖。"
            ),
        },
    }

    return "\n".join(
        [
            REPORT_CONTEXT_HEADING,
            "",
            "下面是 Sharp 项目的结构化证据上下文。请基于这些材料编写工程化安全测试报告，不要逐条机械复述。",
            "",
            _json_block(context),
        ]
    )


def export_latest_engineered_report(conn: sqlite3.Connection, project_id: str, *, force: bool = True) -> ReportExport:
    project = get_project_or_404(conn, project_id)
    row = _latest_engineered_report_fact(conn, project_id)
    if row is None:
        raise ValueError("没有找到 AI 工程化报告草稿，请先生成并等待 AI 任务完成")

    project_dir = reports_root() / _safe_name(project_id)
    project_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    report_path = project_dir / f"engineered-report-{timestamp}.md"
    latest_path = project_dir / "engineered-report-latest.md"

    report_content = (row["description"] or "").rstrip() + "\n"
    if force or not report_path.exists():
        report_path.write_text(report_content, encoding="utf-8")
        latest_path.write_text(report_content, encoding="utf-8")

    findings = _load_findings(conn, project_id)
    packets = _extract_packets(findings)
    return ReportExport(
        project_id=project_id,
        report_dir=project_dir,
        report_path=report_path,
        finding_count=len(findings),
        packet_count=len(packets),
        high_risk=bool(findings),
    )



def _load_findings(conn: sqlite3.Connection, project_id: str) -> list[Finding]:
    rows = conn.execute(
        """
        SELECT f.id AS fact_id,
               f.description AS fact_description,
               i.description AS intent_description,
               i.id AS intent_id,
               i.worker AS worker,
               i.concluded_at AS concluded_at
        FROM facts f
        LEFT JOIN intents i
          ON i.project_id = f.project_id
         AND i.to_fact_id = f.id
        WHERE f.project_id = ?
          AND f.id NOT IN ('origin', 'goal')
        ORDER BY COALESCE(i.concluded_at, i.created_at, f.id), f.id
        """,
        (project_id,),
    ).fetchall()
    findings = []
    for row in rows:
        fact_description = row["fact_description"] or ""
        if _is_report_internal_fact(fact_description):
            continue
        intent_description = row["intent_description"] or ""
        if intent_description.startswith(ENGINEERED_REPORT_INTENT_PREFIX):
            continue
        source_text = "\n".join(part for part in (fact_description, intent_description) if part).strip()
        if _looks_high_risk(source_text):
            findings.append(
                Finding(
                    fact_id=row["fact_id"],
                    description=fact_description,
                    intent_id=row["intent_id"],
                    worker=row["worker"],
                    concluded_at=row["concluded_at"],
                    source_text=source_text,
                )
            )
    return findings


def _looks_high_risk(text: str) -> bool:
    normalized = re.sub(r"\s+", " ", text).lower()
    return any(pattern.lower() in normalized for pattern in HIGH_RISK_PATTERNS)


def _extract_packets(findings: list[Finding]) -> list[Packet]:
    packets: list[Packet] = []
    seen: set[str] = set()
    for finding in findings:
        for index, packet_text in enumerate(_packet_blocks(finding.source_text), start=1):
            digest = f"{finding.fact_id}:{packet_text}"
            if digest in seen:
                continue
            seen.add(digest)
            packets.append(
                Packet(
                    name=f"{_safe_name(finding.fact_id)}-{index:02d}.txt",
                    source_id=finding.fact_id,
                    text=packet_text,
                )
            )
    return packets


def _packet_blocks(text: str) -> list[str]:
    blocks = []
    scrubbed_parts = []
    last_end = 0
    for match in PACKET_FENCE_RE.finditer(text):
        scrubbed_parts.append(text[last_end:match.start()])
        last_end = match.end()
        body = match.group("body").strip()
        lang = (match.group("lang") or "").lower()
        if lang or HTTP_START_RE.search(body):
            blocks.append(body)
    scrubbed_parts.append(text[last_end:])
    blocks.extend(_raw_http_blocks("\n".join(scrubbed_parts)))
    return blocks


def _raw_http_blocks(text: str) -> list[str]:
    lines = text.splitlines()
    blocks: list[str] = []
    current: list[str] = []
    for line in lines:
        if HTTP_START_RE.search(line):
            if current:
                blocks.append("\n".join(current).strip())
            current = [line]
            continue
        if not current:
            continue
        if line.startswith("```") or re.match(r"^\s{0,3}#{1,6}\s+", line):
            blocks.append("\n".join(current).strip())
            current = []
            continue
        if len(current) < 120:
            current.append(line)
    if current:
        blocks.append("\n".join(current).strip())
    return [block for block in blocks if block]



def _json_block(value: Any) -> str:
    return "```json\n" + json.dumps(value, ensure_ascii=False, indent=2) + "\n```"


def _truncate_report_text(text: str, limit: int) -> str:
    compact = str(text or "").strip()
    if len(compact) <= limit:
        return compact
    return compact[:limit] + "\n... [truncated]"


def _is_report_internal_fact(description: str) -> bool:
    text = str(description or "").lstrip()
    return text.startswith(REPORT_CONTEXT_HEADING) or text.startswith(ENGINEERED_REPORT_HEADING)


def is_engineered_report_intent_description(description: str | None) -> bool:
    return str(description or "").startswith(ENGINEERED_REPORT_INTENT_PREFIX)


def _intent_sources(conn: sqlite3.Connection, project_id: str) -> dict[str, list[str]]:
    rows = conn.execute(
        "SELECT intent_id, fact_id FROM intent_sources WHERE project_id = ? ORDER BY rowid",
        (project_id,),
    ).fetchall()
    sources: dict[str, list[str]] = {}
    for row in rows:
        sources.setdefault(row["intent_id"], []).append(row["fact_id"])
    return sources


def _engineered_report_intent_description(project_title: str, custom_instructions: str = "") -> str:
    base = (
        f"{ENGINEERED_REPORT_INTENT_PREFIX}：请基于来源 fact 中的 Sharp 工程化报告上下文，为项目《{project_title}》编写一份面向安全测试人员的 Markdown 报告草稿。"
        "要求：1) 不要机械照抄原始 facts/intents；2) 按测试范围、执行摘要、资产概览、漏洞/风险详情、POC或验证方法、影响分析、修复建议、复测清单组织；"
        "3) 每个问题必须标注状态 verified/suspected/todo/info 和证据可信度；4) 只有存在明确请求/响应或复现证据时才写成已验证 POC；"
        "5) 没有数据包时只能写验证方法和所需条件，不得编造请求包；6) 必须把完整 Markdown 报告放入 JSON 的 data.description 字段，"
        "不要只返回文件路径、摘要或保存成功说明；7) 输出应以 '# 工程化测试报告 - 项目名称' 开头，并作为本 intent 的结论 fact；"
        "8) 每个漏洞/风险条目必须在标题前加风险类型标签：🔴 已验证漏洞（有完整请求/响应证据且可复现）、"
        "🟡 未测试（需认证/需账号/因401或403跳过，有接口路径但缺乏测试条件）、🔵 已排除（经测试无风险或属误报）；"
        "9) 报告末尾必须新增「未测试接口清单」章节，列出所有因 401/403 响应、缺少账号或无法认证而跳过的接口路径，"
        "格式：`METHOD /path — 跳过原因`，供下一轮测试使用，不得省略此章节（若确实无任何跳过接口可写'本次测试覆盖全部接口'）；"
        "10) 报告末尾增加「逻辑漏洞覆盖度自查」章节，逐项说明本次测试是否覆盖了：IDOR/水平越权、垂直越权、业务流程绕过、参数篡改、竞态条件，"
        "每项写明覆盖情况（已测试/未测试/无相关接口）。"
    )
    if custom_instructions and custom_instructions.strip():
        base += f" 【自定义补充要求】{custom_instructions.strip()}"
    return base


def _latest_engineered_report_fact(conn: sqlite3.Connection, project_id: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT f.id, f.description, i.id AS intent_id, i.concluded_at
        FROM facts f
        JOIN intents i
          ON i.project_id = f.project_id
         AND i.to_fact_id = f.id
        WHERE f.project_id = ?
          AND i.description LIKE ?
          AND ltrim(f.description) LIKE ?
          AND f.description NOT LIKE ?
        ORDER BY COALESCE(i.concluded_at, i.created_at, f.id) DESC
        LIMIT 1
        """,
        (
            project_id,
            f"{ENGINEERED_REPORT_INTENT_PREFIX}%",
            f"{ENGINEERED_REPORT_HEADING}%",
            f"{REPORT_CONTEXT_HEADING}%",
        ),
    ).fetchone()


def _latest_open_engineered_report_intent(conn: sqlite3.Connection, project_id: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT id, created_at
        FROM intents
        WHERE project_id = ?
          AND to_fact_id IS NULL
          AND description LIKE ?
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """,
        (project_id, f"{ENGINEERED_REPORT_INTENT_PREFIX}%"),
    ).fetchone()


def _safe_name(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip())
    return safe.strip("-") or "item"
