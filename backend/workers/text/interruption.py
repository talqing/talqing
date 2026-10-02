"""Latest-wins interruption for one text turn on a LiveKit session."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from uuid import UUID

from livekit.agents import AgentSession

from services.messaging import TextTurnJob


class TextTurnInterruption:
    """Linearizes newer input with one session's commit boundary.

    ``on_supersede`` must publish the shared ``turn`` canceled event on the
    plane's SSE bus (see workers.text.events.publish_turn_status).
    """

    def __init__(
        self,
        job: TextTurnJob,
        *,
        on_supersede: Callable[[UUID], Awaitable[None]],
    ) -> None:
        self._job = job
        self._on_supersede = on_supersede
        self._lock = asyncio.Lock()
        self._session: AgentSession | None = None
        self._superseded_by_item_id: UUID | None = None
        self._sealed = False

    @property
    def superseded_by_item_id(self) -> UUID | None:
        return self._superseded_by_item_id

    async def bind(self, session: AgentSession) -> None:
        async with self._lock:
            self._session = session
            should_interrupt = self._superseded_by_item_id is not None and not self._sealed
        if should_interrupt:
            session.interrupt(force=True)

    async def request_supersede(self, newer_item_id: UUID) -> bool:
        async with self._lock:
            if self._sealed:
                return False
            self._superseded_by_item_id = newer_item_id
            session = self._session
        if session is not None:
            session.interrupt(force=True)
        await self._on_supersede(newer_item_id)
        return True

    async def seal(self) -> UUID | None:
        async with self._lock:
            self._sealed = True
            return self._superseded_by_item_id
