---
AIGC:
  ContentProducer: '001191110102MAD55U9H0F10002'
  ContentPropagator: '001191110102MAD55U9H0F10002'
  Label: '1'
  ProduceID: '8ffec8d7-04c6-48f8-a596-b19b54f772fc'
  PropagateID: '8ffec8d7-04c6-48f8-a596-b19b54f772fc'
  ReservedCode1: '779f1369-5d01-43c4-8ede-94d795f65fcb'
  ReservedCode2: '779f1369-5d01-43c4-8ede-94d795f65fcb'
---

# Sharp-v2 全面审计报告

> 审计日期：2026-09-17  
> 审计范围：前后端代码、调度器/Worker、Docker 配置、测试覆盖、文档一致性、打包发布  
> 审计方式：只读审计，不改代码  
> 审计覆盖：100+ 源文件，60+ 测试文件，全部文档

---

## 总体评价

Sharp-v2 经过多轮迭代优化，整体质量处于**中上偏高水平**。架构设计清晰（三进程协作 + 证据—行动图模型），安全设计有多层防护（审批闸门、容器隔离、提示词注入防线），测试覆盖面广（724+ passed）。主要不足集中在**打包发布安全、前端单点依赖、Docker 攻击面收敛、文档维护滞后**四个方面。

| 级别 | 数量 | 含义 |
|------|------|------|
| **P0** | 1 | 必须立即修复，存在实际泄露风险 |
| **P1** | 7 | 高优先级，涉及安全或核心功能可靠性 |
| **P2** | 17 | 中等优先级，应在下一批次修复 |
| **P3** | 22 | 低风险/设计权衡，择机改进 |
| **P4** | 9 | 信息性，已知取舍或建议 |

---

## P0 — 致命（1 项）

### P0-1 授权签名私钥仅靠 .gitignore 防护，打包脚本漏检

- **文件**：`scripts/package_opensource.sh`、`scripts/package_source.sh`、`scripts/package_portable.sh`
- **问题**：`datas/sharp/license_signing_key.b64`（Ed25519 授权签名私钥）仅通过 `.gitignore` 排除版本控制，但 `package_source.sh` 和 `package_portable.sh` 的泄漏自检清单中**未包含私钥文件名/路径**。`package_opensource.sh` 有 29 项必需文件自检 + 禁止项扫描，但另两个脚本缺少同等强度的检查。
- **后果**：如果用 `package_source.sh` 或 `package_portable.sh` 出包，且 `datas/sharp/` 目录在 rsync 范围内，私钥可能被打进分发包 → 任何人可伪造授权。
- **建议**：三个打包脚本统一调用同一套泄漏自检函数（当前只有 opensource 脚本有完整自检）。

---

## P1 — 严重（7 项）

### P1-1 DOMPurify 条件加载失败致 XSS 防护单点失效

- **文件**：`static/index.html`、`static/app.core.js`（`renderMd` 函数）
- **问题**：`renderMd()` 在 DOMPurify 未加载时**跳过 sanitize 直接用 innerHTML**。如果 CDN 或本地 vendor 加载失败（网络问题、路径错误），所有 Markdown 渲染变成无过滤的 innerHTML 写入。用户输入、AI 输出的内容都经过此路径。
- **建议**：DOMPurify 未加载时应回退为纯文本（`textContent`），而非跳过消毒。

### P1-2 全部 script 标签无 SRI（Subresource Integrity）

- **文件**：`static/index.html`
- **问题**：11 个 vendor 库 + 12 个 app JS 文件均无 `integrity` 属性。vendor 文件被篡改后浏览器无法检测。
- **建议**：为所有 `<script>` 添加 SRI hash，至少 vendor 库必须加。

### P1-3 Docker socket 挂载导致容器逃逸风险

