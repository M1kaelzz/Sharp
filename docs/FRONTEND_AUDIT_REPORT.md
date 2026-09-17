---
AIGC:
  ContentProducer: '001191110102MAD55U9H0F10002'
  ContentPropagator: '001191110102MAD55U9H0F10002'
  Label: '1'
  ProduceID: '9d85d7fe-c54a-4b91-9073-9a0a1e0a7e6c'
  PropagateID: '9d85d7fe-c54a-4b91-9073-9a0a1e0a7e6c'
  ReservedCode1: '7aa6f602-3ab3-4463-aff0-27ad0e84c3be'
  ReservedCode2: '7aa6f602-3ab3-4463-aff0-27ad0e84c3be'
---

# Sharp-v2 前端代码安全与质量审计报告

- **审计对象**：`runtime/src/sharp/server/static/`（10 个 JS 文件 + index.html，共 7865 行）
- **审计时间**：2026-08-26
- **审计范围**：代码质量与可维护性、模块重复/循环依赖/职责不清、mixin 组装模式隐患、错误处理、XSS 等前端安全风险、性能问题、与后端 API 交互的健壮性
- **说明**：本次为只读研究分析，未修改任何文件

---

## 复核状态（2026-09-16，开源发布前）

本文是**自审**记录，因此公开前逐条复核了高危项。本次只复核了下面两条（其余条目未逐条复验，
阅读时请以代码现状为准）：

| 条目 | 复核结论 | 证据 |
|---|---|---|
| §1.1 `renderMd()` 存储型 XSS | **已修** | `static/app.core.js` 的 `renderMd()` 现对文本做 `& < >` 转义（`esc()`），再处理代码块占位符 |
| §1.2 `authToken` 明文存于 `localStorage` | **已修** | token 改存 `sessionStorage`（`sharpStoreToken`），且主动 `localStorage.removeItem()` 清理历史遗留 |

> 复核方式：只读代码核对（`grep` 定位到上述实现）。若你依赖本文的其余结论做安全判断，
> 请先在当前代码上复验——这是一份会过期的报告，不是当前状态的断言。

---

## 一、发现清单

### 1.1【高】`renderMd()` 存在存储型 XSS 漏洞

- **位置**：`app.core.js:174-211`，被 `index.html` 的 `x-html="renderMd(...)"` 在 7 处渲染（`index.html:1662/1699/1758/2208/2218/2880/3421`）
- **问题描述**：
  1. 第 176 行 `esc` 仅转义 `& < >`，不含引号，且**只对整体文本转义一次**；
  2. 第 183 行整体转义后，第 189-201 行的正则替换会把 `**<img src=x onerror=alert(1)>**` 这类内容还原为 `<strong><img src=x onerror=alert(1)></strong>` —— 捕获组内的文本**未再次转义**，原始转义被“覆盖”，形成可利用的 HTML 注入；
  3. 该函数渲染的是 AI 生成的事实描述、报告、聊天消息等**不可信外部内容**，攻击者只需让 AI/报告包含恶意 Markdown 即可在任意用户浏览器执行脚本 → **存储型 XSS**，配合 localStorage 明文 token（见 1.2）可完全接管会话。
- **修复建议**：改用正规 Markdown 渲染库（如 marked + DOMPurify，或至少对替换捕获组再次调用 `esc()`）；同时将原始文本中的 `<`/`>` 在**任何正则替换前**先占位化（如 `\x01LT\x01`）再还原，避免二次注入。注意第 179-185 行代码块保护逻辑同样存在该问题。

### 1.2【高】`authToken` 明文存储于 localStorage

- **位置**：`app.js:200`、`app.core.js:121/161`、`app.core.js:81`
- **问题描述**：认证 token 明文写入 `localStorage`。任何 XSS（含 1.1 的漏洞）或同一浏览器内其他页面脚本均可窃取 token，实现账户接管。Sharp 为安全测试工具，token 即系统全部权限。
- **修复建议**：优先在 1.1 修复后，将 token 迁入 `sessionStorage` 或 HttpOnly Cookie（需后端支持，注意 SSE 需要 header 传 token 的现状）；至少为 token 增加过期时间与滑动续期。

### 1.3【高】全局搜索对每个项目顺序发起全量请求

