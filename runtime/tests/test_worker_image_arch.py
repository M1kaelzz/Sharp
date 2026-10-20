"""Arch-aware worker image tests (batch: auto arch detection).

Locks the launcher's platform logic that replaced the hardcoded
``--platform linux/amd64``:

- **normalize_arch**: maps uname/docker spellings to amd64/arm64.
- **resolve_build_platform**: the vendored binaries' arch (.arch marker) wins
  over the daemon arch (mixed-arch vendor is an exec-format-error landmine);
  without a marker the docker DAEMON arch decides (not uname — remote docker
  contexts may differ); a vendor≠daemon mismatch warns but still builds for
  the vendor arch (never silently deletes downloaded artifacts).
- **fetch_vendor_if_missing**: no-op when the marker exists (never re-downloads),
  runs fetch_vendor.sh with ARCH + forwarded proxy env when missing, returns
  False on script failure or unknown platform.
- **ensure_worker_image**: an existing image of the WRONG arch is rebuilt
  automatically (decision #1), a matching image is reused, arch-unknown passes
  through (no false rebuilds); build_worker_image passes --platform linux/<arch>
  and omits it only when nothing can be determined.
- **mutant gates**: reverting to hardcoded amd64, or dropping the arch-mismatch
  rebuild branch, must fail the guards.

The launcher is a single-file python script (no package); it is imported via
importlib from its path. subprocess calls are monkeypatched — nothing here
needs a docker daemon or network.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = ROOT / "sharp"

if not LAUNCHER.is_file():
    pytest.skip("launcher not found", allow_module_level=True)

_spec = importlib.util.spec_from_loader("sharp_launcher", loader=None)
launcher = types.ModuleType("sharp_launcher")
launcher.__dict__["__file__"] = str(LAUNCHER)
exec(compile(LAUNCHER.read_text(encoding="utf-8"), str(LAUNCHER), "exec"), launcher.__dict__)


# ── normalize_arch ─────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("amd64", "amd64"), ("x86_64", "amd64"), ("x64", "amd64"), ("AMD64", "amd64"),
        ("arm64", "arm64"), ("aarch64", "arm64"), ("ARM64", "arm64"),
        ("", None), (None, None), ("riscv64", None), ("  arm64 \n", "arm64"),
    ],
)
def test_normalize_arch(raw, expected):
    assert launcher.normalize_arch(raw) == expected


# ── resolve_build_platform ─────────────────────────────────────────────────

def test_vendor_arch_wins(monkeypatch):
    monkeypatch.setattr(launcher, "read_vendor_arch", lambda: "arm64")
    monkeypatch.setattr(launcher, "docker_daemon_arch", lambda: "amd64")
    assert launcher.resolve_build_platform() == "arm64"


def test_vendor_matches_daemon_no_warning(monkeypatch, capsys):
    monkeypatch.setattr(launcher, "read_vendor_arch", lambda: "arm64")
    monkeypatch.setattr(launcher, "docker_daemon_arch", lambda: "arm64")
    assert launcher.resolve_build_platform() == "arm64"
    assert "qemu" not in capsys.readouterr().out


def test_vendor_daemon_mismatch_warns_but_keeps_vendor(monkeypatch, capsys):
    monkeypatch.setattr(launcher, "read_vendor_arch", lambda: "amd64")
    monkeypatch.setattr(launcher, "docker_daemon_arch", lambda: "arm64")
    assert launcher.resolve_build_platform() == "amd64"  # vendor wins
    out = capsys.readouterr().out
    assert "qemu" in out
    assert "fetch_vendor" in out  # guidance for native speed included


def test_no_vendor_falls_back_to_daemon_arch(monkeypatch):
    monkeypatch.setattr(launcher, "read_vendor_arch", lambda: None)
    monkeypatch.setattr(launcher, "docker_daemon_arch", lambda: "arm64")
    assert launcher.resolve_build_platform() == "arm64"


def test_no_vendor_no_daemon_returns_none(monkeypatch):
    monkeypatch.setattr(launcher, "read_vendor_arch", lambda: None)
    monkeypatch.setattr(launcher, "docker_daemon_arch", lambda: None)
    assert launcher.resolve_build_platform() is None


def test_daemon_arch_uses_docker_version_not_uname(monkeypatch):
    """The DAEMON arch decides, not the local machine (remote docker context
    support). Proves the subprocess command is `docker version --format`."""
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return types.SimpleNamespace(returncode=0, stdout="aarch64\n")

    monkeypatch.setattr(launcher.subprocess, "run", fake_run)
    assert launcher.docker_daemon_arch() == "arm64"
    assert calls and calls[0][:3] == ["docker", "version", "--format"]


def test_daemon_arch_unavailable_returns_none(monkeypatch):
    def fake_run(cmd, **kw):
        return types.SimpleNamespace(returncode=1, stdout="")

    monkeypatch.setattr(launcher.subprocess, "run", fake_run)
    assert launcher.docker_daemon_arch() is None


# ── fetch_vendor_if_missing ────────────────────────────────────────────────

def test_fetch_vendor_noop_when_marker_exists(monkeypatch):
    monkeypatch.setattr(launcher, "read_vendor_arch", lambda: "arm64")
    # If it tried to run anything, the fake process would blow up the list.
    monkeypatch.setattr(
        launcher.subprocess, "run",
        lambda *a, **k: pytest.fail("must not spawn when vendor already present"),
    )
    assert launcher.fetch_vendor_if_missing("arm64") is True


def test_fetch_vendor_runs_script_with_arch(monkeypatch):
    ran = {}
    state = {"marker": False}

    def fake_read_vendor_arch():
        return "arm64" if state["marker"] else None

    monkeypatch.setattr(launcher, "read_vendor_arch", fake_read_vendor_arch)
    script = ROOT / "container" / "fetch_vendor.sh"
    assert script.is_file(), "repo layout changed: fetch_vendor.sh missing"

    def fake_run(cmd, **kw):
        ran["cmd"] = cmd
        ran["env"] = kw.get("env")
        ran["cwd"] = kw.get("cwd")
        state["marker"] = True  # simulate the script writing its .arch marker
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(launcher.subprocess, "run", fake_run)
    assert launcher.fetch_vendor_if_missing("arm64") is True
    assert "fetch_vendor.sh" in str(ran["cmd"])
    assert ran["env"]["ARCH"] == "arm64"
    assert isinstance(ran["cwd"], Path)  # runs from container/ dir


def test_fetch_vendor_failure_returns_false(monkeypatch):
    monkeypatch.setattr(launcher, "read_vendor_arch", lambda: None)
    monkeypatch.setattr(
        launcher.subprocess, "run",
        lambda cmd, **kw: types.SimpleNamespace(returncode=1),
    )
    # Marker still absent after the failed run -> not vendored -> False.
    assert launcher.fetch_vendor_if_missing("arm64") is False


def test_fetch_vendor_unknown_platform_returns_false(monkeypatch):
    monkeypatch.setattr(launcher, "read_vendor_arch", lambda: None)
    monkeypatch.setattr(
        launcher.subprocess, "run",
        lambda *a, **k: pytest.fail("must not spawn for unknown platform"),
    )
    assert launcher.fetch_vendor_if_missing(None) is False


# ── build_worker_image: --platform wiring ──────────────────────────────────

def test_build_passes_resolved_platform(monkeypatch):
    builds = []
    monkeypatch.setattr(launcher, "resolve_build_platform", lambda: "arm64")
    monkeypatch.setattr(launcher, "docker_run", lambda cmd: builds.append(cmd) or True)
    assert launcher.build_worker_image("sharp-worker:latest") is True
    assert "--platform" in builds[0]
    assert "linux/arm64" in builds[0]


def test_build_with_no_platform_omits_flag(monkeypatch):
    builds = []
    monkeypatch.setattr(launcher, "resolve_build_platform", lambda: None)
    monkeypatch.setattr(launcher, "docker_run", lambda cmd: builds.append(cmd) or True)
    assert launcher.build_worker_image("sharp-worker:latest") is True
    assert "--platform" not in builds[0]


def test_no_hardcoded_amd64_left():
    """Mutant gate: the old bug was `--platform linux/amd64` hardcoded. The
    source must contain the flag ONLY inside resolve/normalize logic, never
    as a literal build argument."""
    src = LAUNCHER.read_text(encoding="utf-8")
    assert '"--platform", "linux/amd64"' not in src
    assert "'--platform', 'linux/amd64'" not in src
    assert "resolve_build_platform" in src


# ── ensure_worker_image: wrong-arch rebuild (decision #1) ──────────────────

def test_existing_wrong_arch_image_rebuilt_automatically(monkeypatch, capsys):
    """Decision #1: mismatch => AUTO rebuild (loud log), not exit."""
    monkeypatch.setattr(launcher, "docker_available", lambda: True)
    monkeypatch.setattr(launcher, "parse_worker_image", lambda p: "sharp-worker:latest")
    monkeypatch.setattr(launcher, "resolve_build_platform", lambda: "arm64")
    monkeypatch.setattr(launcher, "image_exists", lambda i: True)
    monkeypatch.setattr(launcher, "image_arch", lambda i: "amd64")  # wrong arch
    monkeypatch.setattr(launcher, "fetch_vendor_if_missing", lambda p: True)
    rebuilt = {}
    monkeypatch.setattr(launcher, "build_worker_image", lambda i: rebuilt.setdefault("i", i) or True)
    launcher.ensure_worker_image(Path("dispatch.yaml"))
    assert rebuilt.get("i") == "sharp-worker:latest"
    out = capsys.readouterr().out
    assert "rebuilding automatically" in out


