"""WhatsApp calls through Twilio: turning calling on, and answering each call.

A user taps "call" in their chat with a Twilio sender. Twilio terminates Meta's
leg and asks our voice webhook what to do, as it would for a call to a Twilio
phone number. The webhook is where every decision is made: it verifies the
signature, refuses what it must, files the call on the chat's thread, creates
the LiveKit room with the agent dispatched, and answers `<Dial><Sip>` into it.
LiveKit's side is one static route per region
(`livekit_sip.ensure_whatsapp_route`) that drops the leg into the room its user
part names. From there it is an ordinary room-SIP call
(`workers/voice/whatsapp.py`).

Turning calling on is a TwiML App in the tenant's own Twilio account whose Voice
URL is our webhook, set as the sender's `voice_application_sid`. Nothing about
it is stored: the app is recognised as ours by that URL, which names the tenant
and the integration.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Mapping
from uuid import UUID, uuid4
from xml.sax.saxutils import escape, quoteattr

from fastapi import HTTPException
from livekit import api as lk_api

import db
from services import credits
from services.close_reasons import WHATSAPP_CALLER_UNIDENTIFIED, WHATSAPP_CALLING_OFF
from services.conversations.keys import whatsapp_conversation_key
from services.conversations.refs import ensure_conversation_ref
from services.integrations.models import (
    INTEGRATION_TRIGGER_COLUMNS,
    Integration,
    IntegrationTrigger,
)
from services.integrations.triggers import get_active_trigger
from services.secrets import load_secrets
from services.user import Context, Tenant
from settings import get_settings
from workers.session import persistence

from .bsp import BspError
from .twilio import TwilioWhatsApp, webhook_peer

logger = logging.getLogger("talqing.integrations.whatsapp.calls")

WHATSAPP_CALL_TRIGGER = "whatsapp.call.inbound"
SESSION_TYPE = "WHATSAPP_INBOUND"

_REJECT = '<?xml version="1.0" encoding="UTF-8"?><Response><Reject/></Response>'


def voice_url(tenant_id: UUID, integration_id: UUID) -> str:
    """The Voice URL of the TwiML App that turns calling on for one integration."""
    base = get_settings().app.api_public_url.rstrip("/")
    return f"{base}/v1/integrations/voice/whatsapp/{tenant_id}/{integration_id}"


def _app_name(sender_e164: str) -> str:
    return f"Talqing WhatsApp calls {sender_e164}"


def _twilio(integration: Integration, secrets: Mapping[str, str]) -> TwilioWhatsApp:
    # Local: the package's __init__ imports this module.
    from . import bsp_client

    client = bsp_client(integration, secrets)
    if not isinstance(client, TwilioWhatsApp):
        raise RuntimeError(f"integration {integration.id} is not on Twilio")
    return client


# ── turning calling on and off ──────────────────────────────────────────────


async def enable_calling(
    ctx: Context, *, integration: Integration, trigger: IntegrationTrigger
) -> IntegrationTrigger:
    """Point the sender's calls at Talqing. Idempotent: re-enabling is a no-op."""
    # Local: `services.telephony` imports the integrations package back at load.
    from services.telephony import livekit_sip

    try:
        await livekit_sip.ensure_whatsapp_route()
    except Exception as exc:
        logger.exception("could not set up the LiveKit route for WhatsApp calls")
        await _set_trigger_status(ctx, trigger, "error", trigger.provider_subscription_ref)
        raise HTTPException(
            status_code=502, detail=f"could not set up calling on Talqing's side: {exc}"
        ) from exc

    url = voice_url(ctx.tenant.id, integration.id)
    sender_sid = str(integration.provider_account_info["sender_sid"])
    sender_e164 = str(integration.provider_account_info["sender_e164"])
    client = _twilio(integration, await load_secrets(ctx.tenant))
    try:
        current = await client.voice_application_sid(sender_sid)
        if current:
            current_url = await client.application_voice_url(current)
            if current_url == url:
                return await _set_trigger_status(ctx, trigger, "active", current)
            # `None` is a SID naming no app — Twilio does not check what it is
            # given — which routes calls nowhere and is nobody's to protect.
            if current_url is not None:
                await _set_trigger_status(ctx, trigger, "error", None)
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Calling on this number is already set up elsewhere in Twilio. "
                        "Turn it off there first."
                    ),
                )
        app_sid = await client.create_application(
            friendly_name=_app_name(sender_e164), voice_url=url
        )
        try:
            await client.set_voice_application(sender_sid, app_sid)
        except BspError:
            await client.delete_application(app_sid)
            raise
    except BspError as exc:
        await _set_trigger_status(ctx, trigger, "error", None)
        # Twilio refuses the sender update when the WABA is below Meta's 2,000
        # messaging limit; its own message is the one sentence that says so.
        raise HTTPException(
            status_code=400 if exc.kind == "account" else 502,
            detail=f"could not turn on calling in Twilio: {exc}",
        ) from exc
    return await _set_trigger_status(ctx, trigger, "active", app_sid)


