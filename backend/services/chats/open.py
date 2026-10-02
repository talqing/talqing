"""Starting a chat, or joining the open one.

Shared by ``POST /v1/chats`` and the channel adapters (Telegram, WhatsApp), so
the rules about which conversation a chat writes to and which chats it replaces
exist once. Everything here runs on the caller's transaction, right after
``ensure_ref`` — whose row lock on the contact is what serializes two messages
racing to open the same chat.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from services.conversation_context import ConversationContext
from services.conversations.refs import ConversationRef, open_conversation
from services.messaging import TextTurnJob, publish_turn

logger = logging.getLogger("talqing.services.chats")


@dataclass(frozen=True, slots=True)
class ChatAgent:
    """What a new chat runs: its entry agent, and the cast when it has one."""

    # None for an agent defined in the request that started the chat.
    agent_id: UUID | None
    agent_version_id: UUID | None
    version: int | None
    name: str
    context: ConversationContext
    analysis_enabled: bool
    plan: Mapping[str, Any] | None = None
    vars: Mapping[str, str] | None = None


@dataclass(frozen=True, slots=True)
class OpenedChat:
    session_id: UUID
    conversation_id: UUID
    # False when a channel message joined a chat that was already open.
    created: bool
    # One `end` job per chat this one replaced. Publish them after the commit and
    # before anything for the new chat (``publish_ends``).
    ends: list[TextTurnJob] = field(default_factory=list)


async def published_chat_agent(conn, tenant_id: UUID, agent_id: UUID) -> ChatAgent:
    """A channel trigger's agent, at its published version."""
    row = await conn.fetchrow(
        """
        SELECT av.id, av.version, av.config->>'name' AS name,
               av.config->'conversation'->>'context' AS context,
               (av.config->'analysis'->>'enabled')::boolean AS analysis_enabled
        FROM agents a
        JOIN agent_versions av
            ON av.agent_id = a.id
            AND av.version = a.published_version
            AND av.tenant_id = a.tenant_id
        WHERE a.id = $1 AND a.tenant_id = $2
        """,
        agent_id,
        tenant_id,
    )
    if row is None:
        raise RuntimeError(f"agent {agent_id} has no published version")
    return ChatAgent(
        agent_id=agent_id,
        agent_version_id=row["id"],
        version=row["version"],
        name=row["name"],
        context=row["context"],
        analysis_enabled=row["analysis_enabled"],
    )


def end_job(tenant_id: UUID, chat: Mapping[str, Any]) -> TextTurnJob:
    """The job that has the worker finish a chat the API just marked ended.

    Carries the chat's channel linkage so whatever its exit hook says can still
    be delivered to the contact.
    """
    return TextTurnJob(
        kind="end",
        tenant_id=tenant_id,
        conversation_id=chat["conversation_id"],
        session_id=chat["id"],
        created_at=datetime.now(UTC),
        requested_agent_id=chat["agent_id"],
        trigger_id=chat["trigger_id"],
        integration_id=chat["integration_id"],
        conversation_ref_id=chat["conversation_ref_id"],
    )


async def publish_ends(ends: list[TextTurnJob]) -> None:
    """Hand replaced chats to the worker. Never raises: the new chat has already
    committed, and a replaced one left unfinished only goes without its analysis
    and its `session.completed`."""
    for job in ends:
        try:
            await publish_turn(job)
        except Exception:
            logger.exception("could not queue the end of replaced chat %s", job.session_id)


async def _conversation_for_new_chat(
    conn,
    *,
    tenant_id: UUID,
    ref: ConversationRef,
    context: ConversationContext,
    source: str,
    awaiting_first_message: bool,
) -> UUID:
    """The conversation a new chat writes to.

    The voice rule (``open_conversation``), with one addition: a newest
    conversation that holds only our own outreach — a WhatsApp template nobody
    has answered — is joined whatever ``context`` says. Otherwise the agent
    would not see the message it is replying to.
    """
    outreach = await conn.fetchval(
        """
        SELECT c.id
        FROM conversations c
        WHERE c.tenant_id = $1 AND c.conversation_ref_id = $2
            AND NOT EXISTS (
                SELECT 1 FROM sessions s
                WHERE s.conversation_id = c.id AND s.tenant_id = c.tenant_id
            )
            AND NOT EXISTS (
                SELECT 1 FROM conversations newer
                WHERE newer.tenant_id = c.tenant_id
                    AND newer.conversation_ref_id = c.conversation_ref_id
                    AND (newer.created_at, newer.id) > (c.created_at, c.id)
            )
        """,
        tenant_id,
        ref.id,
    )
    return await open_conversation(
        conn,
        tenant_id=tenant_id,
        ref=ref,
        context="transcript" if outreach is not None else context,
        source=source,
        # A chat started over the API has nobody in it yet. Its conversation
        # stays out of the inbox, as unanswered outreach does, until the first
        # message arrives (`service.accept_message`).
        outbound=awaiting_first_message,
    )


