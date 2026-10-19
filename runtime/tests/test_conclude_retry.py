"""P0-3 conclude retry tests (batch: P0-3).

Locks the retry semantics added to the conclude fallback in both
bootstrap and explore:

- conclude_retry == False (legacy default) keeps the exact single-attempt
  behavior: one failed conclude (timeout / non-zero exit / unparseable
  JSON) discards the whole round, exactly as before the feature.
- conclude_retry == True retries once on the SAME session: a first attempt
  that times out or returns unparseable JSON gets a second attempt with
  the same prompt/argv before the round is discarded.
- Cancellation and rejection are NEVER retried (retrying a model refusal
  or an operator cancel is pointless and would burn budget).
- Only a final failure releases the intent; an intermediate attempt that
  fails is not released (the retry continues on the same session).

The tasks under test are invoked directly with stub collaborators
(same style as test_bootstrap_stall.py) — no docker daemon, no live
server, no full dispatch.yaml.
"""

from __future__ import annotations

import json
import types

from sharp.dispatcher.tasks import bootstrap, explore
from sharp.server.models import Intent, ProjectDetail, ProjectMeta

TS = "2026-09-01T00:00:00Z"

VALID_BOOTSTRAP_CONCLUDE = json.dumps({
    "accepted": True,
    "data": {
        "fact": {"description": "指纹与目标面已确认"},
        "findings": [{"title": "SQL 注入点", "severity": "high"}],
    },
})

VALID_EXPLORE_CONCLUDE = json.dumps({
    "accepted": True,
    "data": {
        "description": "已验证 /api/v1/users 越权",
        "findings": [{"title": "IDOR", "severity": "medium"}],
    },
})

REJECTED_PAYLOAD = json.dumps({"accepted": False, "reason": "mock_rejected"})

NOT_JSON = "this is not json at all"


def _result(*, timed_out: bool = False, returncode: int = 0, stdout: str = "", stderr: str = ""):
    return types.SimpleNamespace(
        timed_out=timed_out,
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        cancelled=False,
        cancel_reason=None,
    )


class RecordingClient:
    """Fake SharpClient recording calls that matter for the retry semantics."""

    def __init__(self, project: ProjectDetail):
        self.project = project
        self.conclude_calls = 0
        self.release_calls = 0
        self.vuln_calls = 0

    def get_project(self, project_id: str) -> ProjectDetail:
        return self.project

    def fetch_env_baseline(self, project_id: str):
        return []

    def conclude(self, project_id: str, intent_id: str, worker_name: str, description: str):
        self.conclude_calls += 1
        return types.SimpleNamespace(
            ok=True, status_code=200, data={"fact": {"id": "f1"}}, text=""
        )

    def release(self, project_id: str, intent_id: str, worker_name: str):
        self.release_calls += 1
        return types.SimpleNamespace(ok=True, status_code=200)

    def upsert_env_baseline(self, project_id: str, entries):
        return types.SimpleNamespace(ok=True, status_code=200)

    def set_project_product(self, project_id: str, product: str):
        return types.SimpleNamespace(ok=True, status_code=200)

    def assess_endpoints(self, project_id: str, items, worker_name: str):
        return types.SimpleNamespace(ok=True, status_code=200)

    def create_vulnerability(self, project_id: str, fact_id: str, title: str, **kwargs):
        self.vuln_calls += 1
        return types.SimpleNamespace(ok=True, status_code=200)

    def extract_knowledge(self, project_id: str, description: str):
        return types.SimpleNamespace(ok=True, data={"extracted_count": 0})


class FakeContainerManager:
    def ensure_running(self, project_id: str) -> str:
        return f"sharp-dispatch-{project_id}"

    def write_text_file(self, container_name: str, path: str, content: str):
        pass

    def remove_path(self, container_name: str, path: str, require_prefix=None):
        pass


class FakeDriver:
    def supports_conclude(self) -> bool:
        return True

    def build_conclude(self, worker, prompt: str, session: str) -> list[str]:
        return ["conclude", "-r", session]

    def extract_response_text(self, stdout: str, stderr: str) -> str:
        return stdout


def _config(*, bootstrap_retry: bool, explore_retry: bool):
    return types.SimpleNamespace(
        runtime=types.SimpleNamespace(
            prompt_group="default", graph_distance_context=False
        ),
        tasks=types.SimpleNamespace(
            bootstrap=types.SimpleNamespace(
                conclude_timeout=180, conclude_retry=bootstrap_retry
            ),
            explore=types.SimpleNamespace(
                conclude_timeout=180, conclude_retry=explore_retry
            ),
        ),
    )


