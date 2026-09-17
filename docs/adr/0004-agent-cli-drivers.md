---
AIGC:
  ContentProducer: '001191110102MAD55U9H0F10002'
  ContentPropagator: '001191110102MAD55U9H0F10002'
  Label: '1'
  ProduceID: 'e57c9a13-48d2-4b60-9f31-7a4e0c8b6215'
  PropagateID: 'e57c9a13-48d2-4b60-9f31-7a4e0c8b6215'
  ReservedCode1: 'e57c9a13-48d2-4b60-9f31-7a4e0c8b6215'
  ReservedCode2: 'e57c9a13-48d2-4b60-9f31-7a4e0c8b6215'
---

# ADR-0004: 用现成 agent CLI 作 worker，不自建工具层

- 状态：已接受
- 日期：2026-07-14
- 修订：2026-09-09（多 provider：支持 claude-code / codex / pi 三类 driver，由 `SHARP_WORKER_MODE` 在派发前选择）

## 背景

需要一个能自主推理、调用安全工具、执行多步渗透测试的 agent。两条路线：用现成的 coding agent CLI（claude-code 等），还是自建工具注册层 + 对接 LLM API。

关键约束来自架构本身：dispatcher 已经是**唯一图写入方**（ADR-0001/0003），工具调用的隔离与并发由容器与调度循环承担；那么 worker 侧真正需要的只有两件事——**强推理 + 能自己敲命令**。

## 决策

以**现成 agent CLI 作为 worker driver**，核心是 `WorkerDriver` 抽象层，而非绑定单一厂商：

1. **driver 抽象**：`WorkerDriver`（ABC）定义"启动一次行动、注入提示、解析输出"的最小契约；`claudecode` / `codex` / `pi` / `mock` 都是其实现。新增 provider = 新增一个 driver，不动调度与图逻辑。
2. **工具即容器内的二进制**：nuclei / ffuf / sqlmap / jadx 等通过 agent 内置的 Bash 能力直接调用；加工具 = 往镜像里塞二进制/脚本 + 在 `AGENTS.md` 工具目录写一条，不需要改代码。
3. **provider 选择在派发前完成**：`SHARP_WORKER_MODE`（如 `anthropic` / `openai` / `inflection`）在读取 `dispatch.yaml` 时按 provider 过滤 worker 定义，未匹配的 worker 不进调度池；因此"这个项目用哪个模型跑"是部署配置，而非运行时代码分支。
4. **凭据按 provider 注入**：每个 provider 的 API key/token 从 secrets 注入容器环境，健康检查用 `printenv`（POSIX）而非 bash 专有语法，保证镜像内 shell 兼容。
5. **不用 MCP 作为主要工具通道**：可选注入用于预检与故障隔离（ADR-0008），但核心能力仍走 Bash，避免工具注册层成为单点。

## 备选方案

- **自建 tool layer + LLM API**：工具入口可硬性拦截（限速、403 冷却、危险操作阻断），安全性更高。但需自维护 tool schema、function calling 对接、流式解析，且每接一个新模型都要重做适配——工程量大且与"多 provider"目标冲突。
- **绑定单一 CLI（claude-code only）**：实现最省，但把可用性押在一家厂商的额度与可用性上；实测中 provider 侧的额度问题会直接让整个项目停摆。
- **LangChain / CrewAI 等 agent 框架**：抽象层太厚，渗透测试需要精细控制超时、进程管理、输出解析。

## 后果

- 优点：零工程量获得成熟 agent 的强推理与自主工具调用；`AGENTS.md` 即"工具说明书"；session 续接支撑 conclude 兜底。
- 优点：多 provider 是配置而非改造；换模型/换额度不必改调度与图逻辑，评测类任务可以在不同模型间对照。
- 缺点：**无法在工具入口硬性拦截**——请求限速、WAF 规避、布尔 oracle 确认只能走提示词软约束（模型通常遵守但非 100% 强制）；真正的硬边界放在图写入路径上的审批闸门（ADR-0011）。
- 缺点：worker 镜像需要内置多个 CLI 及其运行时（体积与构建时间上升）；provider 差异（参数、输出格式、会话续接方式）由各 driver 各自适配，driver 层的测试成本随 provider 数量线性增长。
