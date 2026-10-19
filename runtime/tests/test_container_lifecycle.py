"""Container lifecycle tests (batch 10) against an injected fake docker client.

ContainerManager used to hardcode ``docker.from_env()`` in __init__, making its
entire lifecycle (create / reuse / restart / stop / remove / orphan cleanup)
untestable without a live docker daemon. ``client`` is now injectable; these
tests drive every lifecycle branch against a minimal fake implementing only the
docker SDK surface ContainerManager touches.
"""

from __future__ import annotations

import types

import docker.errors
import pytest
import requests

from sharp.dispatcher.config import ContainerConfig
from sharp.dispatcher.runtime.containers import ContainerManager


class FakeContainer:
    """Minimal docker Container stand-in with an in-memory state."""

    def __init__(self, name: str, state: str = "running", store: dict | None = None):
        self.name = name
        self.id = f"sha256:{name}"
        self._state = state
        self.attrs = {"State": {"Status": state}}
        self._store = store  # owner map so remove() mirrors docker semantics
        self.calls: list[object] = []

    def reload(self):
        self.calls.append("reload")
        self.attrs["State"]["Status"] = self._state

    def start(self):
        self.calls.append("start")
        self._state = "running"
        self.attrs["State"]["Status"] = "running"

    def stop(self, timeout=None):
        self.calls.append(("stop", timeout))
        self._state = "exited"
        self.attrs["State"]["Status"] = "exited"

    def remove(self, force=False):
        self.calls.append(("remove", force))
        if self._store is not None:
            self._store.pop(self.name, None)

    def exec_run(self, cmd, user=None, **kwargs):
        self.calls.append(("exec_run", cmd, user))
        return types.SimpleNamespace(exit_code=0, output=b"")

    def put_archive(self, path, data):
        self.calls.append(("put_archive", path))
        return True


class FakeContainers:
    def __init__(self):
        self.by_name: dict[str, FakeContainer] = {}
        self.run_calls: list[tuple[str, list, dict]] = []
        self._conflict_once = False

    def seed(self, name: str, state: str = "running") -> FakeContainer:
        c = FakeContainer(name, state, store=self.by_name)
        self.by_name[name] = c
        return c

    def run(self, image, command, **kwargs):
        self.run_calls.append((image, command, kwargs))
        name = kwargs["name"]
        if self._conflict_once:
            # Simulate a concurrent creator winning the race: by the time OUR
            # run call lands, the container exists and docker answers 409.
            self._conflict_once = False
            self.by_name[name] = FakeContainer(name, "running", store=self.by_name)
            raise docker.errors.APIError(
                "Conflict. The container name is already in use",
                response=_conflict_response(),
                explanation="Conflict. The container name is already in use",
            )
        c = FakeContainer(name, "running", store=self.by_name)
        self.by_name[name] = c
        return c

    def get(self, name: str) -> FakeContainer:
        c = self.by_name.get(name)
        if c is None:
            raise docker.errors.NotFound(f"container {name} not found")
        return c

    def list(self, all=False):
        return list(self.by_name.values())


class FakeDockerClient:
    def __init__(self):
        self.containers = FakeContainers()
        self.closed = False

    def close(self):
        self.closed = True


def _conflict_response() -> requests.Response:
    """A real requests.Response shaped like docker's 409 name-conflict answer.
    docker.errors.APIError.__str__ reads response.url/reason/status_code, so a
    bare SimpleNamespace would crash inside _is_name_conflict's str(exc)."""
    resp = requests.Response()
    resp.status_code = 409
    resp.url = "http://docker.local/v1.41/containers/create"
    resp.reason = "Conflict"
    return resp


def _manager(fake: FakeDockerClient, *, action: str = "remove") -> ContainerManager:
    cfg = ContainerConfig(image="sharp-worker:latest", completed_action=action)  # type: ignore[arg-type]
    return ContainerManager(cfg, client=fake)


def test_ensure_running_creates_when_absent():
    fake = FakeDockerClient()
    m = _manager(fake)
    name = m.ensure_running("proj-a")
    assert name == "sharp-dispatch-proj-a"
    assert len(fake.containers.run_calls) == 1
    image, cmd, kwargs = fake.containers.run_calls[0]
    assert image == "sharp-worker:latest"
    assert cmd == ["sleep", "infinity"]
    assert kwargs["name"] == "sharp-dispatch-proj-a"
    assert kwargs["detach"] is True


def test_ensure_running_reuses_running_container():
    fake = FakeDockerClient()
    fake.containers.seed("sharp-dispatch-proj-a", "running")
    m = _manager(fake)
    assert m.ensure_running("proj-a") == "sharp-dispatch-proj-a"
    assert fake.containers.run_calls == []


def test_ensure_running_starts_existing_stopped_container():
    fake = FakeDockerClient()
    c = fake.containers.seed("sharp-dispatch-proj-a", "exited")
    m = _manager(fake)
    assert m.ensure_running("proj-a") == "sharp-dispatch-proj-a"
    assert fake.containers.run_calls == []
    assert "start" in c.calls


