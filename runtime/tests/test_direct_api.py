"""直连 API 模式（SHARP_DIRECT_API）守卫测试。

背景：claude CLI 自带产品级内容安全过滤，对"攻击意图"的 prompt 静默返回空
输出——渗透工具的 explore 任务构造 payload 时全被吞掉，成为空转（i002 实测
复现）。直连模式用内嵌 node agent-loop（API tool-calling + bash 执行）替代
CLI argv。本文件守卫：

- OFF：旧 argv 逐字节不变（回归红线）
- ON：build_execute/build_conclude 产出 node agent-loop argv（session/phase/prompt）
- 内嵌脚本必须保留核心机制（工具循环 / 会话持久化 / 强制总结收敛 / 计量包）
"""
import types

from sharp.dispatcher.workers.adapters.claudecode import (
    _AGENT_LOOP_SCRIPT,
    ClaudeCodeDriver,
)


def _worker(env: dict | None = None):
    return types.SimpleNamespace(name="w1", env=env or {})


def _driver():
    return ClaudeCodeDriver()


LEGACY_EXECUTE = [
    "claude", "--session-id", "sess", "--dangerously-skip-permissions", "-p", "--", "prompt",
]
LEGACY_CONCLUDE = [
    "claude", "-r", "sess", "--dangerously-skip-permissions", "-p", "--", "prompt",
]


# ── gate off: legacy argv byte-for-byte ─────────────────────────────────────

def test_direct_off_keeps_legacy_execute_argv():
    # SHARP_TOKEN_METERING=0 isolates the direct-API dimension: metering has
    # been default-ON since 2026-10-28 and injects --output-format json.
    w = _worker({"SHARP_TOKEN_METERING": "0"})
    assert _driver().build_execute(w, "prompt", "sess").argv == LEGACY_EXECUTE


def test_direct_off_keeps_legacy_conclude_argv():
    w = _worker({"SHARP_TOKEN_METERING": "0"})
    assert _driver().build_conclude(w, "prompt", "sess") == LEGACY_CONCLUDE


# ── gate on: node agent-loop argv ───────────────────────────────────────────

def test_direct_on_uses_node_agent_loop_execute():
    r = _driver().build_execute(_worker({"SHARP_DIRECT_API": "1"}), "prompt", "sess")
    assert r.argv[:2] == ["node", "-e"]
    assert _AGENT_LOOP_SCRIPT in r.argv
    assert r.session == "sess"
    assert r.argv[r.argv.index("--session") + 1] == "sess"
    assert r.argv[r.argv.index("--phase") + 1] == "execute"
    assert r.argv[r.argv.index("--prompt") + 1] == "prompt"
    assert "claude" not in r.argv


def test_direct_on_uses_node_agent_loop_conclude():
    argv = _driver().build_conclude(_worker({"SHARP_DIRECT_API": "1"}), "prompt", "sess")
    assert argv[:2] == ["node", "-e"]
    assert argv[argv.index("--phase") + 1] == "conclude"
    assert argv[argv.index("--session") + 1] == "sess"


def test_direct_argv_uses_dash_dash_separator():
    # node -e <code> 之后必须用 "--" 分隔：否则 node 会把 --session 当作
    # 自己的选项吞掉（实测：node: bad option: --session，bootstrap 全失败）。
    r = _driver().build_execute(_worker({"SHARP_DIRECT_API": "1"}), "prompt", "sess")
    assert "--" in r.argv
    assert r.argv.index("--") < r.argv.index("--session")
    argv = _driver().build_conclude(_worker({"SHARP_DIRECT_API": "1"}), "prompt", "sess")
    assert "--" in argv
    assert argv.index("--") < argv.index("--session")


def test_direct_on_plus_metering_keeps_script_owned_envelope():
    # 计量开启时不再注入 claude 的 --output-format；envelope 由脚本自行输出。
    r = _driver().build_execute(
        _worker({"SHARP_DIRECT_API": "1", "SHARP_TOKEN_METERING": "1"}), "prompt", "sess"
    )
    assert "--output-format" not in r.argv
    assert "SHARP_TOKEN_METERING" in _AGENT_LOOP_SCRIPT


# ── embedded script core mechanics (mutation guard) ────────────────────────

def test_agent_loop_script_keeps_core_mechanics():
    s = _AGENT_LOOP_SCRIPT
    for needle in (
        "SHARP_AGENT_MAX_ROUNDS",
        "SHARP_AGENT_BASH_TIMEOUT",
        "SHARP_AGENT_MAX_TOKENS",  # 4096 曾被 thinking 烧尽致 reason 空转循环
        "tool_use",
        "tool_result",
        "spawnSync('bash'",
        "/tmp/sharp-sessions",
        "fetch(baseUrl + '/v1/messages'",
        "不要再调用任何工具",  # 轮数用尽后的强制收敛总结
    ):
        assert needle in s, f"agent loop script must keep: {needle!r}"


def test_agent_loop_registers_bash_tool():
    s = _AGENT_LOOP_SCRIPT
    assert "name: 'bash'" in s
    assert "input_schema" in s
    assert "'command'" in s


def test_agent_loop_persists_session_history():
    s = _AGENT_LOOP_SCRIPT
    assert "readFileSync(histFile" in s
    assert "writeFileSync(histFile" in s