"""Dispatcher 侧"结论字段 → 持久化"的接线测试（确定性，不依赖模型行为）。

**为什么需要这一层**：本轮三处缺陷（#3 env_facts / #4 假设 / 产品标注）单测全绿却在真实跑批里
产出 0 结果。单测证明"函数正确"，不证明"字段真被接住"。这里用假 client 把每条通道走一遍，
区分两类故障：**提示词没让模型输出** vs **代码没接住**。

（这也是旧清单里"调度成功派发路径需更完整假 SharpClient"那条遗留的落点。）
"""

from __future__ import annotations

from sharp.dispatcher.tasks.reason import _apply_hypothesis_actions


class _Resp:
    def __init__(self, ok: bool = True, status_code: int = 200):
        self.ok = ok
        self.status_code = status_code


class FakeClient:
    """只记录调用，不碰网络。"""

    def __init__(self, fail: bool = False):
        self.calls: list[tuple] = []
        self.fail = fail

    def add_hypothesis(self, project_id, statement, premise_ids):
        self.calls.append(("add", statement, tuple(premise_ids)))
        return _Resp(ok=not self.fail, status_code=500 if self.fail else 201)

    def update_hypothesis(self, project_id, hid, status, *, note="", result_fact_id=None):
        self.calls.append(("update", hid, status, note, result_fact_id))
        return _Resp(ok=not self.fail, status_code=500 if self.fail else 200)


# ── #4：假设回流 ───────────────────────────────────────────────────────────

def test_hypothesis_add_is_applied():
    client = FakeClient()
    _apply_hypothesis_actions(client, "p1", "w1", {
        "data": {"hypotheses": {"add": [
            {"statement": "若 /admin/ 返回 403 则存在路径但未授权", "premise_fact_ids": ["f001"]},
        ]}}
    })
    assert client.calls == [
        ("add", "若 /admin/ 返回 403 则存在路径但未授权", ("f001",)),
    ]


def test_hypothesis_update_is_applied():
    client = FakeClient()
    _apply_hypothesis_actions(client, "p1", "w1", {
        "data": {"hypotheses": {"update": [
            {"id": "h001", "status": "refuted", "note": "试过 .git/config 与备份文件，全部 404"},
        ]}}
    })
    assert client.calls == [
        ("update", "h001", "refuted", "试过 .git/config 与备份文件，全部 404", None),
    ]


def test_hypothesis_update_requires_id_and_status():
    client = FakeClient()
    _apply_hypothesis_actions(client, "p1", "w1", {
        "data": {"hypotheses": {"update": [{"note": "没有 id 也没有 status"}]}}
    })
    assert client.calls == []


def test_blank_statement_is_skipped():
    client = FakeClient()
    _apply_hypothesis_actions(client, "p1", "w1", {
        "data": {"hypotheses": {"add": [{"statement": "   "}]}}
    })
    assert client.calls == []


def test_malformed_block_does_not_raise():
    """best-effort：worker 回包结构不对时不能把主流程带崩。"""
    for payload in ({}, {"data": {}}, {"data": {"hypotheses": []}},
                    {"data": {"hypotheses": "nope"}}, {"data": {"hypotheses": {"add": "x"}}}):
        _apply_hypothesis_actions(FakeClient(), "p1", "w1", payload)


def test_failed_write_does_not_raise():
    _apply_hypothesis_actions(FakeClient(fail=True), "p1", "w1", {
        "data": {"hypotheses": {"add": [{"statement": "若 X 则 Y"}]}}
    })


# ── 取证：payload 键名日志 ─────────────────────────────────────────────────

def test_log_payload_keys_reports_shape(caplog):
    import logging

    from sharp.dispatcher.tasks.common import log_payload_keys

    logger = logging.getLogger("test.payload")
    with caplog.at_level(logging.DEBUG, logger="test.payload"):
        log_payload_keys(logger, "reason", {"data": {"intents": [], "hypotheses": {"add": []}}})
    assert "keys=['hypotheses', 'intents']" in caplog.text


def test_log_payload_keys_survives_junk(caplog):
    import logging

    from sharp.dispatcher.tasks.common import log_payload_keys

    logger = logging.getLogger("test.payload2")
    with caplog.at_level(logging.DEBUG, logger="test.payload2"):
        log_payload_keys(logger, "reason", {"data": {"weird": object()}})
    assert "payload" in caplog.text


