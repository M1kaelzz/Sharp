from __future__ import annotations

from dataclasses import dataclass

from sharp.dispatcher.runtime.cancellation import TaskCancellation


@dataclass(slots=True)
class RunningTask:
    project_id: str
    task_type: str
    worker_name: str
    cancellation: TaskCancellation
    start_time: float  # monotonic timestamp when task started (timeout math only)
    started_at_wall: float  # wall-clock epoch seconds when task started (trace archive display)
    intent_id: str | None = None
    fact_count: int | None = None
    hint_count: int | None = None
    open_intent_count: int | None = None


@dataclass(slots=True)
class ReasonCheckpoint:
    fact_count: int
    hint_count: int
    open_intent_count: int
