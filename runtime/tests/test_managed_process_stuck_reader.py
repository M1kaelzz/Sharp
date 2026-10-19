"""Merge-gate tests for patch #2: a reader thread that ignores kill must still
be unblocked by force-closing the docker stream, so communicate() returns and
no reader thread is leaked.
"""

from __future__ import annotations

import threading
import types

from sharp.dispatcher.runtime.process import ManagedProcess


class _BlockingStream:
    """Iterator that blocks in __next__ until close() is called, then raises a
    NON-DockerException (as the real urllib3/socket stack does when closed from
    another thread). Verifies both the stream-close unblock (#2) and the widened
    ``except Exception`` in the reader."""

    def __init__(self):
        self._closed = threading.Event()
        self.close_calls = 0

    def __iter__(self):
        return self

    def __next__(self):
        self._closed.wait()
        raise ValueError("read of closed file")

    def close(self):
        self.close_calls += 1
        self._closed.set()


class _YieldingStream:
    def __init__(self, chunks):
        self._chunks = list(chunks)
        self.close_calls = 0

    def __iter__(self):
        return iter(self._chunks)

    def close(self):
        self.close_calls += 1


class _FakeAPI:
    def __init__(self, stream, inspect):
        self._stream = stream
        self._inspect = inspect

    def exec_create(self, *args, **kwargs):
        return {"Id": "exec-1"}

    def exec_start(self, *args, **kwargs):
        return self._stream

    def exec_inspect(self, exec_id):
        return self._inspect


class _FakeExecRun:
    def __init__(self, exit_code=0):
        self.exit_code = exit_code


class _FakeContainer:
    def __init__(self, api):
        self.id = "cont-1"
        self.name = "test-container"
        self.client = types.SimpleNamespace(api=api)

    def exec_run(self, command, stdout=False, stderr=False):
        return _FakeExecRun(0)


def test_stuck_reader_is_unblocked_by_stream_close():
    stream = _BlockingStream()
    # kill() path: exec_inspect must report Running + a Pid; _resolve_exit_code
    # then reads ExitCode.
    api = _FakeAPI(stream, inspect={"Running": True, "Pid": 4242, "ExitCode": 137})
    process = ManagedProcess(_FakeContainer(api), ["sleep", "infinity"], env={})
    process.start()

    result = process.communicate(timeout=0.2)

    assert result.timed_out is True
    assert result.returncode == 137
    assert stream.close_calls >= 1  # stream was force-closed to unblock the reader
    # The reader thread must have terminated (no leak).
    assert process._reader is not None
    process._reader.join(timeout=2)
    assert not process._reader.is_alive()


def test_normal_stream_completes_cleanly():
    stream = _YieldingStream([(b"hello ", None), (None, b"a warning"), (b"world", None)])
    api = _FakeAPI(stream, inspect={"Running": False, "ExitCode": 0})
    process = ManagedProcess(_FakeContainer(api), ["echo", "hi"], env={})
    process.start()

    result = process.communicate(timeout=5)

    assert result.timed_out is False
    assert result.returncode == 0
    assert result.stdout == "hello world"
    assert result.stderr == "a warning"
    process._reader.join(timeout=2)
    assert not process._reader.is_alive()
