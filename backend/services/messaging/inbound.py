"""Shared inbound messaging infrastructure.

Provider-specific resolve/process lives on channel adapters under
``services.integrations.providers`` (telegram). This module owns
shared inbound item persistence/publish helpers used by those adapters.
Conversation binding is ``services.conversations.refs``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import asyncpg

from services import conversations
from services.messaging.textq import TextTurnJob


@dataclass(frozen=True)
class InboundTurn:
    """One stored channel message, and the turn to queue for it."""

    # The whole inserted row, so the live `item.created` frame can carry a full
    # ConversationItemResponse rather than a hand-picked subset of it.
    item: Mapping[str, Any]
    job: TextTurnJob
    # Chats this message's chat replaced. Publish before `job`.
    ends: list[TextTurnJob]


async def publish_inbound_item_created(
    *,
    tenant_id: UUID,
    conversation_id: UUID,
    item: Mapping[str, Any],
) -> None:
    """Notify live conversation listeners about a newly persisted inbound item.

    Takes the inserted row so the frame carries a whole `ConversationItemResponse`.
    It used to hand-pick seven keys, which meant this stream sent one shape here,
    another from the worker and a third from the API — while every client typed
    all three as the same thing.
    """
    # Imported here, not at module scope: services.conversations.models reaches
    # services.agents.plan, which reaches this module back through the Telegram
    # provider. A module-level import makes that cycle real at first import.
    from services.conversations.models import ItemCreatedEvent

    await conversations.publish(
        tenant_id, conversation_id, ItemCreatedEvent.model_validate(dict(item))
    )


async def insert_inbound_item(
    conn,
    *,
    tenant_id: UUID,
    conversation_id: UUID,
    session_id: UUID | None,
    agent_id: UUID | None,
    agent_version: int | None,
    text: str,
    provider_message_id: str | None,
    item_metadata: dict[str, Any],
    raw_payload: dict[str, Any],
    missing_error: str,
) -> tuple[asyncpg.Record, bool]:
    """Store one inbound message; ``created`` is False for a provider's redelivery.

    ``session_id`` is the chat it belongs to, and so whether an agent will
    answer it. A message nobody is assigned to answer has no chat and carries no
    turn, so nothing waits on one for ever.
    """
    item = await conn.fetchrow(
        """
        INSERT INTO conversation_items (
            tenant_id, conversation_id, session_id,
            direction, type, role, agent_id, agent_version, text,
            provider_message_id, source,
            visibility, metadata, raw_payload,
            turn_status
        )
        VALUES ($1, $2, $9, 'inbound', 'message', 'user', $3, $4, $5,
                $6, 'provider_webhook', 'customer_visible', $7::jsonb, $8::jsonb,
                CASE WHEN $9::uuid IS NULL THEN NULL ELSE 'running' END)
        ON CONFLICT DO NOTHING
        RETURNING *
        """,
        tenant_id,
        conversation_id,
        agent_id,
        agent_version,
        text,
        provider_message_id,
        # `data.origin`: every user row states how it arrived, as a call's do.
        json.dumps(item_metadata | {"data": {"origin": "typed"}}),
        json.dumps(raw_payload),
        session_id,
    )
    created = item is not None
    if created:
        await conn.execute(
            """
            UPDATE conversations SET last_activity_at = GREATEST(last_activity_at, $3)
            WHERE id = $1 AND tenant_id = $2
            """,
            conversation_id,
            tenant_id,
            item["created_at"],
        )
    if not item and provider_message_id:
        item = await conn.fetchrow(
            """
            SELECT *
            FROM conversation_items
            WHERE tenant_id = $1
                AND conversation_id = $2
                AND provider_message_id = $3
            """,
            tenant_id,
            conversation_id,
            provider_message_id,
        )
    if not item:
        raise RuntimeError(missing_error)
    return item, created


async def already_received(
    conn, *, tenant_id: UUID, conversation_ref_id: UUID, provider_message_id: str | None
) -> bool:
    """Whether this contact's thread already holds this provider message.

    Asked before a chat is opened for it: a provider's redelivery must not start
    a new chat — and under `none`, a new conversation — for a message we stored.
    """
    if not provider_message_id:
        return False
    return await conn.fetchval(
        """
        SELECT EXISTS (
            SELECT 1
            FROM conversation_items ci
            JOIN conversations c ON c.id = ci.conversation_id AND c.tenant_id = ci.tenant_id
            WHERE ci.tenant_id = $1 AND c.conversation_ref_id = $2
                AND ci.provider_message_id = $3
        )
        """,
        tenant_id,
        conversation_ref_id,
        provider_message_id,
    )


async def receive_chat_message(
    conn,
    *,
    tenant_id: UUID,
    integration_id: UUID,
    trigger_id: UUID,
    agent_id: UUID,
    conversation_key: str,
    source: str,
    customer_metadata: Mapping[str, Any],
    userdata_seed: Mapping[str, Any],
    text: str,
    provider_message_id: str | None,
    item_metadata: dict[str, Any],
    raw_payload: dict[str, Any],
) -> InboundTurn | None:
    """Store a channel message an agent will answer, in the chat it belongs to.

    The contact's open chat with the trigger's agent, or a new one — see
    ``services.chats.open``. None for a provider's redelivery of a message we
    already hold. Runs on the caller's transaction.
    """
    # Imported here for the same reason as above: these reach back into this
    # package through the channel adapters.
    from services.chats.open import open_or_join_chat, published_chat_agent
    from services.conversations.refs import ConversationRefConflict, ensure_ref

    try:
        ref = await ensure_ref(
            conn,
            tenant_id=tenant_id,
            conversation_key=conversation_key,
            kind="integration",
            bind_id=integration_id,
            customer_metadata=customer_metadata,
            userdata_seed=userdata_seed,
        )
    except ConversationRefConflict as exc:
        raise RuntimeError(exc.message) from exc
    if await already_received(
        conn,
        tenant_id=tenant_id,
        conversation_ref_id=ref.id,
        provider_message_id=provider_message_id,
    ):
        return None
    agent = await published_chat_agent(conn, tenant_id, agent_id)
    chat = await open_or_join_chat(
        conn,
        tenant_id=tenant_id,
        ref=ref,
        agent=agent,
        source=source,
        userdata=userdata_seed,
        trigger_id=trigger_id,
        integration_id=integration_id,
    )
    item, _ = await insert_inbound_item(
        conn,
        tenant_id=tenant_id,
        conversation_id=chat.conversation_id,
        session_id=chat.session_id,
        agent_id=agent_id,
        agent_version=agent.version,
        text=text,
        provider_message_id=provider_message_id,
        item_metadata=item_metadata,
        raw_payload=raw_payload,
        missing_error=f"{source} inbound item was not inserted and no existing item was found",
    )
    return InboundTurn(
        item=item,
        job=TextTurnJob(
            tenant_id=tenant_id,
            conversation_id=chat.conversation_id,
            input_item_id=item["id"],
            created_at=item["created_at"],
            session_id=chat.session_id,
            requested_agent_id=agent_id,
            trigger_id=trigger_id,
            integration_id=integration_id,
            conversation_ref_id=ref.id,
        ),
        ends=chat.ends,
    )
