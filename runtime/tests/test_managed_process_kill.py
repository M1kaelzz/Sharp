"""Unit tests for KILL-1: ManagedProcess.kill() resolves the exec's session id
from the session file and reaps the whole session via `pkill -s`, run as root."""

from __future__ import annotations

import types

import pytest

from sharp.dispatcher.runtime import process as process_mod
from sharp.dispatcher.runtime.process import ManagedProcess

SESSION_FILE = "/tmp/sharp-exec/tok.sid"


class _FakeExecRun:
    def __init__(self, exit_code, output=b""):
        self.exit_code = exit_code
        self.output = output


class _RecordingContainer:
    def __init__(self, sid_output: bytes | None):
        self.name = "test-container"
        self.id = "cid"
        self.calls: list[tuple[list[str], str | None]] = []
        self._sid_output = sid_output
        self.client = types.SimpleNamespace(api=types.SimpleNamespace(exec_inspect=lambda eid: {"Running": True}))

    def exec_run(self, cmd, user=None, **kwargs):
        self.calls.append((cmd, user))
        if cmd[0] == "cat":
            if self._sid_output is None:
                return _FakeExecRun(1, b"")
            return _FakeExecRun(0, self._sid_output)
        return _FakeExecRun(0, b"")


def _make(container, session_file=SESSION_FILE) -> ManagedProcess:
    proc = ManagedProcess(container, ["setsid", "-w", "sh"], {}, session_file=session_file)
    proc._exec_id = "e1"  # bypass start(): we are testing kill() in isolation
    return proc


def test_kill_reaps_session_as_root():
    c = _RecordingContainer(sid_output=b"27\n")
    _make(c).kill()

    assert (["cat", SESSION_FILE], "0") in c.calls, "session file must be read as root"
    assert (["pkill", "-KILL", "-s", "27"], "0") in c.calls, "whole session must be killed as root"
    # cleanup of the session file the killed shell could not remove
    assert (["rm", "-f", "--", SESSION_FILE], "0") in c.calls


def test_kill_falls_back_when_session_id_unavailable(monkeypatch):
    monkeypatch.setattr(process_mod, "SESSION_ID_READ_TIMEOUT_SECONDS", 0.1)
    c = _RecordingContainer(sid_output=None)  # cat always fails -> no sid
    _make(c).kill()

    assert not any(cmd[0] == "pkill" for cmd, _ in c.calls), "must not pkill without a resolved session id"


def test_kill_noop_without_session_file(monkeypatch):
    monkeypatch.setattr(process_mod, "SESSION_ID_READ_TIMEOUT_SECONDS", 0.1)
    c = _RecordingContainer(sid_output=b"27\n")
    _make(c, session_file=None).kill()

    # No session file -> no reads, no pkill, no crash.
    assert c.calls == []


def test_kill_skips_when_exec_not_running():
    c = _RecordingContainer(sid_output=b"27\n")
    c.client.api.exec_inspect = lambda eid: {"Running": False}
    _make(c).kill()

    assert c.calls == [], "a finished exec needs no kill"
