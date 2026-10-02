"""Chat use-cases behind ``/v1/chats``: start, read, message, end, token, erase.

A route reaches these as a workspace member or as a browser holding a chat
token, so most take the ``Tenant`` rather than a ``Context``; ``customer_only``
is what a token caller is held to — what the end customer could already see.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import secrets
import time
from collections.abc import AsyncIterator, Mapping
from dataclasses import replace
from typing import Any, Literal
from uuid import UUID

import anyio
import asyncpg
from fastapi import HTTPException
from pydantic import ValidationError

import db
from api.core import security
from api.core.schemas import ErrorBody, JsonObject, Page
from api.core.sse import KEEPALIVE_SECONDS, decode_event, sse
from iredis.client import get_redis
from services import attachments, credits
from services.agents import AgentConfig
from services.agents.plan import load_plan_roster, resolve_call_plan
from services.attachments import ImageRejected
from services.calls.analysis_ops import (
    PreviewAnalysisRequest,
    PreviewAnalysisResponse,
    preview_call_analysis,
    rerun_analysis,
)
from services.calls.service import delete_call
from services.catalog import get_catalog
from services.conversations import events
from services.conversations.models import ConversationItemResponse, ItemCreatedEvent
from services.conversations.refs import ConversationRefConflict, ensure_ref, web_conversation_key
from services.conversations.service import CONVERSATION_ITEM_COLUMNS, _item_out, items_out
from services.messaging import TextTurnJob, publish_turn
from services.session_snapshot import RunFacts, build_health_snapshot
from services.sessions.detail import load_session_reads
from services.sessions.models import (
    AgentMetadata,
    SessionEventResponse,
    analysis_from_row,
    detail_cost,
    summary_cost,
)
from services.user import Context, Tenant
from settings import get_settings

from .models import (
    ChatDetailResponse,
    ChatMessageRequest,
    ChatResponse,
    ChatTokenResponse,
    ChatTurnResponse,
    ChatTurnResultEvent,
    CreateChatRequest,
)
from .open import ChatAgent, end_job, publish_ends, start_chat

logger = logging.getLogger("talqing.services.chats")

# How long a synchronous message waits for its reply before answering `running`.
CHAT_SYNC_WAIT_SECONDS = 120
# Redis frames are best-effort, so the turn's row is re-read this often as well.
_TURN_POLL_SECONDS = 2.0

_OPEN = ("queued", "running")

_CHAT_SELECT = """
    SELECT s.id, s.conversation_id, s.conversation_ref_id, s.agent_id, s.agent_name,
           s.agent_plan, s.status, s.close_reason, s.created_at, s.started_at, s.ended_at,
           s.userdata, s.vars, s.duration_s, s.provider_cost, s.platform_fee, s.total_charge,
           s.pricing_snapshot, s.billing_status, s.analysis_status, s.analysis_skip_reason, s.summary, s.outcome,
           s.outcome_rationale, s.analysis_fields, s.trigger_id, s.integration_id,
           ref.conversation_key AS contact_key,
           av.version AS agent_version, av.config AS version_config
    FROM sessions s
    LEFT JOIN conversation_refs ref
        ON ref.id = s.conversation_ref_id AND ref.tenant_id = s.tenant_id
    LEFT JOIN agent_versions av
        ON av.id = s.agent_version_id AND av.tenant_id = s.tenant_id
