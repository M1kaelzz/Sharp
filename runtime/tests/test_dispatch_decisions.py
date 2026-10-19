"""Dispatch decision tests (batch 10): pure rotation helper + gate branches.

The dispatcher's per-cycle decision tree (_try_dispatch_project) is exercised
without a docker daemon, a live server, or a full dispatch.yaml: instances are
built via ``DispatcherLoop.__new__`` and only the attributes each method touches
are attached (client / container_manager fakes + plain dicts). The point is to
lock the *gate semantics* — which conditions must skip a project, and which
side effects must NOT fire (e.g. no get_project on a summary-level skip).
"""

from __future__ import annotations

import time
import types

from sharp.dispatcher.models import RunningTask
from sharp.dispatcher.runtime.cancellation import TaskCancellation
from sharp.dispatcher.scheduler.loop import DispatcherLoop, rotate_ids
from sharp.server.models import (
    Fact,
    Intent,
    ProjectDetail,
    ProjectMeta,
    ProjectReason,
    ProjectSummary,
)

TS = "2026-09-01T00:00:00Z"


def _summary(pid: str, **over) -> ProjectSummary:
    base = dict(
        id=pid,
        title=pid,
        status="active",
        created_at=TS,
        fact_count=3,
        intent_count=0,
        working_intent_count=0,
        unclaimed_intent_count=0,
        hint_count=0,
    )
    base.update(over)
    return ProjectSummary(**base)


class RecordingClient:
    """Fake SharpClient: records get_project/create_hint; get_project raises if
    no detail was configured (proves a branch never reached the server)."""

    def __init__(self, detail: ProjectDetail | None = None):
        self.detail = detail
        self.get_calls = 0
        self.hint_calls = 0

    def get_project(self, project_id: str) -> ProjectDetail:
        self.get_calls += 1
        if self.detail is None:
            raise AssertionError(f"unexpected get_project({project_id})")
        return self.detail

    def create_hint(self, project_id: str, content: str, creator: str):
        self.hint_calls += 1
        return types.SimpleNamespace(ok=True, status_code=200)


def _make_loop(client: RecordingClient | None = None, max_project_workers: int = 4) -> DispatcherLoop:
    loop = DispatcherLoop.__new__(DispatcherLoop)
    loop.config = types.SimpleNamespace(
        runtime=types.SimpleNamespace(max_project_workers=max_project_workers)
    )
    loop.client = client if client is not None else RecordingClient()
    loop.container_manager = types.SimpleNamespace(
        container_name=lambda pid: f"sharp-dispatch-{pid}"
    )
    loop._cleanup_pending = set()
    loop._budget_hint_written = set()
    loop.futures = {}
    loop.runtime_project_ids = set()
    loop._log_state = {}
    loop.reason_checkpoints = {}
    loop.reason_stall_counts = {}
    loop.reason_stalled_hint_counts = {}
    loop.project_cursor = 0
    return loop


def _running_task(pid: str, task_type: str = "explore") -> RunningTask:
    return RunningTask(pid, task_type, "w1", TaskCancellation(), time.monotonic())


def _detail(project: ProjectMeta | None = None, intents=None, facts=None) -> ProjectDetail:
    return ProjectDetail(
        project=project or ProjectMeta(id="p1", title="t", status="active", created_at=TS),
        facts=facts or [Fact(id="origin", description="o"), Fact(id="goal", description="g")],
        intents=intents or [],
        hints=[],
    )


# ── rotate_ids (pure) ──────────────────────────────────────────────────────────

def test_rotate_ids_starts_at_cursor_and_wraps():
    ids = ["a", "b", "c"]
    assert rotate_ids(ids, 0) == (["a", "b", "c"], 1)
    assert rotate_ids(ids, 1) == (["b", "c", "a"], 2)
    assert rotate_ids(ids, 2) == (["c", "a", "b"], 3)
    assert rotate_ids(ids, 3) == (["a", "b", "c"], 4)  # wraps past len


def test_rotate_ids_empty_does_not_advance_cursor():
    assert rotate_ids([], 7) == ([], 7)


