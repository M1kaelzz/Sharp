"""直连 API 模式（SHARP_DIRECT_API）守卫测试 —— codex 适配器。

与 claudecode 同理：codex CLI 自带内容过滤会静默吞掉攻击意图的 prompt
（渗透 explore 的 payload 构造全部空转）。直连模式用内嵌 node agent-loop
（chat completions + function calling + bash）替代 CLI。本文件守卫：

- OFF：旧 argv 逐字节不变（回归红线）
- ON：node agent-loop argv + DriverResult.session 由适配器生成（uuid），
  经 extract_session 短路透传给 conclude（旧 CLI 模式仍从 stderr 提取）
- 内嵌脚本核心机制（tool_calls / role:tool 回填 / 会话持久化 / 强制收敛 /
  轮数与 bash 超时参数化）
- metering envelope 的解包与 usage 提取（codex 此前完全没有计量）
"""
import json
import re
import types
import uuid

from sharp.dispatcher.workers.adapters.codex import (
    _OPENAI_AGENT_LOOP_SCRIPT,
    CodexDriver,
)


def _worker(env: dict | None = None):
    base = {
        "CODEX_MODEL": "test-model",
        "CODEX_BASE_URL": "https://api.test/v1",
        "OPENAI_API_KEY": "k",
    }
    base.update(env or {})
    return types.SimpleNamespace(name="w1", env=base)


def _driver():
    return CodexDriver()


LEGACY_EXECUTE_PREFIX = ["codex", "exec", "--dangerously-bypass-approvals-and-sandbox"]
LEGACY_CONCLUDE_PREFIX = ["codex", "exec", "resume", "sess"]


# ── gate off: legacy argv byte-for-byte ─────────────────────────────────────

def test_direct_off_keeps_legacy_execute_argv():
    r = _driver().build_execute(_worker(), "prompt", None)
    assert r.argv[:3] == LEGACY_EXECUTE_PREFIX
    assert r.argv[-1] == "prompt"
    assert r.session is None
    assert "node" not in r.argv


def test_direct_off_keeps_legacy_conclude_argv():
    argv = _driver().build_conclude(_worker(), "prompt", "sess")
    assert argv[:4] == LEGACY_CONCLUDE_PREFIX
    assert argv[-1] == "prompt"


# ── gate on: node agent-loop argv + seeded session ─────────────────────────

def test_direct_on_uses_node_agent_loop_execute():
    r = _driver().build_execute(_worker({"SHARP_DIRECT_API": "1"}), "prompt", None)
    assert r.argv[:2] == ["node", "-e"]
    assert _OPENAI_AGENT_LOOP_SCRIPT in r.argv
    # session 由适配器生成（uuid），经 DriverResult 流转到 conclude
    assert r.session is not None
    uuid.UUID(r.session)  # 必须是合法 uuid
    assert r.argv[r.argv.index("--session") + 1] == r.session
    assert r.argv[r.argv.index("--phase") + 1] == "execute"
    assert r.argv[r.argv.index("--prompt") + 1] == "prompt"
    assert "codex" not in r.argv


def test_direct_on_uses_node_agent_loop_conclude():
    sid = str(uuid.uuid4())
    argv = _driver().build_conclude(_worker({"SHARP_DIRECT_API": "1"}), "prompt", sid)
    assert argv[:2] == ["node", "-e"]
    assert argv[argv.index("--session") + 1] == sid
    assert argv[argv.index("--phase") + 1] == "conclude"


def test_direct_argv_uses_dash_dash_separator():
    # node -e <code> 之后必须用 "--" 分隔（否则 node 吞掉 --session）。
    r = _driver().build_execute(_worker({"SHARP_DIRECT_API": "1"}), "prompt", None)
    assert "--" in r.argv
    assert r.argv.index("--") < r.argv.index("--session")
    argv = _driver().build_conclude(_worker({"SHARP_DIRECT_API": "1"}), "prompt", "s")
    assert "--" in argv
    assert argv.index("--") < argv.index("--session")


def test_direct_session_flows_through_extract_session():
    # RegexSessionDriver.extract_session 对非空 session 短路透传：
    # direct 模式生成的 uuid 必须原样到达 conclude。
    d = _driver()
    r = d.build_execute(_worker({"SHARP_DIRECT_API": "1"}), "prompt", None)
    assert d.extract_session(r.session, "", "whatever-stderr") == r.session


# ── embedded script core mechanics (mutation guard) ────────────────────────

def test_agent_loop_script_keeps_core_mechanics():
    s = _OPENAI_AGENT_LOOP_SCRIPT
    for needle in (
        "/chat/completions",
        "tool_calls",
        "role: 'tool'",
        "tool_call_id",
        "spawnSync('bash'",
        "/tmp/sharp-sessions",
        "SHARP_AGENT_MAX_ROUNDS",
        "SHARP_AGENT_BASH_TIMEOUT",
        "SHARP_AGENT_MAX_TOKENS",
        "不要再调用任何工具",  # 轮数用尽后的强制收敛总结
    ):
        assert needle in s, f"codex agent loop must keep: {needle!r}"


def test_agent_loop_registers_bash_function_tool():
    s = _OPENAI_AGENT_LOOP_SCRIPT
    assert "type: 'function'" in s
    assert "name: 'bash'" in s
    assert "'command'" in s


# ── metering envelope (P1-3, previously absent for codex) ─────────────────

def test_extract_response_text_unwraps_envelope():
    d = _driver()
    envelope = json.dumps({"result": "最终结论", "usage": {}, "total_cost_usd": 0, "model": "m"})
    assert d.extract_response_text(envelope, "") == "最终结论"
    # 纯文本（legacy CLI 输出）原样返回
    assert d.extract_response_text("plain output", "") == "plain output"


def test_extract_usage_parses_envelope():
    d = _driver()
    envelope = json.dumps({
        "result": "x",
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        "total_cost_usd": 0.01,
        "model": "deepseek-chat",
    })
    out = d.extract_usage(envelope)
    assert out == {"usage": {"prompt_tokens": 10, "completion_tokens": 5},
                   "total_cost_usd": 0.01, "model": "deepseek-chat"}


def test_extract_usage_none_for_plain_text():
    # legacy CLI 模式无计量
    assert _driver().extract_usage("plain cli stdout") is None
    assert _driver().extract_usage("") is None


def test_session_seed_is_unique_per_execute():
    # 每次执行生成独立 uuid，避免两个任务共用同一会话历史
    d = _driver()
    w = _worker({"SHARP_DIRECT_API": "1"})
    s1 = d.build_execute(w, "p", None).session
    s2 = d.build_execute(w, "p", None).session
    assert s1 != s2
    assert re.fullmatch(r"[0-9a-f-]{36}", s1)