"""


def _chat_out(row: asyncpg.Record, *, customer_only: bool = False) -> ChatResponse:
    """One chat row → the API shape.

    A browser holding a chat token is the end customer, so it is not shown what
    the workspace keeps about them: the price, the analysis, the userdata.
    """
    ended = row["status"] not in _OPEN
    userdata = row["userdata"]
    return ChatResponse(
        id=row["id"],
        conversation_id=row["conversation_id"],
        contact_key=row["contact_key"],
        agent=AgentMetadata(agent_id=row["agent_id"], name=row["agent_name"], channel="text"),
        status="ended" if ended else "open",
        close_reason=row["close_reason"],
        created_at=row["created_at"],
        started_at=row["started_at"],
        ended_at=row["ended_at"],
        cost=None if customer_only else summary_cost(row),
        analysis=analysis_from_row(row) if ended and not customer_only else None,
        userdata=userdata if isinstance(userdata, dict) and not customer_only else {},
    )


async def _require_chat(executor, tenant: Tenant, chat_id: UUID) -> asyncpg.Record:
    row = await executor.fetchrow(
        _CHAT_SELECT + " WHERE s.id = $1 AND s.tenant_id = $2 AND s.channel = 'text'",
        chat_id,
        tenant.id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="chat not found")
    return row


def _refuse_channel_chat(chat: asyncpg.Record) -> None:
    """A chat on a messaging channel is written to by the contact, on that channel.

    A message posted here would be answered on the contact's phone — the reply is
    delivered wherever the chat lives — by someone who never wrote to them.
    """
    if chat["integration_id"] is not None:
        raise HTTPException(
            status_code=409,
            detail="this chat belongs to a messaging channel; its messages arrive from there",
        )


# ───────────────────────────── start, read, end ─────────────────────────────


async def create_chat(body: CreateChatRequest, ctx: Context) -> ChatResponse:
    """Start a chat. Nothing is sent until a message is.

    Two bags, as on a call: `userdata` is about the PERSON and is merged into
    their contact record; `vars` is about THIS chat, read-only and gone with it.
    """
    plan = await resolve_call_plan(
        ctx,
        body,
        channels=("text",),
        channel_hint=" - place a voice or video call with POST /v1/calls/token instead",
    )
    entry = plan.entry
    # After the plan resolves, as on a call: an unpublished agent is the more
    # useful error. The only credit check on the API side — the worker makes the
    # other one, each time a chat's warm window has to be rebuilt.
    if not await credits.has_credit(ctx.tenant):
        raise HTTPException(
            status_code=402,
            detail=ErrorBody(message=credits.INSUFFICIENT_CREDITS_MESSAGE, errors=[]).model_dump(),
        )
    contact_key = web_conversation_key(body.contact_key or f"web-chat:{secrets.token_hex(8)}")
    pool = await ctx.tenant_pool()
    async with pool.acquire() as conn, conn.transaction():
        try:
            ref = await ensure_ref(
                conn,
                tenant_id=ctx.tenant.id,
                conversation_key=contact_key,
                kind="web",
                userdata_seed=body.userdata,
            )
        except ConversationRefConflict as exc:
            raise HTTPException(status_code=409, detail=exc.message) from exc
        opened = await start_chat(
            conn,
            tenant_id=ctx.tenant.id,
            ref=ref,
            agent=ChatAgent(
                agent_id=UUID(entry.agent_id) if entry.agent_id else None,
                agent_version_id=UUID(entry.agent_version_id) if entry.agent_version_id else None,
                version=entry.version,
                name=entry.config.name,
                context=entry.config.conversation.context,
                analysis_enabled=entry.config.analysis.enabled,
                plan=plan.stored(),
                vars=plan.vars,
            ),
            source="text_api",
            userdata=body.userdata,
        )
    await publish_ends(opened.ends)
    return _chat_out(await _require_chat(pool, ctx.tenant, opened.session_id))


async def list_chats(
    ctx: Context,
    *,
    limit: int,
    offset: int,
    agent_id: UUID | None,
    contact_key: str | None,
    status: Literal["open", "ended", "all"],
) -> Page[ChatResponse]:
    pool = await ctx.tenant_pool()
    rows = await pool.fetch(
        _CHAT_SELECT
        + """
        WHERE s.tenant_id = $1 AND s.channel = 'text'
            AND ($2::uuid IS NULL OR s.agent_id = $2)
            AND ($3::text IS NULL OR ref.conversation_key = $3)
            AND ($4 = 'all' OR (s.status IN ('queued', 'running')) = ($4 = 'open'))
        ORDER BY s.created_at DESC, s.id DESC
        LIMIT $5 OFFSET $6
        """,
        ctx.tenant.id,
        agent_id,
        (contact_key or "").strip() or None,
        status,
        limit + 1,
        offset,
    )
    return Page[ChatResponse](
        items=[_chat_out(row) for row in rows[:limit]],
        has_more=len(rows) > limit,
        limit=limit,
        offset=offset,
    )


async def get_chat(chat_id: UUID, tenant: Tenant, *, customer_only: bool = False) -> ChatResponse:
    pool = await db.tenant_pool(tenant)
    return _chat_out(await _require_chat(pool, tenant, chat_id), customer_only=customer_only)


async def list_chat_items(
    chat_id: UUID,
    tenant: Tenant,
    *,
    limit: int,
    offset: int,
    order: str,
    customer_only: bool = False,
) -> Page[ConversationItemResponse]:
    pool = await db.tenant_pool(tenant)
    await _require_chat(pool, tenant, chat_id)
    order_sql = "created_at DESC, id DESC" if order == "desc" else "created_at ASC, id ASC"
    rows = await pool.fetch(
        f"""
        SELECT {CONVERSATION_ITEM_COLUMNS}
        FROM conversation_items
        WHERE session_id = $1 AND tenant_id = $2
            AND ($3::boolean IS FALSE OR visibility = 'customer_visible')
        ORDER BY {order_sql}
        LIMIT $4 OFFSET $5
        """,
        chat_id,
        tenant.id,
        customer_only,
        limit + 1,
        offset,
    )
    # Chronological within the page either way; `desc` only picks the newest window.
    page = list(reversed(rows[:limit])) if order == "desc" else rows[:limit]
    return Page[ConversationItemResponse](
        items=await items_out(pool, tenant.id, page, internal=not customer_only),
        has_more=len(rows) > limit,
        limit=limit,
        offset=offset,
    )


async def get_chat_detail(chat_id: UUID, tenant: Tenant) -> ChatDetailResponse:
    """One chat, with its health, priced lines, vars and trace."""
    pool = await db.tenant_pool(tenant)
    chat = await _require_chat(pool, tenant, chat_id)
    reads = await load_session_reads(pool, tenant.id, chat_id)
    return ChatDetailResponse(
        chat=_chat_out(chat),
        snapshot=build_health_snapshot(
            replace(RunFacts.from_session(chat), noun="chat"),
            reads.transcript,
            reads.events,
            reads.usage,
        ),
        cost=detail_cost(chat),
        vars=chat["vars"] or {},
        events=[SessionEventResponse.model_validate(dict(event)) for event in reads.events],
    )


async def rerun_chat_analysis(chat_id: UUID, ctx: Context) -> ChatResponse:
    """Analyse an ended chat again with its agent's saved definition."""
    await rerun_analysis(chat_id, ctx, channels=("text",), noun="chat")
    return await get_chat(chat_id, ctx.tenant)


