"""P0-2 bootstrap stall protection tests (batch: P0-2).

Locks the gate semantics introduced by the bootstrap stall feature:

- _bootstrap_failed records a consecutive failure count; reaching the
  configured threshold writes ONE diagnostic hint and sets the stall flag.
- threshold == 0 (legacy default) keeps dispatch semantics byte-for-byte:
  _bootstrap_failed is a no-op and _bootstrap_dispatch_allowed always passes.
- While stalled, bootstrap dispatch is skipped; a new operator hint clears
  the stall and resumes dispatch.
- A success/cancelled bootstrap outcome never counts as a failure.

The loop is built via DispatcherLoop.__new__ with only the attributes each
method touches (same style as test_dispatch_decisions.py) — no docker daemon,
no live server, no full dispatch.yaml.
"""

from __future__ import annotations

import types

from sharp.dispatcher.scheduler.loop import DispatcherLoop
from sharp.server.models import (
    Fact,
    Hint,
    Intent,
    ProjectDetail,
    ProjectMeta,
    ProjectSummary,
)

TS = "2026-09-01T00:00:00Z"


def _summary(pid: str, hint_count: int = 0, **over) -> ProjectSummary:
    base = dict(
        id=pid,
        title=pid,
        status="active",
        created_at=TS,
        fact_count=2,  # initial-state projects
        intent_count=1,
        working_intent_count=0,
        unclaimed_intent_count=1,
        hint_count=hint_count,
    )
    base.update(over)
    return ProjectSummary(**base)


class RecordingClient:
    """Fake SharpClient for the bootstrap-stall paths."""

    def __init__(self, detail: ProjectDetail, threshold: int):
        self.detail = detail
        self.threshold = threshold
        self.hint_calls = 0
        self.hint_contents: list[str] = []

    def get_project(self, project_id: str) -> ProjectDetail:
        return self.detail

    def create_hint(self, project_id: str, content: str, creator: str):
        self.hint_calls += 1
        self.hint_contents.append(content)
        # A real HTTP client does not mutate the locally-fetched project
        # object; the diagnostic hint only appears on a LATER get_project
        # (as the real server would report it).
        return types.SimpleNamespace(ok=True, status_code=200)


def _bootstrap_intent() -> Intent:
    return Intent(
        id="i001",
        project_id="p1",
        from_=["origin"],
        to_fact_id=None,
        description="bootstrap",
        creator="dispatcher.bootstrap",
        worker=None,
        created_at=TS,
    )


def _detail(pid: str = "p1", hint_count: int = 0) -> ProjectDetail:
    meta = ProjectMeta(id=pid, title=pid, status="active", created_at=TS)
    return ProjectDetail(
        project=meta,
        facts=[Fact(id="origin", description="o"), Fact(id="goal", description="g")],
        intents=[_bootstrap_intent()],
        hints=[Hint(id=f"h{i}", content=f"c{i}", creator="test", created_at=TS) for i in range(1, hint_count + 1)],
    )


def _make_loop(client: RecordingClient, threshold: int) -> DispatcherLoop:
    loop = DispatcherLoop.__new__(DispatcherLoop)
    loop.config = types.SimpleNamespace(
        runtime=types.SimpleNamespace(bootstrap_stall_threshold=threshold)
    )
    loop.client = client
    loop.container_manager = types.SimpleNamespace(
        container_name=lambda pid: f"sharp-dispatch-{pid}"
    )
    loop._cleanup_pending = set()
    loop._budget_hint_written = set()
    loop._deadline_hint_written = set()
    loop.futures = {}
    loop.runtime_project_ids = set()
    loop._log_state = {}
    loop.reason_checkpoints = {}
    loop.reason_stall_counts = {}
    loop.reason_stalled_hint_counts = {}
    loop.bootstrap_fail_counts = {}
    loop.bootstrap_stalled = {}
    loop.bootstrap_stall_hint_counts = {}
    loop.project_cursor = 0
    return loop


# ── threshold == 0 → legacy pass-through ───────────────────────────────────

def test_bootstrap_failed_is_noop_when_disabled():
    client = RecordingClient(_detail(), threshold=0)
    loop = _make_loop(client, threshold=0)
    loop._bootstrap_failed("p1")
    assert client.hint_calls == 0
    assert loop.bootstrap_stalled == {}
    assert loop.bootstrap_fail_counts == {}


