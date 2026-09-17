---
AIGC:
  ContentProducer: '001191110102MAD55U9H0F10002'
  ContentPropagator: '001191110102MAD55U9H0F10002'
  Label: '1'
  ProduceID: '2d67ed4e-4986-43a2-9ec0-f9b9b0ce1436'
  PropagateID: '2d67ed4e-4986-43a2-9ec0-f9b9b0ce1436'
  ReservedCode1: '6f17c976-9ee6-4660-af60-b8c70e167363'
  ReservedCode2: '6f17c976-9ee6-4660-af60-b8c70e167363'
---

# ADR-0007: 用任务次数代理 API token 预算

- 状态：已接受
- 日期：2026-08-29

## 背景

claude-code CLI 不暴露 API token 计数。无法直接获取每次任务消耗了多少 token，但需要控制项目总消耗，防止失控项目烧光 API 额度。

## 决策

用**任务次数**（`task_count`）作为 token 消耗的代理度量。每个项目可设 `task_budget`（默认 0 = 无限制），dispatcher 每次成功提交任务后 `task_count += 1`，达到预算时停止派发新任务并写 hint 提示。

## 备选方案

- **解析 claude-code 输出提取 token 计数**：claude-code CLI 的 stdout 格式不稳定，且不同版本输出不同；解析脆弱。
- **API 层 token 计数**：需走 Anthropic API 而非 claude-code CLI，放弃 claude-code 的推理能力（见 ADR-0004）。
- **按时间预算**（限制项目运行时长）：不同目标扫描时间差异大，时间预算不反映 token 消耗。
- **不做预算**：已有项目因 agent 死循环空转烧光额度的先例。

## 后果

- 优点：实现简单（projects 表加两列）；可靠（不依赖外部 API）；用户可直觉理解"这个项目跑 50 次任务"。
- 缺点：不同任务 token 消耗差异大（bootstrap 可能 20k token，explore 可能 100k），代理不精确。但作为保护红线足够——宁可提前停也不要烧光。用户可随时调高预算继续。

> AI生成