"""Knowledge base routes.

The knowledge_base is a global (non-project-scoped) table that stores
credentials, endpoints, and fingerprints extracted from fact descriptions
during explore tasks. When a new project is created, matching entries are
injected as hints so the agent can leverage prior findings.

Extraction endpoint: called by the dispatcher after a fact is written.
The server resolves root_domain from the project's origin fact, then tries
to extract knowledge from the fact description.
"""

from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException

from sharp.server.db import get_conn
from sharp.server.models import (
    CreateKnowledgeRequest,
    ExtractKnowledgeRequest,
    KnowledgeEntry,
    SetProductRequest,
)
from sharp.server.repository import knowledge as kb_repo
from sharp.server.repository import facts as facts_repo
from sharp.server.hostkey import (
    canonical_key,
    key_from_sources,
    key_from_text,
)
from sharp.server.services import utcnow

router = APIRouter(tags=["knowledge"])


def _extract_root_domain(origin: str) -> str | None:
    """把项目的 origin 事实折算成规范知识键。

    **这里曾经是整个复用链路的断点**。旧实现只处理两种形态："以 `://` 开头的 URL"
    或"裸域名"，其余走 `else` 分支按 `/` 和 `:` 切割。但真实 origin 事实是**句子**：

        目标地址：https://app.fh.example.com/cgi-bin/MANGA/index.cgi
        授权范围：仅主域

    这种输入切出 `目标地址：https` → 再按 `.` 分段不足两段 → 返回 None →
    `inject_knowledge_hints` 直接 `return []`。也就是说 **proj_002 那次的跨目标知识注入
    从未执行过一次**，而代码看起来完全正常。

    现在统一交给 `hostkey.canonical_key`：先在原文里扫出 URL/主机（`key_from_text`），
    再退化为直接解析。键形如 `domain:example.com` / `ip:10.0.100.58` / `host:internal-api`。
    """
    return key_from_sources(origin)


def _first_host_in_text(text: str) -> str | None:
    """自由文本里第一个可信主机 → 规范键（内部与上方同一口径）。"""
    return key_from_text(text)


# 从事实描述里抽取知识的正则。刻意保守：只抽看起来确实是凭据/接口/指纹的东西，
# 不抽任意文本 —— 垃圾条目比没有条目更糟，因为它会污染匹配。
_CRED_PATTERNS = [
    # token=xxx, password=xxx, api_key=xxx etc.
    # 字符集必须覆盖真实口令里的常见特殊字符：此前只有 `[A-Za-z0-9_\-\.=:+/]`，
    # 于是 `password=Admin@123` 在 `@` 处断掉、**存成 `password=Admin`** ——
    # 一条被截断的凭据不是"少一条"，是**一条错的**：worker 拿它去登录必然失败，
    # 还要再花一轮去怀疑方向。刻意不含 `*` 与 `#`（markdown 强调符会粘进值里）。
    re.compile(
        r"(?i)(?:token|password|passwd|api[_-]?key|secret|auth|bearer)"
        r"\s*[:=]\s*['\"]?([A-Za-z0-9_\-\.=:+/@!$%^&?]{8,})['\"]?"
    ),
    # JWT tokens
    re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),
]

_ENDPOINT_PATTERNS = [
    # URLs with paths
    re.compile(r"https?://[^\s<>'\")\]]+"),
    # IP:port
    re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}:\d{2,5}\b"),
]

_FINGERPRINT_PATTERNS = [
    # Server header values
    re.compile(r"(?i)Server:\s*(.+)"),
    # X-Powered-By values
    re.compile(r"(?i)X-Powered-By:\s*(.+)"),
    # Framework / technology mentions
    re.compile(r"(?i)(?:framework|powered by|running)\s+(.+?)(?:\.|$)"),
]

# 指纹/标题都是要**被阅读**的短值，不是段落。实测未加界时会抓到整段结论
# （标题里塞了 2000 字符的探测过程），注入提示词等于投毒。这里在第一个分隔符处截断。
_VALUE_CUTS = ("；", ";", "，", ",", "(", "（", "。", "\n")

