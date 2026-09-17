/**
 * app.chat.js — 通用 AI 对话 / 设置加载保存 / Secrets / Dispatcher 状态管理
 */
function applyChatModule(obj) {
  Object.assign(obj, {

    // ═══════════════════════════════════════════════════════════════════════
    //  Sharp 助手（通用 AI 对话）
    // ═══════════════════════════════════════════════════════════════════════

    async goChat() {
      if (this.view === 'graph') this.backToList(true);
      this.view = 'chat';
      await this.loadChatSessions();
    },

    async loadChatSessions() {
      try {
        const data = await this.api('GET', '/chat/sessions');
        this.chat.sessions = data.sessions || [];
      } catch(e) { console.error(e); }
    },

    startNewChatSelect() {
      // 从活跃会话退回新建选择区
      this.chat.sessionId = '';
      this.chat.messages = [];
      this.chat.projectId = '';
    },

    async confirmNewChat() {
      try {
        const data = await this.api('POST', '/chat/sessions', {
          role: this.chat.role,
          project_id: this.chat.projectId || null,
          context: '',
        });
        this.chat.sessionId = data.session_id;
        this.chat.messages = [];
        this.chat.role = data.role;
        this.chat.projectId = data.project_id || '';
        this.chat.streaming = false;
        this.chat.streamBuffer = '';
        await this.loadChatSessions();
      } catch(e) {
        this.showToast(e.message, 'error');
      }
    },

    async switchChatSession(id) {
      if (this.chat.streaming) this.stopChat();
      try {
        const data = await this.api('GET', `/chat/sessions/${id}/messages`);
        this.chat.sessionId = id;
        this.chat.role = data.role || 'assistant';
        this.chat.projectId = data.project_id || '';
        this.chat.messages = (data.messages || []).map((m, i) => ({
          id: `r_${i}`, role: m.role, content: m.content,
        }));
        this.chat.streaming = false;
        this.chat.streamBuffer = '';
      } catch(e) {
        this.showToast(e.message, 'error');
      }
    },

    deleteChatSession(id) {
      this.confirmDialog({
        title: '删除会话',
        message: '确定删除此会话？删除后对话历史不可恢复。',
        okText: '删除',
        danger: true,
        onOk: () => this._doDeleteChatSession(id),
      });
    },
    async _doDeleteChatSession(id) {
      try {
        await this.api('DELETE', `/chat/sessions/${id}`);
        if (this.chat.sessionId === id) {
          this.chat.sessionId = '';
          this.chat.messages = [];
        }
        await this.loadChatSessions();
      } catch(e) {
        this.showToast(e.message, 'error');
      }
    },

    deleteCurrentChat() {
      if (this.chat.sessionId) this.deleteChatSession(this.chat.sessionId);
    },

    async sendChatMessage() {
      const content = (this.chat.input || '').trim();
      if (!content || !this.chat.sessionId || this.chat.streaming) return;

      this.chat.input = '';
      this.chat._msgIdSeq++;
      this.chat.messages.push({ id: 'u_' + this.chat._msgIdSeq, role: 'user', content });
      this.chat.streaming = true;
      this.chat.streamBuffer = '';
      this.$nextTick(() => {
        const el = this.$refs.chatMessages;
        if (el) el.scrollTop = el.scrollHeight;
      });

      const ctrl = new AbortController();
      this.chat.abortController = ctrl;
      try {
        const resp = await fetch(`/chat/sessions/${this.chat.sessionId}/stream`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${this.authToken}` },
          body: JSON.stringify({ content }),
          signal: ctrl.signal,
        });
        if (!resp.ok) {
          const err = await resp.json().catch(() => ({}));
          throw new Error(err.detail || `HTTP ${resp.status}`);
        }
        const reader = resp.body.getReader();
        const dec = new TextDecoder();
        let buf = '';
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buf += dec.decode(value, { stream: true });
          const lines = buf.split('\n');
          buf = lines.pop();
          for (const line of lines) {
            if (!line.startsWith('data:')) continue;
            try {
              const ev = JSON.parse(line.slice(5).trim());
              if (ev.type === 'delta') {
                this.chat.streamBuffer += ev.text;
                this.$nextTick(() => {
                  const el = this.$refs.chatMessages;
                  if (el) el.scrollTop = el.scrollHeight;
                });
              } else if (ev.type === 'done') {
                this.chat._msgIdSeq++;
                this.chat.messages.push({
                  id: 'a_' + this.chat._msgIdSeq,
                  role: 'assistant',
                  content: this.chat.streamBuffer,
                });
                this.chat.streamBuffer = '';
              } else if (ev.type === 'error') {
                this.showToast(ev.message, 'error');
              }
            } catch (_) { /* ignore parse errors */ }
          }
        }
      } catch (e) {
        if (e.name !== 'AbortError') this.showToast(e.message, 'error');
      } finally {
        this.chat.streaming = false;
        this.chat.streamBuffer = '';
        this.chat.abortController = null;
      }
    },

    stopChat() {
      if (this.chat.abortController) {
        this.chat.abortController.abort();
        this.chat.abortController = null;
      }
      this.chat.streaming = false;
      this.chat.streamBuffer = '';
    },

    // ═══════════════════════════════════════════════════════════════════════
    //  设置 / Secrets / Dispatcher 加载
    // ═══════════════════════════════════════════════════════════════════════

    async loadSettings() {
      try {
        const s = await this.api('GET', '/settings');
        this.settingsForm.intent_timeout = s.intent_timeout;
        this.settingsForm.reason_timeout = s.reason_timeout;
        this.settingsForm.report_instructions = s.report_instructions || '';
      } catch(e) { console.error(e); }
      await this.loadMcpServers();
      await this.loadServerSecrets();
    },

    async loadDispatcherStatus() {
      try {
        this.dispatcherStatusLoading = true;
        const data = await this.api('GET', '/dispatcher/status');
        this.dispatcherStatus = data || [];
      } catch(e) {
        this.showToast('读取 Dispatcher 状态失败：' + e.message, 'error');
      } finally {
        this.dispatcherStatusLoading = false;
      }
    },

    async restartDispatcher() {
      if (this.dispatcherRestarting) return;
      this.dispatcherRestarting = true;
      try {
        const data = await this.api('POST', '/dispatcher/restart');
        if (data && data.message) {
          // 仅停止+请手动重启 launcher 属预期流程用 info；真失败（找不到配置等）才 error
          const kind = (data.spawned || data.action === 'relaunch_launcher') ? 'info' : 'error';
          this.showToast(data.message, kind);
        } else {
          this.showToast('Dispatcher 已重启');
        }
        // 重启后拉取一次最新状态，覆盖心跳快照
        await this.loadDispatcherStatus();
      } catch(e) {
        this.showToast('重启 Dispatcher 失败：' + e.message, 'error');
      } finally {
        this.dispatcherRestarting = false;
      }
    },

    deleteDispatcherStatus(dispatcherId) {
      if (!dispatcherId) return;
      this.confirmDialog({
        title: '删除 Dispatcher 状态记录',
        message: `确定删除此 Dispatcher 状态记录（${dispatcherId}）？`,
        okText: '删除',
        danger: true,
        onOk: () => this._doDeleteDispatcherStatus(dispatcherId),
      });
    },
    async _doDeleteDispatcherStatus(dispatcherId) {
      try {
        await this.api('DELETE', `/dispatcher/status/${encodeURIComponent(dispatcherId)}`);
        this.showToast('已删除 Dispatcher 状态记录');
        // 从本地列表移除，避免整表刷新闪烁
        this.dispatcherStatus = this.dispatcherStatus.filter(e => e.dispatcher_id !== dispatcherId);
      } catch(e) {
        this.showToast('删除失败：' + e.message, 'error');
      }
    },

    // Dispatcher Worker 状态徽章颜色
    dispatcherWorkerBadgeClass(worker) {
      if (!worker) return 'bg-slate-100 text-slate-500';
      if (worker.running > 0) return 'bg-emerald-100 text-emerald-700';
      return 'bg-slate-100 text-slate-500';
    },
    // Dispatcher Worker 状态文字
    dispatcherWorkerState(worker) {
      if (!worker) return '—';
      if (worker.running > 0) return '运行中';
      return '空闲';
    },

    // 保存本地偏好（操作者名 + 默认布局），仅写入浏览器 localStorage
    saveLocalSettings() {
      try {
        // 复用已有的持久化逻辑，并把布局同步到当前图布局
        this.saveLocalPrefs();
        this.layoutMode = this.localPrefs.layout_mode;
        if (this.view === 'graph' && this.cy) this.applySelectedLayout();
        this.showToast('本地设置已保存');
        this.showLocalPrefs = false;
      } catch(e) {
        this.showToast('保存失败：' + e.message, 'error');
      }
    },

    // 保存服务端设置（intent_timeout / reason_timeout / report_instructions）
    async saveServerSettings() {
      try {
        await this.api('PUT', '/settings', {
          intent_timeout: this.settingsForm.intent_timeout,
          reason_timeout: this.settingsForm.reason_timeout,
          report_instructions: this.settingsForm.report_instructions || '',
        });
        this.showToast('服务端设置已保存');
      } catch(e) {
        this.showToast('保存失败：' + e.message, 'error');
      }
    },

    // ═══════════════════════════════════════════════════════════════════════
    //  MCP 服务器配置（设置页）
    // ═══════════════════════════════════════════════════════════════════════

    async loadMcpServers() {
      try {
        this.mcpLoading = true;
        const data = await this.api('GET', '/settings/mcp');
        this.mcpServers = (data.servers || []).map(s => this._normalizeMcpServer(s));
      } catch(e) {
        // 后端不支持（旧版本）时静默，不影响设置页其他功能
        console.error(e);
      } finally {
        this.mcpLoading = false;
      }
    },

    _normalizeMcpServer(s) {
      return {
        name: s.name || '',
        transport: s.transport || 'http',
        url: s.url || '',
        headers: this._dictToList(s.headers || {}),
        command: s.command || '',
        args: s.args || [],
        env: this._dictToList(s.env || {}),
        enabled: s.enabled !== false,
        max_tools: s.max_tools ?? 100,
      };
    },

    _dictToList(d) {
      return Object.entries(d).map(([k, v]) => ({ key: k, value: v }));
    },

    _listToDict(list) {
      const d = {};
      for (const item of list || []) {
        const k = (item.key || '').trim();
        if (k) d[k] = (item.value || '').trim();
      }
      return d;
    },

    _emptyMcpForm() {
      return {
        name: '', transport: 'http', url: '',
        headers: [], command: '', args: [], env: [],
        enabled: true, max_tools: 100,
      };
    },

    addMcpServer() {
      this.mcpEditor.show = true;
      this.mcpEditor.editing = false;
      this.mcpEditor.idx = -1;
      this.mcpEditor.form = this._emptyMcpForm();
    },

    editMcpServer(idx) {
      const s = this.mcpServers[idx];
      if (!s) return;
      this.mcpEditor.show = true;
      this.mcpEditor.editing = true;
      this.mcpEditor.idx = idx;
      this.mcpEditor.form = JSON.parse(JSON.stringify(s));
    },

    removeMcpServer(idx) {
      this.mcpServers.splice(idx, 1);
    },

    cancelMcpEdit() {
      this.mcpEditor.show = false;
      this.mcpEditor.editing = false;
      this.mcpEditor.idx = -1;
      this.mcpEditor.form = this._emptyMcpForm();
    },

    async saveMcpServer() {
      const f = this.mcpEditor.form;
      if (!f.name.trim()) { this.showToast('MCP 服务器名称不能为空', 'error'); return; }
      if (f.transport === 'http' && !f.url.trim()) { this.showToast('HTTP 型服务器必须填写 URL', 'error'); return; }
      if (f.transport === 'stdio' && !f.command.trim()) { this.showToast('stdio 型服务器必须填写 command', 'error'); return; }

      const server = {
        name: f.name.trim(),
        transport: f.transport,
        url: f.transport === 'http' ? f.url.trim() : null,
        headers: this._listToDict(f.headers),
        command: f.transport === 'stdio' ? f.command.trim() : null,
        args: (f.args || []).map(a => a.trim()).filter(Boolean),
        env: this._listToDict(f.env),
        enabled: !!f.enabled,
        max_tools: parseInt(f.max_tools, 10) || 100,
      };

      if (this.mcpEditor.editing && this.mcpEditor.idx >= 0) {
        this.mcpServers[this.mcpEditor.idx] = this._normalizeMcpServer(server);
      } else {
        // 新增：name 冲突检查
        if (this.mcpServers.some(s => s.name === server.name)) {
          this.showToast('已存在同名 MCP 服务器', 'error'); return;
        }
        this.mcpServers.push(this._normalizeMcpServer(server));
      }
      this.cancelMcpEdit();
    },

    async saveMcpServers() {
      try {
        this.mcpSaving = true;
        const payload = { servers: this.mcpServers.map(s => ({
          name: s.name,
          transport: s.transport,
          url: s.transport === 'http' ? s.url : null,
          headers: this._listToDict(s.headers),
          command: s.transport === 'stdio' ? s.command : null,
          args: s.args || [],
          env: this._listToDict(s.env),
          enabled: !!s.enabled,
          max_tools: parseInt(s.max_tools, 10) || 100,
        })) };
        await this.api('PUT', '/settings/mcp', payload);
        this.showToast('MCP 配置已保存，重启 Dispatcher 后生效');
      } catch(e) {
        this.showToast('保存失败：' + e.message, 'error');
      } finally {
        this.mcpSaving = false;
      }
    },

    // 便捷：headers / env 列表项操作
    addMcpHeader() { this.mcpEditor.form.headers.push({ key: '', value: '' }); },
    removeMcpHeader(i) { this.mcpEditor.form.headers.splice(i, 1); },
    addMcpEnv() { this.mcpEditor.form.env.push({ key: '', value: '' }); },
    removeMcpEnv(i) { this.mcpEditor.form.env.splice(i, 1); },
    addMcpArg() { this.mcpEditor.form.args.push(''); },
    removeMcpArg(i) { this.mcpEditor.form.args.splice(i, 1); },

    // 加载服务器端 secrets 状态（GET /settings/secrets → { path, values: [{key, configured, source, masked}] }）
    async loadServerSecrets() {
      try {
        const data = await this.api('GET', '/settings/secrets');
        this.secretsStatus = data.values || [];
        this.secretsPath = data.path || '';
        // 回显激活模式：SHARP_WORKER_MODE 是明文 key（masked=原值）
        const modeItem = (this.secretsStatus || []).find(x => x.key === 'SHARP_WORKER_MODE' && x.configured && x.masked);
        if (modeItem && ['anthropic', 'openai', 'inflection'].includes(modeItem.masked)) {
          this.dispatcherMode = modeItem.masked;
        }
      } catch(e) { console.error(e); }
    },

    // 检查某个 secret key 是否已配置（环境变量或文件中存在值）
    secretConfigured(key) {
      const item = this.secretsStatus.find(s => s.key === key);
      return item ? item.configured : false;
    },

    // 返回某个 secret key 的掩码值（如 "sk-1...abcd"），未配置时返回空串
    secretMasked(key) {
      const item = this.secretsStatus.find(s => s.key === key);
      return item ? (item.masked || '') : '';
    },

    async saveServerSecrets() {
      const values = {};

      // 根据选中的 dispatcher 模式，收集对应的配置字段
      if (this.dispatcherMode === 'anthropic') {
        const token = this.secretsForm.SHARP_ANTHROPIC_AUTH_TOKEN.trim();
        const model = this.secretsForm.SHARP_MODEL.trim();
        const baseUrl = this.secretsForm.SHARP_BASE_URL.trim();
        if (token) values.SHARP_ANTHROPIC_AUTH_TOKEN = token;
        if (model) values.SHARP_MODEL = model;
        if (baseUrl) values.SHARP_BASE_URL = baseUrl;
      } else if (this.dispatcherMode === 'openai') {
        const token = this.secretsForm.OPENAI_API_KEY.trim();
        const model = this.secretsForm.OPENAI_MODEL.trim();
        const baseUrl = this.secretsForm.OPENAI_BASE_URL.trim();
        if (token) values.OPENAI_API_KEY = token;
        if (model) values.OPENAI_MODEL = model;
        if (baseUrl) values.OPENAI_BASE_URL = baseUrl;
      } else if (this.dispatcherMode === 'inflection') {
        const token = this.secretsForm.PI_API_KEY.trim();
        const model = this.secretsForm.PI_MODEL.trim();
        const baseUrl = this.secretsForm.PI_BASE_URL.trim();
        if (token) values.PI_API_KEY = token;
        if (model) values.PI_MODEL = model;
        if (baseUrl) values.PI_BASE_URL = baseUrl;
      }

      // 激活模式本身也持久化：dispatcher 按 SHARP_WORKER_MODE 只激活对应
      // worker 类型（选 claude → 只跑 claudecode；选 openai → 只跑 codex）。
      values.SHARP_WORKER_MODE = this.dispatcherMode || 'all';

      if (Object.keys(values).length === 0) return;

      try {
        const data = await this.api('PUT', '/settings/secrets', { values });
        this.secretsStatus = data.values || [];
        this.secretsPath = data.path || this.secretsPath;
        // 清空当前模式下的表单字段
        if (this.dispatcherMode === 'anthropic') {
          this.secretsForm.SHARP_ANTHROPIC_AUTH_TOKEN = '';
          this.secretsForm.SHARP_MODEL = '';
          this.secretsForm.SHARP_BASE_URL = '';
        } else if (this.dispatcherMode === 'openai') {
          this.secretsForm.OPENAI_API_KEY = '';
          this.secretsForm.OPENAI_MODEL = '';
          this.secretsForm.OPENAI_BASE_URL = '';
        } else if (this.dispatcherMode === 'inflection') {
          this.secretsForm.PI_API_KEY = '';
          this.secretsForm.PI_MODEL = '';
          this.secretsForm.PI_BASE_URL = '';
        }
        this.showToast('配置已保存，重启 Dispatcher 后生效');
      } catch(e) { this.showToast(e.message, 'error'); }
    },

  });
}