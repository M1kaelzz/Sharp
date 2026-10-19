from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

DEFAULT_DB = Path.home() / ".local" / "share" / "sharp" / "sharp.db"

_db_path: Path | None = None

SCHEMA = """\
CREATE TABLE IF NOT EXISTS settings (
    intent_timeout INTEGER NOT NULL DEFAULT 15,
    reason_timeout INTEGER NOT NULL DEFAULT 15,
    report_instructions TEXT NOT NULL DEFAULT ''
);

INSERT OR IGNORE INTO settings (rowid, intent_timeout, reason_timeout, report_instructions) VALUES (1, 15, 15, '');

CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    reason_worker TEXT,
    reason_trigger TEXT,
    reason_started_at TEXT,
    reason_last_heartbeat_at TEXT,
    emergency_until TEXT,
    emergency_reason TEXT NOT NULL DEFAULT '',
    -- P2-4: pause/resume without killing running tasks. paused=1 means the
    -- dispatcher skips this project for new dispatches but does not cancel
    -- already-running tasks. Independent of status (active/stopped/completed).
    paused INTEGER NOT NULL DEFAULT 0,
    -- P2-3: token-budget proxy. task_budget=0 means unlimited. task_count
    -- is incremented each time a task (reason/explore/bootstrap) is dispatched.
    -- When task_count >= task_budget (>0), no new tasks are dispatched and a
    -- budget-exhausted hint is written once.
    task_budget INTEGER NOT NULL DEFAULT 0,
    task_count INTEGER NOT NULL DEFAULT 0,
    -- Asset-centric metadata (batch 11.4): what kind of target this project
    -- investigates and the canonical asset key it belongs to. asset_ref:
    --   web        → origin host (e.g. example.com)
    --   miniprogram→ WeChat AppID (wx...)
    --   android    → application package (com.example.app)
    -- Projects sharing an asset_ref form a "target space" (asset center view).
    target_kind TEXT NOT NULL DEFAULT 'web',
    asset_ref TEXT NOT NULL DEFAULT '',
    -- Optional hard deadline (batch A): after this instant the dispatcher stops
    -- handing out NEW work and the reason prompt switches to wrap-up mode.
    deadline_at TEXT,
    -- Task mode: 'pentest' (default) keeps the product purely about security
    -- findings; 'scored' additionally exposes flag/score artifacts for
    -- CTF / benchmark runs. Scoring is a task-scoped option, NOT a core
    -- concept — penetration projects must not show scoreboard semantics.
    task_mode TEXT NOT NULL DEFAULT 'pentest',
    -- 产品/指纹标注（P0-C）：知识复用的第二维度。同一个产品换个域名/IP 部署时，
    -- 只按目标键匹配的知识一条都过不来。空 = 未标注（不参与跨产品匹配）。
    -- 注意：_migrate() 在 CREATE 之前执行，所以**老库靠 ALTER 补、新库靠这里建**，
    -- 两处都必须有，否则全新库会缺列（实测被测试抓到过）。
    product TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS facts (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    description TEXT NOT NULL,
    -- Human-correction flag (batch 11.1): 1 = trusted (AI/human finding stands),
    -- 0 = human marked it untrusted and it should not feed conclusions/knowledge
    -- without review. Every change is audited in fact_edits.
    trusted INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (id, project_id)
);

CREATE TABLE IF NOT EXISTS intents (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    to_fact_id TEXT,
    description TEXT NOT NULL,
    creator TEXT NOT NULL,
    worker TEXT,
    last_heartbeat_at TEXT,
    created_at TEXT NOT NULL,
    concluded_at TEXT,
    idempotency_key TEXT,
    -- Risk classification & human-approval gate. Defaults keep legacy rows /
    -- non-classified intents fully functional: 'low' risk, no approval needed.
    risk_level TEXT NOT NULL DEFAULT 'low',
    risk_reason TEXT NOT NULL DEFAULT '',
    approval_status TEXT NOT NULL DEFAULT 'none',  -- none/pending/approved/rejected/expired
    approval_note TEXT NOT NULL DEFAULT '',
    approval_decided_at TEXT,
    -- Reason-driven intent lifecycle (batch A): the planning pass may raise or
    -- lower a step's priority, or abandon a low-value step outright instead of
    -- letting it occupy a worker slot until timeout.
    priority INTEGER NOT NULL DEFAULT 0,
    abandoned_at TEXT,
    abandon_reason TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (id, project_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_intents_idempotency
    ON intents(project_id, idempotency_key) WHERE idempotency_key IS NOT NULL;

-- Lightweight approval audit trail (submitted/approved/rejected/expired/emergency).
CREATE TABLE IF NOT EXISTS approval_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    intent_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    action TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS intent_sources (
    intent_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    fact_id TEXT NOT NULL,
    PRIMARY KEY (intent_id, project_id, fact_id),
    FOREIGN KEY (intent_id, project_id) REFERENCES intents(id, project_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS hints (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    content TEXT NOT NULL,
    creator TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (id, project_id)
);

CREATE TABLE IF NOT EXISTS scoped_counters (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    value INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (project_id, kind)
);

CREATE TABLE IF NOT EXISTS dispatcher_status (
    dispatcher_id TEXT PRIMARY KEY,
    updated_at TEXT NOT NULL,
    data TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS auth (
    id INTEGER PRIMARY KEY DEFAULT 1,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS android_chat_sessions (
    id TEXT PRIMARY KEY,
    apk_path TEXT NOT NULL,
    apk_filename TEXT NOT NULL DEFAULT '',
    apk_context TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_active_at TEXT NOT NULL,
    -- Interactive dynamic-debugging fields. 'chat' = stateless LLM Q&A (default,
    -- unchanged behavior); 'dynamic' = bound to a live worker container running
    -- claude-code with adb/frida, driven turn-by-turn via claude session resume.
    mode TEXT NOT NULL DEFAULT 'chat',
    container_name TEXT NOT NULL DEFAULT '',
    claude_session_id TEXT NOT NULL DEFAULT '',
    device_info TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS android_chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES android_chat_sessions(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- Generic AI chat sessions (independent of Android). Any role; no APK required.
-- project_id optionally binds the session to a project so the assistant can
-- answer questions about that project's facts/intents/hints.
CREATE TABLE IF NOT EXISTS chat_sessions (
    id TEXT PRIMARY KEY,
    role TEXT NOT NULL DEFAULT 'assistant',
    context TEXT NOT NULL DEFAULT '',
    project_id TEXT,
    created_at TEXT NOT NULL,
    last_active_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- Structured vulnerability records, created by workers during explore tasks
-- and verified by the lightweight verifier. Each vuln is linked to the fact
-- that documented it and the intent that produced the exploration.
CREATE TABLE IF NOT EXISTS vulnerabilities (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    fact_id TEXT NOT NULL,
    intent_id TEXT,
    title TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT 'info',      -- critical/high/medium/low/info
    status TEXT NOT NULL DEFAULT 'pending',     -- pending/confirmed/dismissed
    url TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    evidence TEXT NOT NULL DEFAULT '',           -- curl command / HTTP request-response pair
    reproduction TEXT NOT NULL DEFAULT '',       -- step-by-step reproduction
    impact TEXT NOT NULL DEFAULT '',
    recommendation TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    verified_at TEXT,
    -- Finding kind (batch B): 'vuln' = security finding (default, unchanged),
    -- 'flag' = a scored artifact of the search (CTF / benchmark answers),
    -- 'finding' = any other structured artifact worth recording.
    kind TEXT NOT NULL DEFAULT 'vuln',
    -- Score awarded for this finding (used by flag kind in scored tasks).
    score INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (id, project_id),
    FOREIGN KEY (fact_id, project_id) REFERENCES facts(id, project_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_vulns_project ON vulnerabilities(project_id);
CREATE INDEX IF NOT EXISTS idx_vulns_severity ON vulnerabilities(project_id, severity);

-- Cross-project knowledge base (P3-1).  Global table (not bound to a project)
-- keyed by a **canonical host key** (see sharp/server/hostkey.py):
--   domain:example.com / ip:10.0.100.58 / host:internal-api / unattributed
-- Workers extract credentials / endpoints / fingerprints / dead-ends from fact
-- descriptions during explore tasks; matching entries are injected as hints.
--
-- `product` 是第二个匹配维度：把同一个**产品**（如 Peplink MANGA）上的经验复用到
-- 另一个不同域名/IP 的同类目标上。只靠 root_domain 时这不可能发生，而"这条路试过
-- 不通"恰恰是跨产品最有价值的情报。
CREATE TABLE IF NOT EXISTS knowledge_base (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    root_domain TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'credential',  -- credential/endpoint/fingerprint/dead_end/other
    title TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL,
    source_project_id TEXT,   -- traceability only, no FK
    product TEXT NOT NULL DEFAULT '',  -- 产品/指纹维度（空 = 未标注，不参与跨产品匹配）
    confidence TEXT NOT NULL DEFAULT 'high',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (root_domain, kind, title)
);

CREATE INDEX IF NOT EXISTS idx_kb_root ON knowledge_base(root_domain, kind);
CREATE INDEX IF NOT EXISTS idx_kb_product ON knowledge_base(product, kind);
CREATE INDEX IF NOT EXISTS idx_kb_updated ON knowledge_base(updated_at);

-- Human fact corrections (batch 11.1): append-only audit trail. Every change
-- to a fact (description rewrite, untrusted marking, restore) records the
-- before/after state so findings can be traced and reverted.
CREATE TABLE IF NOT EXISTS fact_edits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    fact_id TEXT NOT NULL,
    prev_description TEXT NOT NULL,
    prev_trusted INTEGER NOT NULL,
    new_description TEXT NOT NULL,
    new_trusted INTEGER NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    annotator TEXT NOT NULL DEFAULT 'human',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fact_edits ON fact_edits(project_id, fact_id, id);

-- Structured asset endpoint ledger (batch A1): interface inventory per asset.
-- Populated automatically when an intent concludes with URLs in its fact;
-- shared across every project of the same asset so new campaigns can see what
-- was already discovered / tested instead of rediscovering it.
CREATE TABLE IF NOT EXISTS asset_endpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_ref TEXT NOT NULL,               -- canonical host (lowercase URL host)
    method TEXT NOT NULL DEFAULT '',
    path TEXT NOT NULL,
    source_project_id TEXT NOT NULL,       -- where first seen; no FK so the
    source_fact_id TEXT,                   -- ledger survives project deletion
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'discovered',  -- discovered/verified/dismissed
    last_assessed_at TEXT,
    note TEXT NOT NULL DEFAULT '',
    UNIQUE (asset_ref, method, path)
);
CREATE INDEX IF NOT EXISTS idx_ae_asset ON asset_endpoints(asset_ref, status);

-- Sub goals (batch C): phase-level objectives on the graph. The planner
-- (reason) may propose them, mark them done, or abandon them; humans can too.
-- They make staged progress visible instead of a single binary goal.
-- Environment baseline (P0-2): 已确认的环境事实，跨会话复用，避免重复探测。
-- 与 facts 的区别：facts 是"探索结论"（会被引用、审计），baseline 是"作业前提"
-- （网络是否连通、平台凭据是否有效、目标端点是否可达），新会话先读它再干活。
CREATE TABLE IF NOT EXISTS env_baseline (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    key TEXT NOT NULL,
    value TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'worker',   -- worker | human
    updated_at TEXT NOT NULL,
    PRIMARY KEY (project_id, key)
);

-- Hypotheses (P0-3): 未验证假设。
-- 真实渗透推进的本来逻辑是假设驱动：观察现象 → 形成可证伪的猜想 → 设计动作验证 → 新猜想。
-- facts 存的是**已确认结论**，故此前猜想只能塞进意图描述的文字里：既无法结算，
-- 被否定的线索也不留痕迹。本表让猜想成为一等对象。
CREATE TABLE IF NOT EXISTS hypotheses (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    statement TEXT NOT NULL,                  -- 可证伪的命题（"若 X 则 Y"），不是"这里有漏洞"
    status TEXT NOT NULL DEFAULT 'open',      -- open / testing / confirmed / refuted
    premise_fact_ids TEXT NOT NULL DEFAULT '',-- 依据哪些证据提出（逗号分隔）
    result_fact_id TEXT,                      -- 结算时引用的证据
    created_by TEXT NOT NULL DEFAULT 'reason',
    note TEXT NOT NULL DEFAULT '',            -- 结算说明（refuted 时写清试过什么）
    created_at TEXT NOT NULL,
    concluded_at TEXT,
    PRIMARY KEY (id, project_id)
);

CREATE INDEX IF NOT EXISTS idx_hypotheses_project ON hypotheses(project_id, status);

CREATE TABLE IF NOT EXISTS sub_goals (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',   -- pending/active/done/abandoned
    note TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL DEFAULT 'reason',
    created_at TEXT NOT NULL,
    concluded_at TEXT,
    PRIMARY KEY (id, project_id)
);

CREATE INDEX IF NOT EXISTS idx_sub_goals_project ON sub_goals(project_id, status);

-- Startup DB-maintenance state (batch DB-health): when the last VACUUM ran.
CREATE TABLE IF NOT EXISTS maintenance_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def configure(path: Path) -> None:
    global _db_path
    if _db_path is not None:
        return
    _db_path = path
    _db_path.parent.mkdir(parents=True, exist_ok=True)
    with get_conn() as conn:
        _migrate(conn)
        conn.executescript(SCHEMA)


def _migrate(conn: sqlite3.Connection) -> None:
    """Idempotent, additive migrations for pre-existing databases. Runs before
    the CREATE ... IF NOT EXISTS schema (which does not add columns to existing
    tables, and whose new index references a column an old DB lacks)."""
    if _table_exists(conn, "intents") and not _column_exists(conn, "intents", "idempotency_key"):
        conn.execute("ALTER TABLE intents ADD COLUMN idempotency_key TEXT")
    if _table_exists(conn, "settings") and not _column_exists(conn, "settings", "report_instructions"):
        conn.execute("ALTER TABLE settings ADD COLUMN report_instructions TEXT NOT NULL DEFAULT ''")
    # Risk-classification & approval columns on intents (incremental, additive).
    if _table_exists(conn, "intents"):
        for col, ddl in (
            ("risk_level", "ALTER TABLE intents ADD COLUMN risk_level TEXT NOT NULL DEFAULT 'low'"),
            ("risk_reason", "ALTER TABLE intents ADD COLUMN risk_reason TEXT NOT NULL DEFAULT ''"),
            ("approval_status", "ALTER TABLE intents ADD COLUMN approval_status TEXT NOT NULL DEFAULT 'none'"),
            ("approval_note", "ALTER TABLE intents ADD COLUMN approval_note TEXT NOT NULL DEFAULT ''"),
            ("approval_decided_at", "ALTER TABLE intents ADD COLUMN approval_decided_at TEXT"),
        ):
            if not _column_exists(conn, "intents", col):
                conn.execute(ddl)
    # Asset-centre columns on projects (batch 11.4).
    if _table_exists(conn, "projects"):
        for col, ddl in (
            ("target_kind", "ALTER TABLE projects ADD COLUMN target_kind TEXT NOT NULL DEFAULT 'web'"),
            ("asset_ref", "ALTER TABLE projects ADD COLUMN asset_ref TEXT NOT NULL DEFAULT ''"),
            ("deadline_at", "ALTER TABLE projects ADD COLUMN deadline_at TEXT"),
        ):
            if not _column_exists(conn, "projects", col):
                conn.execute(ddl)
    # Hypotheses (P0-3): 行动可挂到假设上（"这一步是为了验证哪个猜想"）。
    if _table_exists(conn, "intents"):
        if not _column_exists(conn, "intents", "hypothesis_id"):
            conn.execute("ALTER TABLE intents ADD COLUMN hypothesis_id TEXT")
    # Task mode on projects (P1-收敛): scoring is opt-in per project.
    if _table_exists(conn, "projects"):
        if not _column_exists(conn, "projects", "task_mode"):
            conn.execute("ALTER TABLE projects ADD COLUMN task_mode TEXT NOT NULL DEFAULT 'pentest'")
    # 知识复用的产品维度（P0-C）：把同一产品上的经验复用到另一个不同域名/IP 的目标。
    if _table_exists(conn, "knowledge_base"):
        if not _column_exists(conn, "knowledge_base", "product"):
            conn.execute("ALTER TABLE knowledge_base ADD COLUMN product TEXT NOT NULL DEFAULT ''")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_kb_product ON knowledge_base(product, kind)")
    if _table_exists(conn, "projects"):
        if not _column_exists(conn, "projects", "product"):
            conn.execute("ALTER TABLE projects ADD COLUMN product TEXT NOT NULL DEFAULT ''")
    # Finding kind/score on vulnerabilities (batch B).
    if _table_exists(conn, "vulnerabilities"):
        for col, ddl in (
            ("kind", "ALTER TABLE vulnerabilities ADD COLUMN kind TEXT NOT NULL DEFAULT 'vuln'"),
            ("score", "ALTER TABLE vulnerabilities ADD COLUMN score INTEGER NOT NULL DEFAULT 0"),
        ):
            if not _column_exists(conn, "vulnerabilities", col):
                conn.execute(ddl)
    # Intent lifecycle columns (batch A): priority / abandonment.
    if _table_exists(conn, "intents"):
        for col, ddl in (
            ("priority", "ALTER TABLE intents ADD COLUMN priority INTEGER NOT NULL DEFAULT 0"),
            ("abandoned_at", "ALTER TABLE intents ADD COLUMN abandoned_at TEXT"),
            ("abandon_reason", "ALTER TABLE intents ADD COLUMN abandon_reason TEXT NOT NULL DEFAULT ''"),
        ):
            if not _column_exists(conn, "intents", col):
                conn.execute(ddl)
    # Human-correction flag on facts (batch 11.1); the fact_edits table itself
    # is created by the IF-NOT-EXISTS schema on every configure().
    if _table_exists(conn, "facts") and not _column_exists(conn, "facts", "trusted"):
        conn.execute("ALTER TABLE facts ADD COLUMN trusted INTEGER NOT NULL DEFAULT 1")
    # Emergency-mode fields on projects (single-user escape hatch, JWT-only).
    if _table_exists(conn, "projects"):
        for col, ddl in (
            ("emergency_until", "ALTER TABLE projects ADD COLUMN emergency_until TEXT"),
            ("emergency_reason", "ALTER TABLE projects ADD COLUMN emergency_reason TEXT NOT NULL DEFAULT ''"),
            ("paused", "ALTER TABLE projects ADD COLUMN paused INTEGER NOT NULL DEFAULT 0"),
            ("task_budget", "ALTER TABLE projects ADD COLUMN task_budget INTEGER NOT NULL DEFAULT 0"),
            ("task_count", "ALTER TABLE projects ADD COLUMN task_count INTEGER NOT NULL DEFAULT 0"),
        ):
            if not _column_exists(conn, "projects", col):
                conn.execute(ddl)
    # Interactive dynamic-debugging columns on android_chat_sessions.
    if _table_exists(conn, "android_chat_sessions"):
        for col, ddl in (
            ("mode", "ALTER TABLE android_chat_sessions ADD COLUMN mode TEXT NOT NULL DEFAULT 'chat'"),
            ("container_name", "ALTER TABLE android_chat_sessions ADD COLUMN container_name TEXT NOT NULL DEFAULT ''"),
            ("claude_session_id", "ALTER TABLE android_chat_sessions ADD COLUMN claude_session_id TEXT NOT NULL DEFAULT ''"),
            ("device_info", "ALTER TABLE android_chat_sessions ADD COLUMN device_info TEXT NOT NULL DEFAULT ''"),
        ):
            if not _column_exists(conn, "android_chat_sessions", col):
                conn.execute(ddl)


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(row["name"] == column for row in conn.execute(f"PRAGMA table_info({table})"))


@contextmanager
def get_conn() -> Generator[sqlite3.Connection, None, None]:
    assert _db_path is not None
    conn = sqlite3.connect(str(_db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    # WAL allows concurrent readers with a single writer, but two concurrent
    # writers (e.g. several workers hitting heartbeat/conclude/complete while a
    # browser poll issues UPDATEs) would otherwise get an immediate SQLITE_BUSY
    # -> 500. Wait up to 5s for the lock instead; real write txns are ms-scale.
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def current_path() -> Path:
    assert _db_path is not None
    return _db_path
