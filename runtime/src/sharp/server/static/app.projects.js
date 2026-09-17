/**
 * app.projects.js — 项目列表/筛选/星标/标签、YAML 高亮/导出、AI 报告、删除/重命名、项目模板
 */
function applyProjectsModule(obj) {
  Object.assign(obj, {
    filteredProjects() {
      const keyword = this.projectSearch.trim().toLowerCase();
      return this.projects.filter((project) => {
        if (this.projectStatusFilter !== 'all' && project.status !== this.projectStatusFilter) return false;
        // 星标/分组过滤
        if (this.projectStarFilter === 'starred' && !this._starred[project.id]) return false;
        if (this.projectStarFilter !== 'all' && this.projectStarFilter !== 'starred') {
          const tags = this._projectTags[project.id] || [];
          if (!tags.includes(this.projectStarFilter)) return false;
        }
        // 资产中心筛选（批次 11.4）：按 asset_ref（含 '' 未归类）精确匹配
        if (this.projectAssetFilter && project.asset_ref !== this.projectAssetFilter) return false;
        if (!keyword) return true;
        const haystack = [
          project.id,
          project.title,
          project.status,
          project.asset_ref || '',
          project.target_kind || '',
          this.statusLabel(project.status),
          ...(this._projectTags[project.id] || []),
        ].filter(Boolean).join(' ').toLowerCase();
        return haystack.includes(keyword);
      });
    },

    // ── 目标空间详情（P2-A）────────────────────────────────────────────────
    // 与已有两端的分工：项目列表看"有哪些资产"，覆盖报告看"这一次打得怎样"，
    // 这里看"**这个资产整体**打到哪了" —— 接口台账本来就按资产键跨项目共享，
    // 此前点芯片只等于过滤项目列表，看不到共享的那部分。
    openAssetSpace(assetRef = null) {
      const ref = assetRef || this.projectAssetFilter;
      if (!ref) {
        this.showToast('请先选择一个资产', 'error');
        return;
      }
      this.assetSpaceOpen = true;
      this.loadAssetSpace(ref);
    },

    closeAssetSpace() {
      this.assetSpaceOpen = false;
    },

    async loadAssetSpace(assetRef) {
      this.assetSpaceLoading = true;
      this.assetSpace = null;
      try {
        this.assetSpace = await this.api('GET', `/asset-spaces/${encodeURIComponent(assetRef)}`);
      } catch (e) {
        this.showToast('资产空间加载失败：' + e.message, 'error');
      } finally {
        this.assetSpaceLoading = false;
      }
    },

    assetSpaceEndpointGroups() {
      const items = this.assetSpace?.endpoints?.items || [];
      return {
        todo: items.filter(i => i.status === 'discovered'),
        verified: items.filter(i => i.status === 'verified'),
        dismissed: items.filter(i => i.status === 'dismissed'),
      };
    },

    assetSpaceSeverityRows() {
      const by = this.assetSpace?.findings?.by_severity || {};
      const order = ['critical', 'high', 'medium', 'low', 'info'];
      return order.filter(k => by[k]).map(k => ({ severity: k, count: by[k] }));
    },

    // ── 资产中心（批次 11.4）──────────────────────────────
    assetKindBadge(kind) {
      const meta = {
        web:         { label: 'Web',    cls: 'bg-sky-50 text-sky-600' },
        miniprogram: { label: '小程序', cls: 'bg-violet-50 text-violet-600' },
        android:     { label: 'App',    cls: 'bg-emerald-50 text-emerald-600' },
      };
      return meta[kind] || { label: '其他', cls: 'bg-slate-100 text-slate-500' };
    },
    async toggleAssetsPanel() {
      this.assetsPanelOpen = !this.assetsPanelOpen;
      if (this.assetsPanelOpen && !this.assetsLoaded) await this.loadAssets();
    },
    async loadAssets() {
      this.assetsLoading = true;
      try {
        this.assets = await this.api('GET', '/assets') || [];
        this.assetsLoaded = true;
      } catch (_) {
        this.assets = [];
      } finally {
        this.assetsLoading = false;
      }
    },

    // ── 项目收藏/分组 helpers ─────────────────────────────────────────────
    isStarred(projectId) { return !!this._starred[projectId]; },
    toggleStar(projectId) {
      if (this._starred[projectId]) delete this._starred[projectId];
      else this._starred[projectId] = true;
      this._starred = { ...this._starred };
      localStorage.setItem('sharp_starred', JSON.stringify(this._starred));
    },
    projectTagList(projectId) { return this._projectTags[projectId] || []; },
    allProjectTagOptions() {
      const s = new Set();
      Object.values(this._projectTags).forEach(tags => tags.forEach(t => s.add(t)));
      return [...s].sort();
    },
    addProjectTag(projectId, tag) {
      tag = tag.trim();
      if (!tag) return;
      const cur = this._projectTags[projectId] || [];
      if (!cur.includes(tag)) {
        this._projectTags = { ...this._projectTags, [projectId]: [...cur, tag] };
        localStorage.setItem('sharp_project_tags', JSON.stringify(this._projectTags));
      }
    },
    removeProjectTag(projectId, tag) {
      const cur = this._projectTags[projectId] || [];
      this._projectTags = { ...this._projectTags, [projectId]: cur.filter(t => t !== tag) };
      localStorage.setItem('sharp_project_tags', JSON.stringify(this._projectTags));
    },
    starredCount() { return Object.values(this._starred).filter(Boolean).length; },

    // ── 任务模式（P1 收敛：旗帜/记分是可选模式，不是核心概念）──────────
    setNewProjectTaskMode(mode) {
      this.newProject.taskMode = mode === 'scored' ? 'scored' : 'pentest';
    },
    isScoredProject() {
      return this.project?.project?.task_mode === 'scored';
    },
    async switchProjectTaskMode(projectId, mode) {
      try {
        await this.api('PUT', `/projects/${projectId}/task-mode`, { task_mode: mode });
        this.showToast(mode === 'scored' ? '已切换为评分类任务模式' : '已切换为渗透测试模式');
        await this.loadProjects();
        if (this.selectedProjectId === projectId) await this.loadProject(projectId);
      } catch (e) {
        this.showToast(e.message || '任务模式切换失败', 'error');
      }
    },

    // ── 全局证据搜索 ────────────────────────────────────────────────────
    openGlobalSearch() { this.globalSearch = { show: true, query: '', results: [], loading: false, done: false }; },
    closeGlobalSearch() { this.globalSearch.show = false; },
    async runGlobalSearch() {
      const q = this.globalSearch.query.trim().toLowerCase();
      if (!q) { this.globalSearch.results = []; this.globalSearch.done = false; return; }
      this.globalSearch.loading = true;
      this.globalSearch.done = false;
      this.globalSearch.results = [];
      try {
        const results = [];
        for (const proj of this.projects) {
          let data;
          try { data = await this.api('GET', `/projects/${proj.id}`); } catch { continue; }
          const facts = data.facts || [];
          for (const fact of facts) {
            if (fact.id === 'origin' || fact.id === 'goal') continue;
            const text = (fact.description || '').toLowerCase();
            if (text.includes(q)) {
              results.push({
                projectId: proj.id,
                projectTitle: proj.title,
                projectStatus: proj.status,
                factId: fact.id,
                snippet: fact.description.slice(0, 200),
              });
              if (results.length >= 50) break;
            }
          }
          if (results.length >= 50) break;
        }
        this.globalSearch.results = results;
      } catch(e) { this.showToast('搜索出错：' + e.message, 'error'); }
      finally { this.globalSearch.loading = false; this.globalSearch.done = true; }
    },
    async jumpToSearchResult(result) {
      this.closeGlobalSearch();
      await this.openProject(result.projectId);
      // 选中对应 fact 节点
      this.$nextTick(() => {
        if (this.cy) {
          const node = this.cy.getElementById(result.factId);
          if (node.length > 0) {
            this.selectedNode = { type: 'fact', id: result.factId };
            this.cy.animate({ fit: { eles: node, padding: 80 } }, { duration: 400 });
          }
        }
      });
    },

    // ── YAML 高亮 ────────────────────────────────────────────────────────
    highlightYamlScalar(value) {
      const escaped = this.escapeHtml(value);
      if (!value.trim()) return escaped;
      if (/^\s*#.*$/.test(value)) return `<span style="color:#64748b">${escaped}</span>`;
      if (/^\s*['"].*['"]\s*$/.test(value)) return `<span style="color:#15803d">${escaped}</span>`;
      if (/^\s*\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}:\d{2})\s*$/.test(value)) return `<span style="color:#b45309">${escaped}</span>`;
      if (/^\s*(true|false|null|~)\s*$/i.test(value)) return `<span style="color:#b91c1c">${escaped}</span>`;
      if (/^\s*-?\d+(\.\d+)?\s*$/.test(value)) return `<span style="color:#0f766e">${escaped}</span>`;
      if (/^\s*(origin|goal|f\d+|i\d+)\s*$/i.test(value)) return `<span style="color:#6d28d9">${escaped}</span>`;
      return `<span style="color:#0f172a">${escaped}</span>`;
    },

    highlightYamlLine(line) {
      if (/^\s*$/.test(line)) return '';
      if (/^\s*#/.test(line)) return `<span style="color:#64748b">${this.escapeHtml(line)}</span>`;

      const listKeyMatch = line.match(/^(\s*-\s+)([^:#\n][^:]*):(.*)$/);
      if (listKeyMatch) {
        const [, prefix, key, rest] = listKeyMatch;
        return `${this.escapeHtml(prefix)}<span style="color:#7dd3fc">${this.escapeHtml(key)}</span>:${this.highlightYamlScalar(rest)}`;
      }

      const keyMatch = line.match(/^(\s*)([^:#\n][^:]*):(.*)$/);
      if (keyMatch) {
        const [, indent, key, rest] = keyMatch;
        return `${this.escapeHtml(indent)}<span style="color:#7dd3fc">${this.escapeHtml(key)}</span>:${this.highlightYamlScalar(rest)}`;
      }

      const listValueMatch = line.match(/^(\s*-\s+)(.*)$/);
      if (listValueMatch) {
        const [, prefix, rest] = listValueMatch;
        return `${this.escapeHtml(prefix)}${this.highlightYamlScalar(rest)}`;
      }

      return this.highlightYamlScalar(line);
    },

    yamlSectionTint(sectionName) {
      const tints = {
        project: { header: '#eff6ff', body: '#fafcff', itemA: '#f3f8ff', itemB: '#edf5ff' },
        hints: { header: '#fffbeb', body: '#fffef8', itemA: '#fffaf0', itemB: '#fff6e8' },
        facts: { header: '#eef2ff', body: '#fafaff', itemA: '#f5f7ff', itemB: '#eef3ff' },
        intents: { header: '#ecfdf5', body: '#f8fdfb', itemA: '#f1fbf5', itemB: '#eaf8ef' },
      };
      return tints[sectionName] || { header: '#f8fafc', body: '#ffffff', itemA: '#f8fafc', itemB: '#f1f5f9' };
    },

    async copyYamlPreview() {
      if (!this.yamlPreviewText) return;
      try {
        await this.copyText(this.yamlPreviewText);
        this.showToast('已复制');
      } catch {
        this.showToast('复制失败', 'error');
      }
    },

    highlightYaml(text) {
      const lines = String(text ?? '').split('\n');
      let currentSection = '';
      let activeItemIndent = null;
      let activeItemStripe = 0;
      let nextItemStripe = 0;

      return lines.map((line) => {
        const topLevelMatch = line.match(/^([A-Za-z_][A-Za-z0-9_-]*):\s*$/);
        if (topLevelMatch) {
          currentSection = topLevelMatch[1];
          activeItemIndent = null;
          activeItemStripe = 0;
          nextItemStripe = 0;
        }

        const tint = this.yamlSectionTint(currentSection);
        const indent = (line.match(/^(\s*)/) || ['',''])[1].length;
        const isBlank = /^\s*$/.test(line);
        const isListItem = /^(\s*)-\s+/.test(line);
        const isTopLevelItem = isListItem && indent === 0;

        if (isTopLevelItem) {
          activeItemIndent = indent;
          activeItemStripe = nextItemStripe;
          nextItemStripe = nextItemStripe === 0 ? 1 : 0;
        } else if (!isBlank && activeItemIndent !== null && indent === 0) {
          activeItemIndent = null;
        }

        const isNestedListItem = isListItem && activeItemIndent !== null && indent > activeItemIndent;
        const isItemContinuation = !isListItem && !isBlank && activeItemIndent !== null && indent > activeItemIndent;
        const isBlankWithinItem = isBlank && activeItemIndent !== null;
        const isItemLine = isTopLevelItem || isNestedListItem || isItemContinuation || isBlankWithinItem;
        const lineHtml = isBlank ? '&nbsp;' : this.highlightYamlLine(line);
        let background = currentSection ? tint.body : 'transparent';
        let fontWeight = '400';

        if (topLevelMatch) {
          background = tint.header;
          fontWeight = '700';
        } else if (isItemLine) {
          background = activeItemStripe === 0 ? tint.itemA : tint.itemB;
        }

        return `<div style="white-space:pre;padding:0 16px;background:${background};font-weight:${fontWeight};color:#0f172a">${lineHtml}</div>`;
      }).join('');
    },

    highlightTimeline(text) {
      const lines = String(text ?? '').split('\n');
      const stripes = ['#fffbf5', '#fef5ee'];
      let blockIndex = -1;
      return lines.map((line) => {
        if (/^\[/.test(line)) blockIndex++;
        const bg = blockIndex < 0 ? stripes[0] : stripes[blockIndex % 2];
        const isBlank = /^\s*$/.test(line);
        return `<div style="white-space:pre;padding:0 16px;background:${bg};color:#0f172a">${isBlank ? '&nbsp;' : this.escapeHtml(line)}</div>`;
      }).join('');
    },

    projectLabel(projectId, projectTitle) {
      return projectTitle ? `${projectId} - ${projectTitle}` : projectId;
    },

    deleteConfirmLabel() {
      return this.projectLabel(this.deleteConfirm.id, this.deleteConfirm.title);
    },

    requestDeleteProject(projectId, projectTitle = '') {
      if (!projectId) return;
      this.deleteConfirm = { id: projectId, title: projectTitle };
      this.showDeleteModal = true;
    },

    closeDeleteModal(force = false) {
      if (this.isDeletingProject && !force) return;
      this.showDeleteModal = false;
      this.deleteConfirm = { id: '', title: '' };
    },

    async viewProjectYaml(projectId, projectTitle = '') {
      if (!projectId) return;
      this.exportProjectId = projectId;
      this.yamlPreviewTitle = this.projectLabel(projectId, projectTitle);
      this.exportTab = 'yaml';
      await this.loadExportTab('yaml');
      this.showYamlModal = true;
    },

    async switchExportTab(tab) {
      if (tab === this.exportTab) return;
      this.exportTab = tab;
      await this.loadExportTab(tab);
    },

    async loadExportTab(tab) {
      if (!this.exportProjectId) return;
      try {
        const text = await this.fetchText(`/projects/${this.exportProjectId}/export?format=${tab}`);
        this.yamlPreviewText = text;
        this.yamlPreviewHtml = tab === 'yaml' ? this.highlightYaml(text) : this.highlightTimeline(text);
      } catch (e) {
        this.showToast(e.message, 'error');
      }
    },

    async exportTestResults() {
      if (!this.exportProjectId) return;
      await this.handleAiReportById(this.exportProjectId, this.yamlPreviewTitle || '');
    },

    async exportEngineeredReport() {
      if (!this.exportProjectId) return;
      await this.handleAiReportById(this.exportProjectId, this.yamlPreviewTitle || '');
    },

    async exportProjectResults() {
      if (!this.selectedProjectId) return;
      await this.handleAiReportById(this.selectedProjectId, this.project?.project?.title || '', this.project?.project?.status || '');
    },

    async exportProjectResultsById(projectId, projectTitle = '') {
      await this.handleAiReportById(projectId, projectTitle);
    },

    async handleAiReport() {
      if (!this.selectedProjectId) return;
      await this.handleAiReportById(this.selectedProjectId, this.project?.project?.title || '', this.project?.project?.status || '');
    },

    async handleAiReportRegenerate() {
      if (!this.selectedProjectId) return;
      const projectId = this.selectedProjectId;
      const projectTitle = this.project?.project?.title || '';
      this.reportDraftingProjectId = projectId;
      try {
        const result = await this.api('POST', `/projects/${projectId}/reports/ai?regenerate=true`, {
          creator: this.actorName(),
        });
        const label = this.projectLabel(projectId, projectTitle);
        if (result.status === 'queued') {
          this.showToast(`${label} 正在重新生成报告，请稍等`);
        } else if (result.status === 'generating') {
          this.showToast(`${label} 报告生成中，请稍等`);
        } else if (result.status === 'exported') {
          this.showToast(`${label} AI报告已导出到 ${result.report_path}`);
        } else {
          this.showToast(`${label} 报告状态未知`, 'error');
        }
        await this.loadProjects();
        if (this.selectedProjectId === projectId && this.view === 'graph') {
          await this.loadProject(projectId);
          this.updateGraph();
        }
      } catch (e) {
        this.showToast(e.message, 'error');
      } finally {
        this.reportDraftingProjectId = '';
      }
    },

    async handleAiReportById(projectId, projectTitle = '', status = '', confirmed = false) {
      if (!projectId) return;
      if (status && !this.projectCanCreateAiReport(status)) {
        this.showToast('项目需要完成后才能生成报告', 'error');
        return;
      }
      this.reportDraftingProjectId = projectId;
      try {
        const url = confirmed
          ? `/projects/${projectId}/reports/ai?confirm_high_risk=true`
          : `/projects/${projectId}/reports/ai`;
        const result = await this.api('POST', url, {
          creator: this.actorName(),
        });
        const label = this.projectLabel(projectId, projectTitle);
        if (result.status === 'pending_confirm') {
          // 高危报告二次确认（站内对话框，替代原生 confirm）
          this.reportDraftingProjectId = '';
          this.confirmDialog({
            title: '高危报告二次确认',
            message:
              `检测到 ${result.finding_count ?? '高危'} 条高危发现。\n\n` +
              `导出报告将包含完整漏洞利用细节（命令、Payload、路径等），\n` +
              `请确认您已获得合法授权，且报告仅用于合规审计目的。`,
            okText: '确认导出',
            danger: true,
            onOk: () => this.handleAiReportById(projectId, projectTitle, status, true),
          });
          return;
        }
        if (result.status === 'exported') {
          this.showToast(`${label} AI报告已导出到 ${result.report_path}`);
        } else if (result.status === 'queued') {
          this.showToast(`${label} 报告生成中，请稍等`);
          // 乐观更新：立刻显示生成中徽章，不等 loadProjects 异步刷新
          const proj = this.projects.find(p => p.id === projectId);
          if (proj) proj.engineered_report_status = 'generating';
        } else if (result.status === 'generating') {
          this.showToast(`${label} 报告生成中，请稍等`);
          const proj = this.projects.find(p => p.id === projectId);
          if (proj) proj.engineered_report_status = 'generating';
        } else {
          this.showToast(`${label} 报告状态未知`, 'error');
        }
        await this.loadProjects();
        if (this.selectedProjectId === projectId && this.view === 'graph') {
          await this.loadProject(projectId);
          this.updateGraph();
        }
      } catch (e) {
        this.showToast(e.message, 'error');
      } finally {
        this.reportDraftingProjectId = '';
      }
    },

    async previewAiReport(projectId, projectTitle) {
      this.reportPreview = { show: true, title: projectTitle || projectId, content: '', loading: true };
      try {
        const resp = await fetch(`/projects/${projectId}/reports/latest/download`, {
          headers: { 'Authorization': `Bearer ${this.authToken}` },
          signal: this._timeoutSignal(300000),
        });
        if (!resp.ok) throw new Error(`${resp.status}`);
        this.reportPreview.content = await resp.text();
      } catch (e) {
        this.showToast('加载报告失败：' + e.message, 'error');
        this.reportPreview.show = false;
      } finally {
        this.reportPreview.loading = false;
      }
    },

    printReport() {
      const content = this.reportPreview.content;
      if (!content) return;
      const area = document.getElementById('sharp-print-area');
      if (!area) return;
      const title = this.reportPreview.title || 'AI 报告';
      const now = new Date().toLocaleString('zh-CN');
      area.innerHTML = `
        <div class="print-meta"><strong>${title}</strong> · 导出时间：${now}</div>
        <div class="md-body">${this.renderMd(content)}</div>
      `;
      window.print();
    },

    async downloadAiReport(projectId, projectTitle) {
      try {
        const name = (projectTitle || projectId).replace(/[/\\?%*:|"<>]/g, '_').slice(0, 60);
        const url = `/projects/${projectId}/reports/latest/download`;
        const resp = await fetch(url, { headers: { 'Authorization': `Bearer ${this.authToken}` }, signal: this._timeoutSignal(300000) });
        if (!resp.ok) {
          const err = await resp.json().catch(() => ({}));
          throw new Error(err.detail || `下载失败 ${resp.status}`);
        }
        const blob = await resp.blob();
        const objUrl = URL.createObjectURL(blob);
        const a = document.createElement('a');
        const ts = new Date().toISOString().slice(0, 10);
        a.href = objUrl; a.download = `${name}-report-${ts}.md`;
        document.body.appendChild(a); a.click();
        document.body.removeChild(a); URL.revokeObjectURL(objUrl);
      } catch(e) { this.showToast(e.message, 'error'); }
    },

    async downloadExportFile(projectId, projectTitle, format) {
      try {
        const text = await this.fetchText(`/projects/${projectId}/export?format=${format}`);
        const ext = format === 'yaml' ? 'yaml' : 'txt';
        const name = (projectTitle || projectId).replace(/[/\\?%*:|"<>]/g, '_').slice(0, 60);
        const blob = new Blob([text], { type: 'text/plain;charset=utf-8' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url; a.download = `${name}-${format}.${ext}`;
        document.body.appendChild(a); a.click();
        document.body.removeChild(a); URL.revokeObjectURL(url);
      } catch(e) { this.showToast(e.message, 'error'); }
    },

    async requestAiReportDraft() {
      if (!this.selectedProjectId) return;
      await this.handleAiReportById(this.selectedProjectId, this.project?.project?.title || '', this.project?.project?.status || '');
    },

    async requestAiReportDraftById(projectId, projectTitle = '', status = '') {
      await this.handleAiReportById(projectId, projectTitle, status);
    },

    deleteProject() {
      if (!this.selectedProjectId) return;
      this.requestDeleteProject(this.selectedProjectId, this.project?.project?.title || '');
    },

    deleteProjectById(projectId, projectTitle = '') {
      this.requestDeleteProject(projectId, projectTitle);
    },

    async confirmDeleteProject() {
      const projectId = this.deleteConfirm.id;
      if (!projectId || this.isDeletingProject) return;
      try {
        this.isDeletingProject = true;
        await this.api('DELETE', `/projects/${projectId}`);
        this.closeDeleteModal(true);
        if (this.selectedProjectId === projectId && this.view === 'graph') {
          this.backToList();
        } else {
          await this.loadProjects();
        }
        this.showToast(`已删除 ${projectId}`);
      } catch (e) {
        this.showToast(e.message, 'error');
      } finally {
        this.isDeletingProject = false;
        if (!this.showDeleteModal) this.deleteConfirm = { id: '', title: '' };
      }
    },

    // ── 项目创建/模板 ────────────────────────────────────────────────────
    async createProject() {
      try {
        const actor = this.actorName();
        const hintContents = this.newProject.hints.filter(h => h.content?.trim());
        const body = {
          title: this.newProject.title, origin: this.newProject.origin, goal: this.newProject.goal,
          target_kind: this.newProject.targetKind || 'web',
          asset_ref: (this.newProject.assetRef || '').trim() || null,
          // 任务模式：pentest（默认）| scored（评分类，才显示记分语义）
          task_mode: this.newProject.taskMode || 'pentest',
        };
        const hints = hintContents.map(h => ({ content: h.content.trim(), creator: actor }));
        if (hints.length > 0) body.hints = hints;
        const data = await this.api('POST', '/projects', body);
        this.showNewProject = false;
        this.newProject = { title:'', origin:'', goal:'', hints: [{ content:'' }], targetKind:'web', assetRef:'', taskMode:'pentest' };
        await this.loadProjects();
        await this.openProject(data.project.id);
        this.showToast('项目已创建');
      } catch(e) { this.showToast(e.message, 'error'); }
    },

    goNewProject() {
      if (this.view === 'graph') this.backToList(true);
      this.newProject = { title: '', origin: '', goal: '', hints: [{ content: '' }], targetKind: 'web', assetRef: '' };
      this.view = 'newproject';
    },

    // ── 资产类型与资产键（批次 A3）────────────────────────────
    setProjectTargetKind(kind) {
      this.newProject.targetKind = kind;
      if (kind === 'web') this.syncAssetRefFromOrigin();
      else this.newProject.assetRef = '';  // 小程序/App 走各自分析器上传（自动带 AppID/包名）
    },
    syncAssetRefFromOrigin() {
      if (!this.newProject || this.newProject.targetKind !== 'web') return;
      const host = this.firstHostOfOrigin(this.newProject.origin || '');
      // 只在用户未手填资产键时自动填充
      if (!(this.newProject.assetRef || '').trim()) this.newProject.assetRef = host;
    },
    firstHostOfOrigin(text) {
      const m = String(text || '').trim().match(/https?:\/\/([^\/\s?#]+)/i)
        || String(text || '').trim().match(/^([a-z0-9][a-z0-9.-]*\.[a-z]{2,})(?![\w-])/i);
      if (!m) return '';
      let host = m[1].toLowerCase();
      if (host.startsWith('[') && host.includes(']')) return host;  // ipv6
      return host.split(':')[0];
    },

    applyProjectTemplate(id) {
      const tpls = {
        attack_defense: {
          title: '目标系统 Web 渗透测试（攻防）',
          origin: '目标地址：https://\n授权范围：',
          goal: '以获取目标系统最高权限为最终目标，按优先级验证以下权限节点的可达性：\n- Web 普通用户权限：注册/爆破/绕过获取普通账号，验证越权可达性\n- Web 后台管理员权限：绕过认证或利用漏洞进入管理界面\n- 服务器权限：通过 RCE/命令执行获取系统 Shell（webshell / 反弹 shell）\n- 数据库权限：直接读写数据库，获取用户数据、配置信息、密钥等敏感内容\n- 内网横向：以 Web 服务器为跳板，探测并渗透内网其他机器和服务\n\n按优先级获取以下数据：\n- 用户数据：注册账号、个人信息、手机号、身份证号、邮箱等\n- 配置信息：AK/SK、JWT Secret、数据库连接串、第三方平台密钥\n- 源码/备份：Web 源码、数据库备份、配置文件备份、历史版本\n- 内部文档：API 文档、接口文档、内部运维手册、网络拓扑\n- 内网数据：内网其他服务/机器上的敏感文件、凭证、数据',
          hints: ['测试账号：\n用户名：\n密码：'],
        },
        vuln_hunt: {
          title: '目标系统漏洞挖掘',
          origin: '目标地址：https://\n授权范围：（全站 / 仅主域）',
          goal: '尽量发现并验证尽可能多的安全漏洞，按以下优先级顺序测试：\n\n第一优先级（高危，优先投入 80% 精力）：\n- 未授权访问：不加认证直接访问所有能抓到的 API 接口\n- 越权/IDOR：用低权限账号访问高权限接口，遍历资源 ID\n- 文件上传/读取：测试绕过限制上传脚本、读取敏感文件\n- SSRF：测试外网/内网地址访问，探测云 metadata 端点\n- SQL 注入 / 命令注入 / SSTI / XXE：测试所有输入点\n\n第二优先级（中危，第一轮无果时投入）：\n- 配置错误：.git/.env/备份文件/目录列举\n- 信息泄露：接口返回多余字段、错误堆栈、调试信息\n- 业务逻辑漏洞：密码重置绕过、验证码复用、支付篡改\n- 第三方组件漏洞：版本识别 + 公开 CVE 匹配\n\n每个漏洞提供完整复现步骤（PoC），必须包含：\n- 原始请求包（可复制直接重放）\n- 关键响应包（证明漏洞存在）\n- 危害说明（一条即可，不堆砌）\n\n不输出的无效发现：\n反射型 XSS、Self-XSS、缺少 HttpOnly/CSP 等安全头、纯信息泄露（不含敏感数据）、理论漏洞（无法实际利用）',
          hints: [],
        },
        ctf: {
          title: 'CTF 题目',
          origin: '题目地址：https://\n题目说明：',
          goal: '获取 flag，格式通常为 flag{...} 或自定义格式。\n\n按以下顺序快速排查（每项 3-5 分钟，无进展则切换）：\n1. 前端直接提取：查看页面源码、JS 注释、Cookie、响应头、robots.txt\n2. 路径探测：尝试 /flag、/flag.txt、/flag.php、/console、/debug、/backup/\n3. 参数模糊测试：常用参数名 flag / f / token / secret / debug，尝试遍历 id\n4. 常见 Web 漏洞快速测试：SQL 注入 / 文件包含 / 命令注入 / SSTI\n5. 工具辅助（如有附件）：strings / binwalk / steghide / exiftool / Wireshark\n\n如果 20 分钟内仍未获取 flag，输出已尝试的路径和线索，等待人工介入。',
          hints: ['附件：（如有）\n给定线索：（如有）'],
        },
        retest: {
          title: '漏洞复测验证',
          origin: '目标地址：https://\n补丁说明：（版本号 / 修复日期）',
          goal: '对以下漏洞逐一进行复测验证：\n\n复测方法（每个漏洞按此流程执行）：\n1. 原 PoC 验证：使用原始漏洞证明请求，确认漏洞是否仍然存在\n   - 无法复现 → 标记为"已修复"，进入下一个漏洞\n   - 仍可复现 → 标记为"未修复"，记录详情\n2. 绕过尝试（仅当原 PoC 被拦截时执行）：\n   - 大小写变形（如 <ScRiPt>）\n   - 编码绕过（URL 编码 / 双重编码 / Unicode）\n   - 参数污染（同名参数重复 / 不同位置）\n   - 协议变形（HTTP/HTTPS 切换 / 换行注入）\n   - 如果任意绕过成功 → 标记为"部分修复"，记录绕过方式\n3. 关联影响检查（修复后是否引入新问题）：\n   - 检查修复点所在模块的其他参数/接口是否有类似问题\n   - 检查修复是否影响了正常业务功能（如误拦截合法请求）\n   - 如有新问题 → 单独记录为"新发现漏洞"\n\n输出每个漏洞复测结论：\n- ✅ 已修复：原 PoC 无法复现，且无绕过方式\n- ❌ 未修复：原 PoC 仍可复现\n- ⚠️ 部分修复：原 PoC 被拦截，但存在绕过方式\n- 🔴 引入新漏洞：修复过程中产生了新的安全问题',
          hints: ['待复测漏洞清单：\n1. [漏洞名称] - [漏洞位置/参数/PoC 要点]\n2. [漏洞名称] - [漏洞位置/参数/PoC 要点]\n3. [漏洞名称] - [漏洞位置/参数/PoC 要点]'],
        },
        logic_vuln: {
          title: '目标系统业务逻辑专项测试',
          origin: '目标地址：https://\n业务类型：（电商 / 社交 / 金融 / SaaS 等）\n授权范围：',
          goal: '对目标系统进行深度业务逻辑漏洞挖掘，重点覆盖以下维度：\n\n1. IDOR / 水平越权\n   - 遍历资源 ID（订单/用户/文件/消息），用账号 A 访问账号 B 的数据\n   - 测试批量操作接口的越权可能（如批量删除/导出）\n\n2. 垂直越权\n   - 用普通用户 token 直接调用管理员/高权限接口\n   - 测试仅前端隐藏但后端未鉴权的功能\n\n3. 业务流程绕过\n   - 跳过必要步骤（如跳过支付/验证直接到确认）\n   - 重放已完成单步流程触发二次操作\n   - 修改订单金额、优惠力度、数量等关键业务参数\n\n4. 状态机滥用\n   - 对已完成/取消/删除的资源发起不允许的操作\n   - 并发请求触发竞态条件（重复领券/重复提现/重复下单）\n\n5. 认证/会话逻辑\n   - 密码重置 token 不过期 / 可预测 / 可枚举\n   - 验证码可复用或可暴力破解\n   - 登出后 token 仍有效，或 JWT 可伪造\n\n每个漏洞提供完整 PoC（原始请求包 + 关键响应包 + 危害说明一条）。',
          hints: ['账号 A（普通用户）：\n  账号：\n  密码：\n\n账号 B（不同数据/权限）：\n  账号：\n  密码：\n\n高权限/管理员账号（如有）：\n  账号：\n  密码：\n\n核心业务流程描述（订单流/审批流/支付流等）：\n已知高风险接口/功能点：'],
        },
      };
      const t = tpls[id];
      if (!t) return;
      this.newProject.title = t.title;
      this.newProject.origin = t.origin;
      this.newProject.goal = t.goal;
      this.newProject.hints = t.hints.length
        ? t.hints.map(c => ({ content: c }))
        : [{ content: '' }];
    },

    applyCustomTemplate(idx) {
      const t = this.npPage.customTemplates[idx];
      if (!t) return;
      this.newProject.title = t.name;
      this.newProject.origin = t.origin || '';
      this.newProject.goal = t.goal || '';
      this.newProject.hints = [{ content: '' }];
    },

    saveCustomTemplate() {
      const f = this.npPage.editForm;
      if (!f.name.trim()) return;
      this.npPage.customTemplates.push({ name: f.name.trim(), origin: f.origin, goal: f.goal });
      localStorage.setItem('sharp_custom_tpls', JSON.stringify(this.npPage.customTemplates));
      this.npPage.editForm = { name: '', origin: '', goal: '' };
      this.npPage.showAddForm = false;
    },

    startEditTemplate(idx) {
      const t = this.npPage.customTemplates[idx];
      this.npPage.editForm = { name: t.name, origin: t.origin || '', goal: t.goal || '' };
      this.npPage.editingIdx = idx;
    },

    updateCustomTemplate(idx) {
      const f = this.npPage.editForm;
      if (!f.name.trim()) return;
      this.npPage.customTemplates[idx] = { name: f.name.trim(), origin: f.origin, goal: f.goal };
      localStorage.setItem('sharp_custom_tpls', JSON.stringify(this.npPage.customTemplates));
      this.npPage.editingIdx = -1;
    },

    deleteCustomTemplate(idx) {
      this.npPage.customTemplates.splice(idx, 1);
      localStorage.setItem('sharp_custom_tpls', JSON.stringify(this.npPage.customTemplates));
    },
  });
}
