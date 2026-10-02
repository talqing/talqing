"""conversation_refs: the durable external identity behind N conversations.

A ref is *who this is* — a phone number, a Telegram chat, a tenant's own web
key, a CoPilot subject — and it is also where what we know about that person
lives (``userdata``). A conversation is *what we are currently talking about*,
and there are many of them per identity. ``open_conversation`` is where a run
picks which one it writes to, from the answering agent's
``AgentConfig.conversation.context``.

One core ``ensure_ref`` path; surface wrappers only validate keys and map
errors. ``kind`` + ``bind_id`` describe the binding (no FK on bind_id so parent
resource delete never cascades into conversation history). ``metadata`` holds
end-customer fields only.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

import asyncpg
from fastapi import HTTPException

from services.conversation_context import ConversationContext
from services.user import Context

from .keys import (
    ALL_REF_KINDS,
    ConversationRefKind,
    copilot_conversation_key,
    validate_web_conversation_key,
)

CONVERSATION_REF_COLUMNS = """
id, tenant_id, conversation_key, kind, bind_id,
userdata, metadata, created_at, updated_at
"""


class ConversationRefConflict(Exception):
    """Conversation key is bound to a different kind/bind identity."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True, slots=True)
class ConversationRef:
    """Internal domain model for one conversation_refs row."""

    id: UUID
    tenant_id: UUID
    conversation_key: str
    kind: ConversationRefKind
    bind_id: UUID | None
    # What the agent has learned about this person, across every conversation
    # with them. Replaced by every run at finalize; read at the start of a call
    # only when the agent asks for it.
    userdata: dict[str, Any]
    metadata: dict[str, Any]
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> ConversationRef:
        meta = row["metadata"]
        if meta is None:
            metadata: dict[str, Any] = {}
        elif isinstance(meta, dict):
            metadata = dict(meta)
        else:
            raise TypeError("conversation_refs.metadata must be a JSON object")
        bag = row["userdata"]
        if bag is None:
            userdata: dict[str, Any] = {}
        elif isinstance(bag, dict):
            userdata = dict(bag)
        else:
            raise TypeError("conversation_refs.userdata must be a JSON object")
        kind = row["kind"]
        if kind not in ALL_REF_KINDS:
            raise TypeError(f"invalid conversation_refs.kind {kind!r}")
        return cls(
            id=row["id"],
            tenant_id=row["tenant_id"],
            conversation_key=row["conversation_key"],
            kind=kind,  # type: ignore[arg-type]
            bind_id=row["bind_id"],
            userdata=userdata,
            metadata=metadata,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


@dataclass(frozen=True, slots=True)
class ConversationThread:
    """The identity a run belongs to, and the conversation it writes to."""

    ref: ConversationRef
    conversation_id: UUID


async def get_ref_by_key(
    conn,
    *,
    tenant_id: UUID,
    conversation_key: str,
) -> ConversationRef | None:
    row = await conn.fetchrow(
        f"""
        SELECT {CONVERSATION_REF_COLUMNS}
        FROM conversation_refs
        WHERE tenant_id = $1 AND conversation_key = $2
        """,
        tenant_id,
        conversation_key,
    )
    return ConversationRef.from_row(row) if row else None


async def get_ref_by_id(
    conn,
    *,
    tenant_id: UUID,
    conversation_ref_id: UUID,
) -> ConversationRef | None:
    row = await conn.fetchrow(
        f"""
        SELECT {CONVERSATION_REF_COLUMNS}
        FROM conversation_refs
        WHERE tenant_id = $1 AND id = $2
        """,
        tenant_id,
        conversation_ref_id,
    )
    return ConversationRef.from_row(row) if row else None


async def get_ref_by_kind_bind(
    conn,
    *,
    tenant_id: UUID,
    kind: ConversationRefKind,
    bind_id: UUID,
) -> ConversationRef | None:
    row = await conn.fetchrow(
        f"""
        SELECT {CONVERSATION_REF_COLUMNS}
        FROM conversation_refs
        WHERE tenant_id = $1 AND kind = $2 AND bind_id = $3
        """,
        tenant_id,
        kind,
        bind_id,
    )
    return ConversationRef.from_row(row) if row else None


def _validate_bind(existing: ConversationRef, *, kind: ConversationRefKind) -> None:
    """Enforce that an existing ref keeps the same kind.

    Only the kind: every platform-built key already encodes its resource
    (``sip:`` the DID, ``telegram:`` the bot, a copilot prefix the subject id),
    so a differing ``bind_id`` means the row was re-created under a stable key —
    deleting a telephony account cascades into ``phone_numbers`` — not that the
    conversation changed identity. Conflicting on that dropped live inbound
    calls; ``ensure_ref`` re-points instead. A web contact is bound to nothing.
    """
    if existing.kind != kind:
        raise ConversationRefConflict(
            f"conversation_key is bound to kind {existing.kind!r}, not {kind!r}"
        )


async def ensure_ref(
    conn,
    *,
    tenant_id: UUID,
    conversation_key: str,
    kind: ConversationRefKind,
    bind_id: UUID | None = None,
    customer_metadata: Mapping[str, Any] | None = None,
    userdata_seed: Mapping[str, Any] | None = None,
) -> ConversationRef:
    """Find or create the identity row (single shared path for all surfaces).

    This creates no conversation — ``open_conversation`` does that, because how
    many conversations an identity has is the agent's choice, not the surface's.

    ``kind`` / ``bind_id``:
      - ``integration`` — messaging; ``bind_id`` = integrations.id (required)
      - ``sip`` — ``bind_id`` = phone_numbers.id (required)
      - ``stream`` — ``bind_id`` = stream_connections.id (required)
      - ``web`` — no ``bind_id``: a tenant's own contact is bound to no agent
      - ``agent_copilot`` — ``bind_id`` = subject agents.id (required)
      - ``tool_copilot`` — ``bind_id`` = subject tools.id (required)
      - ``task_copilot`` — ``bind_id`` = subject agent_tasks.id (required)

    ``bind_id`` on an existing ref is re-pointed to the incoming one rather than
    kept: the key already names the resource, so the stored id is derived data
    that goes stale when the row is re-created.

    ``userdata_seed`` is merged (top-level ``||``) into the identity's bag: what
    the caller already knows about this person before the run starts.

    Callers own the transaction. Concurrent creates race on
    ``UNIQUE (tenant_id, conversation_key)``; the loser adopts the winner's row.
    """
    if kind not in ALL_REF_KINDS:
        raise ValueError(f"invalid conversation ref kind {kind!r}")
    if kind != "web" and bind_id is None:
        raise ValueError(f"{kind} bind requires bind_id")

    key = conversation_key.strip()
    if not key:
        raise ValueError("conversation_key is required")

    meta_json = json.dumps(dict(customer_metadata or {}))
    seed_json = json.dumps(dict(userdata_seed or {}))

    existing = await get_ref_by_key(conn, tenant_id=tenant_id, conversation_key=key)
    if existing is None:
        try:
            # Nested transaction = SAVEPOINT: a unique violation would otherwise
            # abort the caller's whole transaction and every statement after it.
            async with conn.transaction():
                row = await conn.fetchrow(
                    f"""
                    INSERT INTO conversation_refs (
                        tenant_id, conversation_key, kind, bind_id, userdata, metadata
                    )
                    VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb)
                    RETURNING {CONVERSATION_REF_COLUMNS}
                    """,
                    tenant_id,
                    key,
                    kind,
                    bind_id,
                    seed_json,
                    meta_json,
                )
            return ConversationRef.from_row(row)
        except asyncpg.UniqueViolationError:
            # Race: another worker created the same key — adopt theirs.
            existing = await get_ref_by_key(conn, tenant_id=tenant_id, conversation_key=key)
            if existing is None:
                raise

    _validate_bind(existing, kind=kind)
    row = await conn.fetchrow(
        f"""
        UPDATE conversation_refs
        SET metadata = metadata || $3::jsonb,
            bind_id = COALESCE($4, bind_id),
            userdata = userdata || $5::jsonb,
            updated_at = now()
        WHERE id = $1 AND tenant_id = $2
        RETURNING {CONVERSATION_REF_COLUMNS}
        """,
        existing.id,
        tenant_id,
        meta_json,
        bind_id,
        seed_json,
    )
    return ConversationRef.from_row(row)


async def open_conversation(
    conn,
    *,
    tenant_id: UUID,
    ref: ConversationRef,
    context: ConversationContext,
    source: str,
    outbound: bool = False,
) -> UUID:
    """The conversation this run writes to.

    ``transcript`` joins the newest conversation on this identity, opening the
    first one when there is none. ``none`` and ``summary`` always open a new one.
    There is no stored "current conversation" pointer - the newest row is the
    answer, so nothing can go stale.

    ``outbound`` says we are the ones reaching out (a template, a dialled call).
    A conversation opened that way is ``inactive`` until the contact takes part,
    and joining an existing one leaves its status alone. Every other caller is
    the contact taking part, which makes the conversation ``active`` for good.

    Must run inside the caller's transaction, alongside ``ensure_ref``: an
    identity created without its conversation is one a crash leaves looking like
    "nothing to continue".
    """
    if context == "transcript":
        row = await conn.fetchrow(
            """
            SELECT id
            FROM conversations
            WHERE tenant_id = $1 AND conversation_ref_id = $2
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            tenant_id,
            ref.id,
        )
        if row:
            if not outbound:
                await conn.execute(
                    """
                    UPDATE conversations SET status = 'active', updated_at = now()
                    WHERE id = $1 AND tenant_id = $2 AND status = 'inactive'
                    """,
                    row["id"],
                    tenant_id,
                )
            return row["id"]

    row = await conn.fetchrow(
        """
        INSERT INTO conversations (tenant_id, conversation_ref_id, metadata, status)
        VALUES ($1, $2, $3::jsonb, $4)
        RETURNING id
        """,
        tenant_id,
        ref.id,
        json.dumps({"source": source}),
        "inactive" if outbound else "active",
    )
    return row["id"]


async def ensure_thread(
    conn,
    *,
    tenant_id: UUID,
    conversation_key: str,
    kind: ConversationRefKind,
    source: str,
    context: ConversationContext,
    bind_id: UUID | None = None,
    customer_metadata: Mapping[str, Any] | None = None,
    userdata_seed: Mapping[str, Any] | None = None,
    outbound: bool = False,
) -> ConversationThread:
    """``ensure_ref`` + ``open_conversation`` — what every surface actually wants."""
    ref = await ensure_ref(
        conn,
        tenant_id=tenant_id,
        conversation_key=conversation_key,
        kind=kind,
        bind_id=bind_id,
        customer_metadata=customer_metadata,
        userdata_seed=userdata_seed,
    )
    conversation_id = await open_conversation(
        conn, tenant_id=tenant_id, ref=ref, context=context, source=source, outbound=outbound
    )
    return ConversationThread(ref=ref, conversation_id=conversation_id)


# ── channel (messaging webhook) ─────────────────────────────────────────────


async def ensure_conversation_ref(
    conn,
    *,
    tenant_id: UUID,
    conversation_key: str,
    integration_id: UUID,
    source: str,
    context: ConversationContext,
    customer_metadata: Mapping[str, Any],
    userdata_seed: Mapping[str, Any] | None = None,
    outbound: bool = False,
) -> ConversationThread:
    """Find or create identity + conversation for a messaging channel.

    ``bind_id`` = integration id. Agents come from the active trigger per event.
    For what is not a chat: our own outreach, a message nobody is assigned to
    answer, and a WhatsApp call — which passes ``transcript`` whatever its voice
    agent is set to, because the call and the chat are one thread on the
    person's phone. A chat picks its conversation in ``services.chats``.
    Raises ``RuntimeError`` on bind conflicts.
    """
    try:
        return await ensure_thread(
            conn,
            tenant_id=tenant_id,
            conversation_key=conversation_key,
            kind="integration",
            source=source,
            context=context,
            bind_id=integration_id,
            customer_metadata=customer_metadata,
            userdata_seed=userdata_seed,
            outbound=outbound,
        )
    except ConversationRefConflict as exc:
        raise RuntimeError(exc.message) from exc


# ── web (HTTP) ──────────────────────────────────────────────────────────────


def web_conversation_key(conversation_key: str) -> str:
    """A tenant's own contact key, cleaned — or the 400 that says what is wrong with it."""
    try:
        return validate_web_conversation_key(conversation_key)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


async def ensure_web_ref(
    ctx: Context,
    *,
    conversation_key: str,
    source: str,
    context: ConversationContext,
    userdata_seed: Mapping[str, Any] | None = None,
) -> ConversationThread:
    """Find or create a web contact, and the conversation a web CALL writes to.

    A chat picks its conversation by a wider rule and takes its own path
    (``services.chats``).
    """
    key = web_conversation_key(conversation_key)
    pool = await ctx.tenant_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            try:
                return await ensure_thread(
                    conn,
                    tenant_id=ctx.tenant.id,
                    conversation_key=key,
                    kind="web",
                    source=source,
                    context=context,
                    userdata_seed=userdata_seed,
                )
            except ConversationRefConflict as exc:
                raise HTTPException(status_code=409, detail=exc.message) from exc


# ── SIP (HTTP + worker) ─────────────────────────────────────────────────────


async def ensure_sip_ref_on_conn(
    conn,
    *,
    tenant_id: UUID,
    conversation_key: str,
    phone_number_id: UUID,
    context: ConversationContext,
    metadata: Mapping[str, Any],
    userdata_seed: Mapping[str, Any] | None = None,
    outbound: bool = False,
) -> ConversationThread:
    """Find or create a SIP thread on an open connection/transaction.

    ``bind_id`` = phone_numbers.id. Raises ``ConversationRefConflict`` or
    ``ValueError``.
    """
    key = conversation_key.strip()
    if not key.startswith("sip:"):
        raise ValueError("SIP conversation_key must use sip: prefix")
    return await ensure_thread(
        conn,
        tenant_id=tenant_id,
        conversation_key=key,
        kind="sip",
        source="sip",
        context=context,
        bind_id=phone_number_id,
        customer_metadata=metadata,
        userdata_seed=userdata_seed,
        outbound=outbound,
    )


# ── stream (worker) ─────────────────────────────────────────────────────────


async def ensure_stream_ref_on_conn(
    conn,
    *,
    tenant_id: UUID,
    conversation_key: str,
    stream_connection_id: UUID,
    context: ConversationContext,
    metadata: Mapping[str, Any],
    userdata_seed: Mapping[str, Any] | None = None,
) -> ConversationThread:
    """Find or create a partner-stream thread on an open connection/transaction.

    ``bind_id`` = stream_connections.id. The HTTP sibling ``ensure_sip_ref`` has
    no counterpart here because nothing but the worker ever binds one: a stream
    call has no token mint and no outbound dial.
    """
    key = conversation_key.strip()
    if not key.startswith("stream:"):
        raise ValueError("stream conversation_key must use stream: prefix")
    return await ensure_thread(
        conn,
        tenant_id=tenant_id,
        conversation_key=key,
        kind="stream",
        source="stream",
        context=context,
        bind_id=stream_connection_id,
        customer_metadata=metadata,
        userdata_seed=userdata_seed,
    )


async def ensure_sip_ref(
    ctx: Context,
    *,
    conversation_key: str,
    phone_number_id: UUID,
    context: ConversationContext,
    metadata: Mapping[str, Any],
    userdata_seed: Mapping[str, Any] | None = None,
) -> ConversationThread:
    """HTTP wrapper: open tenant pool + transaction around SIP ensure."""
    pool = await ctx.tenant_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            try:
                return await ensure_sip_ref_on_conn(
                    conn,
                    tenant_id=ctx.tenant.id,
                    conversation_key=conversation_key,
                    phone_number_id=phone_number_id,
                    context=context,
                    metadata=metadata,
                    userdata_seed=userdata_seed,
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except ConversationRefConflict as exc:
                raise HTTPException(status_code=409, detail=exc.message) from exc


# ── CoPilots ────────────────────────────────────────────────────────────────


async def ensure_copilot_ref(
    ctx: Context,
    *,
    kind: ConversationRefKind,
    subject_id: UUID,
) -> ConversationThread:
    """One CoPilot conversation per subject — an agent for AgentCoPilot, a tool
    for ToolCoPilot, a task for TaskCoPilot. ``kind`` names which
    copilot; ``bind_id`` is what it edits.

    Always ``transcript``, and always will be: one chat per subject is what the
    three unique indexes on ``conversation_refs`` enforce, and an editor chat
    that started over on every reload would be a different product.
    """
    key = copilot_conversation_key(kind, subject_id)
    pool = await ctx.tenant_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            try:
                return await ensure_thread(
                    conn,
                    tenant_id=ctx.tenant.id,
                    conversation_key=key,
                    kind=kind,
                    source=kind,
                    context="transcript",
                    bind_id=subject_id,
                )
            except ConversationRefConflict as exc:
                raise HTTPException(status_code=409, detail=exc.message) from exc


async def require_ref_by_key(
    ctx: Context,
    *,
    conversation_key: str,
) -> ConversationRef:
    """Load any ref by key; 404 if missing. Web keys are validated; channel keys pass through."""
    try:
        key = validate_web_conversation_key(conversation_key)
    except ValueError as exc:
        key = conversation_key.strip()
        if not key:
            raise HTTPException(status_code=400, detail="conversation_key is required") from exc

    pool = await ctx.tenant_pool()
    async with pool.acquire() as conn:
        ref = await get_ref_by_key(conn, tenant_id=ctx.tenant.id, conversation_key=key)
    if ref is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    return ref