# 明显是"占位符/模板变量"而不是真凭据的值 —— 在**抽取时**就丢掉。
#
# 为什么不能留给人工清理：凭据类知识注入时的语义是"已知凭据（同目标）"，也就是
# **可以直接用**。一条 `secret=PLACEHOLDER_FLAG1` 留在库里，下游会拿它去跑一轮
# 真实登录。实测（TSec 跑分，2026-09-16）从源码引文里抽到了
# `password=0/true/null/`、`secret=PLACEHOLDER_FLAG1`、`api_key=dk_live_a1b2c3d4e5f6g7h8i9j0`
# 三条合成值，混在真凭据里一起喂给规划器 —— 假知识的代价比漏一条大，
# 与本文件上方"不复用垃圾键"是同一条口径。
_SYNTHETIC_CRED_RE = re.compile(
    r"(?i)placeholder|dummy|sample|changeme|change_me"
    r"|your[_-]?(?:pass|passwd|pwd|token|key|secret)"
    r"|a1b2c3d4|xxxx+|0/true/null|\{\{|\$\{|<[a-z_]+>"
)

# 值本身就是"密钥变量名"的模板变量（`1panel_password`、`db_token`）不是凭据。
_TEMPLATE_VAR_RE = re.compile(
    r"[A-Za-z0-9]+_(?:password|passwd|pwd|token|secret|api_?key)", re.I
)


def _looks_synthetic_credential(value: str) -> bool:
    """这个"凭据"是不是占位符/模板变量（而不是真的能用）？"""
    text = (value or "").strip()
    if _SYNTHETIC_CRED_RE.search(text):
        return True
    tail = re.split(r"[:=]\s*", text, maxsplit=1)[-1].strip().strip("'\"")
    return bool(_TEMPLATE_VAR_RE.fullmatch(tail))


def _bounded_value(raw: str, limit: int = 80) -> str:
    """把抓到的值截成"一眼能读完"的短值。

    先切到第一个分隔符，再按长度截断 —— 指纹本来就该是 `nginx/1.18.0` 这种，
    不是"某次探测做了什么"的叙述。
    """
    text = (raw or "").strip()
    cut = len(text)
    for sep in _VALUE_CUTS:
        idx = text.find(sep)
        if idx > 0:
            cut = min(cut, idx)
    text = text[:cut].strip()
    return text[:limit]


@router.post("/knowledge/extract")
def extract_knowledge(body: ExtractKnowledgeRequest):
    """Extract knowledge from a fact description and store it in the
    knowledge_base. Called by the dispatcher after a fact is written.

    The key is resolved from the project's origin fact (which is a **sentence**,
    not a bare URL — see `_extract_root_domain`). If the origin yields nothing
    (e.g. APK / miniprogram projects whose origin is a local path or AppID),
    fall back to the first host found inside the fact description itself —
    mobile analysis facts routinely carry the API hosts they discovered, so
    their knowledge can still flow into the shared base under a usable key.

    Nothing usable at all → store nothing. **不复用垃圾键**：一条挂在
    `flag.txt` 上的知识比没有知识更糟，因为它永远匹配不上还占着位置。
    """
    with get_conn() as conn:
        origin_row = facts_repo.fetch(conn, "origin", body.project_id)
        if origin_row is None:
            raise HTTPException(404, f"Project {body.project_id} or its origin fact not found")
        now = utcnow()
        result = extract_knowledge_for_fact(
            conn, project_id=body.project_id, description=body.description, now=now
        )
        if result is None:
            return {"extracted_count": 0, "root_domain": None}
        return result


