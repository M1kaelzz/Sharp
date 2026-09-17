/**
 * app.js — 组装入口（2026-09-06 重建）
 *
 * 职责：定义全局根组件 data 工厂 `sharpApp()`：
 *   1) 声明全部响应式状态字段（模块方法共享的 this.* 起点）
 *   2) 依序把各功能模块的方法挂到 obj 上（apply*Module）
 *   3) return obj（Alpine 见 obj.init() 自动调用 → 启动鉴权流）
 *
 * index.html <body x-data="sharpApp()">。状态字段 = 模板顶层引用 + 方法内
 * 链式访问所需初值；未在此声明的字段由模块方法运行时惰性创建（Alpine
 * reactive proxy 支持动态 key），模板读 undefined 一律安全（x-show 关闭）。
 * 方法完整性由 scripts/check_methods.py 校验；组装完整性 = 本文件。
 */
function sharpApp() {
  const obj = {
    // ── 认证 / 外观 / 导航 ──────────────────────────────────────────────
    authView: 'loading',        // loading | setup | login | app
    // token 主存 sessionStorage（P0 安全修复）；sharpReadStoredToken 迁移旧 localStorage 值
    authToken: sharpReadStoredToken(),
    authError: '',
    authLoading: false,
    authAgree: false,
    authPassword: '',
    authPasswordConfirm: '',
    darkActive: false,
    themeMode: 'system',   // 模板直接引用（light/system/dark），applyTheme 切换
    isResizingPanel: false,
    view: 'dashboard',          // dashboard | list | newproject | graph | ...
    sideTab: 'detail',
    workbench: 'graph',         // 工作台三视图（批次 D3）：graph | board | chain
    // 免责声明：首次展示；点「我已知悉」后记 localStorage，之后刷新不再弹
    // 授权条款确认：按**会话**记忆（sessionStorage）——刷新不重复打扰，
    // 但每次新会话都必须重新确认。此前用 localStorage 永久记住，等于同意一次就
    // 再也不提示，授权边界被削弱。
    disclaimerOpen: !sessionStorage.getItem('sharp.disclaimer_agreed'),

    // ── 项目列表 / 资产中心 ─────────────────────────────────────────────
    project: {},            // 当前项目详情（loadProject 填充；{} 避免模板 null.title 崩）
    vulns: [],              // 当前/全部漏洞列表（loadVulns 填充）
    vulnsLoading: false,
    showVulnDetail: false,
    vulnDetail: {},
    projects: [],
    projectSearch: '',
    graphSearch: '',
    vulnFilter: { severity: 'all', status: 'all', kind: 'all' },
    scoreboard: null,          // 项目记分板（批次 B：旗帜数/得分）
    subGoals: [],              // 阶段目标（批次 C）
    hypotheses: [],            // 未验证假设（P0-3）
    hypothesisDraft: '',       // 新假设输入（可证伪的命题）
    hypothesisShowClosed: false,  // 是否显示已结算的假设
    subGoalDraft: '',          // 新阶段目标输入
    coverage: null,            // 覆盖报告（P1-A）：打到了什么 / 已验证不通 / 盲区
    coverageLoading: false,
    assetSpace: null,          // 目标空间详情（P2-A）：一个资产键下跨项目的整体状态
    assetSpaceLoading: false,
    assetSpaceOpen: false,
    layoutMode: 'dagre_tb',
    sidePanelWidth: 320,   // 图右侧栏初始宽（loadLocalPrefs 会从 localStorage 覆盖）
    projectStatusFilter: 'all',
    projectStarFilter: 'all',
    projectAssetFilter: '',
    assets: [],
    assetsLoaded: false,
    assetsLoading: false,
    assetsPanelOpen: false,
    _starred: {},
    _projectTags: {},

    // ── 图选区 ────────────────────────────────────────────────────────
    selectedNode: null,
    selectedFacts: [],
    selectedProjectId: '',
    selectedFactIds: [],
    selectedIntentId: null,
    selectedTimelineEntryId: null,

    // ── 通用对话框 ──────────────────────────────────────────────────────
    uiConfirm: { show: false, title: '', message: '', okText: '确定', danger: false, onOk: null },
    uiPrompt: { show: false, title: '', placeholder: '', value: '', onOk: null },
    factCorrection: { open: false, busy: false, description: '', untrusted: false, note: '', edits: [] },
    acceptance: null,
    acceptanceLoading: false,
    globalSearch: { show: false, query: '', results: [], loading: false, done: false },
    reportPreview: { show: false, title: '', content: '', loading: false },

    // ── 项目操作面板 ────────────────────────────────────────────────────
    projectAction: { show: false, mode: '' },
    newProject: { title: '', origin: '', goal: '', hints: [{ content: '' }], targetKind: 'web', assetRef: '', taskMode: 'pentest' },
    intentForm: { description: '' },
    completeForm: { description: '' },
    concludeForm: { description: '' },
    reopenForm: { description: '' },
    hintForm: { content: '' },
    renameForm: { title: '' },
    emergencyForm: {},
    approvalActionNote: '',
    settingsForm: {},
    // localPrefs 细结构：loadLocalPrefs 会读 .actor_name/.layout_mode/.layout_dir，
    // 空对象会让 .actor_name.trim() 抛错中断 init → 必须带默认初值
    localPrefs: { actor_name: '', layout_mode: 'dagre_tb', layout_dir: '' },
    pwForm: {},
    dispatcherMode: 'anthropic',  // 设置页下拉默认（模板 x-model + x-if 面板依赖；'' 会导致面板全空）

    // ── 回放 / 图形（x-model 对象）──────────────────────────────────
    replay: {},
    npPage: { showAddForm: false, editingIdx: -1, customTemplates: [], addTitle: '', addOrigin: '', addGoal: '' },
    exportTab: 'yaml',
    exportProjectId: '',
    reportDraftingProjectId: '',
    deleteConfirm: { id: '', title: '' },
    isDeletingProject: false,
    isStoppingAllProjects: false,
    appAnalysisTab: 'miniprogram',

    // ── 模态开关 ────────────────────────────────────────────────────────
    showConcludeModal: false,
    showCompleteModal: false,
    showHintModal: false,
    showReopenModal: false,
    showRenameModal: false,
    showNewProject: false,
    showDeleteModal: false,
    showSettings: false,
    showLegend: true,
    showDispatcherStatus: false,
    showYamlModal: false,
    showLocalPrefs: false,
    showApprovalDetail: false,
    showEmergencyModal: false,

    // ── 审批 ────────────────────────────────────────────────────────────
    approvalTab: 'pending',
    approvalDetail: null,
    approvalActing: false,
    approvalList: [],          // 模板 approvalList.length 直访 → 必须数组初值
    emergencyReleases: [],     // 急模式自动放行清单（P1-5：事后复核的落点）
    emergencyReleasesLoading: false,
    approvalLoading: false,
    approvalStats: {},         // loadApprovals 赋值；{} 防模板 ?? 前 undefined

    // ── 对话 / 应用分析 ─────────────────────────────────────────────────
    androidChat: { sessions: [], messages: [], streaming: false, sessionId: '' },
    chat: { sessions: [], messages: [], sessionId: '', role: 'assistant', context: '' },
    liveOutput: { active: false, intentId: '', worker: '', text: '' },
    miniProgram: { dispatching: false, harDispatching: false, harTargetProject: 'new' },
    android: { dispatching: false },
    secretsForm: {},
    secretsStatus: [],
    secretsPath: '',
    dispatcherRestarting: false,

    // ── MCP / Dispatcher ────────────────────────────────────────────────
    mcpServers: [],
    mcpEditor: { show: false, editing: null },
    dispatcherStatus: [],      // 数组：模板 x-for 遍历 dispatcher 状态条目
    dispatcherStatusLoading: false,

    // ── 漏洞 / Dashboard ────────────────────────────────────────────────
    // Dashboard/风险总览直接读 vulnStats.xxx —— 必须给零值对象而非 null
    vulnStats: { total: 0, confirmed: 0, pending: 0, critical: 0, high: 0, medium: 0, low: 0, info: 0 },
    vulnDonutHover: null,
    _searchHits: [],
  };

  // 模块挂载顺序 = 依赖方向：core（init/api/格式化）→ 数据模块 → 展示模块
  applyCoreModule(obj);
  applyProjectsModule(obj);
  applyGraphModule(obj);
  applyBoardModule(obj);
  applyTimelineModule(obj);
  applyReplayModule(obj);
  applyProjectDetailModule(obj);
  applyIntentsModule(obj);
  applyApprovalsModule(obj);
  applyAnalyzersModule(obj);
  applyChatModule(obj);

  return obj;
}
