"""WhatsApp through a BSP — Telegram's channel, with a different adapter.

One integration is one sender number at the user's own BSP account (Twilio or
Gupshup, ``provider_account_info.bsp``). Enabling its ``whatsapp.message.inbound``
trigger puts a published text agent on the number: every inbound text becomes a
turn and the agent's reply goes back over WhatsApp. The same integration sends
template campaigns (``services.whatsapp``), whose receipts arrive on this
webhook too.

Each BSP module parses its own wire format into ``bsp`` shapes; everything from
there on is BSP-neutral.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from fastapi import HTTPException

import db
from services.chats.open import publish_ends
from services.conversations.keys import whatsapp_conversation_key
from services.conversations.refs import ensure_conversation_ref
from services.integrations.channel import (
    OutboundSendResult,
    PartialSendError,
    UnsupportedReplyMode,
    mark_webhook_received,
    register_channel_adapter,
    split_message_text,
)
from services.integrations.models import (
    INTEGRATION_TRIGGER_COLUMNS,
    Integration,
    IntegrationTrigger,
)
from services.integrations.triggers import get_active_trigger
from services.messaging import inbound, publish_turn
from services.secrets import load_secrets, resolve_secretish
from services.user import Context, Tenant
from settings import get_settings

from . import calls, gupshup, twilio
from .bsp import (
    WHATSAPP_MESSAGE_LIMIT,
    BspError,
    FailureKind,
    InboundText,
    InboundUnsupported,
    StatusEvent,
)
from .gupshup import GupshupWhatsApp
from .twilio import TwilioWhatsApp

logger = logging.getLogger("talqing.integrations.whatsapp")

PROVIDER = "whatsapp"
WHATSAPP_MESSAGE_TRIGGER = "whatsapp.message.inbound"

# Twilio's typing indicator lasts 25 seconds or until the reply lands, so it is
# refreshed just inside that.
# https://www.twilio.com/docs/whatsapp/api/typing-indicators-resource
_TYPING_REFRESH_SECONDS = 20.0

WhatsAppBsp = TwilioWhatsApp | GupshupWhatsApp


def webhook_url(tenant_id: UUID, integration_id: UUID) -> str:
    """Where both BSPs deliver messages and receipts for one integration."""
    base = get_settings().app.api_public_url.rstrip("/")
    return f"{base}/v1/integrations/webhooks/{PROVIDER}/{tenant_id}/{integration_id}"


def webhook_token(tenant_id: UUID, integration_id: UUID) -> str:
    """The token a Gupshup webhook URL must carry.

    Gupshup signs nothing, so the URL itself is the credential. Derived rather
    than stored: nothing to rotate by hand, nothing in the tenant's secrets list
    to delete by mistake, and a database read alone does not reveal it.
    """
    key = get_settings().security.secrets_fernet_key.encode("utf-8")
    message = f"whatsapp-webhook:{tenant_id}:{integration_id}".encode()
    return hmac.new(key, message, hashlib.sha256).hexdigest()[:40]


def gupshup_callback_url(tenant_id: UUID, integration_id: UUID) -> str:
    """Where Gupshup delivers: the webhook URL plus its token."""
    return (
        f"{webhook_url(tenant_id, integration_id)}?token={webhook_token(tenant_id, integration_id)}"
    )


def bsp_client(integration: Integration, secrets: Mapping[str, str]) -> WhatsAppBsp:
    """The BSP client for an integration. Raises ``BspError`` on a broken credential."""
    info = integration.provider_account_info
    credential = resolve_secretish(integration.credentials_ref, dict(secrets))
    if not credential:
        raise BspError(
            "this WhatsApp integration's credential does not resolve to a secret; "
            "update it with a new one",
            kind="account",
        )
    if info.get("bsp") == "twilio":
        return TwilioWhatsApp(
            account_sid=str(info["account_sid"]),
            auth_token=credential,
            sender_e164=str(info["sender_e164"]),
        )
    if info.get("bsp") == "gupshup":
        return GupshupWhatsApp(
            api_key=credential,
            app_id=str(info["app_id"]),
            app_name=str(info["app_name"]),
            sender_e164=str(info["sender_e164"]),
        )
    raise RuntimeError(f"unknown WhatsApp BSP {info.get('bsp')!r}")


def failure_kind(integration: Integration, code: str | None) -> FailureKind:
    """What an asynchronous failure code on a receipt means for the sender."""
    if integration.provider_account_info.get("bsp") == "gupshup":
        return gupshup.failure_kind(code)
    return twilio.classify(code, 400)  # type: ignore[return-value]


def expected_refusals(integration: Integration) -> frozenset[str]:
    """Receipt codes a cold list produces in bulk, which no breaker should count."""
    if integration.provider_account_info.get("bsp") == "gupshup":
        return gupshup.EXPECTED_REFUSAL_CODES
    return twilio.EXPECTED_REFUSAL_CODES


async def _set_trigger_status(
    ctx: Context, trigger: IntegrationTrigger, status: str, ref: str | None
) -> IntegrationTrigger:
    pool = await ctx.tenant_pool()
    row = await pool.fetchrow(
        f"""
        UPDATE integration_triggers
        SET status = $3, provider_subscription_ref = $4, updated_at = now()
        WHERE id = $1 AND tenant_id = $2
        RETURNING {INTEGRATION_TRIGGER_COLUMNS}
        """,
        trigger.id,
        ctx.tenant.id,
        status,
        ref,
    )
    if not row:
        raise HTTPException(status_code=404, detail="integration trigger not found")
    return IntegrationTrigger.from_row(row)


class WhatsAppChannelAdapter:
    provider = PROVIDER

    def supports_reply_mode(self, reply_mode: str) -> bool:
        return reply_mode == "public_reply"

    async def send_outbound(
        self,
        *,
        tenant: Tenant,
        integration: Integration,
        thread_metadata: Mapping[str, Any],
        text: str,
        reply_mode: str,
    ) -> OutboundSendResult:
        if reply_mode != "public_reply":
            raise UnsupportedReplyMode("WhatsApp has no internal notes; use public_reply or none")
        peer_address = str(thread_metadata.get("peer_address") or "").strip()
        if not peer_address:
            raise RuntimeError("WhatsApp thread has no peer_address")
        parts = split_message_text(text, WHATSAPP_MESSAGE_LIMIT)
        if not parts:
            raise RuntimeError("WhatsApp outbound message has no text to send")

        try:
            client = bsp_client(integration, await load_secrets(tenant))
        except BspError as exc:
            raise RuntimeError(str(exc)) from exc
        message_ids: list[str] = []
        for part in parts:
            try:
                message_ids.append(await client.send_text(to=peer_address, text=part))
            except BspError as exc:
                if message_ids:
                    raise PartialSendError(
                        f"WhatsApp delivered {len(message_ids)} of {len(parts)} message parts "
                        f"before failing: {exc}",
                        sent_message_ids=message_ids,
                    ) from exc
                # Nothing retries a reply, so every failure is final — and a
                # reply the customer waits for is better failed than stuck.
                raise RuntimeError(str(exc)) from exc
        return OutboundSendResult(
            provider_message_ids=tuple(message_ids),
            raw_responses=tuple({"message_id": m} for m in message_ids),
            visibility="customer_visible",
        )

    async def keep_typing(
        self,
        *,
        tenant: Tenant,
        integration: Integration,
        thread_metadata: Mapping[str, Any],
        inbound_provider_message_id: str | None,
    ) -> None:
        # Gupshup's self-serve API has no typing indicator.
        if integration.provider_account_info.get("bsp") != "twilio":
            return
        if not inbound_provider_message_id:
            return
        client = bsp_client(integration, await load_secrets(tenant))
        assert isinstance(client, TwilioWhatsApp)
        while True:
            await client.keep_typing_once(inbound_provider_message_id)
            await asyncio.sleep(_TYPING_REFRESH_SECONDS)

    async def on_trigger_enabled(
        self,
        ctx: Context,
        *,
        integration: Integration,
        trigger: IntegrationTrigger,
    ) -> IntegrationTrigger | None:
        if trigger.trigger_type == calls.WHATSAPP_CALL_TRIGGER:
            return await calls.enable_calling(ctx, integration=integration, trigger=trigger)
        if trigger.trigger_type != WHATSAPP_MESSAGE_TRIGGER:
            return None
        if integration.provider_account_info.get("bsp") == "gupshup":
            return await _subscribe_gupshup(ctx, integration, trigger)
        url = webhook_url(ctx.tenant.id, integration.id)
        try:
            client = bsp_client(integration, await load_secrets(ctx.tenant))
            assert isinstance(client, TwilioWhatsApp)
            sender = await client.require_online_sender()
            await client.set_inbound_webhook(sender["sid"], url)
        except BspError as exc:
            await _set_trigger_status(ctx, trigger, "error", trigger.provider_subscription_ref)
            raise HTTPException(
                status_code=400 if exc.kind == "account" else 502,
                detail=f"could not point the Twilio sender at Talqing: {exc}",
            ) from exc
        return await _set_trigger_status(ctx, trigger, "active", url)

    async def on_trigger_disabled(
        self,
        ctx: Context,
        *,
        integration: Integration,
        trigger_type: str,
    ) -> None:
        if trigger_type == calls.WHATSAPP_CALL_TRIGGER:
            try:
                await calls.disable_calling(ctx.tenant, integration)
            except BspError as exc:
                # The trigger is already off on our side and stays off; the
                # number's calls would now reach a webhook that refuses them.
                raise HTTPException(
                    status_code=502,
                    detail=(
                        f"Calls are off in Talqing, but Twilio refused to turn them off on "
                        f"the number ({exc}). Clear its voice configuration in Twilio."
                    ),
                ) from exc
            return
        if trigger_type != WHATSAPP_MESSAGE_TRIGGER:
            return
        await clear_inbound_webhook(ctx.tenant, integration)

    async def receive_webhook(
        self,
        *,
        tenant: Tenant,
        integration: Integration,
        headers: Mapping[str, str],
        query: Mapping[str, str],
        raw_body: bytes,
    ) -> None:
        bsp = integration.provider_account_info.get("bsp")
        if bsp == "gupshup":
            # Checked before anything else is read: an unauthenticated request
            # must not learn whether the credential resolves.
            expected = webhook_token(tenant.id, integration.id)
            if not hmac.compare_digest(query.get("token") or "", expected):
                raise HTTPException(status_code=403, detail="invalid webhook token")
        try:
            client = bsp_client(integration, await load_secrets(tenant))
        except BspError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

        if isinstance(client, TwilioWhatsApp):
            # Rebuilt from the configured public URL, never the request's host:
            # Caddy sits in front, and the signature covers the URL Twilio called.
            event = client.parse_webhook(
                url=webhook_url(tenant.id, integration.id), headers=headers, raw_body=raw_body
            )
        else:
            try:
                payload = json.loads(raw_body.decode("utf-8"))
            except json.JSONDecodeError as exc:
                raise HTTPException(status_code=400, detail="invalid JSON webhook payload") from exc
            if not isinstance(payload, dict):
                raise HTTPException(status_code=400, detail="webhook payload must be an object")
            event = client.parse_webhook(payload)

        # Not before this: Gupshup's check when a webhook is registered, and
        # events nothing here reads, are not activity on the number.
        if event is None:
            return
        await mark_webhook_received(tenant, integration.id)
        if isinstance(event, StatusEvent):
            # Imported here: services.whatsapp reaches back into this package.
            from services.whatsapp import record_status

            await record_status(tenant, integration, event)
            return
        if isinstance(event, InboundUnsupported):
            logger.error(
                "Unsupported WhatsApp %s message (id=%s, integration_id=%s): %s",
                event.kind,
                event.message_id,
                integration.id,
                json.dumps(event.raw, default=str),
            )
            return
        await _process_inbound_text(tenant, integration, event)


async def _subscribe_gupshup(
    ctx: Context, integration: Integration, trigger: IntegrationTrigger
) -> IntegrationTrigger:
    """Register the app's webhook with Gupshup. Idempotent: our URL is found and kept."""
    url = gupshup_callback_url(ctx.tenant.id, integration.id)
    tag = _gupshup_tag(integration.id)
    try:
        client = bsp_client(integration, await load_secrets(ctx.tenant))
        assert isinstance(client, GupshupWhatsApp)
        subscriptions = await client.subscriptions()
        ours = next((s for s in subscriptions if s.get("url") == url), None)
        if ours is not None:
            return await _set_trigger_status(ctx, trigger, "active", str(ours["id"]))
        # Tags are unique per app, so one left behind at an old URL is in the way.
        for stale in (s for s in subscriptions if s.get("tag") == tag):
            await client.unsubscribe(str(stale["id"]))
        subscription_id = await client.subscribe(tag=tag, url=url)
    except BspError as exc:
        await _set_trigger_status(ctx, trigger, "error", trigger.provider_subscription_ref)
        raise HTTPException(
            status_code=400 if exc.kind == "account" else 502,
            detail=f"could not register Talqing's webhook with Gupshup: {exc}",
        ) from exc
    return await _set_trigger_status(ctx, trigger, "active", subscription_id)