def extract_knowledge_for_fact(
    conn, *, project_id: str, description: str, now: str
) -> dict | None:
    """从一条事实描述里抽取跨项目知识并落库。**可被任意写 fact 的入口复用。**

    抽成函数（而不是只留在端点里）的原因，是本项目反复出现过的同一类缺陷：
    某个能力只接在**一条**路径上。知识提取此前只由 dispatcher 在 explore/bootstrap
    之后触发 —— 于是经**chat「写入证据图」**、**重开项目的外部反馈**、
    **Android/小程序分析事实**写入的证据，从来不产出任何知识。
    现在这些入口统一走 `server/fact_hooks.py::after_fact_write()`。

    返回 `None` 表示"该项目没有可归属的主机"（调用方按 0 条处理）。
    """
    origin_row = facts_repo.fetch(conn, "origin", project_id)
    root_domain = _extract_root_domain(origin_row["description"]) if origin_row else None
    if root_domain is None:
        # Mobile (APK/miniprogram) fallback: host from the fact description itself.
        root_domain = _first_host_in_text(description)
    if root_domain is None:
        return None

    # 产品维度随项目走：条目落库时带上它，才能被"同类产品的另一个目标"匹配到。
    project_row = conn.execute(
        "SELECT product FROM projects WHERE id = ?", (project_id,)
    ).fetchone()
    product = (project_row["product"] if project_row else "") or ""
    count = _do_extract_with_conn(
        conn, description, root_domain, project_id, now, product=product
    )
    return {"extracted_count": count, "root_domain": root_domain, "product": product}


# 「这条路不通」的强标记。命中即产出一条 `dead_end` 知识。
# 为什么值得单独一类：**否定结论是跨产品最有价值的情报** ——
# 正向结论（某个接口长什么样）换个产品通常失效，但"MANGA 的这个接口不吃注入"
# 对下一个同类产品直接省掉整轮尝试。实测一次真实项目产出 10 条这类结论，
# 却因为被归进 endpoint 而无法被任何匹配命中、也不会被专门提示给后续项目。
_DEAD_END_MARKERS = (
    "不适用", "不可行", "无法利用", "试过不通", "走不通", "行不通", "无效",
    "未发现", "不存在", "未授权访问不存在", "均失败", "全部失败", "无果",
    # 实测补入：探测"全部 404 / 无泄露"这类措辞同样是"这条路走不通"
    "全部 404", "均返回 404", "全部返回 404", "无泄露", "未泄露", "无配置文件",
    "均无响应", "无响应", "无法绕过",
    "not applicable", "not vulnerable", "ineffective", "dead end", "no effect",
    "no bypass", "unexploitable",
    # 中文措辞（与 coverage.py 同一份语义）：真实结论里工人写"本 intent 为死胡同"、
    # "结论为负"这类说法，只认英文等于漏报。
    "死胡同", "结论为负", "结果为负", "未获取有效会话", "无可确认漏洞", "未确认漏洞",
)


# 否定标记只在**结论头部**才算数：全文匹配会把"目标描述里恰好出现的否定词"也算进来，
# 产出一条假的 dead_end —— 而假死胡同的危害是**让后续项目不敢重试**，比漏报更糟。
_DEAD_END_HEADLINE_WINDOW = 300


def _looks_like_dead_end(description: str) -> bool:
    lowered = (description or "").lower()[:_DEAD_END_HEADLINE_WINDOW]
    return any(marker in lowered for marker in _DEAD_END_MARKERS)


def _do_extract_with_conn(
    conn,
    description: str,
    root_domain: str,
    project_id: str,
    now: str,
    *,
    product: str = "",
) -> int:
    """Extract knowledge entries from a fact description and upsert them using
    the given connection. Returns the number of entries extracted."""
    entries: list[tuple[str, str, str]] = []

    for pattern in _CRED_PATTERNS:
        for match in pattern.finditer(description):
            value = match.group(0).strip()
            if len(value) > 10 and not _looks_synthetic_credential(value):
                entries.append(("credential", value[:80], value))

    for pattern in _ENDPOINT_PATTERNS:
        for match in pattern.finditer(description):
            value = match.group(0).strip().rstrip(".,;)")
            if len(value) > 8:
                entries.append(("endpoint", value[:80], value))

    for pattern in _FINGERPRINT_PATTERNS:
        for match in pattern.finditer(description):
            raw = match.group(1) if match.lastindex else match.group(0)
            value = _bounded_value(raw)
            if len(value) > 2:
                entries.append(("fingerprint", value[:80], value))

    # 否定结论单独成类（标题取首行，便于同一条结论幂等 upsert）
    if _looks_like_dead_end(description):
        headline = next(
            (line.strip() for line in (description or "").splitlines() if line.strip()),
            "",
        )
        if headline:
            entries.append(("dead_end", headline[:80], description.strip()[:2000]))

    count = 0
    for kind, title, content in entries:
        try:
            kb_repo.upsert(
                conn,
                root_domain,
                kind,
                title,
                content,
                source_project_id=project_id,
                product=product,
                confidence="medium",
                now=now,
            )
            count += 1
        except Exception:
            pass
    return count


