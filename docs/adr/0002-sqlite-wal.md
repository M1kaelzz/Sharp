---
AIGC:
  ContentProducer: '001191110102MAD55U9H0F10002'
  ContentPropagator: '001191110102MAD55U9H0F10002'
  Label: '1'
  ProduceID: 'ea51100c-786d-4112-af6f-b3df46d18b0d'
  PropagateID: 'ea51100c-786d-4112-af6f-b3df46d18b0d'
  ReservedCode1: 'cee8f506-aff5-489a-bb39-7e4a217db768'
  ReservedCode2: 'cee8f506-aff5-489a-bb39-7e4a217db768'
---

# ADR-0002: 存储用 SQLite + WAL 而非 PostgreSQL

- 状态：已接受
- 日期：2026-07-14

## 背景

数据量：facts / intents / hints / vulnerabilities / approval_events（千~万行级），单机部署为主，需零运维成本。

## 决策

SQLite，WAL 模式 + `foreign_keys=ON` + `busy_timeout=5000`。每请求一个连接。schema 由 `db.py` 的 `SCHEMA` 字符串 + `_migrate()` 幂等管理（`CREATE TABLE IF NOT EXISTS` + 列探测 `ALTER TABLE ADD COLUMN`）。

## 备选方案

- **PostgreSQL**：运维成本高（需安装、配置、备份），单机部署无必要；但多租户/高并发写场景需迁移。
- **内存/JSON 文件**：无事务/索引/外键，无法支撑 SSE 并发读写和 intent 认领的 CAS 事务。
- **其他嵌入式 DB**（如 DuckDB）：生态不如 SQLite 成熟，Python 标准库自带 `sqlite3` 零依赖。

## 后果

- 优点：零依赖（Python 自带）、单文件备份（拷贝 `sharp.db` 即可）、跨平台。
- 缺点：并发写受限（单写者）。已用 WAL + `busy_timeout=5000` 缓解——并发写撞锁时等最多 5s 而非立刻 `SQLITE_BUSY`。未来多租户/集群需迁移，迁移路径：repository 层 SQL 已集中，替换 driver 即可。

> AI生成