"""写 fact 之后的统一副作用（P2-D）。

**为什么需要这个模块**：这个项目反复出现同一类缺陷 —— **某个能力只接在一条路径上**。
`facts` 目前有五个写入入口：

| 入口 | 位置 |
|---|---|
| 行动结论（主链） | `routers/intents.py::conclude_intent` |
| chat「写入证据图」 | `routers/android_chat.py` |
| 重开项目的外部反馈 | `routers/projects.py::reopen` |
| Android 分析事实 | `routers/android.py` |
| 小程序分析事实 | `routers/miniprogram.py` |

而副作用只有主链做了**一半**（登记接口台账），知识提取更是只由 dispatcher 在
explore/bootstrap 之后单独触发。结果是：**从 chat 或分析器写进去的证据，既不进接口台账，
也不产出任何跨项目知识** —— 明明是同一条证据，走哪个入口决定了它有没有沉淀价值。

所以这里把"写完 fact 该做的事"收口成一个函数，并由
`tests/test_fact_hooks.py` 做**源码级结构断言**：凡是往 facts 里写的模块，都必须调用它。
行为级测试覆盖不到"某个入口忘了调"，这正是它此前能长期存在的问题。
"""

from __future__ import annotations

import logging
import sqlite3

LOG = logging.getLogger("sharp.server.fact_hooks")

# 结构事实：origin 是目标、goal 是验收标准，二者都是**输入**而不是**产出**。
# 它们不参与沉淀 —— 早先实测过一次教训：goal 文本里出现"无效发现"这类词，
# 被当成"已验证不通"的结论。这里显式排除，避免任何入口把输入当产出。
STRUCTURAL_FACT_IDS = frozenset({"origin", "goal"})


def after_fact_write(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    fact_id: str,
    description: str,
    now: str,
) -> dict[str, int]:
    """写 fact 之后的统一副作用：登记接口台账 + 抽取跨项目知识。

    - 结构事实（origin/goal）直接跳过
    - **best-effort**：任何一步失败只记日志，绝不让"沉淀"反过来把"写证据"这个主操作搞挂
      （调用方通常在事务里，异常会连事实一起回滚 —— 那是最坏的结果）
    """
    if fact_id in STRUCTURAL_FACT_IDS or not (description or "").strip():
        return {"endpoints": 0, "knowledge": 0}

    endpoints = 0
    try:
        from sharp.server.asset_endpoints import register_endpoints_from_fact

        endpoints = register_endpoints_from_fact(
            conn, project_id=project_id, fact_id=fact_id, description=description, now=now,
        )
    except Exception as exc:  # noqa: BLE001 - 沉淀失败不该影响写证据
        LOG.warning("endpoint registration failed project=%s fact=%s error=%s",
                    project_id, fact_id, exc)

    knowledge = 0
    try:
        from sharp.server.routers.knowledge import extract_knowledge_for_fact

        result = extract_knowledge_for_fact(
            conn, project_id=project_id, description=description, now=now
        )
        knowledge = int((result or {}).get("extracted_count") or 0)
    except Exception as exc:  # noqa: BLE001
        LOG.warning("knowledge extraction failed project=%s fact=%s error=%s",
                    project_id, fact_id, exc)

    return {"endpoints": endpoints, "knowledge": knowledge}