async def disable_calling(tenant: Tenant, integration: Integration) -> None:
    """Stop the sender's calls reaching Talqing, and delete our app.

    Only ever undoes what is ours: the sender is cleared while it still points
    at our app, and only apps whose Voice URL is ours are deleted. Raises
    ``BspError`` when Twilio refuses, so the caller decides how loudly to say so.
    """
    url = voice_url(tenant.id, integration.id)
    sender_sid = str(integration.provider_account_info["sender_sid"])
    sender_e164 = str(integration.provider_account_info["sender_e164"])
    client = _twilio(integration, await load_secrets(tenant))
    current = await client.voice_application_sid(sender_sid)
    if current and await client.application_voice_url(current) == url:
        await client.set_voice_application(sender_sid, "")
    for app in await client.applications_named(_app_name(sender_e164)):
        if app.get("voice_url") == url:
            await client.delete_application(str(app["sid"]))


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


# ── answering a call ────────────────────────────────────────────────────────


def _dial(room: str) -> str:
    """`<Dial><Sip>` into ``room`` on this region's edge.

    `answerOnBridge`: the caller keeps hearing ringing, and Meta's leg stays
    unanswered, until the agent picks up — livekit-sip answers only once the
    agent has subscribed to the caller's audio.
    """
    # Local: `services.telephony` imports the integrations package back at load.
    from services.telephony import livekit_sip

    sip = get_settings().livekit.sip
    edge = livekit_sip.platform_sip_uri_for_provider(livekit_sip.assign_sip_uri())
    return (
        '<?xml version="1.0" encoding="UTF-8"?><Response>'
        f'<Dial answerOnBridge="true" timeLimit="{sip.max_call_duration_seconds}">'
        f"<Sip username={quoteattr(sip.whatsapp.username)} "
        f"password={quoteattr(sip.whatsapp.password)}>{escape(f'sip:{room}@{edge}')}</Sip>"
        "</Dial></Response>"
    )


