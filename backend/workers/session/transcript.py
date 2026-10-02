"""Conversation item hydration and persistence."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from livekit.agents import llm

import db
from services import attachments
from services.attachments import ImageAttachment
from services.tools import HandoffTarget
from services.user import Tenant

logger = logging.getLogger("talqing.workers.session")

from workers.session._util import _jsonable_str, _string_list, item_row
from workers.session.runtime_kind import item_source_for_session_type

# What the model is told where an image used to be. Lives ONLY in the chat
# context: it is never written to `conversation_items`, so the transcript a human
# reads still shows the picture. The archive is complete, the working set is
# bounded, and the two are allowed to differ.
#
# A correctness requirement, not a nicety. An image-only message whose image was
# dropped would become a ChatMessage with empty content — which the Google format
# emits as a turn with no parts — and the agent would have no idea a photo was
# ever sent, rather than being able to ask for it again.
IMAGE_ELIDED_PLACEHOLDER = "[image no longer in view: {name}]"


async def _one_image(read: attachments.Reader, item: ImageAttachment) -> llm.ImageContent | str:
    """One stored image as a data URL, or the placeholder if it will not load.

    An object the bucket will not serve becomes the placeholder rather than being
    dropped, so the agent can say "I can't see that image any more, could you
    send it again?" instead of having no idea a photo was ever sent.
    """
    try:
        raw = await read(item.object_key)
    except Exception:
        logger.warning("image %s could not be loaded into the chat context", item.id, exc_info=True)
        return IMAGE_ELIDED_PLACEHOLDER.format(name=item.filename or "image")
    return llm.ImageContent(
        image=attachments.data_url(item.mime_type, raw), inference_detail="auto"
    )


async def image_contents(
    items: Sequence[ImageAttachment],
) -> list[llm.ImageContent | str]:
    """Load a batch of stored images into chat content, in one S3 client.

    There is no cap on what a caller may pass: a cold start on a long thread
    hands over every photo the conversation has ever held, which is the known
    cost of this feature. Concurrent, so it is
    one round trip's latency rather than N.
    """
    if not items:
        return []
    async with attachments.reader() as read:
        return list(await asyncio.gather(*(_one_image(read, item) for item in items)))


async def _hydrate_images(rows: Sequence[object]) -> dict[UUID, list[llm.ImageContent | str]]:
    """Every image in a loaded window, keyed by the row it belongs to."""
    flat: list[tuple[UUID, ImageAttachment]] = [
        (row["id"], item) for row in rows for item in attachments.stored(row["attachments"])
    ]
    loaded = await image_contents([item for _, item in flat])
    out: dict[UUID, list[llm.ImageContent | str]] = {}
    for (row_id, _), content in zip(flat, loaded, strict=True):
        out.setdefault(row_id, []).append(content)
    return out


def _conversation_item_to_chat_item(
    row: object, images: list[llm.ImageContent | str] | None = None
) -> llm.ChatItem | None:
    """Rebuild one LiveKit ChatItem from a conversation_items row.

    ``conversation_items.type`` mirrors LiveKit ChatItem discriminators:
    message | function_call | function_call_output | agent_handoff | agent_config_update.

    Uses metadata.livekit_item_id when present so reloaded history keeps stable
    ids; otherwise falls back to a deterministic ci_<uuid> id.

    ``images`` is what `_hydrate_images` already fetched for this row; a message
    is valid when it has text OR images, so a photo sent with no caption replays
    as the turn it was.
    """
    item_type = row["type"]
    metadata = row["metadata"] or {}
    if not isinstance(metadata, dict):
        metadata = {}
    data = metadata.get("data") if isinstance(metadata.get("data"), dict) else {}
    livekit_id = metadata.get("livekit_item_id")
    item_id = str(livekit_id) if isinstance(livekit_id, str) and livekit_id else f"ci_{row['id']}"
    created_at = row["created_at"]
    created_ts = created_at.timestamp() if hasattr(created_at, "timestamp") else None

    if item_type == "message":
        role = row["role"]
        text = row["text"]
        if role not in ("user", "assistant", "system", "developer"):
            return None
        content: list[llm.ChatContent] = [
            *([text] if isinstance(text, str) and text else []),
            *(images or []),
        ]
        if not content:
            return None
        kwargs: dict[str, object] = {"id": item_id, "role": role, "content": content}
        if created_ts is not None:
            kwargs["created_at"] = created_ts
        interrupted = bool(data.get("interrupted")) if data else False
        if interrupted:
            kwargs["interrupted"] = True
        return llm.ChatMessage(**kwargs)

    if item_type == "function_call":
        call_id = data.get("call_id")
        name = data.get("name")
        if call_id is None or name is None:
            raise ValueError("function_call item missing call_id or name")
        kwargs = {
            "id": item_id,
            "call_id": str(call_id),
            "name": str(name),
            "arguments": _jsonable_str(data.get("arguments"), field="arguments"),
        }
        if created_ts is not None:
            kwargs["created_at"] = created_ts
        group_id = data.get("group_id")
        if isinstance(group_id, str) and group_id:
            kwargs["group_id"] = group_id
        return llm.FunctionCall(**kwargs)

    if item_type == "function_call_output":
        call_id = data.get("call_id")
        if call_id is None:
            raise ValueError("function_call_output item missing call_id")
        kwargs = {
            "id": item_id,
            "call_id": str(call_id),
            "name": str(data.get("name") or ""),
            "output": _jsonable_str(data.get("output"), field="output"),
            "is_error": bool(data.get("is_error", False)),
        }
        if created_ts is not None:
            kwargs["created_at"] = created_ts
        return llm.FunctionCallOutput(**kwargs)

    if item_type == "agent_handoff":
        new_agent_id = data.get("new_agent_id")
        if new_agent_id is None:
            raise ValueError("agent_handoff item missing new_agent_id")
        kwargs = {
            "id": item_id,
            "new_agent_id": str(new_agent_id),
            "old_agent_id": (
                str(data["old_agent_id"]) if data.get("old_agent_id") is not None else None
            ),
        }
        if created_ts is not None:
            kwargs["created_at"] = created_ts
        return llm.AgentHandoff(**kwargs)

    if item_type == "agent_config_update":
        kwargs = {
            "id": item_id,
            "instructions": (
                str(data["instructions"]) if data.get("instructions") is not None else None
            ),
            "tools_added": _string_list(data.get("tools_added")),
            "tools_removed": _string_list(data.get("tools_removed")),
        }
        if created_ts is not None:
            kwargs["created_at"] = created_ts
        return llm.AgentConfigUpdate(**kwargs)

    return None


def current_call_marker(at: datetime | None) -> str:
    """The line that separates replayed history from the call starting now.

    Without it a model reading a continued thread cannot tell where history ends
    and now begins, and answers as though the last thing it read was said a
    moment ago.

    Times are UTC and say so. We have no tenant timezone to render them in, and
    an unlabelled local-looking time the model then repeats to a caller would be
    worse than an honest UTC one.
    """
    when = f" ({at.astimezone(UTC).strftime('%Y-%m-%d %H:%M UTC')})" if at else ""
    return (
        f"--- The call you are on now starts here{when}. Everything above is from earlier "
        "calls with this person - context, not something said a moment ago. ---"
    )


async def load_conversation_chat_context(
    tenant: Tenant,
    conversation_id: str | UUID,
    *,
    exclude_item_ids: list[UUID] | None = None,
    through_created_at: datetime | None = None,
    through_item_id: UUID | None = None,
    include_internal: bool = False,
) -> llm.ChatContext:
    """Load canonical conversation history into a LiveKit ChatContext.

    Loads all stored ChatItem-shaped rows (no visibility filter — the LLM sees
    the full durable timeline). The caller may exclude newly persisted inbound
    items that it will add to the LiveKit context itself for the current turn.

    When through_created_at + through_item_id are set, only items at or before
    that (created_at, id) order are included.

    History arrives as one continuous transcript, with no markers between the
    runs that produced it. A caller who phoned twice and chatted in between held
    one conversation, and the only seam that matters to the model is where the
    replay stops and the live call begins — that one is added by the caller
    (``current_call_marker``).

    **History handed to a model must never contain a dangling tool call.** Every
    provider rejects a `function_call` with no matching `function_call_output`
    with a 400 — not a degraded reply, a failed turn, and a permanent one, since
    the orphan stays in the table. The table can hold one: a task entry is
    `await`ing when the session it runs in dies (a text window idling out, a
    caller hanging up mid-task), so its output is never written. Repaired on the
    way out rather than on the way in, because every writer that could leave one
    is a worker that may already be gone by the time it matters.

    include_internal is accepted for call-site compatibility and ignored.
    """
    if (through_created_at is None) ^ (through_item_id is None):
        raise ValueError("through_created_at and through_item_id must be set together")
    pool = await db.tenant_pool(tenant)
    rows = await pool.fetch(
        """
        SELECT ci.id, ci.type, ci.role, ci.text, ci.attachments, ci.metadata,
               ci.created_at
        FROM conversation_items ci
        WHERE ci.tenant_id = $1
            AND ci.conversation_id = $2
            AND NOT (ci.id = ANY($3::uuid[]))
            AND (
                $4::timestamptz IS NULL
                OR (ci.created_at, ci.id) <= ($4::timestamptz, $5::uuid)
            )
            AND ci.type IN (
                'message',
                'function_call',
                'function_call_output',
                'agent_handoff',
                'agent_config_update'
            )
        ORDER BY ci.created_at, ci.id
        """,
        tenant.id,
        conversation_id,
        exclude_item_ids or [],
        through_created_at,
        through_item_id,
    )
    images = await _hydrate_images(rows)
    chat_ctx = llm.ChatContext()
    for row in rows:
        try:
            item = _conversation_item_to_chat_item(row, images.get(row["id"]))
        except (TypeError, ValueError) as exc:
            # Incomplete tool/handoff rows would poison the whole turn; skip the
            # bad row loudly so the rest of history still loads.
            logger.warning(
                "skipping conversation item %s in chat context: %s",
                row["id"],
                exc,
            )
            continue
        if item is None:
            continue
        chat_ctx.items.append(item)
    return llm.ChatContext(_without_orphan_tool_calls(chat_ctx.items))


def _without_orphan_tool_calls(items: list[llm.ChatItem]) -> list[llm.ChatItem]:
    """The same items, minus any `function_call` nothing answered.

    One pass. An output with no call is left alone: providers accept it, and the
    only way to produce one is a call this loader itself dropped as malformed,
    where losing the answer too would lose more than it repairs.
    """
    answered = {item.call_id for item in items if item.type == "function_call_output"}
    return [item for item in items if item.type != "function_call" or item.call_id in answered]


def summary_block(header: str, rows: Sequence[Mapping[str, Any]]) -> str:
    """The one system message `summary` mode adds: ``header``, then each earlier
    session's summary, oldest first."""
    parts = [header]
    for row in rows:
        at = row["at"]
        heading = [at.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")]
        duration = row["duration_s"]
        # A chat has no length worth stating; only a call carries one.
        if isinstance(duration, int):
            heading.append(f"{duration // 60}m{duration % 60:02d}s")
        if row["outcome"]:
            heading.append(f"outcome: {row['outcome']}")
        parts.append(f"{' - '.join(heading)}\n{str(row['summary']).strip()}")
    return "\n\n".join(parts)


async def load_conversation_summaries(
    tenant: Tenant,
    *,
    conversation_ref_id: str | UUID,
    exclude_session_id: str | UUID,
    limit: int | None,
) -> list[Mapping[str, object]]:
    """Analysis summaries of this person's earlier calls, oldest first.

    Every past call on the identity, regardless of which agent took it: the
    identity is a phone number, not an agent, and a caller who explained their
    problem last week does not care who answered. A DID's inbound agent gets
    changed, or a call is handed off, and the next agent is then briefed on it.

    ``limit`` is passed straight through, ``None`` and all: Postgres reads
    ``LIMIT NULL`` as ``LIMIT ALL``, so "every past call" needs no second query
    and no sentinel. The absent value IS the unbounded case.

    The join goes through ``conversations`` because
    ``conversations.conversation_ref_id`` is the invariant edge, while
    ``sessions.conversation_ref_id`` is a nullable convenience column. No status
    filter: analysis already refuses to summarize a failed or too-short call, so
    ``summary IS NOT NULL`` says the same thing without a second opinion.
    """
    pool = await db.tenant_pool(tenant)
    rows = await pool.fetch(
        """
        SELECT COALESCE(s.started_at, s.created_at) AS at,
               CASE WHEN s.channel = 'text' THEN NULL ELSE s.duration_s END AS duration_s,
               s.outcome, s.summary
        FROM sessions s
        JOIN conversations c ON c.id = s.conversation_id AND c.tenant_id = s.tenant_id
        WHERE s.tenant_id = $1
            AND c.conversation_ref_id = $2
            AND s.id <> $3
            AND s.summary IS NOT NULL
        ORDER BY at DESC
        LIMIT $4
        """,
        tenant.id,
        conversation_ref_id,
        exclude_session_id,
        limit,
    )
    # Oldest first: the order a person tells a story in, and the order the model
    # will assume. Reversed after the LIMIT so the limit keeps the NEWEST calls.
    return list(reversed(rows))


async def load_conversation_chat_items_since(
    tenant: Tenant,
    conversation_id: str | UUID,
    *,
    after_created_at: datetime | None,
    before_created_at: datetime,
    exclude_item_ids: list[UUID] | None = None,
    include_internal: bool = False,
) -> list[tuple[datetime, llm.ChatItem]]:
    """Load ChatItem rows with created_at in (after, before).

    Used by warm text turns to append items that landed in the DB while the
    LiveKit window was busy (e.g. superseded inbound user texts). No visibility
    filter — full timeline for the LLM. ``include_internal`` is ignored.

    Postgres ``timestamptz`` is microsecond resolution; ordering is created_at
    only (MVP).
    """
    pool = await db.tenant_pool(tenant)
    rows = await pool.fetch(
        """
        SELECT ci.id, ci.type, ci.role, ci.text, ci.attachments, ci.metadata, ci.created_at
        FROM conversation_items ci
        WHERE ci.tenant_id = $1
            AND ci.conversation_id = $2
            AND NOT (ci.id = ANY($3::uuid[]))
            AND ci.created_at < $4::timestamptz
            AND ($5::timestamptz IS NULL OR ci.created_at > $5::timestamptz)
            AND ci.type IN (
                'message',
                'function_call',
                'function_call_output',
                'agent_handoff',
                'agent_config_update'
            )
        ORDER BY ci.created_at, ci.id
        """,
        tenant.id,
        conversation_id,
        exclude_item_ids or [],
        before_created_at,
        after_created_at,
    )
    images = await _hydrate_images(rows)
    out: list[tuple[datetime, llm.ChatItem]] = []
    for row in rows:
        try:
            item = _conversation_item_to_chat_item(row, images.get(row["id"]))
        except (TypeError, ValueError) as exc:
            logger.warning(
                "skipping conversation item %s in chat delta: %s",
                row["id"],
                exc,
            )
            continue
        if item is None:
            continue
        out.append((row["created_at"], item))
    return out


async def load_ref_userdata(tenant: Tenant, conversation_id: str | UUID) -> dict[str, object]:
    """What the agent has learned about this person, across every conversation.

    Lives on the identity rather than on a conversation: the last run overwrites
    it whichever conversation it ran in, so pinning it to one of them has no
    answer once a person has many.

    Reached through the conversation rather than by ref id, so every caller that
    holds a thread can ask without also threading the identity through — and
    because ``conversations.conversation_ref_id`` is the invariant edge, while
    ``sessions.conversation_ref_id`` is a nullable convenience column.
    """
    pool = await db.tenant_pool(tenant)
    row = await pool.fetchrow(
        """
        SELECT r.userdata
        FROM conversations c
        JOIN conversation_refs r
            ON r.id = c.conversation_ref_id AND r.tenant_id = c.tenant_id
        WHERE c.id = $1 AND c.tenant_id = $2
        """,
        conversation_id,
        tenant.id,
    )
    if not row:
        return {}
    userdata = row["userdata"] or {}
    if not isinstance(userdata, dict):
        raise RuntimeError("conversation ref userdata must be a JSON object")
    return dict(userdata)


# One statement per item, against the partial unique index this used to emulate
# with a SELECT (`uq_conversation_items_livekit_item`, data/0001_init.sql). The
# conflict target restates the index predicate because Postgres will not infer a
# PARTIAL index from the column list alone; both clauses are always true here —
# every LiveKit item carries an id, and `session_id` is always set on this path.
#
# The SET list carries the old UPDATE branch across exactly, including what is
# NOT in it: conversation_id, session_id, source and attachments are never
# overwritten by a re-fire. `created_at` is the item's own LiveKit timestamp,
# which LiveKit moves on a function call once its tool starts.
_UPSERT_ITEM = """
INSERT INTO conversation_items (
    tenant_id, conversation_id, session_id,
    direction, type, role, agent_id, agent_version,
    text, source, visibility, metrics, metadata, created_at
)
VALUES (
    $1, $2, $3, $4, $5, $6, $7::uuid, $8, $9, $10, $11,
    $12::jsonb, $13::jsonb, $14::timestamptz
)
ON CONFLICT (tenant_id, session_id, (metadata->>'livekit_item_id'))
    WHERE session_id IS NOT NULL AND metadata->>'livekit_item_id' IS NOT NULL
DO UPDATE SET
    direction = EXCLUDED.direction,
    type = EXCLUDED.type,
    role = EXCLUDED.role,
    text = EXCLUDED.text,
    visibility = EXCLUDED.visibility,
    agent_id = COALESCE(EXCLUDED.agent_id, conversation_items.agent_id),
    agent_version = COALESCE(EXCLUDED.agent_version, conversation_items.agent_version),
    metrics = EXCLUDED.metrics,
    metadata = EXCLUDED.metadata,
    created_at = EXCLUDED.created_at,
    updated_at = now()
"""


def _upsert_params(
    item: llm.ChatItem,
    *,
    tenant_id: str,
    session_id: str,
    conversation_id: str,
    source: str,
    agent_id: str | None,
    agent_version: int | None,
    handoff_targets: Mapping[str, HandoffTarget],
) -> tuple[object, ...] | None:
    """One LiveKit chat item as parameters for ``_UPSERT_ITEM``.

    None for an item this table does not hold: an unknown ChatItem type.
    """
    row = item_row(item, handoff_targets)
    if row is None:
        return None

    # Stamp the agent that produced the item. A handoff row uses the TARGET —
    # the item IS the switch, so it belongs to the agent taking over — read from
    # the map the run recorded as each target was built, rather than from a fresh
    # `agents.published_version` lookup.
    #
    # A team member has no `agents` row, so the two columns keep the stamp the
    # caller captured: writing its LiveKit id into `agent_id` is not possible
    # (the column is a uuid) and inventing one would be worse. A task has no row
    # either, and keeps the calling agent's stamp deliberately: the
    # sub-conversation is still that agent's stretch of the call, and the two
    # rows that bracket it are what let a reader collapse it.
    if row.handoff is not None and row.handoff.agent_id is not None:
        agent_id = row.handoff.agent_id
        agent_version = row.handoff.version

    # A handoff deliberately does NOT re-point sessions.agent_id /
    # agent_version_id. The session names the agent that ANSWERED and the exact
    # version it ran, permanently — which is what makes
    # `session → agent_versions.config` a faithful record of how the call was set
    # up, prompt and all, for every field including ones not added yet. Per-item
    # attribution lives here, at the right grain.
    return (
        tenant_id,
        conversation_id,
        session_id,
        row.direction,
        row.type,
        row.role,
        agent_id,
        agent_version,
        row.text,
        source,
        row.visibility,
        json.dumps(row.metrics, default=str) if row.metrics is not None else None,
        json.dumps({"livekit_item_id": row.livekit_item_id, "data": row.data}, default=str),
        row.created_at,
    )


async def persist_transcript_items(
    tenant: Tenant,
    session_id: str,
    items: Sequence[llm.ChatItem],
    *,
    conversation_id: str | None,
    session_type: str,
    agent_id: str | None,
    agent_version: int | None,
    handoff_targets: Mapping[str, HandoffTarget],
) -> None:
    """Persist the chat items one session event produced, in one statement.

    Idempotent on (tenant_id, session_id, livekit_item_id) so concurrent live
    event handlers can safely re-fire.

    Every field this needs about the session is passed in rather than read back:
    the caller wrote that row itself and holds all of it. ``conversation_id``
    None means a call with nobody to file it against — a refused inbound leg —
    and there is nothing to write.

    ``agent_id`` / ``agent_version`` name who produced these items, captured by
    the caller when they were emitted. Deliberately not read from the session
    row: that row names the agent that ANSWERED and never moves (see
    ``sessions.agent_id``), and even if it did move, items are persisted on
    concurrent tasks, so two turns either side of a handoff could both read it
    after the switch and stamp the pre-handoff turn with the wrong agent.

    Voice only. The text worker inserts the same rows (`item_row`) one at a
    time, as a turn produces them (`workers/text/steps.py`).
    """
    if conversation_id is None or not items:
        return
    try:
        source = item_source_for_session_type(session_type)
        params: list[tuple[object, ...]] = []
        for item in items:
            row = _upsert_params(
                item,
                tenant_id=tenant.id,
                session_id=session_id,
                conversation_id=conversation_id,
                source=source,
                agent_id=agent_id,
                agent_version=agent_version,
                handoff_targets=handoff_targets,
            )
            if row is not None:
                params.append(row)
        if not params:
            return
        # `_UPSERT_ITEM` order: $5 is the item's type, $14 its own timestamp.
        message_times = [row[13] for row in params if row[4] == "message"]
        pool = await db.tenant_pool(tenant)
        async with pool.acquire() as conn, conn.transaction():
            await conn.executemany(_UPSERT_ITEM, params)
            if message_times:
                await conn.execute(
                    """
                    UPDATE conversations
                    SET last_activity_at = GREATEST(last_activity_at, $3::timestamptz)
                    WHERE id = $1 AND tenant_id = $2
                    """,
                    conversation_id,
                    tenant.id,
                    max(message_times),
                )
    except Exception:
        # A detached task: a transcript row that will not write must not take
        # the call down with it.
        logger.warning(
            "failed to persist %d conversation item(s) for session %s",
            len(items),
            session_id,
            exc_info=True,
        )