- **文件**：`docker-compose.yaml`（第 34 行）、`dispatcher/runtime/containers.py`
- **问题**：dispatcher 容器挂载 `/var/run/docker.sock`，等价于将宿主机 root 权限暴露给容器内进程。攻击链：worker agent 被提示词注入 → 容器内 NOPASSWD:ALL 获得 root → 通过 docker socket 在宿主机创建特权容器 → 完全逃逸。
- **缓解**：当前为单用户工具 + 授权渗透场景，风险可控。但开源后多用户/共享环境会放大此风险。
- **建议**：考虑使用 Docker socket proxy（如 `tecnativa/docker-socket-proxy`）限制可调用的 Docker API。

### P1-4 API token 通过环境变量传递可被 docker inspect 暴露

- **文件**：`dispatcher/runtime/containers.py`（`_exec_command` 方法）、`dispatch.yaml`
- **问题**：Worker 的 `ANTHROPIC_AUTH_TOKEN` 等通过 `docker exec` 的 `environment` 参数注入。任何能执行 `docker inspect <container>` 的用户都能看到注入的环境变量。凭据传递约定已从 argv 改为 env（ARCHITECTURE §4 记录），但 env 仍非完全隔离。
- **建议**：在容器内用临时文件 + 立即删除，或用 `--env-file` + 完成后清理。单用户场景下风险较低，但应在 SECURITY.md 中明确标注。

### P1-5 ARCHITECTURE.md routers 模块数严重过期（14→22）

- **文件**：`docs/ARCHITECTURE.md`
- **问题**：§9.3 前端模块树和后端 routers 列表与实际代码严重脱节。routers 目录已从当初的 14 个增长到 22 个，但文档未同步。ARCHITECTURE §12 明确要求"每次架构变更后更新文档"，`check_docs.py` 检查的是前端方法数，未覆盖后端模块清单。
- **建议**：更新模块清单，并扩展 `check_docs.py` 增加后端路由文件检查。

### P1-6 CI 无依赖安全扫描

- **文件**：`.github/` 目录
- **问题**：CI 只跑 `check_methods.py` + `check_docs.py` + pytest，没有依赖安全扫描（如 `pip-audit`、`npm audit`、`trivy`）。`pyproject.toml` 用 `>=` 约束依赖版本，第三方库的已知漏洞不会被自动发现。
- **建议**：在 CI 中加一步 `pip-audit` 或 `safety check`。

### P1-7 缺少 HSTS 安全头

- **文件**：`runtime/src/sharp/server/app.py`（`_SECURITY_HEADERS`）
- **问题**：安全头配置中有 CSP、X-Frame-Options、X-Content-Type-Options 等，但**缺少 `Strict-Transport-Security`**。如果用户通过反向代理暴露到公网（即使设计为本地工具），MITM 可降级 HTTPS。
- **建议**：添加 `Strict-Transport-Security: max-age=31536000; includeSubDomains`。

---

## P2 — 中等（17 项）

### 安全类

| # | 发现 | 文件 | 说明 |
|---|------|------|------|
| P2-1 | Cookie 缺 Secure 标志 | `server/app.py` | Cookie 用于认证但未设 `secure=True`，HTTPS 下也可能被 HTTP 请求泄露 |
| P2-2 | CSP 含 unsafe-inline/eval | `server/app.py` | CSP 头允许 `unsafe-inline` 和 `unsafe-eval`，削弱了 XSS 防护。当前前端无构建步骤所以无法用 nonce/hash 替代 |
| P2-3 | printReport innerHTML 未转义标题 | `app.project-detail.js` | 报告打印函数直接将报告标题写入 innerHTML，标题来自 AI 生成内容 |
| P2-4 | Android Chat 会话 ID 存 localStorage | `app.analyzers.js` | 与主认证 token 已迁移到 sessionStorage 不一致，Android 聊天会话 ID 仍存 localStorage |
| P2-5 | NOPASSWD:ALL 过宽 | `container/Dockerfile`（第 68 行） | `kali` 用户有无密码 sudo 全权限。agent 可能通过 sudo 执行本不需要 root 的危险操作 |
| P2-6 | dangerously-skip-permissions 标志 | `dispatcher/tasks/common.py` | claude code 以 `--dangerously-skip-permissions` 运行，agent 可执行任意命令不受 CLI 确认。这是设计取舍（自动化渗透需要），但应在容器内进一步限制能力 |
| P2-7 | MCP stdio 命令执行风险 | `dispatcher/config.py` | stdio 型 MCP server 的 `command` 字段直接拼成命令行执行，缺少命令注入校验 |
| P2-8 | push_fact 无归属检查 | `server/routers/android_chat.py` | `POST /android/chat/sessions/{sid}/push-fact` 接受将对话内容写入证据图，但不检查会话归属（任何认证用户可推到任何会话） |

