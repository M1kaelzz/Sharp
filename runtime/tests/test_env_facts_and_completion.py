"""环境事实上报 + 完成说明质量（P0-2 修正版 与 #5）。

**为什么基线要改成"上报"而不是"worker 自己调 API"**：真实项目验证发现基线一条都没写。
根因是设计错——worker 容器里没有 Sharp 的凭据、AGENTS.md 也从未教它调 Sharp API，
而按架构 worker 本就不该直连 server（结论一律由 dispatcher 代写）。所以改成：
worker 在结论里输出 `env_facts`，dispatcher 代为持久化。

**为什么要管完成说明**：知识库里出现过 `"1"` 这样的完成说明——它不携带任何信息，
事后无法复核"这个项目凭什么算完成"。这里只做最低限度把关。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sharp.dispatcher.prompting import load_prompt
from sharp.dispatcher.tasks.explore import _extract_env_facts
from sharp.server.models import CompleteRequest


# ── env_facts 提取（#3）──────────────────────────────────────────────────────

def test_extract_list_of_dicts():
    payload = {"data": {"env_facts": [
        {"key": "network.vpn", "value": "ok", "note": "10.0.100.58 → ok"},
        {"key": "platform.token", "value": "valid"},
    ]}}
    out = _extract_env_facts(payload)
    assert [e["key"] for e in out] == ["network.vpn", "platform.token"]
    assert out[0]["note"] == "10.0.100.58 → ok"


def test_extract_plain_mapping():
    assert _extract_env_facts({"data": {"env_facts": {"a": "1"}}}) == [
        {"key": "a", "value": "1", "note": ""}
    ]


def test_extract_top_level_payload():
    assert _extract_env_facts({"env_facts": [{"key": "k", "value": "v"}]})[0]["key"] == "k"


@pytest.mark.parametrize("payload", [{}, {"data": {}}, {"data": {"env_facts": None}},
                                     {"data": {"env_facts": []}}, {"data": {"env_facts": "x"}}])
def test_extract_absent_or_malformed_is_empty(payload):
    assert _extract_env_facts(payload) == []


def test_extract_skips_blank_keys_and_caps_count():
    payload = {"data": {"env_facts": [{"key": "  "}, {"key": "ok", "value": "1"}]}}
    assert [e["key"] for e in _extract_env_facts(payload)] == ["ok"]
    many = {"data": {"env_facts": [{"key": f"k{i}"} for i in range(50)]}}
    assert len(_extract_env_facts(many)) == 20


# ── 完成说明质量（#5）────────────────────────────────────────────────────────

def _complete(description: str) -> CompleteRequest:
    return CompleteRequest(**{"from": ["origin"]}, description=description, worker="w1")


@pytest.mark.parametrize("bad", ["1", "123", "0", "!!!", "  ", "ok"])
def test_completion_needs_meaningful_description(bad):
    with pytest.raises(ValidationError):
        _complete(bad)


@pytest.mark.parametrize("good", [
    "验证完成：目标已达成",
    "覆盖了全部 3 个接口，未发现可确认漏洞",
    "done: all listed vectors were tested",
])
def test_meaningful_completion_accepted(good):
    assert _complete(good).description == good.strip()


# ── 提示词（#4 强化 + #3 的上报约定）──────────────────────────────────────────

def test_reason_prompt_requires_settling_hypotheses():
    text = load_prompt("default", "reason.md")
    assert "REQUIRED when open hypotheses exist" in text
    assert '"hypotheses"' in text, "输出格式里必须出现 hypotheses 字段，否则模型不会主动输出"


@pytest.mark.parametrize("name", ["explore.md", "bootstrap.md"])
def test_output_templates_mention_env_facts(name):
    text = load_prompt("default", name)
    assert "env_facts" in text
    assert "Do NOT try to call the Sharp API yourself" in text
