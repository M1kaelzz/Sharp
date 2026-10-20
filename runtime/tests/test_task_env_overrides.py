"""P2 task-type env overrides tests (batch: 11, reason/explore model split).

Locks three layers:

- **config**: ``runtime.task_env_overrides`` defaults to an empty dict (legacy
  behavior, bit-for-bit); keys are validated against the task types
  (reason/explore/bootstrap); each value must be an env mapping.
- **merge helper** (``apply_task_env_overrides``): no overrides / empty mapping
  for the task type returns the SAME worker object untouched; overrides return
  a copy whose env is {**worker.env, **overrides} with the SAME name (scheduling
  identity preserved).
- **wiring**: all three task entry points (reason/bootstrap/explore) call the
  helper before ``get_driver`` so every phase (healthcheck/execute/conclude/
  metering) runs with the overridden env.
- **mutant gate**: removing the entry-point wiring must fail the source guards.
"""

from __future__ import annotations

import inspect

import pytest

from sharp.dispatcher.config import DispatchConfig, RuntimeConfig, WorkerConfig
from sharp.dispatcher.tasks import bootstrap as bootstrap_mod
from sharp.dispatcher.tasks import common
from sharp.dispatcher.tasks import explore as explore_mod
from sharp.dispatcher.tasks import reason as reason_mod
from sharp.dispatcher.tasks.common import apply_task_env_overrides


def _runtime(**over) -> RuntimeConfig:
    base = dict(
        max_workers=8,
        max_running_projects=3,
        max_project_workers=4,
        interval=3,
        healthcheck_timeout=60,
        prompt_group="default",
    )
    base.update(over)
    return RuntimeConfig(**base)


def _worker(**over) -> WorkerConfig:
    base = dict(
        name="claude_worker",
        type="claudecode",
        task_types=["bootstrap", "reason", "explore"],
        max_running=3,
        priority=0,
        env={
            "ANTHROPIC_MODEL": "sonnet",
            "ANTHROPIC_BASE_URL": "https://api.anthropic.com",
            "ANTHROPIC_AUTH_TOKEN": "tok",
        },
    )
    base.update(over)
    return WorkerConfig(**base)


def _config(overrides: dict | None = None) -> DispatchConfig:
    return DispatchConfig(
        server="http://localhost:8000",
        runtime=_runtime(task_env_overrides=overrides or {}),
        tasks={
            "bootstrap": {"timeout": 400, "conclude_timeout": 180, "conclude_retry": False},
            "reason": {"timeout": 300, "max_intents": 4},
            "explore": {"timeout": 300, "conclude_timeout": 180, "conclude_retry": False},
        },
        container={"image": "sharp-worker:latest", "network_mode": "bridge", "completed_action": "stop"},
        workers=[_worker()],
    )


# ── 1. config ──────────────────────────────────────────────────────────────

def test_default_is_empty_dict_legacy():
    assert RuntimeConfig.model_fields["task_env_overrides"].default == {}
    cfg = _config()
    assert cfg.runtime.task_env_overrides == {}


def test_config_accepts_valid_keys():
    cfg = _config({"reason": {"ANTHROPIC_MODEL": "opus"}, "explore": {"ANTHROPIC_MODEL": "haiku"}})
    assert cfg.runtime.task_env_overrides["reason"]["ANTHROPIC_MODEL"] == "opus"


def test_config_rejects_unknown_task_type():
    with pytest.raises(Exception, match="task_env_overrides"):
        _config({"not_a_task": {"ANTHROPIC_MODEL": "x"}})


# ── 2. merge helper ────────────────────────────────────────────────────────

def test_no_overrides_returns_same_worker_object():
    cfg = _config()
    worker = _worker()
    assert apply_task_env_overrides(cfg, "reason", worker) is worker


def test_other_task_type_untouched():
    cfg = _config({"reason": {"ANTHROPIC_MODEL": "opus"}})
    worker = _worker()
    assert apply_task_env_overrides(cfg, "explore", worker) is worker


def test_override_merges_env_and_keeps_name():
    cfg = _config({"reason": {"ANTHROPIC_MODEL": "opus"}})
    worker = _worker()
    merged = apply_task_env_overrides(cfg, "reason", worker)
    assert merged is not worker
    # Same scheduling identity.
    assert merged.name == worker.name
    assert merged.type == worker.type
    assert merged.task_types == worker.task_types
    assert merged.max_running == worker.max_running
    # Env merged: override wins, other keys preserved.
    assert merged.env["ANTHROPIC_MODEL"] == "opus"
    assert merged.env["ANTHROPIC_BASE_URL"] == worker.env["ANTHROPIC_BASE_URL"]
    assert merged.env["ANTHROPIC_AUTH_TOKEN"] == "tok"
    # Original worker untouched (no in-place mutation).
    assert worker.env["ANTHROPIC_MODEL"] == "sonnet"


def test_empty_mapping_for_task_type_is_legacy():
    cfg = _config({"reason": {}})
    worker = _worker()
    assert apply_task_env_overrides(cfg, "reason", worker) is worker


def test_multiple_vars_merged():
    cfg = _config({"explore": {"ANTHROPIC_MODEL": "haiku", "EXTRA_VAR": "1"}})
    merged = apply_task_env_overrides(cfg, "explore", _worker())
    assert merged.env["ANTHROPIC_MODEL"] == "haiku"
    assert merged.env["EXTRA_VAR"] == "1"


# ── 3. entry-point wiring (source guards) ──────────────────────────────────

def test_all_entry_points_wired_before_driver():
    for module, task_type in (
        (reason_mod, "reason"),
        (bootstrap_mod, "bootstrap"),
        (explore_mod, "explore"),
    ):
        src = inspect.getsource(module)
        assert f'apply_task_env_overrides(config, "{task_type}", worker)' in src, (
            f"{task_type} entry point missing env-override wiring"
        )
        # Wiring must come before any driver construction.
        run_src = inspect.getsource(
            reason_mod.run_reason_task
            if task_type == "reason"
            else bootstrap_mod.run_bootstrap_task
            if task_type == "bootstrap"
            else explore_mod.run_explore_task
        )
        assert run_src.index("apply_task_env_overrides") < run_src.index("get_driver"), (
            f"{task_type}: overrides must merge before get_driver (healthcheck env)"
        )


def test_helper_imported_in_all_task_modules():
    for module in (reason_mod, bootstrap_mod, explore_mod):
        assert "apply_task_env_overrides" in dir(module) or any(
            name == "apply_task_env_overrides" for name in vars(module)
        ), module.__name__


# ── 4. dispatch.yaml documents the option commented-out ───────────────────

def test_dispatch_yaml_documents_option():
    from pathlib import Path

    from sharp.dispatcher.config import DispatchConfig

    root = Path(__file__).resolve().parents[2]
    text = (root / "dispatch.yaml").read_text(encoding="utf-8")
    # Documented but commented out by default (legacy off).
    assert "task_env_overrides:" in text
    assert "reason:" in text and "explore:" in text

    # The bare-shipped config parses with no overrides active. Secrets/env
    # expansion happens at load; tokens may be empty in test environments so
    # drive the schema directly instead of DispatchConfig.load.
    import yaml as _yaml

    data = _yaml.safe_load(text)
    assert data["runtime"].get("task_env_overrides", {}) in (None, {})