def test_ordered_projects_rotates_fairly_across_cycles():
    loop = _make_loop()
    summaries = [_summary("b"), _summary("a"), _summary("c")]
    first = loop._ordered_projects(summaries)
    second = loop._ordered_projects(summaries)
    third = loop._ordered_projects(summaries)
    # Every cycle starts where the previous one left off: no project starves.
    assert [s.id for s in first] == ["a", "b", "c"]
    assert [s.id for s in second] == ["b", "c", "a"]
    assert [s.id for s in third] == ["c", "a", "b"]


def test_ordered_projects_empty_keeps_cursor():
    loop = _make_loop()
    assert loop._ordered_projects([]) == []
    assert loop.project_cursor == 0


# ── _try_dispatch_project gate branches ───────────────────────────────────────

def test_paused_project_skipped_before_get_project():
    client = RecordingClient()
    loop = _make_loop(client)
    assert loop._try_dispatch_project(_summary("p1", paused=True)) is False
    assert client.get_calls == 0  # summary-level skip: no detail request


def test_cleanup_pending_skips_dispatch():
    client = RecordingClient()
    loop = _make_loop(client)
    loop._cleanup_pending.add("sharp-dispatch-p1")
    assert loop._try_dispatch_project(_summary("p1")) is False
    assert client.get_calls == 0


def test_max_project_workers_skips_dispatch():
    client = RecordingClient()
    loop = _make_loop(client, max_project_workers=1)
    loop.futures[object()] = _running_task("p1")
    assert loop._try_dispatch_project(_summary("p1")) is False
    assert client.get_calls == 0


def test_summary_no_work_skips_get_project():
    # reason claimed by another worker + no unclaimed intents → nothing to do.
    client = RecordingClient()
    loop = _make_loop(client)
    summary = _summary(
        "p1",
        reason=ProjectReason(worker="other", trigger="initial", started_at=TS, last_heartbeat_at=TS),
    )
    assert loop._try_dispatch_project(summary) is False
    assert client.get_calls == 0


def test_task_budget_exhausted_blocks_and_hints_once():
    detail = _detail(
        project=ProjectMeta(id="p1", title="t", status="active", created_at=TS, task_budget=2, task_count=2)
    )
    client = RecordingClient(detail=detail)
    loop = _make_loop(client)
    summary = _summary("p1", fact_count=10, intent_count=1, unclaimed_intent_count=1)
    assert loop._try_dispatch_project(summary) is False
    assert client.get_calls == 1
    assert client.hint_calls == 1
    # A second attempt in a later cycle must not duplicate the hint.
    assert loop._try_dispatch_project(summary) is False
    assert client.hint_calls == 1


def test_approval_pending_blocks_reason_pass():
    pending = Intent(
        id="i2",
        from_=["origin"],
        description="写入 webshell 获取权限",
        creator="ai_worker",
        created_at=TS,
        risk_level="critical",
        approval_status="pending",
    )
    detail = _detail(
        project=ProjectMeta(id="p1", title="t", status="active", created_at=TS),
        intents=[pending],
        facts=[Fact(id="origin", description="o"), Fact(id="goal", description="g"), Fact(id="f3", description="x")],
    )
    client = RecordingClient(detail=detail)
    loop = _make_loop(client)
    summary = _summary("p1", fact_count=3, intent_count=1, unclaimed_intent_count=1)
    # Pending high-risk intent must NOT be explored; reason must wait for the
    # human decision instead of re-proposing the same intent.
    assert loop._try_dispatch_project(summary) is False
    assert client.get_calls == 1


def test_non_active_project_without_report_work_skipped():
    detail = _detail(
        project=ProjectMeta(id="p1", title="t", status="completed", created_at=TS),
        intents=[],
    )
    client = RecordingClient(detail=detail)
    loop = _make_loop(client)
    assert loop._try_dispatch_project(_summary("p1", status="completed")) is False
    assert client.get_calls == 1


# ── 容器回收的决策层（ENV-2 查证后补）──────────────────────────────────────
#
# 背景：观察"暂停/失败的项目容器长期 Up"时，我一度把它当成"漏了回收"。查证后
# 结论相反 —— 这是**刻意的热容器策略**：
#   completed / stopped → 回收（stop 或 remove，由 completed_action 配置）
#   active（含 paused）  → 不回收：随时可能派发新任务，停掉只会让下一轮付出冷启动
# 行为层（cleanup_completed / cleanup_stopped / managed_container_names）已有测试，
# 但**决策层**（哪些状态进清理队列）此前没有 —— 回归后果是两头的：
# 该清的没清（容器泄漏）或不该清的清了（正要用的容器被停）。

