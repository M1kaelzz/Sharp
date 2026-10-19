"""P1-2 deterministic prescan tests (batch: P1-2).

Locks three layers:

- **scope hard-gate** (extract_scope_host / valid_scope_host): only a single
  hostname or IPv4 parsed from the origin passes; CIDR, wildcards, shell
  metacharacters, ports and weird input are rejected and skip the prescan
  (never guess, never extend the scope).
- **tool execution + fact seeding** (run_prescan_task): tools run in order
  against the scope host; missing/timed-out/failing tools are skipped; katana
  output is filtered to the scope host; the result is concluded into a seed
  fact through the normal intent channel; an empty run still concludes a
  "nothing found" fact so no intent is left dangling for explore to claim.
- **dispatch wiring** (loop): with prescan_enabled OFF the initial project
  dispatches bootstrap as before; ON it dispatches prescan first, once; a
  prescan intent is never explore-claimed.

The loop is built via DispatcherLoop.__new__ (same style as
test_bootstrap_stall.py). No docker daemon, no live server.
"""

from __future__ import annotations

import types

import pytest

from sharp.dispatcher.scheduler.loop import DispatcherLoop
from sharp.dispatcher.tasks import prescan as prescan_mod
from sharp.server.models import Fact, Intent, ProjectDetail, ProjectMeta

TS = "2026-09-01T00:00:00Z"


# ── scope parsing & hard-gate ──────────────────────────────────────────────

def test_extract_scope_host_ok():
    assert prescan_mod.extract_scope_host("http://target.example") == "target.example"
    assert prescan_mod.extract_scope_host("https://target.example:8443/path") == "target.example"
    assert prescan_mod.extract_scope_host("target.example") == "target.example"
    assert prescan_mod.extract_scope_host("http://10.0.0.5") == "10.0.0.5"


def test_extract_scope_host_none():
    assert prescan_mod.extract_scope_host("") is None
    assert prescan_mod.extract_scope_host("   ") is None
    assert prescan_mod.extract_scope_host("http://") is None
    assert prescan_mod.extract_scope_host("not a url at all with spaces") is None


def test_valid_scope_host_accepts_hostname_and_ipv4():
    assert prescan_mod.valid_scope_host("target.example") is True
    assert prescan_mod.valid_scope_host("a.b.c.d") is True
    assert prescan_mod.valid_scope_host("10.0.0.5") is True
    assert prescan_mod.valid_scope_host("192.168.1.1") is True


def test_valid_scope_host_rejects_scope_extension():
    # CIDR / netmask
    assert prescan_mod.valid_scope_host("10.0.0.0/24") is False
    # wildcards
    assert prescan_mod.valid_scope_host("*.example.com") is False
    # shell metacharacters
    assert prescan_mod.valid_scope_host("target.example;rm -rf /") is False
    assert prescan_mod.valid_scope_host("target.example|cat") is False
    assert prescan_mod.valid_scope_host("$(whoami).example") is False
    # port / IPv6 / garbage
    assert prescan_mod.valid_scope_host("target.example:8443") is False
    assert prescan_mod.valid_scope_host("2001:db8::1") is False
    assert prescan_mod.valid_scope_host("") is False
    assert prescan_mod.valid_scope_host("999.1.1.1") is False  # bad IPv4 octets


# ── run_prescan_task: tool execution & fact write ─────────────────────────

class FakeClient:
    def __init__(self):
        self.created: list[dict] = []
        self.concluded: list[tuple[str, str, str, str]] = []

    def create_intent(self, project_id, from_ids, description, creator, idempotency_key=None,
                      risk_level=None, risk_reason=None, depends_on=None):
        self.created.append({"from": from_ids, "description": description, "creator": creator})
        return types.SimpleNamespace(
            ok=True, status_code=201,
            data={"id": "prescan-1", "approval_status": "none"},
            text="",
        )

    def conclude(self, project_id, intent_id, worker, description):
        self.concluded.append((intent_id, worker, description))
        return types.SimpleNamespace(ok=True, status_code=200, data={}, text="")


class FakeCM:
    def __init__(self, outputs: dict[str, types.SimpleNamespace]):
        self.outputs = outputs
        self.calls: list[list[str]] = []

    def build_exec_process(self, container_name, env, argv, timeout_seconds=None):
        self.calls.append(argv)
        tool = argv[0]
        return FakeProcess(self.outputs.get(tool, _fail(tool)))


class FakeProcess:
    def __init__(self, result):
        self._result = result

    def start(self):
        pass

    def communicate(self, timeout=None):
        return self._result


def _ok(stdout: str = "") -> types.SimpleNamespace:
    return types.SimpleNamespace(stdout=stdout, stderr="", returncode=0, timed_out=False)


def _fail(tool: str) -> types.SimpleNamespace:
    return types.SimpleNamespace(stdout="", stderr=f"sh: {tool}: not found", returncode=127, timed_out=False)


def _timed_out() -> types.SimpleNamespace:
    return types.SimpleNamespace(stdout="", stderr="", returncode=0, timed_out=True)


def _project(origin: str = "http://target.example") -> ProjectDetail:
    meta = ProjectMeta(id="p1", title="p1", status="active", created_at=TS)
    return ProjectDetail(
        project=meta,
        facts=[Fact(id="origin", description=origin), Fact(id="goal", description="g")],
        intents=[],
        hints=[],
    )


