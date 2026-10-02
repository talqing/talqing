"""Fire-and-forget task spawning with strong references.

A bare `asyncio.create_task(coro)` returns a task the caller must hold onto — the
event loop keeps only a weak reference, so without a strong ref the task can be
garbage-collected mid-flight, silently dropping the work (webhooks, deferred
replies). `spawn()` keeps the ref until the task completes. (asyncio docs.)"""

from __future__ import annotations

import asyncio

_bg_tasks: set[asyncio.Task] = set()


def spawn(coro) -> asyncio.Task:
    """Schedule `coro` as a background task, retaining a strong ref until done."""
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)
    return task
