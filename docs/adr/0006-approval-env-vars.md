---
AIGC:
  ContentProducer: '001191110102MAD55U9H0F10002'
  ContentPropagator: '001191110102MAD55U9H0F10002'
  Label: '1'
  ProduceID: '3875408b-2b61-4db2-82b9-a9344bcb881d'
  PropagateID: '3875408b-2b61-4db2-82b9-a9344bcb881d'
  ReservedCode1: 'fc59eb0a-d542-41b4-9408-112ca4db5441'
  ReservedCode2: 'fc59eb0a-d542-41b4-9408-112ca4db5441'
---

# ADR-0006: 审批闸门用环境变量控制而非 dispatch.yaml

- 状态：已接受
- 日期：2026-08-27

## 背景

高危操作审批闸门需要配置：总开关、medium 是否拦截、超时小时数。这些参数决定安全边界——如果 AI 能修改它们，就能自行绕过审批。

## 决策

审批配置由**服务端环境变量**控制，不读 `dispatch.yaml`：

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `SHARP_APPROVAL_ENABLED` | `true` | 审批闸门总开关 |
| `SHARP_APPROVAL_BLOCK_MEDIUM` | `false` | medium 是否拦截 |
| `SHARP_APPROVAL_TIMEOUT_HOURS` | `24` | 待审批超时小时数 |

理由：`dispatch.yaml` 是 dispatcher 和 worker 共享的配置文件，**worker 端可读可改**。如果审批配置放在 dispatch.yaml 里，agent（跑在 worker 容器内）理论上可以修改它来关闭审批。环境变量由 server 进程持有，worker 无法触及。

## 备选方案

- **dispatch.yaml 配置**：部署简单，但 worker 可改——不构成安全边界。
- **数据库配置表**：灵活性最高，但需额外的管理 UI 和权限控制；环境变量够用。
- **硬编码**：最安全但无法调整。

## 后果

- 优点：安全边界清晰——server 进程持有配置，worker/AI 无法触及；部署时即确定，不可被运行时篡改。
- 缺点：修改审批配置需重启 server 进程（改环境变量）；不够灵活，但对单用户工具足够。
- 补充：审批/紧急模式接口仅认 JWT，dispatcher 的 server_token 被显式拒绝——双重保障。

> AI生成