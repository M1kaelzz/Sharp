"""Structured asset endpoint ledger (batch A1).

Goal: when a project concludes an intent whose fact names URLs/endpoints, the
server records them in ``asset_endpoints`` keyed by the canonical host. Every
later project of the same asset can then *see* what was already discovered and
tested — turning the knowledge_base's soft hint memory into a structured
interface inventory with coverage state.

Coverage semantics (status):
  discovered  — seen in a concluded fact, not yet assessed this campaign
  verified    — a later fact/vuln confirmed behavior for this endpoint
  dismissed   — assessed and irrelevant / false positive
No UI writes status yet beyond registration; verified/dismissed are reserved
for the assessed flows that build on this table.
"""

from __future__ import annotations

import re
import sqlite3
from urllib.parse import urlsplit

# Static/resource suffixes that are assets but not interesting API endpoints.
_STATIC_SUFFIXES = (
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico", ".css", ".woff",
    ".woff2", ".ttf", ".eot", ".pdf", ".zip", ".tar", ".gz", ".mp4", ".mp3",
    ".wasm", ".map", ".js.map",
)
_URL_RE = re.compile(r"https?://[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%+-]+")
# Bare path-ish API references like /api/v1/users (schema-less) — skipped to
# avoid false positives; only full URLs are registered.
_MAX_PATHS_PER_FACT = 40  # cap pathological conclusion texts


def extract_endpoints_from_text(text: str) -> list[tuple[str, str]]:
    """Return deduplicated (host_lower, path) pairs for API-looking URLs in text.

    Heuristics (deliberately conservative — false registrations pollute the
    shared ledger):
    - full http(s):// URLs only (no schema-less guessing)
    - path must be non-empty, not a bare "/", not a static resource
    - at most _MAX_PATHS_PER_FACT endpoints per fact
    """
    if not text:
        return []
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str]] = []
    for m in _URL_RE.finditer(text):
        url = m.group(0).rstrip(".,;:!?)]}")
        try:
            parts = urlsplit(url)
        except ValueError:
            continue
        if parts.scheme not in ("http", "https") or not parts.netloc:
            continue
        host = (parts.netloc or "").lower()
        # strip userinfo + port for the canonical key (IPv6 kept as-is)
        if "@" in host:
            host = host.rsplit("@", 1)[1]
        if host.startswith("[") and "]" in host:
            host = host  # IPv6 literal
        else:
            host = host.split(":", 1)[0]
        path = parts.path or ""
        if not host or not path or path == "/":
            continue
        lowered_path = path.lower()
        if any(lowered_path.endswith(sfx) for sfx in _STATIC_SUFFIXES):
            continue
        key = (host, path)
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
        if len(out) >= _MAX_PATHS_PER_FACT:
            break
    return out


def register_endpoints_from_fact(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    fact_id: str,
    description: str,
    now: str,
) -> int:
    """Upsert every API-looking URL of a concluded fact into the ledger.

    Also backfills ``projects.asset_ref`` when a web project has no asset key
    yet (its origin had no parseable host) — the first concluded URL names it.
    Returns the number of endpoints registered/refreshed.
    """
    endpoints = extract_endpoints_from_text(description)
    if not endpoints:
        return 0

    # Backfill the project's asset key from the first endpoint host when the
    # project is web and currently uncategorized.
    row = conn.execute(
        "SELECT asset_ref, target_kind FROM projects WHERE id = ?", (project_id,)
    ).fetchone()
    if row is not None:
        first_host = endpoints[0][0]
        if row["target_kind"] == "web" and not row["asset_ref"]:
            conn.execute(
                "UPDATE projects SET asset_ref = ? WHERE id = ?",
                (first_host, project_id),
            )

    registered = 0
    for host, path in endpoints:
        conn.execute(
            "INSERT INTO asset_endpoints "
            "(asset_ref, method, path, source_project_id, source_fact_id, first_seen, last_seen) "
            "VALUES (?, '', ?, ?, ?, ?, ?) "
            "ON CONFLICT(asset_ref, method, path) DO UPDATE SET "
            "last_seen = excluded.last_seen",
            (host, path, project_id, fact_id, now, now),
        )
        registered += 1
    return registered


