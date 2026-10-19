"""Provider 故障分类与冷却测试（P2 provider 降级）。

重点是**负例**：渗透输出里遍地 `401`/`403`/`429`，如果分类器按裸状态码匹配，
健康 worker 会被误冷却——那比"不降级"更糟。本文件用真实渗透输出片段固定这一点。
"""

from __future__ import annotations

import pytest

from sharp.dispatcher.scheduler.provider_health import (
    PROVIDER_COOLDOWN_SECONDS,
    classify_provider_error,
    cooldown_for,
    provider_error_status,
    provider_kind_from_status,
    provider_status,
)

QUOTA_STDERR = "Error: 402 Insufficient Balance. Please top up your account."
CODEX_QUOTA = "stream error: insufficient_quota: You exceeded your current quota, please check your plan"
CREDIT_LOW = "API Error: 400 Credit balance is too low"
AUTH_STDERR = "Error: 401 Unauthorized: invalid api key provided"
AUTH_STDERR_2 = "authentication_error: please check your api key"
RATE_STDERR = "Error: rate limit exceeded for api.openai.com, please retry later"
OVERLOADED = "overloaded_error: server is overloaded"


# ── 正例：真实 provider 故障应被识别 ─────────────────────────────────────────

@pytest.mark.parametrize("text", [QUOTA_STDERR, CODEX_QUOTA, CREDIT_LOW])
def test_quota_failures_recognized(text):
    assert classify_provider_error(text) == "quota"


@pytest.mark.parametrize("text", [AUTH_STDERR, AUTH_STDERR_2])
def test_auth_failures_recognized(text):
    assert classify_provider_error(text) == "auth"


@pytest.mark.parametrize("text", [RATE_STDERR, OVERLOADED])
def test_rate_failures_recognized(text):
    assert classify_provider_error(text) == "rate"


def test_none_and_empty_are_safe():
    assert classify_provider_error(None) is None
    assert classify_provider_error("") is None


# ── 负例：渗透输出不能触发冷却 ───────────────────────────────────────────────

@pytest.mark.parametrize(
    "pentest_output",
    [
        "GET /admin -> 401 Unauthorized (login_required)",
        "POST /api/user -> 403 Forbidden, 越权被拒",
        "HTTP/1.1 429 Too Many Requests from target WAF",
        "发现 402 Payment Required 页面，疑似业务逻辑漏洞",
        "response codes observed: 200,301,302,401,403,500",
        "SQL injection test: ' OR 1=1 -> 500 Internal Server Error",
    ],
)
def test_target_side_status_codes_do_not_trigger(pentest_output):
    """目标站点返回 401/402/429 是**被测对象**的行为，不是 provider 故障。"""
    assert classify_provider_error(pentest_output) is None


def test_plain_error_is_not_provider_failure():
    assert classify_provider_error("connection reset by peer") is None
    assert classify_provider_error("timeout after 300s") is None


def test_weak_marker_requires_provider_context():
    """`too many requests` 这类短语必须伴随 provider 上下文才算 provider 故障。"""
    assert classify_provider_error("429 Too Many Requests (api.openai.com)") == "rate"
    assert classify_provider_error("HTTP/1.1 429 Too Many Requests from target WAF") is None


def test_target_401_does_not_trigger():
    """目标返回 401 Unauthorized 是最常见的"预期结果"，不能冷却自己的 worker。"""
    assert classify_provider_error("GET /api/admin -> 401 Unauthorized") is None


# ── status 往返与冷却时长 ────────────────────────────────────────────────────

def test_status_roundtrip():
    assert provider_status("quota") == "provider_quota"
    assert provider_kind_from_status("provider_quota") == "quota"
    assert provider_error_status(QUOTA_STDERR) == "provider_quota"
    assert provider_error_status("connection reset") is None


def test_non_provider_status_is_ignored():
    for status in ("failed", "success", "rejected", "unhealthy", "cancelled", None, ""):
        assert provider_kind_from_status(status) is None


def test_cooldowns_are_long_enough_to_switch_provider():
    """provider 故障的冷却必须远大于 5 秒级别的启动健康检查退避。"""
    assert cooldown_for("quota") >= 600
    assert cooldown_for("auth") >= 600
    assert cooldown_for("rate") >= 60
    assert cooldown_for("unknown-kind") == 600.0
    assert all(v >= 60 for v in PROVIDER_COOLDOWN_SECONDS.values())


# ── 调度器决策（provider 降级是否真的发生）────────────────────────────────────

def test_suspension_decision_for_provider_outcomes():
    from sharp.dispatcher.scheduler.provider_health import suspension_for_outcome

    assert suspension_for_outcome("provider_quota") == ("quota", 1800.0)
    assert suspension_for_outcome("provider_auth") == ("auth", 3600.0)
    assert suspension_for_outcome("provider_rate") == ("rate", 180.0)


def test_suspension_decision_ignores_normal_outcomes():
    """普通结果不触发暂停 —— 否则一个超时就把 provider 停了。"""
    from sharp.dispatcher.scheduler.provider_health import suspension_for_outcome

    for outcome in ("success", "failed", "rejected", "cancelled", None, ""):
        assert suspension_for_outcome(outcome) is None
