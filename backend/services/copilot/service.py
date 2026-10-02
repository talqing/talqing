"""CoPilot chat use-cases, shared by every CoPilot.

The subject is what the CoPilot edits — an agent, a tool or a task —
held as ``conversation_refs.bind_id`` under the CoPilot's ref kind.
Timeline lives in conversation_items. SSE is keyed by the subject id, which is
what the editor rail has in hand.

Request/response shapes: ``services.copilot.models``.
Timeline projection: ``services.copilot.items``.
SSE bus: ``services.copilot.stream``.
"""

from __future__ import annotations

import logging
from uuid import UUID

from fastapi import HTTPException

from services.conversations import ensure_copilot_ref
from services.messaging import TextTurnJob, TurnPayload, publish_turn, record_turn_status
from services.user import Context

from .items import hidden_from_rail, public_message
from .models import (
    CopilotMessageEvent,
    SendMessageRequest,
    SendMessageResponse,
    SnapshotPayload,
)
from .stream import publish
from .subjects import CopilotSubject

logger = logging.getLogger("talqing.services.copilot")


async def _latest_turn(conversation_id: UUID, ctx: Context) -> tuple[bool, str | None]:
    """The newest turn's state: (still working, why it stopped).

    Read from ``turn_status`` on the item that opened the turn, never inferred
    from the shape of the timeline. The rows cannot answer this question — a
    turn waiting on a tool and a turn that died right after one end in the same
    ``function_call_output``, and the old guess ("the newest row is not an
    assistant message, so work is in flight") called both of them busy. That is
    a lie no reload can clear, which is what it became when a CoPilot turn hit
    OpenAI's rate limit mid-build.

    Only the newest turn is consulted: an older one still marked running was
    left behind by a worker that died mid-turn, and the message that opened this
    one has superseded it either way.
    """
    pool = await ctx.tenant_pool()
    turn = await pool.fetchrow(
        """
        SELECT turn_status, turn_error
        FROM conversation_items
        WHERE conversation_id = $1 AND tenant_id = $2
            AND turn_status IS NOT NULL
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """,
        conversation_id,
        ctx.tenant.id,
    )
    if turn is None:
        return False, None
    return turn["turn_status"] == "running", turn["turn_error"]


async def get_snapshot(
    subject: CopilotSubject,
    subject_id: UUID,
    ctx: Context,
) -> SnapshotPayload:
    """Load the editor timeline snapshot for one subject (404 if it is gone)."""
    await subject.require(subject_id, ctx)
    thread = await ensure_copilot_ref(ctx, kind=subject.ref_kind, subject_id=subject_id)
    pool = await ctx.tenant_pool()
    rows = await pool.fetch(
        """
        SELECT id, type, role, text, metadata, created_at
        FROM conversation_items
        WHERE conversation_id = $1 AND tenant_id = $2
            AND type IN (
                'message',
                'function_call',
                'function_call_output',
                'agent_handoff',
                'agent_config_update'
            )
        ORDER BY created_at, id
        """,
        thread.conversation_id,
        ctx.tenant.id,
    )
    busy, error = await _latest_turn(thread.conversation_id, ctx)
    return SnapshotPayload(
        messages=[public_message(r) for r in rows if not hidden_from_rail(r)],
        busy=busy,
        error=error,
    )


async def send_message(
    subject: CopilotSubject,
    subject_id: UUID,
    body: SendMessageRequest,
    ctx: Context,
) -> SendMessageResponse:
    text = body.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="empty message")
    pool = await ctx.tenant_pool()
    await subject.require(subject_id, ctx)
    thread = await ensure_copilot_ref(ctx, kind=subject.ref_kind, subject_id=subject_id)

    # Items only; no sessions row for a CoPilot (unbilled warm window is in-process).
    #
    # `turn_status` is set here rather than when the worker picks the job up:
    # the turn exists from the moment it is queued, and cold-starting an agent
    # takes seconds during which a reloading rail would otherwise be told that
    # nothing is running.
    user_row = await pool.fetchrow(
        """
        INSERT INTO conversation_items (
            tenant_id, conversation_id,
            direction, type, role, text, source, visibility, metadata,
            turn_status
        )
        VALUES (
            $1, $2, 'inbound', 'message', 'user', $3,
            $4, 'internal', '{}'::jsonb,
            'running'
        )
        RETURNING id, type, role, text, metadata, created_at
        """,
        ctx.tenant.id,
        thread.conversation_id,
        text,
        subject.api_source,
    )
    if not user_row:
        raise RuntimeError(f"failed to create {subject.label} user message")

    item_id = user_row["id"]
    # SSE is keyed by the subject id — what the editor rail has in hand.
    key = str(subject_id)
    await publish(
        subject, key, CopilotMessageEvent.model_validate(public_message(user_row).model_dump())
    )
    await publish(subject, key, TurnPayload(turn_id=item_id, status="running"))

    job = TextTurnJob(
        tenant_id=ctx.tenant.id,
        conversation_id=thread.conversation_id,
        input_item_id=item_id,
        created_at=user_row["created_at"],
        requested_agent_id=subject.sentinel_id,
        user_id=ctx.user.id,
        conversation_ref_id=thread.ref.id,
    )
    unavailable = f"{subject.label} execution queue is unavailable"
    try:
        await publish_turn(job)
    except Exception as exc:
        logger.exception(
            "%s turn enqueue failed subject=%s conversation=%s item=%s",
            subject.label,
            subject_id,
            thread.conversation_id,
            item_id,
        )
        # Row first, then the frame — a turn that never reached a worker still
        # has to stop reading as running to the next client that reconnects.
        # Same ordering, and the same reason, as publish_turn_status.
        await record_turn_status(
            ctx.tenant,
            turn_id=item_id,
            status="error",
            error=unavailable,
        )
        await publish(
            subject,
            key,
            TurnPayload(turn_id=item_id, status="error", error=unavailable),
        )
        raise HTTPException(status_code=503, detail=unavailable) from exc

    return SendMessageResponse(turn_id=item_id, conversation_id=thread.conversation_id)
