/**
 * app.analyzers.js — 小程序分析 + Android 分析 + Android Chat + 派发 + 筛选
 */
function applyAnalyzersModule(obj) {
  Object.assign(obj, {

    // ═══════════════════════════════════════════════════════════════════════
    //  小程序分析
    // ═══════════════════════════════════════════════════════════════════════

    openMiniProgramAnalyzer() {
      this.showMiniProgramAnalyzer = true;
    },

    selectMiniProgramFile(event) {
      const file = event.target.files?.[0] || null;
      this.miniProgram.file = file;
      this.miniProgram.filename = file ? file.name : '';
      this.miniProgram.result = null;
      this.miniProgram.selectedPath = '';
      this.miniProgram.source = 'upload';
      this.miniProgram.query = '';
      this.miniProgram.tab = 'findings';
    },

    resetMiniProgramAnalysis() {
      this.miniProgram = {
        file: null,
        filename: '',
        loading: false,
        scanning: false,
        result: null,
        tab: 'findings',
        query: '',
        packages: [],
        selectedPath: '',
        scanSummary: '',
        source: 'upload',
        unpacking: false,
        unpackOutputDir: '',
        unpackResult: null,
        dispatching: false,
        dispatchResult: null,
        harFile: null,
        harFilename: '',
        harDispatching: false,
        harDispatchResult: null,
        harTargetProject: 'auto',
      };
      if (this.$refs.miniProgramFileInput) this.$refs.miniProgramFileInput.value = '';
      if (this.$refs.harFileInput) this.$refs.harFileInput.value = '';
    },

    async analyzeMiniProgram() {
      if (!this.miniProgram.file || this.miniProgram.loading) return;
      if (this.miniProgram.file.size > 80 * 1024 * 1024) {
        this.showToast('文件超过 80MB', 'error');
        return;
      }
      this.miniProgram.loading = true;
      try {
        const params = new URLSearchParams({ filename: this.miniProgram.filename || 'upload.wxapkg' });
        const r = await fetch(`/miniprogram/wxapkg/analyze?${params.toString()}`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/octet-stream', 'Authorization': `Bearer ${this.authToken}` },
          body: this.miniProgram.file,
          signal: this._timeoutSignal(600000),
        });
        const data = await r.json();
        if (!r.ok) {
          let msg = `HTTP ${r.status}`;
          if (typeof data.detail === 'string') msg = data.detail;
          else if (Array.isArray(data.detail)) msg = data.detail.map(e => e.msg).join('; ');
          throw new Error(msg);
        }
        this.miniProgram.result = data;
        this.miniProgram.tab = data.findings?.length ? 'findings' : 'domains';
        this.miniProgram.query = '';
        this.miniProgram.source = 'upload';
        this.showToast('小程序包分析完成');
      } catch (e) {
        this.showToast(e.message, 'error');
      } finally {
        this.miniProgram.loading = false;
      }
    },

    async scanMiniProgramPackages() {
      if (this.miniProgram.scanning) return;
      this.miniProgram.scanning = true;
      try {
        const data = await this.api('GET', '/miniprogram/wxapkg/scan?limit=300');
        this.miniProgram.packages = data.packages || [];
        this.miniProgram.scanSummary = `${data.platform || 'unknown'} · 找到 ${data.count || 0} 个包 · 扫描 ${data.roots?.length || 0} 个目录${data.truncated ? ' · 已截断' : ''}`;
        if (!this.miniProgram.unpackOutputDir && data.default_output_dir) {
          this.miniProgram.unpackOutputDir = data.default_output_dir;
        }
        if (!this.miniProgram.selectedPath && this.miniProgram.packages.length > 0) {
          this.selectScannedMiniProgramPackage(this.miniProgram.packages[0], { quiet: true });
        }
        this.showToast(this.miniProgram.packages.length ? `找到 ${this.miniProgram.packages.length} 个 wxapkg` : '没有找到 wxapkg');
      } catch (e) {
        this.showToast(e.message, 'error');
      } finally {
        this.miniProgram.scanning = false;
      }
    },

    selectScannedMiniProgramPackage(pkg, options = {}) {
      if (!pkg?.path) return;
      this.miniProgram.selectedPath = pkg.path;
      this.miniProgram.filename = pkg.name || pkg.path.split('/').pop() || '';
      this.miniProgram.file = null;
      this.miniProgram.source = 'scan';
      this.miniProgram.unpackResult = null;
      this.miniProgram.dispatchResult = null;
      if (this.$refs.miniProgramFileInput) this.$refs.miniProgramFileInput.value = '';
      if (!options.quiet) this.showToast('已选择微信缓存包');
    },

    async analyzeSelectedMiniProgramPackage() {
      if (!this.miniProgram.selectedPath || this.miniProgram.loading) return;
      this.miniProgram.loading = true;
      this.miniProgram.source = 'scan';
      try {
        const data = await this.api('POST', '/miniprogram/wxapkg/analyze-path', {
          path: this.miniProgram.selectedPath,
        });
        this.miniProgram.result = data;
        this.miniProgram.tab = data.findings?.length ? 'findings' : 'domains';
        this.miniProgram.query = '';
        this.showToast('缓存包分析完成');
      } catch (e) {
        this.showToast(e.message, 'error');
      } finally {
        this.miniProgram.loading = false;
      }
    },

    async unpackSelectedMiniProgramPackage() {
      if (!this.miniProgram.selectedPath || this.miniProgram.unpacking) return;
      this.miniProgram.unpacking = true;
      this.miniProgram.source = 'scan';
      try {
        const data = await this.api('POST', '/miniprogram/wxapkg/unpack-path', {
          path: this.miniProgram.selectedPath,
          output_dir: this.miniProgram.unpackOutputDir || '',
          decrypt: true,
        });
        this.miniProgram.unpackResult = data;
        if (!this.miniProgram.unpackOutputDir && data.output_dir) {
          this.miniProgram.unpackOutputDir = data.output_dir;
        }
        this.showToast(`已解包 ${data.written_count || 0} 个文件`);
      } catch (e) {
        this.showToast(e.message, 'error');
      } finally {
        this.miniProgram.unpacking = false;
      }
    },

    async dispatchMiniProgramStaticAnalysis() {
      if (!this.miniProgram.selectedPath || this.miniProgram.dispatching) return;
      this.miniProgram.dispatching = true;
      this.miniProgram.source = 'scan';
      try {
        const projectId = this.projectIsActive() ? this.selectedProjectId : '';
        const data = await this.api('POST', '/miniprogram/wxapkg/dispatch-static-analysis', {
          path: this.miniProgram.selectedPath,
          project_id: projectId,
          creator: this.actorName(),
        });
        this.miniProgram.dispatchResult = data;
        this.showMiniProgramAnalyzer = false;
        await this.loadProjects();
        await this.openProject(data.project_id);
        this.showToast(data.created_project ? '已创建项目并发送 AI 分析' : '已发送到当前项目 AI 分析');
      } catch (e) {
        this.showToast(e.message, 'error');
      } finally {
        this.miniProgram.dispatching = false;
      }
    },

    selectHarFile(event) {
      const file = event?.target?.files?.[0] || null;
      this.miniProgram.harFile = file;
      this.miniProgram.harFilename = file ? file.name : '';
      this.miniProgram.harDispatchResult = null;
    },

    async dispatchHarDynamicAnalysis() {
      if (!this.miniProgram.harFile || this.miniProgram.harDispatching) return;
      if (this.miniProgram.harFile.size > 80 * 1024 * 1024) {
        this.showToast('HAR 文件超过 80MB', 'error'); return;
      }
      this.miniProgram.harDispatching = true;
      try {
        const filename = this.miniProgram.harFilename || 'capture.har';
        // Determine target project from the selector
        let targetProject;
        if (this.miniProgram.harTargetProject === 'auto') {
          targetProject = this.miniProgram.dispatchResult?.project_id ||
                          (this.projectIsActive() ? this.selectedProjectId : '');
        } else if (this.miniProgram.harTargetProject === 'new') {
          targetProject = '';
        } else {
          targetProject = this.miniProgram.harTargetProject;
        }
        const params = new URLSearchParams({ filename, creator: this.actorName() });
        let endpoint;
        if (targetProject) {
          params.set('project_id', targetProject);
          endpoint = `/miniprogram/traffic/import?${params}`;
        } else {
          endpoint = `/miniprogram/traffic/dispatch-dynamic-analysis?${params}`;
        }
        const r = await fetch(endpoint, {
          method: 'POST',
          headers: { 'Authorization': `Bearer ${this.authToken}` },
          body: this.miniProgram.harFile,
          signal: this._timeoutSignal(600000),
        });
        if (!r.ok) {
          const err = await r.json().catch(() => ({}));
          throw new Error(err.detail || r.statusText);
        }
        const data = await r.json();
        this.miniProgram.harDispatchResult = data;
        const msg = data.created_project
          ? '已创建动态分析项目并发送 AI'
          : '已注入到现有项目，AI 将联动静态+动态分析';
        this.showToast(msg);
        await this.loadProjects();
        if (data.project_id && !this.selectedProjectId) {
          this.openProject(data.project_id);
        }
      } catch (e) {
        this.showToast(e.message || 'HAR 导入失败', 'error');
      } finally {
        this.miniProgram.harDispatching = false;
        if (this.$refs.harFileInput) this.$refs.harFileInput.value = '';
      }
    },

    // ═══════════════════════════════════════════════════════════════════════
    //  Android 分析
    // ═══════════════════════════════════════════════════════════════════════

    openAndroidAnalyzer() {
      this.showAndroidAnalyzer = true;
      if (!this.android.browser.loaded) this.browseAndroidDir('');
    },

    resetAndroidAnalysis() {
      localStorage.removeItem('sharp_android_chat');
      this.androidChat = {
        sessionId: '', apkFilename: '', messages: [], input: '',
        streaming: false, streamBuffer: '', abortController: null,
        linkedProjectId: '', _msgIdSeq: 0, sessions: [], showHistory: false,
        // 动态调试（dynamic mode）：容器内 claude + adb/frida
        dynamicMode: false,
        dynamicStarting: false,
        dynamicContainer: '',
        dynamicDevice: '',
        dynamicReady: false,
      };
      this.android = {
        path: '',
        loading: false,
        uiPane: 'result',          // 'result' | 'chat'（右栏子视图切换）
        uploading: false,
        result: null,
        tab: 'domains',
        query: '',
        dispatching: false,
        dispatchResult: null,
        browser: {
          loaded: false,
          loading: false,
          rel: '',
          crumbs: [],
          parent_rel: '',
          at_home: true,
          dirs: [],
          files: [],
          truncated: false,
        },
      };
    },

    async browseAndroidDir(rel) {
      if (this.android.browser.loading) return;
      this.android.browser.loading = true;
      try {
        const params = new URLSearchParams({ path: rel || '' });
        const data = await this.api('GET', `/android/fs/list?${params.toString()}`);
        this.android.browser.rel = data.rel || '';
        this.android.browser.crumbs = data.crumbs || [];
        this.android.browser.parent_rel = data.parent_rel || '';
        this.android.browser.at_home = !!data.at_home;
        this.android.browser.dirs = data.dirs || [];
        this.android.browser.files = data.files || [];
        this.android.browser.truncated = !!data.truncated;
        this.android.browser.loaded = true;
      } catch (e) {
        this.showToast(e.message, 'error');
      } finally {
        this.android.browser.loading = false;
      }
    },

    selectAndroidApk(file) {
      if (!file?.abs) return;
      this.android.path = file.abs;
      this.showToast(`已选择 ${file.name}`);
    },

    androidTabs() {
      const result = this.android.result || {};
      return [
        { id: 'domains', label: '域名', count: (result.domains || []).length },
        { id: 'endpoints', label: '接口', count: (result.endpoints || []).length },
        { id: 'secrets', label: '疑似密钥', count: (result.secrets || []).length },
        { id: 'libs', label: 'Native 库', count: (result.native_libs || []).length },
      ];
    },

    androidQuery() {
      return (this.android.query || '').trim().toLowerCase();
    },

    androidMatches(...values) {
      const q = this.androidQuery();
      if (!q) return true;
      return values.some(value => String(value ?? '').toLowerCase().includes(q));
    },

    androidFilteredDomains() {
      const rows = this.android.result?.domains || [];
      return rows.filter(item => this.androidMatches(item.host, item.scheme));
    },

    androidFilteredEndpoints() {
      const rows = this.android.result?.endpoints || [];
      return rows.filter(item => this.androidMatches(item.path));
    },

    androidFilteredSecrets() {
      const rows = this.android.result?.secrets || [];
      return rows.filter(item => this.androidMatches(item));
    },

    androidFilteredLibs() {
      const rows = this.android.result?.native_libs || [];
      return rows.filter(item => this.androidMatches(item.name, item.abi, item.path));
    },

    async analyzeAndroidApk() {
      const path = (this.android.path || '').trim();
      if (!path || this.android.loading) return;
      this.android.loading = true;
      try {
        const data = await this.api('POST', '/android/apk/analyze-path', { path });
        this.android.result = data;
        this.showToast('APK 浅层分析完成');
        // 自动初始化 AI 对话会话
        await this.initAndroidChatSession();
      } catch (e) {
        this.showToast(e.message, 'error');
      } finally {
        this.android.loading = false;
      }
    },

    async uploadAndroidApk(event) {
      const file = event.target.files?.[0];
      if (!file) return;
      if (this.android.uploading || this.android.loading) return;
      this.android.uploading = true;
      try {
        const resp = await fetch(
          `/android/apk/upload?filename=${encodeURIComponent(file.name)}`,
          { method: 'POST', headers: { 'Authorization': `Bearer ${this.authToken}` }, body: file,
            signal: this._timeoutSignal(600000) }
        );
        if (!resp.ok) {
          const err = await resp.json().catch(() => ({}));
          throw new Error(err.detail || `上传失败 ${resp.status}`);
        }
        const data = await resp.json();
        this.android.path = data.saved_path;
        this.android.result = data;
        this.showToast(`${file.name} 上传并分析完成`);
        await this.initAndroidChatSession();
      } catch (e) {
        this.showToast(e.message, 'error');
      } finally {
        this.android.uploading = false;
        event.target.value = '';   // 允许重复选同一文件
      }
    },

    // ═══════════════════════════════════════════════════════════════════════
    //  Android Chat
    // ═══════════════════════════════════════════════════════════════════════

    async initAndroidChatSession() {
      const path = (this.android.path || '').trim();
      if (!path) return;
      try {
        const data = await this.api('POST', '/android/chat/sessions', { apk_path: path });
        this.androidChat.sessionId = data.session_id;
        this.androidChat.apkFilename = data.apk_filename || '';
        this.androidChat.messages = [];
        this.androidChat.streamBuffer = '';
        // 持久化到 localStorage，刷新后可恢复
        localStorage.setItem('sharp_android_chat', JSON.stringify({
          sessionId: data.session_id,
          apkFilename: data.apk_filename || '',
          apkPath: path,
        }));
        // 刷新后若该会话曾处于 dynamic 模式，恢复状态展示
        await this.restoreDynamicStatus(data.session_id);
      } catch (e) {
        this.showToast('AI 会话初始化失败：' + e.message, 'error');
      }
    },

    // ── 动态调试（dynamic mode）─────────────────────────────────────────
    // 在容器内跑 claude-code（bash + adb/frida）逐轮续接，让 AI 真实连真机。
    async restoreDynamicStatus(sessionId) {
      if (!sessionId) return;
      try {
        const st = await this.api('GET', `/android/chat/sessions/${sessionId}/dynamic/status`);
        this.androidChat.dynamicMode = st.mode === 'dynamic';
        this.androidChat.dynamicContainer = st.container_name || '';
        this.androidChat.dynamicDevice = st.device_info || '';
        this.androidChat.dynamicReady = this.androidChat.dynamicMode && !!st.container_name;
      } catch (_) { /* 非 dynamic 会话 404 或其它 —— 忽略 */ }
    },

    async startDynamicChat() {
      const sid = this.androidChat.sessionId;
      if (!sid || this.androidChat.dynamicStarting) return;
      this.androidChat.dynamicStarting = true;
      try {
        // start 返回 SSE 首轮引导流；这里用 fetch 消费到 done，把引导回复加进消息列表
        const resp = await fetch(`/android/chat/sessions/${sid}/dynamic/start`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${this.authToken}` },
          body: JSON.stringify({ prompt: '', device_info: '' }),
          signal: this._timeoutSignal(600000),
        });
        if (!resp.ok) {
          const err = await resp.json().catch(() => ({}));
          throw new Error(err.detail || `HTTP ${resp.status}`);
        }
        this.androidChat.dynamicMode = true;
        this.androidChat.dynamicReady = true;
        this._streamDynamicResponse(resp);
      } catch (e) {
        this.showToast('动态调试启动失败：' + e.message, 'error');
        this.androidChat.dynamicStarting = false;
      }
    },

    async stopDynamicChat() {
      const sid = this.androidChat.sessionId;
      if (!sid) return;
      try {
        await this.api('POST', `/android/chat/sessions/${sid}/dynamic/stop`, {});
        this.androidChat.dynamicMode = false;
        this.androidChat.dynamicContainer = '';
        this.androidChat.dynamicDevice = '';
        this.androidChat.dynamicReady = false;
        this.showToast('已退出动态调试（容器已释放）');
      } catch (e) {
        this.showToast('退出动态调试失败：' + e.message, 'error');
      }
    },

    async _streamDynamicResponse(resp) {
      // 复用 sendAndroidChatMessage 的 SSE 消费逻辑：start 的首轮引导已由服务端
      // 以 user 消息落库，这里只把 assistant 流追加到 UI。置 streaming 以锁输入。
      this.androidChat.streaming = true;
      this.androidChat.streamBuffer = '';
      try {
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
                this.androidChat.streamBuffer += ev.text;
                this.$nextTick(() => {
                  const el = this.$refs.androidChatMessages;
                  if (el) el.scrollTop = el.scrollHeight;
                });
              } else if (ev.type === 'done') {
                this.androidChat._msgIdSeq++;
                if (this.androidChat.streamBuffer) {
                  this.androidChat.messages.push({
                    id: 'a_' + this.androidChat._msgIdSeq,
                    role: 'assistant',
                    content: this.androidChat.streamBuffer,
                  });
                }
                this.androidChat.streamBuffer = '';
              } else if (ev.type === 'error') {
                this.showToast(ev.message, 'error');
              }
            } catch (_) { /* ignore */ }
          }
        }
      } catch (e) {
        if (e.name !== 'AbortError') this.showToast(e.message, 'error');
      } finally {
        this.androidChat.streaming = false;
        this.androidChat.dynamicStarting = false;
        this.$nextTick(() => {
          const el = this.$refs.androidChatMessages;
          if (el) el.scrollTop = el.scrollHeight;
        });
      }
    },

    async sendAndroidChatMessage() {
      const content = this.androidChat.input.trim();
      if (!content || !this.androidChat.sessionId || this.androidChat.streaming) return;
      this.androidChat.input = '';
      // 插入用户消息
      this.androidChat._msgIdSeq++;
      this.androidChat.messages.push({ id: 'u_' + this.androidChat._msgIdSeq, role: 'user', content });
      this.androidChat.streaming = true;
      this.androidChat.streamBuffer = '';
      this.$nextTick(() => {
        const el = this.$refs.androidChatMessages;
        if (el) el.scrollTop = el.scrollHeight;
      });
      const ctrl = new AbortController();
      this.androidChat.abortController = ctrl;
      // dynamic 模式走容器内 claude 续接端点，chat 模式走无状态 LLM
      const streamPath = this.androidChat.dynamicMode ? 'dynamic/stream' : 'stream';
      try {
        const resp = await fetch(`/android/chat/sessions/${this.androidChat.sessionId}/${streamPath}`, {
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
                this.androidChat.streamBuffer += ev.text;
                this.$nextTick(() => {
                  const el = this.$refs.androidChatMessages;
                  if (el) el.scrollTop = el.scrollHeight;
                });
              } else if (ev.type === 'done') {
                this.androidChat._msgIdSeq++;
                this.androidChat.messages.push({
                  id: 'a_' + this.androidChat._msgIdSeq,
                  role: 'assistant',
                  content: this.androidChat.streamBuffer,
                });
                this.androidChat.streamBuffer = '';
              } else if (ev.type === 'error') {
                this.showToast(ev.message, 'error');
              }
            } catch (_) { /* ignore parse errors */ }
          }
        }
      } catch (e) {
        if (e.name !== 'AbortError') this.showToast(e.message, 'error');
      } finally {
        this.androidChat.streaming = false;
        this.androidChat.abortController = null;
        this.$nextTick(() => {
          const el = this.$refs.androidChatMessages;
          if (el) el.scrollTop = el.scrollHeight;
        });
      }
    },

    stopAndroidChat() {
      if (this.androidChat.abortController) {
        this.androidChat.abortController.abort();
      }
      if (this.androidChat.streamBuffer) {
        this.androidChat._msgIdSeq++;
        this.androidChat.messages.push({
          id: 'a_' + this.androidChat._msgIdSeq,
          role: 'assistant',
          content: this.androidChat.streamBuffer + ' _(已停止)_',
        });
        this.androidChat.streamBuffer = '';
      }
      this.androidChat.streaming = false;
    },

    async clearAndroidChat() {
      if (!this.androidChat.sessionId) return;
      try {
        await this.api('DELETE', `/android/chat/sessions/${this.androidChat.sessionId}`);
        this.androidChat.messages = [];
        this.androidChat.streamBuffer = '';
        localStorage.removeItem('sharp_android_chat');
        this.showToast('对话已清空');
      } catch (e) {
        this.showToast(e.message, 'error');
      }
    },

    async loadAndroidChatSessions() {
      try {
        const data = await this.api('GET', '/android/chat/sessions');
        this.androidChat.sessions = data.sessions || [];
      } catch (e) { /* ignore */ }
    },

    async switchAndroidChatSession(sessionId, apkFilename) {
      this.androidChat.showHistory = false;
      if (sessionId === this.androidChat.sessionId) return;
      try {
        const data = await this.api('GET', `/android/chat/sessions/${sessionId}/messages`);
        this.androidChat.sessionId = sessionId;
        this.androidChat.apkFilename = apkFilename || '';
        this.androidChat.messages = (data.messages || []).map((m, i) => ({
          id: `h_${i}`, role: m.role, content: m.content,
        }));
        this.androidChat.streamBuffer = '';
        localStorage.setItem('sharp_android_chat', JSON.stringify({
          sessionId, apkFilename: apkFilename || '', apkPath: '',
        }));
        this.$nextTick(() => {
          const el = this.$refs.androidChatMessages;
          if (el) el.scrollTop = el.scrollHeight;
        });
      } catch (e) {
        this.showToast('切换会话失败：' + e.message, 'error');
      }
    },

    async androidChatPushToGraph(content) {
      const projectId = this.androidChat.linkedProjectId;
      if (!projectId || !this.androidChat.sessionId) return;
      try {
        await this.api('POST', `/android/chat/sessions/${this.androidChat.sessionId}/push-fact`, {
          project_id: projectId, content, kind: 'hint',
        });
        await this.loadProjects();
        this.showToast('已写入项目证据图');
      } catch (e) {
        this.showToast(e.message, 'error');
      }
    },

    androidChatSendQuick(q) { this.androidChat.input = q.prompt; this.sendAndroidChatMessage(); },
    lastAndroidChatMessage() {
      for (let i = this.androidChat.messages.length - 1; i >= 0; i--) {
        if (this.androidChat.messages[i].role === 'assistant') return this.androidChat.messages[i];
      }
      return null;
    },

    // ═══════════════════════════════════════════════════════════════════════
    //  Android / 小程序 派发 + 筛选
    // ═══════════════════════════════════════════════════════════════════════

    androidChatQuickActions() {
      return [
        { label: '🔒 SSL Pinning 绕过', prompt: '生成通用 frida SSL pinning 绕过脚本，兼容 OkHttp/TrustKit/系统证书' },
        { label: '🔓 Root 检测绕过', prompt: '找出可能的 root 检测逻辑并给出 frida hook 脚本' },
        { label: '🛡️ 加固分析', prompt: '判断这个 APK 是否有加固，如有请说明加固厂商及脱壳思路' },
        { label: '🪝 Frida 模板', prompt: '给我一个针对此包名的 frida 基础注入脚本模板' },
        { label: '🔍 安全测试清单', prompt: '基于以上静态分析结果，给我一份完整的安全测试检查清单' },
        { label: '🌐 流量分析', prompt: '给我配置 Burp Suite 抓包这个 App 的详细步骤' },
      ];
    },

    async dispatchAndroidStaticAnalysis() {
      const path = (this.android.path || '').trim();
      if (!path || this.android.dispatching) return;
      this.android.dispatching = true;
      try {
        const projectId = this.projectIsActive() ? this.selectedProjectId : '';
        const data = await this.api('POST', '/android/apk/dispatch-static-analysis', {
          path,
          project_id: projectId,
          creator: this.actorName(),
        });
        this.android.dispatchResult = data;
        this.showAndroidAnalyzer = false;
        // 自动关联到刚创建的项目，方便一键写入证据图
        if (data.project_id) this.androidChat.linkedProjectId = data.project_id;
        await this.loadProjects();
        await this.openProject(data.project_id);
        this.showToast(data.created_project ? '已创建项目并发送 AI 分析' : '已发送到当前项目 AI 分析');
      } catch (e) {
        this.showToast(e.message, 'error');
      } finally {
        this.android.dispatching = false;
      }
    },

    miniProgramTabs() {
      const result = this.miniProgram.result || {};
      return [
        { id: 'findings', label: '风险', count: (result.findings || []).length },
        { id: 'domains', label: '域名', count: (result.domains || []).length },
        { id: 'endpoints', label: '接口', count: (result.endpoints || []).length },
        { id: 'apis', label: 'wx API', count: (result.api_calls || []).length },
        { id: 'files', label: '文件', count: (result.files || []).length },
      ];
    },

    miniQuery() {
      return this.miniProgram.query.trim().toLowerCase();
    },

    miniMatches(...values) {
      const q = this.miniQuery();
      if (!q) return true;
      return values.some(value => String(value ?? '').toLowerCase().includes(q));
    },

    miniProgramFilteredFindings() {
      const rows = this.miniProgram.result?.findings || [];
      return rows.filter(item => this.miniMatches(item.severity, item.title, item.detail, item.file, item.evidence));
    },

    miniProgramFilteredDomains() {
      const rows = this.miniProgram.result?.domains || [];
      return rows.filter(item => this.miniMatches(item.host, item.scheme));
    },

    miniProgramFilteredEndpoints() {
      const rows = this.miniProgram.result?.endpoints || [];
      return rows.filter(item => this.miniMatches(item.url, item.host, item.scheme, item.file));
    },

    miniProgramFilteredApis() {
      const rows = this.miniProgram.result?.api_calls || [];
      return rows.filter(item => this.miniMatches(item.api, (item.files || []).join(' ')));
    },

    miniProgramFilteredFiles() {
      const rows = this.miniProgram.result?.files || [];
      return rows.filter(item => this.miniMatches(item.path, item.type, item.size));
    },

    miniProgramFilteredPackages() {
      const rows = this.miniProgram.packages || [];
      return rows.filter(item => this.miniMatches(item.name, item.path, item.app_id, item.mtime));
    },

    miniSeverityClass(severity) {
      return {
        high: 'bg-rose-50 text-rose-700 border border-rose-100',
        medium: 'bg-amber-50 text-amber-700 border border-amber-100',
        info: 'bg-sky-50 text-sky-700 border border-sky-100',
      }[severity] || 'bg-slate-100 text-slate-600 border border-slate-200';
    },

  });
}