- **位置**：`app.projects.js:61-92`（`runGlobalSearch`）
- **问题**：`for (const proj of this.projects)` 内逐个 `this.api('GET', '/projects/{id}')`，N 个项目 = N 次请求，且每次拉取完整项目详情（facts/intents 全部），无缓存、无并发限制、无超时。项目较多时（如 30+）会明显卡顿，且每个失败的请求都被 `catch { continue }` 静默吞掉。
- **修复建议**：改为后端提供 `/projects/search?q=` 聚合搜索接口；或前端一次性拉取全部项目详情后做本地缓存（按 `last_modified` 失效），并限制并发 3-5 个。

### 1.4【中】`api()` / `fetchText()` 无超时、SSE 无重连上限

- **位置**：`app.core.js:75-108`、`app.project-detail.js:233-271`
- **问题**：
  1. `api()` 的 `fetch` 未设置 `AbortSignal.timeout`，后端异常挂起时前端永久 pending，弹窗/按钮状态卡死；
  2. `_connectProjectSse` 的 fetch 流断开后直接静默退出（`app.project-detail.js:266-269`），SSE 失效期间依赖 5 秒轮询兜底，但**没有自动重连**，长期运行后 SSE 可能永久失效；
  3. `_handleProjectSseEvent`（`app.project-detail.js:281-326`）对 `fact_created`/`intent_created`/`project_completed` 事件都做「全量 loadProject + updateGraph + loadProjects」三连，SSE 高频事件时开销大。
- **修复建议**：给 `api()` 增加 15-30s 超时（`AbortSignal.timeout` 或手动 controller）；SSE 增加指数退避重连（上限若干次）；对 SSE 事件做节流/合并。

### 1.5【中】`runGlobalSearch` 结果条目使用列表摘要 status

- **位置**：`app.projects.js:80`（`projectStatus: proj.status`）
- **问题**：`proj` 来自 `this.projects`（列表摘要），而 `data` 才是完整详情。status 摘要与详情一致时无问题，但若列表摘要过期（如刚停止/完成），搜索结果的徽章可能显示旧状态。功能 bug 级别，非安全。
- **修复建议**：使用 `data.project.status` 作为搜索结果状态。

### 1.6【中】`renderMd` 处理 `<h1>` 未转义即可注入，且代码块占位符可能被破坏

- **位置**：`app.core.js:174-211`
- **问题**：
  1. 第 183 行整体 `esc()` 之后，任何原生的 `<h1>`、`<p>`、`<a>` 等标签在 `esc()` 前未先占位，替换阶段（189-199 行）只认 Markdown 标记，但第 207 行 `/^<(pre|ul|ol|h[1-6]|hr)/` 的判断会把**未转义的 HTML 标签**当作合法内容跳过段落包裹 → 原始 `<img onerror>` 直接输出；
  2. 代码块内若包含 `\x00BLOCK(\d+)\x00` 字面量，第 185 行还原时可能被错误替换（低概率但可被构造）。
- **修复建议**：见 1.1，统一交给经过验证的 Markdown 渲染 + 严格 HTML 过滤（DOMPurify 白名单），不要手写正则拼 HTML。

### 1.7【中】`confirmDeleteProject` 删除后 `selectedProjectId` 仍指向已删除项目

- **位置**：`app.projects.js:447-466`
- **问题**：删除当前打开项目时调用 `backToList()` 会清空 `selectedProjectId`，但删除**非当前**项目时 `else` 分支只 `loadProjects()`，不处理 `selectedProjectId` 引用。若正打开的是被删项目而用户从列表删除，`project` 仍保留旧数据，后续轮询会持续 404（`startPolling` 每 5 秒 `loadProject(selectedProjectId)`）。
- **修复建议**：`confirmDeleteProject` 成功分支统一检查 `if (this.selectedProjectId === projectId) this.backToList(); else await this.loadProjects();`（当前 454 行已覆盖 `view === 'graph'`，但删除非当前项目后仍需刷新 `selectedProjectId` 的引用一致性）。

### 1.8【中】`downloadAiReport` / `downloadExportFile` 下载失败被 `catch(() => ({}))` 吞掉

- **位置**：`app.projects.js:396-427`
- **问题**：下载接口返回非 2xx 时 `resp.json().catch(() => ({}))` 只提取 `detail`，若响应体不是 JSON 则错误信息丢失，用户只看到「下载失败 HTTP 500」之类；`downloadExportFile` 使用 `fetchText` 本身 OK，但 `format` 未做白名单校验，可注入任意值。
- **修复建议**：非 JSON 响应时回退读取 `resp.text()` 摘要；`format` 白名单 `['yaml','json','md']`。

