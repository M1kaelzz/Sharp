---
AIGC:
  ContentProducer: '001191110102MAD55U9H0F10002'
  ContentPropagator: '001191110102MAD55U9H0F10002'
  Label: '1'
  ProduceID: '449769d9-fbca-442a-985d-2472c661158c'
  PropagateID: '449769d9-fbca-442a-985d-2472c661158c'
  ReservedCode1: '69871540-2d2c-46f4-a6c5-adcf680325f2'
  ReservedCode2: '69871540-2d2c-46f4-a6c5-adcf680325f2'
---

# ADR-0010: 跨目标知识复用全局表

- 状态：已接受
- 日期：2026-08-29

## 背景

渗透测试常对同一目标的不同子域多次建项目。每次新项目从零开始，之前的发现（.credentials、hidden endpoints、技术栈指纹）无法复用，agent 重复劳动。

## 决策

建一张 `knowledge_base` 全局表（不绑定 project_id），以 `root_domain` 为聚合键，存三类知识：credential / endpoint / fingerprint。

- **提取**：explore 写 fact 后自动从描述中用正则提取，best-effort 上传到 knowledge_base
- **注入**：创建项目时解析 origin 提取 root_domain，查 knowledge_base 匹配条目注入为 hints
- **幂等**：`(root_domain, k_type, content)` 三元组唯一约束，重复提取不产生重复记录

## 备选方案

- **按项目存知识，跨项目查询时 JOIN**：查询复杂；且项目可能被删除，知识随之丢失。
- **人工手动管理知识**：用户需记得上次的发现并手动填 hint——违背自动化目标。
- **不做知识复用**：agent 每次从零探索，浪费 token 和时间。

## 后果

- 优点：新项目自动获得同域名历史知识，减少重复侦察；知识跨项目积累，越用越聪明。
- 缺点：正则提取有噪声（误匹配），需要后续加正则质量检验；全局表无权限隔离（单用户工具可接受）；origin 无域名时（APK/小程序项目）无法注入——优雅跳过。

> AI生成