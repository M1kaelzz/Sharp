from __future__ import annotations

from typing import Any
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class Settings(BaseModel):
    intent_timeout: int = Field(ge=5)
    reason_timeout: int = Field(ge=5)
    report_instructions: str = ""


class Fact(BaseModel):
    id: str
    description: str
    # Human-correction flag (batch 11.1): True = trusted, False = a human marked
    # the fact untrusted (do not feed it into conclusions / knowledge without
    # review). Default keeps legacy payloads valid.
    trusted: bool = True


class FactCorrectRequest(BaseModel):
    """Human correction of a fact (batch 11.1).

    Semantics (server-side, idempotent):
      - ``description`` non-empty & different → rewrite the fact text, marking
        it trusted again (a human just reviewed it).
      - ``untrusted=True`` → mark the fact untrusted (do not wipe the text).
      - ``untrusted=False`` with no new description → clear a previous
        untrusted marking (restore).
    Every actual change is appended to the fact_edits audit trail.
    """

    description: str | None = None
    untrusted: bool | None = None
    note: str = ""

    @field_validator("description", "note")
    @classmethod
    def _strip(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        return text or None


class FactEdit(BaseModel):
    id: int
    fact_id: str
    prev_description: str
    prev_trusted: bool
    new_description: str
    new_trusted: bool
    note: str
    annotator: str
    created_at: str


class AcceptanceItem(BaseModel):
    """One open item shown by the pre-completion acceptance check (batch 11.2)."""

    id: str
    label: str


class AssetGroup(BaseModel):
    """One asset key (host / wx AppID / package) with its projects (batch 11.4).

    ``asset_ref == ""`` is the uncategorized bucket (web origins with no
    parseable host). ``projects`` are the full ProjectSummary models so the UI
    can render the underlying list without a second round trip.
    """

    asset_ref: str
    target_kind: str
    project_count: int
    vuln_high_total: int
    pending_approval_total: int
    latest_created_at: str
    # Structured interface ledger (batch A1): rows seen across every project of
    # this asset, and how many are still unassessed.
    endpoint_total: int = 0
    endpoint_todo: int = 0
    projects: list[ProjectSummary] = []


class AcceptanceCheck(BaseModel):
    """Server-side pre-completion inventory (batch 11.2).

    Computed when the human is about to mark a project completed, so the
    decision is informed: open intents, pending human approvals, facts a human
    marked untrusted, and high/critical vulnerabilities not yet confirmed.
    Purely advisory — completion is the human's call.
    """

    project_id: str
    status: str
    open_intents: list[AcceptanceItem] = []
    pending_approvals: list[AcceptanceItem] = []
    untrusted_facts: list[AcceptanceItem] = []
    unconfirmed_high_vulns: list[AcceptanceItem] = []
    # Batch C: phase objectives still open (pending/active) at completion time.
    open_sub_goals: list[AcceptanceItem] = []
    confirmed_high_vulns: int = 0
    has_open_work: bool = False


class Intent(BaseModel):
    id: str
    from_: list[str] = Field(alias="from")
    to: str | None = None
    description: str
    creator: str
    worker: str | None = None
    last_heartbeat_at: str | None = None
    created_at: str
    concluded_at: str | None = None
    # Risk & approval gate. Defaults keep legacy / non-classified intents valid.
    risk_level: Literal["low", "medium", "high", "critical"] = "low"
    risk_reason: str = ""
    approval_status: Literal["none", "pending", "approved", "rejected", "expired"] = "none"
    approval_note: str = ""
    approval_decided_at: str | None = None
    # Reason-driven lifecycle (batch A): planner-set priority (higher first) and
    # abandonment (a step the planner decided is no longer worth a worker slot).
    priority: int = 0
    abandoned_at: str | None = None
    abandon_reason: str = ""

    model_config = {"populate_by_name": True}


class Hint(BaseModel):
    id: str
    content: str
    creator: str
    created_at: str


class ProjectReason(BaseModel):
    worker: str
    trigger: str
    started_at: str
    last_heartbeat_at: str


class ProjectMeta(BaseModel):
    id: str
    title: str
    status: Literal["active", "stopped", "completed"]
    created_at: str
    reason: ProjectReason | None = None
    # Asset-centre metadata (batch 11.4). target_kind ∈ web|miniprogram|android;
    # asset_ref is the canonical asset key (host / wx AppID / package).
    target_kind: str = "web"
    asset_ref: str = ""
    # Optional hard deadline (batch A): ISO-8601 UTC. After it the dispatcher
    # stops handing out new work and reason switches to wrap-up mode.
    deadline_at: str | None = None
    # Task mode (opt-in scoring): 'pentest' (default) = the product is about
    # security findings only; 'scored' = CTF / benchmark run, additionally
    # exposes flag/score artifacts (scoreboard, flag badges, prompt guidance).
    task_mode: Literal["pentest", "scored"] = "pentest"
    # 产品/指纹标注（P0-C）：知识复用的第二维度。同名产品换个域名/IP 部署时，
    # 只靠目标键的知识一条都过不来；这个字段让"同类产品上的经验"跨目标复用。
    # 空 = 未标注（未标注不参与跨产品匹配，否则所有未标注行会互相匹配）。
    product: str = ""
    # P2-4: independent of status. When True, dispatcher skips new dispatches
    # for this project but does not cancel already-running tasks.
    paused: bool = False
    # P2-3: task-count budget proxy for token budget. task_budget=0 → unlimited.
    task_budget: int = 0
    task_count: int = 0


class ProjectSummary(ProjectMeta):
    fact_count: int
    intent_count: int
    working_intent_count: int
    unclaimed_intent_count: int
    hint_count: int
    # High/critical intents awaiting human approval in this project.
    pending_approval_count: int = 0
    # Computed from existing intent/fact data; no extra DB column needed.
    # "generating" = open report intent exists; "done" = concluded report fact exists; None = not started.
    engineered_report_status: Literal["generating", "done"] | None = None
    # Vulnerability counts by severity (excluding dismissed).
    vuln_count: int = 0
    vuln_high_count: int = 0


class ProjectDetail(BaseModel):
    project: ProjectMeta
    facts: list[Fact]
    intents: list[Intent]
    hints: list[Hint]


class CreateHintInline(BaseModel):
    content: str
    creator: str

    @field_validator("content", "creator")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class CreateProjectRequest(BaseModel):
    title: str
    origin: str
    goal: str
    hints: list[CreateHintInline] | None = None
    # Optional asset metadata (batch 11.4): when omitted, kind defaults to web
    # and asset_ref is derived from origin (host extraction server-side).
    target_kind: Literal["web", "miniprogram", "android"] | None = None
    asset_ref: str | None = None
    # Opt-in scoring mode (see ProjectMeta.task_mode). Omitted → 'pentest'.
    task_mode: Literal["pentest", "scored"] | None = None

    @field_validator("asset_ref")
    @classmethod
    def _clean_asset_ref(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        return text or None

    @field_validator("title", "origin", "goal")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class CreateHintRequest(BaseModel):
    content: str
    creator: str

    @field_validator("content", "creator")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class CreateIntentRequest(BaseModel):
    from_: list[str] = Field(alias="from", min_length=1)
    description: str
    creator: str
    worker: str | None = None
    # Optional client-supplied key that makes intent creation idempotent under
    # retries. When present, (project_id, idempotency_key) is unique server-side:
    # a retry with the same key returns the existing intent instead of creating a
    # duplicate. Absent -> legacy non-idempotent behavior (backward compatible).
    idempotency_key: str | None = None
    # Model-provided risk annotation (advisory only). Server-side keyword check
    # in risk.py is the final authority; model annotation is just a hint.
    risk_level: Literal["low", "medium", "high", "critical"] | None = None
    risk_reason: str = ""

    model_config = {"populate_by_name": True}

    @field_validator("description", "creator", "worker", "idempotency_key")
    @classmethod
    def validate_non_empty_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("from_")
    @classmethod
    def validate_fact_ids(cls, value: list[str]) -> list[str]:
        cleaned = []
        for item in value:
            text = item.strip()
            if not text:
                raise ValueError("fact ids must not be empty")
            cleaned.append(text)
        return cleaned


class HeartbeatRequest(BaseModel):
    worker: str

    @field_validator("worker")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class ReasonClaimRequest(BaseModel):
    worker: str
    trigger: str

    @field_validator("worker", "trigger")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class ConcludeRequest(BaseModel):
    worker: str
    description: str

    @field_validator("worker", "description")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class CompleteRequest(BaseModel):
    from_: list[str] = Field(alias="from", min_length=1)
    description: str
    worker: str

    model_config = {"populate_by_name": True}

    @field_validator("description", "worker")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("description")
    @classmethod
    def validate_meaningful_description(cls, value: str) -> str:
        """完成说明必须能说明"为什么可以收工"。

        知识库里见到过 `"1"` 这类完成说明——它不携带任何信息，事后无法复核这个项目
        凭什么算完成。这里只做最低限度的把关（长度 + 不能是纯数字/纯符号），
        不强制格式，避免把"快速收工"变成负担。
        """
        text = value.strip()
        if len(text) < 4:
            raise ValueError("完成说明过短：请写清为什么可以收工（至少 4 个字符）")
        if not any(ch.isalpha() or '\u4e00' <= ch <= '\u9fff' for ch in text):
            raise ValueError("完成说明不能只是数字或符号：请写一句人话说明完成依据")
        return text

    @field_validator("from_")
    @classmethod
    def validate_fact_ids(cls, value: list[str]) -> list[str]:
        cleaned = []
        for item in value:
            text = item.strip()
            if not text:
                raise ValueError("fact ids must not be empty")
            cleaned.append(text)
        return cleaned


class ConcludeResponse(BaseModel):
    fact: Fact
    intent: Intent


class UpdateProjectStatusRequest(BaseModel):
    status: Literal["active", "stopped"]


class UpdateProjectPausedRequest(BaseModel):
    paused: bool


class UpdateProjectBudgetRequest(BaseModel):
    task_budget: int = Field(ge=0)


class UpdateProjectTitleRequest(BaseModel):
    title: str

    @field_validator("title")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class ReopenRequest(BaseModel):
    description: str
    creator: str

    @field_validator("description", "creator")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class ReopenResponse(BaseModel):
    project: ProjectMeta
    fact: Fact
    intent: Intent


class ReportExportResponse(BaseModel):
    project_id: str
    report_dir: str
    report_path: str
    finding_count: int
    packet_count: int
    high_risk: bool


class DispatcherStatusUpdate(BaseModel):
    dispatcher_id: str
    data: dict[str, Any]

    @field_validator("dispatcher_id")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class DispatcherStatusEntry(BaseModel):
    dispatcher_id: str
    updated_at: str
    data: dict[str, Any]


class SecretStatus(BaseModel):
    key: str
    configured: bool
    source: str
    masked: str


class SecretsResponse(BaseModel):
    path: str
    values: list[SecretStatus]


class UpdateSecretsRequest(BaseModel):
    values: dict[str, str | None]


# ---------------------------------------------------------------------------
# MCP server settings (settings page, 2026-08-30)
# ---------------------------------------------------------------------------

class MCPServerSetting(BaseModel):
    name: str = Field(pattern=r"^[A-Za-z0-9_.\-]{1,64}$")
    transport: Literal["http", "stdio"] = "http"
    url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True
    max_tools: int = Field(default=100, ge=1)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("name must not be empty")
        return text


class MCPSettingsRequest(BaseModel):
    servers: list[MCPServerSetting]


# ---------------------------------------------------------------------------
# Vulnerability models (P1, 2026-08-29)
# ---------------------------------------------------------------------------

class Hypothesis(BaseModel):
    """未验证假设（P0-3）：可证伪的命题，验证后结算为 confirmed / refuted。"""

    id: str
    project_id: str
    statement: str
    status: Literal["open", "testing", "confirmed", "refuted"] = "open"
    premise_fact_ids: list[str] = []
    result_fact_id: str | None = None
    created_by: str = "reason"
    note: str = ""
    created_at: str
    concluded_at: str | None = None


class CreateHypothesisRequest(BaseModel):
    statement: str
    premise_fact_ids: list[str] = []
    created_by: str = "reason"

    @field_validator("statement")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        text = (value or "").strip()
        if not text:
            raise ValueError("statement 不能为空")
        if len(text) > 2000:
            raise ValueError("statement 过长（上限 2000）")
        return text


class UpdateHypothesisRequest(BaseModel):
    status: Literal["open", "testing", "confirmed", "refuted"] | None = None
    note: str | None = None
    result_fact_id: str | None = None


class BaselineEntry(BaseModel):
    """环境基线条目（P0-2）：已确认的作业前提，跨会话复用。

    `inherited=True` 表示这条来自**同目标的另一个项目**（读侧继承，不复制数据）——
    带上来源项目，worker 与人都能判断它是"这台机器上确认的"还是别处确认的。
    本项目自己写入的键永远优先，不会被继承值覆盖。
    """

    key: str
    value: str = ""
    note: str = ""
    source: str = "worker"
    updated_at: str = ""
    inherited: bool = False
    source_project_id: str | None = None
    source_project_title: str = ""


class UpsertBaselineRequest(BaseModel):
    entries: list[BaselineEntry]


class FindingQualityModel(BaseModel):
    """产物的可交付性评估（P0-1，派生字段：随响应计算，不入库）。"""

    applicable: bool = True
    score: int = 0
    total: int = 0
    complete: bool = True
    missing: list[str] = []
    missing_labels: list[str] = []
    notes: list[str] = []


class Vulnerability(BaseModel):
    id: str
    project_id: str
    fact_id: str
    intent_id: str | None = None
    title: str
    severity: Literal["critical", "high", "medium", "low", "info"] = "info"
    status: Literal["pending", "confirmed", "dismissed"] = "pending"
    kind: Literal["vuln", "flag", "finding"] = "vuln"
    score: int = 0
    url: str = ""
    description: str = ""
    evidence: str = ""
    reproduction: str = ""
    impact: str = ""
    recommendation: str = ""
    created_at: str
    verified_at: str | None = None
    # Deliverability (P0-1): 派生自 evidence/reproduction/impact/url 的完备度。
    # 只对 vuln/finding 有意义；flag 等评分类产物 applicable=False。
    quality: FindingQualityModel = FindingQualityModel()


class CreateVulnerabilityRequest(BaseModel):
    fact_id: str
    # Finding kind (batch B): vuln (default) | flag | finding. 'flag' entries are
    # scored artifacts (CTF/benchmark answers) rather than security findings.
    kind: Literal["vuln", "flag", "finding"] = "vuln"
    score: int = Field(default=0, ge=-1000000, le=1000000)
    intent_id: str | None = None
    title: str
    severity: Literal["critical", "high", "medium", "low", "info"] = "info"
    url: str = ""
    description: str = ""
    evidence: str = ""
    reproduction: str = ""
    impact: str = ""
    recommendation: str = ""

    @field_validator("title", "fact_id")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class SubGoal(BaseModel):
    """A phase-level objective on the graph (batch C)."""

    id: str
    project_id: str
    title: str
    status: Literal["pending", "active", "done", "abandoned"] = "pending"
    note: str = ""
    created_by: str = "reason"
    created_at: str
    concluded_at: str | None = None


class CreateSubGoalRequest(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    created_by: str = "human"

    @field_validator("title", "created_by")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip()


class UpdateSubGoalRequest(BaseModel):
    status: Literal["pending", "active", "done", "abandoned"]
    note: str = Field(default="", max_length=2000)


class AbandonIntentRequest(BaseModel):
    """Reason/planner abandons an open intent (batch A)."""

    reason: str = Field(default="", max_length=2000)
    worker: str = ""


class UpdateIntentPriorityRequest(BaseModel):
    """Planner raises/lowers an open intent's scheduling priority (batch A)."""

    priority: int = Field(default=0, ge=-100, le=100)
    worker: str = ""


class UpdateProjectDeadlineRequest(BaseModel):
    """Set (or clear, with null) a project's hard deadline (batch A)."""

    deadline_at: str | None = None


class UpdateProjectTaskModeRequest(BaseModel):
    task_mode: Literal["pentest", "scored"]


class UpdateVulnStatusRequest(BaseModel):
    status: Literal["pending", "confirmed", "dismissed"]


class VulnSeverityStats(BaseModel):
    """Cross-project vulnerability severity distribution (excludes dismissed)."""
    critical: int = 0
    high: int = 0
    medium: int = 0
    low: int = 0
    info: int = 0
    confirmed: int = 0
    total: int = 0


class VulnTrendPoint(BaseModel):
    """Single data point in the vulnerability discovery trend."""
    date: str
    count: int


# ---------------------------------------------------------------------------
# Knowledge base models (P3-1, 2026-08-29)
# ---------------------------------------------------------------------------

class KnowledgeEntry(BaseModel):
    """一条跨项目知识。

    `root_domain` 是规范主机键（`domain:…` / `ip:…` / `host:…` / `unattributed`），
    见 `sharp.server.hostkey`；`product` 是第二个匹配维度（空 = 未标注，
    不参与跨产品匹配）。`kind='dead_end'` 表示"已验证此路不通"。
    """

    id: int
    root_domain: str
    kind: Literal["credential", "endpoint", "fingerprint", "dead_end", "other"] = "credential"
    title: str = ""
    content: str
    source_project_id: str | None = None
    product: str = ""
    confidence: Literal["high", "medium", "low"] = "high"
    created_at: str
    updated_at: str


class CreateKnowledgeRequest(BaseModel):
    root_domain: str
    kind: Literal["credential", "endpoint", "fingerprint", "dead_end", "other"] = "credential"
    title: str = ""
    content: str
    product: str = ""
    confidence: Literal["high", "medium", "low"] = "high"

    @field_validator("root_domain", "content")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class AssessEndpointItem(BaseModel):
    """worker 对某个接口的评估结论（P1-B）。

    只有两种可写入的状态：`verified`（测过了）/ `dismissed`（确认无价值/已排除）。
    `discovered` 是**创建时**的初始态，不让 worker 把已评估的条目退回未评估 ——
    那会让覆盖报告的盲区清单重新变脏。
    """

    method: str = ""
    path: str
    status: Literal["verified", "dismissed"]
    note: str = ""

    @field_validator("path")
    @classmethod
    def _path_required(cls, value: str) -> str:
        text = (value or "").strip()
        if not text:
            raise ValueError("path 不能为空")
        return text


class AssessEndpointsRequest(BaseModel):
    items: list[AssessEndpointItem] = []
    worker: str = ""


class SetProductRequest(BaseModel):
    """设置项目的产品/指纹标注 —— 知识复用第二维度的来源。

    允许置空（表示"撤销标注"），所以不做非空校验；但限制长度与字符，
    避免把一句话当产品名写进去（产品名是要参与相等匹配的键，不是自由文本）。

    `force=False` 时**不覆盖已有的非空标注**：产品名决定跨产品匹配的走向，
    不该被某一轮的猜测改写（否则匹配结果会随轮次漂移）。要改由人工带 force 改。
    """

    product: str = ""
    force: bool = False

    @field_validator("product")
    @classmethod
    def validate_product(cls, value: str) -> str:
        text = (value or "").strip()
        if len(text) > 64:
            raise ValueError("产品名过长（最多 64 字符）")
        return text


class ExtractKnowledgeRequest(BaseModel):
    project_id: str
    description: str

    @field_validator("project_id", "description")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text



class ApprovalDecisionRequest(BaseModel):
    """Human decision on a pending intent (approve / reject).

    ``note`` is required on reject (fed back to the AI as a hint) and optional
    on approve (stored as the approval note).
    """

    note: str = Field(default="", max_length=2000)

    @field_validator("note")
    @classmethod
    def validate_note(cls, value: str) -> str:
        text = value.strip()
        if not text:
            return ""
        return text


class EmergencyRelease(BaseModel):
    """紧急模式自动放行的高危行动（P1-5）：供事后复核。

    急模式下不产生 pending（创建即自动批准），因此"审批队列"在急模式下是空的——
    人工协作的落点从"审批"变成"复核"，本模型就是那份清单。
    """

    project_id: str = ""
    project_title: str = ""
    intent_id: str
    description: str = ""
    risk_level: str = "low"
    released_at: str = ""
    release_note: str = ""
    status: str = ""                    # 执行状态描述（已结论/执行中/未开始/已放弃）
    has_evidence: bool = False          # 该行动是否已产出证据（供复核参考）
    reviewed: bool = False
    verdict: str = ""                   # ok | follow_up
    review_note: str = ""
    reviewed_at: str | None = None


class EmergencyReviewRequest(BaseModel):
    verdict: Literal["ok", "follow_up"] = "ok"
    note: str = ""


class EmergencyModeRequest(BaseModel):
    """Open / close emergency mode for a project (JWT-only, human action)."""

    enabled: bool | None = None   # None → toggle from current state
    reason: str = Field(default="", max_length=2000)
    hours: float = Field(default=1.0, gt=0, le=24 * 30)  # 1h..30d

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value: str) -> str:
        return value.strip()
