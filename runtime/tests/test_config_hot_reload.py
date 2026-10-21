"""Dispatcher config hot-reload (P2, 2026-10-21, runtime.config_hot_reload).

背景：配置（dispatch.yaml + secrets.env）此前只在 ``DispatcherLoop.__init__``
加载一次并全程冻结——这是设置页"保存后需重启 Dispatcher 才生效"的唯一原因。
开启本开关后，调度循环每 tick（~3s）指纹探测 (mtime_ns, size)，变化即原地重载
``self.config``：MCP servers / worker 选择 / secrets / interval 对**新任务**
立即生效（配置按任务读取，如 tasks/common.py 的 mcp_servers）。

刻意语义（逐条有测试锁定）：
- server 覆盖保留（启动器 pin 的 URL 不被 yaml 抢回）；
- secrets 重载走 override=True：**启动时**进程真实 env 胜（CI/测试惯例），**重载时**
  文件胜——设置页就是那个"正在改文件的人"；
- 重载失败（半写态 yaml / 校验错误）只告警并保留旧配置，绝不打断调度循环，
  下一 tick 自动重试；
- init 期定型的字段（线程池大小 / 容器配置 / HA 开关）不热加载。

默认 OFF = 与历史行为逐字节等价（test_off_never_reloads 用对象身份锁定）。
"""

from __future__ import annotations

import inspect
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from sharp.dispatcher.config import DispatchConfig, RuntimeConfig
from sharp.dispatcher.scheduler.loop import DispatcherLoop

WORKER = textwrap.dedent(
    """
    workers:
      - name: w1
        type: claudecode
        task_types: ["reason"]
        max_running: 1
        priority: 0
        env: {ANTHROPIC_AUTH_TOKEN: "tok", ANTHROPIC_MODEL: "m", ANTHROPIC_BASE_URL: "http://x"}
    """
)


def _yaml(*, hot: bool = True, mcp: str = "mcp_servers: []") -> str:
    flag = f"config_hot_reload: {'true' if hot else 'false'}"
    return (
        textwrap.dedent(
            f"""
            server: "http://yaml-side:8000"
            runtime:
              interval: 1
              prompt_group: default
              healthcheck_timeout: 60
              max_workers: 2
              max_running_projects: 3
              max_project_workers: 2
              {flag}
            tasks:
              bootstrap: {{timeout: 400, conclude_timeout: 180, conclude_retry: false}}
              reason: {{timeout: 300, max_intents: 4}}
              explore: {{timeout: 300, conclude_timeout: 180, conclude_retry: false}}
            container: {{image: "sharp-worker:latest", network_mode: bridge, completed_action: stop}}
            """
        )
        + WORKER
        + mcp
        + "\n"
    )


MCP_ON = textwrap.dedent(
    """
    mcp_servers:
      - name: httpx
        transport: http
        url: "http://127.0.0.1:9/mcp"
        enabled: true
        max_tools: 50
    """
)


def _loop_for(path: Path, *, server: str = "http://launcher-side:1") -> DispatcherLoop:
    cfg = DispatchConfig.load(path).model_copy(update={"server": server})
    loop = DispatcherLoop.__new__(DispatcherLoop)
    loop.config_path = path
    loop.config = cfg
    loop._config_watch = None
    return loop


def _bump_mtime(path: Path, tick: int) -> None:
    """Force a fingerprint change even if mtime granularity were coarse."""
    import os

    when = 1_700_000_000 + tick * 100
    os.utime(path, (when, when))


# ── 1. flag default ─────────────────────────────────────────────────────────

def test_flag_defaults_off():
    assert RuntimeConfig.model_fields["config_hot_reload"].default is False


# ── 2. OFF = legacy frozen config (differential pin) ────────────────────────

def test_off_never_reloads(tmp_path):
    yaml = tmp_path / "dispatch.yaml"
    yaml.write_text(_yaml(hot=False))
    loop = _loop_for(yaml)
    before = loop.config

    loop._maybe_reload_config()  # first tick: warms the watch
    yaml.write_text(_yaml(hot=False, mcp=MCP_ON))
    _bump_mtime(yaml, 1)
    loop._maybe_reload_config()

    assert loop.config is before, "flag off: config object must stay frozen"
    assert loop.config.mcp_servers == []