def endpoint_counts_by_asset(conn: sqlite3.Connection, asset_ref: str) -> dict:
    """(total, todo) counts for one asset — todo = discovered & never assessed."""
    row = conn.execute(
        "SELECT COUNT(*) c FROM asset_endpoints WHERE asset_ref = ?", (asset_ref,)
    ).fetchone()
    todo = conn.execute(
        "SELECT COUNT(*) c FROM asset_endpoints WHERE asset_ref = ? AND status = 'discovered'",
        (asset_ref,),
    ).fetchone()
    return {"total": int(row["c"]), "todo": int(todo["c"])}


# ── 端点评估（P1-B）：把"这个接口测过了"落进台账 ────────────────────────────
#
# 为什么必须有这一半：此前 `status` / `last_assessed_at` **全代码库无人写入** ——
# 状态机只有"创建"没有"推进"，于是所有行永远停在 `discovered`。
# 后果不是"少了个字段"，而是覆盖报告的「未验证接口」清单**只增不减**：
# 明明测过的接口会一直挂在盲区里，越用越吵，最后没人看。

ASSESSABLE_STATUSES = frozenset({"verified", "dismissed"})


def normalize_path(path: str) -> str:
    """接口路径归一化：去查询/片段、补前导斜杠、去尾斜杠（根路径除外）。

    不做归一化就会"同一个接口两行"：提取器存 `/api/v1/users`，worker 报
    `/api/v1/users?page=2`，两边永远匹配不上，评估落不到台账上。
    """
    text = (path or "").strip()
    for sep in ("?", "#"):
        if sep in text:
            text = text.split(sep, 1)[0]
    text = text.strip()
    if not text:
        return ""
    if not text.startswith("/"):
        text = "/" + text
    if len(text) > 1:
        text = text.rstrip("/") or "/"
    return text


def list_for_asset(conn: sqlite3.Connection, asset_ref: str) -> list[sqlite3.Row]:
    """某个资产的全部台账行（未验证的排前面，便于"接着测"）。"""
    return conn.execute(
        "SELECT e.*, p.title AS source_project_title "
        "FROM asset_endpoints e LEFT JOIN projects p ON p.id = e.source_project_id "
        "WHERE e.asset_ref = ? "
        "ORDER BY (e.status = 'discovered') DESC, e.path",
        (asset_ref,),
    ).fetchall()


def assess_endpoint(
    conn: sqlite3.Connection,
    *,
    asset_ref: str,
    method: str,
    path: str,
    status: str,
    note: str,
    project_id: str,
    fact_id: str | None,
    now: str,
) -> str:
    """把一条"测过了/排除了"的评估写进台账。返回 `updated` / `created` / `skipped`。

    - **匹配历史行时容忍 `method=''`**：提取阶段只记 path（method 留空），
      而 worker 报的是带方法的请求。若严格按 (method, path) 匹配，评估永远打不中历史行。
      命中空 method 的行时顺带把 method 补上（信息只增不减）。
    - 台账里没有这条记录时**新建**：worker 真的测过一个我们没登记过的接口，这本身就是覆盖证据。
    """
    norm_path = normalize_path(path)
    norm_method = (method or "").strip().upper()
    if not norm_path or status not in ASSESSABLE_STATUSES:
        return "skipped"

    updated = conn.execute(
        "UPDATE asset_endpoints SET status = ?, note = ?, last_assessed_at = ?, last_seen = ?, "
        "    method = CASE WHEN method = '' THEN ? ELSE method END "
        "WHERE asset_ref = ? AND path = ? AND (method = '' OR method = ?)",
        (status, note, now, now, norm_method, asset_ref, norm_path, norm_method),
    ).rowcount
    if updated:
        return "updated"

    conn.execute(
        "INSERT INTO asset_endpoints "
        "(asset_ref, method, path, source_project_id, source_fact_id, first_seen, last_seen, "
        " status, last_assessed_at, note) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(asset_ref, method, path) DO UPDATE SET "
        "  status = excluded.status, note = excluded.note, "
        "  last_assessed_at = excluded.last_assessed_at, last_seen = excluded.last_seen",
        (asset_ref, norm_method, norm_path, project_id, fact_id, now, now, status, now, note),
    )
    return "created"
