"""环境基线仓储（P0-2）。

基线 = **已确认的作业前提**（网络连通性、平台凭据有效性、目标可达性、工作目录布局…），
跨会话复用。它与 `facts` 刻意的区别：

- `facts` 是探索**结论**：参与证据链、会被引用与审计，通常每个行动新增若干条；
- `baseline` 是**前提**：同一件事只需确认一次，之后每轮直接读，不该反复探测。

实测依据：一次长任务里同一条网络预检被重复执行了 144 次——每轮行动都是独立会话，
上下文从零构建，于是"已经确认过的事"被反复支付。基线就是用来消灭这类重复的。
"""

from __future__ import annotations

import sqlite3


def list_for_project(conn: sqlite3.Connection, project_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT key, value, note, source, updated_at FROM env_baseline "
        "WHERE project_id = ? ORDER BY key",
        (project_id,),
    ).fetchall()


def upsert(
    conn: sqlite3.Connection,
    project_id: str,
    key: str,
    value: str,
    note: str,
    source: str,
    now: str,
) -> None:
    conn.execute(
        "INSERT INTO env_baseline (project_id, key, value, note, source, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(project_id, key) DO UPDATE SET "
        "value = excluded.value, note = excluded.note, source = excluded.source, "
        "updated_at = excluded.updated_at",
        (project_id, key, value, note, source, now),
    )


def delete(conn: sqlite3.Connection, project_id: str, key: str) -> None:
    conn.execute("DELETE FROM env_baseline WHERE project_id = ? AND key = ?", (project_id, key))


def list_inherited(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    sibling_ids: list[str],
    skip_keys: set[str],
    limit: int = 50,
) -> list[sqlite3.Row]:
    """从**同目标**的其他项目继承基线条目（读侧继承，不复制数据）。

    为什么这样做而不是建库时拷贝一份：拷贝之后两边会各自演化 ——
    一个项目里人工纠正过的前提，另一个项目永远看不到。读侧继承则始终反映最新事实，
    并且**带来源**（`source_project_id` / `source_project_title`）：worker 与人都能判断
    "这条是这台机器上确认的，还是同目标另一个项目确认的"。

    `skip_keys`：本项目自己已有的键优先，不被继承值覆盖（本地事实永远赢）。
    按 `updated_at` 倒序取最新的，同一键只保留第一条。
    """
    if not sibling_ids:
        return []
    placeholders = ",".join("?" for _ in sibling_ids)
    rows = conn.execute(
        "SELECT b.key, b.value, b.note, b.source, b.updated_at, "
        "       b.project_id AS source_project_id, p.title AS source_project_title "
        f"FROM env_baseline b JOIN projects p ON p.id = b.project_id "
        f"WHERE b.project_id IN ({placeholders}) "
        # `updated_at` 是**秒级**时间戳：同一秒内写入的多条会并列，此时"取最新"就是任意的。
        # 用 rowid 兜底（隐式自增，越晚插入越大），保证结果是确定的 ——
        # 否则"同目标多个项目都确认过同一个键"时，谁赢取决于数据库的返回顺序。
        "ORDER BY b.updated_at DESC, b.rowid DESC",
        tuple(sibling_ids),
    ).fetchall()

    out: list[sqlite3.Row] = []
    seen: set[str] = set()
    for row in rows:
        key = row["key"]
        if key in skip_keys or key in seen:
            continue
        seen.add(key)
        out.append(row)
        if len(out) >= limit:
            break
    return out