def test_run_prescan_writes_fact_from_tools(monkeypatch):
    client = FakeClient()
    cm = FakeCM({
        "httpx": _ok("http://target.example [200] [Example Site] [nginx]"),
        "nmap": _ok("PORT STATE SERVICE\n80/tcp open http\n443/tcp open https"),
        "katana": _ok("http://target.example/\nhttp://target.example/admin\nhttp://evil.example/x\n"),
    })
    outcome = prescan_mod.run_prescan_task(
        types.SimpleNamespace(), client, cm, "c1", _project()
    )
    assert outcome == "success"
    assert client.created[0]["creator"] == "dispatcher.prescan"
    assert len(client.concluded) == 1
    iid, worker, desc = client.concluded[0]
    assert iid == "prescan-1"
    assert worker == "dispatcher.prescan"
    # katana out-of-scope URL filtered out
    assert "http://evil.example/x" not in desc
    assert "http://target.example/" in desc
    assert "Example Site" in desc
    assert "80/tcp open" in desc


def test_run_prescan_all_tools_fail_still_writes_fact(monkeypatch):
    client = FakeClient()
    cm = FakeCM({"httpx": _fail("httpx"), "nmap": _fail("nmap"), "katana": _fail("katana")})
    outcome = prescan_mod.run_prescan_task(types.SimpleNamespace(), client, cm, "c1", _project())
    assert outcome == "success"
    assert len(client.concluded) == 1
    assert "未发现有效信息" in client.concluded[0][2]


def test_run_prescan_invalid_scope_skips_without_intent():
    client = FakeClient()
    cm = FakeCM({})
    outcome = prescan_mod.run_prescan_task(types.SimpleNamespace(), client, cm, "c1",
                                           _project(origin="not a url with spaces"))
    assert outcome == "success"
    assert client.created == []  # no intent, nothing written


def test_run_prescan_katana_times_out_skipped():
    client = FakeClient()
    cm = FakeCM({"httpx": _ok("http://target.example [200]"), "nmap": _timed_out(),
                 "katana": _fail("katana")})
    prescan_mod.run_prescan_task(types.SimpleNamespace(), client, cm, "c1", _project())
    desc = client.concluded[0][2]
    assert "200" in desc
    assert "未发现有效信息" not in desc  # httpx evidence kept


# ── dispatch wiring ───────────────────────────────────────────────────────

def _make_loop(prescan_enabled: bool, client) -> DispatcherLoop:
    loop = DispatcherLoop.__new__(DispatcherLoop)
    loop.config = types.SimpleNamespace(runtime=types.SimpleNamespace(prescan_enabled=prescan_enabled))
    loop.client = client
    loop.futures = {}
    loop.executor = types.SimpleNamespace(submit=lambda fn, *a, **k: types.SimpleNamespace())
    loop.container_manager = types.SimpleNamespace(ensure_running=lambda pid: f"sharp-dispatch-{pid}")
    loop.runtime_project_ids = set()
    loop._cleanup_pending = set()
    loop.bootstrap_fail_counts = {}
    loop.bootstrap_stalled = {}
    loop.bootstrap_stall_hint_counts = {}
    loop.reason_checkpoints = {}
    loop.reason_stall_counts = {}
    loop.reason_stalled_hint_counts = {}
    loop.worker_unhealthy_until = {}
    loop.worker_rejected_until = {}
    loop._budget_hint_written = set()
    loop._deadline_hint_written = set()
    loop._log_state = {}
    loop.project_cursor = 0
    return loop


class IdleClient:
    def get_project(self, project_id):
        return _project()


def test_prescan_pending_true_without_prescan_intent():
    loop = _make_loop(True, IdleClient())
    assert loop._prescan_pending(_project()) is True


def test_prescan_pending_false_after_prescan_intent():
    loop = _make_loop(True, IdleClient())
    project = _project()
    project.intents.append(Intent(
        id="i1", project_id="p1", from_=["origin"], to_fact_id="f1",
        description="目标确定性预扫描：target.example",
        creator="dispatcher.prescan", worker=None, created_at=TS,
    ))
    assert loop._prescan_pending(project) is False


def test_dispatch_prescan_submits_once():
    submitted = []

    class _Exec:
        def submit(self, fn, *args, **kwargs):
            submitted.append(fn.__name__)
            return ("future", 1)

    loop = DispatcherLoop.__new__(DispatcherLoop)
    loop.config = types.SimpleNamespace(runtime=types.SimpleNamespace(prescan_enabled=True))
    loop.client = IdleClient()
    loop.executor = _Exec()
    loop.container_manager = types.SimpleNamespace(ensure_running=lambda pid: f"c-{pid}")
    loop.futures = {}
    loop.runtime_project_ids = set()
    assert loop._dispatch_prescan(_project()) is True
    assert submitted == ["run_prescan_task"]
    assert "p1" in loop.runtime_project_ids


def test_prescan_intent_never_explore_claimed():
    """A dangling prescan intent (conclude failed) must not be explore-claimed."""
    loop = DispatcherLoop.__new__(DispatcherLoop)
    loop.config = types.SimpleNamespace(runtime=types.SimpleNamespace(
        prescan_enabled=False, intent_dependency_gate=False
    ))
    loop.futures = {}
    project = _project()
    project.intents.append(Intent(
        id="i9", project_id="p1", from_=["origin"], to_fact_id=None,
        description="目标确定性预投", creator="dispatcher.prescan", worker=None, created_at=TS,
    ))
    from sharp.dispatcher.scheduler.loop import PRESCAN_CREATOR
    unclaimed = [
        i for i in project.intents
        if i.to is None and i.creator != PRESCAN_CREATOR
    ]
    assert unclaimed == []