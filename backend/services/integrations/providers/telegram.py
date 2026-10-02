"""Telegram Bot API — identity helpers, channel adapter, inbound handling.

Docs: https://core.telegram.org/bots/api

Telegram bots have no internal-note API for private chats. ``internal_note`` is
unsupported; use ``public_reply`` (sendMessage) or ``none``.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import re
from collections.abc import Mapping
from typing import Any

import httpx
from fastapi import HTTPException

import db
from services.chats.open import publish_ends
from services.conversations.keys import telegram_conversation_key
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

# setWebhook secret_token: 1–256 chars, only A-Z a-z 0-9 _ -
# https://core.telegram.org/bots/api#setwebhook
WEBHOOK_SECRET_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{1,256}$")
WEBHOOK_SECRET_TOKEN_ERROR = (
    "Telegram webhook secret must be 1–256 characters and only use "
    "letters, digits, underscore, and hyphen"
)

logger = logging.getLogger("talqing.integrations.telegram")

PROVIDER = "telegram"
TELEGRAM_MESSAGE_TRIGGER = "telegram.message.inbound"

# sendMessage text is "1-4096 characters after entities parsing".
# https://core.telegram.org/bots/api#sendmessage
TELEGRAM_MESSAGE_LIMIT = 4096

# sendChatAction sets the status "for 5 seconds or less", so refresh just
# inside that window to keep it unbroken across a long turn.
# https://core.telegram.org/bots/api#sendchataction
TELEGRAM_TYPING_REFRESH_SECONDS = 4.0


class TelegramBotApiError(RuntimeError):
    """Telegram Bot API call failed with a permanent configuration outcome."""


def validate_webhook_secret_token(secret_token: str) -> str:
    """Return the secret if it meets Telegram setWebhook rules.

    Never include the secret value in error messages.
    """
    if not isinstance(secret_token, str):
        raise TelegramBotApiError(WEBHOOK_SECRET_TOKEN_ERROR)
    cleaned = secret_token.strip()
    if not WEBHOOK_SECRET_TOKEN_RE.fullmatch(cleaned):
        raise TelegramBotApiError(WEBHOOK_SECRET_TOKEN_ERROR)
    return cleaned


def _parse_bot_token(token: str) -> str:
    cleaned = token.strip()
    if not cleaned or ":" not in cleaned:
        raise TelegramBotApiError("Telegram bot token is malformed")
    bot_id, secret = cleaned.split(":", 1)
    if not bot_id.strip() or not secret.strip():
        raise TelegramBotApiError("Telegram bot token is malformed")
    return cleaned


async def fetch_bot_identity(bot_token: str) -> dict[str, str]:
    """Call getMe and return canonical provider_account_info fields.

    Raises TelegramBotApiError when the token is invalid or Telegram rejects
    the request. Does not fall back to parsing the token id alone — bot_id must
    come from Telegram so thread keys cannot drift from a mistyped account.
    """
    token = _parse_bot_token(bot_token)
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(f"https://api.telegram.org/bot{token}/getMe")
    except httpx.RequestError as exc:
        raise TelegramBotApiError(f"Telegram getMe request failed: {exc}") from exc

    try:
        data = response.json()
    except ValueError as exc:
        raise TelegramBotApiError(
            f"Telegram getMe returned invalid JSON (HTTP {response.status_code})"
        ) from exc
    if not isinstance(data, dict):
        raise TelegramBotApiError("Telegram getMe returned a non-object response")

    if not response.is_success or not data.get("ok"):
        description = str(data.get("description") or "").strip()
        if response.status_code in {401, 403} or "unauthorized" in description.lower():
            raise TelegramBotApiError(description or "Telegram bot token is invalid or revoked")
        raise TelegramBotApiError(
            description or f"Telegram getMe failed (HTTP {response.status_code})"
        )

    result = data.get("result")
    if not isinstance(result, dict):
        raise TelegramBotApiError("Telegram getMe response missing result")
    if not result.get("is_bot"):
        raise TelegramBotApiError("Telegram token does not belong to a bot")

    bot_id = str(result.get("id") or "").strip()
    if not bot_id:
        raise TelegramBotApiError("Telegram getMe response missing bot id")

    identity: dict[str, str] = {"bot_id": bot_id}
    username = str(result.get("username") or "").strip()
    if username:
        identity["bot_username"] = username.lstrip("@")
    return identity


def merge_bot_identity(existing: dict[str, Any] | None, identity: dict[str, str]) -> dict[str, Any]:
    """Replace bot identity fields while preserving any non-bot account keys."""
    merged = dict(existing or {})
    merged.pop("bot_id", None)
    merged.pop("bot_username", None)
    merged.update(identity)
    return merged


async def delete_bot_webhook(bot_token: str) -> None:
    """Best-effort deleteWebhook so Telegram stops POSTing to Talqing."""
    token = _parse_bot_token(bot_token)
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            await client.post(f"https://api.telegram.org/bot{token}/deleteWebhook", json={})
    except (httpx.HTTPError, TelegramBotApiError):
        return


def _telegram_bot_token(integration: Integration, secrets: dict[str, str]) -> str:
    token = resolve_secretish(integration.credentials_ref, secrets)
    if not token:
        raise RuntimeError("Telegram integration credentials_ref did not resolve to a bot token")
    return _parse_bot_token(token)


# ── Channel adapter ─────────────────────────────────────────────────────────


async def _send_telegram_message(
    client: httpx.AsyncClient,
    url: str,
    chat_id: str,
    text: str,
) -> tuple[str, dict[str, Any]]:
    """Send one sendMessage part and return its (message_id, raw response)."""
    response = await client.post(url, json={"chat_id": chat_id, "text": text})

    if not response.is_success:
        if response.status_code == 429 or response.status_code >= 500:
            response.raise_for_status()
        raise RuntimeError(f"provider returned HTTP {response.status_code}")

    try:
        response_data = response.json()
    except ValueError as exc:
        raise RuntimeError("provider returned invalid JSON") from exc
    if not isinstance(response_data, dict):
        raise RuntimeError("provider returned a non-object response")
    if not response_data.get("ok"):
        raise RuntimeError(str(response_data.get("description") or "Telegram sendMessage failed"))

    result = response_data.get("result") if isinstance(response_data.get("result"), dict) else {}
    message_id = str(result.get("message_id") or "").strip()
    if not message_id:
        raise RuntimeError("provider accepted the request without a message ID")
    return message_id, response_data


class TelegramChannelAdapter:
    provider = PROVIDER

    def supports_reply_mode(self, reply_mode: str) -> bool:
        # Bot API sendMessage is always customer-visible in the chat.
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
            raise UnsupportedReplyMode(
                "Telegram does not support internal notes; use public_reply or none"
            )

        chat_id = str(thread_metadata.get("chat_id") or "").strip()
        if not chat_id:
            raise RuntimeError("Telegram thread has no chat_id")

        parts = split_message_text(text, TELEGRAM_MESSAGE_LIMIT)
        if not parts:
            raise RuntimeError("Telegram outbound message has no text to send")

        secrets = await load_secrets(tenant)
        bot_token = _telegram_bot_token(integration, secrets)
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"

        message_ids: list[str] = []
        raw_responses: list[dict[str, Any]] = []
        async with httpx.AsyncClient(timeout=20) as client:
            for part in parts:
                try:
                    message_id, raw_response = await _send_telegram_message(
                        client, url, chat_id, part
                    )
                except Exception as exc:
                    # Parts already in the chat cannot be un-sent; tell the
                    # orchestrator not to retry the whole message.
                    if message_ids:
                        raise PartialSendError(
                            f"Telegram delivered {len(message_ids)} of {len(parts)} "
                            f"message parts before failing: {exc}",
                            sent_message_ids=message_ids,
                        ) from exc
                    raise
                message_ids.append(message_id)
                raw_responses.append(raw_response)

        return OutboundSendResult(
            provider_message_ids=tuple(message_ids),
            raw_responses=tuple(raw_responses),
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
        # Telegram's chat action is addressed by chat, not by the message we
        # are replying to.
        del inbound_provider_message_id

        chat_id = str(thread_metadata.get("chat_id") or "").strip()
        if not chat_id:
            raise RuntimeError("Telegram thread has no chat_id")

        secrets = await load_secrets(tenant)
        bot_token = _telegram_bot_token(integration, secrets)
        url = f"https://api.telegram.org/bot{bot_token}/sendChatAction"
        payload = {"chat_id": chat_id, "action": "typing"}

        # One client and one token resolve for the whole turn; the caller
        # cancels this task when the turn is done.
        async with httpx.AsyncClient(timeout=20) as client:
            while True:
                response = await client.post(url, json=payload)
                response.raise_for_status()
                await asyncio.sleep(TELEGRAM_TYPING_REFRESH_SECONDS)

    async def on_trigger_enabled(
        self,
        ctx: Context,
        *,
        integration: Integration,
        trigger: IntegrationTrigger,
    ) -> IntegrationTrigger | None:
        if trigger.trigger_type != TELEGRAM_MESSAGE_TRIGGER:
            return None

        secrets = await load_secrets(ctx.tenant)
        try:
            token = _telegram_bot_token(integration, secrets)
        except TelegramBotApiError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        secret_token = resolve_secretish(
            integration.webhook_config.get("secret_token"),
            secrets,
        )
        if not secret_token:
            raise HTTPException(
                status_code=400,
                detail="Telegram integration webhook_config.secret_token did not resolve",
            )
        try:
            secret_token = validate_webhook_secret_token(secret_token)
        except TelegramBotApiError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        webhook_url = (
            f"{get_settings().app.api_public_url.rstrip('/')}"
            f"/v1/integrations/webhooks/telegram/{ctx.tenant.id}/{integration.id}"
        )

        try:
            bot_identity = await fetch_bot_identity(token)
        except TelegramBotApiError as exc:
            pool = await ctx.tenant_pool()
            await pool.execute(
                """
                UPDATE integration_triggers
                SET status = 'error',
                    updated_at = now()
                WHERE id = $1 AND tenant_id = $2
                """,
                trigger.id,
                ctx.tenant.id,
            )
            raise HTTPException(
                status_code=400,
                detail=f"Telegram bot identity check failed: {exc}",
            ) from exc

        payload = {
            "url": webhook_url,
            "allowed_updates": ["message"],
            "secret_token": secret_token,
            # Telegram holds up to 24 h of messages while no webhook is set
            # (trigger or integration off) and would replay them here; an agent
            # answering day-old messages is worse than not answering them.
            "drop_pending_updates": True,
        }
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.post(
                    f"https://api.telegram.org/bot{token}/setWebhook",
                    json=payload,
                )
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict) or not data.get("ok"):
                description = (
                    data.get("description") if isinstance(data, dict) else None
                ) or "Telegram setWebhook failed"
                raise RuntimeError(str(description))
        except Exception as exc:
            logger.exception("Telegram webhook registration failed")
            pool = await ctx.tenant_pool()
            await pool.execute(
                """
                UPDATE integration_triggers
                SET status = 'error',
                    updated_at = now()
                WHERE id = $1 AND tenant_id = $2
                """,
                trigger.id,
                ctx.tenant.id,
            )
            raise HTTPException(
                status_code=502,
                detail=f"Telegram webhook registration failed: {exc}",
            ) from exc

        pool = await ctx.tenant_pool()
        account_ids = merge_bot_identity(integration.provider_account_info, bot_identity)
        await pool.execute(
            """
            UPDATE integrations
            SET provider_account_info = $3::jsonb, updated_at = now()
            WHERE id = $1 AND tenant_id = $2
            """,
            integration.id,
            ctx.tenant.id,
            json.dumps(account_ids),
        )
        row = await pool.fetchrow(
            f"""
            UPDATE integration_triggers
            SET status = 'active',
                provider_subscription_ref = $3,
                updated_at = now()
            WHERE id = $1 AND tenant_id = $2
            RETURNING {INTEGRATION_TRIGGER_COLUMNS}
            """,
            trigger.id,
            ctx.tenant.id,
            webhook_url,
        )
        if not row:
            raise HTTPException(status_code=404, detail="integration trigger not found")
        return IntegrationTrigger.from_row(row)

    async def on_trigger_disabled(
        self,
        ctx: Context,
        *,
        integration: Integration,
        trigger_type: str,
    ) -> None:
        if trigger_type != TELEGRAM_MESSAGE_TRIGGER:
            return
        secrets = await load_secrets(ctx.tenant)
        try:
            token = _telegram_bot_token(integration, secrets)
        except (RuntimeError, TelegramBotApiError):
            return
        await delete_bot_webhook(token)

    async def receive_webhook(
        self,
        *,
        tenant: Tenant,
        integration: Integration,
        headers: Mapping[str, str],
        query: Mapping[str, str],
        raw_body: bytes,
    ) -> None:
        del query  # Telegram authenticates with a header.
        secrets = await load_secrets(tenant)
        expected = resolve_secretish(integration.webhook_config.get("secret_token"), secrets)
        if not expected:
            raise HTTPException(
                status_code=403, detail="Telegram webhook secret token is not configured"
            )
        provided = headers.get("x-telegram-bot-api-secret-token")
        if not provided or not hmac.compare_digest(provided, expected):
            raise HTTPException(status_code=403, detail="invalid Telegram webhook token")

        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="invalid JSON webhook payload") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="webhook payload must be an object")
        update_id = str(payload.get("update_id") or "").strip()
        if not update_id:
            raise HTTPException(status_code=400, detail="Telegram update_id is required")

        await mark_webhook_received(tenant, integration.id)
        await _process_telegram_update(tenant, integration, payload)


telegram_adapter = register_channel_adapter(TelegramChannelAdapter())


# ── Inbound ─────────────────────────────────────────────────────────────────


def _telegram_bot_id(integration: Integration, secrets: dict[str, str]) -> str:
    """Return the bot id stored from getMe at connect time.

    secrets is unused; kept so call sites stay uniform with other resolvers.
    Identity must not be re-derived from the token at message time — wrong
    optional account fields used to silently fork provider_thread_key values.
    """
    del secrets
    configured_bot_id = str(integration.provider_account_info.get("bot_id") or "").strip()
    if configured_bot_id:
        return configured_bot_id
    raise RuntimeError(
        "Telegram integration has no bot_id; reconnect the integration so "
        "Talqing can call getMe and store the bot identity"
    )


def _telegram_display_name(user: dict[str, Any]) -> str | None:
    first_name = str(user.get("first_name") or "").strip()
    last_name = str(user.get("last_name") or "").strip()
    username = str(user.get("username") or "").strip()
    full_name = " ".join(part for part in (first_name, last_name) if part).strip()
    if full_name:
        return full_name
    if username:
        return f"@{username}"
    return None


def _is_telegram_bot_authored_message(message: dict[str, Any], bot_id: str) -> bool:
    sender = message.get("from") if isinstance(message.get("from"), dict) else {}
    return str(sender.get("id") or "").strip() == bot_id


async def _resolve_telegram_inbound(
    tenant: Tenant,
    integration: Integration,
    trigger: IntegrationTrigger,
    *,
    update_id: str,
    message: dict[str, Any],
    text: str,
) -> inbound.InboundTurn | None:
    secrets = await load_secrets(tenant)
    bot_id = _telegram_bot_id(integration, secrets)
    chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
    user = message.get("from") if isinstance(message.get("from"), dict) else {}
    chat_id = str(chat.get("id") or "").strip()
    if not chat_id:
        raise RuntimeError("Telegram webhook missing chat.id")
    chat_type = str(chat.get("type") or "").strip()
    if chat_type != "private":
        raise RuntimeError("Telegram MVP only supports private chats")
    if not trigger.agent_id:
        raise RuntimeError("Telegram trigger has no agent_id")

    user_id = str(user.get("id") or "").strip() or chat_id
    username = str(user.get("username") or "").strip() or None
    display_name = _telegram_display_name(user) or username or user_id
    conversation_key = telegram_conversation_key(bot_id=bot_id, chat_id=chat_id)
    provider_message_id = str(message.get("message_id") or "").strip() or None
    # End-customer only (bot_id lives on the integration account).
    customer_metadata: dict[str, Any] = {
        "chat_id": chat_id,
        "user_id": user_id,
    }
    if username:
        customer_metadata["username"] = username
    if display_name:
        customer_metadata["display_name"] = display_name
    userdata_seed = {
        "telegram": {
            "chat_id": chat_id,
            "user_id": user_id,
            **({"username": username} if username else {}),
            **({"display_name": display_name} if display_name else {}),
        }
    }

    item_metadata: dict[str, Any] = {
        "telegram_update_id": update_id,
        "telegram_message_id": provider_message_id,
        **customer_metadata,
    }
    pool = await db.tenant_pool(tenant)
    async with pool.acquire() as conn, conn.transaction():
        return await inbound.receive_chat_message(
            conn,
            tenant_id=tenant.id,
            integration_id=integration.id,
            trigger_id=trigger.id,
            # Text turns always run the active message-trigger agent.
            agent_id=trigger.agent_id,
            conversation_key=conversation_key,
            source="telegram",
            customer_metadata=customer_metadata,
            userdata_seed=userdata_seed,
            text=text,
            provider_message_id=provider_message_id,
            item_metadata=item_metadata,
            raw_payload=message,
        )


async def _process_telegram_update(
    tenant: Tenant,
    integration: Integration,
    update: dict[str, Any],
) -> None:
    try:
        # TODO: We could have done this check in backend/api/dataplane/routes/integrations/provider_webhooks.py itself
        if integration.status != "active":
            return
        trigger = await get_active_trigger(tenant, integration.id, TELEGRAM_MESSAGE_TRIGGER)
        if not trigger:
            return

        message = update.get("message") if isinstance(update.get("message"), dict) else None
        if not message:
            return
        chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
        if str(chat.get("type") or "").strip() != "private":
            return

        secrets = await load_secrets(tenant)
        bot_id = _telegram_bot_id(integration, secrets)
        if _is_telegram_bot_authored_message(message, bot_id):
            return

        if not isinstance(message.get("text"), str):
            logger.error(
                "Unsupported Telegram non-text message (update_id=%s, integration_id=%s): %s",
                update.get("update_id"),
                integration.id,
                json.dumps(message, default=str),
            )
            return
        text = message["text"].strip()
        if not text:
            logger.error(
                "Empty Telegram text message (update_id=%s, integration_id=%s): %s",
                update.get("update_id"),
                integration.id,
                json.dumps(message, default=str),
            )
            return

        turn = await _resolve_telegram_inbound(
            tenant,
            integration,
            trigger,
            update_id=str(update.get("update_id") or ""),
            message=message,
            text=text,
        )
        if turn is None:
            # Telegram retried an update we already stored.
            return
        await publish_ends(turn.ends)
        await inbound.publish_inbound_item_created(
            tenant_id=tenant.id,
            conversation_id=turn.job.conversation_id,
            item=turn.item,
        )
        await publish_turn(turn.job)
    except Exception:
        logger.exception("Telegram webhook event processing failed")
        raise