async def preview_chat_analysis(
    chat_id: UUID, body: PreviewAnalysisRequest, ctx: Context
) -> PreviewAnalysisResponse:
    """Try an analysis definition against an ended chat, saving nothing."""
    return await preview_call_analysis(chat_id, body, ctx, channels=("text",), noun="chat")


async def end_chat(chat_id: UUID, tenant: Tenant, *, customer_only: bool = False) -> ChatResponse:
    """End a chat now. The worker then runs its exit hook, analysis and billing.

    Marked ended here, so the next message is refused at once. Ending a chat that
    has already ended changes nothing.
    """
    pool = await db.tenant_pool(tenant)
    async with pool.acquire() as conn, conn.transaction():
        ended = await conn.fetchrow(
            """
            UPDATE sessions
            SET status = 'completed', ended_at = now(), close_reason = 'api_end',
                updated_at = now()
            WHERE id = $1 AND tenant_id = $2 AND channel = 'text'
                AND status IN ('queued', 'running')
            RETURNING id, conversation_id, agent_id, trigger_id, integration_id,
                      conversation_ref_id
            """,
            chat_id,
            tenant.id,
        )
        if ended is not None:
            # Inside the transaction, so a queue that is down leaves the chat
            # open and the caller free to try again — rather than a chat marked
            # ended that nothing will ever finish.
            try:
                await publish_turn(end_job(tenant.id, ended))
            except Exception as exc:
                logger.exception("could not queue the end of chat %s", chat_id)
                raise HTTPException(
                    status_code=503, detail="text execution queue is unavailable"
                ) from exc
    return _chat_out(await _require_chat(pool, tenant, chat_id), customer_only=customer_only)


async def create_chat_token(chat_id: UUID, ctx: Context) -> ChatTokenResponse:
    pool = await ctx.tenant_pool()
    chat = await _require_chat(pool, ctx.tenant, chat_id)
    if chat["status"] not in _OPEN:
        raise HTTPException(status_code=409, detail="this chat has ended")
    _refuse_channel_chat(chat)
    token, expires_at = security.create_chat_token(str(ctx.tenant.id), str(chat_id))
    return ChatTokenResponse(
        chat_id=chat_id,
        token=token,
        expires_at=expires_at,
        api_url=get_settings().app.api_public_url,
    )