def test_existing_matching_image_reused_no_rebuild(monkeypatch):
    monkeypatch.setattr(launcher, "docker_available", lambda: True)
    monkeypatch.setattr(launcher, "parse_worker_image", lambda p: "sharp-worker:latest")
    monkeypatch.setattr(launcher, "resolve_build_platform", lambda: "arm64")
    monkeypatch.setattr(launcher, "image_exists", lambda i: True)
    monkeypatch.setattr(launcher, "image_arch", lambda i: "arm64")  # matches
    monkeypatch.setattr(
        launcher, "build_worker_image",
        lambda i: pytest.fail("matching image must not be rebuilt"),
    )
    launcher.ensure_worker_image(Path("dispatch.yaml"))  # returns quietly


def test_arch_unknown_on_either_side_no_rebuild(monkeypatch):
    """Old images (or odd daemons) may expose no arch — never false-rebuild."""
    monkeypatch.setattr(launcher, "docker_available", lambda: True)
    monkeypatch.setattr(launcher, "parse_worker_image", lambda p: "sharp-worker:latest")
    monkeypatch.setattr(launcher, "resolve_build_platform", lambda: None)
    monkeypatch.setattr(launcher, "image_exists", lambda i: True)
    monkeypatch.setattr(launcher, "image_arch", lambda i: None)
    monkeypatch.setattr(
        launcher, "build_worker_image",
        lambda i: pytest.fail("arch unknown: must reuse, not rebuild"),
    )
    launcher.ensure_worker_image(Path("dispatch.yaml"))


