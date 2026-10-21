"""Launcher dispatcher auto-respawn (P2, 2026-10-21, SHARP_DISPATCH_AUTO_RESPAWN).

背景：``POST /dispatcher/restart`` 刻意只"停止"不代生——server 派生的 dispatcher
是启动器监管外的孤儿，下次 ./sharp 会叠出双 dispatcher。但"谁来重启"的正确答案
本来就是启动器（进程的 owner），原 supervise 却只记录副进程退出不重拉——结果：
设置页「重启 Dispatcher」名不副实（真停假重启），批12 NameError 那种崩溃也需要
整场 ./run.sh 重启才能恢复。

本开关（默认 OFF=逐字节旧行为）让启动器自动重拉：
- 退出即重拉，但 &lt;10s 存活的算 quick fail，按 1s/2s/5s/10s/30s 退避；
- 连续 5 次 quick fail 放弃（坏配置 / 全 401 的 worker 队伍不能变成重启风暴），
  回落到与旧行为一致；
- 主进程（window/server）在退避睡眠中死掉则不再复活副进程；
- 存活 ≥10s 的退出重置计数（正常重启不受惩罚）。

测试策略：直接加载无扩展名的 ``sharp`` 启动器为模块（SourceFileLoader），用
脚本化 poll() 的假主/副进程 + 可控时钟驱动 supervise 全循环——确定性、单线程、
毫秒级。端点消息的 env 感知在 test_ha_standby.py 的 restart 组覆盖。
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[2]
SHARP = ROOT / "sharp"

pytestmark = pytest.mark.skipif(not SHARP.exists(), reason="sharp launcher missing")


def _load_launcher():
    spec = importlib.util.spec_from_file_location(
        "sharp_launcher_under_test", SHARP,
        loader=importlib.machinery.SourceFileLoader("sharp_launcher_under_test", str(SHARP)),
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["sharp_launcher_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


class FakeClock:
    """Deterministic clock: sleep(d) records the delay and advances time."""

    def __init__(self):
        self.now = 1000.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class FakeProc:
    """Scripted subprocess stand-in: poll() returns values from a script.

    An optional ``on_poll`` hook lets a test advance the fake clock (to
    simulate a long-lived secondary) before reporting death.
    """

    def __init__(self, script: list[int | None], on_poll=None):
        self.script = list(script)
        self.on_poll = on_poll
        self.terminated = False

    def poll(self):
        if self.on_poll and self.script and self.script[0] is not None:
            self.on_poll()
        value = self.script.pop(0) if self.script else (0 if self.terminated else None)
        self.terminated = self.terminated or value is not None
        return value

    def wait(self, timeout=None):  # terminate_process fallback path
        return 0

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.terminated = True


def _run_supervise(launcher, primary, secondary, *, respawn_factory=None, env_on=False, clock=None):
    with mock.patch.dict(launcher.os.environ, {}, clear=False):
        if env_on:
            launcher.os.environ["SHARP_DISPATCH_AUTO_RESPAWN"] = "1"
        else:
            launcher.os.environ.pop("SHARP_DISPATCH_AUTO_RESPAWN", None)
        # The module instance under test is freshly exec'd per call and thrown
        # away — replacing terminate_process keeps atexit teardown (which fires
        # long after the test restored other patches) safe on FakeProc objects.
        launcher.terminate_process = lambda proc, name: (
            setattr(proc, "terminated", True)
            if proc is not None and not getattr(proc, "terminated", False)
            else None
        )
        # Patch the module's time for deterministic backoff & lifetimes.
        real_time = launcher.time
        fake_time = clock or FakeClock()
        launcher.time = fake_time
        try:
            rc = launcher.supervise(
                primary, secondary,
                primary_name="primary", secondary_name="dispatcher",
                respawn_factory=respawn_factory,
            )
        finally:
            launcher.time = real_time
    return rc


# ── 1. default OFF = legacy: no respawn, old message ───────────────────────

def test_off_never_respawns():
    launcher = _load_launcher()
    primary = FakeProc([None, None, 7])          # dies on 3rd poll
    secondary = FakeProc([1])                    # dead from the start
    calls = []

    rc = _run_supervise(launcher, primary, secondary,
                        respawn_factory=lambda: calls.append("must-not-happen"))

    assert calls == [], "flag off: respawn_factory must never be called"
    # Legacy semantics: when the secondary died first, supervise reports ITS
    # status as the launcher exit code (existing behavior, preserved).
    assert rc == 1
    assert secondary.terminated


def test_off_without_factory_and_without_env():
    """No env flag: even a provided factory is inert (= default bundle)."""
    launcher = _load_launcher()
    primary = FakeProc([None, 3])
    secondary = FakeProc([None, 1])
    calls = []

    rc = _run_supervise(launcher, primary, secondary,
                        respawn_factory=lambda: calls.append("x") or FakeProc([None]))

    assert calls == []
    assert rc == 3


# ── 2. ON: quick-fail backoff then give-up (storm protection) ──────────────

def test_on_quick_fail_backoff_then_gives_up():
    launcher = _load_launcher()
    clock = FakeClock()
    spawns: list[FakeProc] = []
    # Primary stays alive long enough for the whole give-up sequence, then dies.
    # 4 loop ticks + 5 (respawn branches) polls before give-up + final ticks.
    primary = FakeProc([None] * 40 + [9])
    secondary = FakeProc([1])  # initial instance: instant death

    def factory():
        proc = FakeProc([1])  # every respawn dies instantly (quick fail)
        spawns.append(proc)
        return proc

    rc = _run_supervise(launcher, primary, secondary,
                        respawn_factory=factory, env_on=True, clock=clock)

    assert len(spawns) == 4, "5 quick failures total (1 initial + 4 respawns) then give up"
    # Backoff sleeps observed on the fake clock: 1, 2, 5, 10 (the 5th failure
    # gives up instead of sleeping 30).
    backoffs = [s for s in clock.sleeps if s >= 1.0]
    assert backoffs[:4] == [1.0, 2.0, 5.0, 10.0], clock.sleeps
    assert 30.0 not in backoffs or len(backoffs) > 4
    # Give-up path records the dispatcher's death as launcher exit (legacy).
    assert rc == 1


def test_on_surviving_instance_resets_counter(capsys):
    """A respawn that lives >= QUICK_FAIL_SECS (10s) resets the quick-fail
    counter — normal restarts don't accumulate toward storm protection."""
    launcher = _load_launcher()
    clock = FakeClock()

    def long_lived_then_die():
        clock.now += 600.0  # simulate a 10min lifespan before reporting death

    # Initial secondary: one alive tick, then reports death AFTER the hook
    # stretched the clock (lived 600s -> reset). Three instant-death respawns
    # (quick_fails 1..3, below the 5 storm threshold) before the final
    # respawn, which simply stays alive until the primary exits.
    secondary = FakeProc([None, 0], on_poll=long_lived_then_die)
    quickies = [FakeProc([1]) for _ in range(3)]
    final = FakeProc([None] * 200)  # never dies within the test run
    spawns = quickies + [final]
    made = []
    primary = FakeProc([None] * 60 + [5])

    rc = _run_supervise(
        launcher, primary, secondary,
        respawn_factory=lambda: made.append(spawns[len(made)]) or spawns[len(made) - 1],
        env_on=True, clock=clock,
    )

    assert len(made) == 4, "3 quick respawns + 1 long-lived replacement"
    assert rc == 5
    assert "gave up" not in capsys.readouterr().err, (
        "long-lived instances must reset the storm counter (never reached 5)"
    )


