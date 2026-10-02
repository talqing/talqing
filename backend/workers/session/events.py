"""Writer for the durable `session_events` trace.

The vocabulary lives in `services/session_events.py`; this module only puts rows
in the table. `record()` is synchronous because every caller is a LiveKit
`EventEmitter` callback, which cannot await.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime

import db
from services.user import Tenant
from utils.bg import spawn

logger = logging.getLogger("talqing.workers.session.events")

_INSERT = """
    INSERT INTO session_events (session_id, seq, type, payload, created_at, tenant_id)
    VALUES ($1, $2, $3, $4::jsonb, $5, $6)
    ON CONFLICT (tenant_id, session_id, seq) DO NOTHING
"""


class SessionEventLog:
    """Append-only trace for one session, buffered and flushed off the hot path.

    `seq` comes from an in-process counter, not a row lock: exactly one worker
    process writes a session's events at a time (see the table comment in
    `0001_init.sql`).

    ``after_seq`` is where the counter starts. A call has one log for its whole
    life and starts at 0; a text chat is served by a new log every time its warm
    session is rebuilt, and each must continue from the last event the chat
    already has — the insert is `ON CONFLICT DO NOTHING`, so a second log
    counting from 0 would silently drop everything it recorded.
    """

    def __init__(self, tenant: Tenant, session_id: str, *, after_seq: int = 0) -> None:
        self._tenant = tenant
        self._session_id = session_id
        self._seq = after_seq
        self._pending: list[tuple[str, int, str, str, datetime, object]] = []
        self._flush_task: asyncio.Task[None] | None = None

    def record(
        self, type_: str, payload: dict[str, object] | None = None, *, at: datetime | None = None
    ) -> None:
        """Buffer one event and make sure a flush is on its way.

        Ordering is the caller's arrival order, which is what the timeline shows.

        ``at`` overrides the timestamp for the handful of events recorded later
        than the moment they describe — a consent withdrawal is discovered at
        finalize but happened mid-call, and a trace that says otherwise is worse
        than no trace. `seq` still reflects arrival order; only the clock moves.
        """
        self._seq += 1
        self._pending.append(
            (
                self._session_id,
                self._seq,
                type_,
                json.dumps(payload or {}, default=str),
                at or datetime.now(UTC),
                self._tenant.id,
            )
        )
        # Single-flight: `record` never awaits, so nothing can interleave between
        # the check and the spawn, and `_flush` re-checks the buffer before it
        # returns — anything appended mid-write goes out on the same task.
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = spawn(self._flush())

    async def drain(self) -> None:
        """Await anything still buffered. Call before finalize reads the trace."""
        if self._flush_task is not None:
            await asyncio.gather(self._flush_task, return_exceptions=True)
        if self._pending:
            await self._flush()

    async def _flush(self) -> None:
        while self._pending:
            batch, self._pending = self._pending, []
            try:
                pool = await db.tenant_pool(self._tenant)
                await pool.executemany(_INSERT, batch)
            except Exception:
                # A diagnostic trace must never take down a live call. The events
                # are dropped, not retried: re-queueing them would reorder the
                # timeline against whatever arrived while the write was failing.
                logger.warning(
                    "failed to write %d session events for %s",
                    len(batch),
                    self._session_id,
                    exc_info=True,
                )