def test_image_missing_builds_after_vendor_fetch(monkeypatch, capsys):
    monkeypatch.setattr(launcher, "docker_available", lambda: True)
    monkeypatch.setattr(launcher, "parse_worker_image", lambda p: "sharp-worker:latest")
    monkeypatch.setattr(launcher, "resolve_build_platform", lambda: "arm64")
    monkeypatch.setattr(launcher, "image_exists", lambda i: False)
    fetched = {}
    monkeypatch.setattr(launcher, "fetch_vendor_if_missing", lambda p: fetched.setdefault("p", p) or True)
    monkeypatch.setattr(launcher, "build_worker_image", lambda i: True)
    launcher.ensure_worker_image(Path("dispatch.yaml"))
    assert fetched["p"] == "arm64"


def test_vendor_fetch_failure_continues_to_build(monkeypatch, capsys):
    """Fetch failure is advisory: continue with whatever vendor/ has (the
    build's own .arch guard remains the last line of defense)."""
    monkeypatch.setattr(launcher, "docker_available", lambda: True)
    monkeypatch.setattr(launcher, "parse_worker_image", lambda p: "sharp-worker:latest")
    monkeypatch.setattr(launcher, "resolve_build_platform", lambda: "arm64")
    monkeypatch.setattr(launcher, "image_exists", lambda i: False)
    monkeypatch.setattr(launcher, "fetch_vendor_if_missing", lambda p: False)
    built = []
    monkeypatch.setattr(launcher, "build_worker_image", lambda i: built.append(i) or True)
    launcher.ensure_worker_image(Path("dispatch.yaml"))
    assert built == ["sharp-worker:latest"]
    assert "vendor fetch failed" in capsys.readouterr().out


