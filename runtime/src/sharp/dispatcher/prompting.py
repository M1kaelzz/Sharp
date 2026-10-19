from __future__ import annotations

import json
from importlib import resources
from typing import Any


def load_prompt(group: str, name: str) -> str:
    return resources.files("sharp.dispatcher.prompts").joinpath(group).joinpath(name).read_text(encoding="utf-8")


# ── task-mode dependent prompt text ───────────────────────────────────────────
# Scoring is an opt-in task mode, not a core concept: penetration projects must
# not be told about flags/scores at all (it invites the model to invent
# "scored" semantics), while CTF / benchmark runs need the exact shape.
PENTEST_SCORING_NOTE = (
    "Task mode: **penetration test**. Report security findings only — do NOT emit "
    "`kind` or `score` fields, every finding is a vulnerability (`vuln`)."
)
# **一个 flag 只有在计分平台接受之后才算拿到。** 这条是 2026-09-16 真实跑分踩出来的：
# 工人按旧提示词"报出 flag"就收尾结轮了 —— 因为旧文案只描述了**我们这边的记录**（findings），
# 完全没提**平台的提交义务**，于是打了 RCE、读到了 flag.txt、写进结论，平台侧却始终 0/4。
# 这与 P0-1「结论的可交付性」是同一条规矩：对渗透结论来说"没有请求响应就不算证据"，
# 对 flag 来说"没有平台接受响应就不算得分"。
SCORED_SCORING_NOTE = (
    "Task mode: **scored run** (CTF / benchmark / capture-the-flag).\n"
    "**A flag counts only after the scoring platform accepts it.** Finding a flag and writing it "
    "into your conclusion does NOT score it — Sharp's findings are *our* record; the platform's "
    "scoreboard is *the* score. So for every flag, in order:\n"
    "1. **Submit it to the scoring platform** exactly as the task text specifies (endpoint, auth "
    "header, JSON body) — the task text contains the flow and credentials to use.\n"
    "2. Treat it as captured only when the platform answers `correct: true`. If it answers "
    "`duplicate`, it was already scored — skip it, do not retry. If it answers incorrect, re-read "
    "the value from the target and resubmit.\n"
    "3. Then report it here as `{\"kind\": \"flag\", \"score\": <the platform's `awarded` points>, "
    "\"title\": \"flag #N\", \"evidence\": \"<flag 原文 + 平台接受响应原文（correct / awarded / "
    "cumulative_score）>\", \"description\": \"where/how it was obtained\"}`.\n"
    "A flag reported **without** the platform's acceptance response in `evidence` does not count as "
    "captured (same rule as the evidence-completeness requirement for findings: a narrative is not "
    "evidence, the raw exchange is). Confirmed security findings keep `kind: \"vuln\"` (the default)."
)


def format_env_baseline(entries: list[dict[str, Any]] | None) -> str:
    """把环境基线渲染成提示词里的一段（空基线给明确的"尚未建立"提示）。"""
    if not entries:
        return (
            "（本项目尚未建立环境基线。一旦你确认了任何作业前提——网络连通性、平台凭据有效性、"
            "目标可达性、工作目录与工具可用性——请把它放进本次结果的 `env_facts` 字段"
            '（形如 [{"key":"network.vpn","value":"ok","note":"..."}]），'
            "控制面会代为持久化；**不要尝试自己调用 Sharp API**（容器内没有它的凭据）。"
            "同一件事只需确认一次。）"
        )
    own = [e for e in entries if not e.get("inherited")]
    inherited = [e for e in entries if e.get("inherited")]

    lines: list[str] = []
    for e in own:
        note = f"　（{e['note']}）" if e.get("note") else ""
        lines.append(f"- {e.get('key')}: {e.get('value')}{note}")
    if inherited:
        # 分开呈现：继承来的前提可信度和本地确认的不一样（可能换了一台机器/端口），
        # 混在一起会让 worker 把"别处确认过"当成"这里已经确认过"。
        if lines:
            lines.append("")
        lines.append("以下前提来自**同目标的其他项目**（同样是这台目标上确认的，可直接复用；"
                     "但若本项目的观测与之矛盾，以本项目观测为准并把它写进 `env_facts` 覆盖）：")
        for e in inherited:
            note = f"　（{e['note']}）" if e.get("note") else ""
            src = e.get("source_project_title") or e.get("source_project_id") or "同目标项目"
            lines.append(f"- {e.get('key')}: {e.get('value')}{note}　[来自 {src}]")
    if not lines:
        return (
            "（本项目尚未建立环境基线。一旦你确认了任何作业前提——网络连通性、平台凭据有效性、"
            "目标可达性、工作目录与工具可用性——请把它放进本次结果的 `env_facts` 字段"
            '（形如 [{"key":"network.vpn","value":"ok","note":"..."}]），'
            "控制面会代为持久化；**不要尝试自己调用 Sharp API**（容器内没有它的凭据）。"
            "同一件事只需确认一次。）"
        )
    return "\n".join(lines)


def scoring_note(task_mode: str | None) -> str:
    """Prompt guidance for findings, depending on the project's task mode."""
    return SCORED_SCORING_NOTE if task_mode == "scored" else PENTEST_SCORING_NOTE


def render_prompt(template: str, replacements: dict[str, str]) -> str:
    text = template
    for key, value in replacements.items():
        text = text.replace("{" + key + "}", value)
    return text


def format_fact_ids(fact_ids: list[str]) -> str:
    return format_json_block(fact_ids)


def format_open_intents(intents: list[dict[str, Any]]) -> str:
    return format_json_block(intents)


def format_hints(hints: list[dict[str, Any]]) -> str:
    return format_json_block(hints)


def format_json_block(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)
