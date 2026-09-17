#!/usr/bin/env python3
"""backfill_flags.py — 把历史任务里"只写在文字里"的旗帜成绩结构化落库。

## 为什么需要它

评分类任务（CTF / 授权评测平台）的产出是**旗帜与分值**，但早期任务跑完后，
成绩只留在证据描述的自然语言里（例如"flag{...}，提交 correct=true +300"），
系统里没有任何 `kind='flag'` 记录 —— 于是 `GET /projects/{id}/scoreboard`
显示 0 分，项目也无法按分数复盘。

本工具读**证据链**（facts 表）里的文本，提取 `flag{...}` / `a-NN` 题号 / 分值，
经服务端 API `POST /projects/{pid}/vulnerabilities` 补登为 `kind='flag'` 记录。

## 设计约束

- **幂等**：同一 flag 值只登记一次（与库中已有 flag 记录比对），重复执行安全。
- **默认 dry-run**：不加 `--apply` 只打印将要写入的内容，不碰数据库。
- **走 API 而非直连 SQLite**：保持 id 生成、SSE 通知、时间戳与服务端一致。
- **不动原始证据**：只新增产物记录，不改写任何 fact。

## 用法

    # 预览（默认，不写库）
    python3 tools/backfill_flags.py --project proj_004

    # 实际写入
    python3 tools/backfill_flags.py --project proj_004 --apply

token 解析顺序：--token 参数 > SHARP_SERVER_TOKEN 环境变量 > 数据库同目录的 server.token。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_DB = Path.home() / ".local/share/sharp/sharp.db"

# 旗帜值：flag{...}（允许连字符、下划线；基准平台多为 UUID）
FLAG_RE = re.compile(r"flag\{([^}\s]{4,})\}", re.IGNORECASE)
# 题号：a-01 / A-18
TASK_RE = re.compile(r"\b([aA]-\d{1,2})\b")
# 分值：优先取自平台回执样式的文本
SCORE_PATTERNS = (
    re.compile(r"correct\s*=\s*true[^+\d]{0,12}\+\s*(\d{1,5})", re.IGNORECASE),
    re.compile(r"\+\s*(\d{1,5})\s*(?:分|points)?\b"),
    re.compile(r"total_score\D{0,4}(\d{1,5})", re.IGNORECASE),
    re.compile(r"得分\D{0,4}(\d{1,5})"),
)
# 就近匹配窗口：题号/分值通常出现在旗帜值附近
WINDOW = 240


def resolve_token(cli_token: str | None, db_path: Path) -> str:
    if cli_token:
        return cli_token
    env = os.environ.get("SHARP_SERVER_TOKEN")
    if env:
        return env
    token_file = db_path.parent / "server.token"
    if token_file.exists():
        return token_file.read_text(encoding="utf-8").strip()
    return ""


def load_facts(db_path: Path, project_id: str) -> list[tuple[str, str]]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT id, description FROM facts WHERE project_id = ? ORDER BY id",
            (project_id,),
        ).fetchall()
    finally:
        conn.close()
    return [(r[0], r[1] or "") for r in rows]


def extract_flags(facts: list[tuple[str, str]]) -> list[dict]:
    """从证据文本提取 (flag, 题号, 分值, 来源 fact)。同 flag 值只保留首见。

    题号/分值的定位规则（按真实证据文本校准过）：
    - **题号**取 flag 之前**最后一个** `a-NN`。不能取窗口内第一个：证据里常有
      "已获取 18 道题清单（a-01~a-18）"这类汇总句，会把它误当成当前题目。
    - **分值**优先在「题号 → flag」之间找 `+N`（如 "a-05（easy，+100，flag{...}"）；
      找不到再退到 flag 之前的整段取最后一个 `+N`（如 "已完成 a-08 并提交通关(+500)"
      与旗帜出现在同一段的情况）；仍找不到就记 **0** —— 宁可缺分，不猜分。
    """
    seen: dict[str, dict] = {}
    for fact_id, text in facts:
        lowered = text.lower()
        if "flag{" not in lowered:
            continue
        for m in FLAG_RE.finditer(text):
            value = m.group(1).strip()
            key = value.lower()
            if key in seen:
                continue
            pre = text[: m.start()]
            post = text[m.end(): m.end() + WINDOW]
            task = ""
            task_at = -1
            pre_tasks = list(TASK_RE.finditer(pre))
            if pre_tasks:
                task = pre_tasks[-1].group(1).lower()
                task_at = pre_tasks[-1].start()
            else:
                post_m = TASK_RE.search(post)
                if post_m:
                    task = post_m.group(1).lower()
            score = 0
            scope = pre[task_at:] if task_at >= 0 else ""
            for pat in SCORE_PATTERNS:
                sm = pat.search(scope)
                if sm:
                    score = int(sm.group(1))
                    break
            if score == 0:
                # 最常见写法是分数跟在旗帜后面："flag{...}，提交 correct=true +300"
                # 只在紧邻的 120 字符内取，避免把下一题的分数算到本题
                near_post = post[:120]
                for pat in SCORE_PATTERNS:
                    sm = pat.search(near_post)
                    if sm:
                        score = int(sm.group(1))
                        break
            if score == 0:
                tail = list(SCORE_PATTERNS[1].finditer(pre))
                if tail:
                    score = int(tail[-1].group(1))
            seen[key] = {
                "flag": value,
                "task": task,
                "score": score,
                "fact_id": fact_id,
                "context": text[max(0, m.start() - WINDOW): min(len(text), m.end() + 60)]
                .strip().replace("\n", " ")[:240],
            }
    # 排序：有题号的按题号，其余排后面
    return sorted(seen.values(), key=lambda d: (d["task"] == "", d["task"] or d["flag"]))


def existing_flag_keys(base_url: str, project_id: str, token: str) -> set[str]:
    """已登记的旗帜值（用于幂等）。"""
    req = urllib.request.Request(
        f"{base_url}/projects/{project_id}/vulnerabilities",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        items = json.loads(resp.read().decode("utf-8"))
    keys: set[str] = set()
    for it in items:
        if it.get("kind") != "flag":
            continue
        blob = f"{it.get('title','')} {it.get('description','')}"
        for m in FLAG_RE.finditer(blob):
            keys.add(m.group(1).strip().lower())
    return keys


def post_flag(base_url: str, project_id: str, token: str, item: dict) -> None:
    title = f"{item['task']} 通关旗帜" if item["task"] else "通关旗帜"
    body = {
        "fact_id": item["fact_id"],
        "kind": "flag",
        "score": item["score"],
        "title": title,
        "severity": "info",
        "status": "confirmed",
        "description": f"flag{{{item['flag']}}}（来源证据 {item['fact_id']}）",
        "evidence": item["context"],
    }
    req = urllib.request.Request(
        f"{base_url}/projects/{project_id}/vulnerabilities",
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        resp.read()


def main() -> int:
    ap = argparse.ArgumentParser(description="把证据链里的旗帜成绩补登为 kind=flag 记录")
    ap.add_argument("--project", required=True, help="项目 id，例如 proj_004")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB, help=f"SQLite 路径（默认 {DEFAULT_DB}）")
    ap.add_argument("--base-url", default="http://127.0.0.1:8000", help="Sharp server 地址")
    ap.add_argument("--token", default=None, help="服务端 token（默认读 env / server.token）")
    ap.add_argument("--apply", action="store_true", help="真正写入（默认仅预览）")
    args = ap.parse_args()

    if not args.db.exists():
        print(f"✗ 数据库不存在：{args.db}", file=sys.stderr)
        return 2

    facts = load_facts(args.db, args.project)
    if not facts:
        print(f"✗ 项目 {args.project} 没有证据记录", file=sys.stderr)
        return 2
    items = extract_flags(facts)
    print(f"项目 {args.project}：扫描 {len(facts)} 条证据，提取到 {len(items)} 个旗帜\n")
    if not items:
        print("没有可提取的 flag{...}，无需回填。")
        return 0

    token = resolve_token(args.token, args.db)
    existing: set[str] = set()
    if token:
        try:
            existing = existing_flag_keys(args.base_url, args.project, token)
        except urllib.error.URLError as exc:
            print(f"⚠ 读取已有旗帜失败（{exc}）；幂等检查跳过", file=sys.stderr)
    else:
        print("⚠ 未找到服务端 token；无法检查已有记录（仍可预览）", file=sys.stderr)

    pending = [it for it in items if it["flag"].lower() not in existing]
    total_score = sum(it["score"] for it in pending)
    print(f"{'题号':<6}{'分值':>6}  {'来源':<7} flag")
    print("-" * 72)
    for it in items:
        mark = "已存在" if it["flag"].lower() in existing else "待写入"
        print(f"{it['task'] or '—':<6}{it['score']:>6}  {it['fact_id']:<7} flag{{{it['flag'][:26]}}}… [{mark}]")
    print("-" * 72)
    print(f"待写入 {len(pending)} 条，合计 {total_score} 分（已有 {len(items) - len(pending)} 条跳过）")

    if not args.apply:
        print("\n[dry-run] 未做任何写入。确认无误后加 --apply 执行。")
        return 0
    if not token:
        print("✗ --apply 需要服务端 token（--token / SHARP_SERVER_TOKEN / server.token）", file=sys.stderr)
        return 2

    written = 0
    for it in pending:
        try:
            post_flag(args.base_url, args.project, token, it)
            written += 1
            print(f"  ✓ 已登记 {it['task'] or it['flag'][:12]}（{it['score']} 分）")
        except urllib.error.HTTPError as exc:
            print(f"  ✗ 写入失败 {it['task'] or it['flag'][:12]}：HTTP {exc.code} {exc.read()[:160]!r}", file=sys.stderr)
        except urllib.error.URLError as exc:
            print(f"  ✗ 写入失败：{exc}", file=sys.stderr)
    print(f"\n完成：写入 {written}/{len(pending)} 条。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