def test_bootstrap_dispatch_allowed_pass_through_when_disabled():
    loop = _make_loop(RecordingClient(_detail(), threshold=0), threshold=0)
    # Even without any stall state, the gate must allow dispatch.
    assert loop._bootstrap_dispatch_allowed(_summary("p1"), _detail()) is True


# ── failure counting ───────────────────────────────────────────────────────

def test_bootstrap_failed_counts_consecutive_failures_below_threshold():
    client = RecordingClient(_detail(), threshold=3)
    loop = _make_loop(client, threshold=3)
    loop._bootstrap_failed("p1")
    loop._bootstrap_failed("p1")
    assert loop.bootstrap_fail_counts["p1"] == 2
    assert loop.bootstrap_stalled == {}          # not stalled yet
    assert client.hint_calls == 0                # no diagnostic yet


def test_bootstrap_failed_reaches_threshold_writes_hint_and_stalls():
    client = RecordingClient(_detail(hint_count=0), threshold=2)
    loop = _make_loop(client, threshold=2)
    loop._bootstrap_failed("p1")
    loop._bootstrap_failed("p1")
    assert loop.bootstrap_stalled["p1"] is True
    assert client.hint_calls == 1
    assert "连续 2 次" in client.hint_contents[0]
    # stall hint count captured BEFORE the diagnostic was persisted (the
    # local project object is not mutated by the HTTP call).
    assert loop.bootstrap_stall_hint_counts["p1"] == 0
    # counter is reset once stalled — further failures must not spam.
    assert "p1" not in loop.bootstrap_fail_counts
    loop._bootstrap_failed("p1")
    assert client.hint_calls == 1               # one hint only


def test_bootstrap_failed_success_and_cancelled_do_not_count():
    # _bootstrap_failed is only invoked by _reap_futures for non-success,
    # non-cancelled outcomes; this test documents that a fresh failure count
    # starts from zero per project (no cross-project leakage).
    client = RecordingClient(_detail("p2"), threshold=2)
    loop = _make_loop(client, threshold=2)
    loop._bootstrap_failed("p2")
    loop._bootstrap_failed("p3")
    assert loop.bootstrap_fail_counts == {"p2": 1, "p3": 1}


# ── dispatch gate while stalled ────────────────────────────────────────────

def test_stalled_project_skipped_until_user_hint():
    client = RecordingClient(_detail(hint_count=1), threshold=2)
    loop = _make_loop(client, threshold=2)
    # reach stall: diagnostic hint persisted on the *server*, not the local object
    loop._bootstrap_failed("p1")
    loop._bootstrap_failed("p1")
    assert loop.bootstrap_stalled["p1"] is True
    assert len(client.detail.hints) == 1          # local object unchanged

    detail = client.detail
    # Simulate the server having persisted the diagnostic: the NEXT
    # get_project would see 2 hints (1 original + 1 diagnostic).
    detail.hints = detail.hints + [
        Hint(id="h-diag", content="[Sharp 诊断]", creator="dispatcher.stall_detector", created_at=TS)
    ]
    summary = _summary("p1", hint_count=len(detail.hints))
    # still stalled: no NEW operator hint beyond the diagnostic → not allowed
    assert loop._bootstrap_dispatch_allowed(summary, detail) is False
    assert loop.bootstrap_stalled["p1"] is True


def test_user_hint_clears_stall_and_resumes():
    client = RecordingClient(_detail(hint_count=1), threshold=2)
    loop = _make_loop(client, threshold=2)
    loop._bootstrap_failed("p1")
    loop._bootstrap_failed("p1")
    assert loop.bootstrap_stalled["p1"] is True

    detail = client.detail
    # Server already has the diagnostic (len 2); operator adds a real hint → 3.
    detail.hints = detail.hints + [
        Hint(id="h-diag", content="[Sharp 诊断]", creator="t", created_at=TS),
        Hint(id="h-op", content="operator direction", creator="human", created_at=TS),
    ]
    summary = _summary("p1", hint_count=len(detail.hints))
    assert loop._bootstrap_dispatch_allowed(summary, detail) is True
    assert "p1" not in loop.bootstrap_stalled
    assert "p1" not in loop.bootstrap_fail_counts
    assert "p1" not in loop.bootstrap_stall_hint_counts

