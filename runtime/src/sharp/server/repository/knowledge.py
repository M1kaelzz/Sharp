"""SQL for the ``knowledge_base`` table.

Global cross-project knowledge (credentials, endpoints, fingerprints).
Functions take an open connection, return rows/None; callers own the transaction.
"""

from __future__ import annotations

import sqlite3

_COLUMNS = (
    "id, root_domain, kind, title, content, source_project_id, product, "
    "confidence, created_at, updated_at"
)


def upsert(
    conn: sqlite3.Connection,
    root_domain: str,
    kind: str,
    title: str,
    content: str,
    *,
    source_project_id: str | None = None,
    product: str = "",
    confidence: str = "high",
    now: str,
) -> int | None:
    """Insert or update by (root_domain, kind, title). Returns the row id.

    `product` 是产品/指纹维度（见 `find_matching`）：同域名匹配之外的复用通道。
    """
    existing = conn.execute(
        "SELECT id FROM knowledge_base "
        "WHERE root_domain = ? AND kind = ? AND title = ?",
        (root_domain, kind, title),
    ).fetchone()
    if existing is not None:
        conn.execute(
            "UPDATE knowledge_base SET content = ?, source_project_id = ?, "
            "product = ?, confidence = ?, updated_at = ? WHERE id = ?",
            (content, source_project_id, product, confidence, now, existing["id"]),
        )
        return existing["id"]
    cursor = conn.execute(
        f"INSERT INTO knowledge_base ({_COLUMNS}) "
        "VALUES (NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            root_domain, kind, title, content,
            source_project_id, product, confidence, now, now,
        ),
    )
    return cursor.lastrowid


def find_by_root_domain(
    conn: sqlite3.Connection, root_domain: str
) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM knowledge_base WHERE root_domain = ? "
        "ORDER BY kind, updated_at DESC",
        (root_domain,),
    ).fetchall()


def list_all(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM knowledge_base ORDER BY root_domain, kind, updated_at DESC",
    ).fetchall()


def fetch(conn: sqlite3.Connection, kb_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM knowledge_base WHERE id = ?",
        (kb_id,),
    ).fetchone()


def delete(conn: sqlite3.Connection, kb_id: int) -> None:
    conn.execute("DELETE FROM knowledge_base WHERE id = ?", (kb_id,))


def find_matching(
    conn: sqlite3.Connection,
    *,
    key: str,
    product: str = "",
    limit: int = 40,
) -> list[sqlite3.Row]:
    """按两级维度取知识：先**同目标**（规范键相同），再**同产品**（键不同但产品相同）。

    两级分开返回（`root_domain` 匹配的排在前面）是为了让调用方能按来源分别呈现 ——
    "这台机器上已知的东西"和"同类产品上已知的东西"可信度并不一样，
    混在一个列表里会误导判断。

    同产品匹配**要求 product 非空**：空字符串是"未标注"，不是"同一个产品"，
    否则所有未标注的行会互相匹配，等于没匹配。
    """
    rows: list[sqlite3.Row] = []
    seen: set[int] = set()

    same_key = conn.execute(
        "SELECT * FROM knowledge_base WHERE root_domain = ? "
        "ORDER BY kind, updated_at DESC LIMIT ?",
        (key, limit),
    ).fetchall()
    for row in same_key:
        rows.append(row)
        seen.add(row["id"])

    if product.strip() and len(rows) < limit:
        same_product = conn.execute(
            "SELECT * FROM knowledge_base WHERE product = ? AND root_domain != ? "
            "ORDER BY kind, updated_at DESC LIMIT ?",
            (product.strip(), key, limit - len(rows)),
        ).fetchall()
        for row in same_product:
            if row["id"] not in seen:
                rows.append(row)
                seen.add(row["id"])

    return rows
