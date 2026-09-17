from __future__ import annotations

import random

from sharp.dispatcher.config import WorkerConfig


def has_valid_api_key(worker: WorkerConfig) -> bool:
    """Check if worker has a valid (non-empty, non-placeholder) API key configured.

    Returns True if the worker has at least one valid API key in its environment.
    """
    # Key environment variables that indicate a worker is properly configured
    api_key_vars = {
        "claudecode": ["ANTHROPIC_AUTH_TOKEN"],
        "codex": ["OPENAI_API_KEY"],
        "pi": ["PI_API_KEY"],
        "mock": [],  # Mock workers don't need API keys
    }

    required_keys = api_key_vars.get(worker.type, [])

    # Mock workers are always valid
    if not required_keys:
        return True

    # Check if at least one required API key is set and not a placeholder
    for key in required_keys:
        value = worker.env.get(key, "").strip()
        if value and value.lower() not in ("", "placeholder", "your-api-key-here", "none"):
            return True

    return False


def choose_worker(candidates: list[WorkerConfig], running_counts: dict[str, int]) -> list[WorkerConfig]:
    # First filter out workers without valid API keys
    valid_candidates = [w for w in candidates if has_valid_api_key(w)]

    # Sort by priority, running count, and randomness
    grouped = sorted(
        valid_candidates,
        key=lambda worker: (
            worker.priority,
            running_counts.get(worker.name, 0),
            random.random(),
        ),
    )
    return grouped
