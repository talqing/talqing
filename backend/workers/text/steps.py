"""Progressive persistence of the LiveKit chat items a text session produces."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from uuid import UUID

from livekit.agents import (
    AgentSession,
    ConversationItemAddedEvent,
    FunctionToolsExecutedEvent,
    io,
    llm,
)

import db
from services import conversations
from services.tools import HandoffTarget
from services.userdata import is_reserved_key
from workers.session._util import item_row
from workers.text.fanout import fanout_step
from workers.text.types import TextTurnInput

logger = logging.getLogger("talqing.workers.text.steps")


@dataclass
class StepState:
    """Mutable progressive-persist state for one turn."""

    session_id: str
    source: str
    outbound_visibility: str
    initial_item_ids: set[str]
    # {LiveKit agent id: HandoffTarget}: who each `agent_handoff` item this turn
    # produces switched to. The window's own map, which the compiler fills.
    handoff_targets: Mapping[str, HandoffTarget]
    # Whose name new items are written under. Follows a handoff to a stored agent.
    agent_id: UUID | None = None
    agent_version: int | None = None
    # Whether this turn handed the chat to another stored agent.
    handed_off: bool = False
    text_output: io.TextOutput | None = None
    seen_livekit_ids: set[str] = field(default_factory=set)
    persisted: list[dict[str, object]] = field(default_factory=list)
    assistant_texts: list[str] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


async def persist_step(
    input: TextTurnInput,
    item: llm.ChatItem,
    state: StepState,
) -> dict[str, object] | None:
    """Persist one completed LiveKit chat item and return the row (or None if skipped)."""
    async with state.lock:
        if item.id in state.initial_item_ids or item.id in state.seen_livekit_ids:
            return None
        # The same row a call writes for this item. What follows is only what a
        # text plane decides for itself: how a reply is delivered, and who it is
        # stamped with.
        step = item_row(item, state.handoff_targets)
        if step is None:
            return None
        if step.role == "user" or bool(step.data.get("interrupted")):
            return None

        deliverable = bool(step.role == "assistant" and step.text)
        # Outbound assistant text is customer_visible; delivery_status tracks
        # provider send (pending→sent). Tools / handoffs stay internal.
        # Platform planes use plane.outbound_visibility (internal).
        delivery_status = (
            "pending"
            if (
                deliverable
                and state.outbound_visibility == "customer_visible"
                and input.job.conversation_ref_id is not None
                and input.job.integration_id is not None
            )
            else "not_applicable"
        )

        pool = await db.tenant_pool(input.tenant)
        # Mid-turn stale work is skipped via interruption.superseded_by_item_id
        # in the step worker (actor request_supersede). No per-step DB poll.

        # Tenant warm windows dedupe by session_id + livekit_item_id.
        # Platform has no sessions row — dedupe by conversation + livekit id.
        persist_session_id = state.session_id if input.plane.name == "tenant" else None
        if persist_session_id is not None:
            existing = await pool.fetchrow(
                """
                SELECT id
                FROM conversation_items
                WHERE tenant_id = $1
                    AND session_id = $2::uuid
                    AND metadata->>'livekit_item_id' = $3
                """,
                input.tenant.id,
                persist_session_id,
                item.id,
            )
        else:
            existing = await pool.fetchrow(
                """
                SELECT id
                FROM conversation_items
                WHERE tenant_id = $1
                    AND conversation_id = $2
                    AND metadata->>'livekit_item_id' = $3
                """,
                input.tenant.id,
                input.job.conversation_id,
                item.id,
            )
        if existing:
            state.seen_livekit_ids.add(item.id)
            return None

        # A handoff row belongs to the agent taking over, so it is stamped with
        # the target rather than with whoever was speaking — and only a target
        # that is a stored agent has a stamp to give. A team member and a task
        # have no `agents` row (`agent_id` is a foreign key to it), so their rows
        # keep the stamp of the agent that was holding the chat.
        entered = step.handoff if step.handoff and step.handoff.kind == "handoff" else None
        if entered is not None and entered.agent_id is not None:
            stamp_agent_id, stamp_agent_version = UUID(entered.agent_id), entered.version
        else:
            stamp_agent_id, stamp_agent_version = state.agent_id, state.agent_version

        # RETURNING the full item shape, not the handful of fields this function
        # itself reads: the row goes straight to `fanout_step`, which turns it
        # into the `item.created` frame the dashboard renders from.
        async with pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                f"""
                INSERT INTO conversation_items (
                    tenant_id, conversation_id,
                    session_id, trigger_item_id, direction, type, role,
                    agent_id, agent_version,
                    text, source, visibility, delivery_status, metrics, metadata, created_at
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11,
                        $12, $13, $14::jsonb, $15::jsonb, $16)
                RETURNING {conversations.CONVERSATION_ITEM_COLUMNS}
                """,
                input.tenant.id,
                input.job.conversation_id,
                persist_session_id,
                input.job.input_item_id,
                "outbound" if deliverable else "internal",
                step.type,
                step.role,
                stamp_agent_id if input.plane.name == "tenant" else None,
                stamp_agent_version if input.plane.name == "tenant" else None,
                step.text,
                state.source,
                state.outbound_visibility if deliverable else "internal",
                delivery_status,
                json.dumps(step.metrics, default=str) if step.metrics is not None else None,
                json.dumps({"livekit_item_id": item.id, "data": step.data}, default=str),
                step.created_at,
            )
            if row and step.type == "message":
                await conn.execute(
                    """
                    UPDATE conversations SET last_activity_at = GREATEST(last_activity_at, $3)
                    WHERE id = $1 AND tenant_id = $2
                    """,
                    input.job.conversation_id,
                    input.tenant.id,
                    row["created_at"],
                )
        if not row:
            return None
        state.seen_livekit_ids.add(item.id)
        out = dict(row)
        state.persisted.append(out)
        if deliverable and step.text:
            state.assistant_texts.append(step.text)
        # Every later item is the new agent's. Entering a task, or returning from
        # one, moves nothing: the sub-conversation is still the calling agent's.
        if entered is not None and entered.agent_id is not None:
            state.agent_id, state.agent_version = stamp_agent_id, stamp_agent_version
            state.handed_off = True
        return out


class StepRecorder:
    """Persists and delivers what a session produces, as it produces it.

    Listens to the session, queues each finished item, and writes them one at a
    time in the order they happened. Armed around a turn, and around a chat's
    end — where the exit hook is the only thing still speaking.

    ``skip`` is asked before each item: a turn a newer message has superseded
    stops writing what it was in the middle of saying.
    """

    def __init__(
        self,
        input: TextTurnInput,
        session: AgentSession,
        state: StepState,
        *,
        skip: Callable[[], bool] = lambda: False,
    ) -> None:
        self._input = input
        self._session = session
        self._state = state
        self._skip = skip
        self._queue: asyncio.Queue[llm.ChatItem | None] = asyncio.Queue()
        self._worker: asyncio.Task[None] | None = None

    def arm(self) -> None:
        self._session.on("conversation_item_added", self._on_item)
        self._session.on("function_tools_executed", self._on_tools)
        self._worker = asyncio.create_task(self._run(), name="text_steps")

    async def drain(self) -> None:
        """Stop listening and wait for everything already queued to be written."""
        self._session.off("conversation_item_added", self._on_item)
        self._session.off("function_tools_executed", self._on_tools)
        worker, self._worker = self._worker, None
        if worker is None or worker.done():
            return
        self._queue.put_nowait(None)
        try:
            # Bounded: this also runs on the way out of a failed turn, where a
            # wedged write must not hold the conversation's actor.
            await asyncio.wait_for(worker, timeout=30)
        except Exception:
            logger.exception("progressive steps did not drain")
            worker.cancel()

    def _on_item(self, ev: ConversationItemAddedEvent) -> None:
        self._enqueue(ev.item)

    def _on_tools(self, ev: FunctionToolsExecutedEvent) -> None:
        # Every call carries an output since livekit-agents 1.7; a silent tool
        # answers with an empty one rather than with nothing.
        for call, output in ev.zipped():
            self._enqueue(call)
            self._enqueue(output)

    def _enqueue(self, item: llm.ChatItem) -> None:
        if item.type not in ("message", "function_call", "function_call_output", "agent_handoff"):
            return
        # User text is already durable from the inbound API/provider path.
        if item.type == "message" and item.role == "user":
            return
        self._queue.put_nowait(item)

    async def _run(self) -> None:
        while (item := await self._queue.get()) is not None:
            try:
                if self._skip():
                    continue
                row = await persist_step(self._input, item, self._state)
                if row is not None:
                    await fanout_step(self._input, row)
            except Exception:
                logger.exception(
                    "progressive step failed for turn %s", self._input.job.input_item_id
                )


async def persist_userdata(input: TextTurnInput, session: AgentSession, session_id: str) -> None:
    """Write what the session now knows: onto the contact, and onto the chat.

    The contact's bag is the one home for a fact about a person, whichever
    thread learned it. The chat's own copy is what its next warm window starts
    from, and what `session.completed` reports. Only when there is something to
    write, so completing a turn does not bump `updated_at` as a side effect.
    """
    userdata = session.userdata if isinstance(session.userdata, dict) else {}
    public = {str(key): value for key, value in userdata.items() if not is_reserved_key(key)}
    if not public:
        return
    payload = json.dumps(public, default=str)
    pool = await db.tenant_pool(input.tenant)
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            """
            UPDATE conversation_refs r
            SET userdata = $3::jsonb, updated_at = now()
            FROM conversations c
            WHERE c.id = $1 AND c.tenant_id = $2
              AND r.id = c.conversation_ref_id AND r.tenant_id = c.tenant_id
            """,
            input.job.conversation_id,
            input.tenant.id,
            payload,
        )
        # sessions.agent_id is deliberately NOT re-pointed after a handoff: it
        # names the agent the chat ENTERED on. Per-item attribution is on
        # conversation_items, and the live agent is the newest handoff item.
        await conn.execute(
            "UPDATE sessions SET userdata = $3::jsonb, updated_at = now() "
            "WHERE id = $1 AND tenant_id = $2",
            session_id,
            input.tenant.id,
            payload,
        )


async def seal_turn_success(
    input: TextTurnInput,
    session: AgentSession,
    state: StepState,
) -> tuple[list[dict[str, object]], str | None]:
    """What a successful turn produced, with its userdata written back.

    Does not end anything — the chat stays open and its window stays warm.
    Platform planes have no seal side-effects beyond assistant text.
    """
    assistant_text = "\n\n".join(t for t in state.assistant_texts if t and str(t).strip()) or None
    if input.plane.name == "tenant":
        await persist_userdata(input, session, state.session_id)
    return list(state.persisted), assistant_text