def _gupshup_tag(integration_id: UUID) -> str:
    return f"talqing-{integration_id}"


async def clear_inbound_webhook(tenant: Tenant, integration: Integration) -> None:
    """Best-effort: stop the BSP posting messages to Talqing.

    Only ever removes what is ours: a Twilio sender's URL while it is still
    ours, a Gupshup subscription by our URL or tag.
    """
    try:
        client = bsp_client(integration, await load_secrets(tenant))
        if isinstance(client, TwilioWhatsApp):
            await client.clear_inbound_webhook(
                str(integration.provider_account_info["sender_sid"]),
                webhook_url(tenant.id, integration.id),
            )
            return
        url = gupshup_callback_url(tenant.id, integration.id)
        tag = _gupshup_tag(integration.id)
        for subscription in await client.subscriptions():
            if subscription.get("url") == url or subscription.get("tag") == tag:
                await client.unsubscribe(str(subscription["id"]))
    except BspError:
        logger.warning("could not clear the WhatsApp webhook for %s", integration.id, exc_info=True)


async def disconnect_number(tenant: Tenant, integration: Integration) -> None:
    """Best-effort, on integration delete: stop the number's messages and calls reaching us."""
    await clear_inbound_webhook(tenant, integration)
    if integration.provider_account_info.get("bsp") != "twilio":
        return
    try:
        await calls.disable_calling(tenant, integration)
    except BspError:
        logger.warning("could not turn off Twilio calling for %s", integration.id, exc_info=True)