async def answer_call(
    *,
    tenant: Tenant,
    integration: Integration,
    headers: Mapping[str, str],
    raw_body: bytes,
) -> str:
    """The TwiML for one inbound WhatsApp call.

    Every refusal is a `<Reject/>` plus a session row saying why, so a call the
    tenant's customer made never vanishes. Twilio may deliver the same call
    twice; the `CallSid` is the session's idempotency key, and a replay gets the
    answer the first delivery got.
    """
    _t0 = time.monotonic()
    if integration.provider_account_info.get("bsp") != "twilio":
        raise HTTPException(status_code=404, detail="integration not found")
    try:
        client = _twilio(integration, await load_secrets(tenant))
    except BspError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    logger.info("latency-debug answer_call secrets %d ms", (time.monotonic() - _t0) * 1000)
    # Rebuilt from the configured public URL, never the request's host: Caddy
    # sits in front, and the signature covers the URL Twilio called.
    form = client.verified_form(
        url=voice_url(tenant.id, integration.id), headers=headers, raw_body=raw_body
    )
    call_sid = (form.get("CallSid") or "").strip()
    if not call_sid:
        raise HTTPException(status_code=400, detail="Twilio call webhook has no CallSid")

    pool = await db.tenant_pool(tenant)
    replay = await _replay(pool, tenant, call_sid)
    if replay is not None:
        return replay
    logger.info("latency-debug answer_call replay_check %d ms", (time.monotonic() - _t0) * 1000)

    sender_e164 = str(integration.provider_account_info["sender_e164"])
    session_id = str(uuid4())
    trigger = await get_active_trigger(tenant, integration.id, WHATSAPP_CALL_TRIGGER)
    logger.info("latency-debug answer_call trigger %d ms", (time.monotonic() - _t0) * 1000)
    peer = webhook_peer(form)
    # A user who hides their number is `CC.<id>`, which is a thread key but not
    # a phone number any column promises.
    from_e164 = peer if peer.startswith("+") else None

    async def refuse(close_reason: str, agent_id: UUID | None = None) -> str:
        logger.info("refusing WhatsApp call %s on %s: %s", call_sid, integration.id, close_reason)
        await persistence.record_refused_call(
            tenant,
            session_id=session_id,
            type_=SESSION_TYPE,
            agent_id=str(agent_id) if agent_id else None,
            close_reason=close_reason,
            livekit_room=None,
            from_e164=from_e164,
            to_e164=sender_e164,
            idempotency_key=call_sid,
            integration_id=str(integration.id),
            trigger_id=str(trigger.id) if trigger else None,
        )
        return _REJECT

    if (
        integration.status != "active"
        or trigger is None
        or trigger.agent_id is None
        or form.get("To") != f"whatsapp:{sender_e164}"
    ):
        return await refuse(WHATSAPP_CALLING_OFF)
    agent_id = trigger.agent_id
    if not peer:
        return await refuse(WHATSAPP_CALLER_UNIDENTIFIED, agent_id)
    if not await credits.has_credit(tenant):
        return await refuse(credits.INSUFFICIENT_CREDITS_CLOSE_REASON, agent_id)
    logger.info("latency-debug answer_call credit %d ms", (time.monotonic() - _t0) * 1000)
    agent = await pool.fetchrow(
        """
        SELECT a.name, a.published_version, av.id AS version_id
        FROM agents a
        JOIN agent_versions av
            ON av.agent_id = a.id AND av.version = a.published_version
            AND av.tenant_id = a.tenant_id
        WHERE a.id = $1 AND a.tenant_id = $2
        """,
        agent_id,
        tenant.id,
    )
    if agent is None:
        return await refuse("unpublished_agent_failed", agent_id)
    logger.info("latency-debug answer_call agent_row %d ms", (time.monotonic() - _t0) * 1000)

    profile_name = (form.get("CallerName") or "").strip() or None
    # The same identity, metadata and seed the chat writes
    # (`_process_inbound_text`), so a call and a chat are one thread and one
    # contact whichever came first.
    customer_metadata: dict[str, object] = {
        "peer_address": form.get("From") or "",
        "sender_e164": sender_e164,
        "phone": peer,
        **({"profile_name": profile_name} if profile_name else {}),
    }
    userdata_seed = {
        "whatsapp": {"phone": peer, **({"profile_name": profile_name} if profile_name else {})}
    }
    async with pool.acquire() as conn:
        async with conn.transaction():
            thread = await ensure_conversation_ref(
                conn,
                tenant_id=tenant.id,
                conversation_key=whatsapp_conversation_key(sender_e164=sender_e164, peer=peer),
                integration_id=integration.id,
                source="whatsapp",
                # Always the chat's conversation, whatever the voice agent is set
                # to: on the person's phone the call and the chat are one thread.
                context="transcript",
                customer_metadata=customer_metadata,
                userdata_seed=userdata_seed,
            )
            inserted = await conn.fetchval(
                """
                INSERT INTO sessions (
                    id, tenant_id, conversation_id, conversation_ref_id, agent_id,
                    agent_version_id, agent_name, channel, type, status,
                    integration_id, trigger_id, livekit_room, from_e164, to_e164,
                    idempotency_key, billing_status
                )
                VALUES (
                    $1::uuid, $2, $3, $4, $5, $6, $7, 'voice', $8, 'queued',
                    $9, $10, $1, $11, $12, $13, 'pending'
                )
                ON CONFLICT DO NOTHING
                RETURNING id
                """,
                session_id,
                tenant.id,
                thread.conversation_id,
                thread.ref.id,
                agent_id,
                agent["version_id"],
                agent["name"],
                SESSION_TYPE,
                integration.id,
                trigger.id,
                from_e164,
                sender_e164,
                call_sid,
            )
    if inserted is None:
        # A concurrent delivery of this same call won the insert.
        return await _replay(pool, tenant, call_sid) or _REJECT
    logger.info(
        "latency-debug answer_call thread_and_session_txn %d ms", (time.monotonic() - _t0) * 1000
    )

    metadata = json.dumps(
        {
            "kind": "whatsapp_call",
            "tenant": str(tenant.id),
            "agent": str(agent_id),
            "version": agent["published_version"],
            "session": session_id,
            "conversation_id": str(thread.conversation_id),
            "conversation_ref_id": str(thread.ref.id),
            "integration_id": str(integration.id),
            "trigger_id": str(trigger.id),
            "sender_e164": sender_e164,
            "from_e164": from_e164,
            "call_sid": call_sid,
        }
    )
    if not await _dispatch(tenant, session_id, metadata):
        return _REJECT
    logger.info(
        "latency-debug answer_call room_and_dispatch %d ms", (time.monotonic() - _t0) * 1000
    )
    return _dial(session_id)