@router.get("/knowledge", response_model=list[KnowledgeEntry])
def list_knowledge():
    with get_conn() as conn:
        rows = kb_repo.list_all(conn)
        return [KnowledgeEntry(**dict(r)) for r in rows]


@router.get("/knowledge/{root_domain}", response_model=list[KnowledgeEntry])
def list_knowledge_by_domain(root_domain: str):
    with get_conn() as conn:
        rows = kb_repo.find_by_root_domain(conn, root_domain)
        return [KnowledgeEntry(**dict(r)) for r in rows]


@router.post("/knowledge", response_model=KnowledgeEntry, status_code=201)
def create_knowledge(body: CreateKnowledgeRequest):
    with get_conn() as conn:
        now = utcnow()
        kb_id = kb_repo.upsert(
            conn,
            body.root_domain.strip(),
            body.kind,
            body.title.strip(),
            body.content.strip(),
            source_project_id=None,
            confidence=body.confidence,
            now=now,
        )
        row = kb_repo.fetch(conn, kb_id)
        assert row is not None
        return KnowledgeEntry(**dict(row))


@router.delete("/knowledge/{kb_id}", status_code=204)
def delete_knowledge(kb_id: int):
    with get_conn() as conn:
        row = kb_repo.fetch(conn, kb_id)
        if row is None:
            raise HTTPException(404, "Knowledge entry not found")
        kb_repo.delete(conn, kb_id)


_KIND_LABELS = {
    "credential": "凭据",
    "endpoint": "接口",
    "fingerprint": "指纹",
    "dead_end": "已验证不通",
    "other": "其他",
}


def _format_knowledge_block(
    key: str,
    rows: list[dict],
    *,
    origin_label: str,
) -> str:
    """把一个来源下的知识条目渲染成给 worker 看的文本块。

    按 kind 分区而不是平铺：`dead_end` 是**禁止重复尝试**的语义务，凭据是"可直接用"，
    接口是"已知存在"，三者的用法完全不同。旧的平铺格式（`Type: xxx / Title: ...`）
    把结论和禁止项混在一起，等于把最有价值的那类信息埋掉了。
    """
    lines: list[str] = []
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(str(row.get("kind") or "other"), []).append(row)

    def _clip(text: str, limit: int = 300) -> str:
        """注入提示词的条目要有界：一条 2000 字符的结论会挤掉整段提示词预算。"""
        body = (text or "").strip().replace("\n", " ")
        return body if len(body) <= limit else body[:limit] + " …（完整内容见知识库）"

    if grouped.get("dead_end"):
        lines.append(f"已知不通的路径（{origin_label}）—— **不要重复尝试**，除非有新证据：")
        for row in grouped["dead_end"]:
            lines.append(f"  - {_clip(str(row['title']), 120)}: {_clip(str(row['content']))}")
    for kind in ("credential", "fingerprint", "endpoint", "other"):
        items = grouped.get(kind) or []
        if not items:
            continue
        label = _KIND_LABELS.get(kind, kind)
        lines.append(f"已知{label}（{origin_label}）：")
        for row in items:
            lines.append(f"  - {_clip(str(row['title']), 120)}: {_clip(str(row['content']))}")
    return "\n".join(lines)


def lookup_knowledge(conn, project_id: str, origin: str, *, product: str = "") -> list[dict]:
    """按两级维度取本项目可用的知识，返回原始行字典列表。

    两级：**同目标**（规范键一致）优先，其次**同产品**（键不同但产品相同）。
    后者正是"跨产品复用"的落点 —— 只靠 root_domain 时，Peplink 上的经验永远
    到不了另一个 Peplink 目标。
    """
    key = _extract_root_domain(origin)
    if key is None:
        return []
    rows = kb_repo.find_matching(conn, key=key, product=product)
    return [dict(r) for r in rows]


