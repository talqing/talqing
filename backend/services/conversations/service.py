"""Conversation inbox use-cases (list, read, patch, SSE snapshot).

Request/response shapes: ``models``. Resolve helpers: ``helpers``. SSE bus: ``events``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

import asyncpg
from fastapi import HTTPException

logger = logging.getLogger("talqing.services.conversations")

from api.core.schemas import JsonObject, Page, coalesce
from services import attachments, close_reasons
from services.agents.plan import draft_agent_ids
from services.attachments import ConversationAttachment
from services.billing import (
    AvatarUsage,
    LLMUsage,
    RealtimeUsage,
    SessionUsage,
    STTUsage,
    TTSUsage,
)
from services.session_events import TOOL_ENDED
from services.session_snapshot import HealthSnapshotResponse, RunFacts, build_health_snapshot
from services.sessions.models import AgentMetadata, analysis_from_row, summary_cost
from services.user import Context
from utils.latency import ReplyLatency, reply_breakdowns

from .keys import CUSTOMER_REF_KINDS
from .models import (
    ConversationActivity,
    ConversationEventSnapshot,
    ConversationItemResponse,
    ConversationRefSummary,
    ConversationResponse,
    ConversationSessionResponse,
    ConversationTraceEventResponse,
    ConversationTraceResponse,
    ItemCreatedEvent,
    PatchConversationRequest,
    SessionError,
    SessionMetrics,
)

# Enriched list/detail select: ref + newest session's agent + surface + last message.
# Customer APIs always filter ref.kind to CUSTOMER_REF_KINDS.
# `userdata` comes from the ref, not the conversation: it is what we know about
# the CONTACT, so every thread with the same caller shows the same value.
_CONVERSATION_COLUMNS = """
    c.id, c.summary, c.status,
    c.metadata, c.created_at, c.updated_at, c.tenant_id
"""

# Every column `_item_out` reads. Public because the text worker's INSERT
# RETURNING uses it too: the row it hands to the SSE fan-out must satisfy the
# same response model this SELECT feeds, and one shared list is what keeps a
# new required field from reaching only one of the two.
CONVERSATION_ITEM_COLUMNS = """
    id, conversation_id, session_id, trigger_item_id,
    direction, type, role, agent_id, agent_version, text, attachments,
    provider_message_id, client_message_id, delivery_status, delivery_error,
    turn_status, turn_error, source, visibility, metrics, metadata, raw_payload, created_at, updated_at,
    tenant_id
"""

_SESSION_SELECT = """
    SELECT s.id, s.conversation_id, s.agent_id, s.agent_version_id, s.agent_name,
           s.agent_plan, s.channel, s.type, s.status, s.close_reason, s.trigger_id,
           s.integration_id, s.conversation_ref_id, s.vars, s.metrics, s.error,
           s.duration_s, s.provider_cost, s.platform_fee, s.total_charge, s.billing_status,
           s.analysis_status, s.analysis_skip_reason, s.summary, s.outcome,
           s.outcome_rationale, s.analysis_fields,
           s.started_at, s.ended_at, s.created_at, s.updated_at,
           av.version AS agent_version
    FROM sessions s
    LEFT JOIN agent_versions av
        ON av.id = s.agent_version_id AND av.tenant_id = s.tenant_id
"""

_CONVERSATION_SELECT = f"""
SELECT {_CONVERSATION_COLUMNS},
       ref.id AS ref_id,
       ref.conversation_key AS conversation_key,
       ref.kind AS ref_kind,
       ref.bind_id AS ref_bind_id,
       ref.metadata AS ref_metadata,
       ref.userdata AS ref_userdata,
       last_session.agent_id AS agent_id,
       last_session.agent_name AS agent_name,
       COALESCE(
           NULLIF(btrim(i.provider), ''),
           CASE WHEN ref.kind = 'sip' THEN 'sip' END,
           CASE WHEN ref.kind = 'web' THEN 'web' END,
           NULLIF(btrim(c.metadata->>'source'), ''),
           ref.kind
       ) AS surface,
       last_item.text AS last_message_text,
       last_item.created_at AS last_message_at