# ── 提示词 ↔ 校验器一致性（真实事故的回归守卫）─────────────────────────────

def test_bootstrap_conclude_accepts_prompted_keys():
    """真实事故：提示词示例里加了 product/env_facts，校验器仍只认 {fact,complete}
    → worker 照提示词输出反而**整包被拒**，事实/基线/产品一起丢。

    实测现场：bootstrap 跑满 400s 超时后的 conclude 回包完整（含 2 条 env_facts 与
    product），却报 `unexpected keys in conclude payload` 被丢弃。
    """
    from sharp.dispatcher.contracts import validate_bootstrap_conclude_payload

    payload = {
        "accepted": True,
        "data": {
            "product": "Acme Router (AcmeOS 2.4.1)",
            "env_facts": [{"key": "network.reachable", "value": "ok"}],
            "fact": {"description": "逐项测试完成"},
            "complete": {"description": "目标已满足"},
        },
    }
    kind, description = validate_bootstrap_conclude_payload(payload)
    assert kind == "fact"
    assert description == "逐项测试完成"


def test_unexpected_key_still_rejected_and_named():
    """放宽不等于不校验：真·意外键仍要拒，而且要说清是哪个键（否则排查只能靠猜）。"""
    import pytest

    from sharp.dispatcher.contracts import validate_bootstrap_conclude_payload

    with pytest.raises(ValueError) as exc:
        validate_bootstrap_conclude_payload(
            {"accepted": True, "data": {"fact": {"description": "x"}, "whatever": 1}}
        )
    assert "whatever" in str(exc.value)


def _prompted_data_keys(template: str) -> set[str]:
    """取提示词输出示例里 `data` 对象的**直接子键**。

    不能直接 `json.loads`：示例里含 `[...]`、`...` 这类占位符，不是合法 JSON。
    所以这里做的是"扫描 data 对象的 depth==1 键名"，对占位符免疫。
    （第一版用非贪婪正则、第二版用严格解析，都因此失败过。）
    """
    keys: set[str] = set()
    idx = template.find('"data": {')
    while idx != -1:
        body_start = template.index("{", idx)
        depth = 0
        in_str = False
        escape = False
        i = body_start
        while i < len(template):
            ch = template[i]
            if in_str:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_str = False
                i += 1
                continue
            if ch == '"':
                in_str = True
                i += 1
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    break
            if depth == 1 and template[i] == ":":
                # 回看这个冒号前面的键名
                j = i - 1
                while j > 0 and template[j] in " \t\n":
                    j -= 1
                if template[j] == '"':
                    k = j - 1
                    while k > 0 and template[k] != '"':
                        k -= 1
                    keys.add(template[k + 1:j])
            i += 1
        idx = template.find('"data": {', body_start)
    return keys


# 每个键给一个"结构上合法"的占位值 —— 断言的是**键被接受**，不是值被解释
_PLACEHOLDERS: dict[str, object] = {
    "fact": {"description": "x"},
    "complete": {"description": "y"},
    "product": "Acme Router",
    "env_facts": [{"key": "network.reachable", "value": "ok"}],
    "endpoint_tests": [{"method": "GET", "path": "/x", "status": "verified"}],
    "findings": [{"title": "T"}],
    "description": "结论",
}


def test_prompted_keys_are_accepted_by_bootstrap_conclude():
    """核心不变量：**提示词要求 worker 输出的每一个键，校验器都必须接受**。

    这条不变量此前没有守护 —— 改提示词的人和改校验器的人各自都对，合起来就是
    "worker 越听话越被拒"（真实事故：示例里加了 product/env_facts，校验器只认
    {fact, complete} → 整包被拒、结论连同基线一起丢）。

    断言方式：从提示词示例里**推导出全部键**，再用这些键拼一个 payload 交给校验器。
    只列几个已知键是不够的 —— 那样新增字段时测试照样通过（这个测试的第一版就是这么弱的）。
    """
    from sharp.dispatcher.contracts import validate_bootstrap_conclude_payload
    from sharp.dispatcher.prompting import load_prompt

    keys = _prompted_data_keys(load_prompt("default", "bootstrap.md"))
    assert {"fact", "complete", "env_facts", "product", "endpoint_tests"} <= keys, \
        f"提示词示例缺字段：{sorted(keys)}"
    unknown = keys - set(_PLACEHOLDERS)
    assert not unknown, f"新增了提示词字段但测试没有占位值：{sorted(unknown)}"

    payload = {"accepted": True, "data": {k: _PLACEHOLDERS[k] for k in keys}}
    assert validate_bootstrap_conclude_payload(payload)[0] == "fact"


