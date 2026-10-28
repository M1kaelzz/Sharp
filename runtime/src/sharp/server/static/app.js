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
    // 项目详情（loadProject 整体替换）。默认形状镜像 GET /projects/{id} 的 ProjectDetail
    // 响应 {project, facts, intents, hints}——graph 视图直接读 project.project.* /
    // project.facts.length / project.hints.length 等嵌套键，缺形状在挂载求值时整块崩
    // （前端状态守护测试锁定）
    project: {
      project: {
        id: '', title: '', status: '', engineered_report_status: '',
        task_mode: '', reason: '', paused: false,
        task_budget: null, deadline_at: null, created_at: null,
      },
      facts: [], intents: [], hints: [],
    },
    vulns: [],              // 当前/全部漏洞列表（loadVulns 填充）
    vulnsLoading: false,
    showVulnDetail: false,
    // 漏洞详情弹窗（openVulnDetail 整体替换；index.html 直接读嵌套键，缺形状在打开
    // 弹窗/首帧求值时崩）
    vulnDetail: {
      id: '', project_id: '', fact_id: '', intent_id: '', title: '',
      severity: '', status: '', kind: '', score: 0, url: '',
      description: '', evidence: '', reproduction: '', impact: '',
      recommendation: '', created_at: '', verified_at: '',
      quality: null,
    },
    projects: [],
    projectSearch: '',
    graphSearch: '',
    // graph 视图过滤器（仅高危/隐藏已结论/血缘链）。此前漏声明 → 首帧 :class 求值
    // ReferenceError（graphFilterVuln 等 is not defined），由状态守护的 :class 盲区放过
    graphFilterVuln: false,
    graphFilterConcluded: false,
    graphFilterBloodline: false,
    vulnFilter: { severity: 'all', status: 'all', kind: 'all' },
    scoreboard: null,          // 项目记分板（批次 B：旗帜数/得分）
    subGoals: [],              // 阶段目标（批次 C）
    hypotheses: [],            // 未验证假设（P0-3）
    hypothesisDraft: '',       // 新假设输入（可证伪的命题）
    hypothesisShowClosed: false,  // 是否显示已结算的假设
    subGoalDraft: '',          // 新阶段目标输入
    coverage: null,            // 覆盖报告（P1-A）：打到了什么 / 已验证不通 / 盲区
    coverageLoading: false,
    // 调度轨迹（P1-4 process exchange）：项目级最近调度轮次，openProject 并行拉取
    traces: [],
    tracesLoading: false,
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
    // Toast（showToast 运行时整体赋值；提前声明避免首帧 x-show/x-text 求值失败 →
    // index.html 内联模板 toast.message 报 ReferenceError，前端状态守护锁定）
    toast: { show: false, message: '', type: 'info' },
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
    concludeForm: { description: '', intentId: '' },
    reopenForm: { projectId: '', projectTitle: '', description: '' },
    hintForm: { content: '' },
    renameForm: { projectId: '', originalTitle: '', title: '' },
    emergencyForm: {},
    approvalActionNote: '',
    settingsForm: { intent_timeout: '', reason_timeout: '', report_instructions: '' },
    // localPrefs 细结构：loadLocalPrefs 会读 .actor_name/.layout_mode/.layout_dir，
    // 空对象会让 .actor_name.trim() 抛错中断 init → 必须带默认初值
    localPrefs: { actor_name: '', layout_mode: 'dagre_tb', layout_dir: '' },
    pwForm: { old: '', new1: '', new2: '' },
    // 修改密码的两个状态（设置页 x-text 直接读——漏声明会让按钮文字变空白，已由
    // 前端状态守护测试锁定，勿删）
    pwLoading: false,
    pwError: '',
    dispatcherMode: 'anthropic',  // 设置页下拉默认（模板 x-model + x-if 面板依赖；'' 会导致面板全空）

    // ── 回放 / 图形（x-model 对象）──────────────────────────────────
    replay: { active: false, playing: false, stepMs: '1100' },
    npPage: {
      showAddForm: false, editingIdx: -1, customTemplates: [],
      addTitle: '', addOrigin: '', addGoal: '',
      // 编辑模板表单（startEditTemplate 赋值 {name,origin,goal}；index.html 模板
      // 直接读 .name/.origin/.goal 及 .name.trim()，缺形状首帧崩溃）
      editForm: { name: '', origin: '', goal: '' },
    },
    exportTab: 'yaml',
    exportProjectId: '',
    // YAML 预览弹窗（openYamlPreview 赋值；index.html 内联模板直接 x-text/x-html 读取，
    // 完全未声明会在首帧报 ReferenceError）
    yamlPreviewTitle: '',
    yamlPreviewHtml: '',
    yamlPreviewText: '',
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
    // 对话 / 应用分析：完整默认形状（各状态在对应页面/重置函数里整体替换；缺键会让
    // 常驻 DOM 的模板首帧求值失败——x-show 视图也求值，前端状态守护锁定）
    androidChat: {
      sessionId: '', apkFilename: '', messages: [], input: '',
      streaming: false, streamBuffer: '', abortController: null,
      linkedProjectId: '', _msgIdSeq: 0, sessions: [], showHistory: false,
      dynamicMode: false, dynamicStarting: false, dynamicContainer: '',
      dynamicDevice: '', dynamicReady: false,
    },
    chat: { sessions: [], messages: [], sessionId: '', role: 'assistant', context: '', input: '', projectId: '', streaming: false, streamBuffer: '' },
    liveOutput: { active: false, intentId: '', worker: '', text: '' },
    // harTargetProject 默认 'auto'（与 resetMiniProgramAnalysis / 下拉首选值一致；
    // 历史上曾写 'new'，会让首次打开分析页静默落到"新建独立项目"）
    miniProgram: {
      file: null, filename: '', loading: false, scanning: false,
      result: null, tab: 'findings', query: '',
      packages: [], selectedPath: '', scanSummary: '', source: 'upload',
      unpacking: false, unpackOutputDir: '', unpackResult: null,
      dispatching: false, dispatchResult: null,
      harFile: null, harFilename: '', harDispatching: false,
      harDispatchResult: null, harTargetProject: 'auto',
    },
    android: {
      path: '', loading: false, uiPane: 'result', uploading: false,
      result: null, tab: 'domains', query: '',
      dispatching: false, dispatchResult: null,
      browser: {
        loaded: false, loading: false, rel: '', crumbs: [], parent_rel: '',
        at_home: true, dirs: [], files: [], truncated: false,
      },
    },
    // 密钥表单：字段必须给空串初值——saveServerSecrets() 直接 .trim()，
    // 未填写字段为 undefined 时会抛 TypeError（发生在 try 块外→保存静默失败）
    secretsForm: {
      SHARP_ANTHROPIC_AUTH_TOKEN: '', SHARP_MODEL: '', SHARP_BASE_URL: '',
      OPENAI_API_KEY: '', OPENAI_BASE_URL: '', OPENAI_MODEL: '',
      PI_API_KEY: '', PI_BASE_URL: '', PI_MODEL: '',
    },
    secretsStatus: [],
    secretsPath: '',
    dispatcherRestarting: false,

    // ── MCP / Dispatcher ────────────────────────────────────────────────
    mcpSaving: false,          // MCP 保存中状态（保存按钮 x-text 依赖，漏声明=按钮空白）

    mcpServers: [],
    // form 默认键集 = app.chat.js 的 _emptyMcpForm()（打开编辑器时整体替换；
    // 提前声明避免常驻模板首帧读 mcpEditor.form.name 崩溃）
    mcpEditor: {
      show: false, editing: null,
      form: {
        name: '', transport: 'http', url: '', headers: [], command: '',
        args: [], env: [], enabled: true, max_tools: 100,
      },
    },
    dispatcherStatus: [],      // 数组：模板 x-for 遍历 dispatcher 状态条目
    dispatcherStatusLoading: false,

    // ── 漏洞 / Dashboard ────────────────────────────────────────────────
    // Dashboard/风险总览直接读 vulnStats.xxx —— 必须给零值对象而非 null
    vulnStats: { total: 0, confirmed: 0, pending: 0, critical: 0, high: 0, medium: 0, low: 0, info: 0 },
    // 成本带（P1-3）：加载前 null（x-show 里判空前先判空值，未开计量时 total_calls=0 整体隐藏）
    usageSummary: null,
    graphActionMenu: false,   // 图视图操作菜单开合（graph.html；历史上漏声明，由前端状态守护锁定）
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