async def delete_chat(chat_id: UUID, ctx: Context) -> None:
    """Erase an ended chat's content. The chat itself stays, with its cost."""
    await delete_call(chat_id, ctx, channels=("text",), noun="chat")


# ───────────────────────────────── messages ─────────────────────────────────


async def _entry_config(conn, tenant: Tenant, chat: asyncpg.Record) -> AgentConfig:
    """The config of the agent this chat entered on, as it last ran."""
    if chat["agent_plan"]:
        try:
            return (await load_plan_roster(conn, tenant.id, chat["agent_plan"]))[0].config
        except (ValueError, ValidationError) as exc:
            raise HTTPException(
                status_code=409, detail=f"this chat's agent plan can no longer run: {exc}"
            ) from exc
    if not isinstance(chat["version_config"], dict):
        raise HTTPException(status_code=409, detail="this chat's agent no longer exists")
    return AgentConfig.model_validate(chat["version_config"])


async def accept_message(
    chat_id: UUID, body: ChatMessageRequest, tenant: Tenant
) -> tuple[asyncpg.Record, TextTurnJob | None]:
    """Store one inbound message and say what still has to be queued for it.

    Returns the inbound item and the job to publish — None when this
    `client_message_id` was already answered, so there is nothing left to run.
    A repeat whose turn is still `running` gets its job back: the first attempt
    may never have reached the queue, and the worker ignores a duplicate.
    """
    message = body.message.strip()
    if not message and not body.images:
        raise HTTPException(status_code=400, detail="a message must carry text, an image, or both")

    pool = await db.tenant_pool(tenant)
    async with pool.acquire() as conn, conn.transaction():
        chat = await _require_chat(conn, tenant, chat_id)
        if chat["status"] not in _OPEN:
            raise HTTPException(status_code=409, detail="this chat has ended")
        _refuse_channel_chat(chat)
        conversation_id = chat["conversation_id"]
        # Locked for an idempotent item insert, and because that lock is what
        # makes the per-conversation image ceiling correct when two messages are
        # sent at once.
        await conn.fetchval(
            "SELECT id FROM conversations WHERE id = $1 AND tenant_id = $2 FOR UPDATE",
            conversation_id,
            tenant.id,
        )

        def job_for(item: asyncpg.Record) -> TextTurnJob:
            return TextTurnJob(
                tenant_id=tenant.id,
                conversation_id=conversation_id,
                input_item_id=item["id"],
                created_at=item["created_at"],
                session_id=chat_id,
                requested_agent_id=chat["agent_id"],
                trigger_id=chat["trigger_id"],
                integration_id=chat["integration_id"],
                conversation_ref_id=chat["conversation_ref_id"],
            )

        existing = await conn.fetchrow(
            f"""
            SELECT {CONVERSATION_ITEM_COLUMNS}
            FROM conversation_items
            WHERE tenant_id = $1 AND conversation_id = $2 AND client_message_id = $3
            """,
            tenant.id,
            conversation_id,
            str(body.client_message_id),
        )
        if existing:
            return existing, job_for(existing) if existing["turn_status"] == "running" else None

        if body.images:
            # Before anything is decoded or uploaded. Refusing afterwards means
            # paying for an image that was always going to be rejected and
            # leaving it in the bucket.
            llm = (await _entry_config(conn, tenant, chat)).llm
            entry = get_catalog().entry("llm", llm.provider, llm.model) if llm else None
            if entry is None or not getattr(entry, "vision", False):
                model = f"{llm.provider}/{llm.model}" if llm else "this agent's model"
                raise HTTPException(
                    status_code=409,
                    detail=f"{model} cannot read images — pick a model that can",
                )

        stored_images: list[attachments.ImageAttachment] = []
        try:
            for image in body.images:
                try:
                    data, declared_mime = attachments.decode_data_url(image.data_url)
                    # The normalized bytes are dropped here on purpose: the turn
                    # runs in the text worker, a different process, which reads
                    # the object back to build the model's view.
                    record, _ = await attachments.ingest_image(
                        conn,
                        tenant,
                        conversation_id,
                        data,
                        declared_mime=declared_mime,
                        filename=image.filename,
                    )
                    stored_images.append(record)
                except ImageRejected as exc:
                    raise HTTPException(status_code=400, detail=str(exc)) from exc

            item = await conn.fetchrow(
                f"""
                INSERT INTO conversation_items (
                    tenant_id, conversation_id, session_id,
                    direction, type, role,
                    agent_id, agent_version,
                    text, attachments, source, visibility, client_message_id, metadata,
                    turn_status
                )
                VALUES ($1, $2, $3, 'inbound', 'message', 'user', $4, $5, $6, $7::jsonb,
                        'text_api', 'customer_visible', $8,
                        '{{"data": {{"origin": "typed"}}}}'::jsonb, 'running')
                RETURNING {CONVERSATION_ITEM_COLUMNS}
                """,
                tenant.id,
                conversation_id,
                chat_id,
                chat["agent_id"],
                chat["agent_version"],
                # NULL rather than '' for a message that is only images, so the
                # inbox lateral and the chat-context rebuilder agree.
                message or None,
                json.dumps([image.model_dump(mode="json") for image in stored_images]),
                str(body.client_message_id),
            )
            await conn.execute(
                """
                UPDATE conversations
                SET last_activity_at = GREATEST(last_activity_at, $3), status = 'active'
                WHERE id = $1 AND tenant_id = $2
                """,
                conversation_id,
                tenant.id,
                item["created_at"],
            )
        except BaseException:
            # The transaction is rolling back, so nothing will point at what was
            # already uploaded — a refused fifth image would otherwise leave the
            # first four in the bucket forever.
            await attachments.discard(stored_images)
            raise

    async with attachments.signer(tenant.id) as sign:
        created = _item_out(item, await sign(item["attachments"]))
    # The frame carries the signed URLs: a dashboard watching this conversation
    # renders the image that was just sent without a reload.
    await events.publish(
        tenant.id, conversation_id, ItemCreatedEvent.model_validate(created.model_dump())
    )
    return item, job_for(item)