async def start_chat(
    conn,
    *,
    tenant_id: UUID,
    ref: ConversationRef,
    agent: ChatAgent,
    source: str,
    userdata: Mapping[str, Any] | None = None,
    trigger_id: UUID | None = None,
    integration_id: UUID | None = None,
) -> OpenedChat:
    """Open a new chat, ending whichever open ones it replaces.

    A contact holds at most one open chat per entry agent and a conversation at
    most one open chat, so starting one ends the contact's open chat with this
    same agent and any other open chat in the conversation this one lands in.
    They are marked ended here — which is what frees both unique indexes for the
    insert below — and finished by the worker.

    ``userdata`` is what the chat starts knowing: the session's own bag, kept
    beside the contact's so an agent that does not initialize from the contact
    still gets what the request sent.
    """
    conversation_id = await _conversation_for_new_chat(
        conn,
        tenant_id=tenant_id,
        ref=ref,
        context=agent.context,
        source=source,
        # A channel chat is opened BY a message; only the API opens an empty one.
        awaiting_first_message=integration_id is None,
    )
    replaced = await conn.fetch(
        """
        UPDATE sessions
        SET status = 'completed', ended_at = now(), close_reason = 'replaced', updated_at = now()
        WHERE tenant_id = $1 AND channel = 'text' AND status IN ('queued', 'running')
            AND (
                (conversation_ref_id = $2 AND agent_id IS NOT DISTINCT FROM $3)
                OR conversation_id = $4
            )
        RETURNING id, conversation_id, agent_id, trigger_id, integration_id, conversation_ref_id
        """,
        tenant_id,
        ref.id,
        agent.agent_id,
        conversation_id,
    )
    session_id = uuid4()
    await conn.execute(
        """
        INSERT INTO sessions (
            id, tenant_id, conversation_id, conversation_ref_id, agent_id, agent_version_id,
            agent_name, channel, type, status, billing_status, analysis_status,
            trigger_id, integration_id, agent_plan, vars, userdata
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, 'text', 'TEXT', 'queued', 'pending', $8,
                $9, $10, $11::jsonb, $12::jsonb, $13::jsonb)
        """,
        session_id,
        tenant_id,
        conversation_id,
        ref.id,
        agent.agent_id,
        agent.agent_version_id,
        agent.name,
        # Claimed up front, as a call's is: 'none' must only ever mean "off".
        "pending" if agent.analysis_enabled else "none",
        trigger_id,
        integration_id,
        json.dumps(dict(agent.plan)) if agent.plan is not None else None,
        json.dumps(dict(agent.vars)) if agent.vars else None,
        json.dumps(dict(userdata or {})),
    )
    return OpenedChat(
        session_id=session_id,
        conversation_id=conversation_id,
        created=True,
        ends=[end_job(tenant_id, row) for row in replaced],
    )


async def open_or_join_chat(
    conn,
    *,
    tenant_id: UUID,
    ref: ConversationRef,
    agent: ChatAgent,
    source: str,
    userdata: Mapping[str, Any] | None,
    trigger_id: UUID,
    integration_id: UUID,
) -> OpenedChat:
    """The chat a channel message belongs to: the contact's open one, or a new one.

    A message joins the open chat only while the channel's trigger still runs
    the agent that chat started with. Once the tenant has pointed the trigger at
    another agent, the old chat is replaced and this message starts a new one.
    """
    joined = await conn.fetchrow(
        """
        SELECT id, conversation_id
        FROM sessions
        WHERE tenant_id = $1 AND conversation_ref_id = $2 AND agent_id = $3
            AND channel = 'text' AND status IN ('queued', 'running')
        """,
        tenant_id,
        ref.id,
        agent.agent_id,
    )
    if joined is not None:
        return OpenedChat(
            session_id=joined["id"], conversation_id=joined["conversation_id"], created=False
        )
    # The trigger's agent has no open chat here; any other agent's is the one
    # the tenant switched away from.
    switched = await conn.fetch(
        """
        UPDATE sessions
        SET status = 'completed', ended_at = now(), close_reason = 'replaced', updated_at = now()
        WHERE tenant_id = $1 AND conversation_ref_id = $2
            AND channel = 'text' AND status IN ('queued', 'running')
        RETURNING id, conversation_id, agent_id, trigger_id, integration_id, conversation_ref_id
        """,
        tenant_id,
        ref.id,
    )
    opened = await start_chat(
        conn,
        tenant_id=tenant_id,
        ref=ref,
        agent=agent,
        source=source,
        userdata=userdata,
        trigger_id=trigger_id,
        integration_id=integration_id,
    )
    return OpenedChat(
        session_id=opened.session_id,
        conversation_id=opened.conversation_id,
        created=True,
        ends=[*(end_job(tenant_id, row) for row in switched), *opened.ends],
    )
