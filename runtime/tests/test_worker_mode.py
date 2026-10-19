"""Worker-mode activation tests: SHARP_WORKER_MODE filters dispatch.yaml workers
so the settings page's provider choice really activates one worker type."""

from __future__ import annotations

from sharp.dispatcher.config import WorkerConfig, filter_workers_by_mode

_COMMON = dict(task_types=["bootstrap", "reason", "explore"], max_running=2, priority=0)


def _workers():
    return [
        WorkerConfig(
            name="claude_worker", type="claudecode",
            env={"ANTHROPIC_MODEL": "m", "ANTHROPIC_BASE_URL": "http://x", "ANTHROPIC_AUTH_TOKEN": "t"}, **_COMMON,
        ),
        WorkerConfig(
            name="openai_worker", type="codex",
            env={"CODEX_MODEL": "m", "CODEX_BASE_URL": "http://x", "OPENAI_API_KEY": "t"}, **_COMMON,
        ),
    ]


def test_no_mode_keeps_all():
    out = filter_workers_by_mode(_workers(), None)
    assert [w.type for w in out] == ["claudecode", "codex"]


def test_all_mode_keeps_all():
    assert len(filter_workers_by_mode(_workers(), "all")) == 2


def test_anthropic_keeps_only_claudecode():
    out = filter_workers_by_mode(_workers(), "anthropic")
    assert [w.type for w in out] == ["claudecode"]


def test_openai_keeps_only_codex():
    out = filter_workers_by_mode(_workers(), "openai")
    assert [w.type for w in out] == ["codex"]


def test_unknown_mode_keeps_all():
    assert len(filter_workers_by_mode(_workers(), "whatever")) == 2