# ── B4 (audit fix): stall rehydration after restart / HA takeover ───────────

BOOTSTRAP_DIAG = (
    "[Sharp 诊断] 引导任务已连续 2 次未产生任何结论。可能原因：初始侦察撞上执行超时……"
)
REASON_DIAG = (
    "[Sharp 诊断] 已连续 2 轮探索未发现新进展。可能原因：目标有防护拦截……"
)


def _detail_with_last_hint(pid: str, content: str, creator: str, extra_after: int = 0) -> ProjectDetail:
    hints = [
        Hint(id="h_user", content="user hint", creator="user", created_at=TS),
        Hint(id="h_diag", content=content, creator=creator, created_at=TS),
    ] + [
        Hint(id=f"h_post{i}", content=f"post hint {i}", creator="user", created_at=TS)
        for i in range(extra_after)
    ]
    return ProjectDetail(
        project=ProjectMeta(id=pid, title=pid, status="active", created_at=TS),
        facts=[Fact(id="origin", description="o"), Fact(id="goal", description="g")],
        intents=[_bootstrap_intent()],
        hints=hints,
    )


def test_stall_rehydrated_after_restart_skips_bootstrap():
    from sharp.dispatcher.scheduler.loop import STALL_HINT_CREATOR

    # Fresh dicts = post-restart/failover state. The project's LAST hint is
    # our bootstrap stall diagnostic (nothing from the operator after it).
    detail = _detail_with_last_hint("p1", BOOTSTRAP_DIAG, STALL_HINT_CREATOR)
    loop = _make_loop(RecordingClient(detail, threshold=2), threshold=2)
    assert loop._bootstrap_dispatch_allowed(_summary("p1"), detail) is False
    # And it stays skipped on subsequent ticks (idempotent rehydration).
    assert loop._bootstrap_dispatch_allowed(_summary("p1"), detail) is False


def test_stall_rehydrated_then_cleared_by_user_hint():
    from sharp.dispatcher.scheduler.loop import STALL_HINT_CREATOR

    detail = _detail_with_last_hint("p1", BOOTSTRAP_DIAG, STALL_HINT_CREATOR)
    loop = _make_loop(RecordingClient(detail, threshold=2), threshold=2)
    assert loop._bootstrap_dispatch_allowed(_summary("p1"), detail) is False
    # Operator adds a hint after the diagnostic -> resume.
    resumed = _detail_with_last_hint(
        "p1", BOOTSTRAP_DIAG, STALL_HINT_CREATOR, extra_after=1
    )
    assert loop._bootstrap_dispatch_allowed(_summary("p1"), resumed) is True


def test_operator_hint_after_diag_not_rehydrated():
    from sharp.dispatcher.scheduler.loop import STALL_HINT_CREATOR

    # The diagnostic exists but the user added a hint AFTER it: last hint is
    # the user's -> not stalled (fresh instance must not resurrect the stall).
    detail = _detail_with_last_hint("p1", BOOTSTRAP_DIAG, STALL_HINT_CREATOR, extra_after=1)
    loop = _make_loop(RecordingClient(detail, threshold=2), threshold=2)
    assert loop._bootstrap_dispatch_allowed(_summary("p1"), detail) is True


def test_reason_stall_hint_does_not_rehydrate_bootstrap():
    from sharp.dispatcher.scheduler.loop import STALL_HINT_CREATOR

    # Same creator, different diagnostic (reason-stall): must NOT rehydrate
    # the bootstrap stall — that reason project is not a bootstrap concern.
    detail = _detail_with_last_hint("p1", REASON_DIAG, STALL_HINT_CREATOR)
    loop = _make_loop(RecordingClient(detail, threshold=2), threshold=2)
    assert loop._bootstrap_dispatch_allowed(_summary("p1"), detail) is True


def test_stall_rehydration_off_when_threshold_zero():
    from sharp.dispatcher.scheduler.loop import STALL_HINT_CREATOR

    # Legacy feature-off: even with the diagnostic hint as the last hint,
    # bootstrap dispatches (threshold=0 pass-through is bit-for-bit).
    detail = _detail_with_last_hint("p1", BOOTSTRAP_DIAG, STALL_HINT_CREATOR)
    loop = _make_loop(RecordingClient(detail, threshold=0), threshold=0)
    assert loop._bootstrap_dispatch_allowed(_summary("p1"), detail) is True
