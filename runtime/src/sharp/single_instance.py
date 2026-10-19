"""Machine-global single-instance guard.

Prevents two Sharp servers (or two dispatchers) from running on the same
machine at once — the root cause of "project lives on one server, dispatcher
talks to another" confusion. Uses an advisory ``fcntl`` lock, so the OS
releases it automatically when the holding process exits (even on SIGKILL);
there is no stale pidfile to clean up.
"""

from __future__ import annotations

import os
from pathlib import Path

from sharp.server import db


class SingleInstanceError(RuntimeError):
    """Raised when another Sharp instance of the same kind already runs."""


def _lock_path(kind: str) -> Path:
    return db.DEFAULT_DB.parent / f"sharp-{kind}.lock"


def acquire_single_instance_lock(kind: str, detail: str = "-"):
    """Acquire the machine-global lock for ``kind`` ("server" or "dispatch").

    Returns an open file handle that MUST stay referenced for the process
    lifetime (closing it releases the lock). Returns ``None`` on platforms
    without ``fcntl`` rather than blocking startup. Raises
    ``SingleInstanceError`` if another instance already holds the lock.
    """
    try:
        import fcntl
    except ImportError:
        return None  # non-POSIX: skip the guard instead of failing

    path = _lock_path(kind)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.seek(0)
        existing = handle.read().strip() or "未知信息"
        handle.close()
        raise SingleInstanceError(
            f"另一个 Sharp {kind} 实例已在运行（{existing}）。\n"
            f"一台机器只允许运行一个 Sharp {kind}。请先停止它再启动。\n"
            f"锁文件：{path}"
        )
    handle.seek(0)
    handle.truncate()
    handle.write(f"pid={os.getpid()} detail={detail}")
    handle.flush()
    return handle
