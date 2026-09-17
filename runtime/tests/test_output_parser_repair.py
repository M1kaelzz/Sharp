"""回包缺收尾括号时的补救（基于真实事故）。

**真实事故**：worker 对本地靶机的 bootstrap conclude 回包内容是完整的
（含 2 条 env_facts、product、1750 字逐项结论、多处"已验证不通"），但会话里
`stop_reason=end_turn` 的最终文本**少写了一个外层 `}`**（需要 `}}}`，实际 `}}`）。

后果链条非常隐蔽：

1. `json.loads` 整段失败；
2. `raw_decode` 从内层 `"data": {` 起解出一个**合法但错误**的对象
   → 解析器静默返回内层 `data`（键只剩 `product/env_facts/fact`）；
3. 校验器报出误导性的 "accepted must be true or false"；
4. **整条结论被丢弃**，环境基线与产品标注一起没了，而日志里看不出真正原因。

下面的样例直接取自那次会话（保留结构与关键片段）。
"""

from __future__ import annotations

import json

import pytest

from sharp.dispatcher.output_parser import _repair_unbalanced, extract_json_object


# 取自真实会话：结尾是 `"}}`（少一个外层 }）
_REAL_TRUNCATED = (
    '{"accepted": true, "data": {"product": "Acme Router mock (AcmeOS/AcmeHTTPD on Python BaseHTTP)", '
    '"env_facts": [{"key": "network.reachable", "value": "ok", "note": "http://host.docker.internal:8099/ reachable"}, '
    '{"key": "target.fingerprint", "value": "ok", "note": "Server: BaseHTTP/0.6"}], '
    '"fact": {"description": "逐项测试完成：(1) 未发现未授权访问；(2) 403 未绕过（已证不通）；'
    '(3) 弱口令均返回 401，未获得有效会话。"}}'
)


def test_real_truncated_payload_is_repaired():
    """核心回归：少一个外层 } 不该让整条结论作废。"""
    parsed = extract_json_object(_REAL_TRUNCATED)
    assert parsed.get("accepted") is True, "必须拿到外层包装，而不是退化成内层 data"
    assert set(parsed) == {"accepted", "data"}
    assert parsed["data"]["product"].startswith("Acme Router mock")
    assert len(parsed["data"]["env_facts"]) == 2


def test_truncated_payload_reaches_validator_and_extractors():
    """修好之后，下游（校验器、env_facts/product 提取器）必须都能接住。"""
    from sharp.dispatcher.contracts import validate_bootstrap_conclude_payload
    from sharp.dispatcher.tasks.explore import _extract_env_facts, _extract_product

    parsed = extract_json_object(_REAL_TRUNCATED)
    kind, description = validate_bootstrap_conclude_payload(parsed)
    assert kind == "fact"
    assert "未发现未授权访问" in description
    assert len(_extract_env_facts(parsed)) == 2
    assert _extract_product(parsed).startswith("Acme Router mock")


def test_repair_only_appends_brackets():
    assert _repair_unbalanced('{"a": {"b": 1}') == '{"a": {"b": 1}}'
    assert _repair_unbalanced('{"a": [1, 2') == '{"a": [1, 2]}'
    assert _repair_unbalanced('{"a": 1}') is None, "本来就平衡 → 不补，避免误改"
    assert _repair_unbalanced('{"a": "未闭合') is None, "字符串未闭合 → 不硬凑"
    assert _repair_unbalanced('}}') is None, "提前闭合 → 不是缺结尾"


def test_repair_ignores_braces_inside_strings():
    """字符串里的括号不能参与计数（否则会把合法 JSON 判成不平衡）。"""
    assert _repair_unbalanced('{"a": "}{"}') is None
    assert _repair_unbalanced('{"a": "\\"}"}') is None
    assert _repair_unbalanced('{"a": "}"') == '{"a": "}"}'


def test_unrepairable_garbage_still_raises():
    with pytest.raises(ValueError):
        extract_json_object("这不是 JSON，也没有大括号")


def test_wellformed_payload_unchanged():
    good = json.dumps({"accepted": True, "data": {"fact": {"description": "x"}}})
    assert extract_json_object(good) == {"accepted": True, "data": {"fact": {"description": "x"}}}


def test_error_message_names_actual_keys():
    """结构不对时要报出**拿到的键**，而不是一句会把人引错方向的固定文案。

    真实事故里那句 "accepted must be true or false" 让人往字段/鉴权方向查，
    而真因是"JSON 被截断、解析退化成内层对象"。
    """
    from sharp.dispatcher.contracts import validate_bootstrap_conclude_payload

    with pytest.raises(ValueError) as exc:
        validate_bootstrap_conclude_payload({"product": "x", "fact": {"description": "d"}})
    message = str(exc.value)
    assert "missing 'accepted'" in message
    assert "product" in message and "fact" in message
