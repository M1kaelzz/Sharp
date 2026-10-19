---
AIGC:
  ContentProducer: '001191110102MAD55U9H0F10002'
  ContentPropagator: '001191110102MAD55U9H0F10002'
  Label: '1'
  ProduceID: '7c4d1e88-5f31-49b2-9a0c-3d6e8b2f5a41'
  PropagateID: '7c4d1e88-5f31-49b2-9a0c-3d6e8b2f5a41'
  ReservedCode1: '7c4d1e88-5f31-49b2-9a0c-3d6e8b2f5a41'
  ReservedCode2: '7c4d1e88-5f31-49b2-9a0c-3d6e8b2f5a41'
---

# Sharp 术语表

本文定稿 Sharp 的**对外术语**（文档、UI 文案、报告措辞）与**内部标识符**的对应关系。原则：**对外说人话、对内不改名**——代码里的表名/字段名/API 路径为兼容性冻结，术语演进只发生在文档与界面层。

## 1. 核心概念

| 对外术语 | 含义 | 内部标识符 |
| --- | --- | --- |
| **证据**（Evidence） | 已确认的事实：目标、接口、凭据、验证过的结论 | 表 `facts`、`Fact`、`/projects/{id}/facts`、id 形如 `f001` |
| **行动**（Action） | 一步探索：从若干证据出发去验证一个猜想，产出新证据 | 表 `intents`、`Intent`、`/projects/{id}/intents`、id 形如 `i001` |
| **线索**（Lead） | 人工给的方向性提示，不构成证据也不构成任务 | 表 `hints`、`Hint` |
| **阶段**（Phase） | 中层的阶段性目标，把长任务切成可结算的小段 | 表 `sub_goals`、`SubGoal`、`/projects/{id}/sub-goals`、id 形如 `sg001` |
| **产物**（Artifact/Finding） | 任务产出的结构化结果：漏洞与其他发现 | 表 `vulnerabilities`、`Vulnerability`，字段 `kind` = `vuln`/`flag`/`finding` |
| **任务模式**（Task Mode） | 项目级定位：`pentest`（默认，只谈安全发现）或 `scored`（评分类任务，才出现旗帜/记分） | `projects.task_mode`、`PUT /projects/{id}/task-mode` |
| **资产**（Asset） | 被测对象与其接口账本（按 host / AppID / 包名聚合） | `target_kind` + `asset_ref`、表 `asset_endpoints`、`/assets` |
| **审批闸门**（Approval Gate） | 高危行动执行前的人工确认（可整项目开放急模式旁路） | 表 `approval_events`、`/projects/{id}/approvals` |
| **项目**（Project） | 一次授权测试的容器：目标、图、产物、预算、截止时间 | 表 `projects`、`ProjectMeta` |

## 2. 与通用 agent 框架的措辞差异

Sharp 不使用如下措辞描述自身机制（它们描述的是"不可控环境里靠间接痕迹涌现协作"的模型，与 Sharp 刻意设计的**唯一写入方 + 人工闸门**不符）：

| 不采用 | 实际机制 |
| --- | --- |
| "黑板 / 群体智能 / 间接协作" | 结构化图存储 + 调度器作为唯一写入方；协作是**显式**的，不是涌现的 |
| "多智能体协商 / agent 互相对话" | agent 之间**不通信**，只读写同一张图；冲突由调度器的认领锁（lease）消解 |
| "prompt 链 / 工作流编排" | 调度循环按图状态决定下一步，流程是**数据驱动**而非脚本驱动 |
| "扫描任务 / 队列任务" | 行动是"证据 → 证据"的推导单元，带来源与产出，不是孤立任务条目 |

## 3. 调度与生命周期术语

| 术语 | 含义 | 内部标识符 |
| --- | --- | --- |
| **认领 / 租约**（Claim / Lease） | 行动被某 worker 取走执行，带超时；超时回收重派 | `claim` / `lease_expires_at` |
| **优先级** | 行动的调度权重（数值越大越先派发） | `intents.priority` |
| **放弃**（Abandon） | 主动终结一个行动并记录理由，不再重派 | `abandoned_at` / `abandon_reason` |
| **截止时间** | 项目级时间预算；到期后不再派发新行动（在跑的收尾） | `projects.deadline_at` |
| **急模式**（Emergency） | 项目级旁路审批闸门（仅 JWT 鉴权，记审计） | `emergency_mode` / `emergency_expires_at` |
| **验收盘点** | 完成前的实时自检：待办行动、待审批、未验证结论、未结算阶段 | `GET /projects/{id}/acceptance-check` |
| **阶段标识** | 服务端启发式判断当前处于侦察/凭据/验证/报告哪一段 | `GET /projects/{id}/phase` |

## 4. 产物语义

| kind | 含义 | 记分 | 适用范围 |
| --- | --- | --- | --- |
| `vuln` | 安全漏洞（默认，走确认/未确认与严重度流程） | 不记分 | 所有项目 |
| `finding` | 其他结构化发现（配置问题、信息泄露线索等） | 不记分 | 所有项目 |
| `flag` | 评分类任务的旗帜（CTF/评测靶场） | `score` 累计，见 `GET /projects/{id}/scoreboard` | **仅 `scored` 模式** |

> `flag` 是场景特化产物：渗透项目（默认模式）不出现记分语义，提示词也明确禁止输出 `kind`/`score`（见 ADR-0013）。

## 5. 命名一致性约定

- 文档与 UI **必须**使用本文第 1 节术语；发现旧措辞按本文修正。
- 新增 API/表/字段沿用内部命名（`fact`/`intent`/`hint`），**不要**为了对齐对外术语而改标识符——迁移成本与兼容风险不成比例。
- 前端文案中"事实"若出现在图视图之外（如提示语、空态），替换为"证据"；"意图"同理替换为"行动"。
- **不使用"图谱"这一泛指词**：需要名词时用「图」或「证据图」，完整语境用「证据—行动图（Evidence-Action Graph）」。代码标识符（`app.graph.js`、`graphSearch`、`export_project`）不变。
- **"线索"专指 Hint**，不得用来指证据——历史上 facts 曾被同时叫作"事实"和"线索"，两者已统一为「证据」；intents 曾叫"步骤"，已统一为「行动」。
- 该约定覆盖**界面文案、后端错误提示、注入 AI 的系统说明与上下文**：这些文本用户与 AI 都会读到，措辞不一致会让 AI 用旧术语回答用户。
