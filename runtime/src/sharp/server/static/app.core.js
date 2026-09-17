/**
 * app.core.js — 核心基础设施：状态初始化、API 封装、认证、主题、通用工具方法
 *
 * 认证 token 存储（P0 安全修复）：主存 **sessionStorage** —— 关闭标签页即失效，
 * 把明文 token 的驻留窗口从"永久"缩到"当前会话"。首次加载若发现旧版
 * localStorage 值，自动迁移并清除旧值（用户不被登出，磁盘上也不再留明文）。
 * 注意：token 仍需驻留内存用于 Bearer 头，因此 XSS 防护依赖 renderMd 的
 * DOMPurify 白名单 + 服务端 CSP（见 sharp/server/app.py 安全头中间件）。
 */
const SHARP_TOKEN_KEY = 'sharp_token';

function sharpReadStoredToken() {
  try {
    const s = sessionStorage.getItem(SHARP_TOKEN_KEY);
    if (s) return s;
    const legacy = localStorage.getItem(SHARP_TOKEN_KEY);   // 旧版遗留，迁移后清除
    if (legacy) {
      sessionStorage.setItem(SHARP_TOKEN_KEY, legacy);
      localStorage.removeItem(SHARP_TOKEN_KEY);
      return legacy;
    }
  } catch (_) { /* 隐私模式 / 存储被禁用时降级为内存态（刷新需重新登录） */ }
  return '';
}

function sharpStoreToken(token) {
  try {
    sessionStorage.setItem(SHARP_TOKEN_KEY, token);
    localStorage.removeItem(SHARP_TOKEN_KEY);
  } catch (_) {}
}

