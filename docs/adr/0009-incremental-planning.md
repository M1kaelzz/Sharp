---
AIGC:
  ContentProducer: '001191110102MAD55U9H0F10002'
  ContentPropagator: '001191110102MAD55U9H0F10002'
  Label: '1'
  ProduceID: 'f351aa72-3db8-4f5b-9e4c-a9cdf9e44e24'
  PropagateID: 'f351aa72-3db8-4f5b-9e4c-a9cdf9e44e24'
  ReservedCode1: '7586593e-9f21-4fe0-9645-e1c0ce7c9264'
  ReservedCode2: '7586593e-9f21-4fe0-9645-e1c0ce7c9264'
---

# ADR-0009: 增量规划——graph YAML 摘要化

- 状态：已接受
- 日期：2026-08-29

## 背景

随着项目推进，fact 数量增长，graph YAML 快照越来越大。每次 reason/explore 都把完整 graph 传给 LLM，导致 prompt 膨胀、token 浪费、响应变慢。一个跑了几十轮的项目 graph 可能超过 50k token。

## 决策

在 `write_graph_snapshot` 之前对 YAML 做摘要化（`summarize_graph_yaml()`）：

- **阈值**：非特殊 fact 数超过 20 时触发
- **保留**：最近 8 条 fact 全文 + origin/goal 始终全文
- **截断**：更早的 fact 描述截断到 300 字符并标 `[truncated]`
- **回退**：YAML 无法解析或 fact 数不足时原样返回
- **prompt 层配合**：提示 agent 引用截断 fact 时用 ID（如 `f003`），而非全文复述

## 备选方案

- **不做摘要，全量传递**：token 线性增长，50 轮后 prompt 超 50k token，成本和延迟不可接受。
- **只传增量 fact（只发新 facts）**：LLM 丢失全局视野，无法判断哪些 intent 已覆盖、哪些路径已走过。dhunter 用 `known_fact_ids` 做增量传递，但也发 graph summary（限 40 条）做全局参考。
- **向量检索按相关性选 fact**：引入 embedding 模型依赖，增加复杂度；且渗透测试的 fact 关联是因果链而非语义相似，向量检索不一定有效。
- **删旧 fact**：破坏审计完整性；回放和报告依赖完整 fact 链。

## 后果

- 优点：prompt token 从 O(N) 降到 O(1)（稳态约 8 条全文 + origin/goal）；agent 仍能引用旧 fact 的 ID 和摘要判断相关性。
- 缺点：截断的 fact 丢失细节——如果 agent 需要旧 fact 的完整描述，需通过 hint 机制请求。这是可接受的 tradeoff：大部分探索只依赖最近的 fact 链。

> AI生成