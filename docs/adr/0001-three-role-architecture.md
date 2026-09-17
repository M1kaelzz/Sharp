---
AIGC:
  ContentProducer: '001191110102MAD55U9H0F10002'
  ContentPropagator: '001191110102MAD55U9H0F10002'
  Label: '1'
  ProduceID: 'f70b66de-28ac-4cf8-84c3-f1d03f9cb595'
  PropagateID: 'f70b66de-28ac-4cf8-84c3-f1d03f9cb595'
  ReservedCode1: '025a4384-4e6c-42f1-ab84-14184eccd19c'
  ReservedCode2: '025a4384-4e6c-42f1-ab84-14184eccd19c'
---

# ADR-0001: 三角色进程架构（Server / Dispatcher / Worker）

- 状态：已接受
- 日期：2026-07-14

## 背景

Sharp 要在单机上跑长周期授权渗透：一次任务可能持续数天、并发多个目标、中途要接受人工判断、随时可能被重启。由此得到三条硬约束：

1. **状态必须比进程活得久**——重启不能失忆（结论在图里，不在进程内存或对话上下文里，见 ADR-0003）。
2. **agent 必须被隔离**——它执行的是真实攻击工具，不能与宿主系统、其他项目的环境相互污染（ADR-0005）。
3. **人工判断必须有落点**——高危动作要能被拦在**图写入路径**上，而不是拦在界面按钮上（ADR-0011）。

此外还要满足：单机一键启动（`./sharp web`）、协议真相源唯一、调度与执行解耦。

## 决策

拆成三个角色，靠 HTTP + 共享 SQLite 协作：

1. **Server**（FastAPI）：协议真相源。维护证据 / 行动 / 线索图谱（内部表名 `facts` / `intents` / `hints`，术语见 `docs/GLOSSARY.md`）+ SQLite 持久化 + SSE 推送 + 前端托管。
2. **Dispatcher**（独立进程）：唯一的 agent-fact 写入方。单线程调度循环，管理任务生命周期、容器编排、agent 进程。
3. **Worker 容器**：每项目一个长驻容器，dispatcher 通过 `docker exec` 注入 agent 命令。Agent 之间不直接通信，只读写同一张图。

`./sharp web` 启动器同时拉起 server + dispatcher（单实例，单机各一个）。

**为什么"唯一写入方"是这个架构的关键**：如果 agent 能直连 API 写图，审批闸门就得在每个入口重复实现（且总有漏掉的入口）；把写入收敛到单线程调度器，闸门、限流、审计、恢复就都只有一处实现，且天然串行。

## 备选方案

- **单体**（server 内嵌调度）：调度循环是阻塞式 I/O，与 FastAPI 的 asyncio 事件循环冲突；且调度器崩溃会拖垮 API 服务。
- **微服务**（多 dispatcher 实例）：单机部署无必要；多 dispatcher 需要分布式锁，复杂度暴涨。

## 后果

- 优点：server 和 dispatcher 可独立重启；调度器单线程简化了并发推理（无需锁）；agent 完全隔离在容器内。
- 缺点：三角色编排由启动器管理（进程守护 + 信号传递）；dispatcher 单线程是吞吐瓶颈（单机最多 `max_workers` 个并发 agent）。

> AI生成