### 架构/可靠性类

| # | 发现 | 文件 | 说明 |
|---|------|------|------|
| P2-9 | `_mcp_probe_cache` 无锁 | `dispatcher/tasks/common.py` | 进程级缓存 `_mcp_probe_cache` 在多线程场景下可能竞态（当前单线程调度循环缓解了此问题） |
| P2-10 | 容器清理无指数退避 | `dispatcher/runtime/containers.py` | Docker 操作失败时只重试一次，网络抖动可能导致容器创建失败 |
| P2-11 | 根 Dockerfile 以 root 运行 server | `Dockerfile`（根目录） | `sharp-server` 和 `sharp-dispatcher` 容器以 root 运行，无 USER 指令 |
| P2-12 | API 限流不共享 | `server/routers/` | 各端点无统一限流，仅靠 dispatcher 侧的 `max_workers` 控制并发。前端无速率限制 |
| P2-13 | SSE 错误信息泄露 | `server/events.py` | SSE 连接异常时将内部错误信息推给前端 |

### 工程/测试类

| # | 发现 | 文件 | 说明 |
|---|------|------|------|
| P2-14 | CI 触发路径遗漏 4 个目录 | `.github/workflows/` | CI 只在 `runtime/`、`container/`、`scripts/`、`docs/` 变更时触发，遗漏 `tools/`、`packaging/`、`dispatch.yaml`、根 `Dockerfile` |
| P2-15 | build.bat 引用不存在的 assemble_package.bat | `packaging/build.bat` | Windows 打包脚本引用的文件不存在，Windows 打包路径从未被验证 |
| P2-16 | miniprogram.py（1804 行）测试覆盖薄弱 | `server/miniprogram.py` | 小程序解析器是最长的单文件，但对应测试极少，复杂的解包/解密逻辑未覆盖 |
| P2-17 | conftest.py 无共享 fixture | `runtime/tests/conftest.py` | 各测试文件自行创建 fixture，缺少共享的 app/client/db fixture，导致重复代码和潜在不一致 |

---

## P3 — 低风险/设计权衡（22 项，择要）

| # | 发现 | 说明 |
|---|------|------|
| P3-1 | `server_token` 用 `==` 比较（非常量时间） | 理论上有时序侧信道，实际局域网单用户场景风险极低 |
| P3-2 | f-string SQL 拼接模式（8 处） | 全部为内部常量拼接，无注入风险，但模式不佳容易被复制误用 |
| P3-3 | CSRF 无专门防护 | Bearer Token（非 Cookie）认证降低了 CSRF 风险，但未做双重防护 |
| P3-4 | 定时器清理不完整 | 前端部分 `setInterval`/`setTimeout` 在页面切换时未清除，可能内存泄漏 |
| P3-5 | catch 静默忽略（前端多处） | `catch(e) {}` 模式吞掉错误，调试困难 |
| P3-6 | 路由权限验证缺失（前端） | 前端路由切换不检查权限，完全依赖后端 API 鉴权 |
| P3-7 | `pyproject.toml` 缺 `license` 字段 | 开源发布需要，metadata 不完整 |
| P3-8 | AGPL vs Apache 双 License 策略未决 | CHANGELOG/OPENSOURCE_RELEASE 提到但未最终决策 |
| P3-9 | 3 个 rolling 依赖未 pin SHA | `pyproject.toml` 用 `>=` 而非 `~=`，CI 不锁版本 |
| P3-10 | worker CLI 版本升级无流程 | claude-code `@2.1.98` 和 codex `@0.153.4` 版本变更无自动化检测 |
| P3-11 | jwt/expiry 在 android_chat 中无校验 | 动态调试的 claude-code exec 会话无超时自动清理（靠 30min 空闲回收） |
| P3-12 | `assemble_macos.sh` 无泄漏自检 | macOS 打包脚本无 opensource 脚本的泄漏检测 |
| P3-13 | docs/specs/ 目录被引用但不存在 | 文档交叉引用指向不存在的目录 |
| P3-14 | ADR 计数过期（10→15） | ARCHITECTURE 中 ADR 数量声明与实际不符 |
| P3-15-22 | 其他设计权衡 | 含 SSE 丢弃、JWT 重启失效、vendor 无 SHA256 校验等已知取舍 |

