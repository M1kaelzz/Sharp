"""结论可交付性评估（P0-1）。

## 为什么需要它

真实授权渗透的交付物是**报告**，而报告的价值取决于结论能否被复现。一个只有标题与
描述的 `confirmed` 漏洞，对外提交时等于"不可验证的声称"——轻则被平台打回，重则让
整份报告的可信度受损。

现状：`vulnerabilities` 表的 `evidence` / `reproduction` / `impact` / `url` 四个字段
**存在但可空**，没有任何地方检查它们是否齐备。于是"证据不足的结论"会安静地混进报告。

本模块只做一件事：对每个产物算出**可复现四件套**的完备度，把缺失项明确列出来，
供 API、报告与界面标注——让"证据不足"在**交付之前**就可见。

## 四件套（仅对 `vuln` / `finding`；`flag` 等评分类产物不适用）

| 项 | 字段 | 含义 |
| --- | --- | --- |
| 原始请求/响应 | `evidence` | 证明漏洞确实存在 |
| 复现步骤 | `reproduction` | 别人能照着做出来 |
| 影响说明 | `impact` | 为什么值得修 |
| 定位 | `url` | 落在哪个资产/接口上 |

纯函数 + 冻结数据类，无副作用，便于单测。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# 各字段的最小有效长度：太短的内容（"见截图"、"如上"）不算证据
_MIN_EVIDENCE = 20
_MIN_REPRODUCTION = 20
_MIN_IMPACT = 10

# 用于给出"更有指导性"的补充提示（不是硬性判定）
_REQUEST_RE = re.compile(r"(?:^|\n)\s*(?:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+\S+|curl\s|wget\s", re.IGNORECASE)
_RESPONSE_RE = re.compile(r"HTTP/\d(?:\.\d)?\s+\d{3}|^\s*(?:Server|Set-Cookie|Content-Type)\s*:", re.IGNORECASE | re.MULTILINE)

# 不适用的产物类型（评分类产物不需要复现证据）
NOT_APPLICABLE_KINDS = frozenset({"flag"})

FIELD_LABELS = {
    "evidence": "原始请求/响应",
    "reproduction": "复现步骤",
    "impact": "影响说明",
    "url": "定位（资产/接口）",
}


@dataclass(frozen=True)
class FindingQuality:
    """单个产物的可交付性评估结果。"""

    applicable: bool
    score: int                      # 已满足项数
    total: int                      # 应满足项数
    complete: bool
    missing: tuple[str, ...]        # 缺失项的**字段名**（便于程序判断）
    missing_labels: tuple[str, ...] # 缺失项的中文标签（便于展示）
    notes: tuple[str, ...]          # 补充提示（例如"有请求但未见响应"）

    def as_dict(self) -> dict:
        return {
            "applicable": self.applicable,
            "score": self.score,
            "total": self.total,
            "complete": self.complete,
            "missing": list(self.missing),
            "missing_labels": list(self.missing_labels),
            "notes": list(self.notes),
        }


def _filled(value: str | None, min_len: int) -> bool:
    return bool(value) and len(str(value).strip()) >= min_len


def assess_finding(
    *,
    kind: str = "vuln",
    url: str = "",
    evidence: str = "",
    reproduction: str = "",
    impact: str = "",
) -> FindingQuality:
    """评估一个产物是否具备"可对外交付"的证据完整度。"""
    if kind in NOT_APPLICABLE_KINDS:
        return FindingQuality(
            applicable=False, score=0, total=0, complete=True,
            missing=(), missing_labels=(),
            notes=("评分类产物（旗帜）不需要复现证据，不参与完备度评估",),
        )

    checks = {
        "url": _filled(url, 1),
        "evidence": _filled(evidence, _MIN_EVIDENCE),
        "reproduction": _filled(reproduction, _MIN_REPRODUCTION),
        "impact": _filled(impact, _MIN_IMPACT),
    }
    missing = tuple(name for name, ok in checks.items() if not ok)

    notes: list[str] = []
    if checks["evidence"]:
        has_req, has_resp = bool(_REQUEST_RE.search(evidence)), bool(_RESPONSE_RE.search(evidence))
        if has_req and not has_resp:
            notes.append("证据含请求但未见响应片段——建议补充关键响应以证明漏洞存在")
        elif has_resp and not has_req:
            notes.append("证据含响应但未见原始请求——建议补充可重放的请求")
    else:
        notes.append("缺少原始请求/响应：这是复核者判断漏洞真伪的第一依据")

    score = sum(1 for ok in checks.values() if ok)
    return FindingQuality(
        applicable=True,
        score=score,
        total=len(checks),
        complete=not missing,
        missing=missing,
        missing_labels=tuple(FIELD_LABELS[name] for name in missing),
        notes=tuple(notes),
    )


def report_warning(quality: FindingQuality) -> str:
    """报告里使用的一行标注（空字符串 = 无需标注）。"""
    if not quality.applicable or quality.complete:
        return ""
    return (
        f"⚠️ 证据不足（缺 {len(quality.missing)} 项：{'、'.join(quality.missing_labels)}）"
        f"—— 建议补充后再对外提交；本结论可能无法通过复核。"
    )
