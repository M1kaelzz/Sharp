/**
 * app.project-detail.js — 项目详情：加载/打开/返回、轮询、SSE、暂停/恢复、重命名、重开
 */
function applyProjectDetailModule(obj) {
  Object.assign(obj, {
    async loadProject(id) {
      try {
        this.project = await this.api('GET', `/projects/${id}`);
        this._updateTitle();
      } catch(e) { this.showToast(e.message, 'error'); }
      // 加载项目漏洞列表，用于大屏节点漏洞色标记
      try { await this.loadVulns(id); } catch(_) {}
    },

    _updateTitle() {
      if (this.view !== 'graph' || !this.project?.project) {
        document.title = 'Sharp';
        return;
      }
      const p = this.project.project;
      const icon = p.status === 'active' ? (p.paused ? '⏸' : '🔄') : p.status === 'completed' ? '✅' : '⏸';
      const name = (p.title || p.id).slice(0, 40);
      document.title = `${icon} ${name} — Sharp`;
    },

    async toggleProjectStop() {
      if (!this.selectedProjectId || !this.project || this.project.project.status === 'completed') return;
      const nextStatus = this.project.project.status === 'active' ? 'stopped' : 'active';
      try {
        await this.setProjectStatus(this.selectedProjectId, nextStatus);
      } catch (e) {
        this.showToast(e.message, 'error');
      }
    },

    async toggleProjectStopById(projectId, currentStatus) {
      if (!projectId || currentStatus === 'completed') return;
      const nextStatus = currentStatus === 'active' ? 'stopped' : 'active';
      try {
        await this.setProjectStatus(projectId, nextStatus);
      } catch (e) {
        this.showToast(e.message, 'error');
      }
    },

    async setProjectStatus(projectId, status, options = {}) {
      const { reload = true, toast = true } = options;
      const updated = await this.api('PUT', `/projects/${projectId}/status`, { status });
      const projectSummary = this.projects.find(item => item.id === projectId);
      if (projectSummary) {
        projectSummary.status = updated.status;
        projectSummary.reason = updated.reason;
      }
      if (this.selectedProjectId === projectId && this.project) {
        this.project.project.status = updated.status;
        this.project.project.reason = updated.reason;
        if (!this.projectIsActive()) {
          this.projectAction = { show: false, mode: '' };
        }
        if (!this.projectCanWriteHints()) {
          this.projectAction = { show: false, mode: '' };
        }
      }
      if (reload) await this.loadProjects();
      if (toast) this.showToast(status === 'stopped' ? '项目已暂停' : '项目已恢复');
      return updated;
    },

    async stopAllActiveProjects() {
      const activeProjects = this.projects.filter(project => project.status === 'active');
      if (activeProjects.length === 0 || this.isStoppingAllProjects) return;
      this.isStoppingAllProjects = true;
      try {
        const results = await Promise.allSettled(
          activeProjects.map(project => this.setProjectStatus(project.id, 'stopped', { reload: false, toast: false }))
        );
        await this.loadProjects();
        const successCount = results.filter(result => result.status === 'fulfilled').length;
        const failureCount = results.length - successCount;
        if (failureCount === 0) {
          this.showToast(`已暂停 ${successCount} 个运行中项目`);
        } else {
          this.showToast(`已暂停 ${successCount} 个项目，${failureCount} 个失败`, 'error');
        }
      } catch (e) {
        this.showToast(e.message, 'error');
      } finally {
        this.isStoppingAllProjects = false;
      }
    },

    async toggleProjectPause(projectId) {
      const summary = this.projects.find(p => p.id === projectId);
      const currentPaused = summary ? summary.paused : (this.project?.project?.paused || false);
      try {
        const updated = await this.api('PUT', `/projects/${projectId}/paused`, { paused: !currentPaused });
        if (summary) summary.paused = updated.paused;
        if (this.selectedProjectId === projectId && this.project) {
          this.project.project.paused = updated.paused;
        }
        this.showToast(updated.paused ? '项目已暂停调度（运行中任务继续）' : '项目已恢复调度');
      } catch (e) {
        this.showToast(e.message, 'error');
      }
    },

    async setProjectBudget(projectId, budget) {
      try {
        const updated = await this.api('PUT', `/projects/${projectId}/budget`, { task_budget: budget });
        const summary = this.projects.find(p => p.id === projectId);
        if (summary) summary.task_budget = updated.task_budget;
        if (this.selectedProjectId === projectId && this.project) {
          this.project.project.task_budget = updated.task_budget;
        }
        this.showToast(budget === 0 ? '已设为无限制' : `已设为 ${budget} 次`);
      } catch (e) {
        this.showToast(e.message, 'error');
      }
    },

    openReopenProject(projectId, projectTitle = '') {
      if (!projectId) return;
      this.reopenForm = { projectId, projectTitle, description: '' };
      this.projectAction = { show: true, mode: 'reopen' };
    },

    // ── 覆盖报告（P1-A）────────────────────────────────────────────────────
    // 结项后"打到了什么 / 漏了什么 / 哪些已验证不通"的唯一落点；
    // 与完成前盘点的分工：盘点答"还有什么没了结"，覆盖报告答"覆盖到哪、哪里没碰过"。
    async openCoverageReport(projectId = null) {
      const pid = projectId || this.selectedProjectId;
      if (!pid) return;
      this.projectAction = { show: true, mode: 'coverage' };
      await this.loadCoverage(pid);
    },

    closeCoverageReport() {
      this.projectAction = { show: false, mode: '' };
    },

    async loadCoverage(projectId = null) {
      const pid = projectId || this.selectedProjectId;
      if (!pid) return;
      this.coverageLoading = true;
      try {
        this.coverage = await this.api('GET', `/projects/${pid}/coverage`);
      } catch (e) {
        this.coverage = null;
        this.showToast('覆盖报告加载失败', 'error');
      } finally {
        this.coverageLoading = false;
      }
    },

    async copyCoverageMarkdown() {
      const md = this.coverage?.markdown || '';
      if (!md) return;
      try {
        await navigator.clipboard.writeText(md);
        this.showToast('覆盖报告已复制（Markdown）');
      } catch (e) {
        this.showToast('复制失败，请手动选择文本', 'error');
      }
    },

    openRenameProject(projectId, projectTitle = '') {
      if (!projectId) return;
      this.renameForm = { projectId, originalTitle: projectTitle, title: projectTitle };
      this.projectAction = { show: true, mode: 'rename' };
      this.$nextTick(() => {
        this.$refs.renameTitleInput?.focus();
        this.$refs.renameTitleInput?.select?.();
      });
    },

    async renameProject() {
      const projectId = this.renameForm.projectId || this.selectedProjectId;
      if (!projectId) return;
      try {
        const updated = await this.api('PUT', `/projects/${projectId}/title`, {
          title: this.renameForm.title,
        });
        const projectSummary = this.projects.find(item => item.id === projectId);
        if (projectSummary) {
          projectSummary.title = updated.title;
        }
        if (this.selectedProjectId === projectId && this.project) {
          this.project.project.title = updated.title;
        }
        this.projectAction = { show: false, mode: '' };
        this.renameForm = { projectId: '', originalTitle: '', title: '' };
        await this.loadProjects();
        this.showToast('项目已重命名');
      } catch (e) {
        this.showToast(e.message, 'error');
      }
    },

    async reopenProject() {
      const projectId = this.reopenForm.projectId || this.selectedProjectId;
      if (!projectId) return;
      try {
        const actor = this.actorName();
        await this.api('POST', `/projects/${projectId}/reopen`, {
          description: this.reopenForm.description,
          creator: actor,
        });
        this.projectAction = { show: false, mode: '' };
        this.reopenForm = { projectId: '', projectTitle: '', description: '' };
        await this.loadProjects();
        if (this.selectedProjectId === projectId && this.view === 'graph') {
          await this.loadProject(projectId);
          this.updateGraph();
        }
        this.showToast('项目已重新打开');
      } catch (e) {
        this.showToast(e.message, 'error');
      }
    },

    // ── 未验证假设（P0-3）────────────────────────────────────────────
    // 假设 = 可证伪的猜想（"若 X 则 Y"）。规划器优先做能一次证实/证伪某个假设的验证；
    // 人可在此增删改（兜底防止"假设刷屏"）。
    async loadHypotheses() {
      const pid = this.selectedProjectId;
      if (!pid) { this.hypotheses = []; return; }
      try {
        this.hypotheses = await this.api('GET', `/projects/${pid}/hypotheses`) || [];
      } catch (_) { this.hypotheses = []; }
    },
    planTabCount() {
      // 「规划」标签的计数 = 未结算假设 + 未完成阶段（两者合并成一个标签后的总待推进项）。
      // 为 0 时隐藏（模板用 x-show），与线索标签同一约定。
      return this.hypothesisOpenCount() + this.subGoals.filter(g => g.status === 'pending' || g.status === 'active').length;
    },

    hypothesisOpenCount() {
      return (this.hypotheses || []).filter(h => h.status === 'open' || h.status === 'testing').length;
    },
    visibleHypotheses() {
      const all = this.hypotheses || [];
      return this.hypothesisShowClosed ? all : all.filter(h => h.status === 'open' || h.status === 'testing');
    },
    hypothesisStatusLabel(status) {
      return { open: '待验证', testing: '验证中', confirmed: '已成立', refuted: '已否定' }[status] || status;
    },
    hypothesisStatusClass(status) {
      return {
        open: 'bg-slate-100 text-slate-600',
        testing: 'bg-amber-100 text-amber-700',
        confirmed: 'bg-teal-100 text-teal-700',
        refuted: 'bg-slate-200 text-slate-500',
      }[status] || 'bg-slate-100 text-slate-500';
    },
    async addHypothesis() {
      const text = (this.hypothesisDraft || '').trim();
      const pid = this.selectedProjectId;
      if (!text || !pid) return;
      try {
        await this.api('POST', `/projects/${pid}/hypotheses`, {
          statement: text, premise_fact_ids: [], created_by: 'human',
        });
        this.hypothesisDraft = '';
        this.showToast('假设已记录');
        await this.loadHypotheses();
      } catch (e) { this.showToast(e.message || '添加假设失败', 'error'); }
    },
    async setHypothesisStatus(h, status) {
      const pid = this.selectedProjectId;
      if (!pid || !h) return;
      try {
        await this.api('PATCH', `/projects/${pid}/hypotheses/${h.id}`, { status, note: h.note || '' });
        await this.loadHypotheses();
      } catch (e) { this.showToast(e.message || '更新失败', 'error'); }
    },
    // 结算必须留依据：成立要指向证据，否定要写清试过什么（与 API 的强制校验一致）
    async resolveHypothesis(h, status) {
      const pid = this.selectedProjectId;
      if (!pid || !h) return;
      let note = '';
      let resultFactId = null;
      if (status === 'refuted') {
        note = (window.prompt('这条假设为什么不成立？请写清尝试过什么：') || '').trim();
        if (!note) { this.showToast('否定必须写清尝试过什么（否定的结论同样是积累）', 'error'); return; }
      } else if (status === 'confirmed') {
        resultFactId = (window.prompt('指向哪条证据？填写 fact id（如 f012）：') || '').trim();
        if (!resultFactId) { this.showToast('标记成立必须指向支撑证据', 'error'); return; }
      }
      try {
        await this.api('PATCH', `/projects/${pid}/hypotheses/${h.id}`,
          { status, note, result_fact_id: resultFactId });
        await this.loadHypotheses();
      } catch (e) { this.showToast(e.message || '结算失败', 'error'); }
    },
    async deleteHypothesis(h) {
      const pid = this.selectedProjectId;
      if (!pid || !h) return;
      try {
        await this.api('DELETE', `/projects/${pid}/hypotheses/${h.id}`);
        await this.loadHypotheses();
      } catch (e) { this.showToast(e.message || '删除失败', 'error'); }
    },

    // ── 阶段目标 Sub Goals（批次 C）────────────────────────────────
    async loadSubGoals() {
      const pid = this.selectedProjectId;
      if (!pid) { this.subGoals = []; return; }
      try {
        this.subGoals = await this.api('GET', `/projects/${pid}/sub-goals`) || [];
      } catch (_) { this.subGoals = []; }
    },
    subGoalProgress() {
      const total = (this.subGoals || []).length;
      const done = (this.subGoals || []).filter(g => g.status === 'done').length;
      return { total, done };
    },
    async addSubGoal(title) {
      const text = (title || this.subGoalDraft || '').trim();
      if (!text) return;
      try {
        await this.api('POST', `/projects/${this.selectedProjectId}/sub-goals`, { title: text, created_by: 'human' });
        this.subGoalDraft = '';
        await this.loadSubGoals();
        this.showToast('阶段目标已添加');
      } catch (e) { this.showToast(e.message, 'error'); }
    },
    async setSubGoalStatus(id, status, note = '') {
      try {
        await this.api('PATCH', `/projects/${this.selectedProjectId}/sub-goals/${id}`, { status, note });
        await this.loadSubGoals();
      } catch (e) { this.showToast(e.message, 'error'); }
    },
    subGoalStatusLabel(status) {
      return ({ pending: '待开始', active: '进行中', done: '已完成', abandoned: '已放弃' })[status] || status;
    },
    subGoalStatusClass(status) {
      return ({
        pending: 'bg-slate-100 text-slate-500',
        active: 'bg-amber-50 text-amber-700 border border-amber-200',
        done: 'bg-teal-50 text-teal-700 border border-teal-200',
        abandoned: 'bg-slate-50 text-slate-400 line-through',
      })[status] || 'bg-slate-100 text-slate-500';
    },

    // ── 项目内紧急模式快捷入口（批次 EM-1）──────────────────────────
    currentProjectInEmergency() {
      const pid = this.selectedProjectId;
      if (!pid || !Array.isArray(this.emergencyProjects)) return false;
      return this.emergencyProjects.some(p => p && p.id === pid);
    },
    toggleProjectEmergency() {
      const pid = this.selectedProjectId;
      if (!pid) return;
      if (this.currentProjectInEmergency()) {
        this.closeEmergencyMode(pid);  // 与审批中心 ✕ 一致：直接关闭
      } else {
        this.openEmergencyModal(pid);  // 弹窗已按项目预选 + 理由/时长必填
      }
    },

    async openProject(id) {
      if (this.replay.active) {
        this.stopReplayTimer();
        this.replay.active = false;
        this.replay.playing = false;
        this.replay.frames = [];
        this.replay.visibleEvents = [];
        this.replay.frameIndex = -1;
        this.replay.sourceProject = null;
        this._timelineEventsCacheProject = null;
        this._timelineEventsCache = [];
        this.polling = true;
      }
      this.rememberProjectListScroll();
      this.liveOutput = { active: false, intentId: '', worker: '', text: '' };
      this.selectedProjectId = id;
      this.selectedNode = null;
      this.selectedFacts = [];
      this.selectedTimelineEntryId = null;
      await this.loadProject(id);
      this.view = 'graph';
      // 项目内紧急模式入口需要新鲜的本项目紧急状态（approvals 页会刷，项目
      // 打开时也刷一次，避免展示过期状态）。
      try { await this.loadEmergencyStatus(); } catch (_) { /* best-effort */ }
      try { await this.loadSubGoals(); } catch (_) { /* best-effort */ }
      try { await this.loadHypotheses(); } catch (_) { /* best-effort */ }
      if (location.hash !== `#/projects/${id}`) location.hash = `/projects/${id}`;
      this.$nextTick(() => {
        const clampedWidth = this.clampPanelWidth(this.sidePanelWidth);
        if (clampedWidth !== this.sidePanelWidth) {
          this.sidePanelWidth = clampedWidth;
          this.saveSidePanelWidth();
        }
        this.teardownAutoFit();
        if (this.cy) { this.cy.destroy(); this.cy = null; }
        this.initGraph();
      });
      this._connectProjectSse(id);
    },

    backToList(fromRoute) {
      this._disconnectProjectSse();
      if (this.replay.active) {
        this.stopReplayTimer();
        this.replay.active = false;
        this.replay.playing = false;
        this.replay.frames = [];
        this.replay.visibleEvents = [];
        this.replay.frameIndex = -1;
        this.replay.sourceProject = null;
        this._timelineEventsCacheProject = null;
        this._timelineEventsCache = [];
        this.polling = true;
      }
      this.view = 'list';
      this._updateTitle();
      this.shouldRestoreProjectListScroll = true;
      this.project = null;
      this.selectedProjectId = '';
      this.selectedNode = null;
      this.selectedFacts = [];
      this.selectedTimelineEntryId = null;
      this.vulns = [];
      this.teardownAutoFit();
      this._teardownNavigator();
      if (this.cy) { this.cy.destroy(); this.cy = null; }
      if (!fromRoute) location.hash = '/';
      this.loadProjects().then(() => this.restoreProjectListScroll());
    },

    startPolling() {
      this.pollTimer = setInterval(async () => {
        if (!this.polling) return;
        // 如果有 SSE 连接且项目视图处于活跃状态，SSE 负责实时更新
        // 轮询只在没有 SSE 或非图视图时才全量刷新
        if (this.selectedProjectId && this.view === 'graph' && this._projectSse) {
          // SSE 在线，跳过全量刷新，由 SSE 驱动
        } else if (this.selectedProjectId && this.view === 'graph') {
          await this.loadProject(this.selectedProjectId);
          this.updateGraph();
        } else {
          await this.loadProjects();
          this.loadVulnStats();
          this.loadVulnTrend();
        }
        if (this.view === 'dispatcher') await this.loadDispatcherStatus();
        if (this.view === 'approvals') await this.loadApprovals();
      }, 5000);
    },

    _connectProjectSse(projectId, isReconnect = false) {
      this._disconnectProjectSse();
      const url = `/projects/${projectId}/stream`;
      // 使用 fetch+ReadableStream 而不是 EventSource，这样可以带 Authorization header
      const ctrl = new AbortController();
      this._projectSse = ctrl;
      this._projectSseId = projectId;
      if (!isReconnect) this._sseReconnectAttempts = 0;
      (async () => {
        try {
          const resp = await fetch(url, {
            headers: { 'Authorization': `Bearer ${this.authToken}` },
            signal: ctrl.signal,
          });
          if (!resp.ok || !resp.body) {
            this._projectSse = null;
            this._projectSseId = null;
            return;
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
              const raw = line.slice(5).trim();
              if (!raw) continue;
              try {
                const ev = JSON.parse(raw);
                this._lastSseFullSync = Date.now();
                await this._handleProjectSseEvent(ev);
              } catch (_) { /* ignore parse errors */ }
            }
            // Batch B4: the server emits a ": heartbeat" comment every 15s even
            // with no events. Use it as a cheap catch-up sync — if an event was
            // dropped while the internal queue was full, the next heartbeat (or
            // the next real event, which already does a full reload) re-syncs
            // within one cadence instead of silently staling.
            if (this._lastSseFullSync && Date.now() - this._lastSseFullSync > 15000
                && this.selectedProjectId === projectId && this.view === 'graph'
                && this.project) {
              this._lastSseFullSync = Date.now();
              try {
                await this.loadProject(projectId);
                this.updateGraph();
              } catch (_) { /* keep the stream alive on refresh failure */ }
            }
          }
          // 流正常结束 — 清除 SSE 状态，让轮询恢复全量刷新
          this._projectSse = null;
          this._projectSseId = null;
        } catch (e) {
          if (e.name === 'AbortError') return;
          // SSE 异常断开 — 清除状态，降级回轮询
          this._projectSse = null;
          this._projectSseId = null;
          // 自动重连（最多3次，指数退避 1s → 2s → 4s）
          this._sseReconnectAttempts++;
          if (this.selectedProjectId === projectId && this.view === 'graph' &&
              this._sseReconnectAttempts <= 3) {
            const delay = 1000 * Math.pow(2, this._sseReconnectAttempts - 1);
            setTimeout(() => {
              if (this.selectedProjectId === projectId && this.view === 'graph') {
                this._connectProjectSse(projectId, true);
              }
            }, delay);
          }
        }
      })();
    },

    _disconnectProjectSse() {
      if (this._projectSse) {
        try { this._projectSse.abort(); } catch (_) {}
        this._projectSse = null;
        this._projectSseId = null;
      }
    },

    async _handleProjectSseEvent(ev) {
      if (!ev || !ev.type) return;
      if (ev.project_id && ev.project_id !== this.selectedProjectId) return;

      if (ev.type === 'worker_output') {
        const text = ev.text || '';
        if (!text.trim()) return;
        this.liveOutput.active = true;
        this.liveOutput.intentId = ev.intent_id || '';
        this.liveOutput.worker = ev.worker || '';
        // Keep last 60KB to prevent unbounded memory growth
        this.liveOutput.text = (this.liveOutput.text + text).slice(-60000);
        // Auto-scroll live output panel
        this.$nextTick(() => {
          const el = this.$refs.liveOutputEl;
          if (el) el.scrollTop = el.scrollHeight;
        });
        return;
      } else if (ev.type === 'sub_goal_created' || ev.type === 'sub_goal_updated') {
        if (this.selectedProjectId) await this.loadSubGoals();
      } else if (ev.type === 'fact_created' || ev.type === 'intent_created' || ev.type === 'fact_corrected') {
        // 增量刷新当前项目（证据纠错同组：untrusted/改写后图与详情需同步）
        if (this.selectedProjectId && this.view === 'graph') {
          await this.loadProject(this.selectedProjectId);
          this.updateGraph();
          await this.loadProjects();
        }
        if (ev.type === 'fact_corrected' && ev.changed) {
          const what = ev.trusted === false ? '标注不可信' : '已修正';
          this.showToast(`✏️ 证据 ${ev.id} ${what}`);
        }
      } else if (ev.type === 'approval_pending' || ev.type === 'intent_approved' || ev.type === 'intent_rejected' || ev.type === 'emergency_mode') {
        // 审批/紧急模式事件：刷新项目详情 + 审批中心 + 导航徽章
        if (this.selectedProjectId && this.view === 'graph') {
          await this.loadProject(this.selectedProjectId);
          this.updateGraph();
        }
        await this.loadProjects();
        if (this.view === 'approvals') await this.loadApprovals();
        if (ev.type === 'intent_approved') {
          const title = this.project?.project?.title || ev.project_id || '项目';
          this.showToast(`✅ ${title}：高危行动已获批`);
        } else if (ev.type === 'intent_rejected') {
          const title = this.project?.project?.title || ev.project_id || '项目';
          this.showToast(`${title}：高危行动被拒绝`, 'error');
        } else if (ev.type === 'approval_pending') {
          const title = this.project?.project?.title || ev.project_id || '项目';
          this.showToast(`${title}：有新的高危行动待审批`, 'info');
        }
      } else if (ev.type === 'vulnerability_created') {
        // 漏洞创建事件：刷新项目列表(漏洞计数) + 当前项目漏洞列表
        await this.loadProjects();
        if (this.selectedProjectId && this.view === 'vulns') {
          await this.loadVulns(this.selectedProjectId);
        }
        if (ev.title) {
          const sev = ev.severity || 'info';
          this.showToast(`🛡️ 新漏洞：${ev.title} (${sev})`, sev === 'critical' || sev === 'high' ? 'error' : 'info');
        }
      } else if (ev.type === 'project_completed') {
        this.liveOutput.active = false;
        if (this.selectedProjectId && this.view === 'graph') {
          await this.loadProject(this.selectedProjectId);
          this.updateGraph();
          await this.loadProjects();
        }
        const title = this.project?.project?.title || ev.project_id || '项目';
        this.showToast(`✅ ${title} 已完成`);
        // 浏览器通知
        if (Notification && Notification.permission === 'granted') {
          new Notification('Sharp — 项目完成', { body: title, icon: '/static/favicon.svg' });
        } else if (Notification && Notification.permission !== 'denied') {
          Notification.requestPermission().then(p => {
            if (p === 'granted')
              new Notification('Sharp — 项目完成', { body: title, icon: '/static/favicon.svg' });
          });
        }
        this._disconnectProjectSse();
      }
    },

    // ── 风险总览条（图视图顶部）──────────────────────────────
    graphRiskCount() {
      const vulns = this.vulns || [];
      const activeVulns = vulns.filter(v => v.status !== 'dismissed');
      return {
        high: activeVulns.filter(v => v.severity === 'high').length,
        critical: activeVulns.filter(v => v.severity === 'critical').length,
        confirmed: activeVulns.filter(v => v.status === 'confirmed').length,
        pending: this.projectPendingApprovalIntents().length,
      };
    },
    graphRiskCounts() {
      const c = this.graphRiskCount();
      // confirmed 也计入 hasRisk：仅"已确认漏洞"非 0 时风险总览条也应显示
      return { ...c, hasRisk: (c.high > 0 || c.critical > 0 || c.pending > 0 || c.confirmed > 0) };
    },

    // ── 图统计条（头部常驻：证据 / 行动 / 高危）────────────────
    graphStats() {
      const facts = this.project?.facts || [];
      const intents = this.project?.intents || [];
      const vulns = this.vulns || [];
      const activeVulns = vulns.filter(v => v.status !== 'dismissed');
      const highVulns = activeVulns.filter(v => v.severity === 'high' || v.severity === 'critical').length;
      const pending = this.projectPendingApprovalIntents().length;
      const working = intents.filter(i => i.worker && !i.to).length;
      const idle = intents.filter(i => !i.worker && !i.to).length;
      return {
        facts: facts.length,
        intents: intents.length,
        concluded: intents.filter(i => i.to).length,
        working,
        idle,
        highVulns,
        pending,
      };
    },
  });
}
