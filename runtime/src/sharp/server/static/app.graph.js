/**
 * app.graph.js — Cytoscape 证据—行动图：初始化/样式/布局/更新、选区/血缘高亮、节点尺寸/文本测量、面板缩放
 */
function applyGraphModule(obj) {
  Object.assign(obj, {
    buildElements() {
      const nodes = [];
      const edges = [];
      // 构建 fact→最高漏洞严重度 映射
      const vulnSeverityMap = {};
      const sevRank = { critical: 4, high: 3, medium: 2, low: 1, info: 0 };
      for (const v of (this.vulns || [])) {
        if (v.status === 'dismissed') continue;
        const cur = vulnSeverityMap[v.fact_id];
        if (cur === undefined || sevRank[v.severity] > sevRank[cur]) {
          vulnSeverityMap[v.fact_id] = v.severity;
        }
      }
      // fact→漏洞计数（用于节点右上角角标）
      const vulnCountMap = {};
      for (const v of (this.vulns || [])) {
        if (v.status === 'dismissed') continue;
        vulnCountMap[v.fact_id] = (vulnCountMap[v.fact_id] || 0) + 1;
      }
      for (const f of this.project.facts) {
        const nodeType = f.id === 'origin' ? 'origin' : f.id === 'goal' ? 'goal' : 'fact';
        const topSev = vulnSeverityMap[f.id];
        const nodeData = {
          id: f.id,
          nodeType,
          description: f.description,
          width: 0,
          height: 0,
        };
        if (topSev) nodeData.hasVuln = topSev;
        if (vulnCountMap[f.id]) nodeData.vulnCount = vulnCountMap[f.id];
        // 人工纠错（批次 11.1）：untrusted 证据以 ⚠ 前缀 + 虚线警示样式呈现
        if (f.trusted === false) nodeData.untrusted = true;
        // 尺寸：cardLabel 内部文本决定
        const cardLabel = this.factCardLabel(f, nodeType, topSev, vulnCountMap[f.id] || 0);
        const size = this.factNodeSize(cardLabel, nodeType);
        nodeData.label = (f.trusted === false ? '⚠ ' : '') + cardLabel;
        nodeData.width = size.width;
        nodeData.height = size.height;
        nodes.push({ data: nodeData });
      }
      for (const intent of this.project.intents) {
        const lbl = intent.description;
        if (intent.to) {
          for (const src of intent.from) {
            edges.push({ data: { id: `${intent.id}_${src}`, source: src, target: intent.to, intentId: intent.id, label: lbl, status: 'concluded' }});
          }
        } else {
          const phId = `_ph_${intent.id}`;
          const nodeSize = this.openIntentNodeSize(intent);
          const nodeType = this.openIntentNodeType(intent);
          nodes.push({ data: {
            id: phId,
            label: this.openIntentNodeLabel(intent),
            description: intent.description,
            nodeType,
            intentId: intent.id,
            width: nodeSize.width,
            height: nodeSize.height,
          }});
          for (const src of intent.from) {
            edges.push({ data: { id: `${intent.id}_${src}`, source: src, target: phId, intentId: intent.id, label: lbl, status: nodeType }});
          }
          if (this.isBootstrapIntent(intent)) {
            edges.push({ data: { id: `${intent.id}_goal`, source: phId, target: 'goal', intentId: intent.id, label: '', status: nodeType, edgeType: 'bootstrap_scope' }});
          }
        }
      }
      return { nodes, edges };
    },

    initGraph() {
      const container = document.getElementById('cy');
      if (!container) return;
      const { nodes, edges } = this.buildElements();
      const rawCy = cytoscape({
        container,
        elements: [...nodes, ...edges],
        style: this.graphStyles(),
        layout: this.layoutOpts(),
        minZoom: 0.15, maxZoom: 3.5,
      });
      this.cy = rawCy;
      const self = this;
      rawCy.on('tap', 'node', e => self.onNodeTap(e));
      rawCy.on('tap', 'edge', e => self.onEdgeTap(e));
      rawCy.on('tap', e => { if (e.target === rawCy) self.clearSelection(); });
      this._setupEdgeLabelLod();
      this._initNavigator();
      this.pulseActive();
      this.setupAutoFit();
    },

    _initNavigator() {
      if (!this.cy || typeof this.cy.navigator !== 'function') return;
      try {
        // cytoscape-navigator 2.0.2：默认在 cy 容器右下角生成小地图
        this._navigator = this.cy.navigator({
          viewLiveFramerate: 0,
          dblClickDelay: 200,
        });
      } catch (e) {
        console.error('navigator init error', e);
        this._navigator = null;
      }
    },

    _teardownNavigator() {
      if (this._navigator && typeof this._navigator.destroy === 'function') {
        try { this._navigator.destroy(); } catch (_) {}
      }
      this._navigator = null;
    },

    // 边标签 LOD：缩小时隐藏全部边标签（text-opacity 0），放大到阈值以上再恢复，
    // 消除大图满屏飘字；隐藏时点边仍可选中，避免信息完全丢失
    _setupEdgeLabelLod() {
      const cy = this.cy;
      if (!cy) return;
      const EDGE_LABEL_MIN_ZOOM = 0.8;
      const apply = () => {
        const hide = cy.zoom() < EDGE_LABEL_MIN_ZOOM;
        cy.batch(() => {
          cy.elements('edge').addClass(hide ? 'hide-labels' : '');
        });
      };
      cy.on('zoom', apply);
      this._edgeLabelLodApply = apply;
      apply();
    },

    teardownEdgeLabelLod() {
      if (this.cy && this._edgeLabelLodApply) {
        this.cy.removeListener('zoom', this._edgeLabelLodApply);
        this._edgeLabelLodApply = null;
      }
    },

    graphStyles() {
      const common = { 'text-valign':'center','text-halign':'center','font-family':'ArkPixel,SFMono-Regular,JetBrains Mono,PingFang SC,monospace' };
      return [
        // 起点/目标：浅蓝填充白底卡片
        { selector: 'node[nodeType="origin"]', style: { ...common, shape:'round-rectangle','background-color':'#e0f2fe','background-opacity':0.95,label:'data(label)',color:'#075985','font-size':'12px','font-weight':'bold','text-wrap':'wrap','text-max-width':'128px',width:'data(width)',height:'data(height)','border-width':1.6,'border-color':'#38bdf8','border-opacity':0.9,'text-line-height':1.35 }},
        { selector: 'node[nodeType="goal"]', style: { ...common, shape:'round-rectangle','background-color':'#fce7f3','background-opacity':0.95,label:'data(label)',color:'#9d174d','font-size':'12px','font-weight':'bold','text-wrap':'wrap','text-max-width':'132px',width:'data(width)',height:'data(height)','border-width':1.6,'border-color':'#ec4899','border-opacity':0.85,'text-line-height':1.35 }},
        { selector: 'node[nodeType="fact"]', style: { ...common, shape:'round-rectangle','background-color':'#ffffff','background-opacity':0.96,label:'data(label)',color:'#0f172a','font-size':'12px','font-weight':'500','text-wrap':'wrap','text-max-width':'152px',width:'data(width)',height:'data(height)','border-width':1.4,'border-color':'#cbd5e1','border-opacity':0.9 }},
        // 探索中/待认领：状态色小卡片（白底+语义色描边，替代原 ? 圆点）
        { selector: 'node[nodeType="in_progress"]', style: { ...common, shape:'round-rectangle','background-color':'#fff7ed','background-opacity':0.95,label:'data(label)',color:'#9a3412','font-size':'11px','font-weight':'bold',width:'data(width)',height:'data(height)','border-width':1.5,'border-color':'#fb923c','border-opacity':0.9 }},
        { selector: 'node[nodeType="unclaimed"]', style: { ...common, shape:'round-rectangle','background-color':'#f8fafc','background-opacity':0.95,label:'data(label)',color:'#64748b','font-size':'11px','font-weight':'bold',width:'data(width)',height:'data(height)','border-width':1.3,'border-color':'#cbd5e1','border-style':'dashed','border-opacity':0.85 }},
        // 待审批：橙色星标卡片（替代红色小圆点「审」），与大屏横幅联动
        { selector: 'node[nodeType="pending_approval"]', style: { ...common, shape:'round-rectangle','background-color':'#fff7ed','background-opacity':0.98,label:'data(label)',color:'#c2410c','font-size':'12px','font-weight':'bold',width:'data(width)',height:'data(height)','border-width':2,'border-color':'#f97316','border-opacity':0.95,'underlay-color':'#f97316','underlay-opacity':0.12,'underlay-padding':3 }},
        { selector: 'node[nodeType="bootstrap_pending"]', style: { ...common, shape:'round-rectangle','background-color':'#fff7ed','background-opacity':0.95,label:'data(label)',color:'#9a3412','font-size':'12px','font-weight':'bold',width:'data(width)',height:'data(height)','border-width':1.5,'border-color':'#fb923c','border-style':'dashed','border-opacity':0.9 }},
        { selector: 'node[nodeType="bootstrap_running"]', style: { ...common, shape:'round-rectangle','background-color':'#fb923c','background-opacity':0.98,label:'data(label)',color:'#ffffff','font-size':'12px','font-weight':'bold',width:'data(width)',height:'data(height)','border-width':2,'border-color':'#fdba74','text-wrap':'wrap','text-max-width':'70px' }},
        // 人工标注不可信的证据（批次 11.1）：琥珀虚线警示，覆盖上面的实线底
        { selector: 'node[?untrusted]', style: { 'border-style':'dashed','border-width':2.2,'border-color':'#f59e0b','border-opacity':1,'background-color':'#fffbeb','background-opacity':1,color:'#92400e' }},

        { selector: 'edge[status="concluded"]', style: { width:1.8,'line-color':'#94a3b8','target-arrow-color':'#94a3b8','target-arrow-shape':'triangle','curve-style':'bezier',label:'data(label)','font-size':'9px',color:'#475569','text-rotation':'autorotate','text-margin-y':-9,'text-max-width':'110px','text-wrap':'ellipsis','text-background-color':'#ffffff','text-background-opacity':0.85,'text-background-padding':'2px','text-events':'yes','arrow-scale':0.8 }},
        { selector: 'edge[status="in_progress"]', style: { width:1.8,'line-color':'#f59e0b','line-style':'dashed','line-dash-pattern':[8,4],'line-dash-offset':0,'target-arrow-color':'#f59e0b','target-arrow-shape':'triangle','curve-style':'bezier',label:'data(label)','font-size':'9px',color:'#b45309','text-rotation':'autorotate','text-margin-y':-9,'text-max-width':'80px','text-wrap':'ellipsis','text-background-color':'#ffffff','text-background-opacity':0.85,'text-background-padding':'2px','text-events':'yes','arrow-scale':0.8 }},
        { selector: 'edge[status="unclaimed"]', style: { width:1.4,'line-color':'#94a3b8','line-style':'dashed','line-dash-pattern':[5,5],'target-arrow-color':'#94a3b8','target-arrow-shape':'triangle','curve-style':'bezier',label:'data(label)','font-size':'9px',color:'#64748b','text-rotation':'autorotate','text-margin-y':-9,'text-max-width':'80px','text-wrap':'ellipsis','text-background-color':'#ffffff','text-background-opacity':0.85,'text-background-padding':'2px','text-events':'yes','arrow-scale':0.7 }},
        { selector: 'edge[status="pending_approval"]', style: { width:2,'line-color':'#f97316','line-style':'dashed','line-dash-pattern':[6,3],'target-arrow-color':'#f97316','target-arrow-shape':'triangle','curve-style':'bezier',label:'data(label)','font-size':'9px',color:'#c2410c','text-rotation':'autorotate','text-margin-y':-9,'text-max-width':'80px','text-wrap':'ellipsis','text-background-color':'#ffffff','text-background-opacity':0.88,'text-background-padding':'2px','text-events':'yes','arrow-scale':0.8 }},
        { selector: 'edge[status="bootstrap_pending"]', style: { width:1.8,'line-color':'#fb923c','line-style':'dashed','line-dash-pattern':[8,4],'line-dash-offset':0,'target-arrow-color':'#fb923c','target-arrow-shape':'triangle','curve-style':'bezier',label:'data(label)','font-size':'9px',color:'#9a3412','text-rotation':'autorotate','text-margin-y':-9,'text-max-width':'88px','text-wrap':'ellipsis','text-background-color':'#ffffff','text-background-opacity':0.85,'text-background-padding':'2px','text-events':'yes','arrow-scale':0.85 }},
        { selector: 'edge[status="bootstrap_running"]', style: { width:2.2,'line-color':'#fb923c','line-style':'dashed','line-dash-pattern':[10,4],'line-dash-offset':0,'target-arrow-color':'#fb923c','target-arrow-shape':'triangle','curve-style':'bezier',label:'data(label)','font-size':'9px',color:'#9a3412','text-rotation':'autorotate','text-margin-y':-9,'text-max-width':'88px','text-wrap':'ellipsis','text-background-color':'#ffffff','text-background-opacity':0.88,'text-background-padding':'2px','text-events':'yes','arrow-scale':0.9 }},
        { selector: 'edge[edgeType="bootstrap_scope"]', style: { label:'',width:1.8,'curve-style':'bezier','line-style':'dotted','line-dash-pattern':[2,5],'target-arrow-shape':'triangle-backcurve','arrow-scale':0.75,'target-distance-from-node':2 }},

        { selector: '.highlight', style: { 'z-index':999 }},
        { selector: 'edge.highlight', style: { 'z-index':999 }},
        { selector: 'edge.hide-labels', style: { 'text-opacity':0, 'text-background-opacity':0 }},
        { selector: 'node.focus', style: { 'border-width':3,'border-color':'#ff6b2c','border-opacity':0.98,'z-index':1000 }},
        { selector: 'edge.focus', style: { 'z-index':1000,'overlay-color':'#22d3ee','overlay-opacity':0.22,'overlay-padding':5 }},
        { selector: 'node.selected-fact', style: { 'border-width':0,'underlay-color':'#22d3ee','underlay-padding':8,'underlay-opacity':0.26,'z-index':1001 }},
        { selector: '.faded', style: { opacity:0.5 }},
        { selector: '.fresh', style: { opacity:0 }},
        // 图过滤（6.2）：f-hide = 不可见且不可交互；保留位置避免整图重排
        { selector: '.f-hide', style: { opacity: 0, events: 'no' } },
        // 搜索命中：橙色描边高亮（比 selected-fact 的 underlay 更醒目）
        { selector: '.search-hit', style: { 'border-width': 3, 'border-color': '#ff6b2c', 'border-opacity': 0.95, 'z-index': 1002 } },

        // 漏洞标记 — 红色角标卡片（第一梯队）：高危/严重红底白字，中/低危描边红
        { selector: 'node.vuln-critical', style: { 'background-color':'#fee2e2','background-opacity':0.98,'border-width':2,'border-color':'#dc2626','border-opacity':0.98,'color':'#991b1b','font-weight':'bold','underlay-color':'#dc2626','underlay-opacity':0.1,'underlay-padding':5,'z-index':998 }},
        { selector: 'node.vuln-high', style: { 'background-color':'#ffedd5','background-opacity':0.98,'border-width':2.5,'border-color':'#ea580c','border-opacity':0.95,'color':'#9a3412','font-weight':700,'underlay-color':'#ea580c','underlay-opacity':0.08,'underlay-padding':4,'z-index':997 }},
        { selector: 'node.vuln-medium', style: { 'background-color':'#fef9c3','background-opacity':0.98,'border-width':2,'border-color':'#d97706','border-opacity':0.9,'z-index':996 }},
        { selector: 'node.vuln-low', style: { 'background-color':'#f0f9ff','background-opacity':0.98,'border-width':1.8,'border-color':'#0284c7','border-opacity':0.85,'z-index':995 }},
        { selector: 'node.vuln-info', style: { 'background-color':'#f1f5f9','background-opacity':0.98,'border-width':1.6,'border-color':'#64748b','border-opacity':0.8,'z-index':994 }},
      ];
    },

    layoutOpts(animate = true, fit = true) {
      const direction = this.layoutDirection();
      if (this.layoutEngine() === 'elk') {
        const elkDirection = direction === 'TB' ? 'DOWN' : 'RIGHT';
        return {
          name: 'elk',
          fit,
          padding: 50,
          animate,
          animationDuration: 350,
          animationEasing: 'ease-in-out-cubic',
          elk: {
            algorithm: 'layered',
            'elk.direction': elkDirection,
            'elk.aspectRatio': '1.5',
            'elk.layered.nodePlacement.strategy': 'BRANDES_KOEPF',
            'elk.spacing.nodeNode': '50',
            'elk.layered.spacing.nodeNodeBetweenLayers': '80',
            'elk.spacing.edgeNode': '25',
            'elk.layered.crossingMinimization.strategy': 'LAYER_SWEEP',
            'elk.layered.nodePlacement.bk.fixedAlignment': 'BALANCED',
          },
        };
      }
      if (this.layoutEngine() === 'klay') {
        const isHorizontal = direction === 'LR';
        return {
          name: 'klay',
          fit,
          padding: 50,
          animate,
          animationDuration: 400,
          animationEasing: 'ease-in-out-cubic',
          klay: {
            direction: direction === 'TB' ? 'DOWN' : 'RIGHT',
            edgeRouting: 'POLYLINE',
            crossingMinimization: 'LAYER_SWEEP',
            nodeLayering: 'NETWORK_SIMPLEX',
            nodePlacement: 'BRANDES_KOEPF',
            separateConnectedComponents: true,
            spacing: isHorizontal ? 52 : 40,
            inLayerSpacingFactor: isHorizontal ? 1.15 : 1.0,
            thoroughness: 8,
          },
        };
      }
      return {
        name: 'dagre',
        rankDir: direction,
        nodeSep: 60,
        rankSep: 80,
        padding: 50,
        fit,
        animate,
        animationDuration: 400,
        animationEasing: 'ease-in-out-cubic',
      };
    },

    snapshotNodePositions() {
      const positions = new Map();
      if (!this.cy) return positions;
      this.cy.nodes().forEach(node => {
        positions.set(node.id(), { x: node.position('x'), y: node.position('y') });
      });
      return positions;
    },

    anchorPositionFromIds(nodeIds, previousPositions, offset = 36) {
      const anchors = [];
      for (const nodeId of nodeIds) {
        const existing = this.cy?.getElementById(nodeId);
        if (existing?.length) {
          anchors.push({ x: existing.position('x'), y: existing.position('y') });
          continue;
        }
        const previous = previousPositions.get(nodeId);
        if (previous) anchors.push(previous);
      }
      if (anchors.length === 0) return null;
      const center = anchors.reduce((acc, pos) => ({ x: acc.x + pos.x, y: acc.y + pos.y }), { x: 0, y: 0 });
      const avg = { x: center.x / anchors.length, y: center.y / anchors.length };
      return this.layoutDirection() === 'TB'
        ? { x: avg.x, y: avg.y + offset }
        : { x: avg.x + offset, y: avg.y };
    },

    initialPositionForNode(nodeData, previousPositions) {
      if (!this.project) return null;
      if (nodeData.intentId) {
        const previousPlaceholder = previousPositions.get(nodeData.id);
        if (previousPlaceholder) return previousPlaceholder;
        const intent = this.project.intents.find(item => item.id === nodeData.intentId);
        return intent ? this.anchorPositionFromIds(intent.from, previousPositions, 32) : null;
      }

      const producingIntent = this.project.intents.find(item => item.to === nodeData.id);
      if (!producingIntent) return null;
      const placeholderPosition = previousPositions.get(`_ph_${producingIntent.id}`);
      if (placeholderPosition) return placeholderPosition;
      return this.anchorPositionFromIds(producingIntent.from, previousPositions, 44);
    },

    fadeInFreshElement(element) {
      if (!element || element.length === 0) return;
      element.addClass('fresh');
      setTimeout(() => {
        if (!element.inside()) return;
        element.animate(
          { style: { opacity: 1 } },
          {
            duration: 500,
            complete: () => {
              if (!element.inside()) return;
              element.removeStyle('opacity');
            },
          },
        );
        element.removeClass('fresh');
      }, 30);
    },

    _applyVulnClass(node) {
      if (!node || node.length === 0) return;
      node.removeClass('vuln-critical vuln-high vuln-medium vuln-low vuln-info');
      const sev = node.data('hasVuln');
      if (sev) node.addClass(`vuln-${sev}`);
    },

    updateGraph() {
      if (!this.cy || !this.project) return;
      const { nodes, edges } = this.buildElements();
      const wantNodes = new Set(nodes.map(n => n.data.id));
      const wantEdges = new Set(edges.map(e => e.data.id));
      const previousPositions = this.snapshotNodePositions();
      let changed = false;

      this.cy.nodes().forEach(n => { if (!wantNodes.has(n.id())) { n.remove(); changed = true; } });
      this.cy.edges().forEach(e => { if (!wantEdges.has(e.id())) { e.remove(); changed = true; } });

      for (const n of nodes) {
        const ex = this.cy.getElementById(n.data.id);
        if (ex.length === 0) {
          const initialPosition = this.initialPositionForNode(n.data, previousPositions);
          const a = this.cy.add(initialPosition ? { ...n, position: initialPosition } : n);
          this._applyVulnClass(a);
          this.fadeInFreshElement(a);
          changed = true;
        } else if (
          ex.data('nodeType') !== n.data.nodeType ||
          ex.data('label') !== n.data.label ||
          ex.data('description') !== n.data.description ||
          ex.data('width') !== n.data.width ||
          ex.data('height') !== n.data.height ||
          ex.data('hasVuln') !== (n.data.hasVuln || null)
        ) {
          ex.data(n.data); changed = true;
          this._applyVulnClass(ex);
        }
      }
      for (const e of edges) {
        const ex = this.cy.getElementById(e.data.id);
        if (ex.length === 0) {
          const a = this.cy.add(e);
          this.fadeInFreshElement(a);
          changed = true;
        } else if (ex.data('status') !== e.data.status) {
          ex.data(e.data);
        }
      }
      // SSE 增量刷新：不 auto-fit（fit:false），避免每次新节点都把用户正在
      // 查看的视口强行缩回全图。新节点经 initialPositionForNode 锚定在就近
      // 位置后由 layout 平滑归位；若超出可视区，右下角 navigator 小地图会
      // 提示，用户自行追踪。首次进图/手动切布局仍走 fit:true（layoutOpts 默认）。
      if (changed) this.cy.layout(this.layoutOpts(true, false)).run();
      this.pulseActive();
      this.markStaleIntents();
      this.refreshGraphDecorations();
      // 增量后重应用过滤（新节点/新边需被当前过滤规则覆盖）
      if (this.graphFilterVuln || this.graphFilterConcluded || this.graphFilterBloodline) {
        this.applyGraphFilters();
      }
    },

    pulseActive() {
      if (!this.cy) return;
      this.cy.nodes('[nodeType="in_progress"], [nodeType="bootstrap_running"]').forEach(node => {
        if (node.scratch('_pulseActive')) return;
        node.scratch('_pulseActive', true);
        const isBootstrap = node.data('nodeType') === 'bootstrap_running';
        const minOpacity = isBootstrap ? 0.55 : 0.35;
        const maxOpacity = isBootstrap ? 0.98 : 0.8;
        const pulseOut = () => {
          if (!node.inside()) {
            node.removeScratch('_pulseActive');
            return;
          }
          node.animate({ style:{'background-opacity':minOpacity} }, { duration:900, complete: pulseIn });
        };
        const pulseIn = () => {
          if (!node.inside()) {
            node.removeScratch('_pulseActive');
            return;
          }
          node.animate({ style:{'background-opacity':maxOpacity} }, { duration:900, complete: pulseOut });
        };
        pulseOut();
      });
      this.cy.edges('[status="in_progress"], [status="bootstrap_running"]').forEach(edge => {
        if (edge.scratch('_flowActive')) return;
        edge.scratch('_flowActive', true);
        const isBootstrap = edge.data('status') === 'bootstrap_running';
        const dashOffset = isBootstrap ? -16 : -12;
        const flow = () => {
          if (!edge.inside() || !['in_progress', 'bootstrap_running'].includes(edge.data('status'))) {
            edge.removeScratch('_flowActive');
            return;
          }
          edge.animate({ style:{'line-dash-offset':dashOffset} }, {
            duration:isBootstrap ? 620 : 700,
            complete:() => {
              if (!edge.inside() || !['in_progress', 'bootstrap_running'].includes(edge.data('status'))) {
                edge.removeScratch('_flowActive');
                return;
              }
              edge.style('line-dash-offset', 0);
              flow();
            }
          });
        };
        flow();
      });
    },

    fitGraph() { if (this.cy) this.cy.fit(undefined, 50); },

    markStaleIntents() {
      // 心跳状态仅在侧边面板显示，不在图上动态修改边框
      return;
    },

    centerGraphOnElements(eles) {
      if (!this.cy || !eles || eles.length === 0) return;
      if (this._centerAnimation) this._centerAnimation.stop();
      this._centerAnimation = this.cy.animation({ center: { eles }, duration: 220, easing: 'ease-in-out-cubic' });
      this._centerAnimation.play();
    },

    centerGraphOnFact(factId) {
      if (!this.cy || !factId) return;
      const node = this.cy.getElementById(factId);
      if (node.length > 0) this.centerGraphOnElements(node);
    },

    centerGraphOnIntent(intentId) {
      if (!this.cy || !intentId) return;
      const edges = this.cy.edges(`[intentId="${intentId}"]`);
      if (edges.length === 0) return;
      this.centerGraphOnElements(edges.add(edges.sources()).add(edges.targets()));
    },

    // ── 图过滤 / 搜索（6.2）───────────────────────────────────────────
    graphFilterToggle(key) {
      if (key === 'vuln') this.graphFilterVuln = !this.graphFilterVuln;
      else if (key === 'concluded') this.graphFilterConcluded = !this.graphFilterConcluded;
      else if (key === 'bloodline') this.graphFilterBloodline = !this.graphFilterBloodline;
      this.applyGraphFilters();
    },

    applyGraphFilters() {
      if (!this.cy || !this.project) return;
      this.cy.batch(() => {
        // 1) 只看高危/严重：隐藏 hasVuln 为空或为 low/medium 的 fact 节点及其孤立边
        this.cy.nodes().removeClass('f-hide');
        this.cy.edges().removeClass('f-hide');
        if (this.graphFilterVuln) {
          this.cy.nodes('[nodeType="fact"]').forEach(n => {
            const sev = n.data('hasVuln');
            if (!sev || !['critical', 'high'].includes(sev)) n.addClass('f-hide');
          });
          // 清理变成孤岛的节点（其所有边都连向被隐藏节点/自身被隐藏）
          this._pruneHiddenOrphans();
        }
        // 2) 隐藏已结论边
        if (this.graphFilterConcluded) {
          this.cy.edges('[status="concluded"]').addClass('f-hide');
        }
        // 3) 仅看血缘：依赖选区（focusFactIds / selectedFactIds 等），先清理后按血缘亮链
        if (this.graphFilterBloodline) {
          const anchor = this._bloodlineAnchorNode();
          if (anchor) this._applyBloodlineView(anchor);
          else this.graphFilterBloodline = false; // 无锚点则自动关闭
        }
      });
      this.refreshGraphDecorations?.();
    },

    _pruneHiddenOrphans() {
      // 隐藏所有端点都被隐藏的边；若某非 fact 节点（origin/goal/intent 占位）因此孤立，
      // 保留其入边（它们表达语义）。只处理 fact 节点隐藏后悬空的 concluded 边。
      const hidden = new Set();
      this.cy.nodes('.f-hide').forEach(n => hidden.add(n.id()));
      this.cy.edges().forEach(e => {
        if (hidden.has(e.source().id()) && hidden.has(e.target().id())) e.addClass('f-hide');
      });
    },

    _bloodlineAnchorNode() {
      // 优先级：当前选中 fact > 当前选中 intent 的占位节点 > goal
      if (this.selectedFactIds && this.selectedFactIds.length > 0) {
        const n = this.cy.getElementById(this.selectedFactIds[0]);
        if (n.length) return n;
      }
      if (this.selectedIntentId && this.selectedIntentId.length) {
        const n = this.cy.getElementById(`_ph_${this.selectedIntentId}`);
        if (n.length) return n;
      }
      const g = this.cy.getElementById('goal');
      return g.length ? g : null;
    },

    _applyBloodlineView(anchor) {
      // 显示"通向 anchor 的血缘证据链"：从 anchor 反向沿边 BFS，收集所有
      // 指向链上节点的 fact / intent 占位 / origin / goal，隐藏其余。
      const keep = new Set([anchor.id(), 'origin', 'goal']);
      const queue = [anchor.id()];
      const seen = new Set(queue);
      const MAX_NODES = 200;
      while (queue.length && keep.size < MAX_NODES) {
        const id = queue.shift();
        const node = this.cy.getElementById(id);
        if (!node.length) continue;
        for (const e of node.connectedEdges()) {
          const other = e.source().id() === id ? e.target() : e.source();
          const oid = other.id();
          // intent 占位节点（_ph_*）也要保留，它们表示"待办行动"本身
          if (oid === 'origin' || oid === 'goal' || oid.startsWith('_ph_') || other.data('nodeType') === 'fact') {
            if (!seen.has(oid)) { seen.add(oid); keep.add(oid); queue.push(oid); }
          }
        }
      }
      this.cy.nodes().forEach(n => {
        if (keep.has(n.id())) n.removeClass('f-hide'); else n.addClass('f-hide');
      });
      this.cy.edges().forEach(e => {
        const s = e.source().id(), t = e.target().id();
        if (keep.has(s) && keep.has(t)) e.removeClass('f-hide'); else e.addClass('f-hide');
      });
    },

    searchGraph() {
      if (!this.cy) return;
      const q = (this.graphSearch || '').trim().toLowerCase();
      this.cy.elements().removeClass('search-hit');
      if (!q) return;
      const hits = this.cy.nodes().filter(n => {
        const hay = [n.id(), n.data('label') || '', n.data('description') || ''].join(' ').toLowerCase();
        return hay.includes(q);
      });
      if (hits.length === 0) return;
      hits.addClass('search-hit');
      // 若节点被过滤隐藏，临时显示它以便居中
      hits.removeClass('f-hide');
      this.centerGraphOnElements(hits.first());
      this._searchHits = hits;
    },
    clearGraphSearch() {
      this.graphSearch = '';
      if (this.cy) this.cy.elements().removeClass('search-hit');
      this.applyGraphFilters();
    },
    nextGraphSearchHit() {
      if (!this._searchHits || this._searchHits.length === 0) return;
      const list = this._searchHits;
      const idx = (this._searchIdx || 0) + 1;
      this._searchIdx = idx % list.length;
      this.centerGraphOnElements(list[this._searchIdx]);
    },

    summarizeFactLabel(fact) {
      if (fact.id === 'origin') return '起点';
      if (fact.id === 'goal') return '目标';
      const normalized = fact.description.replace(/\s+/g, ' ').trim();
      const chars = Array.from(normalized);
      if (chars.length <= 24) return normalized || fact.id;
      return `${chars.slice(0, 24).join('')}…`;
    },

    // 卡片式标签：两行结构
    //   第 1 行：状态行（线索/起点/目标 + 高危等级 + 漏洞数 ×N）
    //   第 2 行：描述（≤24 字，多行时 wrap）
    factCardLabel(fact, nodeType, topSev, vulnCount) {
      const desc = this.summarizeFactLabel(fact);
      const status = nodeType === 'origin' ? '起点' : nodeType === 'goal' ? '目标' : '线索';
      // 起点/目标：单行状态词即可
      if (nodeType === 'origin' || nodeType === 'goal') return status;
      if (topSev) {
        const sevCn = topSev === 'critical' ? '严重' : topSev === 'high' ? '高危' : topSev === 'medium' ? '中危' : topSev === 'low' ? '低危' : '提示';
        const count = vulnCount ? ` ×${vulnCount}` : '';
        return `${status} · ${sevCn}${count}\n${desc}`;
      }
      return `${status}\n${desc}`;
    },

    factNodeSize(label, nodeType) {
      const fontSize = 12;
      const lineH = 16;
      const padX = 14;
      const padY = 11;
      const maxDescW = nodeType === 'fact' ? 148 : 120;
      const isCjk = c => /[\u2E80-\u9FFF\uF900-\uFAFF\uFF00-\uFF60]/.test(c);
      const charW = c => isCjk(c) ? fontSize : fontSize * 0.58;
      const lines = (label || '').split('\n');
      const firstLine = lines[0] || '';
      let descLineCount = 0;
      lines.slice(1).forEach(l => {
        if (!l) return;
        let w = 0, count = 1;
        for (const c of Array.from(l)) {
          w += charW(c);
          if (w > maxDescW) { count += 1; w = 0; }
        }
        descLineCount += count;
      });
      const totalLines = 1 + descLineCount;
      const firstW = Array.from(firstLine).reduce((s, c) => s + charW(c), 0);
      const width = Math.max(92, Math.min(220, Math.ceil(firstW) + padX * 2));
      const height = Math.max(44, Math.ceil(totalLines * lineH + padY * 2));
      return { width, height };
    },

    measureWrappedText(text, maxWidth, fontSize) {
      const content = (text || '').trim() || ' ';
      const lines = [];
      let currentWidth = 0;
      let currentChars = 0;
      let maxLineWidth = 0;

      const pushLine = () => {
        if (currentChars === 0 && lines.length > 0) lines.push(0);
        else if (currentChars > 0) lines.push(currentWidth);
        maxLineWidth = Math.max(maxLineWidth, currentWidth);
        currentWidth = 0;
        currentChars = 0;
      };

      for (const char of Array.from(content)) {
        if (char === '\n') {
          pushLine();
          continue;
        }
        const charWidth = this.estimateLabelCharWidth(char, fontSize);
        if (currentChars > 0 && currentWidth + charWidth > maxWidth) pushLine();
        currentWidth += charWidth;
        currentChars += 1;
      }

      pushLine();

      const lineCount = Math.max(1, lines.length);
      const lineHeight = fontSize * 1.35;
      return {
        width: Math.min(maxWidth, Math.max(fontSize * 1.6, maxLineWidth)),
        height: lineCount * lineHeight,
      };
    },

    estimateLabelCharWidth(char, fontSize) {
      if (/\s/.test(char)) return fontSize * 0.35;
      if (/[\u1100-\u115F\u2E80-\uA4CF\uAC00-\uD7A3\uF900-\uFAFF\uFE10-\uFE6F\uFF00-\uFF60\uFFE0-\uFFE6]/.test(char)) {
        return fontSize * 1.0;
      }
      return fontSize * 0.58;
    },

    setupAutoFit() {
      this.teardownAutoFit();
      const container = document.getElementById('cy');
      if (!container || !this.cy) return;
      let fitTimer = null;
      this._resizeObserver = new ResizeObserver(() => {
        clearTimeout(fitTimer);
        fitTimer = setTimeout(() => {
          if (this.cy) {
            const clampedWidth = this.clampPanelWidth(this.sidePanelWidth);
            if (clampedWidth !== this.sidePanelWidth) {
              this.sidePanelWidth = clampedWidth;
              this.saveSidePanelWidth();
            }
            this.cy.resize();
            this.cy.fit(undefined, 50);
          }
        }, 200);
      });
      this._resizeObserver.observe(container);
    },

    teardownAutoFit() {
      if (this._resizeObserver) {
        this._resizeObserver.disconnect();
        this._resizeObserver = null;
      }
    },

    applySelectedLayout() {
      this.layoutMode = this.isValidLayoutMode(this.layoutMode) ? this.layoutMode : 'dagre_tb';
      this.localPrefs.layout_mode = this.layoutMode;
      this.saveLocalPrefs();
      if (this.cy) this.cy.layout(this.layoutOpts()).run();
    },

    clampPanelWidth(width) {
      const containerWidth = document.getElementById('graphLayout')?.getBoundingClientRect().width || window.innerWidth;
      const min = 260;
      const max = Math.max(min, containerWidth - 260);
      return Math.min(max, Math.max(min, width));
    },

    startPanelResize(e) {
      e.currentTarget?.setPointerCapture?.(e.pointerId);
      this.isResizingPanel = true;
      this.onPanelResize(e);
    },

    onPanelResize(e) {
      if (!this.isResizingPanel) return;
      const rect = document.getElementById('graphLayout')?.getBoundingClientRect();
      if (!rect) return;
      this.sidePanelWidth = this.clampPanelWidth(rect.right - e.clientX);
    },

    stopPanelResize() {
      if (!this.isResizingPanel) return;
      this.isResizingPanel = false;
      this.saveSidePanelWidth();
    },

    clearGraphSelection(preserveTimeline = false) {
      this.selectedNode = null;
      this.selectedFacts = [];
      if (!preserveTimeline) this.selectedTimelineEntryId = null;
      if (this.cy) this.cy.elements().removeClass('highlight focus faded selected-fact');
    },

    clearSelection() {
      this.clearGraphSelection(false);
    },

    toggleFactSelection(fid) {
      this.selectedTimelineEntryId = null;
      const idx = this.selectedFacts.indexOf(fid);
      if (idx >= 0) {
        this.selectedFacts.splice(idx, 1);
        if (this.selectedFacts.length === 0) {
          this.clearSelection();
          return;
        }
        if (this.selectedNode?.type === 'fact' && this.selectedNode.id === fid) {
          this.selectedNode = { type:'fact', id: this.selectedFacts[this.selectedFacts.length - 1] };
        }
      } else {
        this.selectedFacts.push(fid);
        this.selectedNode = { type:'fact', id: fid };
      }
      this.refreshGraphDecorations();
    },

    removeFactSelection(fid) {
      this.selectedTimelineEntryId = null;
      const idx = this.selectedFacts.indexOf(fid);
      if (idx < 0) return;
      this.selectedFacts.splice(idx, 1);
      if (this.selectedFacts.length === 0) {
        this.clearSelection();
        return;
      }
      if (this.selectedNode?.type === 'fact' && this.selectedNode.id === fid) {
        this.selectedNode = { type:'fact', id: this.selectedFacts[this.selectedFacts.length - 1] };
      }
      this.refreshGraphDecorations();
    },

    onNodeTap(e) {
      const node = e.target;
      const id = node.data('id');
      const nt = node.data('nodeType');
      const keepLogFocus = this.sideTab === 'log';

      if (['in_progress', 'unclaimed', 'bootstrap_pending', 'bootstrap_running', 'pending_approval'].includes(nt)) {
        this.selectIntent(node.data('intentId'));
        if (keepLogFocus) this.scrollTimelineToSelection();
        else this.sideTab = 'detail';
        return;
      }
      if (e.originalEvent.shiftKey) {
        this.toggleFactSelection(id);
        if (keepLogFocus) this.scrollTimelineToSelection();
        else this.sideTab = 'detail';
        return;
      } else {
        this.selectedFacts = [id];
        this.selectFact(id);
      }
      if (keepLogFocus) this.scrollTimelineToSelection();
      else this.sideTab = 'detail';
    },

    onEdgeTap(e) {
      this.selectIntent(e.target.data('intentId'));
      if (this.sideTab === 'log') this.scrollTimelineToSelection();
      else this.sideTab = 'detail';
    },

    selectIntent(intent) {
      const intentId = typeof intent === 'string' ? intent : intent?.id;
      if (!intentId) return;
      this.selectedFacts = [];
      this.selectedTimelineEntryId = null;
      this.selectedNode = { type:'intent', id: intentId };
      this.applyLineageHighlightForIntent(intentId);
    },

    selectFact(fact) {
      const factId = typeof fact === 'string' ? fact : fact?.id;
      if (!factId) return;
      if (!this.selectedFacts.includes(factId)) this.selectedFacts = [factId];
      this.selectedTimelineEntryId = null;
      this.selectedNode = { type:'fact', id: factId };
      if (this.selectedFacts.length > 1) {
        this.applyMultiFactSelectionHighlight();
        return;
      }
      this.applyLineageHighlightForFact(factId);
    },

    refreshGraphDecorations() {
      if (!this.selectedNode) {
        this.syncFactSelections();
        return;
      }
      if (this.selectedNode.type === 'intent') this.applyLineageHighlightForIntent(this.selectedNode.id);
      if (this.selectedNode.type === 'fact') {
        if (this.selectedFacts.length > 1 && this.selectedFacts.includes(this.selectedNode.id)) {
          this.applyMultiFactSelectionHighlight();
          return;
        }
        this.applyLineageHighlightForFact(this.selectedNode.id);
      }
    },

    syncFactSelections() {
      if (!this.cy) return;
      this.cy.nodes().removeClass('selected-fact');
      for (const fid of this.selectedFacts) {
        const node = this.cy.getElementById(fid);
        if (node.length > 0) node.addClass('selected-fact');
      }
    },

    collectFactLineage(fid) {
      const upstreamFacts = new Set();
      const upstreamIntents = new Set();

      const walkFactUpstream = (factId) => {
        if (upstreamFacts.has(factId) || !this.project) return;
        upstreamFacts.add(factId);
        for (const intent of this.project.intents) {
          if (intent.to === factId) walkIntentUpstream(intent.id);
        }
      };

      const walkIntentUpstream = (iid) => {
        if (upstreamIntents.has(iid) || !this.project) return;
        upstreamIntents.add(iid);
        const intent = this.project.intents.find(i => i.id === iid);
        if (!intent) return;
        for (const sourceId of intent.from) walkFactUpstream(sourceId);
      };

      walkFactUpstream(fid);
      return { upstreamFacts, upstreamIntents };
    },

    collectIntentElements(intent, nodeIds, edgeIds) {
      if (intent.to) nodeIds.add(intent.to);
      else nodeIds.add(`_ph_${intent.id}`);
      for (const sourceId of intent.from) {
        nodeIds.add(sourceId);
        edgeIds.add(`${intent.id}_${sourceId}`);
      }
      if (this.isBootstrapIntent(intent)) {
        nodeIds.add('goal');
        edgeIds.add(`${intent.id}_goal`);
      }
    },

    applyLineageHighlightForIntent(intentId) {
      if (!this.cy || !this.project) return;
      const intent = this.project.intents.find(i => i.id === intentId);
      if (!intent) return;
      this.cy.elements().removeClass('highlight focus faded');

      const nodeIds = new Set();
      const edgeIds = new Set();
      this.collectIntentElements(intent, nodeIds, edgeIds);
      const highlightNodes = this.cy.nodes().filter(n => nodeIds.has(n.id()));
      const focusEdges = this.cy.edges().filter(e => edgeIds.has(e.id()));

      highlightNodes.addClass('highlight');
      focusEdges.addClass('focus');

      const visible = highlightNodes.add(focusEdges);
      this.cy.elements().not(visible).addClass('faded');
      this.syncFactSelections();
    },

    applyLineageHighlightForFact(factId) {
      if (!this.cy || !this.project) return;
      const { upstreamFacts, upstreamIntents } = this.collectFactLineage(factId);
      const nodeIds = new Set(upstreamFacts);
      const edgeIds = new Set();

      for (const iid of upstreamIntents) {
        const intent = this.project.intents.find(i => i.id === iid);
        if (intent) this.collectIntentElements(intent, nodeIds, edgeIds);
      }

      this.cy.elements().removeClass('highlight focus faded');
      const highlightNodes = this.cy.nodes().filter(n => nodeIds.has(n.id()));
      const highlightEdges = this.cy.edges().filter(e => edgeIds.has(e.id()));
      const focusNode = this.cy.getElementById(factId);

      highlightNodes.addClass('highlight');
      highlightEdges.addClass('highlight');
      focusNode.addClass('focus');

      const visible = highlightNodes.add(highlightEdges).add(focusNode);
      this.cy.elements().not(visible).addClass('faded');
      this.syncFactSelections();
    },

    applyMultiFactSelectionHighlight() {
      if (!this.cy) return;
      this.cy.elements().removeClass('highlight focus faded selected-fact');
      this.syncFactSelections();
    },

    getProducingIntent(fid) {
      return this.project ? this.project.intents.find(i => i.to === fid) || null : null;
    },

    selectedFactId() {
      return this.selectedNode?.type === 'fact' ? this.selectedNode.id : null;
    },

    selectedFactRecord() {
      const factId = this.selectedFactId();
      if (!this.project || !factId) return null;
      return this.project.facts.find(f => f.id === factId) || null;
    },

    selectedFactProducingIntent() {
      const factId = this.selectedFactId();
      return factId ? this.getProducingIntent(factId) : null;
    },

    // ── 证据人工纠错（批次 11.1）────────────────────────────
    currentFactTrusted() {
      const r = this.selectedFactRecord();
      return r ? r.trusted !== false : true;
    },
    openFactCorrection() {
      const r = this.selectedFactRecord();
      const factId = this.selectedFactId();
      if (!r || !factId || factId === 'origin') return;
      this.factCorrection = {
        open: true, busy: false,
        description: '', untrusted: r.trusted === false, note: '', edits: [],
      };
      this.loadFactEdits();
    },
    closeFactCorrection() {
      this.factCorrection.open = false;
    },
    async loadFactEdits() {
      const factId = this.selectedFactId();
      const pid = this.selectedProjectId;
      if (!factId || !pid || !this.factCorrection?.open) return;
      try {
        const res = await fetch(`/projects/${pid}/facts/${encodeURIComponent(factId)}/edits`, {
          headers: { 'Authorization': `Bearer ${this.authToken}` },
          signal: this._timeoutSignal(30000),
        });
        if (!res.ok) return;
        this.factCorrection.edits = await res.json();
      } catch (_) { /* 历史读取失败不阻断修正 */ }
    },
    async submitFactCorrection() {
      const factId = this.selectedFactId();
      const pid = this.selectedProjectId;
      if (!factId || !pid || factId === 'origin') return;
      const fc = this.factCorrection;
      const payload = {
        description: fc.description ? fc.description : null,
        untrusted: fc.untrusted,
        note: fc.note || '',
      };
      fc.busy = true;
      try {
        const res = await fetch(`/projects/${pid}/facts/${encodeURIComponent(factId)}/correct`, {
          method: 'POST',
          headers: {
            'Authorization': `Bearer ${this.authToken}`,
            'Content-Type': 'application/json',
          },
          body: JSON.stringify(payload),
          signal: this._timeoutSignal(30000),
        });
        if (!res.ok) {
          const data = await res.json().catch(() => ({}));
          this.showToast((data.detail || '证据修正失败'), 'error');
          return;
        }
        this.closeFactCorrection();
        this.showToast('✅ 证据已记录人工修正');
        if (this.selectedProjectId && this.view === 'graph') {
          await this.loadProject(this.selectedProjectId);
          this.updateGraph();
        }
      } catch (_) {
        this.showToast('证据修正请求失败', 'error');
      } finally {
        fc.busy = false;
      }
    },

    selectedFactRecords() {
      if (!this.project || this.selectedFacts.length === 0) return [];
      const factsById = new Map(this.project.facts.map(f => [f.id, f]));
      return this.selectedFacts.map(fid => factsById.get(fid)).filter(Boolean);
    },

    selectedIntentId() {
      return this.selectedNode?.type === 'intent' ? this.selectedNode.id : null;
    },

    selectedIntentRecord() {
      const intentId = this.selectedIntentId();
      if (!this.project || !intentId) return null;
      return this.project.intents.find(i => i.id === intentId) || null;
    },

    selectedOpenIntentRecord() {
      const intent = this.selectedIntentRecord();
      if (!intent || intent.to || !this.projectIsActive()) return null;
      return intent;
    },

    selectedActionableOpenIntentRecord() {
      const intent = this.selectedOpenIntentRecord();
      if (!intent) return null;
      const actor = this.actorName();
      return !intent.worker || intent.worker === actor ? intent : null;
    },

    selectedReleasableOpenIntentRecord() {
      const intent = this.selectedOpenIntentRecord();
      if (!intent?.worker) return null;
      return intent.worker === this.actorName() ? intent : null;
    },

    selectedIntentPrimaryActionLabel() {
      const intent = this.selectedOpenIntentRecord();
      if (!intent) return '认领';
      if (!intent.worker) return '认领';
      return intent.worker === this.actorName() ? '发送心跳' : '已被认领';
    },

    getFactRecord(fid) {
      return this.project ? this.project.facts.find(f => f.id === fid) || null : null;
    },

    intentDotClass(i) {
      if (i.to) return 'bg-teal-400';
      if (this.isBootstrapIntent(i)) return i.worker ? 'bg-orange-400' : 'bg-orange-200';
      if (i.approval_status === 'pending') return 'bg-rose-400';
      return i.worker ? 'bg-amber-400' : 'bg-slate-300';
    },
    intentStatusClass(i) {
      if (i.abandoned_at) return 'text-slate-400 line-through';
      if (i.to) return 'text-teal-600';
      if (this.isBootstrapIntent(i)) return i.worker ? 'text-orange-600' : 'text-orange-400';
      if (i.approval_status === 'pending') return 'text-rose-500';
      if (i.approval_status === 'rejected' || i.approval_status === 'expired') return 'text-slate-400';
      return i.worker ? 'text-amber-600' : 'text-slate-400';
    },
    intentStatusLabel(i) {
      if (i.abandoned_at) return '已放弃';
      if (i.to) return '已结论';
      if (this.isBootstrapIntent(i)) return i.worker ? '启动中' : '待启动';
      if (i.approval_status === 'pending') return '待审批';
      if (i.approval_status === 'rejected') return '已拒绝';
      if (i.approval_status === 'expired') return '审批过期';
      if (i.approval_status === 'approved') return '已批准';
      return i.worker ? '探索中' : '待认领';
    },
  });
}