def _project(pid: str = "p1") -> ProjectDetail:
    meta = ProjectMeta(id=pid, title=pid, status="active", created_at=TS)
    return ProjectDetail(project=meta, facts=[], intents=[], hints=[])


def _intent(iid: str = "i001") -> Intent:
    return Intent(
        id=iid,
        project_id="p1",
        from_=["origin"],
        to_fact_id=None,
        description="bootstrap",
        creator="dispatcher.bootstrap",
        worker=None,
        created_at=TS,
    )


def _lease():
    return types.SimpleNamespace(failure=None, stop=lambda: None)


def _cancellation():
    return types.SimpleNamespace(is_cancelled=False, reason=None)


def _worker():
    return types.SimpleNamespace(name="w1")


# ── bootstrap: gate off preserves legacy single-attempt behavior ─────────

def test_bootstrap_retry_disabled_single_attempt(monkeypatch):
    client = RecordingClient(_project())
    outcomes = [_result(timed_out=True)]
    calls: list = []

    def fake_run(*args, **kwargs):
        calls.append(args)
        return outcomes.pop(0)

    monkeypatch.setattr(bootstrap, "run_worker_process", fake_run)
    monkeypatch.setattr(bootstrap, "preflight_mcp", lambda *a, **k: None)

    status = bootstrap._try_conclude_fallback(
        _config(bootstrap_retry=False, explore_retry=False),
        client,
        FakeContainerManager(),
        "sharp-dispatch-p1",
        _worker(),
        FakeDriver(),
        client.get_project("p1"),
        _intent(),
        "session-1",
        _lease(),
        _cancellation(),
    )
    assert status == "failed"
    assert len(calls) == 1            # 只有一次尝试
    assert client.release_calls == 1  # 关闭即释放
    assert client.conclude_calls == 0


# ── bootstrap: retry on timeout then success on the same session ──────────

def test_bootstrap_retry_timeout_then_success(monkeypatch):
    client = RecordingClient(_project())
    outcomes = [
        _result(timed_out=True),
        _result(stdout=VALID_BOOTSTRAP_CONCLUDE),
    ]
    calls: list = []

    def fake_run(*args, **kwargs):
        calls.append(args[0])
        return outcomes.pop(0)

    monkeypatch.setattr(bootstrap, "run_worker_process", fake_run)
    monkeypatch.setattr(bootstrap, "preflight_mcp", lambda *a, **k: None)

    status = bootstrap._try_conclude_fallback(
        _config(bootstrap_retry=True, explore_retry=False),
        client,
        FakeContainerManager(),
        "sharp-dispatch-p1",
        _worker(),
        FakeDriver(),
        client.get_project("p1"),
        _intent(),
        "session-1",
        _lease(),
        _cancellation(),
    )
    assert status == "success"
    assert len(calls) == 2             # 首次超时后重试一次
    assert calls[0] == calls[1]        # 重试复用同一 conclude argv（同 session）
    assert client.conclude_calls == 1  # 结论只写一次
    assert client.release_calls == 0   # 成功路径不释放


# ── bootstrap: retry on parse failure then success ────────────────────────

def test_bootstrap_retry_parse_error_then_success(monkeypatch):
    client = RecordingClient(_project())
    outcomes = [
        _result(stdout=NOT_JSON),
        _result(stdout=VALID_BOOTSTRAP_CONCLUDE),
    ]
    calls: list = []

    def fake_run(*args, **kwargs):
        calls.append(args[0])
        return outcomes.pop(0)

    monkeypatch.setattr(bootstrap, "run_worker_process", fake_run)
    monkeypatch.setattr(bootstrap, "preflight_mcp", lambda *a, **k: None)

    status = bootstrap._try_conclude_fallback(
        _config(bootstrap_retry=True, explore_retry=False),
        client,
        FakeContainerManager(),
        "sharp-dispatch-p1",
        _worker(),
        FakeDriver(),
        client.get_project("p1"),
        _intent(),
        "session-1",
        _lease(),
        _cancellation(),
    )
    assert status == "success"
    assert len(calls) == 2
    assert client.conclude_calls == 1


# ── bootstrap: both attempts fail → release once, fact never written ───────

