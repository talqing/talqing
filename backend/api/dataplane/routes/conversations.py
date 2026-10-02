"""Persistent conversation inbox APIs (thin HTTP adapters)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Query

from api.core.schemas import JsonObject, Page
from api.core.sse import EventStreamResponse, event_stream_responses, redis_event_stream
from api.dataplane.deps import Context, CtxDep, WriteCtxDep
from services import conversations
from services.conversations import (
    ConversationEvent,
    ConversationItemResponse,
    ConversationResponse,
    ConversationSessionResponse,
    ConversationSnapshotEvent,
    ConversationTraceResponse,
    PatchConversationRequest,
    channel,
)
from services.session_snapshot import HealthSnapshotResponse

router = APIRouter(prefix="/conversations", tags=["conversations"])


@router.get("", response_model=Page[ConversationResponse])
async def list_conversations(
    ctx: Context = CtxDep,
    limit: int = Query(100, ge=1, le=200),
    offset: int = Query(0, ge=0),
    agent_id: UUID | None = Query(default=None),
    contact_key: str | None = Query(
        default=None,
        description="Only conversations with this contact - their whole history with you.",
    ),
    status: Literal["active", "inactive", "all"] = Query(
        default="active",
        description="`inactive`: we reached out and the contact has not replied or answered yet.",
    ),
) -> Page[ConversationResponse]:
    """List conversations across every channel, most recently active first.

    One conversation is one thread — a web chat, a Telegram thread, or a single
    phone call — with `surface` saying which. Several of them
    can belong to the same person: pass `contact_key` to list only theirs.
    """
    return await conversations.list_conversations(
        ctx,
        limit=limit,
        offset=offset,
        agent_id=agent_id,
        contact_key=contact_key,
        status=status,
    )


@router.get("/{conversation_id}", response_model=ConversationResponse)
async def get_conversation(
    conversation_id: UUID,
    ctx: Context = CtxDep,
) -> ConversationResponse:
    """Read one conversation's header: agent, surface, summary and userdata.

    `userdata` is what the agent knows about the *contact* — what its tools
    published to the `userdata` store, across every conversation with them. For
    what one call ended up knowing, read a session's own `userdata` from
    `list_conversation_sessions`. The messages are in `list_conversation_items`.
    """
    return await conversations.get_conversation(conversation_id, ctx)


@router.get(
    "/{conversation_id}/events",
    response_class=EventStreamResponse,
    responses=event_stream_responses(ConversationEvent),
)
async def conversation_event_stream(
    conversation_id: UUID,
    ctx: Context = CtxDep,
) -> EventStreamResponse:
    """Watch one conversation happen: a snapshot, then every item and turn live.

    Frames: `conversation.snapshot` (the conversation and its items, sent first
    and on every reconnect), `item.created`, `item.delivery_updated`, `turn` and
    `turn.failed` (turn lifecycle), and `assistant.started` / `assistant.delta`
    / `assistant.completed` for the reply as it is generated.

    Nothing here is canonical — a dropped frame is recovered by reconnecting
    (the snapshot re-syncs) or by `list_conversation_items`.
    """
    # Fail-fast 404 before opening the stream.
    await conversations.get_conversation(conversation_id, ctx)

    async def snapshot() -> JsonObject:
        snap = await conversations.get_event_snapshot(conversation_id, ctx)
        return ConversationSnapshotEvent.model_validate(snap.model_dump()).model_dump(mode="json")

    def item_id(data: JsonObject) -> str | None:
        """Only a new item has an id worth resuming from."""
        if data.get("event") == "item.created" and data.get("id") is not None:
            return str(data["id"])
        return None

    return EventStreamResponse(
        redis_event_stream(
            channel(ctx.tenant.id, conversation_id), snapshot=snapshot, event_id=item_id
        )
    )


@router.get("/{conversation_id}/items", response_model=Page[ConversationItemResponse])
async def list_conversation_items(
    conversation_id: UUID,
    ctx: Context = CtxDep,
    limit: int = Query(200, ge=1, le=500),
    offset: int = Query(0, ge=0),
    order: str = Query(
        "asc",
        pattern="^(asc|desc)$",
        description="desc returns the newest window (chronological within the page).",
    ),
) -> Page[ConversationItemResponse]:
    """The messages in a conversation — the transcript of what was said.

    `order=asc` reads from the beginning; `order=desc` returns the newest
    window, still chronological within the page, which is what you want to see
    an agent's latest reply. `direction` distinguishes inbound from outbound
    and `delivery_status` says whether an outbound message actually landed.
    """
    return await conversations.list_conversation_items(
        conversation_id, ctx, limit=limit, offset=offset, order=order
    )


@router.get("/{conversation_id}/sessions", response_model=Page[ConversationSessionResponse])
async def list_conversation_sessions(
    conversation_id: UUID,
    ctx: Context = CtxDep,
    limit: int = Query(100, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> Page[ConversationSessionResponse]:
    """The agent sessions behind a conversation, newest first.

    A thread can be handled many times — each phone call and each chat is one
    session, recording which agent version ran, how it finished and what its
    analysis concluded. A session's id is its `get_call` or `get_chat` id.
    """
    return await conversations.list_conversation_sessions(
        conversation_id, ctx, limit=limit, offset=offset
    )


@router.get("/{conversation_id}/trace", response_model=ConversationTraceResponse)
async def list_conversation_trace(
    conversation_id: UUID,
    ctx: Context = CtxDep,
    after: datetime | None = Query(default=None, description="Only events at or after this."),
    before: datetime | None = Query(default=None, description="Only events before this."),
) -> ConversationTraceResponse:
    """What the platform did during this conversation's sessions, oldest first."""
    return await conversations.list_conversation_trace(
        conversation_id, ctx, after=after, before=before
    )


@router.get("/{conversation_id}/snapshot", response_model=HealthSnapshotResponse)
async def get_conversation_snapshot(
    conversation_id: UUID, ctx: Context = CtxDep
) -> HealthSnapshotResponse:
    """Did this conversation go well? One verdict over the whole thread.

    `issues` names every problem found across every session in the thread —
    errors, failed tools, slow responses, a bad connection — and `ok` is true
    when there were none. Cost and duration sum across sessions; latency and
    tooling come from the merged timeline.
    """
    return await conversations.get_conversation_snapshot(conversation_id, ctx)


@router.patch("/{conversation_id}", response_model=ConversationResponse)
async def patch_conversation(
    conversation_id: UUID,
    body: PatchConversationRequest,
    ctx: Context = WriteCtxDep,
) -> ConversationResponse:
    """Edit a conversation's summary, userdata or metadata.

    Writing `userdata` changes what the agent sees as `{{userdata.field}}` on the
    next turn — the way to correct or preload what it knows from outside a call.
    It belongs to the *contact*, so it reaches every conversation with them, not
    just this one. Each field you send replaces that whole object; omitted fields
    are left alone.
    """
    return await conversations.patch_conversation(conversation_id, body, ctx)
