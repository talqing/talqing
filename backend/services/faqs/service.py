"""FAQ CRUD, and the read a session makes when it starts.

Content is live and unversioned: an edit reaches every agent using the FAQ from
its next session. Every entry write goes through `_touch`, which bumps the
FAQ's `updated_at` and — because that is a row lock held to the end of the
transaction — serializes entry writes to one FAQ, so the duplicate-question and
500-entry checks below read a list nothing else is changing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from uuid import UUID

import asyncpg
from fastapi import HTTPException

import db
from api.core.schemas import ErrorBody, OkResponse, Page, page_slice
from services.user import Context, Tenant

from .models import (
    MAX_ENTRIES,
    CreateFaqEntriesRequest,
    FaqDefinition,
    FaqDetail,
    FaqEntriesResponse,
    FaqEntry,
    FaqEntryInput,
    FaqForPrompt,
    FaqSummary,
    UpdateFaqEntryRequest,
    UpdateFaqRequest,
    question_key,
)

_ENTRY_COLUMNS = "id, question, answer, created_at, updated_at"
_FAQ_SELECT = """
    SELECT f.id, f.name, f.created_by, f.created_at, f.updated_at,
        (SELECT count(*) FROM faq_entries e
         WHERE e.faq_id = f.id AND e.tenant_id = f.tenant_id) AS entry_count
    FROM faqs f