def test_prompted_keys_are_accepted_by_explore_validation():
    """explore 侧同样：提示词要求的键一个都不能被校验器拒掉。"""
    from sharp.dispatcher.contracts import validate_explore_payload
    from sharp.dispatcher.prompting import load_prompt

    keys = _prompted_data_keys(load_prompt("default", "explore.md"))
    assert {"description", "env_facts", "product", "endpoint_tests"} <= keys, \
        f"提示词示例缺字段：{sorted(keys)}"
    unknown = keys - set(_PLACEHOLDERS)
    assert not unknown, f"新增了提示词字段但测试没有占位值：{sorted(unknown)}"

    payload = {"accepted": True, "data": {k: _PLACEHOLDERS[k] for k in keys}}
    assert validate_explore_payload(payload)[0] == "fact"


def test_conclude_shaped_payload_reaches_baseline_and_product_helpers():
    """conclude 回包（键在 data 内）也要能被两个提取器接住。"""
    from sharp.dispatcher.tasks.explore import _extract_env_facts, _extract_product

    payload = {
        "accepted": True,
        "data": {
            "product": "Acme Router (AcmeOS 2.4.1)",
            "env_facts": [{"key": "network.reachable", "value": "ok", "note": "200"}],
            "fact": {"description": "x"},
        },
    }
    assert _extract_product(payload) == "Acme Router (AcmeOS 2.4.1)"
    assert _extract_env_facts(payload) == [
        {"key": "network.reachable", "value": "ok", "note": "200"}
    ]


# ── 结构性不变量：每条"写结论"的路径都要做全副作用 ────────────────────────

def test_every_conclusion_path_persists_all_side_effects():
    """每个写结论的任务路径都必须把副作用做全：知识提取 / 环境基线 / 产品标注。

    **为什么用源码级断言**：这不是假设出来的风险，而是一个 session 内实测出现三次的
    同一类缺陷 —— 机制只接在 explore 上，bootstrap 路径漏接：

    1. `env_facts` / `findings` / `product` 在 bootstrap conclude 被**整包拒绝**（校验器
       允许键没跟上提示词）；
    2. conclude 兜底路径只写 fact，丢掉 env_facts / product / findings；
    3. `_best_effort_extract_knowledge` **只接在 explore**，于是"整个项目在 bootstrap
       阶段就完成"的场景（实测 proj_001/003/004 三条全如此）对知识库贡献恒为 0。

    行为级测试覆盖不到"某条路径忘了接"，所以这里直接断言两条任务模块都出现这三个调用。
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "src" / "sharp" / "dispatcher" / "tasks"
    explore_src = (root / "explore.py").read_text(encoding="utf-8")
    bootstrap_src = (root / "bootstrap.py").read_text(encoding="utf-8")

    for helper in ("_best_effort_extract_knowledge", "_best_effort_upsert_baseline",
                   "_best_effort_set_product", "_best_effort_assess_endpoints"):
        assert helper in explore_src, f"explore 缺 {helper}"
        assert helper in bootstrap_src, f"bootstrap 缺 {helper}（实测漏接过三次）"


def test_bootstrap_conclude_path_extracts_knowledge():
    """conclude 兜底路径（bootstrap 超时后的那条）也要提取知识。

    实测：proj_004 的结论完全来自这条路径，包含大量"已验证不通"，但知识库里
    一条都没有 —— 因为它当时没接提取。
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "src" / "sharp" / "dispatcher" / "tasks"
    src = (root / "bootstrap.py").read_text(encoding="utf-8")
    start = src.index("def run_bootstrap_conclude_task") if "def run_bootstrap_conclude_task" in src else 0
    tail = src[start:]
    assert "_best_effort_extract_knowledge" in tail