whatsapp_adapter = register_channel_adapter(WhatsAppChannelAdapter())


# ── Inbound ─────────────────────────────────────────────────────────────────


async def _process_inbound_text(
    tenant: Tenant, integration: Integration, message: InboundText
) -> None:
    if integration.status != "active":
        return
    # Imported here for the same reason as `record_status` above.
    from services.whatsapp import record_reply

    trigger = await get_active_trigger(tenant, integration.id, WHATSAPP_MESSAGE_TRIGGER)
    # None when nobody is assigned to answer. A campaign reply is still a reply:
    # it is stored on the thread like any other, and no turn runs.
    run_agent_id = trigger.agent_id if trigger else None

    sender_e164 = str(integration.provider_account_info["sender_e164"])
    customer_metadata: dict[str, Any] = {
        "peer_address": message.peer_address,
        "sender_e164": sender_e164,
        "phone": message.peer,
    }
    if message.profile_name:
        customer_metadata["profile_name"] = message.profile_name
    userdata_seed = {
        "whatsapp": {
            "phone": message.peer,
            **({"profile_name": message.profile_name} if message.profile_name else {}),
        }
    }
    conversation_key = whatsapp_conversation_key(sender_e164=sender_e164, peer=message.peer)
    item_metadata = {"whatsapp_message_id": message.message_id, **customer_metadata}
    pool = await db.tenant_pool(tenant)
    if trigger is None or run_agent_id is None:
        # Nobody is assigned to answer, so there is no chat: the reply is stored
        # on the contact's thread and no turn runs.
        async with pool.acquire() as conn, conn.transaction():
            thread = await ensure_conversation_ref(
                conn,
                tenant_id=tenant.id,
                conversation_key=conversation_key,
                integration_id=integration.id,
                source=PROVIDER,
                context="transcript",
                customer_metadata=customer_metadata,
                userdata_seed=userdata_seed,
            )
            item, created = await inbound.insert_inbound_item(
                conn,
                tenant_id=tenant.id,
                conversation_id=thread.conversation_id,
                session_id=None,
                agent_id=None,
                agent_version=None,
                text=message.text,
                provider_message_id=message.message_id,
                item_metadata=item_metadata,
                raw_payload=message.raw,
                missing_error="WhatsApp inbound item was not inserted and no existing item was found",
            )
        if created:
            await record_reply(tenant, integration, message.peer)
            await inbound.publish_inbound_item_created(
                tenant_id=tenant.id, conversation_id=thread.conversation_id, item=item
            )
        return

    async with pool.acquire() as conn, conn.transaction():
        turn = await inbound.receive_chat_message(
            conn,
            tenant_id=tenant.id,
            integration_id=integration.id,
            trigger_id=trigger.id,
            agent_id=run_agent_id,
            conversation_key=conversation_key,
            source=PROVIDER,
            customer_metadata=customer_metadata,
            userdata_seed=userdata_seed,
            text=message.text,
            provider_message_id=message.message_id,
            item_metadata=item_metadata,
            raw_payload=message.raw,
        )
    if turn is None:
        # The BSP retried a delivery we already stored.
        return
    await publish_ends(turn.ends)
    await record_reply(tenant, integration, message.peer)
    await inbound.publish_inbound_item_created(
        tenant_id=tenant.id, conversation_id=turn.job.conversation_id, item=turn.item
    )
    await publish_turn(turn.job)