### 1.9【中】上传接口参数拼接方式不统一

- **位置**：`app.analyzers.js:62/209/282`、`app.analyzers.js:339`
- **问题**：`URLSearchParams` 会转义 `&`、`=`，但 339 行 `uploadAndroidApk` 手动拼 `?filename=${encodeURIComponent(file.name)}` 已正确转义；62/209 行用 `URLSearchParams` 构造参数正确。整体风险低，但 339 行拼串风格与 62/209 行不统一，且 62 行 `filename` 若含 `#` 可能截断 query（取决于后端解析）。建议统一用 `URLSearchParams`。
- **修复建议**：统一改用 `new URLSearchParams({ filename, ... }).toString()`；对 `#` 等特殊字符单独测试。

### 1.10【中】`timelineEvents()` 缓存命中率低，频繁重建大数组

- **位置**：`app.timeline.js:6-9`
- **问题**：缓存 key 为 `this.project`（对象引用），而每次 `loadProject` 都返回新对象 → 缓存基本不命中，每次渲染都重建全部事件数组。`startPolling` 5 秒刷新 + SSE 刷新时，大项目（数百 intent/fact）会反复重建并触发 Vue 重渲染。
- **修复建议**：缓存 key 改为 `project.project.id + 项目 updated_at`（或项目详情版本号）；或对事件构建做增量更新。

### 1.11【中】`updateGraph` 全量 diff 每次 `cy.layout()`，节点多时卡顿

- **位置**：`app.graph.js:222-264`
- **问题**：`buildElements()` 每次全量重建节点/边集合；`cy.nodes().forEach` + `cy.edges().forEach` 逐节点 `getElementById`（O(n²) 级，元素多时明显）；且只要 `changed` 就 `cy.layout()`（第 260 行），SSE 高频事件下频繁布局重排，破坏用户平移/缩放状态。
- **修复建议**：按 `project.updated_at` 判断是否需要重算；布局仅在结构变化（节点/边新增删除）时运行，纯 data 更新用 `cy.batch` 合并。

### 1.12【中】5 秒轮询全量刷新 + dispatcher 状态

- **位置**：`app.project-detail.js:216-231`
- **问题**：非 graph 视图（列表/仪表盘）时每 5 秒 `loadProjects()` 全量拉取；`view === 'dispatcher'` 时再叠加 `loadDispatcherStatus()`。项目多时网络开销与后端压力不小，且轮询与 SSE 共存会重复刷新。
- **修复建议**：仅在可见标签页时轮询（`document.hidden` 时跳过）；列表视图改用增量刷新（返回 `updated_at` 后按需全量）。

### 1.13【中】模块间隐式循环依赖/顺序耦合

- **位置**：`app.replay.js:253`（调用 `timelineEvents` 与 `applyReplayFrame` 内建图）、`app.timeline.js:7`（读取 `replay.visibleEvents`）、`app.js:210-218`（按序挂载 9 个模块）
- **问题**：模块通过共享 `this` 对象互相调用，但没有任何依赖声明；`app.replay.js` 与 `app.timeline.js` 相互引用（timeline 依赖 replay，replay 又依赖 timeline），若调整挂载顺序即崩。Mixin 模式本身可接受，但缺依赖图/文档。
- **修复建议**：在 `app.js` 头部注释声明依赖 DAG（见 ARCHITECTURE.md 已有第 9 节），并在模块函数入口断言依赖存在；或引入显式 `this._depends(['timeline','replay'])`。

### 1.14【中】图生命周期销毁/重建逻辑重复（4 处）

- **位置**：`app.projects.js`（打开项目逻辑在 detail 模块，但 `goNewProject`/`createProject` 也调 `backToList`/`openProject`）、`app.project-detail.js:153-214`（`openProject`/`backToList` 内直接操作 replay 与 graph 生命周期）
- **问题**：`openProject` 里硬编码「重置 replay 状态」+「销毁 cy + 重建」等，这些职责横跨 replay、graph、project-detail 三个模块；`app.replay.js:232-242/345-352` 又**重复**了几乎相同的 `cy.destroy() + initGraph()` 逻辑（4 处重复销毁/初始化）。
- **修复建议**：抽取 `resetGraph()`/`resetReplayState()` 工具到 core 或 graph 模块，统一由 `openProject`/`backToList` 调用，消除 4 处重复。

