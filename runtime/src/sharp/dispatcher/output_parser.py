from __future__ import annotations

import json
import re
from typing import Any


FENCED_BLOCK_RE = re.compile(r"```(?:json)?\s*\n?(.*?)```", re.IGNORECASE | re.DOTALL)
# Cap how many "{" start positions we attempt per segment. Worker output can
# contain thousands of braces (code, logs, nested JSON); trying every one makes
# extraction pathologically slow. The real answer is almost always the first or
# last well-formed object, so a bounded scan is sufficient.
MAX_OBJECT_START_ATTEMPTS = 2000


def _repair_unbalanced(text: str) -> str | None:
    """补齐**缺失的收尾括号**（只补 `}` / `]`，绝不发明内容）。

    为什么需要：实测 worker 会把嵌套很深的回包少写一个外层 `}`（会话里
    `stop_reason=end_turn`，模型自认写完了）。此时的后果很隐蔽：

    1. `json.loads` 整段失败；
    2. `raw_decode` 从内层 `"data": {` 起却能解出一个**合法但错误**的对象
       → 解析器静默返回内层 `data`（键只剩 `product/env_facts/fact`，少了 `accepted`）；
    3. 校验器于是报出误导性的 "accepted must be true or false" ——
       真实原因（JSON 不完整）完全不可见，整条结论被丢。

    修法：数一遍字符串外的括号余额，缺多少补多少（只补结尾，不动中间）。
    "半截 JSON" 因此可解析；真正的乱码仍会失败，不会被硬凑出来。
    """
    depth_curly = 0
    depth_square = 0
    in_str = False
    escape = False
    for ch in text:
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth_curly += 1
        elif ch == "}":
            depth_curly -= 1
        elif ch == "[":
            depth_square += 1
        elif ch == "]":
            depth_square -= 1
        if depth_curly < 0 or depth_square < 0:
            return None  # 提前闭合 → 不是"缺结尾"，别乱补
    if in_str or depth_curly < 0 or depth_square < 0:
        return None
    missing = "]" * depth_square + "}" * depth_curly
    return text + missing if missing else None


def extract_json_object(text: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    seen: set[str] = set()

    for candidate in _candidate_segments(text):
        segment = candidate.strip()
        if not segment or segment in seen:
            continue
        seen.add(segment)

        try:
            parsed = json.loads(segment)
        except json.JSONDecodeError:
            pass
        else:
            if isinstance(parsed, dict):
                return parsed

        # 缺收尾括号的补救必须**排在 raw_decode 扫描之前**：否则会先返回内层对象
        repaired = _repair_unbalanced(segment)
        if repaired is not None:
            try:
                parsed = json.loads(repaired)
            except json.JSONDecodeError:
                pass
            else:
                if isinstance(parsed, dict):
                    return parsed

        for start in _object_start_positions(segment):
            try:
                # Pass the index to raw_decode instead of slicing the string;
                # segment[start:] would copy O(len) bytes on every attempt,
                # making the whole scan O(n^2).
                parsed, _end = decoder.raw_decode(segment, start)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed

    raise ValueError("no JSON object found in output")


def _candidate_segments(text: str) -> list[str]:
    segments = [text.strip()]
    segments.extend(match.group(1).strip() for match in FENCED_BLOCK_RE.finditer(text))
    return segments


def _object_start_positions(text: str) -> list[int]:
    # Collect the first and last "{" positions (bounded by
    # MAX_OBJECT_START_ATTEMPTS) so an object at either the head or the tail of
    # a very large output is still found without scanning every brace.
    half = max(1, MAX_OBJECT_START_ATTEMPTS // 2)

    head: list[int] = []
    index = text.find("{")
    while index != -1 and len(head) < half:
        head.append(index)
        index = text.find("{", index + 1)

    tail: list[int] = []
    index = text.rfind("{")
    while index != -1 and len(tail) < half:
        tail.append(index)
        index = text.rfind("{", 0, index)
    tail.reverse()

    if not tail:
        return head
    seen = set(head)
    return head + [pos for pos in tail if pos not in seen]
