"""Provider typing indicator for the length of a text turn.

A channel customer otherwise stares at nothing while the agent thinks and runs
tools. This runs the provider's hint as a background task alongside the turn and
cancels it when the turn ends; providers clear the hint themselves once the
reply arrives.

Adapters own their own cadence in ``ChannelAdapter.keep_typing`` — Telegram, for
instance, refreshes a 5s action.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from services.integrations.channel import get_channel_adapter
from services.messaging.delivery import load_delivery_context
from services.messaging.textq import TextTurnJob
from services.user import Tenant

logger = logging.getLogger("talqing.services.messaging.typing_indicator")


async def _keep_typing(
    tenant: Tenant,
    job: TextTurnJob,
    inbound_provider_message_id: str | None,
) -> None:
    """Drive one adapter's typing hint until cancelled. Never raises into the turn."""
    try:
        context = await load_delivery_context(tenant, job)
        if context.trigger.reply_mode == "none":
            # Both providers ask that the hint only be shown when a reply is
            # actually coming.
            return
        adapter = get_channel_adapter(context.integration.provider)
        if adapter is None:
            return
        await adapter.keep_typing(
            tenant=tenant,
            integration=context.integration,
            thread_metadata=context.ref.metadata,
            inbound_provider_message_id=inbound_provider_message_id,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        # A missing hint is cosmetic; the turn and its reply carry on.
        logger.warning(
            "typing indicator failed for conversation %s",
            job.conversation_id,
            exc_info=True,
        )


@asynccontextmanager
async def provider_typing(
    tenant: Tenant,
    job: TextTurnJob,
    *,
    inbound_provider_message_id: str | None,
) -> AsyncIterator[None]:
    """Show the customer that the agent is working, for as long as the block runs.

    A no-op unless the turn came from a channel provider: ``integration_id`` is
    set only by the Telegram webhook, never by web text or a CoPilot
    (which have their own SSE progress).
    """
    if job.integration_id is None:
        yield
        return

    task = asyncio.create_task(
        _keep_typing(tenant, job, inbound_provider_message_id),
        name=f"text_typing_{job.conversation_id}",
    )
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
