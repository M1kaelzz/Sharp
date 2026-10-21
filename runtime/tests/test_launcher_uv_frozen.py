"""Launcher uv-freeze contract (batch: mirror-safe frozen, 2026-10-21).

run.sh 向整个进程树注入清华镜像（``UV_DEFAULT_INDEX``），而 uv.lock 记录的包源
是 pypi.org。非 frozen 的 uv 命令会在当前索引下重新解析：

- ``uv sync --locked`` → 直接硬失败 ``lockfile needs to be updated``（首次真实
  ``./run.sh`` 启动即触发，对照实验复现：无镜像 env 通过 / 注入镜像失败）；
- ``uv run`` → **静默重写 uv.lock**（实测 369 行源 diff 全部改成镜像 URL，
  仓库与 CI 同遭污染）。

因此启动器（``./sharp``）的全部 uv 调用必须 ``--frozen``：

- ``sync_project()`` 用 ``--frozen``（且全文不出现 ``--locked``）；
- 所有 ``uv run`` 只能走 ``uv_run_cli()`` 统一出口（原文中 ``uv_cmd("run"`` 仅允许
  出现在该帮助函数体内）——防止将来新增调用点忘了带 --frozen。

锁的新鲜度校验由 CI 的 ``uv sync --locked``（无镜像环境）承担，launcher 不做
（做了就会在镜像下崩）。突变门：把 --frozen 换回 --locked 或绕过帮助函数直接
调 ``uv_cmd("run"...)``，本文件必须失败。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SHARP = ROOT / "sharp"

pytestmark = pytest.mark.skipif(
    not SHARP.exists(), reason="sharp launcher not available"
)

SRC = SHARP.read_text(encoding="utf-8")


def _uv_run_bodies(src: str) -> list[str]:
    """提取每个 uv_run_cli/uv_cmd("run"... 调用的整行（含续行折叠前的行）。"""
    return [line for line in src.splitlines() if 'uv_cmd("run"' in line]


def test_sync_project_uses_frozen_not_locked():
    """sync 必须走 --frozen；--locked 在镜像 env 下必炸（启动器全文禁用）。"""
    assert '"--frozen"' in SRC, "launcher must use --frozen (mirror-safe)"
    assert '"--locked"' not in SRC, (
        "--locked 在 run.sh 注入的 UV_DEFAULT_INDEX 镜像下会因重解析与锁源"
        "（pypi.org）不一致而硬失败；新鲜度校验属于 CI（无镜像环境），不属于启动器"
    )


def test_all_uv_run_go_through_frozen_helper():
    """uv run 只能走 uv_run_cli()（--frozen 统一出口），不得旁路。

    旁路的 uv run 在镜像 env 下会静默重写 uv.lock（369 行实测）。
    """
    bodies = _uv_run_bodies(SRC)
    assert bodies, "uv_run_cli helper must exist and funnel every uv run"
    assert len(bodies) == 1, (
        "uv_cmd(\"run\" 只允许出现在 uv_run_cli 帮助函数体内；"
        f"发现 {len(bodies)} 处：{bodies}"
    )
    helper = re.search(r"def uv_run_cli\(.*?(?=\ndef |\Z)", SRC, re.S)
    assert helper and '"--frozen"' in helper.group(0), (
        "uv_run_cli 必须把 --frozen 放进 uv run 参数"
    )
    # 其余启动/透传路径全部经由 uv_run_cli(...)
    assert "uv_run_cli(" in SRC.replace("def uv_run_cli(", "")


def test_mutant_frozen_to_locked_trips_guard():
    """突变门：把 --frozen 全部换回 --locked → 守护必须失败。"""
    mutated = SRC.replace('"--frozen"', '"--locked"')
    assert mutated != SRC, "mutation did not apply"
    with pytest.raises(AssertionError):
        assert '"--locked"' not in mutated, (
            "--locked 在 run.sh 注入的 UV_DEFAULT_INDEX 镜像下会因重解析与锁源"
            "（pypi.org）不一致而硬失败；新鲜度校验属于 CI（无镜像环境），不属于启动器"
        )


def test_mutant_bypassing_helper_trips_guard():
    """突变门：绕过帮助函数直接 uv_cmd("run"...) → 守护必须失败。"""
    mutated = SRC.replace(
        'return uv_cmd("run", "--frozen", *project_args(), "sharp", *args)',
        'return uv_cmd("run", "--locked", *project_args(), "sharp", *args)',
    )
    mutated = mutated.replace('uv_run_cli("window",', 'uv_cmd("run", *project_args(), "sharp", "window",')
    assert mutated != SRC, "mutation did not apply"
    bodies = _uv_run_bodies(mutated)
    with pytest.raises(AssertionError):
        assert len(bodies) == 1, f"uv_cmd(\"run\" 旁路了帮助函数：{bodies}"