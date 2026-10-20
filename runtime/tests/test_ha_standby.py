"""P2 HA standby tests (batch: 12, active-standby hot spare).

Locks three layers:

- **server lease endpoint** (``POST /dispatcher/lease[/release]``): acquiring
  an empty/expired lease or renewing as the current holder wins; anyone else
  is told who holds it; release clears the holder. Server is the sole arbiter
  of liveness.
- **dispatcher state machine** (``DispatcherLoop._ha_tick``): disabled returns
  True forever (legacy); enabled promotes on acquire, demotes on an explicit
  "held by another", and keeps the previous mode on transport errors
  (fail-open — nobody can steal a lease from an unreachable server).
- **wiring**: the dispatching part of the run loop is gated on ``_ha_tick()``;
  close() releases the lease; cli bypasses the machine dispatch lock only when
  ha_standby is on.
- **mutant gate**: removing the run-loop gate must fail the source guards.

Loop state machine via DispatcherLoop.__new__ (same style as earlier batches);
server tests via temp DB + TestClient. No docker daemon, no live server.
"""

from __future__ import annotations

import inspect
import types

import pytest

from sharp.dispatcher.scheduler.loop import DispatcherLoop
from sharp.server import db

SERVER_TOKEN = "server-token-xyz"


# ── shared fixtures ────────────────────────────────────────────────────────

@pytest.fixture
def env_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_db_path", None, raising=False)
    monkeypatch.setenv("SHARP_SERVER_TOKEN", SERVER_TOKEN)
    db.configure(tmp_path / "sharp.db")


@pytest.fixture
def client(env_db):
    from fastapi.testclient import TestClient
    from sharp.server.app import app

    c = TestClient(app)
    yield c
    c.close()


def _h() -> dict[str, str]:
    return {"Authorization": f"Bearer {SERVER_TOKEN}"}


# ── 1. server lease endpoint ───────────────────────────────────────────────

def test_lease_acquire_then_others_rejected(client):
    r1 = client.post(
        "/dispatcher/lease",
        json={"dispatcher_id": "d1", "ttl_seconds": 30},
        headers=_h(),
    )
    assert r1.status_code == 200, r1.text
    assert r1.json()["acquired"] is True
    assert r1.json()["holder"] == "d1"

    r2 = client.post(
        "/dispatcher/lease",
        json={"dispatcher_id": "d2", "ttl_seconds": 30},
        headers=_h(),
    )
    assert r2.json()["acquired"] is False
    assert r2.json()["holder"] == "d1"


def test_lease_holder_renewal_wins(client):
    client.post("/dispatcher/lease", json={"dispatcher_id": "d1", "ttl_seconds": 30}, headers=_h())
    again = client.post(
        "/dispatcher/lease",
        json={"dispatcher_id": "d1", "ttl_seconds": 30},
        headers=_h(),
    )
    assert again.json()["acquired"] is True
    assert again.json()["holder"] == "d1"


def test_lease_expired_can_be_taken_over(client):
    # d1 acquires with a 1s ttl.
    client.post("/dispatcher/lease", json={"dispatcher_id": "d1", "ttl_seconds": 1}, headers=_h())
    import time as _time

    _time.sleep(1.2)  # let it expire (ISO-second resolution)
    taken = client.post(
        "/dispatcher/lease",
        json={"dispatcher_id": "d2", "ttl_seconds": 30},
        headers=_h(),
    )
    assert taken.json()["acquired"] is True
    assert taken.json()["holder"] == "d2"


def test_lease_release_allows_immediate_takeover(client):
    client.post("/dispatcher/lease", json={"dispatcher_id": "d1", "ttl_seconds": 60}, headers=_h())
    rel = client.post(
        "/dispatcher/lease/release",
        json={"dispatcher_id": "d1"},
        headers=_h(),
    )
    assert rel.json()["released"] is True
    taken = client.post(
        "/dispatcher/lease",
        json={"dispatcher_id": "d2", "ttl_seconds": 60},
        headers=_h(),
    )
    assert taken.json()["acquired"] is True