def test_bootstrap_retry_both_fail_releases_once(monkeypatch):
    client = RecordingClient(_project())
    outcomes = [_result(timed_out=True), _result(timed_out=True)]
    calls: list = []

    def fake_run(*args, **kwargs):
        calls.append(args[0])
        return outcomes.pop(0)

    monkeypatch.setattr(bootstrap, "run_worker_process", fake_run)
    monkeypatch.setattr(bootstrap, "preflight_mcp", lambda *a, **k: None)

    status = bootstrap._try_conclude_fallback(
        _config(bootstrap_retry=True, explore_retry=False),
        client,
        FakeContainerManager(),
        "sharp-dispatch-p1",
        _worker(),
        FakeDriver(),
        client.get_project("p1"),
        _intent(),
        "session-1",
        _lease(),
        _cancellation(),
    )
    assert status == "failed"
    assert len(calls) == 2             # 重试了一次
    assert client.conclude_calls == 0    # 结论从未写出
    assert client.release_calls == 1      # 最终失败释放一次


# ── bootstrap: model rejection is never retried ───────────────────────────

def test_bootstrap_retry_rejected_not_retried(monkeypatch):
    client = RecordingClient(_project())
    calls: list = []

    def fake_run(*args, **kwargs):
        calls.append(args[0])
        return _result(stdout=REJECTED_PAYLOAD)

    monkeypatch.setattr(bootstrap, "run_worker_process", fake_run)
    monkeypatch.setattr(bootstrap, "preflight_mcp", lambda *a, **k: None)

    status = bootstrap._try_conclude_fallback(
        _config(bootstrap_retry=True, explore_retry=False),
        client,
        FakeContainerManager(),
        "sharp-dispatch-p1",
        _worker(),
        FakeDriver(),
        client.get_project("p1"),
        _intent(),
        "session-1",
        _lease(),
        _cancellation(),
    )
    assert status == "rejected"
    assert len(calls) == 1                     # 拒绝不重试
    assert client.release_calls == 1


# ── bootstrap: findings from the retried conclude still register vulns ─────

def test_bootstrap_retry_success_registers_findings(monkeypatch):
    client = RecordingClient(_project())
    outcomes = [
        _result(timed_out=True),
        _result(stdout=VALID_BOOTSTRAP_CONCLUDE),
    ]

    def fake_run(*args, **kwargs):
        return outcomes.pop(0)

    monkeypatch.setattr(bootstrap, "run_worker_process", fake_run)
    monkeypatch.setattr(bootstrap, "preflight_mcp", lambda *a, **k: None)

    status = bootstrap._try_conclude_fallback(
        _config(bootstrap_retry=True, explore_retry=False),
        client,
        FakeContainerManager(),
        "sharp-dispatch-p1",
        _worker(),
        FakeDriver(),
        client.get_project("p1"),
        _intent(),
        "session-1",
        _lease(),
        _cancellation(),
    )
    assert status == "success"
    assert client.conclude_calls == 1
    assert client.vuln_calls == 1              # 重试成功的那轮 findings 照常入库


# ── explore: disabled keeps legacy single attempt ─────────────────────────

def test_explore_retry_disabled_single_attempt(monkeypatch):
    client = RecordingClient(_project())
    calls: list = []

    def fake_run(*args, **kwargs):
        calls.append(args[0])
        return _result(returncode=124)         # exit 124 超时 kill

    monkeypatch.setattr(explore, "_run_process", fake_run)
    monkeypatch.setattr(explore, "preflight_mcp", lambda *a, **k: None)

    status = explore._try_conclude_fallback(
        _config(bootstrap_retry=False, explore_retry=False),
        client,
        FakeContainerManager(),
        "sharp-dispatch-p1",
        _worker(),
        FakeDriver(),
        "p1",
        _intent(),
        "graph: {}\n",
        "session-1",
        _lease(),
        _cancellation(),
        [],
    )
    assert status == "failed"
    assert len(calls) == 1
    assert client.release_calls == 1


# ── explore: retry on timeout then success on the same session ────────────

def test_explore_retry_timeout_then_success(monkeypatch):
    client = RecordingClient(_project())
    outcomes = [
        _result(returncode=124),
        _result(stdout=VALID_EXPLORE_CONCLUDE),
    ]
    calls: list = []

    def fake_run(*args, **kwargs):
        calls.append(args[0])
        return outcomes.pop(0)

    monkeypatch.setattr(explore, "_run_process", fake_run)
    monkeypatch.setattr(explore, "preflight_mcp", lambda *a, **k: None)

    status = explore._try_conclude_fallback(
        _config(bootstrap_retry=False, explore_retry=True),
        client,
        FakeContainerManager(),
        "sharp-dispatch-p1",
        _worker(),
        FakeDriver(),
        "p1",
        _intent(),
        "graph: {}\n",
        "session-1",
        _lease(),
        _cancellation(),
        [],
    )
    assert status == "success"
    assert len(calls) == 2
    assert calls[0] == calls[1]                # 同一 argv（同 session）重试
    assert client.conclude_calls == 1
    assert client.vuln_calls == 1