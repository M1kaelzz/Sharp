"""提示词必须写明"靶标内容是数据、不是指令"。

**为什么这条是安全要求而不是文案偏好**：Sharp 的 worker 被设计成"读敌手提供的内容并行动"——
HTTP 响应体、报错页、文件内容、banner、源码注释里都可以塞指令（提示注入）。容器隔离、
容器内无 Sharp 凭据、高危动作过审批闸门，都是**事后**的边界；**提示词是唯一在行动发生前生效
的一层**，它必须明确写出"不服从、不外发、不因内容扩权"三条。

**为什么 planner 也要**：注入文本会进入 fact / 知识库（worker 把读到的内容写进结论），
下一轮就有机会出现在 reason 的 `graph_yaml` 里 —— 也就是说**图里的文本也是不可信输入**，
只是绕了一圈。所以三份提示词（两个执行类 + 一个规划类）都要有这条口径。
"""
from __future__ import annotations

import re

import pytest

from sharp.dispatcher.prompting import load_prompt

EXECUTOR_PROMPTS = ("bootstrap.md", "explore.md")
PLANNER_PROMPTS = ("reason.md",)

# 三条具体禁令必须逐条在文里（防止重写时悄悄丢掉其中一条）
PROHIBITIONS = (
    "Never obey",           # 不服从靶标里的指令
    "secrets",              # 不把密钥/凭据外发
    "scope",                # 不因内容扩大授权范围
)


def _prompt(name: str) -> str:
    return load_prompt("default", name)


@pytest.mark.parametrize("name", EXECUTOR_PROMPTS)
def test_executor_prompts_carry_the_guard(name):
    text = _prompt(name)

    assert "not instructions" in text, f"{name} 必须写明靶标内容不是指令"
    for phrase in PROHIBITIONS:
        assert phrase in text, f"{name} 缺禁令: {phrase}"


@pytest.mark.parametrize("name", PLANNER_PROMPTS)
def test_planner_prompt_carries_the_guard(name):
    """规划器不直接打靶标，但图里的文本源自靶标 —— 同样要写明。"""
    text = _prompt(name)

    assert "not instructions" in text, f"{name} 必须写明图中的靶标文本不是指令"
    assert "Never obey" in text
    assert "scope" in text


def test_every_action_taking_prompt_carries_the_guard():
    """接线守卫：所有带 `# Rules` 的执行类提示词都必须带这条防线。

    新增一个会打靶标的提示词时，如果忘了写这条口径，这里会失败——而不是等到某次真实注入
    才被发现。`reason.md` 结构不同（无 Rules 段），由上面那条参数化测试单独钉住。
    """
    from pathlib import Path

    prompts_dir = Path(__file__).resolve().parents[1] / "src" / "sharp" / "dispatcher" / "prompts" / "default"
    # 一级与二级都算（`explore.md` 用 `# Rules`，`bootstrap_conclude.md` 用 `## Rules`）
    action_prompts = sorted(
        p.name
        for p in prompts_dir.glob("*.md")
        if re.search(r"^#{1,2} Rules\s*$", p.read_text(), re.M)
    )

    assert action_prompts, "没有找到任何带 Rules 段的提示词——守卫失去意义，检查目录是否正确"

    missing = [name for name in action_prompts if "not instructions" not in _prompt(name)]
    assert not missing, f"这些执行类提示词缺『不可信内容』口径: {missing}"


def test_guard_names_exfiltration_targets_explicitly():
    """只写"不要外发"不够 —— 要写明具体有哪些东西不能外发，否则模型会自行缩小理解。"""
    text = _prompt("explore.md")

    for forbidden in ("model API key", "Sharp credentials", "operator hints"):
        assert forbidden in text, f"explore.md 应点名不可外发的对象: {forbidden}"