class _FakeFuture:
    """可哈希的假 Future —— 循环会把它当 dict 键（SimpleNamespace 不可哈希）。"""

    def done(self) -> bool:
        return True

    def result(self) -> bool:
        return True


class _SyncExecutor:
    """同步执行的替身：把 cleanup_* 的调用直接落到记录里，避免测试再解一层 Future。"""

    def submit(self, fn, *args, **kwargs):
        fn(*args, **kwargs)
        return _FakeFuture()


def _cleanup_loop(statuses: dict[str, str], *, completed_action: str = "stop"):
    """带记录能力的容器管理器替身 + 一个循环实例。

    **替身必须忠实于真实语义**：`needs_*_cleanup` 在真实实现里问的是"容器在不在跑"
    （`completed_action == 'remove'` 或 state == 'running'），**与项目状态无关**。
    第一版让它对 active 直接返回 False，结果把循环自己的状态过滤掩盖了 ——
    注入"active 也清理"的突变时测试照样通过（守卫是假的）。改成"容器都在跑"之后，
    真正拦住 active 的是循环里的状态判断，突变才会被抓到。
    """
    calls: list[tuple[str, str]] = []
    loop = _make_loop()
    loop.cleanup_executor = _SyncExecutor()
    loop.cleanup_futures = {}
    loop._inactive_cleanup_done = {}
    loop.container_manager = types.SimpleNamespace(
        container_name=lambda pid: f"sharp-dispatch-{pid}",
        # 所有容器都在跑 → 是否需要清理与项目状态无关，交给循环自己判断
        needs_completed_cleanup=lambda pid: True,
        needs_stopped_cleanup=lambda pid: True,
        cleanup_completed=lambda pid: (calls.append(("completed", pid)), True)[1],
        cleanup_stopped=lambda pid: (calls.append(("stopped", pid)), True)[1],
        # 孤儿清理：这里返回空，单独用 orphan 用例测
        managed_container_names=lambda: [],
    )
    return loop, calls


def test_active_project_container_is_never_cleaned():
    """active 项目（含 paused）必须保留热容器 —— 这是设计，不是漏回收。

    真实观察：暂停的 proj_003/005 容器长期 Up。查证结论是刻意的：
    随时可能派发新任务，停掉只会让下一轮付冷启动成本。
    """
    loop, calls = _cleanup_loop({"p1": "active"})
    loop._queue_container_cleanups([_summary("p1", status="active")])
    assert calls == [], f"active 项目不该被清理，实得 {calls}"


def test_paused_active_project_container_is_kept():
    loop, calls = _cleanup_loop({"p1": "active"})
    loop._queue_container_cleanups([_summary("p1", status="active", paused=True)])
    assert calls == []


def test_stopped_project_container_is_cleaned():
    loop, calls = _cleanup_loop({"p1": "stopped"})
    loop._queue_container_cleanups([_summary("p1", status="stopped")])
    assert ("stopped", "p1") in calls


def test_completed_project_container_is_cleaned():
    loop, calls = _cleanup_loop({"p1": "completed"})
    loop._queue_container_cleanups([_summary("p1", status="completed")])
    assert ("completed", "p1") in calls


def test_completed_cleanup_waits_for_running_task():
    """容器还在跑任务时不能清（否则正在执行的工作被打断）。"""
    loop, calls = _cleanup_loop({"p1": "completed"})
    loop.futures = {_FakeFuture(): _running_task("p1")}   # 真实结构是 {Future: RunningTask}
    loop._queue_container_cleanups([_summary("p1", status="completed")])
    assert calls == []


def test_cleanup_is_not_repeated_for_same_status():
    """同一状态只清理一次 —— 否则每个调度周期都会去 stop 一遍。"""
    loop, calls = _cleanup_loop({"p1": "stopped"})
    loop._queue_container_cleanups([_summary("p1", status="stopped")])
    first = list(calls)
    assert first, "第一次应触发清理"
    loop._queue_container_cleanups([_summary("p1", status="stopped")])
    assert calls == first, f"不该重复清理，实得 {calls}"