def test_lease_release_by_non_holder_noop(client):
    client.post("/dispatcher/lease", json={"dispatcher_id": "d1", "ttl_seconds": 60}, headers=_h())
    rel = client.post(
        "/dispatcher/lease/release",
        json={"dispatcher_id": "someone-else"},
        headers=_h(),
    )
    assert rel.json()["released"] is False
    still = client.post(
        "/dispatcher/lease",
        json={"dispatcher_id": "d2", "ttl_seconds": 60},
        headers=_h(),
    )
    assert still.json()["acquired"] is False


def test_lease_row_seeded_once_for_legacy_new_dbs(env_db):
    with db.get_conn() as conn:
        rows = conn.execute("SELECT COUNT(*) AS n FROM dispatcher_lease").fetchone()
        assert rows["n"] == 1


# ── 2. dispatcher state machine (_ha_tick) ─────────────────────────────────

class FakeLeaseClient:
    def __init__(self, result=None, error: Exception | None = None):
        self.result = result or {"acquired": True, "holder": "me", "expires_at": "x"}
        self.error = error
        self.calls: list[tuple[str, int]] = []

    def acquire_dispatcher_lease(self, dispatcher_id: str, ttl_seconds: int):
        self.calls.append((dispatcher_id, ttl_seconds))
        if self.error:
            raise self.error
        return self.result


def _make_loop(*, ha_enabled: bool, ha_active: bool, lease_client) -> DispatcherLoop:
    loop = DispatcherLoop.__new__(DispatcherLoop)
    loop.dispatcher_id = "me"
    loop._ha_enabled = ha_enabled
    loop._ha_active = ha_active
    loop.client = lease_client
    loop.config = types.SimpleNamespace(
        runtime=types.SimpleNamespace(ha_standby=ha_enabled, ha_takeover_after=15)
    )
    return loop


def test_ha_disabled_returns_true_always():
    loop = _make_loop(
        ha_enabled=False,
        ha_active=True,
        lease_client=FakeLeaseClient(error=RuntimeError("never called")),
    )
    assert loop._ha_tick() is True
    assert loop._ha_enabled is False


def test_standby_promoted_on_acquire():
    loop = _make_loop(
        ha_enabled=True, ha_active=False, lease_client=FakeLeaseClient({"acquired": True})
    )
    assert loop._ha_tick() is True
    assert loop._ha_active is True


def test_active_demoted_when_lease_held_by_other():
    loop = _make_loop(
        ha_enabled=True,
        ha_active=True,
        lease_client=FakeLeaseClient({"acquired": False, "holder": "other"}),
    )
    assert loop._ha_tick() is False
    assert loop._ha_active is False


def test_standby_stays_standby_when_other_holds():
    loop = _make_loop(
        ha_enabled=True,
        ha_active=False,
        lease_client=FakeLeaseClient({"acquired": False, "holder": "other"}),
    )
    assert loop._ha_tick() is False
    assert loop._ha_active is False


def test_transport_error_keeps_previous_mode():
    import requests

    # Active instance + network blip -> stays active (fail-open).
    act = _make_loop(
        ha_enabled=True,
        ha_active=True,
        lease_client=FakeLeaseClient(error=requests.ConnectionError("down")),
    )
    assert act._ha_tick() is True
    assert act._ha_active is True

    # Standby instance + network blip -> stays standby (no flapping).
    stb = _make_loop(
        ha_enabled=True,
        ha_active=False,
        lease_client=FakeLeaseClient(error=requests.ConnectionError("down")),
    )
    assert stb._ha_tick() is False
    assert stb._ha_active is False


def test_ha_takeover_ttl_passed_to_lease():
    lease_client = FakeLeaseClient()
    loop = _make_loop(ha_enabled=True, ha_active=False, lease_client=lease_client)
    loop._ha_tick()
    assert lease_client.calls == [("me", 15)]