async def turn_result(
    tenant: Tenant, chat_id: UUID, item_id: UUID, *, customer_only: bool
) -> ChatTurnResponse:
    """Where a turn stands, read from the database."""
    pool = await db.tenant_pool(tenant)
    input_row = await pool.fetchrow(
        f"SELECT {CONVERSATION_ITEM_COLUMNS} FROM conversation_items "
        "WHERE id = $1 AND tenant_id = $2",
        item_id,
        tenant.id,
    )
    rows = await pool.fetch(
        f"""
        SELECT {CONVERSATION_ITEM_COLUMNS}
        FROM conversation_items
        WHERE trigger_item_id = $1 AND tenant_id = $2 AND conversation_id = $3
            AND ($4::boolean IS FALSE OR visibility = 'customer_visible')
        ORDER BY created_at, id
        """,
        item_id,
        tenant.id,
        input_row["conversation_id"],
        customer_only,
    )
    status = input_row["turn_status"]
    superseded_by = None
    if status == "canceled":
        # Latest-wins: the message that arrived next is the one that took over.
        superseded_by = await pool.fetchval(
            """
            SELECT id FROM conversation_items
            WHERE tenant_id = $1 AND session_id = $2 AND direction = 'inbound'
                AND (created_at, id) > ($3, $4)
            ORDER BY created_at, id
            LIMIT 1
            """,
            tenant.id,
            chat_id,
            input_row["created_at"],
            item_id,
        )
    chat_status = await pool.fetchval(
        "SELECT status FROM sessions WHERE id = $1 AND tenant_id = $2", chat_id, tenant.id
    )
    async with attachments.signer(tenant.id) as sign:
        return ChatTurnResponse(
            input=_item_out(input_row, await sign(input_row["attachments"])),
            status=status,
            error=input_row["turn_error"],
            superseded_by_item_id=superseded_by,
            items=[_item_out(row, await sign(row["attachments"])) for row in rows],
            chat_status="open" if chat_status in _OPEN else "ended",
        )


async def _turn_running(tenant: Tenant, item_id: UUID) -> bool:
    pool = await db.tenant_pool(tenant)
    status = await pool.fetchval(
        "SELECT turn_status FROM conversation_items WHERE id = $1 AND tenant_id = $2",
        item_id,
        tenant.id,
    )
    return status == "running"


