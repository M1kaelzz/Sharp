---
AIGC:
  ContentProducer: '001191110102MAD55U9H0F10002'
  ContentPropagator: '001191110102MAD55U9H0F10002'
  Label: '1'
  ProduceID: '8a27dcfc-1258-4b06-a47c-f5fac1f4d130'
  PropagateID: '8a27dcfc-1258-4b06-a47c-f5fac1f4d130'
  ReservedCode1: 'e9000e1d-5cd3-4c3c-98f7-9e3b34137dbd'
  ReservedCode2: 'e9000e1d-5cd3-4c3c-98f7-9e3b34137dbd'
---

# 架构决策记录（ADR）索引

> ADR（Architecture Decision Record）记录 Sharp 的重大架构决策：为什么这么选、放弃了什么、带来了什么后果。
>
> 格式参考 Michael Nygard 的 ADR 模板。每条 ADR 独立文件，按编号递增。
>
> 与 CHANGELOG 的区别：CHANGELOG 记录"改了什么"，ADR 记录"为什么这么决策"。
>
> 术语口径见 `docs/GLOSSARY.md`（证据 / 行动 / 线索 / 阶段 / 产物）。

## 一、基础结构（0001–0005）

| 编号 | 标题 | 日期 | 状态 |
|------|------|------|------|
| [0001](0001-three-role-architecture.md) | 三角色进程架构（Server / Dispatcher / Worker） | 2026-07-14 | 已接受 |
| [0002](0002-sqlite-wal.md) | 存储用 SQLite + WAL 而非 PostgreSQL | 2026-07-14 | 已接受 |
| [0003](0003-evidence-action-graph.md) | 证据—行动图作唯一协作媒介 | 2026-07-14（2026-09-09 术语修订） | 已接受 |
| [0004](0004-agent-cli-drivers.md) | 用现成 agent CLI 作 worker，不自建工具层 | 2026-07-14（2026-09-09 多 provider 修订） | 已接受 |
| [0005](0005-per-project-container.md) | 每项目独立容器隔离 | 2026-07-14 | 已接受 |

## 二、运行时与工程机制（0006–0010）

| 编号 | 标题 | 日期 | 状态 |
|------|------|------|------|
| [0006](0006-approval-env-vars.md) | 审批闸门用环境变量控制而非 dispatch.yaml | 2026-08-27 | 已接受 |
| [0007](0007-task-count-budget.md) | 用任务次数代理 API token 预算 | 2026-08-29 | 已接受 |
| [0008](0008-mcp-preflight.md) | MCP 注入前预检做故障隔离 | 2026-08-29 | 已接受 |
| [0009](0009-incremental-planning.md) | 增量规划：graph YAML 摘要化 | 2026-08-29 | 已接受 |
| [0010](0010-global-knowledge-base.md) | 跨目标知识复用全局表 | 2026-08-29 | 已接受 |

## 三、产品边界与建模（0011–0014）

| 编号 | 标题 | 日期 | 状态 |
|------|------|------|------|
| [0011](0011-approval-gate-as-boundary.md) | 审批闸门是产品边界，不是权限装饰 | 2026-09-09 | 已接受 |
| [0012](0012-asset-space-and-endpoint-ledger.md) | 资产空间与接口账本 | 2026-09-09 | 已接受 |
| [0013](0013-task-modes-and-artifact-kinds.md) | 任务模式与产物分类（评分是可选模式） | 2026-09-09（重写） | 已接受 |
| [0014](0014-action-lifecycle-and-phases.md) | 行动生命周期与阶段模型 | 2026-09-09 | 已接受 |

## 四、架构演进（0015–）

| 编号 | 标题 | 日期 | 状态 |
|------|------|------|------|
| [0015](0015-delivery-and-hypotheses.md) | 交付质量 + 工作记忆 + 假设驱动（不引入 supervisor agent） | 2026-09-09（修订） | 已接受（P0-1/P0-2 已实现，P0-3 设计中） |

> 新决策请沿用同一模板另开文件；被取代的 ADR 不删除，改状态为"已被 ADR-XXXX 取代"。
