from __future__ import annotations

from typing import Any

from sharp.dispatcher.output_parser import extract_json_object


def parse_json_output(stdout: str) -> dict[str, Any]:
    return extract_json_object(stdout)


def _unwrap_wrapped_payload(payload: dict[str, Any]) -> tuple[bool | None, dict[str, Any] | None]:
    accepted = payload.get("accepted")
    if accepted is False:
        return False, None
    if accepted is True:
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ValueError("data must be an object")
        return True, data
    return None, None


def _is_dict(value: Any) -> bool:
    return isinstance(value, dict)


def _looks_like_reason_data(payload: dict[str, Any]) -> bool:
    if not isinstance(payload, dict):
        return False
    keys = set(payload)
    if keys == {"complete"}:
        complete = payload["complete"]
        return isinstance(complete, dict) and "from" in complete and "description" in complete
    if keys == {"intents"}:
        return isinstance(payload["intents"], list)
    if keys == {"intent"}:
        intent = payload["intent"]
        return isinstance(intent, dict) and "from" in intent and "description" in intent
    return False


def _looks_like_bootstrap_execute_data(payload: dict[str, Any]) -> bool:
    if not isinstance(payload, dict) or set(payload) != {"fact", "complete"}:
        return False
    return _is_dict(payload.get("fact")) and _is_dict(payload.get("complete"))


def _looks_like_bootstrap_conclude_data(payload: dict[str, Any]) -> bool:
    if not isinstance(payload, dict):
        return False
    keys = set(payload)
    if keys not in ({"fact"}, {"fact", "complete"}):
        return False
    return _is_dict(payload.get("fact"))


def _looks_like_explore_data(payload: dict[str, Any]) -> bool:
    if not isinstance(payload, dict):
        return False
    keys = set(payload)
    return keys == {"description"} or keys == {"description", "findings"}


def validate_reason_payload(
    payload: dict[str, Any], open_intents_empty: bool, max_intents: int,
) -> tuple[str, dict[str, Any] | list[dict[str, Any]] | None]:
    accepted, data = _unwrap_wrapped_payload(payload)
    if accepted is False:
        return "rejected", None
    if accepted is None:
        if not _looks_like_reason_data(payload):
            raise ValueError("accepted must be true or false")
        data = payload
    if not isinstance(data, dict):
        raise ValueError("accepted must be true or false")
    complete = data.get("complete")
    intents = data.get("intents")
    # backward compat: accept singular "intent" key from LLMs
    if intents is None:
        singular = data.get("intent")
        if isinstance(singular, dict):
            intents = [singular]
    if complete is not None:
        if intents is not None:
            raise ValueError("complete and intents cannot coexist")
        if not isinstance(complete, dict) or "from" not in complete or "description" not in complete:
            raise ValueError("invalid complete payload")
        return "complete", complete
    if intents is not None:
        if not isinstance(intents, list):
            raise ValueError("intents must be an array")
        for i, intent in enumerate(intents):
            if not isinstance(intent, dict) or "from" not in intent or "description" not in intent:
                raise ValueError(f"invalid intent at index {i}")
        if not intents and open_intents_empty:
            raise ValueError("intents must not be empty when open_intents is empty")
        intents = intents[:max_intents]
        if not intents:
            return "noop", None
        return "intents", intents
    if open_intents_empty:
        raise ValueError("intents is required when open_intents is empty")
    return "noop", None


def validate_bootstrap_execute_payload(payload: dict[str, Any]) -> tuple[str, dict[str, str] | None]:
    accepted, data = _unwrap_wrapped_payload(payload)
    if accepted is False:
        return "rejected", None
    if accepted is None:
        if not _looks_like_bootstrap_execute_data(payload):
            raise ValueError("accepted must be true or false")
        data = payload
    if not isinstance(data, dict):
        raise ValueError("accepted must be true or false")

    fact = data.get("fact")
    if not isinstance(fact, dict):
        raise ValueError("fact is required")
    fact_description = fact.get("description")
    if not isinstance(fact_description, str) or not fact_description.strip():
        raise ValueError("fact.description is required")

    result = {"fact_description": fact_description.strip()}
    complete = data.get("complete")
    if complete is None:
        raise ValueError("complete is required")
    if not isinstance(complete, dict):
        raise ValueError("complete must be an object")
    complete_description = complete.get("description")
    if not isinstance(complete_description, str) or not complete_description.strip():
        raise ValueError("complete.description is required")
    result["complete_description"] = complete_description.strip()
    findings = data.get("findings")
    if findings is not None:
        if not isinstance(findings, list):
            raise ValueError("findings must be an array")
        result["findings"] = findings
    return "complete", result