def test_ensure_running_recovers_from_name_conflict():
    fake = FakeDockerClient()
    fake.containers._conflict_once = True  # first run() raises 409
    m = _manager(fake)
    # Entry inspect finds nothing -> tries run -> 409 -> falls back to the
    # container a concurrent creator just registered.
    assert m.ensure_running("proj-a") == "sharp-dispatch-proj-a"
    assert len(fake.containers.run_calls) == 1
    assert "sharp-dispatch-proj-a" in fake.containers.by_name


def test_cleanup_completed_remove_action():
    fake = FakeDockerClient()
    c = fake.containers.seed("sharp-dispatch-proj-a", "running")
    m = _manager(fake, action="remove")
    assert m.cleanup_completed("proj-a") is True
    assert any(call == ("remove", True) for call in c.calls)
    assert "sharp-dispatch-proj-a" not in fake.containers.by_name


def test_cleanup_completed_stop_action():
    fake = FakeDockerClient()
    c = fake.containers.seed("sharp-dispatch-proj-a", "running")
    m = _manager(fake, action="stop")
    assert m.cleanup_completed("proj-a") is True
    assert any(call[0] == "stop" for call in c.calls)
    assert "sharp-dispatch-proj-a" in fake.containers.by_name  # stopped, not removed


def test_cleanup_completed_absent_is_noop():
    fake = FakeDockerClient()
    m = _manager(fake)
    assert m.cleanup_completed("ghost") is True


def test_cleanup_stopped_stops_running_container():
    fake = FakeDockerClient()
    c = fake.containers.seed("sharp-dispatch-proj-a", "running")
    m = _manager(fake)
    assert m.cleanup_stopped("proj-a") is True
    assert any(call[0] == "stop" for call in c.calls)
    # Already stopped -> no-op.
    assert m.cleanup_stopped("proj-a") is True


def test_needs_cleanup_helpers():
    fake = FakeDockerClient()
    fake.containers.seed("sharp-dispatch-proj-a", "running")
    m_remove = _manager(fake, action="remove")
    assert m_remove.needs_completed_cleanup("proj-a") is True
    assert m_remove.needs_orphan_cleanup("sharp-dispatch-proj-a") is True
    assert m_remove.needs_stopped_cleanup("proj-a") is True
    assert m_remove.needs_completed_cleanup("ghost") is False


def test_remove_container_idempotent_and_validates_name():
    fake = FakeDockerClient()
    m = _manager(fake)
    # Absent container: silent no-op.
    m.remove_container("sharp-dispatch-ghost")
    # Injection guard: invalid names raise before touching docker.
    with pytest.raises(ValueError):
        m.remove_container("bad;name")
    with pytest.raises(ValueError):
        m.remove_container("../../etc")


def test_managed_container_names_filters_prefix():
    fake = FakeDockerClient()
    fake.containers.seed("sharp-dispatch-a", "exited")
    fake.containers.seed("sharp-startup-healthcheck-b", "exited")
    fake.containers.seed("unrelated", "running")
    m = _manager(fake)
    assert m.managed_container_names() == ["sharp-dispatch-a"]


def test_container_name_sanitize_and_validate():
    m = _manager(FakeDockerClient())
    # Project ids map to safe container names (injection characters replaced).
    assert m.container_name("acme corp") == "sharp-dispatch-acme-corp"
    assert m._sanitize_container_name("a b") == "a-b"
    # Anything non-alphanumeric leading is stripped; empty degrades to a placeholder.
    assert m._sanitize_container_name("!!!") == "project"
    assert m._sanitize_container_name("x" * 300) == "x" * 200


def test_remove_path_guards_outside_prefix():
    fake = FakeDockerClient()
    c = fake.containers.seed("sharp-dispatch-proj-a", "running")
    m = _manager(fake)
    # Refusing a path outside the scratch prefix must not exec anything.
    m.remove_path("sharp-dispatch-proj-a", "/etc/passwd", require_prefix="/tmp/sharp")
    assert not any(call[0] == "exec_run" for call in c.calls)
    # Allowlist root '/' is never acceptable either.
    m.remove_path("sharp-dispatch-proj-a", "/", require_prefix="/")
    assert not any(call[0] == "exec_run" for call in c.calls)


def test_write_text_file_and_missing_container_error():
    fake = FakeDockerClient()
    c = fake.containers.seed("sharp-dispatch-proj-a", "running")
    m = _manager(fake)
    m.write_text_file("sharp-dispatch-proj-a", "/tmp/sharp/x.txt", "hello")
    assert any(call[0] == "put_archive" for call in c.calls)
    with pytest.raises(RuntimeError):
        m.write_text_file("sharp-dispatch-missing", "/tmp/sharp/x.txt", "hello")


def test_close_delegates_to_client():
    fake = FakeDockerClient()
    m = _manager(fake)
    m.close()
    assert fake.closed is True
