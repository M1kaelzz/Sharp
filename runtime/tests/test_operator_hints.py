"""操作员人工线索（hints）必须真正被 agent 读到。

本次基准测试暴露的缺陷：`POST /projects/{pid}/hints` 允许在项目运行中补线索，
但提示词侧只有 bootstrap 渲染 hints（`_bootstrap_prompt_replacements`），reason
只记了一条 `len(project.hints)` 的日志——**运行中补的线索没有任何 agent 会读**，
操作员的中途纠偏（"别打这条死胡同，去打内网"）全部落空，UI 上却看得见。

这里的守卫分两层：
  1. 块本身渲染正确（含 id / content / creator，空态有明示）。
  2. reason 模板的每个占位符都真的在 `run_reason_task` 里喂了值——render_prompt
     是裸 `str.replace`，漏接线不报错、只是把 `{xxx}` 字面留在提示词里静默降级。
"""
from __future__ import annotations

import inspect
import re

from sharp.dispatcher.prompting import load_prompt
from sharp.dispatcher.tasks.reason import _hints_block, run_reason_task
from sharp.server.models import Fact, Hint, ProjectDetail, ProjectMeta

TS = "2026-01-01T00:00:00Z"


def _project(hints: list[Hint]) -> ProjectDetail:
    return ProjectDetail(
        project=ProjectMeta(id="p1", title="t", status="active", created_at=TS),
        facts=[Fact(id="origin", description="o"), Fact(id="goal", description="g")],
        intents=[],
        hints=hints,
    )


def _hint(hid: str, content: str, creator: str = "人工操作员") -> Hint:
    return Hint(id=hid, content=content, creator=creator, created_at=TS)


def test_hints_block_marks_the_empty_state():
    assert "No operator hints" in _hints_block(_project([]))


def test_hints_block_renders_operator_hint_with_creator():
    block = _hints_block(_project([_hint("h002", "b-01 别打 LFI，去打内网 172.20.0.3")]))

    assert "h002" in block
    assert "b-01 别打 LFI，去打内网 172.20.0.3" in block
    assert "人工操作员" in block, "线索的来源要带出去，worker 才知道这是人给的"


def test_hints_block_keeps_every_hint():
    block = _hints_block(_project([_hint("h001", "线索一"), _hint("h002", "线索二")]))

    assert "线索一" in block and "线索二" in block


def test_reason_prompt_has_hints_placeholder():
    text = load_prompt("default", "reason.md")

    assert "{hints}" in text, "reason 提示词必须给人工线索留位置"
    assert "hint wins" in text, "线索与图中已有结论冲突时的效力等级必须写明"


def test_every_reason_placeholder_is_wired_in_run_reason_task():
    """模板占位符 ⊆ 源码喂值键；否则提示词会静默留下 `{xxx}` 字面量。"""
    template = load_prompt("default", "reason.md")
    placeholders = set(re.findall(r"\{([a-z_]+)\}", template))

    assert "hints" in placeholders
    source = inspect.getsource(run_reason_task)
    missing = sorted(name for name in placeholders if f'"{name}":' not in source)

    assert not missing, f"reason 模板占位符没有喂值（会静默留下字面量）: {missing}"
    # 键存在还不够：hints 的值必须真的由项目数据渲染（而不是空串/写死）
    assert "_hints_block(project)" in source, "hints 必须来自项目的 hints"
