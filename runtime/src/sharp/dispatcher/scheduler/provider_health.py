"""Provider 故障分类与冷却（P2：provider 降级）。

## 问题

provider 额度耗尽（例如 `402 Insufficient Balance`）或凭据失效时，worker 进程非零退出，
调度器只看到通用的 `"failed"`。而 `choose_worker` 的排序只取决于 priority 与在跑数量——
**不可用的 worker 会被反复选中**，项目就在原地空转，配置里的其它 provider 永远轮不到。

## 设计

1. **只认 provider 专有文案，不用裸 HTTP 状态码**。渗透输出里 `401`/`403`/`429` 到处都是
   （未授权访问测试本身就在造这些响应），按数字匹配会把健康 worker 误冷却，属于比原问题更糟的
   故障。因此标记都是完整短语："insufficient balance" / "invalid api key" 之类。
2. **只扫 stderr，且调用方保证 worker 非零退出**（见三个任务里的 command-failed 分支）。
   渗透结论写在 stdout，正常任务不参与判定。
3. **冷却时长远大于启动健康检查的 5 秒**：provider 故障重试无意义，必须冷却足够久，
   让调度器自然换用下一个候选（其它 provider）。

纯函数 + 常量，无副作用，便于单测（`tests/test_provider_health.py`）。
"""

from __future__ import annotations

import re

# ── 故障标记 ─────────────────────────────────────────────────────────────────
# 分两档是刻意的：`too many requests` / `payment required` 这类短语**目标站点也会返回**
# （渗透测试本身就在造 401/402/429），单独命中不足以断定是 provider 故障，必须同时
# 出现 provider 上下文（api / openai / anthropic / billing …）。
STRONG_MARKERS: dict[str, tuple[str, ...]] = {
    "quota": (
        "insufficient balance",
        "insufficient_quota",
        "insufficient quota",
        "quota exceeded",
        "exceeded your current quota",
        "credit balance is too low",
        "out of credits",
        "no remaining credits",
        "billing hard limit",
    ),
    "auth": (
        "invalid api key",
        "invalid_api_key",
        "invalid x-api-key",
        "authentication_error",
        "authentication failed",
        "api key not valid",
        "please check your api key",
    ),
    "rate": (
        "overloaded_error",
        "server is overloaded",
    ),
}

WEAK_MARKERS: dict[str, tuple[str, ...]] = {
    "quota": ("payment required", "402 payment"),
    # 注意：不要放裸数字（"429"）或裸 "401 unauthorized" —— 目标站点输出里遍地都是，
    # 且一旦写成单元素字符串（漏逗号）会被逐字符迭代，等于匹配任意含数字的文本。
    "rate": ("too many requests", "rate limit exceeded", "rate_limit_exceeded"),
}

# provider 上下文：出现这些词才认为错误来自模型服务侧而非被测目标
CONTEXT_RE = re.compile(
    r"\bapi\b|api key|openai|anthropic|deepseek|claude|codex|provider|billing|"
    r"subscription|\bplan\b|llm|completion",
    re.IGNORECASE,
)

# ── 冷却时长（秒）────────────────────────────────────────────────────────────
PROVIDER_COOLDOWN_SECONDS: dict[str, float] = {
    "quota": 1800.0,   # 30 分钟：额度问题不会自己好，够久以便换 provider
    "auth": 3600.0,    # 60 分钟：凭据失效需要人工介入
    "rate": 180.0,     # 3 分钟：限流通常短暂
}
PROVIDER_COOLDOWN_DEFAULT = 600.0

STATUS_PREFIX = "provider_"


def classify_provider_error(stderr: str | None) -> str | None:
    """从 worker 的 stderr 识别 provider 级故障：'quota' | 'auth' | 'rate' | None。

    强标记单独命中即可；弱标记必须伴随 provider 上下文（见 CONTEXT_RE）。
    """
    if not stderr:
        return None
    low = stderr.lower()
    for kind, markers in STRONG_MARKERS.items():
        if any(marker in low for marker in markers):
            return kind
    for kind, markers in WEAK_MARKERS.items():
        if any(marker in low for marker in markers) and CONTEXT_RE.search(stderr):
            return kind
    return None


def provider_status(kind: str) -> str:
    """任务函数返回的 status（如 'provider_quota'）。"""
    return f"{STATUS_PREFIX}{kind}"


def provider_error_status(stderr: str | None) -> str | None:
    """一步到位：给 stderr 返回可返回的 status，认不出则 None。"""
    kind = classify_provider_error(stderr)
    return provider_status(kind) if kind else None


def provider_kind_from_status(status: str | None) -> str | None:
    """从任务 outcome 反查故障类型（调度器用；非 provider 状态返回 None）。"""
    if not status or not status.startswith(STATUS_PREFIX):
        return None
    return status[len(STATUS_PREFIX):] or None


def cooldown_for(kind: str) -> float:
    return PROVIDER_COOLDOWN_SECONDS.get(kind, PROVIDER_COOLDOWN_DEFAULT)


def suspension_for_outcome(outcome: str | None) -> tuple[str, float] | None:
    """调度器决策：任务 outcome → (故障类型, 暂停秒数)；非 provider 故障返回 None。

    抽成纯函数是为了让"要不要暂停这个 worker、暂停多久"这条规则可测——
    它决定 provider 降级是否真的发生（此前 unhealthy 一律只退避 5 秒）。
    """
    kind = provider_kind_from_status(outcome)
    if not kind:
        return None
    return kind, cooldown_for(kind)