function sharpClearToken() {
  try {
    sessionStorage.removeItem(SHARP_TOKEN_KEY);
    localStorage.removeItem(SHARP_TOKEN_KEY);
  } catch (_) {}
}
function applyCoreModule(obj) {
  Object.assign(obj, {
    async init() {
      // ── auth gate ──────────────────────────────────────────────────────────
      try {
        const status = await fetch('/auth/status').then(r => r.json());
        if (!status.initialized) { this.authView = 'setup'; return; }
        if (!this.authToken)     { this.authView = 'login'; return; }
        const check = await fetch('/projects', {
          headers: { 'Authorization': `Bearer ${this.authToken}` },
        });
        if (check.status === 401) {
          this.authToken = ''; sharpClearToken();
          this.authView = 'login'; return;
        }
      } catch (e) { this.authView = 'login'; return; }
      this.authView = 'app';
      // ── normal startup ─────────────────────────────────────────────────────────
      this._panelResizeMove = (e) => this.onPanelResize(e);
      this._panelResizeStop = () => this.stopPanelResize();
      window.addEventListener('pointermove', this._panelResizeMove);
      window.addEventListener('pointerup', this._panelResizeStop);
      this.loadLocalPrefs();
      await this.loadProjects();
      await this.loadSettings();
      await this.loadApprovals();   // 审批中心统计（含导航徽章），失败静默
      this.loadVulnStats();          // 全局漏洞分级统计（仪表盘环形图用），失败静默
      this.loadVulnTrend();          // 全局漏洞发现趋势（仪表盘面积图用），失败静默
      this.startPolling();
      // 每5秒刷新 _now，驱动心跳进度条/超时报警实时更新
      this._heartbeatTimer = setInterval(() => { this._now = Date.now(); this.markStaleIntents(); }, 5000);
      this._hashChangeHandler = () => this.handleRoute();
      window.addEventListener('hashchange', this._hashChangeHandler);
      // ── 全局快捷键 ─────────────────────────────────────────────────────────────────────────
      this._keydownHandler = (e) => {
        // Cmd/Ctrl+K 打开全局搜索
        if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
          e.preventDefault();
          if (this.authView === 'app') this.openGlobalSearch();
          return;
        }
        if (e.key === 'Escape') {
          // 按优先级关闭最顶层弹窗
          if (this.uiPrompt?.show)                { this.uiPrompt.show = false; return; }
          if (this.uiConfirm?.show)               { this.uiConfirm.show = false; return; }
          if (this.globalSearch?.show)            { this.closeGlobalSearch(); return; }
          if (this.reportPreview?.show)           { this.reportPreview.show = false; return; }
          if (this.factCorrection?.open)          { this.closeFactCorrection(); return; }
          if (this.showDeleteModal)               { this.showDeleteModal = false; return; }
          if (this.npPage.showAddForm || this.npPage.editingIdx !== -1) {
            this.npPage.showAddForm = false; this.npPage.editingIdx = -1; return;
          }
          if (this.showNewProject)                { this.showNewProject = false; return; }
          if (this.showYamlModal)                 { this.showYamlModal = false; return; }
          if (this.projectAction.show)             { this.projectAction = { show: false, mode: '' }; return; }
          if (this.showApprovalDetail)           { this.closeApprovalDetail(); return; }
          if (this.showEmergencyModal)           { this.closeEmergencyModal(); return; }
        }
      };
      window.addEventListener('keydown', this._keydownHandler);
      this.handleRoute();
    },

    destroy() {
      // 清理全局事件监听器
      if (this._panelResizeMove) { window.removeEventListener('pointermove', this._panelResizeMove); this._panelResizeMove = null; }
      if (this._panelResizeStop) { window.removeEventListener('pointerup', this._panelResizeStop); this._panelResizeStop = null; }
      if (this._hashChangeHandler) { window.removeEventListener('hashchange', this._hashChangeHandler); this._hashChangeHandler = null; }
      if (this._keydownHandler) { window.removeEventListener('keydown', this._keydownHandler); this._keydownHandler = null; }
      // 清理定时器
      if (this._heartbeatTimer) { clearInterval(this._heartbeatTimer); this._heartbeatTimer = null; }
      if (this.pollTimer) { clearInterval(this.pollTimer); this.pollTimer = null; }
      // 清理 SSE
      this._disconnectProjectSse();
    },

    handleRoute() {
      const hash = location.hash || '#/';
      const m = hash.match(/^#\/projects\/(.+)$/);
      if (m) {
        const id = m[1];
        if (this.selectedProjectId !== id || this.view !== 'graph') {
          this.openProject(id);
        }
      } else {
        if (this.view === 'graph') this.backToList(true);
      }
    },

    // ── 请求超时（P0 健壮性修复）────────────────────────────────────────
    // 后端异常挂起时前端不再永久 pending（此前 fetch 无任何超时）。
    _timeoutSignal(timeoutMs) {
      if (!timeoutMs || timeoutMs <= 0) return undefined;
      if (typeof AbortSignal !== 'undefined' && typeof AbortSignal.timeout === 'function') {
        return AbortSignal.timeout(timeoutMs);
      }
      const c = new AbortController();
      setTimeout(() => c.abort(), timeoutMs);
      return c.signal;
    },
    _isTimeoutError(e) {
      return !!e && (e.name === 'TimeoutError' || e.name === 'AbortError');
    },
    // 长耗时端点（AI 报告生成、路径分析/解包、深度分析派发、调度器重启、动态分析）
    // 按路径自动放宽到 10 分钟，避免逐个调用点漏改；仍可用 opts.timeoutMs 显式覆盖。
    apiTimeoutMs(path, opts) {
      if (opts && opts.timeoutMs !== undefined) return opts.timeoutMs;
      const long = ['/reports/ai', '/analyze', '/unpack', '/dispatch-static-analysis',
                    '/dispatcher/restart', '/dynamic/'];
      return long.some(p => String(path).includes(p)) ? 600000 : 30000;
    },

    async api(method, path, body, opts = {}) {
      const timeoutMs = this.apiTimeoutMs(path, opts);
      const reqOpts = { method, headers: { 'Content-Type': 'application/json' } };
      if (this.authToken) reqOpts.headers['Authorization'] = `Bearer ${this.authToken}`;
      if (body) reqOpts.body = JSON.stringify(body);
      const signal = this._timeoutSignal(timeoutMs);
      if (signal) reqOpts.signal = signal;
      let r;
      try {
        r = await fetch(path, reqOpts);
      } catch (e) {
        if (this._isTimeoutError(e)) {
          throw new Error(`请求超时（${Math.round(timeoutMs / 1000)}s）：服务端可能仍在处理，请稍后刷新查看`);
        }
        throw e;
      }
      if (r.status === 401) {
        this.authToken = ''; sharpClearToken();
        this.authView = 'login';
        throw new Error('登录已过期，请重新登录');
      }
      if (r.status === 204) return null;
      const data = await r.json();
      if (!r.ok) {
        let msg = `HTTP ${r.status}`;
        if (typeof data.detail === 'string') msg = data.detail;
        else if (Array.isArray(data.detail)) msg = data.detail.map(e => e.msg).join('; ');
        throw new Error(msg);
      }
      return data;
    },

    async fetchText(path, opts = {}) {
      const timeoutMs = opts.timeoutMs === undefined ? 120000 : opts.timeoutMs;
      const signal = this._timeoutSignal(timeoutMs);
      let r;
      try {
        r = await fetch(path, {
          headers: this.authToken ? { 'Authorization': `Bearer ${this.authToken}` } : {},
          ...(signal ? { signal } : {}),
        });
      } catch (e) {
        if (this._isTimeoutError(e)) throw new Error(`导出超时（${Math.round(timeoutMs / 1000)}s）`);
        throw e;
      }
      if (r.status === 401) {
        this.authToken = ''; sharpClearToken();
        this.authView = 'login';
        throw new Error('登录已过期，请重新登录');
      }
      const text = await r.text();
      if (!r.ok) {
        let detail = text;
        try {
          const data = JSON.parse(text);
          detail = data.detail || text;
        } catch {}
        throw new Error(detail || `HTTP ${r.status}`);
      }
      return text;
    },

    // ── auth actions ───────────────────────────────────────────────────────────
    async authLogin() {
      if (!this.authPassword || this.authLoading) return;
      this.authLoading = true; this.authError = '';
      try {
        const r = await fetch('/auth/login', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ password: this.authPassword }),
        });
        const d = await r.json();
        if (!r.ok) throw new Error(d.detail || '登录失败');
        this.authToken = d.token; sharpStoreToken(d.token);
        this.authPassword = '';
        await this.init();
      } catch (e) { this.authError = e.message; }
      finally { this.authLoading = false; }
    },
    _checkPwComplexity(pw, label = '密码') {
      if (pw.length < 10) return `${label}长度至少 10 位`;
      if (!/[A-Z]/.test(pw)) return `${label}需包含至少 1 个大写字母`;
      if (!/[a-z]/.test(pw)) return `${label}需包含至少 1 个小写字母`;
      if (!/\d/.test(pw)) return `${label}需包含至少 1 个数字`;
      if (!/[!@#$%^&*()\-_=+\[\];':"\\|,.<>/?`~]/.test(pw)) return `${label}需包含至少 1 个特殊字符`;
      return null;
    },

    async authSetup() {
      if (!this.authAgree) { this.authError = '请先阅读并勾选同意授权使用条款'; return; }
      if (this.authPassword !== this.authPasswordConfirm) { this.authError = '两次密码不一致'; return; }
      const err = this._checkPwComplexity(this.authPassword);
      if (err) { this.authError = err; return; }
      if (this.authLoading) return;
      this.authLoading = true; this.authError = '';
      try {
        const r = await fetch('/auth/setup', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ password: this.authPassword }),
        });
        const d = await r.json();
        if (!r.ok) throw new Error(d.detail || '设置失败');
        // 初始化成功后直接切换到登录界面，避免重新加载页面导致的缓存问题
        this.authPassword = '';
        this.authPasswordConfirm = '';
        this.authAgree = false;
        this.authError = '';
        this.authView = 'login';
      } catch (e) { this.authError = e.message; }
      finally { this.authLoading = false; }
    },
    async authLogout() {
      this.destroy();
      await fetch('/auth/logout', { method: 'POST' }).catch(() => {});
      this.authToken = ''; sharpClearToken();
      this.authView = 'login'; this.authError = '';
    },

    applyTheme(mode) {
      this.themeMode = mode;
      localStorage.setItem('sharp_theme', mode);
      const prefersDark = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
      const dark = mode === 'dark' || (mode === 'system' && prefersDark);
      this.darkActive = dark;
      document.documentElement.setAttribute('data-theme', dark ? 'dark' : 'light');
    },

    renderMd(text) {
      if (!text) return '';
      const esc = s => s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
      // Extract code blocks first to protect their content
      const blocks = [];
      let s = text.replace(/```[\w]*\n?([\s\S]*?)```/g, (_,code) => {
        blocks.push(`<pre><code>${esc(code).trimEnd()}</code></pre>`);
        return `\x00BLOCK${blocks.length-1}\x00`;
      });
      s = esc(s);
      // Restore code blocks
      s = s.replace(/\x00BLOCK(\d+)\x00/g, (_,i) => blocks[i]);
      // Inline code
      s = s.replace(/`([^`]+)`/g, (_,c) => `<code>${esc(c)}</code>`);
      // Headers
      s = s.replace(/^### (.+)$/gm, '<h3>$1</h3>');
      s = s.replace(/^## (.+)$/gm, '<h2>$1</h2>');
      s = s.replace(/^# (.+)$/gm, '<h2>$1</h2>');
      // Bold / italic
      s = s.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
      s = s.replace(/\*(.+?)\*/g, '<em>$1</em>');
      // HR
      s = s.replace(/^---+$/gm, '<hr>');
      // Lists
      s = s.replace(/^[-*] (.+)$/gm, '<li>$1</li>');
      s = s.replace(/^\d+\. (.+)$/gm, '<li>$1</li>');
      s = s.replace(/(<li>[\s\S]*?<\/li>)(\n<li>|$)/g, '$1$2');
      s = s.replace(/((?:<li>.*<\/li>\n?)+)/g, m => `<ul>${m}</ul>`);
      // Paragraphs
      const parts = s.split(/\n\n+/);
      s = parts.map(p => {
        p = p.trim();
        if (!p) return '';
        if (/^<(pre|ul|ol|h[1-6]|hr)/.test(p)) return p;
        return `<p>${p.replace(/\n/g,'<br>')}</p>`;
      }).join('');
      // DOMPurify sanitize：防止 XSS，白名单仅保留 renderMd 生成的标签。
      // 若 DOMPurify 未加载（CDN/vendor 失败），回退为纯文本而非跳过消毒 ——
      // 跳过等于无过滤 innerHTML，XSS 防护单点失效。
      if (typeof DOMPurify !== 'undefined') {
        s = DOMPurify.sanitize(s, { ALLOWED_TAGS: ['p','br','pre','code','h2','h3','strong','em','hr','ul','ol','li'] });
      } else {
        // 回退：strip 所有 HTML 标签，只保留文本内容
        s = s.replace(/<[^>]*>/g, '');
      }
      return s;
    },

    async changePassword() {
      if (this.pwForm.new1 !== this.pwForm.new2) { this.pwError = '两次新密码不一致'; return; }
      const err = this._checkPwComplexity(this.pwForm.new1, '新密码');
      if (err) { this.pwError = err; return; }
      if (this.pwLoading) return;
      this.pwLoading = true; this.pwError = '';
      try {
        await this.api('POST', '/auth/change-password', {
          old_password: this.pwForm.old, new_password: this.pwForm.new1,
        });
        this.pwForm = { old: '', new1: '', new2: '' };
        this.pwError = '密码修改成功';
        this.showToast('密码已修改');
      } catch (e) { this.pwError = e.message; }
      finally { this.pwLoading = false; }
    },

    showToast(msg, type = 'info') {
      this.toast = { show: true, message: msg, type };
      setTimeout(() => this.toast.show = false, 3000);
    },

    // ── 通用站内对话框（替代原生 confirm/prompt）────────────────────────
    confirmDialog(opts) {
      // opts: { title, message, okText?, danger?, onOk }
      if (this.uiPrompt?.show) this.uiPrompt.show = false;
      this.uiConfirm = {
        show: true,
        title: opts.title || '确认操作',
        message: opts.message || '',
        okText: opts.okText || '确定',
        danger: !!opts.danger,
        onOk: opts.onOk || null,
      };
    },
    closeConfirmDialog() { this.uiConfirm.show = false; this.uiConfirm.onOk = null; },
    confirmDialogOk() {
      const onOk = this.uiConfirm.onOk;
      this.closeConfirmDialog();
      if (onOk) onOk();
    },

    promptDialog(opts) {
      // opts: { title, placeholder?, initial?, onOk(value) }
      if (this.uiConfirm?.show) this.uiConfirm.show = false;
      this.uiPrompt = {
        show: true,
        title: opts.title || '输入',
        placeholder: opts.placeholder || '',
        value: opts.initial || '',
        onOk: opts.onOk || null,
      };
      this.$nextTick(() => {
        const el = document.getElementById('uiPromptInput');
        if (el) { el.focus(); el.select(); }
      });
    },
    closePromptDialog() { this.uiPrompt.show = false; this.uiPrompt.onOk = null; },
    promptDialogOk() {
      const onOk = this.uiPrompt.onOk;
      const value = this.uiPrompt.value;
      this.closePromptDialog();
      if (onOk) onOk(value);
    },

    cloneData(value) {
      try {
        return JSON.parse(JSON.stringify(value));
      } catch (error) {
        if (typeof structuredClone === 'function') return structuredClone(value);
        throw error;
      }
    },

    isValidLayoutMode(mode) {
      return ['dagre_tb', 'dagre_lr', 'klay_tb', 'klay_lr', 'elk_tb', 'elk_lr'].includes(mode);
    },

    statusLabel(status) {
      return ({
        active: '运行中',
        stopped: '已暂停',
        completed: '已完成',
      })[status] || status || '未知';
    },

    projectPauseLabel(p) {
      if (!p) return '';
      const paused = p.paused !== undefined ? p.paused : (p.project?.paused || false);
      return paused ? '调度暂停' : '';
    },

    budgetText(p) {
      if (!p) return '';
      const budget = p.task_budget !== undefined ? p.task_budget : (p.project?.task_budget || 0);
      const count = p.task_count !== undefined ? p.task_count : (p.project?.task_count || 0);
      if (budget === 0) return '';
      return `${count}/${budget}`;
    },

    taskTypeLabel(type) {
      return ({
        bootstrap: '启动探索',
        reason: '推理决策',
        explore: '定向探索',
        report: '报告编写',
      })[type] || type || '未知任务';
    },

    projectCanCreateAiReport(status) {
      return status === 'completed';
    },

    layoutEngine(mode = this.layoutMode) {
      if (mode.startsWith('elk')) return 'elk';
      return mode.startsWith('klay') ? 'klay' : 'dagre';
    },

    layoutDirection(mode = this.layoutMode) {
      return mode.endsWith('_lr') ? 'LR' : 'TB';
    },

    loadLocalPrefs() {
      try {
        const raw = localStorage.getItem('sharp.localPrefs');
        if (!raw) {
          this.localPrefs.actor_name = '人工操作员';
        } else {
          const parsed = JSON.parse(raw);
          if (typeof parsed.actor_name === 'string') this.localPrefs.actor_name = parsed.actor_name;
          if (this.isValidLayoutMode(parsed.layout_mode)) {
            this.localPrefs.layout_mode = parsed.layout_mode;
          } else if (parsed.layout_dir === 'TB' || parsed.layout_dir === 'LR') {
            this.localPrefs.layout_mode = parsed.layout_dir === 'LR' ? 'dagre_lr' : 'dagre_tb';
          }
        }
        const rawPanelWidth = localStorage.getItem('sharp.sidePanelWidth');
        if (rawPanelWidth !== null) {
          const savedPanelWidth = Number(rawPanelWidth);
          if (Number.isFinite(savedPanelWidth)) this.sidePanelWidth = savedPanelWidth;
        }
      } catch (e) {
        console.error(e);
      }
      if (!this.localPrefs.actor_name.trim()) this.localPrefs.actor_name = '人工操作员';
      if (!this.isValidLayoutMode(this.localPrefs.layout_mode)) this.localPrefs.layout_mode = 'dagre_tb';
      this.layoutMode = this.localPrefs.layout_mode;
    },

    saveLocalPrefs() {
      try {
        localStorage.setItem('sharp.localPrefs', JSON.stringify(this.localPrefs));
      } catch (e) {
        console.error(e);
      }
    },

    saveSidePanelWidth() {
      try {
        localStorage.setItem('sharp.sidePanelWidth', String(this.sidePanelWidth));
      } catch (e) {
        console.error(e);
      }
    },

    actorName() {
      return this.localPrefs.actor_name.trim() || '人工操作员';
    },

    projectStatusBadgeClass(status) {
      const classes = {
        active: 'bg-teal-50 text-teal-600',
        stopped: 'bg-amber-50 text-amber-700',
        completed: 'bg-slate-100 text-slate-500',
      };
      return classes[status] || 'bg-slate-100 text-slate-500';
    },

    reasonBadgeText(reason, compact = false) {
      if (!reason) return '';
      if (compact) return `推理 · ${reason.worker}`;
      const trigger = reason.trigger ? ` (${reason.trigger})` : '';
      return `推理中 · ${reason.worker}${trigger}`;
    },

    workingIntentBadgeText(count) {
      return `探索中 · ${count}`;
    },

    isBootstrapIntent(intent) {
      return Boolean(
        intent
        && intent.description === 'bootstrap'
        && intent.creator === 'dispatcher.bootstrap'
        && Array.isArray(intent.from)
        && intent.from.length === 1
        && intent.from[0] === 'origin'
        && intent.to === null,
      );
    },

    openIntentNodeType(intent) {
      if (this.isBootstrapIntent(intent)) return intent.worker ? 'bootstrap_running' : 'bootstrap_pending';
      // 高危/严重待审批行动：黄色虚线节点（未认领且无 worker）
      if (intent.approval_status === 'pending' && !intent.worker) return 'pending_approval';
      return intent.worker ? 'in_progress' : 'unclaimed';
    },

    openIntentNodeLabel(intent) {
      if (this.isBootstrapIntent(intent)) return '启动探索';
      if (intent.approval_status === 'pending' && !intent.worker) return '待审批';
      if (intent.worker) return '探索中';
      return '待认领';
    },

    openIntentNodeSize(intent) {
      if (this.isBootstrapIntent(intent)) return { width: 90, height: 34 };
      if (intent.approval_status === 'pending' && !intent.worker) return { width: 86, height: 34 };
      return { width: 74, height: 34 };
    },

    projectIsActive() {
      return !this.replay.active && this.project?.project?.status === 'active';
    },

    countProjectsByStatus(status) {
      return this.projects.filter(project => project.status === status).length;
    },

    goDashboard() {
      if (this.view === 'graph') this.backToList(true);
      this.view = 'dashboard';
    },
    goProjectList() {
      if (this.view === 'graph') { this.backToList(); return; }
      this.view = 'list';
    },
    async goDispatcher() {
      if (this.view === 'graph') this.backToList(true);
      this.view = 'dispatcher';
      await this.loadDispatcherStatus();
    },
    async goSettings() {
      if (this.view === 'graph') this.backToList(true);
      this.view = 'settings';
      await this.loadSettings();
    },
    goMiniProgram() {
      if (this.view === 'graph') this.backToList(true);
      this.showMiniProgramAnalyzer = false;
      this.appAnalysisTab = 'miniprogram';
      this.view = 'app-analysis';
    },
    goAndroid() {
      if (this.view === 'graph') this.backToList(true);
      this.appAnalysisTab = 'android';
      this.view = 'app-analysis';
      this._restoreAndroidChatSession();
    },
    goAppAnalysis() {
      if (this.view === 'graph') this.backToList(true);
      this.showMiniProgramAnalyzer = false;
      this.view = 'app-analysis';
    },

    async _restoreAndroidChatSession() {
      try {
        const saved = localStorage.getItem('sharp_android_chat');
        if (!saved) return;
        const { sessionId, apkFilename, apkPath } = JSON.parse(saved);
        if (!sessionId) return;
        const data = await this.api('GET', `/android/chat/sessions/${sessionId}/messages`);
        this.androidChat.sessionId = sessionId;
        this.androidChat.apkFilename = apkFilename || '';
        if (apkPath) this.android.path = apkPath;
        this.androidChat.messages = (data.messages || []).map((m, i) => ({
          id: `r_${i}`, role: m.role, content: m.content,
        }));
      } catch (_) {
        localStorage.removeItem('sharp_android_chat');
      }
    },
    async goReports() {
      if (this.view === 'graph') this.backToList(true);
      this.view = 'reports';
      await this.loadProjects();
    },
    async goApprovals() {
      if (this.view === 'graph') this.backToList(true);
      this.view = 'approvals';
      await this.loadApprovals();
      await this.loadEmergencyStatus();
    },
    _sum(field) {
      return this.projects.reduce((acc, p) => acc + (p[field] || 0), 0);
    },
    totalFacts() { return this._sum('fact_count'); },
    totalIntents() { return this._sum('intent_count'); },
    totalWorkingIntents() { return this._sum('working_intent_count'); },
    totalUnclaimedIntents() { return this._sum('unclaimed_intent_count'); },
    totalHints() { return this._sum('hint_count'); },
    // ── 结论可交付性（P0-1）──────────────────────────────────────────────
    // "证据不足的产物" = 有结论但缺少可复现四件套（证据/复现/影响/定位）。
    // 判定逻辑在服务端 finding_quality.py；这里只做展示层聚合。
    incompleteFindings() {
      return (this.vulns || []).filter(v => v.quality && v.quality.applicable && !v.quality.complete);
    },
    qualityMissingText(v) {
      const q = v && v.quality;
      if (!q || q.applicable === false || q.complete) return '';
      return '证据不足，缺：' + ((q.missing_labels || []).join('、') || '（未填）');
    },
    totalVulns() { return this._sum('vuln_count'); },
    totalHighVulns() { return this._sum('vuln_high_count'); },

    // ── 仪表盘：漏洞严重度环形图 SVG arc path 计算 ──────────────────────────────
    vulnDonutPaths() {
      const s = this.vulnStats;
      const total = s.total;
      if (!total) return { arcs: [], total: 0, dominantPct: 0 };
      const size = 160, r = size / 2, thickness = 24, inner = r - thickness, cx = r, cy = r;
      const colors = { critical: '#dc2626', high: '#ea580c', medium: '#d97706', low: '#0284c7', info: '#64748b' };
      const labels = { critical: '严重', high: '高危', medium: '中危', low: '低危', info: '信息' };
      const order = ['critical', 'high', 'medium', 'low', 'info'];
      let acc = 0;
      const arcs = [];
      for (const key of order) {
        const val = s[key];
        if (!val) continue;
        const from = acc / total, to = (acc + val) / total;
        acc += val;
        const a0 = from * Math.PI * 2 - Math.PI / 2;
        const a1 = to * Math.PI * 2 - Math.PI / 2;
        const large = (to - from) > 0.5 ? 1 : 0;
        const x0o = cx + r * Math.cos(a0), y0o = cy + r * Math.sin(a0);
        const x1o = cx + r * Math.cos(a1), y1o = cy + r * Math.sin(a1);
        const x0i = cx + inner * Math.cos(a0), y0i = cy + inner * Math.sin(a0);
        const x1i = cx + inner * Math.cos(a1), y1i = cy + inner * Math.sin(a1);
        const d = `M ${x0o.toFixed(2)} ${y0o.toFixed(2)} A ${r} ${r} 0 ${large} 1 ${x1o.toFixed(2)} ${y1o.toFixed(2)} L ${x1i.toFixed(2)} ${y1i.toFixed(2)} A ${inner} ${inner} 0 ${large} 0 ${x0i.toFixed(2)} ${y0i.toFixed(2)} Z`;
        arcs.push({ d, color: colors[key], label: labels[key], key, value: val, pct: Math.round(val / total * 100) });
      }
      // 中心显示占比最大档的百分比
      const top = order.reduce((m, k) => s[k] > s[m] ? k : m, 'critical');
      const dominantPct = total ? Math.round(s[top] / total * 100) : 0;
      return { arcs, total, dominantPct, dominantLabel: labels[top] };
    },

    // ── 仪表盘：漏洞发现趋势（近 14 天按日期分桶）────────────────────────────────
    vulnTrendData() {
      const days = 14;
      const today = new Date();
      today.setHours(0, 0, 0, 0);
      const buckets = [];
      for (let i = days - 1; i >= 0; i--) {
        const d = new Date(today);
        d.setDate(d.getDate() - i);
        buckets.push({ date: `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`, count: 0, label: `${d.getMonth() + 1}/${d.getDate()}` });
      }
      const trend = this.vulnTrend || [];
      const map = {};
      for (const t of trend) { map[t.date] = t.count; }
      for (const b of buckets) { b.count = map[b.date] || 0; }
      return buckets;
    },

    // 漏洞趋势 SVG 面积图 path
    vulnTrendPaths() {
      const data = this.vulnTrendData();
      if (!data.length) return { area: '', line: '', maxVal: 0, points: [], labels: [] };
      const W = 320, H = 80, padX = 4, padY = 8;
      const maxVal = Math.max(1, ...data.map(d => d.count));
      const stepX = (W - padX * 2) / (data.length - 1);
      const pts = data.map((d, i) => {
        const x = padX + i * stepX;
        const y = H - padY - (d.count / maxVal) * (H - padY * 2);
        return { x, y, ...d };
      });
      // 平滑曲线（简单贝塞尔近似）
      let line = `M ${pts[0].x.toFixed(1)} ${pts[0].y.toFixed(1)}`;
      for (let i = 1; i < pts.length; i++) {
        const prev = pts[i - 1], curr = pts[i];
        const cpx = (prev.x + curr.x) / 2;
        line += ` C ${cpx.toFixed(1)} ${prev.y.toFixed(1)} ${cpx.toFixed(1)} ${curr.y.toFixed(1)} ${curr.x.toFixed(1)} ${curr.y.toFixed(1)}`;
      }
      const area = line + ` L ${pts[pts.length - 1].x.toFixed(1)} ${H - padY} L ${pts[0].x.toFixed(1)} ${H - padY} Z`;
      return { area, line, maxVal, points: pts, labels: data.map(d => d.label) };
    },

    // 环形图完整 SVG 字符串（注入到 <div x-html> 中，HTML 解析器能正确创建 inline SVG）
    vulnDonutSvg() {
      const { arcs, total } = this.vulnDonutPaths();
      if (!total) return '';
      const paths = arcs.map(a =>
        `<path d="${a.d}" fill="${a.color}" data-key="${a.key}" style="opacity:${this.vulnDonutHover === a.key ? 1 : 0.88};transition:opacity .15s;cursor:pointer" />`
      ).join('');
      return `<svg width="160" height="160" viewBox="0 0 160 160" class="overflow-visible">${paths}</svg>`;
    },

    // 趋势图完整 SVG 字符串
    vulnTrendSvg() {
      const tp = this.vulnTrendPaths();
      if (!tp.line) return '';
      const dots = tp.points.map(pt =>
        `<circle cx="${pt.x.toFixed(1)}" cy="${pt.y.toFixed(1)}" r="2.5" fill="#0ea5e9" style="opacity:${pt.count > 0 ? 0.8 : 0}" />`
      ).join('');
      // Y 方向网格（3 档：25%/50%/75% 高度处虚线；不画数值标签避免与 x 轴标签抢空间，
      // 卡片副行已有峰值/总计数字）。viewBox 与绘图坐标一致（320×80），显式 height
      // 且不加 preserveAspectRatio="none" —— 旧实现横纵一起拉伸导致图形变形。
      const gridY = [0.25, 0.5, 0.75].map(frac => {
        const y = (80 - 8) - frac * (80 - 16); // H=80, padY=8 → 数据区 8..72
        return `<line x1="4" y1="${y.toFixed(1)}" x2="316" y2="${y.toFixed(1)}" stroke="#e2e8f0" stroke-width="1" stroke-dasharray="3,3" />`;
      }).join('');
      return `<svg width="100%" height="80" viewBox="0 0 320 80" class="overflow-visible" style="min-height:80px">` +
        `<defs><linearGradient id="vulnTrendGrad" x1="0" y1="0" x2="0" y2="1">` +
        `<stop offset="0%" stop-color="#0ea5e9" stop-opacity="0.25" />` +
        `<stop offset="100%" stop-color="#0ea5e9" stop-opacity="0.02" />` +
        `</linearGradient></defs>` +
        gridY +
        `<line x1="4" y1="72" x2="316" y2="72" stroke="#e2e8f0" stroke-width="1" />` +
        `<path d="${tp.area}" fill="url(#vulnTrendGrad)" />` +
        `<path d="${tp.line}" fill="none" stroke="#0ea5e9" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" />` +
        dots +
        `</svg>`;
    },

    // 环形图 hover（事件委托）
    donutHandleEnter(e) {
      const path = e.target.closest('path[data-key]');
      if (path) this.vulnDonutHover = path.dataset.key;
    },
    donutHandleLeave() {
      this.vulnDonutHover = null;
    },

    // ── 漏洞库 ──────────────────────────────────────────────────────────────────
    async goVulns() {
      if (this.view === 'graph') this.backToList(true);
      this.view = 'vulns';
      if (this.selectedProjectId) {
        await this.loadVulns(this.selectedProjectId);
      } else {
        await this.loadAllVulns();
      }
    },
    async loadVulns(projectId) {
      this.vulnsLoading = true;
      try {
        this.vulns = await this.api('GET', `/projects/${projectId}/vulnerabilities`);
      } catch (e) { console.error('loadVulns error', e); this.vulns = []; }
      finally { this.vulnsLoading = false; }
      // 记分板（批次 B）：旗帜数/得分合计——评分类任务一眼看分
      try {
        this.scoreboard = await this.api('GET', `/projects/${projectId}/scoreboard`);
      } catch (e) { this.scoreboard = null; }
    },
    async loadAllVulns() {
      this.allVulnsLoading = true;
      try {
        this.allVulns = await this.api('GET', '/vulnerabilities');
        this.vulns = this.allVulns;
      } catch (e) { console.error('loadAllVulns error', e); this.allVulns = []; this.vulns = []; }
      finally { this.allVulnsLoading = false; }
    },
    filteredVulns() {
      let list = this.vulns || [];
      if ((this.vulnFilter.kind || 'all') !== 'all') {
        list = list.filter(v => (v.kind || 'vuln') === this.vulnFilter.kind);
      }
      if (this.vulnFilter.severity !== 'all') {
        list = list.filter(v => v.severity === this.vulnFilter.severity);
      }
      if (this.vulnFilter.status !== 'all') {
        list = list.filter(v => v.status === this.vulnFilter.status);
      }
      return list;
    },
    vulnSeverityClass(sev) {
      const map = {
        critical: 'bg-red-100 text-red-700 border-red-300',
        high: 'bg-orange-100 text-orange-700 border-orange-300',
        medium: 'bg-amber-100 text-amber-700 border-amber-300',
        low: 'bg-sky-100 text-sky-700 border-sky-300',
        info: 'bg-slate-100 text-slate-600 border-slate-300',
      };
      return map[sev] || map.info;
    },
    vulnStatusClass(status) {
      const map = {
        confirmed: 'bg-emerald-100 text-emerald-700 border-emerald-300',
        pending: 'bg-amber-100 text-amber-700 border-amber-300',
        dismissed: 'bg-slate-100 text-slate-400 border-slate-300',
      };
      return map[status] || map.pending;
    },
    vulnStatusLabel(status) {
      return { confirmed: '已确认', pending: '待审', dismissed: '已忽略' }[status] || status;
    },
    vulnSeverityLabel(sev) {
      return { critical: '严重', high: '高危', medium: '中危', low: '低危', info: '信息' }[sev] || sev;
    },
    openVulnDetail(vuln) {
      this.vulnDetail = vuln;
      this.showVulnDetail = true;
    },
    async updateVulnStatus(projectId, vulnId, newStatus) {
      try {
        await this.api('PATCH', `/projects/${projectId}/vulnerabilities/${vulnId}`, { status: newStatus });
        if (this.vulnDetail && this.vulnDetail.id === vulnId) {
          this.vulnDetail.status = newStatus;
          this.vulnDetail.verified_at = newStatus === 'confirmed' ? new Date().toISOString() : null;
        }
        // refresh list
        if (this.selectedProjectId) {
          await this.loadVulns(this.selectedProjectId);
        } else {
          await this.loadAllVulns();
        }
        await this.loadProjects();
        this.showToast(`漏洞状态已更新为「${this.vulnStatusLabel(newStatus)}」`);
      } catch (e) {
        this.showToast('更新失败: ' + e.message, 'error');
      }
    },
    async deleteVuln(projectId, vulnId) {
      try {
        await this.api('DELETE', `/projects/${projectId}/vulnerabilities/${vulnId}`);
        this.showVulnDetail = false;
        if (this.selectedProjectId) {
          await this.loadVulns(this.selectedProjectId);
        } else {
          await this.loadAllVulns();
        }
        await this.loadProjects();
        this.showToast('漏洞已删除');
      } catch (e) {
        this.showToast('删除失败: ' + e.message, 'error');
      }
    },
    recentProjects() {
      return [...this.projects]
        .sort((a, b) => String(b.created_at || '').localeCompare(String(a.created_at || '')))
        .slice(0, 6);
    },
    projectStatusDotClass(status) {
      const classes = {
        active: 'bg-teal-500',
        stopped: 'bg-amber-500',
        completed: 'bg-slate-400',
      };
      return classes[status] || 'bg-slate-400';
    },
    dashboardStatusBars() {
      const total = this.projects.length || 1;
      const defs = [
        { key: 'active', label: '运行中', dot: 'bg-teal-500', bar: 'bg-teal-500' },
        { key: 'stopped', label: '已暂停', dot: 'bg-amber-500', bar: 'bg-amber-500' },
        { key: 'completed', label: '已完成', dot: 'bg-emerald-500', bar: 'bg-emerald-500' },
      ];
      return defs.map(d => {
        const count = this.countProjectsByStatus(d.key);
        return { ...d, count, pct: Math.round((count / total) * 100) };
      });
    },

    hasActiveProjects() {
      return this.projects.some(project => project.status === 'active');
    },

    projectCanWriteHints() {
      return !this.replay.active && ['active', 'stopped', 'completed'].includes(this.project?.project?.status);
    },

    canActOnSelectedFacts() {
      return this.projectIsActive() && this.selectedFacts.length > 0 && !this.selectedFacts.includes('goal');
    },

    async loadProjects() {
      try { this.projects = await this.api('GET', '/projects'); } catch(e) { console.error(e); }
    },

    async loadVulnStats() {
      try { this.vulnStats = await this.api('GET', '/vulnerabilities/stats'); } catch(e) { console.error(e); }
    },

    async loadVulnTrend() {
      try { this.vulnTrend = await this.api('GET', '/vulnerabilities/trend?days=14'); } catch(e) { console.error(e); }
    },

    rememberProjectListScroll() {
      const scroller = this.$refs.projectListScroll;
      if (!scroller) return;
      this.projectListScrollTop = scroller.scrollTop;
    },

    restoreProjectListScroll() {
      if (!this.shouldRestoreProjectListScroll) return;
      this.$nextTick(() => {
        const scroller = this.$refs.projectListScroll;
        if (!scroller) return;
        const maxScrollTop = Math.max(0, scroller.scrollHeight - scroller.clientHeight);
        scroller.scrollTop = Math.min(this.projectListScrollTop, maxScrollTop);
        this.shouldRestoreProjectListScroll = false;
      });
    },

    escapeHtml(text) {
      return String(text ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
    },

    async copyText(text) {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(text);
        return;
      }
      const textarea = document.createElement('textarea');
      textarea.value = text;
      textarea.setAttribute('readonly', '');
      textarea.style.position = 'fixed';
      textarea.style.top = '0';
      textarea.style.left = '-9999px';
      textarea.style.opacity = '0';
      document.body.appendChild(textarea);
      textarea.select();
      textarea.setSelectionRange(0, textarea.value.length);
      try {
        if (!document.execCommand('copy')) {
          throw new Error('copy command rejected');
        }
      } finally {
        document.body.removeChild(textarea);
      }
    },

    // ── 时间/字节格式化 ──────────────────────────────────────────────────
    formatTime(ts) { if (!ts) return ''; return new Date(ts).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit',second:'2-digit'}); },
    formatDate(ts) { if (!ts) return ''; const d = new Date(ts); return d.toLocaleDateString([],{year:'numeric',month:'short',day:'numeric'}) + ' ' + d.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'}); },
    formatTimelineDate(ts) { if (!ts) return ''; return new Date(ts).toLocaleDateString([],{year:'numeric',month:'short',day:'numeric'}); },
    formatBytes(value) {
      const bytes = Number(value) || 0;
      if (bytes < 1024) return `${bytes} B`;
      const units = ['KB', 'MB', 'GB'];
      let size = bytes / 1024;
      let unit = units[0];
      for (let i = 1; i < units.length && size >= 1024; i += 1) {
        size /= 1024;
        unit = units[i];
      }
      return `${size >= 10 ? size.toFixed(1) : size.toFixed(2)} ${unit}`;
    },
    formatDurationMs(ms) {
      const totalSeconds = Math.max(0, Math.round(ms / 1000));
      const hours = Math.floor(totalSeconds / 3600);
      const minutes = Math.floor((totalSeconds % 3600) / 60);
      const seconds = totalSeconds % 60;
      if (hours > 0) return `${hours}小时 ${minutes}分`;
      if (minutes > 0) return `${minutes}分 ${seconds}秒`;
      return `${seconds}秒`;
    },

    // ── Dispatcher 状态工具 ──────────────────────────────────────────────
    dispatcherStatusAge(ts) {
      if (!ts) return '未知';
      const seconds = Math.max(0, Math.floor((Date.now() - new Date(ts).getTime()) / 1000));
      if (seconds < 60) return `${seconds}s 前`;
      const minutes = Math.floor(seconds / 60);
      if (minutes < 60) return `${minutes}m 前`;
      return `${Math.floor(minutes / 60)}h 前`;
    },
    dispatcherIsStale(ts) {
      if (!ts) return true;
      return (Date.now() - new Date(ts).getTime()) / 1000 > 60;
    },

    // ── 证据自动标签 ────────────────────────────────────────────────────
    factAutoTags(fact) {
      if (!fact || fact.id === 'origin' || fact.id === 'goal') return [];
      const text = ((fact.description || '') + ' ' + (fact.id || '')).toLowerCase();
      const tags = [];

      // 严重程度
      const sevMap = [
        { keys: ['严重','critical','rce','命令执行','远程代码','remote code','命令注入'], label: '严重', cls: 'bg-red-50 text-red-700 border-red-200' },
        { keys: ['高危','high','sql注入','sqli','sql injection','文件上传','任意文件','反序列化','xxe','ldap注入','模板注入','ssti','权限绕过','越权'], label: '高危', cls: 'bg-orange-50 text-orange-700 border-orange-200' },
        { keys: ['中危','medium','xss','ssrf','csrf','信息泄露','目录遍历','弱口令','暴力破解'], label: '中危', cls: 'bg-amber-50 text-amber-700 border-amber-200' },
        { keys: ['低危','low','信息收集','版本泄露','路径泄露','banner'], label: '低危', cls: 'bg-yellow-50 text-yellow-700 border-yellow-200' },
        { keys: ['信息','info','端口','服务','资产'], label: '信息', cls: 'bg-slate-100 text-slate-600 border-slate-200' },
      ];
      let sevMatched = false;
      for (const s of sevMap) {
        if (!sevMatched && s.keys.some(k => text.includes(k))) {
          tags.push({ label: s.label, cls: s.cls });
          sevMatched = true;
        }
      }

      // 漏洞类型
      const typeMap = [
        { keys: ['rce','命令执行','远程代码','command'],    label: 'RCE',    cls: 'bg-red-50 text-red-600 border-red-200' },
        { keys: ['sql注入','sqli','sql injection'],         label: 'SQLi',   cls: 'bg-purple-50 text-purple-700 border-purple-200' },
        { keys: ['xss','跨站脚本'],                         label: 'XSS',    cls: 'bg-pink-50 text-pink-700 border-pink-200' },
        { keys: ['ssrf','服务器请求伪造'],                   label: 'SSRF',   cls: 'bg-fuchsia-50 text-fuchsia-700 border-fuchsia-200' },
        { keys: ['csrf','跨站请求'],                         label: 'CSRF',   cls: 'bg-violet-50 text-violet-700 border-violet-200' },
        { keys: ['xxe','xml'],                               label: 'XXE',    cls: 'bg-indigo-50 text-indigo-700 border-indigo-200' },
        { keys: ['ssti','模板注入'],                         label: 'SSTI',   cls: 'bg-rose-50 text-rose-700 border-rose-200' },
        { keys: ['文件上传','upload'],                       label: '文件上传', cls: 'bg-orange-50 text-orange-600 border-orange-200' },
        { keys: ['目录遍历','path traversal','lfi','rfi'],  label: '路径穿越', cls: 'bg-amber-50 text-amber-600 border-amber-200' },
        { keys: ['反序列化','deserialization'],              label: '反序列化', cls: 'bg-red-50 text-red-600 border-red-200' },
        { keys: ['越权','水平越权','垂直越权','idor'],        label: '越权',   cls: 'bg-orange-50 text-orange-700 border-orange-200' },
        { keys: ['弱口令','暴力破解','brute'],               label: '弱口令', cls: 'bg-yellow-50 text-yellow-700 border-yellow-200' },
        { keys: ['信息泄露','泄露','leak'],                  label: '信息泄露', cls: 'bg-slate-50 text-slate-600 border-slate-200' },
      ];
      for (const t of typeMap) {
        if (t.keys.some(k => text.includes(k))) {
          tags.push({ label: t.label, cls: t.cls });
          if (tags.length >= 5) break; // 最多显示5个标签
        }
      }
      return tags;
    },

    // ── 行动心跳/超时 ────────────────────────────────────────────────────
    _intentTimeoutMs() {
      const t = this.settings?.intent_timeout ?? this.settingsForm?.intent_timeout ?? 5;
      return t * 60 * 1000;
    },
    intentHeartbeatAgeSec(intent) {
      void this._now; // 依赖 _now 触发响应式刷新
      if (!intent?.last_heartbeat_at) return null;
      return Math.max(0, Math.floor((Date.now() - new Date(intent.last_heartbeat_at).getTime()) / 1000));
    },
    intentHeartbeatPct(intent) {
      const age = this.intentHeartbeatAgeSec(intent);
      if (age === null) return 0;
      return Math.min(100, Math.round(age / (this._intentTimeoutMs() / 1000) * 100));
    },
    intentIsStale(intent) {
      if (!intent?.last_heartbeat_at) return false;
      void this._now;
      return (Date.now() - new Date(intent.last_heartbeat_at).getTime()) >= this._intentTimeoutMs();
    },
    intentHeartbeatAgeLabel(intent) {
      const s = this.intentHeartbeatAgeSec(intent);
      if (s === null) return '—';
      if (s < 60) return `${s}s 前`;
      const m = Math.floor(s / 60);
      if (m < 60) return `${m}m ${s % 60}s 前`;
      return `${Math.floor(m / 60)}h ${m % 60}m 前`;
    },
  });
}
