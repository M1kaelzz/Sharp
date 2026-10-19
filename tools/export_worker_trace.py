#!/usr/bin/env python3
"""export_worker_trace.py — 把 worker 容器里 claude-code 的解题过程导成可读 trace。

Sharp 的 worker 在容器内跑 claude-code，每次行动（bootstrap / reason / explore）产生一个
会话，记录落在容器内 `~/.claude/projects/<工作目录>/<session-id>.jsonl`。这些记录是
**真实的解题对话**：每一步推理、每条命令、每次输出。本工具把它们拉出来渲染成 Markdown，
用于复盘（哪一步卡住、哪个 flag 怎么拿到的）。

用法：

    # 导出某个项目容器的全部会话
    python3 tools/export_worker_trace.py --container sharp-dispatch-proj_001 \\
        --out "/Users/you/Downloads/sharp-traces"

    # 只导出最近 10 个会话，并限制每条输出长度
    python3 tools/export_worker_trace.py --container sharp-dispatch-proj_001 \\
        --out /tmp/trace --limit 10 --max-output 300

产出：
    <out>/worker-trace.md   —— 按时间排序的完整时间线（推理 / 命令 / 输出 / flag）
    <out>/flags-found.md    —— 从 trace 里正则提取的 flag 清单（含出现位置）
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import subprocess
import sys
from collections import Counter

FLAG_RE = re.compile(r"flag\{[^}\s]{4,}\}", re.IGNORECASE)
WORKDIR_IN_CONTAINER = "/home/kali/.claude/projects/-home-kali-workspace"


def docker(*args: str, capture: bool = True) -> str:
    res = subprocess.run(["docker", *args], capture_output=capture, text=True)
    if res.returncode != 0 and capture:
        print(f"docker {' '.join(args[:2])} 失败: {res.stderr.strip()[:200]}", file=sys.stderr)
    return res.stdout if capture else ""


def list_sessions(container: str) -> list[str]:
    out = docker("exec", container, "sh", "-c", f"ls -t {WORKDIR_IN_CONTAINER}/*.jsonl 2>/dev/null")
    return [line.strip() for line in out.splitlines() if line.strip()]


def copy_out(container: str, remote: str, dest_dir: pathlib.Path) -> pathlib.Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    docker("cp", f"{container}:{remote}", str(dest_dir))
    return dest_dir / pathlib.Path(remote).name


def parse_session(path: pathlib.Path, max_output: int) -> list[tuple[str, str, str]]:
    """→ [(timestamp, kind, text)]，kind ∈ {reason, tool:<Name>, result}"""
    events: list[tuple[str, str, str]] = []
    with path.open(encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            try:
                rec = json.loads(raw)
            except Exception:
                continue
            if rec.get("type") not in ("assistant", "user"):
                continue
            ts = str(rec.get("timestamp", ""))[11:19]  # 只留 HH:MM:SS
            content = (rec.get("message") or {}).get("content")
            if isinstance(content, str):
                items = [{"type": "text", "text": content}]
            elif isinstance(content, list):
                items = content
            else:
                continue
            role = (rec.get("message") or {}).get("role")
            for item in items:
                if not isinstance(item, dict):
                    continue
                kind = item.get("type")
                if kind == "text":
                    text = str(item.get("text") or "").strip()
                    if text:
                        # user 的文本是任务输入（prompt），只有 assistant 的才是推理
                        events.append((ts, "prompt" if role == "user" else "reason", text))
                elif kind == "tool_use":
                    payload = item.get("input") or {}
                    detail = (payload.get("command") or payload.get("file_path")
                              or payload.get("pattern") or payload.get("prompt")
                              or json.dumps(payload, ensure_ascii=False))
                    events.append((ts, f"tool:{item.get('name')}", str(detail).strip()))
                elif kind == "tool_result":
                    body = item.get("content")
                    if isinstance(body, list):
                        body = "\n".join(b.get("text", "") for b in body if isinstance(b, dict))
                    events.append((ts, "result", str(body).strip()[:max_output]))
    return events


def main() -> int:
    ap = argparse.ArgumentParser(description="导出 worker 容器内 claude-code 的解题 trace")
    ap.add_argument("--container", required=True, help="worker 容器名，如 sharp-dispatch-proj_001")
    ap.add_argument("--out", required=True, help="输出目录（宿主路径）")
    ap.add_argument("--limit", type=int, default=0, help="只导出最近 N 个会话（0=全部）")
    ap.add_argument("--max-output", type=int, default=500, help="单条工具输出保留的最大字符数")
    ap.add_argument("--keep-jsonl", action="store_true", help="同时保留原始 jsonl")
    args = ap.parse_args()

    out_dir = pathlib.Path(args.out).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir = out_dir / "_jsonl"

    sessions = list_sessions(args.container)
    if not sessions:
        print(f"容器 {args.container} 内没有找到会话记录", file=sys.stderr)
        return 2
    if args.limit:
        sessions = sessions[: args.limit]

    all_sessions: list[tuple[str, list[tuple[str, str, str]]]] = []
    for remote in sessions:
        sid = pathlib.Path(remote).stem
        local = copy_out(args.container, remote, tmp_dir)
        events = parse_session(local, args.max_output)
        if events:
            all_sessions.append((sid, events))

    tools = Counter()
    flags: dict[str, list[str]] = {}
    lines = ["# Sharp worker 解题 trace", "",
             f"- 容器：`{args.container}`",
             f"- 会话数：{len(all_sessions)}（每个会话 = 一次行动执行）", ""]

    for sid, events in all_sessions:
        first_ts = next((e[0] for e in events if e[0]), "")
        last_ts = next((e[0] for e in reversed(events) if e[0]), "")
        lines.append(f"\n---\n\n## 会话 `{sid[:8]}`　{first_ts} → {last_ts}　({len(events)} 步)\n")
        for ts, kind, text in events:
            if kind.startswith("tool:"):
                tools[kind[5:]] += 1
                lines.append(f"**[{ts}] 🔧 {kind[5:]}**\n\n```\n{text[:2000]}\n```\n")
            elif kind == "reason":
                lines.append(f"**[{ts}] 🤖 推理**\n\n{text[:1500]}\n")
            elif kind == "prompt":
                lines.append(f"**[{ts}] 📋 任务输入**\n\n```\n{text[:800]}\n```\n")
            else:
                found = FLAG_RE.findall(text)
                for f in found:
                    flags.setdefault(f, []).append(f"{sid[:8]}@{ts}")
                tag = "　🚩 **发现 flag**" if found else ""
                lines.append(f"**[{ts}] 📤 输出{tag}**\n\n```\n{text}\n```\n")

    trace_path = out_dir / "worker-trace.md"
    trace_path.write_text("\n".join(lines), encoding="utf-8")

    flag_lines = ["# trace 中出现的 flag", ""]
    for f, where in sorted(flags.items()):
        flag_lines.append(f"- `{f}`　出现 {len(where)} 次（{', '.join(where[:4])}）")
    (out_dir / "flags-found.md").write_text("\n".join(flag_lines), encoding="utf-8")

    if not args.keep_jsonl:
        for p in tmp_dir.glob("*.jsonl"):
            p.unlink()
        tmp_dir.rmdir()

    total_steps = sum(len(e) for _, e in all_sessions)
    print(f"✓ 会话 {len(all_sessions)} 个 / 步骤 {total_steps} 条")
    print(f"  工具分布：{dict(tools.most_common(8))}")
    print(f"  发现 flag：{len(flags)} 个")
    print(f"  trace：{trace_path}（{trace_path.stat().st_size // 1024} KB）")
    print(f"  flag 清单：{out_dir / 'flags-found.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
