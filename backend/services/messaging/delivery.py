"""Outbound sendback for provider-backed text turns.

Delivers assistant text via the channel adapter for the integration's provider.
Progressive turns call ``deliver_assistant_item`` as each assistant step is
persisted.

Tool calls/results are never sent to providers — only assistant message text.

Every chat provider caps a single message (4096 characters on Telegram), so
adapters split a long reply and send the parts in order. That is a transport
detail: the conversation timeline keeps one item per assistant message, holding
the first part's provider id.

Reply modes:
- ``public_reply`` — adapter sends customer-visible message
- ``internal_note`` — adapter sends via provider internal-note API when supported;
  otherwise skipped permanently (Telegram has no such API)
- ``none`` — skip send (generate only)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from uuid import UUID

import httpx

import db
from services import conversations
from services.conversations.models import ItemDeliveryUpdatedEvent
from services.conversations.refs import CONVERSATION_REF_COLUMNS, ConversationRef
from services.integrations.channel import (
    PartialSendError,
    UnsupportedReplyMode,
    require_channel_adapter,
)
from services.integrations.models import (
    INTEGRATION_COLUMNS,
    INTEGRATION_TRIGGER_COLUMNS,
    Integration,
    IntegrationTrigger,
)
from services.messaging.textq import TextTurnJob
from services.user import Tenant

logger = logging.getLogger("talqing.services.messaging.delivery")


class ProviderDeliveryPermanentError(RuntimeError):
    """Provider delivery failed with an outcome that must not be retried."""


@dataclass(frozen=True, slots=True)
class ProviderDeliveryContext:
    """The trigger, account, and thread a provider-backed turn replies through."""

    trigger: IntegrationTrigger
    integration: Integration
    ref: ConversationRef


async def load_delivery_context(tenant: Tenant, job: TextTurnJob) -> ProviderDeliveryContext:
    """Load the provider linkage for a channel-backed text turn.

    Shared by outbound delivery and the typing indicator, which both need the
    same trigger / integration / thread triple.
    """
    if job.trigger_id is None or job.integration_id is None or job.conversation_ref_id is None:
        raise RuntimeError("provider text input is missing trigger/conversation_ref linkage")

    pool = await db.tenant_pool(tenant)
    trigger_row = await pool.fetchrow(
        f"""
        SELECT {INTEGRATION_TRIGGER_COLUMNS}
        FROM integration_triggers
        WHERE id = $1 AND tenant_id = $2
        """,
        job.trigger_id,
        tenant.id,
    )
    integration_row = await pool.fetchrow(
        f"""
        SELECT {INTEGRATION_COLUMNS}
        FROM integrations
        WHERE id = $1 AND tenant_id = $2
        """,
        job.integration_id,
        tenant.id,
    )
    ref_row = await pool.fetchrow(
        f"""
        SELECT {CONVERSATION_REF_COLUMNS}
        FROM conversation_refs
        WHERE id = $1 AND tenant_id = $2
        """,
        job.conversation_ref_id,
        tenant.id,
    )
    if not trigger_row or not integration_row or not ref_row:
        raise RuntimeError("provider text delivery configuration is missing")

    return ProviderDeliveryContext(
        trigger=IntegrationTrigger.from_row(trigger_row),
        integration=Integration.from_row(integration_row),
        ref=ConversationRef.from_row(ref_row),
    )


async def deliver_assistant_item(
    tenant: Tenant,
    job: TextTurnJob,
    item_id: UUID,
    text: str,
) -> None:
    """Deliver one assistant message via the integration's channel adapter.

    Idempotent on delivery_status: only claims rows still ``pending``.
    Never sends tool calls or tool results.
    """
    body = (text or "").strip()
    if not body:
        return
    if job.integration_id is None:
        return

    context = await load_delivery_context(tenant, job)
    trigger = context.trigger
    integration = context.integration
    ref = context.ref
    pool = await db.tenant_pool(tenant)
    item_ids = [item_id]
    reply_mode = trigger.reply_mode

    async def set_delivery(
        status: str,
        error: str | None = None,
        *,
        visibility: str | None = None,
        provider_message_id: str | None = None,
        raw_payload: dict | None = None,
    ) -> None:
        if status == "sent" and provider_message_id is not None:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute(
                        """
                        UPDATE conversation_items
                        SET delivery_status = 'sent',
                            provider_message_id = $3,
                            source = 'text/provider_send',
                            visibility = $4,
                            raw_payload = $5::jsonb,
                            delivery_error = NULL,
                            updated_at = now()
                        WHERE tenant_id = $1 AND id = $2
                        """,
                        tenant.id,
                        item_id,
                        provider_message_id,
                        visibility or "customer_visible",
                        json.dumps(raw_payload or {}),
                    )
                    await conn.execute(
                        """
                        UPDATE conversation_refs
                        SET updated_at = now()
                        WHERE id = $1 AND tenant_id = $2
                        """,
                        job.conversation_ref_id,
                        tenant.id,
                    )
            await conversations.publish(
                tenant.id,
                job.conversation_id,
                ItemDeliveryUpdatedEvent(
                    item_ids=[item_id],
                    delivery_status="sent",
                    provider_message_id=provider_message_id,
                ),
            )
            return

        await pool.execute(
            """
            UPDATE conversation_items
            SET delivery_status = $3,
                delivery_error = $4::jsonb,
                updated_at = now()
            WHERE tenant_id = $1 AND id = ANY($2::uuid[])
            """,
            tenant.id,
            item_ids,
            status,
            json.dumps({"message": error}) if error else None,
        )
        await conversations.publish(
            tenant.id,
            job.conversation_id,
            ItemDeliveryUpdatedEvent(
                item_ids=[item_id],
                delivery_status=status,
                error=error,
            ),
        )

    if reply_mode == "none":
        await set_delivery("skipped", "reply_mode:none")
        return

    if reply_mode not in {"public_reply", "internal_note"}:
        await set_delivery("failed", f"unknown reply_mode:{reply_mode}")
        raise ProviderDeliveryPermanentError(f"unknown reply_mode: {reply_mode}")

    provider = integration.provider
    try:
        adapter = require_channel_adapter(provider)
    except RuntimeError as exc:
        await set_delivery("failed", str(exc))
        raise ProviderDeliveryPermanentError(str(exc)) from exc

    if not adapter.supports_reply_mode(reply_mode):
        await set_delivery(
            "skipped",
            f"provider {provider} does not support reply_mode:{reply_mode}",
        )
        return

    claimed = await pool.fetch(
        """
        UPDATE conversation_items
        SET delivery_status = 'sending', delivery_error = NULL, updated_at = now()
        WHERE tenant_id = $1 AND id = ANY($2::uuid[])
            AND delivery_status = 'pending'
        RETURNING id
        """,
        tenant.id,
        item_ids,
    )
    if not claimed:
        return

    # End-customer recipient fields only (wa_id/bsuid/chat_id, …).
    customer_metadata = dict(ref.metadata)
    try:
        result = await adapter.send_outbound(
            tenant=tenant,
            integration=integration,
            thread_metadata=customer_metadata,
            text=body,
            reply_mode=reply_mode,  # type: ignore[arg-type]
        )
    except UnsupportedReplyMode as exc:
        await set_delivery("skipped", str(exc))
        return
    except PartialSendError as exc:
        # Some parts are already in the customer's chat. Returning the item to
        # pending would duplicate them on the next attempt, so fail it here and
        # keep the delivered ids for support.
        await set_delivery("failed", str(exc))
        logger.error(
            "partial provider delivery for item %s: sent %s part(s) before failing",
            item_id,
            len(exc.sent_message_ids),
        )
        raise ProviderDeliveryPermanentError(str(exc)) from exc
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        error = f"provider returned HTTP {status}"
        if status == 429 or status >= 500:
            await set_delivery("pending", error)
            raise
        await set_delivery("failed", error)
        raise ProviderDeliveryPermanentError(error) from exc
    except httpx.RequestError as exc:
        await set_delivery("failed", str(exc))
        raise ProviderDeliveryPermanentError(
            "provider request failed with an unknown delivery outcome"
        ) from exc
    except RuntimeError as exc:
        await set_delivery("failed", str(exc))
        raise ProviderDeliveryPermanentError(str(exc)) from exc

    await set_delivery(
        "sent",
        provider_message_id=result.provider_message_id,
        visibility=result.visibility,
        raw_payload={"parts": list(result.raw_responses)},
    )