"""


@dataclass(frozen=True)
class FaqRef:
    """id + name + size — the workspace inventory a CoPilot is shown."""

    id: UUID
    name: str
    entry_count: int


def _unprocessable(errors: list[str]) -> HTTPException:
    """A 422 in the shape request validation answers with, for the rules that
    need the stored rows to check."""
    message = errors[0] if len(errors) == 1 else "invalid request"
    return HTTPException(
        status_code=422, detail=ErrorBody(message=message, errors=errors).model_dump()
    )


def _name_taken(name: str) -> HTTPException:
    return HTTPException(status_code=409, detail=f"an FAQ named '{name}' already exists")


def _entry_out(row: asyncpg.Record) -> FaqEntry:
    return FaqEntry(**dict(row))


def _summary_out(row: asyncpg.Record) -> FaqSummary:
    return FaqSummary(**dict(row))


async def list_faq_refs(tenant: Tenant) -> list[FaqRef]:
    pool = await db.tenant_pool(tenant)
    rows = await pool.fetch(
        f"{_FAQ_SELECT} WHERE f.tenant_id = $1 ORDER BY lower(f.name)", tenant.id
    )
    return [FaqRef(id=r["id"], name=r["name"], entry_count=r["entry_count"]) for r in rows]


async def load_stored_faqs(tenant: Tenant, faq_ids: list[UUID]) -> dict[UUID, FaqForPrompt]:
    """The FAQs a session attaches by id, questions and answers together.

    One statement, entries in their stable read order. An id that does not
    resolve is simply absent; an FAQ with no entries comes back with none.
    """
    pool = await db.tenant_pool(tenant)
    rows = await pool.fetch(
        """
        SELECT f.id, f.name, e.question, e.answer
        FROM faqs f
        LEFT JOIN faq_entries e ON e.faq_id = f.id AND e.tenant_id = f.tenant_id
        WHERE f.tenant_id = $1 AND f.id = ANY($2::uuid[])
        ORDER BY e.created_at, e.id
        """,
        tenant.id,
        faq_ids,
    )
    loaded: dict[UUID, FaqForPrompt] = {}
    for r in rows:
        faq = loaded.setdefault(r["id"], FaqForPrompt(name=r["name"], entries=[]))
        if r["question"] is not None:
            faq.entries.append((r["question"], r["answer"]))
    return loaded


async def list_faqs(ctx: Context, limit: int, offset: int) -> Page[FaqSummary]:
    pool = await ctx.tenant_pool()
    rows = await pool.fetch(
        f"{_FAQ_SELECT} WHERE f.tenant_id = $1 ORDER BY lower(f.name) LIMIT $2 OFFSET $3",
        ctx.tenant.id,
        limit + 1,
        offset,
    )
    return page_slice([_summary_out(r) for r in rows], limit=limit, offset=offset)


async def insert_faq(conn: asyncpg.Connection, ctx: Context, faq: FaqDefinition) -> asyncpg.Record:
    """The `faqs` row and its entries, inside the caller's transaction.

    Raises `asyncpg.UniqueViolationError` on a taken name — the two callers
    (`create_faq`, and an agent write materializing an inline FAQ) word that
    refusal differently.
    """
    row = await conn.fetchrow(
        "INSERT INTO faqs (tenant_id, name, created_by) VALUES ($1, $2, $3) "
        "RETURNING id, name, created_by, created_at, updated_at",
        ctx.tenant.id,
        faq.name,
        ctx.user.id,
    )
    await _insert_entries(conn, ctx, row["id"], faq.entries)
    return row


async def _insert_entries(
    conn: asyncpg.Connection, ctx: Context, faq_id: UUID, entries: list[FaqEntryInput]
) -> list[asyncpg.Record]:
    """Append ``entries`` in the order given.

    `created_at` is the read order, and one statement stamps every row with the
    same `now()` — so each row is offset by its position, or a bulk add would
    read back shuffled by random id.
    """
    rows = await conn.fetch(
        f"""
        INSERT INTO faq_entries (faq_id, question, answer, created_at, updated_at, tenant_id)
        SELECT $1, e.question, e.answer, e.added_at, e.added_at, $4
        FROM (
            SELECT question, answer, now() + position * interval '1 microsecond' AS added_at
            FROM unnest($2::text[], $3::text[]) WITH ORDINALITY AS u(question, answer, position)
        ) e
        RETURNING {_ENTRY_COLUMNS}
        """,
        faq_id,
        [e.question for e in entries],
        [e.answer for e in entries],
        ctx.tenant.id,
    )
    return sorted(rows, key=lambda r: r["created_at"])


async def create_faq(body: FaqDefinition, ctx: Context) -> FaqDetail:
    pool = await ctx.tenant_pool()
    try:
        async with pool.acquire() as conn, conn.transaction():
            row = await insert_faq(conn, ctx, body)
    except asyncpg.UniqueViolationError:
        raise _name_taken(body.name) from None
    return await get_faq(row["id"], ctx)


async def get_faq(faq_id: UUID, ctx: Context) -> FaqDetail:
    pool = await ctx.tenant_pool()
    row = await pool.fetchrow(
        f"{_FAQ_SELECT} WHERE f.id = $1 AND f.tenant_id = $2", faq_id, ctx.tenant.id
    )
    if row is None:
        raise HTTPException(status_code=404, detail="FAQ not found")
    entries = await pool.fetch(
        f"SELECT {_ENTRY_COLUMNS} FROM faq_entries "
        "WHERE faq_id = $1 AND tenant_id = $2 ORDER BY created_at, id",
        faq_id,
        ctx.tenant.id,
    )
    return FaqDetail(**dict(row), entries=[_entry_out(e) for e in entries])


async def update_faq(faq_id: UUID, body: UpdateFaqRequest, ctx: Context) -> FaqSummary:
    pool = await ctx.tenant_pool()
    try:
        updated = await pool.fetchval(
            "UPDATE faqs SET name = $3, updated_at = now() "
            "WHERE id = $1 AND tenant_id = $2 RETURNING id",
            faq_id,
            ctx.tenant.id,
            body.name,
        )
    except asyncpg.UniqueViolationError:
        raise _name_taken(body.name) from None
    if updated is None:
        raise HTTPException(status_code=404, detail="FAQ not found")
    row = await pool.fetchrow(
        f"{_FAQ_SELECT} WHERE f.id = $1 AND f.tenant_id = $2", faq_id, ctx.tenant.id
    )
    return _summary_out(row)


async def delete_faq(faq_id: UUID, ctx: Context) -> OkResponse:
    pool = await ctx.tenant_pool()
    # The same rule `delete_tool` keeps: refuse while an agent or a task names
    # it in its draft or in the version currently serving sessions, and say
    # which. A session whose FAQ is gone would run without it and answer from
    # the model's own guesswork — the opposite of what attaching one is for.
    # Older versions in the history are allowed to dangle, or one publish would
    # make an FAQ undeletable for ever.
    in_use = await pool.fetch(
        """
        SELECT DISTINCT owner, name FROM (
            SELECT 'agent' AS owner, a.name, c.config
            FROM agents a
            LEFT JOIN agent_versions av
                ON av.agent_id = a.id AND av.tenant_id = a.tenant_id
                AND av.version = a.published_version
            CROSS JOIN LATERAL (VALUES (a.config), (av.config)) AS c(config)
            WHERE a.tenant_id = $2
            UNION ALL
            SELECT 'task', t.name, c.config
            FROM agent_tasks t
            LEFT JOIN agent_task_versions tv
                ON tv.task_id = t.id AND tv.tenant_id = t.tenant_id
                AND tv.version = t.published_version
            CROSS JOIN LATERAL (VALUES (t.config), (tv.config)) AS c(config)
            WHERE t.tenant_id = $2
        ) owners
        WHERE config IS NOT NULL AND config @> $1::jsonb
        ORDER BY owner, name
        """,
        json.dumps({"faqs": [{"faq_id": str(faq_id)}]}),
        ctx.tenant.id,
    )
    if in_use:
        names = ", ".join(f"{r['owner']} '{r['name']}'" for r in in_use)
        raise HTTPException(
            status_code=409,
            detail=f"this FAQ is attached to {names} — detach it there before deleting it",
        )
    deleted = await pool.fetchval(
        "DELETE FROM faqs WHERE id = $1 AND tenant_id = $2 RETURNING id", faq_id, ctx.tenant.id
    )
    if deleted is None:
        raise HTTPException(status_code=404, detail="FAQ not found")
    return OkResponse()


async def _touch(conn: asyncpg.Connection, faq_id: UUID, ctx: Context) -> None:
    """Mark the FAQ edited and hold its row for this transaction (see the module
    docstring). 404 when it is not this workspace's."""
    touched = await conn.fetchval(
        "UPDATE faqs SET updated_at = now() WHERE id = $1 AND tenant_id = $2 RETURNING id",
        faq_id,
        ctx.tenant.id,
    )
    if touched is None:
        raise HTTPException(status_code=404, detail="FAQ not found")


