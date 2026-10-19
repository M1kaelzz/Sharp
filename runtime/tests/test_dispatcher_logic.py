"""Dispatcher 纯逻辑测试（P1 覆盖率补强）。

`dispatcher/scheduler/loop.py`（1420 行调度循环）覆盖率低是因为它大量与 Docker、
子进程、真实容器交互——那属于集成路径（靠 `smoke_container.py` 与真机跑分验证）。
这里补的是**不需要容器就能测、错了却很难查**的两块纯逻辑：

1. `scheduler/worker_select.py` —— 选谁去跑：API key 是否真的配了（含占位符识别）、
   优先级与在跑数量的排序。
2. `contracts.py` —— 解析 AI 输出的契约：畸形/自相矛盾的 payload 必须被明确拒绝，
   否则错误数据会静默进入证据图。

关于 worker type：`WorkerConfig.type` 受 `WorkerType` 枚举约束，配置加载阶段即拒绝
未知类型（本文件固定这一边界，避免以后有人把它放宽成自由字符串）。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sharp.dispatcher.config import WorkerConfig
from sharp.dispatcher.contracts import (
    validate_bootstrap_conclude_payload,
    validate_bootstrap_execute_payload,
    validate_explore_payload,
    validate_reason_payload,
)
from sharp.dispatcher.scheduler.worker_select import choose_worker, has_valid_api_key


# 配置层要求每个 worker 类型的关键 env 齐备且非空（WORKER_ENV_KEYS）。
ENV_BY_TYPE: dict[str, dict[str, str]] = {
    "claudecode": {
        "ANTHROPIC_MODEL": "claude-sonnet",
        "ANTHROPIC_BASE_URL": "https://api.anthropic.example",
        "ANTHROPIC_AUTH_TOKEN": "sk-real-token",
    },
    "codex": {
        "CODEX_MODEL": "o4-mini",
        "CODEX_BASE_URL": "https://api.openai.example",
        "OPENAI_API_KEY": "sk-real-token",
    },
    "pi": {
        "PI_MODEL": "pi-model",
        "PI_BASE_URL": "https://pi.example",
        "PI_API_KEY": "sk-real-token",
        "PI_PROVIDER_API": "https://provider.example",
    },
    "mock": {},
}


def _worker(name: str, type_: str = "claudecode", **overrides) -> WorkerConfig:
    env = dict(ENV_BY_TYPE[type_])
    env.update(overrides.pop("env", {}))
    body = {
        "name": name,
        "type": type_,
        "task_types": ["explore"],
        "max_running": 1,
        "priority": 0,
        "env": env,
    }
    body.update(overrides)
    return WorkerConfig(**body)


# ── WorkerConfig 校验边界 ─────────────────────────────────────────────────────

def test_unknown_worker_type_is_rejected_at_config_load():
    """未知 type 必须在配置加载阶段失败（fail closed），不能溜进调度池。"""
    with pytest.raises(ValidationError):
        WorkerConfig(
            name="w1", type="claud",  # 拼写错误
            task_types=["explore"], max_running=1, priority=0,
            env={"ANTHROPIC_AUTH_TOKEN": "sk-x"},
        )


def test_mock_worker_type_is_allowed():
    assert _worker("m", type_="mock").type == "mock"


def test_empty_or_duplicate_task_types_rejected():
    with pytest.raises(ValidationError):
        _worker("w1", task_types=[])
    with pytest.raises(ValidationError):
        _worker("w1", task_types=["explore", "explore"])


def test_numeric_bounds_enforced():
    with pytest.raises(ValidationError):
        _worker("w1", max_running=0)
    with pytest.raises(ValidationError):
        _worker("w1", priority=-1)


# ── API key 有效性（含占位符）────────────────────────────────────────────────

def test_mock_worker_needs_no_api_key():
    assert has_valid_api_key(_worker("m", type_="mock")) is True


def test_missing_env_keys_rejected_at_config_load():
    """env 缺项（含空串）在配置加载阶段就失败，不会留到派发时才炸。"""
    with pytest.raises(ValidationError, match="missing env keys"):
        WorkerConfig(name="c", type="claudecode", task_types=["explore"],
                     max_running=1, priority=0, env={})
    with pytest.raises(ValidationError, match="missing env keys"):
        WorkerConfig(name="c", type="claudecode", task_types=["explore"], max_running=1, priority=0,
                     env={"ANTHROPIC_MODEL": "m", "ANTHROPIC_BASE_URL": "u", "ANTHROPIC_AUTH_TOKEN": ""})


def test_valid_token_passes_key_check():
    assert has_valid_api_key(_worker("c")) is True


@pytest.mark.parametrize(
    "placeholder",
    ["placeholder", "PLACEHOLDER", "your-api-key-here", "none"],
)
def test_placeholder_keys_are_not_valid(placeholder):
    """占位符不算配好 —— 否则调度器会把任务派给一个必然失败的 worker。"""
    assert has_valid_api_key(_worker("c", env={"ANTHROPIC_AUTH_TOKEN": placeholder})) is False


def test_empty_key_rejected_by_config():
    """空串 key 连配置层都过不去（比 has_valid_api_key 更早失败）。"""
    with pytest.raises(ValidationError, match="missing env keys"):
        _worker("c", env={"ANTHROPIC_AUTH_TOKEN": ""})


def test_whitespace_key_rejected_by_key_check():
    """纯空白能过配置层（非空字符串），但必须被 key 有效性检查拦下。"""
    assert has_valid_api_key(_worker("c", env={"ANTHROPIC_AUTH_TOKEN": "   "})) is False


def test_codex_key_check():
    assert has_valid_api_key(_worker("x", type_="codex")) is True
    assert has_valid_api_key(_worker("x", type_="codex", env={"OPENAI_API_KEY": "placeholder"})) is False


# ── 选择顺序 ──────────────────────────────────────────────────────────────────

def test_choose_worker_filters_invalid():
    good = _worker("good")
    bad = _worker("bad", env={"ANTHROPIC_AUTH_TOKEN": "placeholder"})
    assert [w.name for w in choose_worker([bad, good], {})] == ["good"]


def test_choose_worker_prefers_lower_priority_value():
    low = _worker("low", priority=0)
    high = _worker("high", priority=5)
    assert [w.name for w in choose_worker([high, low], {})] == ["low", "high"]


def test_choose_worker_prefers_less_loaded_worker_at_same_priority():
    a = _worker("a")
    b = _worker("b")
    assert [w.name for w in choose_worker([a, b], {"a": 3, "b": 0})] == ["b", "a"]


def test_choose_worker_returns_all_valid_candidates():
    ws = [_worker(n) for n in ("a", "b", "c")]
    assert len(choose_worker(ws, {})) == 3


def test_choose_worker_empty_when_nothing_valid():
    ph = {"ANTHROPIC_AUTH_TOKEN": "placeholder"}
    assert choose_worker([_worker("a", env=ph), _worker("b", env=ph)], {}) == []


# ── 规划器输出契约 ────────────────────────────────────────────────────────────

def test_reason_rejects_when_model_says_not_accepted():
    assert validate_reason_payload({"accepted": False}, open_intents_empty=False, max_intents=3) == ("rejected", None)


def test_reason_accepts_plural_and_singular_intent_shape():
    plural = {"accepted": True, "data": {"intents": [{"from": ["origin"], "description": "d"}]}}
    kind, data = validate_reason_payload(plural, open_intents_empty=True, max_intents=5)
    assert (kind, len(data)) == ("intents", 1)

    singular = {"accepted": True, "data": {"intent": {"from": ["origin"], "description": "d"}}}
    kind, data = validate_reason_payload(singular, open_intents_empty=True, max_intents=5)
    assert (kind, len(data)) == ("intents", 1)


def test_reason_rejects_complete_and_intents_together():
    payload = {"accepted": True, "data": {
        "complete": {"from": ["origin"], "description": "done"},
        "intents": [{"from": ["origin"], "description": "d"}],
    }}
    with pytest.raises(ValueError, match="cannot coexist"):
        validate_reason_payload(payload, open_intents_empty=True, max_intents=3)


def test_reason_rejects_incomplete_complete_payload():
    with pytest.raises(ValueError, match="invalid complete payload"):
        validate_reason_payload({"accepted": True, "data": {"complete": {"description": "x"}}},
                                open_intents_empty=True, max_intents=3)


def test_reason_rejects_malformed_intent_entries():
    payload = {"accepted": True, "data": {"intents": [{"description": "missing from"}]}}
    with pytest.raises(ValueError, match="invalid intent at index 0"):
        validate_reason_payload(payload, open_intents_empty=True, max_intents=3)


def test_reason_rejects_non_array_intents():
    with pytest.raises(ValueError, match="must be an array"):
        validate_reason_payload({"accepted": True, "data": {"intents": {"from": []}}},
                                open_intents_empty=True, max_intents=3)


def test_reason_truncates_to_max_intents():
    intents = [{"from": ["origin"], "description": f"d{i}"} for i in range(5)]
    kind, data = validate_reason_payload({"accepted": True, "data": {"intents": intents}},
                                        open_intents_empty=True, max_intents=2)
    assert kind == "intents"
    assert len(data) == 2


def test_reason_empty_intents_requires_open_work():
    with pytest.raises(ValueError, match="must not be empty"):
        validate_reason_payload({"accepted": True, "data": {"intents": []}},
                                open_intents_empty=True, max_intents=3)
    assert validate_reason_payload({"accepted": True, "data": {"intents": []}},
                                   open_intents_empty=False, max_intents=3) == ("noop", None)


def test_reason_without_intents_or_complete_needs_open_work():
    with pytest.raises(ValueError, match="intents is required"):
        validate_reason_payload({"accepted": True, "data": {}}, open_intents_empty=True, max_intents=3)
    assert validate_reason_payload({"accepted": True, "data": {}},
                                   open_intents_empty=False, max_intents=3) == ("noop", None)


def test_reason_bare_payload_without_accepted_flag():
    """兼容模型直接输出意图主体（不带 accepted 包装）。"""
    kind, data = validate_reason_payload(
        {"intents": [{"from": ["origin"], "description": "d"}]},
        open_intents_empty=True, max_intents=3,
    )
    assert (kind, len(data)) == ("intents", 1)


def test_reason_garbage_payload_raises():
    with pytest.raises(ValueError, match="accepted must be true or false"):
        validate_reason_payload({"whatever": 1}, open_intents_empty=False, max_intents=3)


# ── 探索输出契约 ──────────────────────────────────────────────────────────────

def test_explore_requires_description():
    with pytest.raises(ValueError, match="description is required"):
        validate_explore_payload({"accepted": True, "data": {"findings": []}})


def test_explore_accepts_valid_payload_without_findings():
    kind, description, findings = validate_explore_payload({"accepted": True, "data": {"description": "做完了"}})
    assert kind == "fact" and description.strip() == "做完了" and findings is None


def test_explore_rejects_unknown_severity():
    payload = {"accepted": True, "data": {
        "description": "d",
        "findings": [{"title": "t", "severity": "catastrophic"}],
    }}
    with pytest.raises(ValueError, match="severity"):
        validate_explore_payload(payload)


def test_explore_rejects_finding_without_title():
    payload = {"accepted": True, "data": {"description": "d", "findings": [{"severity": "high"}]}}
    with pytest.raises(ValueError, match="title is required"):
        validate_explore_payload(payload)


def test_explore_keeps_flag_kind_and_score():
    """评分类任务的旗帜（kind=flag + score）必须原样透传。"""
    payload = {"accepted": True, "data": {
        "description": "拿到 flag",
        "findings": [{"title": "a-05 旗帜", "kind": "flag", "score": 100}],
    }}
    kind, _description, findings = validate_explore_payload(payload)
    assert kind == "fact"
    # 这是真实回归点：契约层重建 finding 字段时曾丢掉 kind/score，
    # 导致 explore 路径上每个旗帜都被登记成 vuln / 0 分
    assert findings[0]["kind"] == "flag"
    assert findings[0]["score"] == 100


def test_bootstrap_execute_requires_fact_description():
    with pytest.raises(ValueError, match="fact.description is required"):
        validate_bootstrap_execute_payload({"accepted": True, "data": {"fact": {"description": "  "}}})


def test_bootstrap_conclude_payload_shapes():
    kind, _value = validate_bootstrap_conclude_payload({"accepted": False})
    assert kind == "rejected"
