"""Unit tests for the post-injection size check in
ContainerManager.write_binary_file: put_archive returning True is not proof the
bytes landed intact, so the method stats the file inside the container and
rejects a size mismatch."""

from __future__ import annotations

import types

import pytest

from sharp.dispatcher.runtime.containers import ContainerManager


class _FakeExecRun:
    def __init__(self, exit_code: int, output: bytes = b""):
        self.exit_code = exit_code
        self.output = output


class _FakeContainer:
    def __init__(self, stat_output: bytes, *, put_ok: bool = True):
        self.name = "sharp-test"
        self._stat_output = stat_output
        self._put_ok = put_ok

    def put_archive(self, path, archive):
        return self._put_ok

    def exec_run(self, cmd, **kwargs):
        if cmd[0] == "stat":
            return _FakeExecRun(0, self._stat_output)
        return _FakeExecRun(0, b"")


def _manager_with(container) -> ContainerManager:
    mgr = ContainerManager.__new__(ContainerManager)
    mgr._client = types.SimpleNamespace(
        containers=types.SimpleNamespace(get=lambda name: container)
    )
    return mgr


def _write(mgr, tmp_path, payload=b"hello apk"):
    src = tmp_path / "target.apk"
    src.write_bytes(payload)
    return mgr.write_binary_file("sharp-test", "/data/target.apk", str(src), max_bytes=1_000_000)


def test_size_match_returns_bytes(tmp_path):
    payload = b"hello apk"
    mgr = _manager_with(_FakeContainer(stat_output=str(len(payload)).encode()))
    assert _write(mgr, tmp_path, payload) == len(payload)


def test_size_mismatch_raises(tmp_path):
    # Container reports fewer bytes than were sent -> truncated write.
    mgr = _manager_with(_FakeContainer(stat_output=b"3"))
    with pytest.raises(RuntimeError, match="size mismatch"):
        _write(mgr, tmp_path, b"hello apk")


def test_unverifiable_size_is_not_fatal(tmp_path):
    # stat unparseable (odd base image) -> cannot verify, must not block.
    payload = b"hello apk"
    mgr = _manager_with(_FakeContainer(stat_output=b"not-a-number"))
    assert _write(mgr, tmp_path, payload) == len(payload)