### 1.15【中】CSP 缺失 + vendor 脚本无 SRI 校验

- **位置**：`index.html:9-16`（tailwind/dagre/cytoscape/klay/elk 全从 `/static/vendor/` 加载，无 `integrity`）、`app.js`/核心均未设置 CSP
- **问题**：本工具本地运行风险可控，但若部署到内网服务器（用户常见场景），第三方大库被替换即可全站 XSS；且 `tailwindcss.js` 是 JIT 编译器，任意 HTML 变更都可能触发其运行时解析，无 CSP 时任何 `x-html` 注入都有执行面。
- **修复建议**：为静态资源设置 `Content-Security-Policy` 响应头（`default-src 'self'; script-src 'self' 'unsafe-inline'` 至少）；为 vendor 文件生成 SRI `integrity`；优先把 tailwind 换成构建期产物。

### 1.16【低】`escapeHtml` 与 `renderMd` 各自维护转义实现

- **位置**：`app.core.js:176`（`renderMd` 内局部 `esc`）vs `app.core.js:497-504`（`escapeHtml`）
- **问题**：两处转义逻辑不统一（`esc` 只转 `&<>`，`escapeHtml` 转 `&<>"'`），且 `renderMd` 的 `esc` 不对引号转义——属性注入场景（如果有字符串拼进属性）会遗漏。属于设计层面的「转义不收敛」。
- **修复建议**：统一使用 `escapeHtml`（或专用 sanitizer），删除局部 `esc`。

---

## 二、按 ROI（修复性价比）排序汇总

| 优先级 | 发现 | 严重程度 | 改动量 | 修复要点 |
|---|---|---|---|---|
| 1 | `renderMd` 存储型 XSS（1.1） | 高 | 中 | 用 DOMPurify/marked 替换手写渲染；7 处 `x-html` 直接受益 |
| 2 | localStorage 明文 token（1.2） | 高 | 低 | token 迁 sessionStorage 或 HttpOnly Cookie，配合 XSS 修复 |
| 3 | `runGlobalSearch` 全量拉取（1.3） | 中 | 中 | 后端搜索接口或前端缓存+并发限制 |
| 4 | `api()` 无超时 / SSE 无重连（1.4） | 中 | 低 | `AbortSignal.timeout(30s)` + SSE 指数退避重连 |
| 5 | `timelineEvents` 缓存失效（1.10） | 中 | 低 | 缓存 key 改为 `id + updated_at` |
| 6 | `updateGraph` 全量 diff + 频繁 layout（1.11） | 中 | 中 | 按 updated_at 增量更新，layout 节流 |
| 7 | 5 秒轮询无节流（1.12） | 中 | 低 | `document.hidden` 跳过、按视图降频 |
| 8 | 图生命周期重复销毁/重建（1.14） | 中 | 低 | 抽取 `resetGraph()`/`resetReplay()` |
| 9 | CSP/SRI 缺失（1.15） | 中 | 低 | 加 CSP 头 + vendor SRI |
| 10 | `confirmDeleteProject` 引用一致性（1.7） | 中 | 低 | 补 `backToList()` 分支 |
| 11 | 下载错误吞掉（1.8） | 中 | 低 | 非 JSON 响应回退 `resp.text()` |
| 12 | 模块依赖未声明（1.13） | 中 | 低 | `app.js` 头部注释 DAG + 断言 |
| 13 | `URLSearchParams` 不统一（1.9） | 低 | 低 | 统一用 `URLSearchParams` |
| 14 | 转义不收敛（1.16） | 低 | 低 | 统一 `escapeHtml`/引入 sanitizer |
| 15 | 搜索结果 status 过期（1.5） | 低 | 低 | 用 `data.status` |

---

## 三、总结

前端整体质量中等偏上：模块化拆分完成度高（9 模块 + 主文件），YAML 高亮正确使用了 `escapeHtml`（`app.projects.js:109-135`），SSE/轮询的降级设计合理。核心风险集中在 **XSS 与认证 token 安全**（1.1/1.2），建议优先修复；其次是**性能与 API 健壮性**（1.3/1.4/1.10/1.11），可在日常迭代中处理。所有修复都不应破坏 mixin 组装方式，建议在 `docs/CHANGELOG.md` 记录修复版本。

> AI生成