def test_build_failure_raises_actionable_system_exit(monkeypatch):
    monkeypatch.setattr(launcher, "docker_available", lambda: True)
    monkeypatch.setattr(launcher, "parse_worker_image", lambda p: "sharp-worker:latest")
    monkeypatch.setattr(launcher, "resolve_build_platform", lambda: None)
    monkeypatch.setattr(launcher, "image_exists", lambda i: False)
    monkeypatch.setattr(launcher, "fetch_vendor_if_missing", lambda p: False)
    monkeypatch.setattr(launcher, "build_worker_image", lambda i: False)
    with pytest.raises(SystemExit, match="docker save"):
        launcher.ensure_worker_image(Path("dispatch.yaml"))


def test_no_docker_raises(monkeypatch):
    monkeypatch.setattr(launcher, "docker_available", lambda: False)
    monkeypatch.setattr(launcher, "parse_worker_image", lambda p: "sharp-worker:latest")
    with pytest.raises(SystemExit, match="Docker daemon is not running"):
        launcher.ensure_worker_image(Path("dispatch.yaml"))


# ── mutant gate: dropping the rebuild branch must be caught ────────────────

def test_mutant_dropping_rebuild_branch_trips_guard(monkeypatch):
    """Differential mutant gate: replacing the arch-check block with an early
    `return` (the legacy behavior) must flip the outcome of the wrong-arch
    scenario — the real launcher rebuilds, the mutant does not. That flip is
    exactly what test_existing_wrong_arch_image_rebuilt_automatically asserts
    on, so removing the branch cannot pass the suite silently."""
    src = LAUNCHER.read_text(encoding="utf-8")
    idx = src.index("if image_exists(image):")
    end = src.index("if not fetch_vendor_if_missing(wanted):", idx)
    mutated = src[:idx] + "if image_exists(image):\n        return\n    " + src[end:]
    assert mutated != src, "mutation did not apply"

    m = types.ModuleType("sharp_launcher_mutant")
    m.__dict__["__file__"] = str(LAUNCHER)
    exec(compile(mutated, "mutant", "exec"), m.__dict__)

    state = {"rebuilt": []}

    def fake_build(image):
        state["rebuilt"].append(image)
        return True

    # Real launcher under the wrong-arch scenario: MUST rebuild.
    monkeypatch.setattr(launcher, "docker_available", lambda: True)
    monkeypatch.setattr(launcher, "parse_worker_image", lambda p: "sharp-worker:latest")
    monkeypatch.setattr(launcher, "resolve_build_platform", lambda: "arm64")
    monkeypatch.setattr(launcher, "image_exists", lambda i: True)
    monkeypatch.setattr(launcher, "image_arch", lambda i: "amd64")
    monkeypatch.setattr(launcher, "fetch_vendor_if_missing", lambda p: True)
    monkeypatch.setattr(launcher, "build_worker_image", fake_build)
    launcher.ensure_worker_image(Path("dispatch.yaml"))
    assert state["rebuilt"] == ["sharp-worker:latest"], "real launcher must rebuild on mismatch"
    monkeypatch.undo()

    # Mutant (rebuild branch removed) under identical conditions: returns
    # early — the regression the wrong-arch test detects and fails on.
    m.docker_available = lambda: True
    m.parse_worker_image = lambda p: "sharp-worker:latest"
    m.resolve_build_platform = lambda: "arm64"
    m.image_exists = lambda i: True
    m.image_arch = lambda i: "amd64"
    m.fetch_vendor_if_missing = lambda p: True
    m.build_worker_image = fake_build
    m.ensure_worker_image(Path("dispatch.yaml"))
    assert state["rebuilt"] == ["sharp-worker:latest"], (
        "mutant unexpectedly rebuilt; the guard would NOT catch this regression"
    )
    # Differential proof: real rebuilt exactly once; the mutant did NOT add a
    # second entry (early return). Identical conditions, flipped outcome —
    # the wrong-arch guard test fails if the branch is ever removed.
    assert state["rebuilt"] == ["sharp-worker:latest"]
