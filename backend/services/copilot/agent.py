"""The code-defined LiveKit Agent every CoPilot runs as.

One class for all of them: what differs — the framing, the live state, the tool
surface — arrives as a ``CopilotSubject``.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterable, Coroutine
from typing import Any
from uuid import UUID

from livekit.agents import Agent, llm
from livekit.agents.types import NOT_GIVEN, NotGivenOr
from livekit.agents.voice.agent import ModelSettings

from services.user import Context

from .const import COPILOT_MAX_STEPS
from .subjects import CopilotSubject

logger = logging.getLogger("talqing.copilot.agent")

_STEP_LIMIT_TEXT = (
    "I hit my step limit for one turn — tell me to continue and I'll pick up where I left off."
)

# The state block is re-read before EVERY model call, so after a successful write
# it already contains that write. Without this, a CoPilot reads back its own edit,
# sees the state the user asked for, and reports that nothing needed doing.
_FRESHNESS_NOTE = (
    "This state was read from the database just now, AFTER every tool call you have "
    "already made this turn — so a change you just wrote is already reflected here. "
    "Seeing what the user asked for means your write landed; it never means the work "
    "was unnecessary. Report what you changed, not what the state looks like now.\n\n"
)


class CopilotAgent(Agent):
    """Platform builder agent: a fixed tool surface + live subject state inject."""

    def __init__(
        self,
        *,
        subject: CopilotSubject,
        subject_id: UUID,
        ctx: Context,
        tools: list[llm.Tool | llm.Toolset],
        chat_ctx: NotGivenOr[llm.ChatContext | None] = NOT_GIVEN,
    ) -> None:
        super().__init__(
            instructions=subject.instructions(),
            tools=tools,
            chat_ctx=chat_ctx,
        )
        self._subject = subject
        self._subject_id = subject_id
        self._ctx = ctx
        self._llm_calls = 0

    def llm_node(
        self,
        chat_ctx: llm.ChatContext,
        tools: list[llm.Tool],
        model_settings: ModelSettings,
    ) -> (
        AsyncIterable[llm.ChatChunk | str | Any]
        | Coroutine[Any, Any, AsyncIterable[llm.ChatChunk | str | Any]]
        | Coroutine[Any, Any, str]
        | Coroutine[Any, Any, llm.ChatChunk]
        | Coroutine[Any, Any, None]
    ):
        self._llm_calls += 1
        if self._llm_calls > COPILOT_MAX_STEPS:

            async def _limited() -> AsyncIterable[str]:
                yield _STEP_LIMIT_TEXT

            return _limited()

        return self._llm_node_with_state(chat_ctx, tools, model_settings)

    async def _llm_node_with_state(
        self,
        chat_ctx: llm.ChatContext,
        tools: list[llm.Tool],
        model_settings: ModelSettings,
    ) -> AsyncIterable[llm.ChatChunk | str | Any]:
        try:
            state_block = await self._subject.state_block(self._ctx, self._subject_id)
        except Exception:
            logger.exception(
                "%s state block failed; continuing without refresh", self._subject.label
            )
            state_block = ""

        # Ephemeral copy: inject live draft/workspace state for this model call
        # only so durable history is not polluted with stale YAML snapshots.
        call_ctx = chat_ctx.copy()
        if state_block:
            call_ctx.add_message(role="system", content=_FRESHNESS_NOTE + state_block)

        result = Agent.default.llm_node(self, call_ctx, tools, model_settings)
        if hasattr(result, "__aiter__"):
            async for chunk in result:  # type: ignore[union-attr]
                yield chunk
            return
        resolved = await result  # type: ignore[misc]
        if resolved is None:
            return
        if isinstance(resolved, (str, llm.ChatChunk)):
            yield resolved
            return
        if hasattr(resolved, "__aiter__"):
            async for chunk in resolved:
                yield chunk
