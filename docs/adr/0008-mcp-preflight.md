---
AIGC:
  ContentProducer: '001191110102MAD55U9H0F10002'
  ContentPropagator: '001191110102MAD55U9H0F10002'
  Label: '1'
  ProduceID: '1402c975-f938-4c2a-84db-dd4bc29e87a0'
  PropagateID: '1402c975-f938-4c2a-84db-dd4bc29e87a0'
  ReservedCode1: 'f21d696e-ec00-4881-8f55-1d3810548098'
  ReservedCode2: 'f21d696e-ec00-4881-8f55-1d3810548098'
---

# ADR-0008: MCP 注入前预检做故障隔离

- 状态：已接受
- 日期：2026-08-29

## 背景

claude-code CLI 支持 `--mcp-config` 标志注入外部 MCP 工具服务器。但 claude 启动时会对 MCP server 做**同步握手**——如果某个 server 不可达，claude 会挂起超过 60 秒不退让，导致整个任务超时失败。

## 决策

在注入 MCP 配置之前，由 Sharp-v2 自行做**预检（preflight）**：

1. 对每个 enabled HTTP MCP server，在 worker 容器内用 Node 脚本做 JSON-RPC probe（initialize + tools/list，10 秒超时）
2. 不可达的 server 不写入 MCP JSON 配置
3. 工具数超过 `max_tools` 的 server 被跳过
4. 全部 server 不可达 → 不注入 MCP（claude 正常运行无 MCP 工具）
5. stdio 型 server 不预检（其在 claude 进程内启动，故障表现为 claude 报错而非挂起）
6. 仅 claudecode driver 注入；codex / pi / mock driver 不注入

## 备选方案

- **不预检，让 claude 自己处理**：claude 不会超时跳过不可达的 server，会挂起到任务超时——不可接受。
- **全局超时包裹**：给整个 claude 进程设更短的超时来间接覆盖 MCP 挂起——但会误杀正常的长时间扫描任务。
- **只支持 stdio MCP**：stdio server 在 claude 进程内启动，不会挂起。但放弃了 HTTP MCP 生态（如远程 Burp 扫描器）。

## 后果

- 优点：不可达的 MCP server 不影响任务执行；用户可放心配置多个 MCP server 而不担心其中一个拖垮全局。
- 缺点：每次任务启动多一轮探测（约 10s/server）；Node 脚本是额外依赖（容器镜像已自带 node）。

> AI生成