"""Per-conversation actor for all text planes (tenant, CoPilots).

Owns the conversation's warm LiveKit window and serializes its work: turns are
latest-wins (a newer message interrupts the one being answered), and the jobs
that END a tenant chat are never dropped — each runs after the turn in flight.

The window is an in-memory convenience. It is built when a message finds none,
kept for TEXT_SESSION_IDLE_TIMEOUT_SECONDS after the last turn, and closed on
that timeout, on a republished agent, or on worker shutdown — none of which
ends the chat it was serving. Only `end_call` and an `end` job do that.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import deque
from uuid import UUID

from services.messaging import TextTurnJob
from services.user import Tenant
from workers.text.end import end_chat
from workers.text.events import publish_turn_status
from workers.text.executor import execute_text_job
from workers.text.interruption import TextTurnInterruption
from workers.text.planes import (
    TEXT_SESSION_IDLE_TIMEOUT_SECONDS,
    TEXT_SESSION_SHUTDOWN_FINALIZE_SECONDS,
)
from workers.text.types import TextWindow
from workers.text.window import close_window

logger = logging.getLogger("talqing.workers.text.actor")


def _interruption_for(tenant: Tenant, job: TextTurnJob) -> TextTurnInterruption:
    """Build interruption with plane-aware supersede notify (shared ``turn`` event)."""

    async def on_supersede(newer_item_id: UUID) -> None:
        await publish_turn_status(
            tenant,
            job,
            status="canceled",
            superseded_by_item_id=newer_item_id,
        )

    return TextTurnInterruption(job, on_supersede=on_supersede)


class ConversationActor:
    def __init__(self, tenant: Tenant, conversation_id: UUID) -> None:
        self.tenant = tenant
        self.conversation_id = conversation_id
        self._lock = asyncio.Lock()
        # The newest message nobody has started answering. One slot: latest wins.
        self._pending: TextTurnJob | None = None
        # Chats to end, in arrival order. Never superseded.
        self._ends: deque[TextTurnJob] = deque()
        self._current: TextTurnJob | None = None
        self._interruption: TextTurnInterruption | None = None
        self._runner: asyncio.Task[None] | None = None
        self._window: TextWindow | None = None
        self._idle_task: asyncio.Task[None] | None = None

    @property
    def idle(self) -> bool:
        """True when no work and no warm session (safe to drop from the actor map)."""
        return (
            self._pending is None
            and not self._ends
            and self._current is None
            and (self._runner is None or self._runner.done())
            and self._window is None
            and (self._idle_task is None or self._idle_task.done())
        )

    def _cancel_idle_timer(self) -> None:
        """Cancel any in-flight idle close (e.g. a new message just arrived)."""
        task = self._idle_task
        self._idle_task = None
        if task is not None and not task.done():
            task.cancel()

    def _schedule_idle_close(self) -> None:
        """Start the idle no-message timer; only called when the runner is empty."""
        self._cancel_idle_timer()
        if self._window is None:
            return

        async def _idle() -> None:
            try:
                await asyncio.sleep(TEXT_SESSION_IDLE_TIMEOUT_SECONDS)
                async with self._lock:
                    # A new message cancelled us, or work arrived in the race
                    # between sleep end and lock acquisition — keep the window.
                    if (
                        self._pending is not None
                        or self._ends
                        or self._current is not None
                        or self._window is None
                    ):
                        return
                    parked = self._window
                    self._window = None
                    self._idle_task = None
                logger.info(
                    "text window idle close conversation=%s session=%s after %ss",
                    self.conversation_id,
                    parked.session_id,
                    TEXT_SESSION_IDLE_TIMEOUT_SECONDS,
                )
                await close_window(parked)
            except asyncio.CancelledError:
                # New message or actor shutdown — leave the warm window alone.
                return

        self._idle_task = asyncio.create_task(_idle(), name=f"text_idle_{self.conversation_id}")

    async def submit(self, job: TextTurnJob) -> None:
        """Enqueue a job. A turn takes the single pending slot; an end job queues."""
        interruption: TextTurnInterruption | None = None
        dropped: TextTurnJob | None = None
        async with self._lock:
            if job.kind == "end":
                self._ends.append(job)
                # A message for the chat that is ending, not yet started, will
                # never be answered.
                if self._pending is not None and self._pending.session_id == job.session_id:
                    dropped, self._pending = self._pending, None
            else:
                # The API re-publishes a turn it is not sure was queued. If it
                # was, this is the same turn again — not a newer one to
                # supersede it with.
                waiting = (self._current, self._pending)
                if any(j is not None and j.input_item_id == job.input_item_id for j in waiting):
                    return
                dropped, self._pending = self._pending, job
                interruption = self._interruption
            # New work: cancel the idle countdown so the warm window stays open.
            self._cancel_idle_timer()
            if self._runner is None or self._runner.done():
                self._runner = asyncio.create_task(
                    self._run(), name=f"text_conversation_{self.conversation_id}"
                )

        if dropped is not None:
            # It never ran, so nothing else will say it stopped — and a turn
            # left `running` keeps its sender waiting.
            await publish_turn_status(
                self.tenant,
                dropped,
                status="canceled",
                superseded_by_item_id=job.input_item_id,
            )
        if interruption is not None:
            await interruption.request_supersede(job.input_item_id)

    def _adopt_window(self, opened: TextWindow) -> None:
        """Take ownership of a cold window the moment it exists.

        Not deferred to the turn's outcome: a shutdown cancels the turn mid-run,
        no outcome is ever returned, and `aclose()` would then have no reference
        to the window whose usage it has to flush. Unlocked because it runs on
        the runner task, between the two places the lock protects.
        """
        self._window = opened

    async def _run(self) -> None:
        while True:
            async with self._lock:
                # Ends first. One is only ever queued behind the turn in flight:
                # a pending message for the same chat was dropped when the end
                # arrived, and one for a newer chat was published after it.
                job = self._ends.popleft() if self._ends else self._pending
                if job is None:
                    self._current = None
                    self._interruption = None
                    self._runner = None
                    # No more work — start the idle timer on the parked window.
                    if self._window is not None:
                        self._schedule_idle_close()
                    return
                if job.kind == "turn":
                    self._pending = None
                self._current = job
                interruption = _interruption_for(self.tenant, job) if job.kind == "turn" else None
                self._interruption = interruption
                window = self._window

            try:
                if interruption is None:
                    await self._end(job, window, close_reason="api_end")
                    continue
                outcome = await execute_text_job(
                    self.tenant,
                    job,
                    interruption,
                    window=window,
                    on_window=self._adopt_window,
                )
                self._window = outcome.window
                if outcome.end_chat:
                    await self._end(job, outcome.window, close_reason="end_call")
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("text job %s failed", job.input_item_id or job.session_id)
                # Leave the warm window as execute left it (may still be reusable).
            finally:
                async with self._lock:
                    if self._current is job:
                        self._current = None
                        self._interruption = None

    async def _end(self, job: TextTurnJob, window: TextWindow | None, *, close_reason: str) -> None:
        """End the chat ``job`` names, taking its warm window with it if it has one."""
        mine = window if window is not None and window.session_id == str(job.session_id) else None
        if mine is not None:
            self._window = None
        logger.info(
            "text chat end conversation=%s session=%s", self.conversation_id, job.session_id
        )
        await end_chat(self.tenant, job, mine, close_reason=close_reason)

    async def aclose(self) -> None:
        self._cancel_idle_timer()
        runner = self._runner
        if runner is not None:
            # Cancel rather than await: a text turn has no deadline at all (see
            # turn.py's "No turn deadline" note), so waiting means the container
            # is SIGKILLed with nothing flushed. The suppress is what keeps the
            # rest of this method reachable — without it the runner's
            # `CancelledError` would propagate out of `aclose()`.
            runner.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await runner
        async with self._lock:
            parked, self._window = self._window, None
            pending, self._pending = self._pending, None
        if pending is not None:
            # The text topic is `latest` with auto-commit, so a message still
            # waiting here is gone. Said plainly, so whoever is waiting on it
            # hears back now instead of timing out.
            with contextlib.suppress(Exception):
                await publish_turn_status(
                    self.tenant,
                    pending,
                    status="error",
                    error=("the worker restarted before this message was answered — send it again"),
                )
        if self._ends:
            logger.warning(
                "worker shutdown with %d chat end(s) not run on conversation %s",
                len(self._ends),
                self.conversation_id,
            )
        if parked is None:
            return
        try:
            # Bounded: this writes the window's usage and closes a live LiveKit
            # session, and a wedged close must not hold the container open for
            # the whole grace period. On timeout the window's usage is lost —
            # the chat it served is untouched — and the deploy still finishes.
            await asyncio.wait_for(close_window(parked), TEXT_SESSION_SHUTDOWN_FINALIZE_SECONDS)
        except TimeoutError:
            logger.warning(
                "text window for %s did not close within %ss; its usage is lost",
                parked.session_id,
                TEXT_SESSION_SHUTDOWN_FINALIZE_SECONDS,
            )
