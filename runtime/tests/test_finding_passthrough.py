"""findings 从 AI 输出到产物登记的通路测试（P1 期间发现的真实缺陷的回归锁）。

**缺陷回顾**：`contracts.validate_explore_payload` 会重建每个 finding 的字段字典，
但它漏掉了 `kind` 与 `score`；而 `tasks/explore._best_effort_create_vulns` 正是靠这两个
字段去登记"旗帜 + 分值"。后果是：

- 评分类任务（CTF / 授权评测）在 **explore 路径**上永远记不上分，scoreboard 恒为 0；
- 旗帜还被错记成 `vuln`（`kind` 默认值），污染漏洞库统计。

bootstrap 路径因为把 `findings` **原样透传**（`result["findings"] = findings`）而侥幸正常
—— 两条路径行为不一致，是这个问题长期没被发现的原因。

本文件同时锁住两条路径：契约解析结果 + 传给 `client.create_vulnerability` 的实际参数。
不需要 docker / 真实容器。
"""

from __future__ import annotations

import pytest

from sharp.dispatcher.contracts import (
    validate_bootstrap_execute_payload,
    validate_explore_payload,
)
from sharp.dispatcher.tasks.explore import _best_effort_create_vulns


class _FakeResponse:
    ok = True
    status_code = 201


class _FakeClient:
    """记录 create_vulnerability 的实际入参。"""

    def __init__(self, ok: bool = True):
        self.calls: list[dict] = []
        self._ok = ok

    def create_vulnerability(self, project_id, fact_id, title, **kwargs):
        self.calls.append({"project_id": project_id, "fact_id": fact_id, "title": title, **kwargs})
        resp = _FakeResponse()
        resp.ok = self._ok
        resp.status_code = 201 if self._ok else 500
        return resp


def _explore_payload(findings: list[dict]) -> dict:
    return {"accepted": True, "data": {"description": "本轮结论", "findings": findings}}


# ── 契约层：kind / score 必须活下来 ───────────────────────────────────────────

def test_explore_contract_keeps_flag_kind_and_score():
    _kind, _desc, findings = validate_explore_payload(
        _explore_payload([{"title": "a-05 旗帜", "kind": "flag", "score": 100}])
    )
    assert findings[0]["kind"] == "flag"
    assert findings[0]["score"] == 100


def test_explore_contract_defaults_to_vuln_zero():
    _kind, _desc, findings = validate_explore_payload(
        _explore_payload([{"title": "SQL 注入", "severity": "high"}])
    )
    assert findings[0]["kind"] == "vuln"
    assert findings[0]["score"] == 0


def test_explore_contract_keeps_finding_kind():
    _kind, _desc, findings = validate_explore_payload(
        _explore_payload([{"title": "配置错误", "kind": "finding"}])
    )
    assert findings[0]["kind"] == "finding"


@pytest.mark.parametrize("bad_kind", ["flags", "FLAG", 5])
def test_explore_contract_rejects_unknown_kind(bad_kind):
    with pytest.raises(ValueError):
        validate_explore_payload(_explore_payload([{"title": "t", "kind": bad_kind}]))


def test_empty_kind_falls_back_to_vuln():
    """模型给空值等同于没给：容错归一到默认 vuln，而不是整轮判畸形。"""
    _kind, _desc, findings = validate_explore_payload(
        _explore_payload([{"title": "t", "kind": "", "score": None}])
    )
    assert findings[0]["kind"] == "vuln"
    assert findings[0]["score"] == 0


@pytest.mark.parametrize("bad_score", ["abc", [1], {"n": 1}])
def test_explore_contract_rejects_non_numeric_score(bad_score):
    with pytest.raises(ValueError, match="score must be an integer"):
        validate_explore_payload(_explore_payload([{"title": "t", "kind": "flag", "score": bad_score}]))


def test_explore_contract_accepts_numeric_string_score():
    """模型常把分值写成字符串，应当接受而不是把整轮结论判为畸形。"""
    _kind, _desc, findings = validate_explore_payload(
        _explore_payload([{"title": "flag", "kind": "flag", "score": "300"}])
    )
    assert findings[0]["score"] == 300


def test_explore_contract_clamps_out_of_range_score():
    """越界分值钳制到服务端模型边界内，避免 create 调用被 422 拒绝。"""
    _kind, _desc, findings = validate_explore_payload(
        _explore_payload([{"title": "flag", "kind": "flag", "score": 10**9}])
    )
    assert findings[0]["score"] == 1000000
    _kind, _desc, findings = validate_explore_payload(
        _explore_payload([{"title": "flag", "kind": "flag", "score": -(10**9)}])
    )
    assert findings[0]["score"] == -1000000


# ── bootstrap 路径：原样透传（与 explore 行为对齐）────────────────────────────

def test_bootstrap_execute_passes_findings_through():
    payload = {
        "accepted": True,
        "data": {
            "fact": {"description": "侦察完成"},
            "complete": {"description": "目标已达成"},
            "findings": [{"title": "flag", "kind": "flag", "score": 500, "severity": "info"}],
        },
    }
    kind, result = validate_bootstrap_execute_payload(payload)
    assert kind == "complete"
    assert result["findings"][0]["kind"] == "flag"
    assert result["findings"][0]["score"] == 500


# ── 登记层：参数真的带着 kind / score 到 client ───────────────────────────────

def test_flag_reaches_client_with_kind_and_score():
    """端到端：契约解析 → 登记调用（这是缺陷发生时断裂的那一段）。"""
    _kind, _desc, findings = validate_explore_payload(
        _explore_payload([
            {"title": "a-05 通关旗帜", "kind": "flag", "score": 100, "evidence": "flag{...}"},
        ])
    )
    client = _FakeClient()
    _best_effort_create_vulns(client, "proj_1", "f001", "i001", "worker-a", findings)

    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["project_id"] == "proj_1"
    assert call["fact_id"] == "f001"
    assert call["kind"] == "flag"
    assert call["score"] == 100
    assert call["intent_id"] == "i001"


def test_multiple_findings_all_registered():
    _kind, _desc, findings = validate_explore_payload(
        _explore_payload([
            {"title": "vuln", "severity": "critical"},
            {"title": "flag-1", "kind": "flag", "score": 100},
            {"title": "flag-2", "kind": "flag", "score": 300},
        ])
    )
    client = _FakeClient()
    _best_effort_create_vulns(client, "p", "f001", "i001", "w", findings)
    assert [c["kind"] for c in client.calls] == ["vuln", "flag", "flag"]
    assert [c["score"] for c in client.calls] == [0, 100, 300]


def test_no_findings_means_no_calls():
    client = _FakeClient()
    _best_effort_create_vulns(client, "p", "f001", "i001", "w", None)
    _best_effort_create_vulns(client, "p", "f001", "i001", "w", [])
    assert client.calls == []


def test_missing_fact_id_skips_registration():
    client = _FakeClient()
    _best_effort_create_vulns(client, "p", None, "i001", "w", [{"title": "t"}])
    assert client.calls == []


def test_client_failure_is_swallowed():
    """登记是 best-effort：产物创建失败不能让整轮任务状态变成失败。"""
    client = _FakeClient(ok=False)
    _best_effort_create_vulns(client, "p", "f001", "i001", "w", [{"title": "t"}])
    assert len(client.calls) == 1  # 调用发生了，但异常被吞掉


def test_client_exception_is_swallowed():
    class _Boom:
        def create_vulnerability(self, *a, **kw):
            raise RuntimeError("boom")

    _best_effort_create_vulns(_Boom(), "p", "f001", "i001", "w", [{"title": "t"}])  # 不应抛出
