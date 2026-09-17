"""Live-Docker smoke test for the patched container/process runtime.

Not part of the unit suite (requires a running Docker daemon + the worker image).
Run manually:

    ./.venv/bin/python tests/smoke_container.py

Asserts the guarantees the runtime provides after patches #1/#2/#7/#9/#11 and
KILL-1: exec + output capture, stderr/exit-code demux, container-side timeout
(124), a hung exec makes communicate() RETURN (bounded) AND the workload is
reaped via session kill, cancellation reaps promptly, graph-snapshot write +
guarded removal, and container removal as the final backstop.

KILL-1 (in-container session kill) is verified here: kill() records the exec's
session id and reaps the whole session with `pkill -s`, so timeout/cancel now
terminate the actual workload rather than relying on container stop/removal.
"""

from __future__ import annotations

import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sharp.dispatcher.config import ContainerConfig
from sharp.dispatcher.runtime.containers import ContainerManager

IMAGE = "sharp-worker:latest"
PROJECT_ID = f"smoke-{uuid.uuid4().hex[:8]}"

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" :: {detail}" if detail else ""))
    if not ok:
        failures.append(name)


def run(cm, container, argv, *, timeout_seconds, communicate_timeout):
    proc = cm.build_exec_process(container, {}, argv, timeout_seconds=timeout_seconds)
    proc.start()
    started = time.perf_counter()
    result = proc.communicate(timeout=communicate_timeout)
    return result, time.perf_counter() - started, proc


def sh(cm, container, cmd, *, timeout=15):
    p = cm.build_exec_process(container, {}, ["sh", "-c", cmd], timeout_seconds=10)
    p.start()
    return p.communicate(timeout=timeout).stdout.strip()


def main() -> int:
    config = ContainerConfig(image=IMAGE, network_mode="bridge", completed_action="remove", cap_add=[])
    cm = ContainerManager(config)
    container = None
    try:
        # 1. ensure_running creates a container and is idempotent.
        container = cm.ensure_running(PROJECT_ID)
        check("ensure_running creates container", cm.inspect_state(container) == "running", container)
        check("ensure_running idempotent", cm.ensure_running(PROJECT_ID) == container)

        # 2. Normal exec: stdout captured, exit code 0.
        r, _, _ = run(cm, container, ["echo", "hello-sharp"], timeout_seconds=10, communicate_timeout=30)
        check("normal exec returncode 0", r.returncode == 0, f"rc={r.returncode}")
        check("normal exec stdout captured", "hello-sharp" in r.stdout, repr(r.stdout.strip()))

        # 3. stderr + non-zero exit code demuxed correctly.
        r, _, _ = run(cm, container, ["sh", "-c", "echo out; echo err 1>&2; exit 3"],
                      timeout_seconds=10, communicate_timeout=30)
        check("exit code propagated", r.returncode == 3, f"rc={r.returncode}")
        check("stdout/stderr demuxed", "out" in r.stdout and "err" in r.stderr,
              f"out={r.stdout.strip()!r} err={r.stderr.strip()!r}")

        # 4. Container-side `timeout` wrapper fires -> 124 (the dominant real
        #    timeout path; validates the did_timeout / #10 reasoning). This works
        #    because `timeout` kills within the container's own PID namespace.
        r, dt, _ = run(cm, container, ["sleep", "60"], timeout_seconds=2, communicate_timeout=30)
        check("container-side timeout returns 124/137", r.returncode in (124, 137), f"rc={r.returncode}")
        check("container-side timeout is prompt", dt < 15, f"{dt:.1f}s")

        # 5. Hung exec (no container-side timeout): communicate() times out and
        #    kill() reaps the exec's session -> returns bounded with timed_out and
        #    a non-zero (killed) code. Core of #1/#2/#9 + KILL-1.
        r, dt, _ = run(cm, container, ["sleep", "60"], timeout_seconds=None, communicate_timeout=3)
        check("hung exec communicate returns killed", r.timed_out is True and r.returncode != 0,
              f"timed_out={r.timed_out} rc={r.returncode}")
        check("hung exec return is bounded", dt < 12, f"{dt:.1f}s")

        # KILL-1: the workload itself is reaped by session kill (exact-match so we
        # don't count wrapper command lines; small settle for async SIGKILL).
        time.sleep(1.5)
        reaped = sh(cm, container, "pgrep -fx 'sleep 60' | wc -l")
        check("hung workload reaped by session kill", reaped == "0", f"exact 'sleep 60' count={reaped!r}")

        # 6. Cancellation reaps the live exec promptly (kill() is now effective),
        #    so communicate() returns well before its own timeout.
        proc = cm.build_exec_process(container, {}, ["sleep", "60"], timeout_seconds=None)
        proc.start()
        time.sleep(0.5)
        c_started = time.perf_counter()
        proc.cancel("smoke-cancel")
        r = proc.communicate(timeout=30)
        c_dt = time.perf_counter() - c_started
        check("cancellation flag observed", r.cancelled is True and r.cancel_reason == "smoke-cancel",
              f"cancelled={r.cancelled} reason={r.cancel_reason}")
        check("cancellation is prompt (kill effective)", c_dt < 10, f"{c_dt:.1f}s")
        time.sleep(1.5)
        c_reaped = sh(cm, container, "pgrep -fx 'sleep 60' | wc -l")
        check("cancelled workload reaped", c_reaped == "0", f"exact 'sleep 60' count={c_reaped!r}")

        # 7. Snapshot write + guarded removal (#7). Snapshots are root-owned
        #    (put_archive); removal must run as root.
        snap = "/tmp/sharp-prompts/smoke-abc123"
        cm.write_text_file(container, f"{snap}/graph.yaml", "origin: x\ngoal: y\n")
        check("snapshot written", "goal: y" in sh(cm, container, f"cat {snap}/graph.yaml"))
        cm.remove_path(container, snap, require_prefix="/tmp/sharp-prompts")
        check("snapshot removed (root)", sh(cm, container, f"test -e {snap}; echo $?") == "1")
        cm.remove_path(container, "/etc", require_prefix="/tmp/sharp-prompts")
        check("remove_path refuses out-of-prefix", sh(cm, container, "test -d /etc; echo $?") == "0")

        # 8. Container removal is the effective backstop: it removes the
        #    container (and therefore reaps any lingering exec processes).
        ok = cm.cleanup_completed(PROJECT_ID)
        check("cleanup removes container (reaps processes)", ok and cm.inspect_state(container) is None)
        container = None
    finally:
        if container is not None:
            cm.remove_container(container)
        cm.close()

    print("\n" + ("SMOKE OK — all guaranteed behaviors passed" if not failures else f"SMOKE FAILED: {failures}"))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
