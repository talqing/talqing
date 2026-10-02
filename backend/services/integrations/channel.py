"""Channel / trigger adapters — thin Protocol + registry.

OAuth MCP providers live under ``providers/`` with ``*OAuthFlow`` classes.
Trigger-capable messaging/support providers register a ``ChannelAdapter`` here
for:

- outbound delivery
- trigger subscription lifecycle
- inbound provider webhook delivery (POST)

Adding a new channel provider:
1. Implement ``ChannelAdapter`` in ``providers/{name}.py``
2. Self-register via ``register_channel_adapter``
3. Add ``ProviderSpec`` + triggers in ``catalog.py``

HTTP surface::

    POST /v1/integrations/webhooks/{provider}/{tenant_id}/{integration_id}
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol
from uuid import UUID

import db
from services.user import Context, Tenant, load_tenant

from .models import INTEGRATION_COLUMNS, Integration, IntegrationTrigger

# Reply modes that actually call a provider API.
OutboundReplyMode = Literal["public_reply", "internal_note"]
ReplyMode = Literal["public_reply", "internal_note", "none"]


@dataclass(frozen=True, slots=True)
class OutboundSendResult:
    """Successful provider send (public reply or internal note).

    Every chat provider caps a single message (4096 characters on Telegram and WhatsApp), so
    one assistant message may go out as several provider messages. Both tuples
    are ordered and non-empty; ``provider_message_id`` is the first id and is
    what lands in ``conversation_items.provider_message_id``.
    """

    provider_message_ids: tuple[str, ...]
    raw_responses: tuple[dict[str, Any], ...]
    # customer_visible for public_reply; internal for internal_note (no agent_visible).
    visibility: Literal["customer_visible", "internal"]

    @property
    def provider_message_id(self) -> str:
        return self.provider_message_ids[0]


class UnsupportedReplyMode(RuntimeError):
    """Provider cannot deliver this reply_mode (e.g. Telegram has no internal notes)."""


class PartialSendError(RuntimeError):
    """A multi-part message failed after some parts were already delivered.

    Retrying would show the customer the delivered parts twice, so the delivery
    orchestrator marks the item failed instead of returning it to ``pending``.
    """

    def __init__(self, message: str, *, sent_message_ids: list[str]) -> None:
        super().__init__(message)
        self.sent_message_ids = sent_message_ids


def split_message_text(text: str, limit: int) -> list[str]:
    """Split one assistant message into ordered parts that each fit ``limit``.

    Cuts at the last line or word boundary inside the limit so a long reply
    reads as whole paragraphs and sentences rather than mid-word fragments; a
    single unbroken run of characters is cut hard at the limit.

    Length is counted in code points, matching how both providers document
    their limit. Chunking is a transport concern only — the conversation
    timeline keeps one item for the message the model actually produced.
    """
    if limit <= 0:
        raise ValueError("message length limit must be positive")
    remaining = text.strip()
    parts: list[str] = []
    while len(remaining) > limit:
        head = remaining[:limit]
        cut = max(head.rfind("\n"), head.rfind(" "))
        if cut <= 0:
            cut = limit
        parts.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()
    if remaining:
        parts.append(remaining)
    return parts


class ChannelAdapter(Protocol):
    """Outbound delivery, trigger lifecycle, and inbound webhooks."""

    provider: str

    def supports_reply_mode(self, reply_mode: str) -> bool:
        """Whether this provider can deliver the given reply mode via API.

        ``none`` is always handled by the delivery orchestrator (no send) and
        is not asked of adapters. ``public_reply`` should be True for chat
        providers. ``internal_note`` is True only when the provider has a real
        internal-note / private-comment API (ticket platforms).
        """
        ...

    async def send_outbound(
        self,
        *,
        tenant: Tenant,
        integration: Integration,
        thread_metadata: Mapping[str, Any],
        text: str,
        reply_mode: OutboundReplyMode,
    ) -> OutboundSendResult:
        """Call the provider API for a public reply or internal note.

        Implementations must split ``text`` to the provider's message length
        limit (see ``split_message_text``) and send the parts in order.

        Raises:
            UnsupportedReplyMode: mode not supported (orchestrator skips permanently).
            PartialSendError: some parts landed before the failure — not retryable.
            RuntimeError: permanent config/payload errors.
            httpx errors / re-raised transient failures for retry.
        """
        ...

    async def keep_typing(
        self,
        *,
        tenant: Tenant,
        integration: Integration,
        thread_metadata: Mapping[str, Any],
        inbound_provider_message_id: str | None,
    ) -> None:
        """Show "typing…" to the customer and keep it visible until cancelled.

        Run as a background task for the length of a turn and cancelled when the
        turn ends; providers also clear the hint themselves once the reply
        arrives. Providers expire the hint quickly (5s on Telegram), so an
        adapter that can refresh should loop here rather than return — that
        keeps each provider's cadence and its HTTP client in one place. Return
        early when the provider keeps the hint up on its own, or cannot show
        one.

        Failures are logged by the caller and never surface into the turn.
        """
        ...

    async def on_trigger_enabled(
        self,
        ctx: Context,
        *,
        integration: Integration,
        trigger: IntegrationTrigger,
    ) -> IntegrationTrigger | None:
        """Optional managed subscription (e.g. Telegram setWebhook).

        Return the updated ``IntegrationTrigger`` when the adapter mutates the
        trigger row (status/subscription ref). Return ``None`` when no managed
        registration is needed (provider console configured by hand).
        """
        ...

    async def on_trigger_disabled(
        self,
        ctx: Context,
        *,
        integration: Integration,
        trigger_type: str,
    ) -> None:
        """Optional teardown when a trigger is disabled or deleted."""
        ...

    async def receive_webhook(
        self,
        *,
        tenant: Tenant,
        integration: Integration,
        headers: Mapping[str, str],
        query: Mapping[str, str],
        raw_body: bytes,
    ) -> None:
        """Validate and process a provider POST webhook delivery.

        ``query`` is the URL's query string, for providers that authenticate a
        webhook by a token in its URL rather than a header or a signature.

        Raise ``fastapi.HTTPException`` for auth / payload errors. Successful
        processing should return normally (the route answers an empty 204).
        """
        ...


_ADAPTERS: dict[str, ChannelAdapter] = {}
_LOADED = False


def register_channel_adapter(adapter: ChannelAdapter) -> ChannelAdapter:
    _ADAPTERS[adapter.provider] = adapter
    return adapter


def get_channel_adapter(provider: str) -> ChannelAdapter | None:
    _ensure_loaded()
    return _ADAPTERS.get(provider)


def require_channel_adapter(provider: str) -> ChannelAdapter:
    adapter = get_channel_adapter(provider)
    if adapter is None:
        raise RuntimeError(f"no channel adapter for provider {provider!r}")
    return adapter


async def resolve_webhook_integration(
    tenant_id: UUID,
    integration_id: UUID,
    provider: str,
) -> tuple[Tenant, Integration] | None:
    """Load tenant + integration for a public webhook URL, or None if missing."""
    tenant = await load_tenant(tenant_id)
    if tenant is None:
        return None
    pool = await db.tenant_pool(tenant)
    row = await pool.fetchrow(
        f"""
        SELECT {INTEGRATION_COLUMNS}
        FROM integrations
        WHERE id = $1 AND tenant_id = $2 AND provider = $3
        """,
        integration_id,
        tenant.id,
        provider,
    )
    if not row:
        return None
    return tenant, Integration.from_row(row)


async def mark_webhook_received(tenant: Tenant, integration_id: UUID) -> None:
    pool = await db.tenant_pool(tenant)
    await pool.execute(
        """
        UPDATE integrations
        SET last_webhook_received_at = now()
        WHERE id = $1 AND tenant_id = $2
        """,
        integration_id,
        tenant.id,
    )


def _ensure_loaded() -> None:
    """Import provider modules that self-register adapters (idempotent).

    Uses an explicit flag — not ``bool(_ADAPTERS)`` — so a partial import of one
    provider module cannot skip loading the rest.
    """
    global _LOADED
    if _LOADED:
        return
    # Local import avoids an import cycle at package load.
    from services.integrations.providers import telegram as _telegram  # noqa: F401
    from services.integrations.providers import whatsapp as _whatsapp  # noqa: F401

    _LOADED = True
