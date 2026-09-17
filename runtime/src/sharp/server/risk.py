"""Server-side risk classification for intents.

This is the *authoritative* gate. The model's annotation (sent from the
dispatcher as ``risk_level``) is advisory only: we take ``max(model, keyword)``
so a model that claims "low" cannot slip a destructive intent past the gate.
"""

from __future__ import annotations

import os
import re

RiskLevel = str  # 'low' | 'medium' | 'high' | 'critical'

_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}


def rank(level: RiskLevel) -> int:
    return _RANK.get(level, 0)


def combine(model: RiskLevel | None, keyword: RiskLevel) -> RiskLevel:
    """Final level = max(model annotation, server keyword judgement).

    A missing model annotation counts as 'low' (backward compatible with old
    models that do not emit risk fields)."""
    # Treat a missing model annotation as low so we never return None here.
    model_level = model if model in _RANK else "low"
    return model_level if rank(model_level) >= rank(keyword) else keyword


# --- Keyword rules: pattern -> minimum risk level -------------------------
# A description hitting a pattern is at least this risky. 'critical' wins.
CRITICAL_PATTERNS = (
    "删除数据库", "清库", "drop database", "truncate",
    "webshell", "上传木马", "写入webshell", "写入木马",
    "提权", "权限提升", "privilege escalation", "getsystem", "admin 权限",
    "doS", "拒绝服务", "耗尽资源", "批量删除",
    "重置管理员", "修改管理员密码", "删除所有", "删除全部",
    "勒索", "加密勒索", "植入后门", "后门",
)
HIGH_PATTERNS = (
    "命令执行", "远程代码执行", "代码执行", "rce",
    "sql注入写", "sql 写", "insert into", "update ", "delete ",
    "文件写入", "写入文件", "上传", "文件覆盖", "文件删除",
    "任意文件", "文件读取", "读取任意文件",
    "未授权", "权限绕过", "认证绕过", "越权",
    "反序列化", "deserialization", "xxe", "ssrf",
    "远程命令", "命令注入",
)
MEDIUM_PATTERNS = (
    "sql注入", "sql injection", "xss", "跨站",
    "参数篡改", "参数污染", "限速爆破", "爆破", "暴力破解",
    "篡改", "越权访问", "csrf",
    "注入", "探测",
)


def classify(text: str) -> tuple[RiskLevel, str]:
    """Classify an intent description into a risk level plus a human reason.

    Returns ``(level, reason)`` where reason is a short Chinese phrase naming
    the strongest matching pattern (or '' for low)."""
    normalized = re.sub(r"\s+", " ", text or "").lower()
    for pattern in CRITICAL_PATTERNS:
        if pattern in normalized:
            return "critical", f"命中高风险模式：{pattern}"
    for pattern in HIGH_PATTERNS:
        if pattern in normalized:
            return "high", f"命中高风险模式：{pattern}"
    for pattern in MEDIUM_PATTERNS:
        if pattern in normalized:
            return "medium", f"命中中风险模式：{pattern}"
    return "low", "信息收集 / 被动分析，默认放行"


# --- Approval policy (server authority) -----------------------------------
# The dispatcher's dispatch.yaml is a *worker-side* config we do not trust for
# the security boundary: AI could edit it to self-approve. So the block policy
# lives here, controlled by env vars (operator-owned). Defaults:
#   high/critical -> always block; medium -> block only if enabled.
BLOCK_MEDIUM = os.environ.get("SHARP_APPROVAL_BLOCK_MEDIUM", "false").strip().lower() in (
    "1", "true", "yes", "on"
)
APPROVAL_ENABLED = os.environ.get("SHARP_APPROVAL_ENABLED", "true").strip().lower() not in (
    "0", "false", "no", "off"
)
# Server-side approval timeout floor (hours). Never below this.
APPROVAL_TIMEOUT_HOURS = max(1, int(os.environ.get("SHARP_APPROVAL_TIMEOUT_HOURS", "24")))


def needs_approval(level: RiskLevel) -> bool:
    """Whether a final risk level requires the human-approval gate.

    Approval can be globally disabled (for tests / single-operator flows that
    explicitly opt out), but then high-risk intents simply run — never silently
    auto-approved, just not gated. Default keeps high/critical gated and medium
    open unless block_medium is on."""
    if not APPROVAL_ENABLED:
        return False
    if level in ("high", "critical"):
        return True
    if level == "medium":
        return BLOCK_MEDIUM
    return False