def test_on_primary_death_during_backoff_skips_respawn(capsys):
    launcher = _load_launcher()
    clock = FakeClock()
    calls = []
    # Primary is alive for its first poll (supervise loop start), then dies —
    # while the respawn branch is sleeping in backoff.
    primary = FakeProc([None] + [4] * 50)
    secondary = FakeProc([1])

    rc = _run_supervise(launcher, primary, secondary,
                        respawn_factory=lambda: calls.append("no") or FakeProc([None]),
                        env_on=True, clock=clock)

    assert calls == [], "must not respawn under a dead primary"
    assert rc == 4
    # After give-up/quit, remaining behavior is identical to legacy: secondary
    # (dead) needs no teardown; message printed to stderr.


# ── 3. source guards ────────────────────────────────────────────────────────

def test_source_flags_and_defaults():
    src = SHARP.read_text(encoding="utf-8")
    assert "SHARP_DISPATCH_AUTO_RESPAWN" in src, "env flag must stay documented in code"
    assert "respawn_factory: \"Callable[[], subprocess.Popen[bytes]] | None\" = None" in src or \
           "respawn_factory=None" in src, "factory parameter defaults to None (legacy)"
    # Both run paths wire the factory (web even under --no-dispatch: supervise
    # only calls it when a secondary exists).
    assert src.count("respawn_factory=lambda: start_managed(") == 2, (
        "run_app and run_web must both pass a respawn factory"
    )
    # Version bumped for this batch.
    assert "LAUNCHER_VERSION = \"2026-10-21-dispatcher-auto-respawn\"" in src
