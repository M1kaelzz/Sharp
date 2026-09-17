"""Extra worker-mode edge tests: filtering happens pre-validation so an
inactive worker's missing env cannot block boot; raw dict list is accepted."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import yaml

from sharp.dispatcher import config as cfg_mod
from sharp.dispatcher.config import filter_workers_by_mode

CFG = {
    "server": "http://127.0.0.1:8000",
    "runtime": {
        "max_workers": 4, "max_running_projects": 2, "max_project_workers": 2,
        "interval": 3, "healthcheck_timeout": 10, "prompt_group": "default",
    },
    "tasks": {
        "bootstrap": {"timeout": 100, "conclude_timeout": 30},
        "reason": {"timeout": 100, "conclude_timeout": 30},
        "explore": {"timeout": 100, "conclude_timeout": 30},
    },
    "container": {"image": "img", "completed_action": "remove"},
    "workers": [
        # claude worker intentionally MISSING env keys (would fail validation)
        {"name": "claude_worker", "type": "claudecode",
         "task_types": ["bootstrap", "reason", "explore"], "max_running": 1, "priority": 0,
         "env": {}},
        {"name": "openai_worker", "type": "codex",
         "task_types": ["bootstrap", "reason", "explore"], "max_running": 1, "priority": 0,
         "env": {"CODEX_MODEL": "m", "CODEX_BASE_URL": "http://x", "OPENAI_API_KEY": "t"}},
    ],
}


def _load_with_mode(mode, monkeypatch, tmp_path):
    cfg_file = tmp_path / "dispatch.yaml"
    cfg_file.write_text(yaml.safe_dump(CFG), encoding="utf-8")
    monkeypatch.setattr(cfg_mod, "load_secrets_file", lambda *a, **k: None)
    monkeypatch.setattr(cfg_mod, "validate_prompt_resources", lambda *a, **k: None)
    if mode is None:
        monkeypatch.delenv("SHARP_WORKER_MODE", raising=False)
    else:
        monkeypatch.setenv("SHARP_WORKER_MODE", mode)
    return cfg_mod.DispatchConfig.load(cfg_file)


def test_openai_mode_boots_despite_claude_missing_env(tmp_path, monkeypatch):
    cfg = _load_with_mode("openai", monkeypatch, tmp_path)
    assert [w.type for w in cfg.workers] == ["codex"]


def test_no_mode_still_validates_all_and_fails(tmp_path, monkeypatch):
    # No mode → both workers kept → claude's missing env must fail validation.
    try:
        _load_with_mode(None, monkeypatch, tmp_path)
        raise AssertionError("expected validation failure without mode")
    except ValueError:
        pass


def test_filter_accepts_raw_dicts():
    out = filter_workers_by_mode(CFG["workers"], "anthropic")
    assert [w["type"] for w in out] == ["claudecode"]
