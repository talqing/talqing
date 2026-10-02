"""Plane-aware fan-out of persisted conversation steps (SSE + provider)."""

from __future__ import annotations

import logging
from uuid import UUID

from services import conversations
from services.copilot.models import CopilotMessageEvent
from workers.text.types import TextTurnInput

logger = logging.getLogger("talqing.workers.text.fanout")

# Dashboard-class surfaces get the full LiveKit timeline (all ChatItem types).
# Telegram only receives assistant message text via provider delivery.
_DASHBOARD_STEP_TYPES = frozenset(
    {
        "message",
        "function_call",
        "function_call_output",
        "agent_handoff",
        "agent_config_update",
    }
)


async def fanout_step(
    input: TextTurnInput,
    row: dict[str, object],
) -> None:
    """Publish one persisted step to the right SSE bus and (if needed) provider.

    CoPilots / tenant web: full ChatItem timeline.
    Telegram provider path: assistant message text only.
    """
    plane = input.plane
    if plane.copilot is not None:
        await _fanout_copilot(input, row)
    elif plane.name == "tenant":
        await _fanout_tenant(input, row)
    else:
        raise RuntimeError(f"unknown text plane: {plane.name}")


async def _fanout_tenant(input: TextTurnInput, row: dict[str, object]) -> None:
    plane = input.plane
    item_id = row["id"]
    item_type = str(row.get("type") or "")
    role = row.get("role")
    text = row.get("text")

    if item_type in _DASHBOARD_STEP_TYPES:
        await conversations.publish(
            input.tenant.id,
            input.job.conversation_id,
            await conversations.item_created_event(input.tenant.id, row),
        )
    # Progressive provider delivery: assistant text only (never tools).
    if (
        item_type == "message"
        and role == "assistant"
        and isinstance(text, str)
        and text.strip()
        and row.get("delivery_status") == "pending"
        and input.job.conversation_ref_id is not None
        and plane.deliver_to_provider
    ):
        from services.messaging.delivery import (
            ProviderDeliveryPermanentError,
            deliver_assistant_item,
        )

        try:
            await deliver_assistant_item(
                input.tenant,
                input.job,
                item_id if isinstance(item_id, UUID) else UUID(str(item_id)),
                text,
            )
        except ProviderDeliveryPermanentError:
            logger.warning(
                "provider delivery permanent failure for item %s",
                item_id,
                exc_info=True,
            )
        except Exception:
            logger.exception("provider delivery failed for item %s", item_id)


async def _fanout_copilot(input: TextTurnInput, row: dict[str, object]) -> None:
    # Full LiveKit timeline for the editor rail.
    # SSE channel is keyed by what this CoPilot edits.
    item_type = str(row.get("type") or "")
    if item_type not in _DASHBOARD_STEP_TYPES:
        return
    from services import copilot
    from services.copilot import hidden_from_rail, public_message

    # Dropped here as well as in the snapshot, so the rail never sees one live
    # and then loses it on the next reload.
    if hidden_from_rail(row):
        return

    subject = input.plane.copilot
    assert subject is not None  # only reached from the CoPilot branch
    if input.subject_id is None:
        logger.error("%s fan-out missing subject_id", subject.label)
        return
    try:
        await copilot.publish(
            subject,
            str(input.subject_id),
            CopilotMessageEvent.model_validate(public_message(row).model_dump()),
        )
    except Exception:
        logger.exception("%s step publish failed for type=%s", subject.label, item_type)