async def _replay(pool, tenant: Tenant, call_sid: str) -> str | None:
    """The answer an already-recorded delivery of this call got, if any."""
    row = await pool.fetchrow(
        """
        SELECT status, livekit_room FROM sessions
        WHERE tenant_id = $1 AND idempotency_key = $2
        """,
        tenant.id,
        call_sid,
    )
    if row is None:
        return None
    if row["status"] in ("queued", "running") and row["livekit_room"]:
        return _dial(row["livekit_room"])
    return _REJECT


async def _dispatch(tenant: Tenant, session_id: str, metadata: str) -> bool:
    """Create the call's room with its agent, as an outbound dial does.

    On failure the session is closed as `sip_dispatch_failed` and the room is
    best-effort deleted. `empty_timeout` covers the gap until Twilio's INVITE,
    which measured ~0.5 s; a room gone by then would be recreated by the SIP
    join with no agent in it, and the caller would ring until they gave up.
    """
    # Local: `services.telephony` imports the integrations package back at load.
    from services.telephony import livekit_sip

    try:
        async with livekit_sip.lk_client() as lk:
            await lk.room.create_room(
                lk_api.CreateRoomRequest(name=session_id, empty_timeout=60, metadata="")
            )
            await lk.agent_dispatch.create_dispatch(
                lk_api.CreateAgentDispatchRequest(
                    agent_name=get_settings().livekit.agent_name,
                    room=session_id,
                    metadata=metadata,
                )
            )
    except Exception:
        logger.exception("LiveKit room/dispatch failed for WhatsApp call %s", session_id)
        pool = await db.tenant_pool(tenant)
        await pool.execute(
            """
            UPDATE sessions
            SET status = 'failed',
                close_reason = 'sip_dispatch_failed',
                ended_at = COALESCE(ended_at, now()),
                updated_at = now()
            WHERE id = $1::uuid AND tenant_id = $2 AND status = 'queued'
            """,
            session_id,
            tenant.id,
        )
        try:
            async with livekit_sip.lk_client() as lk:
                await lk.room.delete_room(lk_api.DeleteRoomRequest(room=session_id))
        except Exception:
            logger.exception("best-effort delete of WhatsApp call room %s failed", session_id)
        return False
    return True
