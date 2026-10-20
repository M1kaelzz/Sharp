"""run.sh contract tests (CyberStrikeAI-style thin facade).

Locks the shell facade the same way earlier batches guard dispatcher wiring:

- **syntax**: ``bash -n`` passes (fast parse-level gate for every future edit).
- **contract**: the facade stays a THIN SHELL — it must keep the pre-flight
  checks (python3 >= 3.12 gate, uv guidance, docker WARNING-only), the Run-Root
  resolution, the mirror default confined to the script run, and above all the
  final ``exec ./sharp "$@"`` handing ALL arguments through (including HA
  second-instance dispatch). It must NOT re-implement launcher internals
  (uv sync / image build / supervision stay in ./sharp).
- **mutant gate**: removing the exec handoff or the python version gate must
  fail the source guards.

Skipped automatically when bash is unavailable (Windows dev machines use
./sharp directly).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
RUN_SH = ROOT / "run.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or not RUN_SH.exists(),
    reason="bash or run.sh not available (Windows dev machine: use ./sharp)",
)

SRC = RUN_SH.read_text(encoding="utf-8")


def test_bash_syntax():
    result = subprocess.run(
        ["bash", "-n", str(RUN_SH)], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, f"bash -n failed: {result.stderr}"


def test_strict_mode_and_root_resolution():
    assert "set -euo pipefail" in SRC
    assert 'ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"' in SRC
    assert 'cd "$ROOT_DIR"' in SRC


def test_python_version_gate_present():
    # Hard gate: python3 missing or < 3.12 must exit non-zero (3.12 matches
    # runtime/pyproject.toml requires-python).
    assert "(3, 12)" in SRC
    assert "requires 3.12+" in SRC


def test_python_version_gate_matches_pyproject():
    pyproject = (ROOT / "runtime" / "pyproject.toml").read_text(encoding="utf-8")
    assert 'requires-python = ">=3.12"' in pyproject
    # The gate and the package requirement must not drift apart silently.
    assert "(3, 12)" in SRC and "3.12" in pyproject


def test_docker_check_is_warning_not_fatal():
    # docker CLI missing / daemon down => `return 0` (warning), never `exit 1`
    # inside check_docker: `web --no-dispatch` must stay usable for the first
    # configuration pass.
    start = SRC.index("check_docker()")
    end = SRC.index("\n}", start)
    body = SRC[start:end]
    assert "exit 1" not in body
    assert "return 0" in body


def test_uv_missing_is_fatal_with_guidance():
    start = SRC.index("check_uv()")
    end = SRC.index("\n}", start)
    body = SRC[start:end]
    assert "exit 1" in body  # without uv nothing works: hard gate
    assert "brew install uv" in body
    assert "astral.sh/uv/install.sh" in body


def test_mirror_default_confined_and_overridable():
    # Default Tsinghua mirror only for this run; an externally exported
    # UV_DEFAULT_INDEX always wins (no force-override of user config).
    assert "SHARP_UV_INDEX:-https://pypi.tuna.tsinghua.edu.cn/simple" in SRC
    assert 'UV_DEFAULT_INDEX="${UV_DEFAULT_INDEX:-$UV_INDEX_DEFAULT}"' in SRC


def test_exec_handoff_passes_all_args():
    # THE contract: the facade ends by exec-ing ./sharp with every argument
    # forwarded — args like `web --no-dispatch`, `doctor`, or an HA
    # second-instance `dispatch --config ...` must reach the launcher intact.
    assert 'exec "$LAUNCHER" "$@"' in SRC


def test_facade_does_not_reimplement_launcher_internals():
    # Thin shell only: no uv sync, no docker build, no port logic here — those
    # live in ./sharp (single source of truth, Windows keeps working).
    for forbidden in ("uv sync", "uv_sync", "docker build", "compose up"):
        assert forbidden not in SRC, f"facade must not contain {forbidden!r}"


def test_no_args_defaults_to_launcher_default_desktop():
    # The facade passes zero args straight through: ./sharp's default is the
    # desktop app, so ./run.sh stays consistent with it (zero surprise).
    assert 'main "$@"' in SRC
    # And main() itself does not fabricate a "web"/"app" argument.
    start = SRC.index("main() {")
    end = SRC.index("\n}", start)
    body = SRC[start:end]
    assert '"web"' not in body.replace('web --no-dispatch', "")  # only in guidance text
    assert "LAUNCHER\" app" not in body
    assert "LAUNCHER\" web" not in body


# ── mutant gate: source-level mutations must trip the guards ───────────────
# The guards read the module-level SRC snapshot, so mutants patch that
# snapshot directly (same style as the scheduler source guards in earlier
# batches, which also assert on a captured source string).

import test_run_sh as _self  # noqa: E402  (module alias for monkeypatching)


def test_mutant_removing_exec_trips_guard(monkeypatch):
    mutated = SRC.replace('exec "$LAUNCHER" "$@"', 'echo blocked')
    assert mutated != SRC, "mutation did not apply"
    monkeypatch.setattr(_self, "SRC", mutated)
    with pytest.raises(AssertionError):
        _self.test_exec_handoff_passes_all_args()


def test_mutant_removing_python_gate_trips_guard(monkeypatch):
    mutated = SRC.replace("(3, 12)", "(3, 0)").replace("requires 3.12+", "requires 3.0+")
    assert mutated != SRC, "mutation did not apply"
    monkeypatch.setattr(_self, "SRC", mutated)
    with pytest.raises(AssertionError):
        _self.test_python_version_gate_present()