@router.get("/projects/{project_id}/knowledge")
def project_knowledge(project_id: str, limit: int = 40):
    """项目当前可用的知识（dispatcher 在 reason 阶段拉取，用于 `{knowledge}` 注入）。

    与**建项时**的 hint 注入互补：这里覆盖"项目进行中新积累出来的知识"——
    旧实现只在创建项目那一刻注入一次，跑了一半新学到的东西对当前项目永远不可见。
    """
    with get_conn() as conn:
        project_row = conn.execute(
            "SELECT product FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        if project_row is None:
            raise HTTPException(404, f"Project {project_id} not found")
        origin = conn.execute(
            "SELECT description FROM facts WHERE project_id = ? AND id = 'origin'",
            (project_id,),
        ).fetchone()
        if origin is None:
            return {"key": None, "product": "", "same_target": [], "same_product": [], "count": 0}

        key = _extract_root_domain(origin["description"])
        product = (project_row["product"] or "")
        if key is None:
            return {"key": None, "product": product, "same_target": [], "same_product": [], "count": 0}

        rows = kb_repo.find_matching(conn, key=key, product=product, limit=limit)
        same_target = [dict(r) for r in rows if r["root_domain"] == key]
        same_product = [dict(r) for r in rows if r["root_domain"] != key]
        return {
            "key": key,
            "product": product,
            "same_target": same_target,
            "same_product": same_product,
            "count": len(same_target) + len(same_product),
            "block": _format_knowledge_block(key, same_target, origin_label=f"同目标 {key}")
            if same_target else "",
            "product_block": _format_knowledge_block(key, same_product, origin_label=f"同产品 {product}")
            if same_product else "",
        }


@router.put("/projects/{project_id}/product")
def set_project_product(project_id: str, body: SetProductRequest):
    """标注项目目标属于哪个**产品**（如 `Peplink MANGA`）。

    这个字段唯一的用途是知识复用的第二维度：同名产品换一个域名/IP 部署时，
    旧实现的知识一条都过不来。标注允许为空（撤销标注）。

    已有非空标注时默认**不覆盖**（见 `SetProductRequest`）：产品名决定跨产品匹配
    走向，被某一轮猜测改写会让匹配结果漂移。
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT product FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(404, f"Project {project_id} not found")
        current = row["product"] or ""
        if current and not body.force and body.product != current:
            return {
                "project_id": project_id,
                "product": current,
                "updated": False,
                "reason": "已有标注，未覆盖（如需修改请带 force=true）",
            }
        conn.execute(
            "UPDATE projects SET product = ? WHERE id = ?", (body.product, project_id)
        )
        return {"project_id": project_id, "product": body.product, "updated": True}


def inject_knowledge_hints(conn, project_id: str, origin: str, now: str) -> list[dict]:
    """把匹配到的知识转成 hint（创建项目时调用）。

    注意这里是**建项时一次性**注入；项目进行中新积累的知识由 dispatcher 在
    reason 阶段通过 `GET /projects/{id}/knowledge` 拉取（见 `{knowledge}` 占位符）。
    """
    key = _extract_root_domain(origin)
    if key is None:
        return []

    project_row = conn.execute(
        "SELECT product FROM projects WHERE id = ?", (project_id,)
    ).fetchone()
    product = (project_row["product"] if project_row else "") or ""

    rows = kb_repo.find_matching(conn, key=key, product=product)
    if not rows:
        return []

    same_key = [dict(r) for r in rows if r["root_domain"] == key]
    same_product = [dict(r) for r in rows if r["root_domain"] != key]

    hints: list[dict] = []
    for group, label in ((same_key, f"同目标 {key}"), (same_product, f"同产品 {product}")):
        if not group:
            continue
        hints.append({
            "content": f"[知识库] {_format_knowledge_block(key, group, origin_label=label)}",
            "creator": "knowledge_base",
        })
    return hints