async def _open_turn_watch(tenant: Tenant, item: Mapping[str, Any], job: TextTurnJob):
    """Subscribe to the turn's conversation, then queue the turn.

    In that order: a reply published in the gap would otherwise never be
    delivered. Raises the 503 before any stream has begun, so a queue that is
    down is an ordinary HTTP error.
    """
    redis = get_redis()
    # See `api.core.sse.redis_event_stream`: a SUBSCRIBE issued before any other
    # command on a fresh process needs the slot map built first.
    await redis.initialize()
    pubsub = redis.pubsub()
    await pubsub.subscribe(events.channel(tenant.id, item["conversation_id"]))
    try:
        await publish_turn(job)
    except Exception as exc:
        logger.exception(
            "text turn enqueue failed conversation=%s item=%s", item["conversation_id"], item["id"]
        )
        await pubsub.aclose()
        # The turn stays `running`, deliberately: the caller's retry with the
        # same `client_message_id` is what queues it again.
        raise HTTPException(status_code=503, detail="text execution queue is unavailable") from exc
    return pubsub


async def _turn_frames(
    tenant: Tenant, item: Mapping[str, Any], pubsub
) -> AsyncIterator[JsonObject | None]:
    """The conversation's live frames until this turn is over.

    Yields ``None`` on a quiet tick so a streaming caller can keep its
    connection warm. Returns once the turn's row is terminal: frames are
    best-effort, so the row is re-read on every `turn` frame and every couple of
    seconds, and a dropped frame cannot leave a caller waiting.
    """
    try:
        checked = time.monotonic()
        while True:
            message = await pubsub.get_message(
                ignore_subscribe_messages=True, timeout=_TURN_POLL_SECONDS
            )
            frame = decode_event(message) if message is not None else None
            yield frame
            about_this_turn = (
                frame is not None
                and frame["event"] == "turn"
                and frame.get("turn_id") == str(item["id"])
            )
            if about_this_turn or time.monotonic() - checked >= _TURN_POLL_SECONDS:
                checked = time.monotonic()
                if not await _turn_running(tenant, item["id"]):
                    return
    finally:
        # Reached inside a cancelled scope when a streaming client disconnects;
        # without the shield the unsubscribe never runs and the connection leaks.
        with anyio.CancelScope(shield=True), contextlib.suppress(Exception):
            await pubsub.unsubscribe()
            await pubsub.aclose()


async def send_message(
    chat_id: UUID, body: ChatMessageRequest, tenant: Tenant, *, customer_only: bool
) -> ChatTurnResponse:
    """Send a message and wait for the turn it opens."""
    item, job = await accept_message(chat_id, body, tenant)
    if job is not None:
        pubsub = await _open_turn_watch(tenant, item, job)

        async def wait() -> None:
            async for _ in _turn_frames(tenant, item, pubsub):
                pass

        # At the cap the answer says `running`; the reply is still on its way
        # and is read from the chat's items.
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(wait(), CHAT_SYNC_WAIT_SECONDS)
    return await turn_result(tenant, chat_id, item["id"], customer_only=customer_only)


async def stream_message(
    chat_id: UUID, body: ChatMessageRequest, tenant: Tenant, *, customer_only: bool
) -> AsyncIterator[str]:
    """Send a message and return its turn as SSE frames, ending in `turn.result`.

    The message is accepted and queued before the stream is returned, so a
    refusal is an ordinary HTTP error rather than a broken stream. No duration
    cap: the stream stays open until the turn is over.
    """
    item, job = await accept_message(chat_id, body, tenant)
    pubsub = await _open_turn_watch(tenant, item, job) if job is not None else None
    item_id = str(item["id"])

    async def frames() -> AsyncIterator[str]:
        sent = time.monotonic()
        if pubsub is not None:
            async for frame in _turn_frames(tenant, item, pubsub):
                relevant = (
                    frame is not None
                    and frame.get("trigger_item_id") == item_id
                    and (
                        frame["event"].startswith("assistant.")
                        or (
                            frame["event"] == "item.created"
                            and (not customer_only or frame.get("visibility") == "customer_visible")
                        )
                    )
                )
                if relevant:
                    sent = time.monotonic()
                    yield sse(frame)
                elif time.monotonic() - sent >= KEEPALIVE_SECONDS:
                    sent = time.monotonic()
                    yield ": keep-alive\n\n"
        result = await turn_result(tenant, chat_id, item["id"], customer_only=customer_only)
        yield sse(ChatTurnResultEvent.model_validate(result.model_dump()).model_dump(mode="json"))

    return frames()
