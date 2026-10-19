/**
 * app.approvals.js — 审批中心：待审批行动列表、详情、批准/拒绝、紧急模式、统计
 * 依赖 app.core.js 的 api()/formatTime()/showToast() 等通用方法。
 */
function applyApprovalsModule(obj) {
  Object.assign(obj, {
    // ── 审批中心状态 ─────────────────────────────────────────────────────────
    approvalList: [],          // 当前审批状态列表（默认 pending）
    approvalStats: { pending: 0, today_approved: 0, total_approved: 0, total_rejected: 0 },
    approvalLoading: false,
    approvalTab: 'pending',    // 'pending' | 'approved' | 'rejected' | 'expired'
    approvalDetail: null,      // 审批详情（含 intent/events）
    showApprovalDetail: false,
    approvalActionNote: '',    // 批准/拒绝备注
    approvalActingId: '',      // 正在操作的 intent id
    emergencyProjects: [],     // 当前处于紧急模式的项目
    showEmergencyModal: false,
    emergencyForm: { projectId: '', reason: '', hours: 1 },

    // ── 急模式放行与事后复核（P1-5）──────────────────────────────────
    // 急模式下高危动作创建即自动放行（不产生 pending），所以人工的介入点不是"审批"
    // 而是"复核"——这份清单就是那个落点。
    async loadEmergencyReleases() {
      this.approvalTab = 'emergency';
      this.emergencyReleasesLoading = true;
      try {
        this.emergencyReleases = await this.api('GET', '/emergency-releases') || [];
      } catch (_) { this.emergencyReleases = []; }
      finally { this.emergencyReleasesLoading = false; }
    },
    emergencyUnreviewedCount() {
      return (this.emergencyReleases || []).filter(r => !r.reviewed).length;
    },
    emergencyVerdictLabel(verdict) {
      return { ok: '已复核：无碍', follow_up: '已复核：需跟进' }[verdict] || (verdict ? '已复核' : '待复核');
    },
    emergencyRiskClass(level) {
      return {
        critical: 'bg-rose-100 text-rose-700', high: 'bg-orange-100 text-orange-700',
        medium: 'bg-amber-100 text-amber-700', low: 'bg-sky-100 text-sky-700',
      }[level] || 'bg-slate-100 text-slate-600';
    },
    async reviewEmergencyRelease(release, verdict) {
      if (!release) return;
      const note = (window.prompt(verdict === 'ok'
        ? '复核备注（可留空）：'
        : '标记「需跟进」请写明跟进事项：') || '').trim();
      if (verdict === 'follow_up' && !note) {
        this.showToast('标记「需跟进」需要写明跟进事项', 'error');
        return;
      }
      try {
        await this.api('POST',
          `/projects/${release.project_id}/intents/${release.intent_id}/emergency-review`,
          { verdict, note });
        this.showToast(verdict === 'ok' ? '已记录：确认无碍' : '已记录：需跟进');
        await this.loadEmergencyReleases();
      } catch (e) { this.showToast(e.message || '复核失败', 'error'); }
    },

    // ── 计算属性：导航徽章 / 计数 ───────────────────────────────
    pendingApprovalCount() {
      if (this.approvalStats && typeof this.approvalStats.pending === 'number') return this.approvalStats.pending;
      // 降级：从 projects 汇总（每个项目 summary 带 pending_approval_count）
      return (this.projects || []).reduce((acc, p) => acc + (Number(p.pending_approval_count) || 0), 0);
    },

    totalPendingApprovals() { return this.pendingApprovalCount(); },

    // 当前项目内待审批行动（用于详情页提示条/图上节点）
    projectPendingApprovalIntents() {
      if (!this.project?.intents) return [];
      return this.project.intents.filter(i => i.approval_status === 'pending');
    },

    // ── 风险等级工具 ─────────────────────────────────────────────
    riskLevelLabel(level) {
      return ({ low: '低危', medium: '中危', high: '高危', critical: '严重' })[level] || level || '未知';
    },
    riskLevelClass(level) {
      return ({
        low:      'bg-yellow-50 text-yellow-700 border-yellow-200',
        medium:   'bg-amber-50 text-amber-700 border-amber-200',
        high:     'bg-orange-50 text-orange-700 border-orange-200',
        critical: 'bg-red-50 text-red-700 border-red-200',
      })[level] || 'bg-slate-50 text-slate-600 border-slate-200';
    },
    riskLevelDotClass(level) {
      return ({
        low: 'bg-yellow-400', medium: 'bg-amber-400',
        high: 'bg-orange-500', critical: 'bg-red-500',
      })[level] || 'bg-slate-400';
    },
    approvalStatusLabel(status) {
      return ({
        none: '无需审批', pending: '待审批', approved: '已批准',
        rejected: '已拒绝', expired: '已过期',
      })[status] || status || '未知';
    },
    approvalStatusClass(status) {
      return ({
        pending:  'text-amber-600 bg-amber-50 border-amber-200',
        approved: 'text-teal-600 bg-teal-50 border-teal-200',
        rejected: 'text-rose-600 bg-rose-50 border-rose-200',
        expired:  'text-slate-400 bg-slate-50 border-slate-200',
      })[status] || 'text-slate-500 bg-slate-50 border-slate-200';
    },

    // 审批剩余超时（小时），用于列表倒计时
    approvalRemainingHours(intent) {
      void this._now;
      if (!intent?.created_at) return null;
      const created = new Date(intent.created_at).getTime();
      const until = created + (24 * 3600 * 1000); // 服务端默认 24h 超时
      return Math.max(0, (until - Date.now()) / 3600000);
    },
    approvalRemainingLabel(intent) {
      const h = this.approvalRemainingHours(intent);
      if (h === null) return '—';
      if (h <= 0) return '已超时';
      if (h < 1) return `${Math.round(h * 60)} 分钟`;
      return `${h.toFixed(1)} 小时`;
    },

    // ── 数据加载 ─────────────────────────────────────────────────
    async loadApprovals(status = this.approvalTab || 'pending') {
      this.approvalLoading = true;
      try {
        const [list, stats] = await Promise.all([
          this.api('GET', `/approvals?status=${encodeURIComponent(status)}`),
          this.api('GET', '/approvals/stats'),
        ]);
        this.approvalList = list || [];
        this.approvalStats = stats || this.approvalStats;
        this.approvalTab = status;
      } catch (e) {
        // 静默失败（审批接口仅在 JWT 下可用；历史会话可能在 auth 失效后重定向）
        if (!this.authToken) return;
        console.error('loadApprovals failed:', e);
      } finally {
        this.approvalLoading = false;
      }
    },

    // ── 导航 ────────────────────────────────────────────────────
    async goApprovals() {
      if (this.view === 'graph') this.backToList(true);
      this.view = 'approvals';
      await this.loadApprovals();
      await this.loadEmergencyStatus();
    },

    // ── 详情 ─────────────────────────────────────────────────────
    async openApprovalDetail(intentId, projectId) {
      if (!intentId || !projectId) return;
      try {
        const detail = await this.api('GET', `/approvals/${intentId}?project_id=${encodeURIComponent(projectId)}`);
        this.approvalDetail = detail;
        this.approvalRejectReason = '';
        this.approvalNote = '';
        this.showApprovalDetail = true;
      } catch (e) {
        this.showToast(e.message, 'error');
      }
    },
    closeApprovalDetail() { this.showApprovalDetail = false; this.approvalDetail = null; },

    // 列表中"拒绝"快捷入口 → 打开详情弹窗（要求填理由）
    async openRejectModal(intentId, projectId) {
      await this.openApprovalDetail(intentId, projectId);
      this.$nextTick(() => {
        this.approvalActionNote = '';
      });
    },

    // ── 批准 / 拒绝 ─────────────────────────────────────────────
    async approveIntent(intentId, projectId, note = '') {
      const actingKey = `${projectId}/${intentId}`;
      if (this.approvalActing) return;
      this.approvalActing = actingKey;
      try {
        await this.api('POST', `/approvals/${intentId}/approve?project_id=${encodeURIComponent(projectId)}`, { note });
        this.showToast('已批准该行动');
        await this.refreshAfterDecision(projectId, intentId);
      } catch (e) {
        this.showToast(e.message, 'error');
      } finally {
        this.approvalActing = '';
      }
    },
    async rejectIntent(intentId, projectId, note = '') {
      if (!note || !note.trim()) {
        this.showToast('拒绝时必须填写理由', 'error');
        return;
      }
      const actingKey = `${projectId}/${intentId}`;
      if (this.approvalActing) return;
      this.approvalActing = actingKey;
      try {
        await this.api('POST', `/approvals/${intentId}/reject?project_id=${encodeURIComponent(projectId)}`, { note: note.trim() });
        this.showToast('已拒绝该行动');
        await this.refreshAfterDecision(projectId, intentId);
      } catch (e) {
        this.showToast(e.message, 'error');
      } finally {
        this.approvalActing = '';
      }
    },
    async refreshAfterDecision(projectId, intentId) {
      // 关闭详情、刷新列表/统计/项目摘要/导航徽章
      this.closeApprovalDetail();
      await Promise.all([
        this.loadApprovals(),
        this.loadProjects(),
      ]);
    },

    // ── 紧急模式 ────────────────────────────────────────────────
    async loadEmergencyStatus() {
      try {
        this.emergencyProjects = await this.api('GET', '/projects/emergency-status');
      } catch (e) {
        if (!this.authToken) return;
        console.error('loadEmergencyStatus failed:', e);
      }
    },
    // 审批空态：把"没有待办"讲成安全状态（闸门价值 + 合规留痕），替代生硬文案
    approvalEmptyState() {
      switch (this.approvalTab) {
        case 'pending':
          return {
            title: '没有待审批的高危行动',
            sub: 'AI 无法自行执行高危操作——闸门此刻处于安全位置。所有高风险行为都已处置完毕，或正在等你放行 / 驳回。',
          };
        case 'approved':
          return {
            title: '暂无已批准记录',
            sub: '你批准过的高危操作会在此留痕（项目 / 行动 / 时间 / 理由），审批全程可追溯——合规从记录开始。',
          };
        case 'rejected':
          return {
            title: '暂无已拒绝记录',
            sub: '被驳回的高危行动会连同理由反馈给 AI：拒绝即终止。此前的每一次拒绝都可在这里回溯。',
          };
        default:
          return {
            title: '暂无已过期记录',
            sub: '超时未处理的高危行动会自动失效，避免审批被无限挂起——这是闸门的兜底防线，同样留痕备查。',
          };
      }
    },
    openEmergencyModal(projectId = '') {
      this.emergencyForm = { projectId, reason: '', hours: 2 };
      this.showEmergencyModal = true;
    },
    closeEmergencyModal() { this.showEmergencyModal = false; },
    async setEmergencyMode() {
      const { projectId, reason, hours } = this.emergencyForm;
      if (!projectId) { this.showToast('请选择项目', 'error'); return; }
      if (!reason.trim()) { this.showToast('请填写紧急模式原因', 'error'); return; }
      if (this.approvalActing) return;
      this.approvalActing = 'emergency';
      try {
        await this.api('POST', `/projects/${projectId}/emergency-mode`, {
          enabled: true, reason: reason.trim(), hours: Number(hours) || 2,
        });
        this.showToast('紧急模式已开启（高危行动将自动放行）');
        this.closeEmergencyModal();
        await this.loadEmergencyStatus();
        await this.loadProjects();
      } catch (e) {
        this.showToast(e.message, 'error');
      } finally {
        this.approvalActing = '';
      }
    },
    async closeEmergencyMode(projectId) {
      if (this.approvalActing) return;
      this.approvalActing = 'emergency';
      try {
        await this.api('POST', `/projects/${projectId}/emergency-mode`, { enabled: false });
        this.showToast('紧急模式已关闭');
        await this.loadEmergencyStatus();
        await this.loadProjects();
      } catch (e) {
        this.showToast(e.message, 'error');
      } finally {
        this.approvalActing = '';
      }
    },
    emergencyRemainingLabel(proj) {
      if (!proj?.emergency_until) return '';
      const ms = new Date(proj.emergency_until).getTime() - Date.now();
      if (ms <= 0) return '已到期';
      const h = ms / 3600000;
      return h >= 1 ? `${h.toFixed(1)} 小时` : `${Math.round(h * 60)} 分钟`;
    },

    // ── 前端 SSE 事件处理（由项目详情调用） ─────────────────────
    async _handleApprovalSseEvent(ev) {
      if (!ev || !ev.type) return;
      // 审批/紧急事件统一触发刷新（审批中心 + 项目摘要徽章）
      const approvalTypes = ['approval_pending', 'intent_approved', 'intent_rejected', 'emergency_mode'];
      if (!approvalTypes.includes(ev.type)) return;
      await this.loadProjects();
      if (this.view === 'approvals') {
        await this.loadApprovals();
        await this.loadEmergencyStatus();
      }
      // 若项目详情正在打开，刷新项目图
      if (this.selectedProjectId && this.view === 'graph') {
        await this.loadProject(this.selectedProjectId);
        this.updateGraph();
      }
    },
  });
}