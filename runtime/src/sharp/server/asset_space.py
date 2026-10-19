"""目标空间详情（P2-A）：把一个资产键下**所有项目**的东西摊开看。

**与已有两端的分工**（避免重复造）：

| 视角 | 端点 | 回答 |
|---|---|---|
| 项目列表 | `GET /assets` | 有哪些资产、各自的壳（项目数 / 高危合计 / 接口 N/M 待测） |
| **资产** | **`GET /asset-spaces/{asset_ref}`（本模块）** | **这个资产整体打到哪了**：共享接口清单与状态、历史发现、已沉淀知识、死胡同 |
| 单项目 | `GET /projects/{id}/coverage` | 这一次打得完整不完整（盲区、证据完备度、成本） |

这个视角此前是空的：点开资产芯片只等于"过滤项目列表"，看不到**跨项目共享的那部分**
（接口台账本来就是按资产键共享的）。而它现在才真正有内容可看 —— 台账状态机（P1-B）、
证据完备度（P0-1）、死胡同（P0-知识复用）都是这轮补上的。
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from typing import Any

from sharp.server.finding_quality import assess_finding
from sharp.server.hostkey import canonical_key

_SEVERITY_ORDER = ("critical", "high", "medium", "low", "info")


def build_asset_space(conn: sqlite3.Connection, asset_ref: str) -> dict[str, Any] | None:
    """聚合一个资产键下的全部内容。资产不存在返回 None。"""
    ref = (asset_ref or "").strip()
    if not ref:
        return None

    projects = conn.execute(
        "SELECT id, title, status, created_at, target_kind, product "
        "FROM projects WHERE asset_ref = ? ORDER BY created_at",
        (ref,),
    ).fetchall()
    if not projects:
        return None

    pids = [p["id"] for p in projects]
    placeholders = ",".join("?" for _ in pids)
    titles = {p["id"]: p["title"] for p in projects}

    # ── 覆盖状态：接口台账（按资产键共享）────────────────────────────────────
    rows = conn.execute(
        "SELECT method, path, status, note, source_project_id, source_project_title, "
        "       first_seen, last_seen, last_assessed_at "
        "FROM (SELECT e.*, p.title AS source_project_title FROM asset_endpoints e "
        "      LEFT JOIN projects p ON p.id = e.source_project_id "
        "      WHERE e.asset_ref = ?) "
        "ORDER BY (status = 'discovered') DESC, path",
        (ref,),
    ).fetchall() if _has_asset_endpoints(conn) else []
    endpoints = {
        "total": len(rows),
        "discovered": sum(1 for r in rows if r["status"] == "discovered"),
        "verified": sum(1 for r in rows if r["status"] == "verified"),
        "dismissed": sum(1 for r in rows if r["status"] == "dismissed"),
        "items": [
            {
                "method": (r["method"] or "ANY").upper(),
                "path": r["path"],
                "status": r["status"],
                "note": r["note"] or "",
                "source_project_id": r["source_project_id"],
                "source_project_title": r["source_project_title"] or "",
                "first_seen": r["first_seen"],
                "last_seen": r["last_seen"],
                "last_assessed_at": r["last_assessed_at"] or "",
            }
            for r in rows
        ],
    }

    # ── 历史发现：跨项目汇总 + 证据完备度 ────────────────────────────────────
    vulns = conn.execute(
        f"SELECT id, project_id, title, severity, status, url, evidence, reproduction, "
        f"       impact, kind FROM vulnerabilities WHERE project_id IN ({placeholders}) "
        f"ORDER BY created_at DESC",
        tuple(pids),
    ).fetchall()
    by_severity = Counter(v["severity"] for v in vulns)
    incomplete = []
    for v in vulns:
        quality = assess_finding(
            kind=(v["kind"] if "kind" in v.keys() else "vuln") or "vuln",
            url=v["url"], evidence=v["evidence"],
            reproduction=v["reproduction"], impact=v["impact"],
        )
        if quality.applicable and not quality.complete:
            incomplete.append({
                "id": v["id"],
                "title": v["title"],
                "project_id": v["project_id"],
                "missing": "、".join(quality.missing_labels),
            })
    findings = {
        "total": len(vulns),
        "by_severity": {s: by_severity.get(s, 0) for s in _SEVERITY_ORDER if by_severity.get(s)},
        "high_unconfirmed": sum(
            1 for v in vulns if v["severity"] in ("critical", "high") and v["status"] != "confirmed"
        ),
        "incomplete_evidence": len(incomplete),
        "incomplete_items": incomplete,
    }

    # ── 已沉淀知识（目标键维度，跨项目共享）─────────────────────────────────
    key = canonical_key(ref) or ref
    kinds = {
        r["kind"]: int(r["c"])
        for r in conn.execute(
            "SELECT kind, COUNT(*) c FROM knowledge_base WHERE root_domain = ? GROUP BY kind",
            (key,),
        ).fetchall()
    }
    dead_ends = [
        {"title": (r["title"] or "")[:160], "content": (r["content"] or "")[:400], "source": "知识库"}
        for r in conn.execute(
            "SELECT title, content FROM knowledge_base WHERE root_domain = ? AND kind = 'dead_end' "
            "ORDER BY updated_at DESC LIMIT 30",
            (key,),
        ).fetchall()
    ]
    # 事实层也要数（与 coverage / 阶段估算同一口径）：知识是在 `dead_end` 这个 kind 出现**之前**
    # 沉淀的项目（实测 proj_002 有 9 条已验证不通、知识库里却 0 条）。三处各写一套"什么算不通"
    # 正是这个项目反复出现的问题。
    from sharp.server.coverage import _headline, _looks_like_dead_end

    seen_titles = {d["title"] for d in dead_ends}
    for row in conn.execute(
        f"SELECT id, description FROM facts WHERE project_id IN ({placeholders}) "
        f"AND id NOT IN ('origin', 'goal') ORDER BY id",
        tuple(pids),
    ).fetchall():
        if not _looks_like_dead_end(row["description"]):
            continue
        headline = _headline(row["description"])
        if not headline or headline in seen_titles:
            continue
        seen_titles.add(headline)
        dead_ends.append({
            "title": headline, "content": row["description"][:400], "source": f"结论 {row['id']}",
        })

    def _total(table: str) -> int:
        return int(conn.execute(
            f"SELECT COUNT(*) c FROM {table} WHERE project_id IN ({placeholders})", tuple(pids)
        ).fetchone()["c"])

    return {
        "asset_ref": ref,
        "target_kinds": sorted({p["target_kind"] or "web" for p in projects}),
        "project_count": len(projects),
        "projects": [
            {
                "id": p["id"],
                "title": p["title"],
                "status": p["status"],
                "target_kind": p["target_kind"] or "web",
                "product": p["product"] or "",
                "created_at": p["created_at"],
            }
            for p in projects
        ],
        "endpoints": endpoints,
        "findings": findings,
        "knowledge": {
            "key": key,
            "by_kind": kinds,
            # 凭据只给条数：知识库里 credential 的 title 就是凭据本身（与覆盖报告同一口径）
            "credential_count": kinds.get("credential", 0),
            "dead_ends": dead_ends,
        },
        "activity": {
            "first_project_at": projects[0]["created_at"],
            "last_project_at": projects[-1]["created_at"],
            "fact_total": _total("facts"),
            "intent_total": _total("intents"),
            "vuln_total": len(vulns),
            "project_titles": {pid: titles[pid] for pid in pids},
        },
    }


def _has_asset_endpoints(conn: sqlite3.Connection) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'asset_endpoints'"
    ).fetchone() is not None
