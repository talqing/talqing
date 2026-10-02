"""Unified text-turn execution for tenant agents and the CoPilots.

Orchestration only: load → reuse or build a window → run turn → park.
Plane-specific window building lives in the tenant and copilot executors.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from services.messaging import TextTurnJob, turn_error_text
from services.messaging.typing_indicator import provider_typing
from services.user import Tenant
from workers.text.copilot_executor import prepare_copilot_agent
from workers.text.events import make_status_publisher, publish_turn_status
from workers.text.interruption import TextTurnInterruption
from workers.text.load import load_text_turn
from workers.text.tenant_executor import (
    TurnRefused,
    attach_tenant_text_output,
    load_chat,
    open_window,
    warm_still_valid,
)
from workers.text.turn import run_text_turn
from workers.text.types import TextTurnInput, TextTurnOutcome, TextWindow
from workers.text.window import close_window

logger = logging.getLogger("talqing.workers.text.executor")


async def _reusable(input: TextTurnInput, window: TextWindow | None) -> bool:
    """Whether the parked window can answer this turn as it stands."""
    if window is None or window.plane.name != input.plane.name:
        return False
    if input.plane.name != "tenant":
        # Platform: same conversation actor + same plane is enough (no publish
        # fingerprint). The actor is keyed by conversation, so the parked window
        # already belongs here.
        return True
    # A window serves one chat. A job for another chat on this conversation is a
    # new chat: the old one's end job normally ran first and took its window
    # with it, and one still parked must never answer for its successor.
    if window.session_id != str(input.job.session_id):
        return False
    return await warm_still_valid(input.tenant, window)


async def _open_cold(input: TextTurnInput) -> TextWindow:
    if input.plane.copilot is not None:
        return await prepare_copilot_agent(input, input.plane.copilot)
    chat = await load_chat(input.tenant, input.job.session_id)
    if chat is None or chat.ended:
        raise TurnRefused("this chat ended before the message was answered")
    return await open_window(input.tenant, input.job, chat)


async def execute_text_job(
    tenant: Tenant,
    job: TextTurnJob,
    interruption: TextTurnInterruption,
    *,
    window: TextWindow | None,
    on_window: Callable[[TextWindow], None],
) -> TextTurnOutcome:
    """Load + prepare + run one text turn. Parks a warm window for reuse.

    ``on_window`` is how the caller learns about a COLD window before the turn
    it opened has finished. Without it a shutdown mid-first-turn cancels this
    coroutine, the outcome never returns, and nothing holds the window whose
    usage ``ConversationActor.aclose()`` still has to flush.
    """
    loaded = await load_text_turn(tenant, job)
    if loaded is None:
        return TextTurnOutcome(result=None, window=window)
    if loaded.plane.name == "tenant" and job.session_id is None:
        raise RuntimeError(f"tenant text turn {job.input_item_id} names no chat")

    if window is not None and not await _reusable(loaded, window):
        await close_window(window)
        window = None

    # Channel customers see "typing…" for the whole turn — cold start included,
    # since compiling an agent is part of the wait. No-op for web and platform
    # turns, which stream their own progress over SSE.
    async with provider_typing(tenant, job, inbound_provider_message_id=loaded.provider_message_id):
        if window is None:
            try:
                window = await _open_cold(loaded)
            except Exception as exc:
                # Nothing has taken ownership of the turn yet, so nothing else
                # will say it stopped — and a turn left `running` keeps its
                # sender waiting on a reply that is not coming.
                if not isinstance(exc, TurnRefused):
                    logger.exception("could not open a window for turn %s", job.input_item_id)
                await publish_turn_status(
                    tenant,
                    job,
                    status="error",
                    subject_id=loaded.subject_id,
                    error=turn_error_text(exc),
                )
                return TextTurnOutcome(result=None, window=None)
            on_window(window)
            path = "cold"
        else:
            if loaded.plane.name == "tenant":
                attach_tenant_text_output(window, job)
            else:
                window.already_started = True
                window.text_output = None
            path = "warm"

        logger.info(
            "text turn %s conversation=%s session=%s path=%s",
            job.input_item_id,
            job.conversation_id,
            window.session_id,
            path,
        )
        try:
            return await run_text_turn(
                loaded, window, interruption, on_status=make_status_publisher(loaded)
            )
        except Exception:
            # `run_text_turn` has already reported the failure on the turn. The
            # window stays warm for the next message.
            logger.exception("text turn %s failed; parking its window", job.input_item_id)
            return TextTurnOutcome(result=None, window=window.park())