def validate_bootstrap_conclude_payload(payload: dict[str, Any]) -> tuple[str, str | None]:
    accepted, data = _unwrap_wrapped_payload(payload)
    if accepted is False:
        return "rejected", None
    if accepted is None:
        if not _looks_like_bootstrap_conclude_data(payload):
            # 说清**到底拿到了什么**。旧文案固定为 "accepted must be true or false"，
            # 而真实故障往往是"外层 JSON 缺收尾括号 → 解析器退化成内层 data 对象"，
            # 那句话只会把人引向字段/鉴权方向，看不到"JSON 不完整"这个真因。
            raise ValueError(
                "not a bootstrap conclude payload: missing 'accepted'; "
                f"got keys={sorted(payload)}"
            )
        data = payload
    if not isinstance(data, dict):
        raise ValueError("accepted must be true or false")
    # 允许的键必须与提示词要求 worker 输出的键**保持一致**。
    # 2026-09-15 实测踩坑：提示词示例里加了 `product` / `env_facts`，但这里仍只认
    # {"fact","complete"} → worker 照提示词输出反而**整包被拒**，事实、环境基线、
    # 产品标注一起丢掉（真实跑批里 bootstrap 跑满 400s 超时后的 conclude 就这样被吞了）。
    # 教训：改提示词输出格式时，必须同步检查校验器的允许键集 —— 现由
    # `test_prompted_keys_are_accepted_by_bootstrap_conclude` 从提示词**反推全部键**来钉住
    # （它加强后的第一跑就抓到了本行的遗漏：endpoint_tests 被拒）。
    extra_keys = set(data) - {
        "fact", "complete", "product", "env_facts", "findings", "endpoint_tests",
    }
    if extra_keys:
        raise ValueError(f"unexpected keys in conclude payload: {sorted(extra_keys)}")
    fact = data.get("fact")
    if not isinstance(fact, dict):
        raise ValueError("fact is required")
    fact_description = fact.get("description")
    if not isinstance(fact_description, str) or not fact_description.strip():
        raise ValueError("fact.description is required")
    return "fact", fact_description.strip()


def normalize_findings(findings_raw: Any) -> list[dict[str, Any]]:
    """校验并规范化 findings 数组（explore 与 bootstrap-conclude 共用）。

    抽出来是因为两条路径都要处理同一形状：分开实现过一次，结果 conclude 路径
    **整包丢掉 findings** —— worker 明明报了，漏洞库却是空的。
    """
    if not isinstance(findings_raw, list):
        raise ValueError("findings must be an array")
    findings: list[dict[str, Any]] = []
    for i, item in enumerate(findings_raw):
        if not isinstance(item, dict):
            raise ValueError(f"findings[{i}] must be an object")
        title = item.get("title")
        if not isinstance(title, str) or not title.strip():
            raise ValueError(f"findings[{i}].title is required")
        severity = item.get("severity", "info")
        if severity not in ("critical", "high", "medium", "low", "info"):
            raise ValueError(
                f"findings[{i}].severity must be one of critical/high/medium/low/info"
            )
        # Finding kind / score must survive this normalization: explore's
        # `_best_effort_create_vulns` reads them to register scored artifacts
        # (`kind='flag'` + points). Dropping them here silently turned every
        # captured flag into a `vuln` with score 0 — the scoreboard stayed
        # empty even when the worker reported flags correctly.
        kind = item.get("kind", "vuln") or "vuln"
        if kind not in ("vuln", "flag", "finding"):
            raise ValueError(f"findings[{i}].kind must be one of vuln/flag/finding")
        raw_score = item.get("score", 0)
        try:
            score = int(raw_score or 0)
        except (TypeError, ValueError):
            raise ValueError(f"findings[{i}].score must be an integer") from None
        # Keep within the server-side model bound so a wild model output can
        # never make the create call fail validation (422).
        score = max(-1000000, min(1000000, score))
        findings.append({
            "title": title.strip(),
            "severity": severity,
            "url": str(item.get("url", "") or ""),
            "description": str(item.get("description", "") or ""),
            "evidence": str(item.get("evidence", "") or ""),
            "reproduction": str(item.get("reproduction", "") or ""),
            "impact": str(item.get("impact", "") or ""),
            "recommendation": str(item.get("recommendation", "") or ""),
            "kind": kind,
            "score": score,
        })
    return findings


def validate_explore_payload(
    payload: dict[str, Any],
) -> tuple[str, str | None, list[dict[str, Any]] | None]:
    accepted, data = _unwrap_wrapped_payload(payload)
    if accepted is False:
        return "rejected", None, None
    if accepted is None:
        if not _looks_like_explore_data(payload):
            raise ValueError("accepted must be true or false")
        data = payload
    if not isinstance(data, dict):
        raise ValueError("accepted must be true or false")
    description = data.get("description")
    if not isinstance(description, str) or not description.strip():
        raise ValueError("description is required")
    findings_raw = data.get("findings")
    if findings_raw is not None:
        return "fact", description.strip(), normalize_findings(findings_raw)
        return "fact", description.strip(), findings or None
    return "fact", description.strip(), None


def conclude_findings(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """从 conclude 回包里尽力取 findings（结构不对就返回空，绝不抛）。

    conclude 路径是 bootstrap 超时后的兜底，不能因为 findings 形状有瑕疵就让
    整条结论作废 —— 但形状正确时也**必须**把它们登记进漏洞库。
    """
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    if not isinstance(data, dict):
        return []
    raw = data.get("findings")
    if not raw:
        return []
    try:
        return normalize_findings(raw)
    except Exception:
        return []
