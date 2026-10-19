#!/usr/bin/env python3
"""把 knowledge_base 里的历史垃圾键重算成规范键。

**背景**：旧的键提取只要求"点后面 ≥2 个字符"，于是文件名被当成域名。实测线上库
166 行 / 54 个键里，真域名只有 3 个（`flag.txt ×18`、`wordlist2.txt ×12`、
`rescan.py ×8`、`Next.js ×7`、`database.host ×6` …），甚至键里出现了整句中文
（`example.com）属不同产品，不适用。`）。这些行永远匹配不上任何项目，
等于知识复用从未生效。

**重算来源（按可信度排序）**：

1. `source_project_id` 指向项目的 origin 事实 —— 最可信（是当时的真实目标）；
2. 行自身 `content` / `title` 里出现的第一个可信主机 —— 实测救回率最高；
3. 都不行 → 键置为 `unattributed`（**保留数据可审计，但永不参与匹配**）。

刻意**不把旧键本身当来源**：旧键正是被判定为垃圾的东西，用垃圾推导垃圾只会
把 `database.host`、`lang.runtime` 这类占位符重新洗进库里。

**冲突处理**：`UNIQUE(root_domain, kind, title)` 下重算后可能撞车。撞车时保留
`updated_at` 最新的一行，其余按 `confidence` 低→高删除（数据不是为了丢，
所以每条删除都会打印出来）。

用法::

    python3 tools/rekey_knowledge.py                 # 预览（默认 dry-run）
    python3 tools/rekey_knowledge.py --apply         # 实际写入
    python3 tools/rekey_knowledge.py --db /path/to/sharp.db
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime" / "src"))

from sharp.server.hostkey import QUARANTINE, canonical_key, key_from_text  # noqa: E402

DEFAULT_DB = Path.home() / ".local" / "share" / "sharp" / "sharp.db"


def _origin_key(conn: sqlite3.Connection, project_id: str | None) -> str | None:
    if not project_id:
        return None
    row = conn.execute(
        "SELECT description FROM facts WHERE project_id = ? AND id = 'origin'",
        (project_id,),
    ).fetchone()
    if not row:
        return None
    origin = row[0]
    return canonical_key(origin) or key_from_text(origin)


def _row_key(conn: sqlite3.Connection, row: sqlite3.Row) -> tuple[str, str]:
    """返回 (新键, 来源说明)。"""
    key = _origin_key(conn, row["source_project_id"])
    if key:
        return key, "origin"
    key = key_from_text(row["content"])
    if key:
        return key, "content"
    key = key_from_text(row["title"])
    if key:
        return key, "title"
    return QUARANTINE, "unattributed"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite 路径")
    parser.add_argument("--apply", action="store_true", help="实际写入（默认只预览）")
    args = parser.parse_args()

    db_path = Path(args.db).expanduser()
    if not db_path.exists():
        print(f"[!] 数据库不存在：{db_path}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, root_domain, kind, title, content, source_project_id, confidence, updated_at "
        "FROM knowledge_base ORDER BY id"
    ).fetchall()
    if not rows:
        print("[=] knowledge_base 为空，无需迁移")
        return 0

    planned: list[tuple[int, str, str, str]] = []  # (id, 旧键, 新键, 来源)
    for row in rows:
        new_key, source = _row_key(conn, row)
        if new_key != row["root_domain"]:
            planned.append((row["id"], row["root_domain"], new_key, source))

    by_source = Counter(item[3] for item in planned)
    new_keys = Counter(item[2] for item in planned)

    print(f"[=] 共 {len(rows)} 行；需要改键 {len(planned)} 行")
    print(f"    改键来源：{dict(by_source)}")
    print("    改键后分布（前 10）：")
    for key, count in new_keys.most_common(10):
        print(f"      {key:34s} {count}")
    quarantined = new_keys.get(QUARANTINE, 0)
    if quarantined:
        print(f"    ⚠ {quarantined} 行无法归属，将置为 {QUARANTINE}（保留数据，永不参与匹配）")

    # 冲突检测：重算后 (key, kind, title) 撞车。
    # 必须**按签名分组后每组只留一个**：逐行两两比较会把"先被保留、随后又被更晚的行顶掉"
    # 的那一行同时记进 keep 和 drop（实测踩过：id=11 既在 keep 列表又在 drop 列表）。
    key_of = {item[0]: item[2] for item in planned}
    groups: dict[tuple[str, str, str], list[sqlite3.Row]] = {}
    for row in rows:
        key = key_of.get(row["id"], row["root_domain"])
        groups.setdefault((key, row["kind"], row["title"]), []).append(row)

    conflicts: list[tuple[sqlite3.Row, sqlite3.Row]] = []
    for signature, members in groups.items():
        if len(members) < 2:
            continue
        keeper = max(members, key=lambda r: ((r["updated_at"] or ""), r["id"]))
        for member in members:
            if member["id"] != keeper["id"]:
                conflicts.append((keeper, member))
    if conflicts:
        print(f"    ⚠ {len(conflicts)} 行撞车（同 key+kind+title 只能留一条，与运行时 upsert 语义一致）：")
        for keeper, loser in conflicts[:8]:
            print(f"      keep id={keeper['id']} drop id={loser['id']} "
                  f"key={key_of.get(keeper['id'], keeper['root_domain'])} kind={keeper['kind']}")

    if not args.apply:
        print("\n[=] dry-run：未写入任何内容。加 --apply 实际执行。")
        return 0

    with conn:
        for loser_row in [c[1] for c in conflicts]:
            conn.execute("DELETE FROM knowledge_base WHERE id = ?", (loser_row["id"],))
        for row_id, old_key, new_key, _source in planned:
            conn.execute(
                "UPDATE knowledge_base SET root_domain = ? WHERE id = ?",
                (new_key, row_id),
            )
    print(f"\n[+] 已改键 {len(planned)} 行，删除撞车 {len(conflicts)} 行")

    after = conn.execute("SELECT count(*) FROM knowledge_base").fetchone()[0]
    left = conn.execute(
        "SELECT count(*) FROM knowledge_base k WHERE NOT ("
        "  k.root_domain LIKE 'domain:%' OR k.root_domain LIKE 'ip:%' "
        "  OR k.root_domain LIKE 'host:%' OR k.root_domain = ?)",
        (QUARANTINE,),
    ).fetchone()[0]
    print(f"[=] 迁移后共 {after} 行；仍是非规范键 {left} 行（应为 0）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