# ── 3. ON: reload swaps config; server override survives ───────────────────

def test_on_reloads_mcp_and_keeps_server_override(tmp_path):
    yaml = tmp_path / "dispatch.yaml"
    yaml.write_text(_yaml(hot=True))
    loop = _loop_for(yaml)  # server pinned to launcher-side

    loop._maybe_reload_config()  # warm
    assert loop.config.mcp_servers == []

    yaml.write_text(_yaml(hot=True, mcp=MCP_ON))
    _bump_mtime(yaml, 2)
    loop._maybe_reload_config()

    names = [s.name for s in loop.config.mcp_servers]
    assert names == ["httpx"], "reload must swap in the new MCP server"
    assert loop.config.server == "http://launcher-side:1", (
        "the launcher-pinned server override must survive hot reload"
    )


# ── 4. ON: secrets.env change wins over process env (override semantics) ───

def test_secrets_file_overrides_env_on_reload(tmp_path, monkeypatch):
    import sharp.secrets as secrets_mod
    import sharp.dispatcher.scheduler.loop as loop_mod

    yaml = tmp_path / "dispatch.yaml"
    yaml.write_text(_yaml(hot=True))
    secrets = tmp_path / "secrets.env"
    secrets.write_text("HOT_RELOAD_TEST_KEY=from-file-v1\n")
    # read_secrets_file() only KEEPS whitelisted SECRET_KEYS — extend the
    # whitelist with our probe key instead of abusing a real token key.
    # Both the loop's imported names AND the source module must point at the
    # tmp file — DispatchConfig.load() resolves secrets internally too.
    monkeypatch.setattr(
        secrets_mod, "SECRET_KEYS", (*secrets_mod.SECRET_KEYS, "HOT_RELOAD_TEST_KEY")
    )
    monkeypatch.setattr(secrets_mod, "resolve_secrets_file", lambda path=None: secrets)
    monkeypatch.setattr(loop_mod, "resolve_secrets_file", lambda: secrets)
    monkeypatch.setenv("HOT_RELOAD_TEST_KEY", "from-shell-boot")

    loop = _loop_for(yaml)
    loop._maybe_reload_config()  # warm

    secrets.write_text("HOT_RELOAD_TEST_KEY=from-file-v2\n")
    _bump_mtime(secrets, 3)
    loop._maybe_reload_config()

    import os

    assert os.environ["HOT_RELOAD_TEST_KEY"] == "from-file-v2", (
        "on reload the file is authoritative (boot keeps env precedence)"
    )


# ── 5. ON: broken yaml keeps previous config, loop never dies ───────────────

def test_malformed_yaml_keeps_previous_then_recovers(tmp_path):
    yaml = tmp_path / "dispatch.yaml"
    yaml.write_text(_yaml(hot=True))
    loop = _loop_for(yaml)
    loop._maybe_reload_config()  # warm

    yaml.write_text("{{ this is : not : valid yaml ]]")
    _bump_mtime(yaml, 4)
    loop._maybe_reload_config()  # must not raise
    assert loop.config.mcp_servers == [], "previous config must be kept"

    yaml.write_text(_yaml(hot=True, mcp=MCP_ON))
    _bump_mtime(yaml, 5)
    loop._maybe_reload_config()
    assert [s.name for s in loop.config.mcp_servers] == ["httpx"], (
        "next tick retries the reload once the file is whole again"
    )


# ── 6. wiring guard: the tick actually calls the reload ─────────────────────

def test_run_tick_invokes_reload():
    src = inspect.getsource(DispatcherLoop.run)
    assert "self._maybe_reload_config()" in src, (
        "run() must call _maybe_reload_config() every tick (hot-reload wiring)"
    )
    assert src.index("self._maybe_reload_config()") < src.index("self._settings_checked"), (
        "reload runs at the top of each tick, before any dispatch decision"
    )