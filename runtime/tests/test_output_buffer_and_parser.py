"""Pure-logic tests for patch #1 (_CappedBuffer) and #3 (extract_json_object)."""

from __future__ import annotations

import pytest

from sharp.dispatcher.output_parser import (
    MAX_OBJECT_START_ATTEMPTS,
    extract_json_object,
)
from sharp.dispatcher.runtime.process import _CappedBuffer, _TRUNCATION_MARKER


# --- #1 _CappedBuffer ------------------------------------------------------


def test_small_output_is_not_truncated():
    buf = _CappedBuffer(limit=1000)
    buf.append("hello ")
    buf.append("world")
    assert buf.value() == "hello world"
    assert buf.has_content()


def test_empty_buffer():
    buf = _CappedBuffer(limit=1000)
    assert not buf.has_content()
    assert buf.value() == ""


def test_keeps_head_and_tail_and_marks_truncation():
    buf = _CappedBuffer(limit=100)
    buf.append("HEAD" + "a" * 40)
    for _ in range(100):
        buf.append("x" * 20)
    buf.append("TAILZZZ")
    value = buf.value()
    assert value.startswith("HEAD")
    assert value.endswith("TAILZZZ")
    assert _TRUNCATION_MARKER.strip() in value


def test_repeated_compaction_stays_bounded_single_marker():
    buf = _CappedBuffer(limit=100)
    for _ in range(500):
        buf.append("y" * 20)
    value = buf.value()
    assert value.count(_TRUNCATION_MARKER) == 1
    # Bounded to a small multiple of the limit even after many compactions.
    assert len(value) <= 100 * 3 + len(_TRUNCATION_MARKER)


def test_truncation_marker_does_not_corrupt_tail_json():
    # The final JSON answer lives at the tail; truncation must not break parsing.
    buf = _CappedBuffer(limit=200)
    buf.append("session-id: abc123\n")
    buf.append("Z" * 5000)  # noise middle that gets dropped
    buf.append('{"description":"final answer"}')
    parsed = extract_json_object(buf.value())
    assert parsed == {"description": "final answer"}


# --- #3 extract_json_object ------------------------------------------------


def test_plain_object():
    assert extract_json_object('{"description":"x"}') == {"description": "x"}


def test_fenced_block():
    text = 'prose\n```json\n{"a": 1}\n```\ntrailer'
    assert extract_json_object(text) == {"a": 1}


def test_embedded_object_with_surrounding_prose():
    text = 'here: {"accepted":true,"data":{"description":"ok"}} done'
    assert extract_json_object(text)["accepted"] is True


def test_returns_first_object_when_multiple():
    assert extract_json_object('{"k":1} then {"k":2}') == {"k": 1}


def test_tail_object_after_many_leading_braces_is_found():
    text = "{" * 3000 + '\n{"description":"tail answer"}'
    assert extract_json_object(text) == {"description": "tail answer"}


def test_no_json_raises():
    with pytest.raises(ValueError):
        extract_json_object("no braces here at all")


def test_pathological_braces_are_bounded_and_do_not_hang():
    # Both ends dense with unmatched braces; must return quickly (bounded scan).
    big = "{" * 80000
    with pytest.raises(ValueError):
        extract_json_object(big + " noise " + big)


def test_known_limitation_object_buried_in_middle_is_missed():
    # Documented trade-off from patch #3: an object sandwiched between more than
    # MAX_OBJECT_START_ATTEMPTS braces on both sides is not found.
    n = MAX_OBJECT_START_ATTEMPTS
    text = "{" * n + '{"description":"buried"}' + "{" * n
    with pytest.raises(ValueError):
        extract_json_object(text)