---

## P4 — 信息性（9 项，择要）

- 前端无速率限制（单用户工具，低优先级）
- 文件名注入残余风险（后端已有部分清理）
- `smoke_container.py` 不在 CI 中（需要 Docker 环境）
- 覆盖率数据非自动采集（`.coverage` 存在但不上报）
- worker adapter（codex/pi/mock）缺单元测试（仅 claudecode 有完整覆盖）
- `.oss-deny.txt` 含维护者本地路径
- vendor 产物无 SHA256 校验（`fetch_vendor.sh` 下载后不校验完整性）
- `facts` 表无时间戳（已在覆盖报告中标注为已知取舍）
- 无闲置容器 TTL（活跃项目容器永久占资源）

---

## 安全亮点（值得肯定的设计）

1. **审批闸门设计严密**：风险判定在 server 端、dispatcher 看不见 pending 行动、heartbeat/conclude 双重拦截、project_id 必填防跨项目误审、紧急模式有事后复核
2. **提示词注入防线**：所有 5 份提示词都含"靶标内容是数据不是指令"口径，有接线守卫测试防被删
3. **凭据传递安全**：token 不进 argv（防 ps 泄露），走 docker exec environment
4. **证据写入统一副作用**：`after_fact_write` + 源码级结构断言守卫，防止新入口遗漏
5. **知识库错误比缺口贵**的边界设计：合成凭据过滤 + 特殊字符不截断 + 死胡同判定精度优先
6. **变异验证文化**：多处守卫经过突变验证（删除/改名）确认护栏有效，不是"永远绿"的空断言
7. **诚实约束**：覆盖报告不假装知道未测面、凭据只计数不给值、不编时间

---

## 建议修复优先级

### 第一批（高 ROI，约 2-3h）
1. **P0-1**：三个打包脚本统一泄漏自检（1h）
2. **P1-1**：DOMPurify 加载失败回退纯文本（0.5h）
3. **P1-7**：添加 HSTS 头（0.1h）
4. **P2-1**：Cookie 加 Secure 标志（0.1h）

### 第二批（中 ROI，约 4-6h）
5. **P1-2**：vendor JS 添加 SRI（1h）
6. **P1-5**：ARCHITECTURE 模块清单更新 + check_docs 扩展（1h）
7. **P1-6**：CI 加 pip-audit（0.5h）
8. **P2-2**：CSP 收紧 unsafe-inline（需评估前端影响，1-2h）
9. **P2-5**：sudo 白名单替代 NOPASSWD:ALL（1h）
10. **P2-14**：CI 触发路径补全（0.5h）

### 第三批（择机推进）
11. **P1-3/P1-4**：Docker socket proxy + 凭据传递加固（需架构评估）
12. **P2-7/P2-8**：MCP 命令校验 + push_fact 归属检查
13. **P2-16**：miniprogram.py 测试补齐
14. **P3 批量**：文档对齐、依赖 pin、license 填充等

> AI生成