async def create_faq_entries(
    faq_id: UUID, body: CreateFaqEntriesRequest, ctx: Context
) -> FaqEntriesResponse:
    """All or nothing: one entry that cannot be added refuses the whole request."""
    pool = await ctx.tenant_pool()
    async with pool.acquire() as conn, conn.transaction():
        await _touch(conn, faq_id, ctx)
        existing = await conn.fetch(
            "SELECT question FROM faq_entries WHERE faq_id = $1 AND tenant_id = $2",
            faq_id,
            ctx.tenant.id,
        )
        if len(existing) + len(body.entries) > MAX_ENTRIES:
            raise _unprocessable(
                [
                    f"an FAQ holds at most {MAX_ENTRIES} questions - this one has "
                    f"{len(existing)}, and this would add {len(body.entries)}"
                ]
            )
        taken = {question_key(r["question"]) for r in existing}
        if repeated := [e.question for e in body.entries if question_key(e.question) in taken]:
            raise _unprocessable([f"this FAQ already has the question '{q}'" for q in repeated])
        rows = await _insert_entries(conn, ctx, faq_id, body.entries)
    return FaqEntriesResponse(entries=[_entry_out(r) for r in rows])


async def update_faq_entry(
    faq_id: UUID, entry_id: UUID, body: UpdateFaqEntryRequest, ctx: Context
) -> FaqEntry:
    pool = await ctx.tenant_pool()
    async with pool.acquire() as conn, conn.transaction():
        await _touch(conn, faq_id, ctx)
        if body.question is not None and await conn.fetchval(
            "SELECT 1 FROM faq_entries WHERE faq_id = $1 AND tenant_id = $2 "
            "AND id <> $3 AND lower(btrim(question)) = $4",
            faq_id,
            ctx.tenant.id,
            entry_id,
            question_key(body.question),
        ):
            raise _unprocessable([f"this FAQ already has the question '{body.question}'"])
        row = await conn.fetchrow(
            f"""
            UPDATE faq_entries
            SET question = COALESCE($4, question), answer = COALESCE($5, answer),
                updated_at = now()
            WHERE id = $1 AND faq_id = $2 AND tenant_id = $3
            RETURNING {_ENTRY_COLUMNS}
            """,
            entry_id,
            faq_id,
            ctx.tenant.id,
            body.question,
            body.answer,
        )
        if row is None:
            raise HTTPException(status_code=404, detail="FAQ entry not found")
    return _entry_out(row)


async def delete_faq_entry(faq_id: UUID, entry_id: UUID, ctx: Context) -> OkResponse:
    pool = await ctx.tenant_pool()
    async with pool.acquire() as conn, conn.transaction():
        await _touch(conn, faq_id, ctx)
        deleted = await conn.fetchval(
            "DELETE FROM faq_entries WHERE id = $1 AND faq_id = $2 AND tenant_id = $3 RETURNING id",
            entry_id,
            faq_id,
            ctx.tenant.id,
        )
        if deleted is None:
            raise HTTPException(status_code=404, detail="FAQ entry not found")
    return OkResponse()
