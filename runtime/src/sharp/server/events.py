"""Lightweight in-process pub/sub for project-level SSE events.

Whenever the server writes a meaningful state change (new fact, intent status
update, project completion), it calls publish().  SSE clients call subscribe()
which returns an async generator that yields SSE-formatted lines as long as the
HTTP connection is open.

No external queue / broker required — everything lives in asyncio memory.
On server restart all subscribers are lost (expected: clients reconnect).
"""
from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from typing import AsyncIterator

# project_id -> list[Queue[str | None]]
# None is the sentinel that tells a subscriber to stop.
_subs: dict[str, list[asyncio.Queue]] = defaultdict(list)
_QUEUE_MAX = 128        # drop events if a slow client falls this far behind
_HEARTBEAT_SECS = 15.0  # keep-alive comment line frequency

# Event loop captured at server startup so sync routes (which run in a
# threadpool) can safely schedule queue writes via call_soon_threadsafe().
_loop: asyncio.AbstractEventLoop | None = None


def set_event_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _loop
    _loop = loop


def _put_nowait_safe(q: asyncio.Queue, item: str | None) -> None:
    """Put an item into the queue in a thread-safe way.

    asyncio.Queue.put_nowait() is NOT thread-safe because its internal
    _wakeup_next() calls Future.set_result() which must run on the event loop
    thread. Sync FastAPI routes execute in a threadpool, so we must schedule
    the put via call_soon_threadsafe when called from a worker thread.
    """
    if _loop is not None and not _loop.is_closed():
        _loop.call_soon_threadsafe(_put_nowait_direct, q, item)
    else:
        # Fallback: best-effort direct call (e.g. during tests without a loop)
        try:
            q.put_nowait(item)
        except asyncio.QueueFull:
            pass


def _put_nowait_direct(q: asyncio.Queue, item: str | None) -> None:
    """Must be called from within the event loop thread."""
    try:
        q.put_nowait(item)
    except asyncio.QueueFull:
        pass  # slow client — drop rather than block


def publish(project_id: str, event_type: str, data: dict | None = None) -> None:
    """Fire-and-forget: push one event to every active subscriber for a project."""
    payload = json.dumps({"type": event_type, **(data or {})}, ensure_ascii=False)
    for q in list(_subs.get(project_id, [])):
        _put_nowait_safe(q, payload)


async def subscribe(project_id: str) -> AsyncIterator[str]:
    """Yield raw SSE text lines for the given project until the client disconnects."""
    q: asyncio.Queue[str | None] = asyncio.Queue(maxsize=_QUEUE_MAX)
    _subs[project_id].append(q)
    try:
        while True:
            try:
                item = await asyncio.wait_for(q.get(), timeout=_HEARTBEAT_SECS)
            except asyncio.TimeoutError:
                # Send a keep-alive comment so the connection stays open
                yield ": heartbeat\n\n"
                continue
            if item is None:   # sentinel — caller asked us to stop
                break
            yield f"data: {item}\n\n"
    except GeneratorExit:
        pass
    finally:
        try:
            _subs[project_id].remove(q)
        except ValueError:
            pass


def close_project(project_id: str) -> None:
    """Push the stop sentinel to all subscribers for a project (e.g. on completion)."""
    for q in list(_subs.get(project_id, [])):
        _put_nowait_safe(q, None)
