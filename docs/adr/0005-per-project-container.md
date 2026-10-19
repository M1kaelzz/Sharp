---
AIGC:
  ContentProducer: '001191110102MAD55U9H0F10002'
  ContentPropagator: '001191110102MAD55U9H0F10002'
  Label: '1'
  ProduceID: '5a3dcae9-a2b8-4547-9283-3660350d99e6'
  PropagateID: '5a3dcae9-a2b8-4547-9283-3660350d99e6'
  ReservedCode1: 'b48d91f5-5bfa-4cb1-b6a9-8dab801654bf'
  ReservedCode2: 'b48d91f5-5bfa-4cb1-b6a9-8dab801654bf'
---

# ADR-0005: 每项目独立容器隔离

- 状态：已接受
- 日期：2026-07-14

## 背景

多个渗透测试项目并行运行，目标不同、工具不同、环境不同（有的需要 APK 注入、有的需要特定 Python 包）。需要执行隔离 + 环境定制 + 资源限制。

## 决策

每个项目一个长驻容器（`sleep infinity`），dispatcher 通过 `docker exec` 注入 agent 命令。

- 容器在项目首次调度时创建，项目删除时连带清理。
- 每个容器有自己的文件系统、网络命名空间、进程空间。
- APK 注入：项目带 `apk_source` fact 时，把 APK 字节注入容器（幂等，已在容器就跳过）。
- 容器网络默认 `bridge`（跨平台一致），可访问互联网目标；宿主服务用 `host.docker.internal`。
- 容器 exec 用 `setsid -w` 独立会话，`kill()` 用 `pkill -KILL -s <sid>` 收割整棵进程树（KILL-1）。

## 备选方案

- **共享容器多项目**：隔离不足——一个项目的 `apt install` 污染另一个项目；进程冲突；文件覆盖。
- **每次任务新建容器**：容器启动开销 3-5s，频繁创建/销毁延迟高；且会话记忆（claude session）无法跨容器续接。
- **无容器直接跑**：安全隔离为零，agent 可能影响宿主系统。

## 后果

- 优点：完全隔离（文件系统/网络/进程）；APK/工具按项目定制；容器可热替换（删了重建，图数据不丢——状态在图里，不在容器里）。
- 缺点：资源占用线性增长（每容器约 200-500MB 内存）；单机并发受 `max_workers` 限制；Docker 是硬依赖。

> AI生成