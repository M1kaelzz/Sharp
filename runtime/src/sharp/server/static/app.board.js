/**
 * app.board.js — 工作台三视图（批次 D3）
 *
 * 把项目工作台从"只有一张节点图"扩展为三种观察方式，各有各要回答的问题：
 *   graph  证据—行动图（Cytoscape，原有）：全貌与关系，适合看结构与断点
 *   board  行动板（看板分列）         ：现在有多少活、卡在哪一列、谁在跑
 *   chain  证据链（按推导顺序排列）   ：结论是怎么一步步得出来的
 *
 * 三视图共享右侧详情栏与同一份 `project` 数据；本模块只做**呈现变换**，
 * 不发起任何写操作，也不改图的语义（证据/行动的字段与状态机保持不变）。
 *
 * 术语按 docs/GLOSSARY.md：证据 = facts，行动 = intents。
 */
function applyBoardModule(obj) {
  Object.assign(obj, {

    // ── 视图切换 ────────────────────────────────────────────────────────
    workbenchTabs() {
      return [
        { key: 'graph', label: '图', hint: '证据—行动图全貌' },
        { key: 'board', label: '行动板', hint: '按状态分列的行动看板' },
        { key: 'chain', label: '证据链', hint: '结论的推导顺序' },
      ];
    },
    workbenchTabClass(key) {
      return this.workbench === key
        ? 'bg-white text-slate-700 shadow-sm'
        : 'text-slate-500 hover:text-slate-700';
    },
    switchWorkbench(mode) {
      if (!['graph', 'board', 'chain'].includes(mode)) return;
      this.workbench = mode;
      if (mode !== 'graph') return;
      // 图容器被 display:none 隐藏过，Cytoscape 记的尺寸是 0——切回必须重新量尺寸
      this.$nextTick(() => {
        try {
          if (this.cy) { this.cy.resize(); this.cy.fit(undefined, 40); }
        } catch (_) { /* 图未就绪（项目空图）时忽略 */ }
      });
    },

    // ── 相对时间（行动板卡片用）─────────────────────────────────────────
    relativeAge(ts) {
      if (!ts) return '';
      const t = new Date(ts).getTime();
      if (!t || Number.isNaN(t)) return '';
      const sec = Math.max(0, Math.floor((Date.now() - t) / 1000));
      if (sec < 60) return '刚刚';
      if (sec < 3600) return `${Math.floor(sec / 60)} 分钟前`;
      if (sec < 86400) return `${Math.floor(sec / 3600)} 小时前`;
      return `${Math.floor(sec / 86400)} 天前`;
    },

    // ── 行动板（看板）──────────────────────────────────────────────────
    // 分列口径 = 行动的实际处境，而不是状态标签：
    // 已放弃/被拒/审批过期 → 关闭；有产出 → 已结论；待审批 → 闸门；
    // 有 worker → 执行中；其余 → 待办。
    intentBoardColumns() {
      const intents = (this.project && this.project.intents) || [];
      const cols = [
        { key: 'todo', title: '待办', dot: 'bg-slate-300', empty: '没有待办行动', items: [] },
        { key: 'running', title: '执行中', dot: 'bg-amber-400', empty: '当前没有 worker 在跑', items: [] },
        { key: 'gate', title: '待审批', dot: 'bg-rose-400', empty: '闸门当前通畅', items: [] },
        { key: 'done', title: '已结论', dot: 'bg-teal-400', empty: '还没有产出的行动', items: [] },
        { key: 'closed', title: '已关闭', dot: 'bg-slate-400', empty: '没有放弃或失效的行动', items: [] },
      ];
      const slot = Object.fromEntries(cols.map((c, i) => [c.key, i]));
      intents.forEach(i => {
        let key;
        if (i.abandoned_at) key = 'closed';
        else if (i.to) key = 'done';
        else if (i.approval_status === 'pending') key = 'gate';
        else if (i.approval_status === 'rejected' || i.approval_status === 'expired') key = 'closed';
        else if (i.worker) key = 'running';
        else key = 'todo';
        cols[slot[key]].items.push(i);
      });
      const byPriority = (a, b) =>
        (b.priority || 0) - (a.priority || 0) || String(a.created_at || '').localeCompare(String(b.created_at || ''));
      const byRecent = (a, b) =>
        String(b.concluded_at || b.abandoned_at || b.created_at || '')
          .localeCompare(String(a.concluded_at || a.abandoned_at || a.created_at || ''));
      cols[slot.todo].items.sort(byPriority);
      cols[slot.running].items.sort(byPriority);
      cols[slot.gate].items.sort(byPriority);
      cols[slot.done].items.sort(byRecent);
      cols[slot.closed].items.sort(byRecent);
      return cols;
    },
    boardSummary() {
      const cols = this.intentBoardColumns();
      const n = k => (cols.find(c => c.key === k) || { items: [] }).items.length;
      const total = cols.reduce((sum, c) => sum + c.items.length, 0);
      if (!total) return '还没有行动';
      return `${total} 个行动 · 待办 ${n('todo')} · 执行中 ${n('running')} · 待审批 ${n('gate')} · 已结论 ${n('done')}`;
    },
    boardCardAge(i) {
      return this.relativeAge(i.concluded_at || i.abandoned_at || i.last_heartbeat_at || i.created_at);
    },
    // 优先级只对"还没结论、还没关闭"的行动有意义——已了结的卡片不显示徽章
    boardShowPriority(i) {
      return (i.priority || 0) > 0 && !i.to && !i.abandoned_at;
    },
    boardCardClass(i) {
      return this.selectedIntentId() === i.id
        ? 'border-brand-300 bg-brand-50/70 ring-1 ring-brand-100'
        : 'border-slate-200/70 bg-white hover:border-slate-300 hover:shadow-sm';
    },
    // 点击卡片 → 复用图上的选中语义（右侧详情栏随之更新）
    openBoardIntent(id) {
      this.selectIntent(id);
      this.sideTab = 'detail';
    },

    // ── 证据链 ─────────────────────────────────────────────────────────
    // 排序 = 推导顺序：起点（origin）→ 中间证据（按产出行动的创建时间）→ 目标（goal）。
    // 同时给出每条证据的"来源证据"，让"结论凭什么得出"一眼可读。
    evidenceChainRows() {
      const p = this.project;
      if (!p || !p.facts) return [];
      const byOutput = new Map();
      (p.intents || []).forEach(i => { if (i.to && !byOutput.has(i.to)) byOutput.set(i.to, i); });
      const rows = p.facts.map(f => {
        const prod = byOutput.get(f.id) || null;
        return {
          id: f.id,
          description: f.description,
          trusted: f.trusted !== false,
          role: f.id === 'origin' ? 'origin' : (f.id === 'goal' ? 'goal' : 'fact'),
          producingIntent: prod ? prod.id : null,
          sources: prod ? (prod.from || []) : [],
          worker: prod ? prod.worker : null,
          order: prod ? String(prod.created_at || '') : '',
        };
      });
      const rank = r => (r.role === 'origin' ? 0 : (r.role === 'goal' ? 2 : 1));
      return rows.sort((a, b) => {
        if (rank(a) !== rank(b)) return rank(a) - rank(b);
        // 有产出行动的按时间先后；未归属证据（无产出行动）排在中间段末尾
        const ka = a.order || '~';
        const kb = b.order || '~';
        if (ka !== kb) return ka < kb ? -1 : 1;
        return String(a.id).localeCompare(String(b.id));
      });
    },
    evidenceChainSummary() {
      const st = this.evidenceChainStats();
      if (!st.total) return '还没有证据';
      return `${st.total} 条证据 · ${st.untrusted} 条不可信 · ${st.orphan} 条未归属`;
    },
    evidenceChainStats() {
      const rows = this.evidenceChainRows();
      return {
        total: rows.length,
        untrusted: rows.filter(r => !r.trusted).length,
        orphan: rows.filter(r => r.role === 'fact' && !r.producingIntent).length,
        reachedGoal: rows.some(r => r.role === 'goal' && r.producingIntent),
      };
    },
    chainRowClass(row) {
      const picked = this.selectedFacts.includes(row.id) || this.selectedFactId() === row.id;
      if (picked) return 'border-brand-300 bg-brand-50/70 ring-1 ring-brand-100';
      if (!row.trusted) return 'border-amber-200 bg-amber-50/50';
      return 'border-slate-200/70 bg-white hover:border-slate-300';
    },
    chainDotClass(row) {
      if (row.role === 'origin') return 'bg-sky-400';
      if (row.role === 'goal') return 'bg-sky-600';
      if (!row.trusted) return 'bg-amber-400';
      return 'bg-teal-400';
    },
    chainRoleLabel(row) {
      if (row.role === 'origin') return '起点';
      if (row.role === 'goal') return '验收标准';
      return row.producingIntent ? '已确认结论' : '未归属证据';
    },
    openChainFact(id) {
      this.selectFact(id);
      this.sideTab = 'detail';
    },
  });
}