FROM conversations c
-- Inner join: every conversation belongs to exactly one identity (invariant 2),
-- so a conversation with no ref would be unreachable history, not a row to show.
JOIN conversation_refs ref
    ON ref.id = c.conversation_ref_id AND ref.tenant_id = c.tenant_id
LEFT JOIN integrations i
    ON ref.kind = 'integration'
    AND i.id = ref.bind_id
    AND i.tenant_id = ref.tenant_id
-- Who answers here: the newest session's agent, whichever channel it ran on.
LEFT JOIN LATERAL (
    SELECT s.agent_id, s.agent_name
    FROM sessions s
    WHERE s.conversation_id = c.id AND s.tenant_id = c.tenant_id
    ORDER BY COALESCE(s.started_at, s.created_at) DESC
    LIMIT 1
) last_session ON true
-- The inbox preview. A message with only images is still the last thing that
-- happened, so it qualifies and renders as a count — `last_message_text` is
-- served to every API consumer, and a null preview under a photo would read as
-- a conversation where nothing was said.
LEFT JOIN LATERAL (
    SELECT COALESCE(
               NULLIF(btrim(text), ''),
               CASE WHEN jsonb_array_length(attachments) = 1 THEN 'Image'
                    ELSE jsonb_array_length(attachments) || ' images' END
           ) AS text,
           created_at
    FROM conversation_items
    WHERE conversation_id = c.id
        AND tenant_id = c.tenant_id
        AND type = 'message'
        AND (btrim(COALESCE(text, '')) <> '' OR jsonb_array_length(attachments) > 0)
    ORDER BY created_at DESC, id DESC
    LIMIT 1
) last_item ON true
"""


# ───────────────────────────── row mappers ──────────────────────────────────


def _json_object(value: object, *, field: str) -> JsonObject:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise TypeError(f"{field} must be a JSON object")
    return dict(value)


def _ref_display_name(metadata: dict[str, Any]) -> str | None:
    # `phone_e164` last: on SIP it is the only name a row has, and without it
    # every SIP conversation rendered nameless in the inbox even though the
    # number was sitting in the ref's metadata. `phone` is the same for
    # WhatsApp: all a thread has until the person writes back.
    for key in (
        "profile_name",
        "display_name",
        "username",
        "wa_id",
        "user_id",
        "chat_id",
        "phone_e164",
        "phone",
    ):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _ref_summary(row: asyncpg.Record | Mapping[str, Any]) -> ConversationRefSummary | None:
    if row["conversation_key"] is None:
        return None
    meta = _json_object(row["ref_metadata"], field="conversation_ref.metadata")
    kind = row["ref_kind"]
    bind_id = row["ref_bind_id"]
    # Internal external-reference key → public contact key. The DB column keeps
    # its name because it also holds CoPilot keys (`agent_copilot:<uuid>`), where
    # "contact" would be a lie; every kind the public API exposes is a contact.
    return ConversationRefSummary(
        contact_key=row["conversation_key"],
        kind=kind,
        bind_id=bind_id,
        integration_id=bind_id if kind == "integration" else None,
        metadata=meta,
        display_name=_ref_display_name(meta),
    )


def _conversation_out(row: asyncpg.Record | Mapping[str, Any]) -> ConversationResponse:
    """One `_CONVERSATION_SELECT` row → the API shape.

    **Every column is required, and indexed directly.** This used to test
    ``if "x" in keys`` for each one against a cached ``row.keys()`` — which on
    an asyncpg Record is a one-shot iterator, not a view, so each test consumed
    it and every column earlier in the SELECT than the last one tested read as
    absent. `contact_key` and `agent_id` were `null` on every conversations
    endpoint for as long as that code existed, and nothing looked broken
    because the value was still there under `row["conversation_key"]`.

    The guards were never load-bearing: all five callers select through
    `_CONVERSATION_SELECT`. Requiring the columns is what makes that true by
    construction — drop one from the SELECT and this raises on the next
    request, instead of quietly serving a null.
    """
    surface = row["surface"]
    last_message_text = row["last_message_text"]
    last_message_at = row["last_message_at"]
    contact_key = row["conversation_key"]
    if isinstance(surface, str):
        surface = surface.strip() or None
    if isinstance(last_message_text, str):
        last_message_text = last_message_text.strip() or None
    return ConversationResponse(
        id=row["id"],
        contact_key=contact_key,
        ref=_ref_summary(row),
        agent_id=row["agent_id"],
        agent_name=row["agent_name"],
        surface=surface,
        status=row["status"],
        last_message_text=last_message_text,
        last_message_at=last_message_at,
        summary=row["summary"],
        userdata=_json_object(row["ref_userdata"], field="conversation_ref.userdata"),
        metadata=_json_object(row["metadata"], field="conversation.metadata"),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _item_out(
    row: asyncpg.Record | Mapping[str, Any],
    attachments_out: list[ConversationAttachment],
    *,
    drafts: Mapping[UUID, set[str]] | None = None,
    latency: ReplyLatency | None = None,
) -> ConversationItemResponse:
    """One item row → the API shape.

    Synchronous, so the presigned links cannot be minted here: the caller signs a
    whole page inside one `attachments.signer()` and passes the result in.

    ``drafts`` is each session's agents that ran their unpublished draft. A
    version is null for a draft and for an inline agent alike, and only the
    session's plan tells them apart.
    """
    metrics = row["metrics"]
    metadata = _json_object(row["metadata"], field="item.metadata")
    agent_version: int | Literal["draft"] | None = row["agent_version"]
    if (
        agent_version is None
        and row["agent_id"]
        and str(row["agent_id"]) in (drafts or {}).get(row["session_id"], ())
    ):
        agent_version = "draft"
    return ConversationItemResponse(
        id=row["id"],
        conversation_id=row["conversation_id"],
        session_id=row["session_id"],
        trigger_item_id=row["trigger_item_id"],
        direction=row["direction"],
        type=row["type"],
        role=row["role"],
        agent_id=row["agent_id"],
        agent_version=agent_version,
        text=row["text"],
        attachments=attachments_out,
        provider_message_id=row["provider_message_id"],
        client_message_id=row["client_message_id"],
        delivery_status=row["delivery_status"],
        delivery_error=row["delivery_error"],
        turn_status=row["turn_status"],
        turn_error=row["turn_error"],
        source=row["source"],
        visibility=row["visibility"],
        metrics=metrics if isinstance(metrics, dict) else None,
        origin=(metadata.get("data") or {}).get("origin"),
        latency=latency,
        metadata=metadata,
        raw_payload=row["raw_payload"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


async def items_out(
    pool: asyncpg.Pool,
    tenant_id: UUID,
    rows: Sequence[asyncpg.Record],
    *,
    events: Sequence[Mapping[str, Any]] | None = None,
    internal: bool = True,
) -> list[ConversationItemResponse]:
    """Stored item rows, oldest first → the API shape, with what a row cannot say.

    Signs the attachments, names a draft as a draft, and attaches each reply's
    latency. ``events`` is the trace when the caller already holds it; otherwise
    the tool endings the latency needs are read here. ``internal=False`` is a
    chat-token caller, who is not shown how the reply was produced.
    """
    session_ids = list({row["session_id"] for row in rows if row["session_id"] is not None})
    plans = await pool.fetch(
        "SELECT id, agent_plan FROM sessions WHERE tenant_id = $1 AND id = ANY($2::uuid[])",
        tenant_id,
        session_ids,
    )
    drafts = {plan["id"]: draft_agent_ids(plan["agent_plan"]) for plan in plans}
    waits: dict[Any, ReplyLatency] = {}
    if internal:
        if events is None:
            events = await pool.fetch(
                "SELECT type, payload, created_at FROM session_events "
                "WHERE tenant_id = $1 AND session_id = ANY($2::uuid[]) AND type = $3",
                tenant_id,
                session_ids,
                TOOL_ENDED,
            )
        waits = reply_breakdowns(rows, events)
    async with attachments.signer(tenant_id) as sign:
        return [
            _item_out(
                row, await sign(row["attachments"]), drafts=drafts, latency=waits.get(row["id"])
            )
            for row in rows
        ]


async def item_created_event(
    tenant_id: UUID, row: asyncpg.Record | Mapping[str, Any]
) -> ItemCreatedEvent:
    """One freshly-inserted item row → the `item.created` frame for it.

    `row` must carry `CONVERSATION_ITEM_COLUMNS`, which is what the text
    worker's INSERT returns. Built through `_item_out` rather than validated
    straight off the row so a missing column names itself here, and so a live
    frame and the same item re-read over REST cannot describe it differently.
    """
    async with attachments.signer(tenant_id) as sign:
        item = _item_out(row, await sign(row["attachments"]))
    return ItemCreatedEvent.model_validate(item.model_dump())


def _session_metrics(value: object) -> SessionMetrics:
    if value is None:
        return SessionMetrics()
    if not isinstance(value, dict):
        raise TypeError("session.metrics must be a JSON object")
    return SessionMetrics.model_validate(value)


def _session_error(value: object) -> SessionError | None:
    if value is None or value == {}:
        return None
    if not isinstance(value, dict):
        raise TypeError("session.error must be a JSON object")
    return SessionError.model_validate(value)


def _empty_usage() -> SessionUsage:
    return SessionUsage()


async def _session_usage_maps(ctx: Context, *, session_ids: list[UUID]) -> dict[UUID, SessionUsage]:
    """Assemble usage from normalized tables (no sessions.usage JSONB)."""
    if not session_ids:
        return {}
    pool = await ctx.tenant_pool()
    out: dict[UUID, SessionUsage] = {sid: SessionUsage() for sid in session_ids}

    def _fields(row: asyncpg.Record) -> dict[str, Any]:
        data = dict(row)
        data.pop("session_id", None)
        return data

    llm_rows = await pool.fetch(
        """
        SELECT session_id, provider, model, input_tokens, input_cached_tokens,
               input_cache_write_tokens, output_tokens, reported_cost, priority
        FROM llm_usage
        WHERE tenant_id = $1 AND session_id = ANY($2::uuid[])
        """,
        ctx.tenant.id,
        session_ids,
    )
    for row in llm_rows:
        out[row["session_id"]].llm.append(LLMUsage.model_validate(_fields(row)))

    tts_rows = await pool.fetch(
        """
        SELECT session_id, provider, model, characters_count, audio_duration,
               input_tokens, output_tokens
        FROM tts_usage
        WHERE tenant_id = $1 AND session_id = ANY($2::uuid[])
        """,
        ctx.tenant.id,
        session_ids,
    )
    for row in tts_rows:
        out[row["session_id"]].tts.append(TTSUsage.model_validate(_fields(row)))

    stt_rows = await pool.fetch(
        """
        SELECT session_id, provider, model, audio_duration, input_tokens, output_tokens,
               reported_cost
        FROM stt_usage
        WHERE tenant_id = $1 AND session_id = ANY($2::uuid[])
        """,
        ctx.tenant.id,
        session_ids,
    )
    for row in stt_rows:
        out[row["session_id"]].stt.append(STTUsage.model_validate(_fields(row)))

    realtime_rows = await pool.fetch(
        """
        SELECT session_id, provider, model, input_text_tokens, input_cached_text_tokens,
               input_audio_tokens, input_cached_audio_tokens, output_text_tokens,
               output_audio_tokens, session_seconds
        FROM realtime_usage
        WHERE tenant_id = $1 AND session_id = ANY($2::uuid[])
        """,
        ctx.tenant.id,
        session_ids,
    )
    for row in realtime_rows:
        out[row["session_id"]].realtime.append(RealtimeUsage.model_validate(_fields(row)))

    avatar_rows = await pool.fetch(
        """
        SELECT session_id, provider, model, avatar_id, avatar_session_id, seconds
        FROM avatar_usage
        WHERE tenant_id = $1 AND session_id = ANY($2::uuid[])
        """,
        ctx.tenant.id,
        session_ids,
    )
    for row in avatar_rows:
        out[row["session_id"]].avatar.append(AvatarUsage.model_validate(_fields(row)))
    return out


def _session_out(
    row: asyncpg.Record | Mapping[str, Any],
    *,
    usage: SessionUsage | None = None,
) -> ConversationSessionResponse:
    version: int | Literal["draft"] | None = row["agent_version"]
    if version is None and str(row["agent_id"]) in draft_agent_ids(row["agent_plan"]):
        version = "draft"
    return ConversationSessionResponse(
        id=row["id"],
        conversation_id=row["conversation_id"],
        trigger_id=row["trigger_id"],
        integration_id=row["integration_id"],
        conversation_ref_id=row["conversation_ref_id"],
        agent=AgentMetadata(
            agent_id=row["agent_id"], name=row["agent_name"], channel=row["channel"]
        ),
        agent_version_id=row["agent_version_id"],
        agent_version=version,
        type=row["type"],
        channel=row["channel"],
        status=row["status"],
        close_reason=row["close_reason"],
        close_reason_label=(
            close_reasons.describe(row["close_reason"]) if row["close_reason"] else None
        ),
        vars=_json_object(row["vars"], field="session.vars"),
        analysis=analysis_from_row(row),
        usage=usage if usage is not None else _empty_usage(),
        cost=summary_cost(row),
        duration_s=row["duration_s"],
        metrics=_session_metrics(row["metrics"]),
        error=_session_error(row["error"]),
        started_at=row["started_at"],
        ended_at=row["ended_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


# ───────────────────────────── internal loads ───────────────────────────────


async def _require_conversation(ctx: Context, conversation_id: UUID) -> asyncpg.Record:
    """Load an enriched customer conversation row for this tenant, or raise 404.

    Platform CoPilot kinds are never exposed here.
    """
    pool = await ctx.tenant_pool()
    row = await pool.fetchrow(
        _CONVERSATION_SELECT
        + """
        WHERE c.id = $1 AND c.tenant_id = $2
            AND ref.kind = ANY($3::text[])
        """,
        conversation_id,
        ctx.tenant.id,
        list(CUSTOMER_REF_KINDS),
    )
    if not row:
        raise HTTPException(status_code=404, detail="conversation not found")
    return row


# ───────────────────────────── use-case API ─────────────────────────────────


async def list_conversations(
    ctx: Context,
    *,
    limit: int = 100,
    offset: int = 0,
    agent_id: UUID | None = None,
    contact_key: str | None = None,
    status: Literal["active", "inactive", "all"] = "active",
) -> Page[ConversationResponse]:
    """The inbox: one row per conversation, newest activity first.

    ``contact_key`` narrows it to one caller or endpoint — every conversation
    ever had with them, which after "a new conversation per call" is how a
    tenant reaches a phone number's whole history.

    ``status`` defaults to ``active``, which is what keeps outreach nobody
    answered out of every caller's list; ``all`` is how a ``contact_key`` lookup
    sees what was sent too.
    """
    pool = await ctx.tenant_pool()
    rows = await pool.fetch(
        _CONVERSATION_SELECT
        + """
        WHERE c.tenant_id = $3
            AND ref.kind = ANY($5::text[])
            AND (
                $4::uuid IS NULL
                OR EXISTS (
                    SELECT 1 FROM sessions s
                    WHERE s.conversation_id = c.id AND s.tenant_id = c.tenant_id
                        AND s.agent_id = $4
                )
            )
            AND ($6::text IS NULL OR ref.conversation_key = $6)
            AND ($7 = 'all' OR c.status = $7)
        ORDER BY c.last_activity_at DESC
        LIMIT $1 OFFSET $2
        """,
        limit + 1,
        offset,
        ctx.tenant.id,
        agent_id,
        list(CUSTOMER_REF_KINDS),
        (contact_key or "").strip() or None,
        status,
    )
    has_more = len(rows) > limit
    return Page[ConversationResponse](
        items=[_conversation_out(row) for row in rows[:limit]],
        has_more=has_more,
        limit=limit,
        offset=offset,
    )


async def get_conversation(
    conversation_id: UUID,
    ctx: Context,
) -> ConversationResponse:
    row = await _require_conversation(ctx, conversation_id)
    return _conversation_out(row)


async def get_event_snapshot(
    conversation_id: UUID,
    ctx: Context,
) -> ConversationEventSnapshot:
    """Canonical conversation + items for the SSE reconnect frame (404 if missing)."""
    conversation = await _require_conversation(ctx, conversation_id)
    pool = await ctx.tenant_pool()
    # Activity: a turn is being answered. Not "a session is running" — an open
    # chat is running for days — and not "newest item is a user message", which
    # reads idle the moment a tool call lands mid-turn.
    has_running = await pool.fetchval(
        """
        SELECT EXISTS (
            SELECT 1
            FROM conversation_items
            WHERE tenant_id = $1
                AND conversation_id = $2
                AND direction = 'inbound'
                AND turn_status = 'running'
        )
        """,
        ctx.tenant.id,
        conversation_id,
    )
    # Same item set as GET .../items and live item.created fan-out
    # Full LiveKit timeline. Do not filter by visibility — tools are internal
    # but must survive reconnect.
    rows = await pool.fetch(
        f"""
        SELECT {CONVERSATION_ITEM_COLUMNS}
        FROM conversation_items
        WHERE conversation_id = $1 AND tenant_id = $2
        ORDER BY created_at, id
        """,
        conversation_id,
        ctx.tenant.id,
    )
    return ConversationEventSnapshot(
        conversation=_conversation_out(conversation),
        items=await items_out(pool, ctx.tenant.id, rows),
        activity=ConversationActivity(state="running" if has_running else "idle"),
    )


async def list_conversation_items(
    conversation_id: UUID,
    ctx: Context,
    *,
    limit: int = 200,
    offset: int = 0,
    order: str = "asc",
) -> Page[ConversationItemResponse]:
    if order not in ("asc", "desc"):
        raise HTTPException(status_code=400, detail="order must be asc or desc")
    await _require_conversation(ctx, conversation_id)
    pool = await ctx.tenant_pool()
    order_sql = "created_at DESC, id DESC" if order == "desc" else "created_at ASC, id ASC"
    rows = await pool.fetch(
        f"""
        SELECT {CONVERSATION_ITEM_COLUMNS}
        FROM conversation_items
        WHERE conversation_id = $3 AND tenant_id = $4
        ORDER BY {order_sql}
        LIMIT $1 OFFSET $2
        """,
        limit + 1,
        offset,
        conversation_id,
        ctx.tenant.id,
    )
    has_more = len(rows) > limit
    page = rows[:limit]
    # Always return chronological order in the page payload so clients can
    # render top→bottom; desc only changes which window of history is loaded.
    if order == "desc":
        page = list(reversed(page))
    return Page[ConversationItemResponse](
        items=await items_out(pool, ctx.tenant.id, page),
        has_more=has_more,
        limit=limit,
        offset=offset,
    )


async def list_conversation_sessions(
    conversation_id: UUID,
    ctx: Context,
    *,
    limit: int = 100,
    offset: int = 0,
) -> Page[ConversationSessionResponse]:
    await _require_conversation(ctx, conversation_id)
    pool = await ctx.tenant_pool()
    rows = await pool.fetch(
        _SESSION_SELECT
        + """
        WHERE s.conversation_id = $3 AND s.tenant_id = $4
        ORDER BY COALESCE(s.started_at, s.created_at) DESC
        LIMIT $1 OFFSET $2
        """,
        limit + 1,
        offset,
        conversation_id,
        ctx.tenant.id,
    )
    has_more = len(rows) > limit
    page_rows = rows[:limit]
    session_ids = [row["id"] for row in page_rows]
    usage_by_session = await _session_usage_maps(ctx, session_ids=session_ids)
    return Page[ConversationSessionResponse](
        items=[_session_out(row, usage=usage_by_session.get(row["id"])) for row in page_rows],
        has_more=has_more,
        limit=limit,
        offset=offset,
    )


async def list_conversation_trace(
    conversation_id: UUID,
    ctx: Context,
    *,
    after: datetime | None = None,
    before: datetime | None = None,
) -> ConversationTraceResponse:
    """What the platform did during a conversation's sessions, oldest first.

    Per-call transport readings are left out: a row per connection-quality
    sample would bury a thread, and the call's own page has them.
    """
    await _require_conversation(ctx, conversation_id)
    pool = await ctx.tenant_pool()
    rows = await pool.fetch(
        """
        SELECT e.session_id, e.seq, e.type, e.payload, e.created_at
        FROM session_events e
        JOIN sessions s ON s.id = e.session_id AND s.tenant_id = e.tenant_id
        WHERE s.conversation_id = $1 AND e.tenant_id = $2
            AND e.type NOT LIKE 'telemetry.rtc.%' AND e.type NOT LIKE 'livekit.participant.%'
            AND ($3::timestamptz IS NULL OR e.created_at >= $3)
            AND ($4::timestamptz IS NULL OR e.created_at < $4)
        ORDER BY e.created_at, e.seq
        """,
        conversation_id,
        ctx.tenant.id,
        after,
        before,
    )
    return ConversationTraceResponse(
        events=[ConversationTraceEventResponse.model_validate(dict(row)) for row in rows]
    )


async def get_conversation_snapshot(
    conversation_id: UUID,
    ctx: Context,
) -> HealthSnapshotResponse:
    """The same health verdict a call gets, folded over the whole thread.

    A conversation spans one or more runs — a chat, a phone call that continued
    it, a handoff. Latency and tooling are read from the merged
    timeline; cost and duration sum across the runs.
    """
    await _require_conversation(ctx, conversation_id)
    pool = await ctx.tenant_pool()
    runs = await pool.fetch(
        """
        SELECT id, status, close_reason, duration_s, total_charge, billing_status
        FROM sessions WHERE conversation_id = $1 AND tenant_id = $2
        """,
        conversation_id,
        ctx.tenant.id,
    )
    session_ids = [row["id"] for row in runs]
    transcript, events_rows, llm, tts, stt, realtime, avatar = await asyncio.gather(
        pool.fetch(
            """
            SELECT id, type, role, created_at, metrics, metadata
            FROM conversation_items
            WHERE conversation_id = $1 AND tenant_id = $2
            ORDER BY created_at, id
            """,
            conversation_id,
            ctx.tenant.id,
        ),
        pool.fetch(
            "SELECT seq, type, payload, created_at FROM session_events "
            "WHERE session_id = ANY($1::uuid[]) AND tenant_id = $2 ORDER BY created_at, seq",
            session_ids,
            ctx.tenant.id,
        ),
        # The five tables carry different columns and the snapshot reads them by
        # name, so `*` is the honest select here — spelling out five distinct
        # column lists would only be a second place to keep them in sync.
        *[
            pool.fetch(
                f"SELECT * FROM {table} WHERE session_id = ANY($1::uuid[]) AND tenant_id = $2",
                session_ids,
                ctx.tenant.id,
            )
            for table in ("llm_usage", "tts_usage", "stt_usage", "realtime_usage", "avatar_usage")
        ],
    )
    return build_health_snapshot(
        RunFacts.from_runs(runs),
        transcript,
        events_rows,
        {"llm": llm, "tts": tts, "stt": stt, "realtime": realtime, "avatar": avatar},
    )


async def patch_conversation(
    conversation_id: UUID,
    body: PatchConversationRequest,
    ctx: Context,
) -> ConversationResponse:
    current = await _require_conversation(ctx, conversation_id)
    pool = await ctx.tenant_pool()

    next_metadata = coalesce(body.metadata, current["metadata"] or {})
    await pool.execute(
        """
        UPDATE conversations
        SET summary = $3,
            metadata = $4::jsonb,
            updated_at = now()
        WHERE id = $1 AND tenant_id = $2
        """,
        conversation_id,
        ctx.tenant.id,
        coalesce(body.summary, current["summary"]),
        json.dumps(next_metadata),
    )
    if body.userdata is not None:
        # The contact's bag, so this reaches every conversation with this caller
        # — and the next call, when the agent is set to initialize from it.
        await pool.execute(
            """
            UPDATE conversation_refs
            SET userdata = $3::jsonb, updated_at = now()
            WHERE id = $1 AND tenant_id = $2
            """,
            current["ref_id"],
            ctx.tenant.id,
            json.dumps(body.userdata),
        )
    refreshed = await pool.fetchrow(
        _CONVERSATION_SELECT + " WHERE c.id = $1 AND c.tenant_id = $2",
        conversation_id,
        ctx.tenant.id,
    )
    return _conversation_out(refreshed)
