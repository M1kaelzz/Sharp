/**
 * app.intents.js — 行动/证据/线索操作：创建/认领/心跳/释放/结论/完成项目/添加线索
 */
function applyIntentsModule(obj) {
  Object.assign(obj, {
    openCreateIntent() {
      if (!this.canActOnSelectedFacts()) return;
      this.intentForm = { description:'' };
      this.projectAction = { show: true, mode: 'intent' };
    },

    async createIntent(claim = false) {
      try {
        const actor = this.actorName();
        await this.api('POST', `/projects/${this.selectedProjectId}/intents`, {
          from: this.selectedFacts,
          description: this.intentForm.description,
          creator: actor,
          worker: claim ? actor : null,
        });
        this.projectAction = { show: false, mode: '' };
        await this.loadProject(this.selectedProjectId);
        this.updateGraph();
        this.showToast(claim ? '行动已声明并认领' : '行动已声明');
      } catch(e) { this.showToast(e.message, 'error'); }
    },

    async sendHeartbeatForSelectedIntent() {
      const intent = this.selectedActionableOpenIntentRecord();
      if (!intent) return;
      try {
        const actor = this.actorName();
        const claiming = !intent.worker;
        await this.api('POST', `/projects/${this.selectedProjectId}/intents/${intent.id}/heartbeat`, { worker: actor });
        await this.loadProject(this.selectedProjectId);
        this.updateGraph();
        this.showToast(claiming ? '行动已认领' : '心跳已发送');
      } catch(e) { this.showToast(e.message, 'error'); }
    },

    async releaseSelectedIntent() {
      const intent = this.selectedReleasableOpenIntentRecord();
      if (!intent) return;
      try {
        const actor = this.actorName();
        await this.api('POST', `/projects/${this.selectedProjectId}/intents/${intent.id}/release`, { worker: actor });
        await this.loadProject(this.selectedProjectId);
        this.updateGraph();
        this.showToast('行动已释放');
      } catch(e) { this.showToast(e.message, 'error'); }
    },

    openConclude(intent) { this.concludeForm = { description:'', intentId: intent.id }; this.projectAction = { show: true, mode: 'conclude' }; },

    async concludeIntent() {
      try {
        const actor = this.actorName();
        await this.api('POST', `/projects/${this.selectedProjectId}/intents/${this.concludeForm.intentId}/conclude`, { worker: actor, description: this.concludeForm.description });
        this.projectAction = { show: false, mode: '' };
        this.selectedNode = null;
        await this.loadProject(this.selectedProjectId);
        this.updateGraph();
        this.showToast('行动结论已写入');
      } catch(e) { this.showToast(e.message, 'error'); }
    },

    openCompleteProject() {
      if (!this.canActOnSelectedFacts()) return;
      this.completeForm = { description:'' };
      this.projectAction = { show: true, mode: 'complete' };
      // 打开面板即拉完成前验收盘点（11.2）
      this.acceptance = null;
      this.acceptanceLoading = true;
      this.fetchAcceptanceCheck();
      // 同时拉覆盖摘要（P1-A）：盘点只说"还有什么没了结"，覆盖报告补上
      // "覆盖到哪、哪里根本没碰过" —— 结项决策两者都要看。
      this.coverage = null;
      this.loadCoverage();
    },

    async fetchAcceptanceCheck() {
      try {
        const res = await this.api('GET', `/projects/${this.selectedProjectId}/acceptance-check`);
        this.acceptance = res;
      } catch (_) {
        this.acceptance = null;
      } finally {
        this.acceptanceLoading = false;
      }
    },

    openHintModal() {
      if (!this.projectCanWriteHints()) return;
      this.hintForm = { content:'' };
      this.projectAction = { show: true, mode: 'hint' };
    },

    async completeProject() {
      try {
        const actor = this.actorName();
        await this.api('POST', `/projects/${this.selectedProjectId}/complete`, { from: this.selectedFacts, description: this.completeForm.description, worker: actor });
        this.projectAction = { show: false, mode: '' };
        await this.loadProjects();
        await this.loadProject(this.selectedProjectId);
        this.updateGraph();
        this.showToast('项目已完成');
      } catch(e) { this.showToast(e.message, 'error'); }
    },

    // ── 项目截止时间（批次 A）──────────────────────────────
    openProjectDeadline() {
      const current = this.project?.project?.deadline_at || '';
      this.promptDialog({
        title: '设置项目截止时间（ISO-8601 UTC，留空清除）',
        placeholder: '例如 2026-09-10T09:00:00Z',
        initial: current,
        onOk: (v) => this.setProjectDeadline((v || '').trim() || null),
      });
      this.projectAction = { show: false, mode: '' };
    },
    async setProjectDeadline(value) {
      try {
        const meta = await this.api('PUT', `/projects/${this.selectedProjectId}/deadline`, { deadline_at: value });
        if (this.project?.project) this.project.project.deadline_at = meta.deadline_at;
        await this.loadProjects();
        this.showToast(meta.deadline_at ? `截止时间已设：${meta.deadline_at}` : '截止时间已清除');
      } catch (e) { this.showToast(e.message, 'error'); }
    },

    async addHint() {
      if (!this.projectCanWriteHints()) return;
      try {
        const actor = this.actorName();
        await this.api('POST', `/projects/${this.selectedProjectId}/hints`, { content: this.hintForm.content, creator: actor });
        this.projectAction = { show: false, mode: '' };
        this.hintForm = { content:'' };
        await this.loadProjects();
        await this.loadProject(this.selectedProjectId);
        this.sideTab = 'hints';
        this.showToast('线索已添加');
      } catch(e) { this.showToast(e.message, 'error'); }
    },
  });
}