# ── 3. wiring (source guards) ──────────────────────────────────────────────

def test_run_loop_gates_dispatch_on_ha_tick():
    src = inspect.getsource(DispatcherLoop.run)
    assert "self._ha_tick()" in src
    assert src.index("self._ha_tick()") < src.index("self._dispatch_available(")


def test_close_releases_lease():
    src = inspect.getsource(DispatcherLoop.close)
    assert "release_dispatcher_lease" in src


def test_cli_bypasses_dispatch_lock_only_when_ha_standby():
    from sharp.dispatcher.tasks import common  # noqa: F401  (import sanity)

    import sharp.cli as cli_mod

    src = inspect.getsource(cli_mod)
    assert "ha_standby" in src
    assert "acquire_single_instance_lock(\"dispatch\"" in src
    # The lock acquisition stays inside the else branch of the ha check.
    assert src.index("if ha_standby:") < src.index("acquire_single_instance_lock(\"dispatch\"")


def test_config_defaults_legacy():
    from sharp.dispatcher.config import RuntimeConfig

    assert RuntimeConfig.model_fields["ha_standby"].default is False
    assert RuntimeConfig.model_fields["ha_takeover_after"].default == 15

# ── 4. audit fix A2: restart endpoint under HA ─────────────────────────────

def _post_restart(client):
    return client.post("/dispatcher/restart", headers=_h())


def test_restart_ha_mode_stops_only_holder_pid(client, monkeypatch):
    """HA mode: the restart endpoint must stop ONLY the lease holder (parsed
    from its dispatcher_id) — never blanket-kill the standby via pgrep."""
    import signal as _signal

    import sharp.server.routers.dispatcher as disp_mod

    # Holder id in the "{host}:{pid}:{config}" format.
    client.post(
        "/dispatcher/lease",
        json={"dispatcher_id": "hosta:4242:dispatch.yaml", "ttl_seconds": 60},
        headers=_h(),
    )

    killed: set[int] = set()
    kills: list[tuple[int, int]] = []

    def fake_kill(pid, sig):
        kills.append((pid, sig))
        if sig == _signal.SIGTERM:
            killed.add(pid)
        elif sig == 0 and pid in killed:  # probe: holder has exited
            raise ProcessLookupError("gone")

    monkeypatch.setattr(disp_mod.os, "kill", fake_kill)

    # pgrep must NOT be used in HA mode; if it were, this would blow up.
    def no_subprocess(*a, **k):
        raise AssertionError("pgrep must not run in HA mode")

    monkeypatch.setattr(disp_mod.subprocess, "run", no_subprocess)

    r = _post_restart(client)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("action") == "ha_standby_takeover"
    assert body["signaled"] == 1
    assert all(pid == 4242 for pid, _ in kills), kills


def test_restart_ha_mode_unparseable_holder_gives_guidance(client):
    client.post(
        "/dispatcher/lease",
        json={"dispatcher_id": "weird-holder", "ttl_seconds": 60},
        headers=_h(),
    )
    r = _post_restart(client)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["signaled"] == 0
    assert "手动" in body["message"]


def test_restart_legacy_mode_keeps_pgrep_behavior(client, monkeypatch):
    """No active lease (legacy single-instance mode): unchanged pgrep + relaunch
    behavior — including an expired lease counting as legacy."""
    import sharp.server.routers.dispatcher as disp_mod
    import time as _time

    # Expired lease must NOT route into the HA branch.
    client.post(
        "/dispatcher/lease",
        json={"dispatcher_id": "hosta:9999:dispatch.yaml", "ttl_seconds": 1},
        headers=_h(),
    )
    _time.sleep(1.2)

    class FakeRun:
        stdout = ""
        returncode = 0

    monkeypatch.setattr(disp_mod.subprocess, "run", lambda *a, **k: FakeRun())
    r = _post_restart(client)
    assert r.status_code == 200, r.text
    assert r.json().get("action") == "relaunch_launcher"
