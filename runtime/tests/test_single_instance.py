"""单实例锁测试（P1 覆盖率补强）。

`single_instance.py` 此前覆盖率为 **0%**，但它防的是最贵的一类事故：
两个 server（或两个 dispatcher）同时跑，一个项目"活"在 A 实例、dispatcher 却连 B 实例
—— 排查成本极高。这里用真实 `fcntl.flock` 验证互斥语义。

**不碰真实环境**：`_lock_path` 默认指向 `~/.local/share/sharp/`，测试里 monkeypatch 到
tmp_path，避免与用户正在运行的 Sharp 抢锁或在真实目录留文件。
"""

from __future__ import annotations

import os

import pytest

from sharp import single_instance as si


@pytest.fixture
def lock_paths(tmp_path, monkeypatch):
    """把锁文件重定向到 tmp_path。"""
    monkeypatch.setattr(si, "_lock_path", lambda kind: tmp_path / f"sharp-{kind}.lock")
    return tmp_path


def test_acquire_succeeds_and_writes_pid(lock_paths):
    handle = si.acquire_single_instance_lock("server", "127.0.0.1:8000")
    try:
        content = (lock_paths / "sharp-server.lock").read_text(encoding="utf-8")
        assert f"pid={os.getpid()}" in content
        assert "127.0.0.1:8000" in content
    finally:
        handle.close()


def test_second_acquire_of_same_kind_is_rejected(lock_paths):
    first = si.acquire_single_instance_lock("server", "first")
    try:
        with pytest.raises(si.SingleInstanceError) as exc:
            si.acquire_single_instance_lock("server", "second")
        msg = str(exc.value)
        assert "已在运行" in msg
        assert str(lock_paths / "sharp-server.lock") in msg
    finally:
        first.close()


def test_lock_is_reusable_after_release(lock_paths):
    """持有者退出（关闭句柄）后锁自动释放，无需清理陈旧文件。"""
    si.acquire_single_instance_lock("server", "first").close()
    handle = si.acquire_single_instance_lock("server", "second")
    try:
        assert "second" in (lock_paths / "sharp-server.lock").read_text(encoding="utf-8")
    finally:
        handle.close()


def test_server_and_dispatch_locks_are_independent(lock_paths):
    """server 与 dispatcher 是不同 kind，互不阻塞（同机本来就该各一个）。"""
    server = si.acquire_single_instance_lock("server", "web")
    dispatch = si.acquire_single_instance_lock("dispatch", "dispatch.yaml")
    try:
        assert (lock_paths / "sharp-server.lock").exists()
        assert (lock_paths / "sharp-dispatch.lock").exists()
    finally:
        server.close()
        dispatch.close()


def test_acquire_creates_parent_directory(tmp_path, monkeypatch):
    nested = tmp_path / "deep" / "nested"
    monkeypatch.setattr(si, "_lock_path", lambda kind: nested / f"sharp-{kind}.lock")
    handle = si.acquire_single_instance_lock("server")
    try:
        assert nested.is_dir()
    finally:
        handle.close()
