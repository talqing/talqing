"""Deferred work: a table that holds the schedule, and three moving parts.

    enqueue ──► scheduled_jobs ──► job-scheduler ──► Kafka ──► background-worker
    (any process)   (the schedule)   (what is due)              (claim + run)

Import the scheduling side from here::

    from services import jobs
    await jobs.enqueue(conn, tenant_id=..., kind=jobs.JobKind.SESSION_PURGE,
                       subject_id=session_id, scheduled_at=...)

The two long-running halves — `services.jobs.scheduler` and
`services.jobs.executor` — are imported directly by the processes that run them,
so nothing that merely schedules work pulls in a Kafka client.
"""

from __future__ import annotations

from .models import JobContext, JobKind, ScheduledJob  # noqa: F401
from .store import MAX_ATTEMPTS, enqueue, enqueue_once  # noqa: F401

__all__ = [
    "MAX_ATTEMPTS",
    "JobContext",
    "JobKind",
    "ScheduledJob",
    "enqueue",
    "enqueue_once",
]
