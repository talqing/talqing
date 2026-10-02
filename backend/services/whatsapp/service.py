"""Everything a human does to a WhatsApp batch: upload, review, send, steer.

**The batch row is policy, the recipient rows are work** — the split batch
calling and email use. Pause, resume, reschedule and pacing edits are writes to
the batch row; the send loop re-reads it on every turn, so there is nothing to
coordinate.

**`ready` means sendable.** A `ready` row has a phone number no earlier row
holds, and every mapped cell of it can be sent as a template variable. A row
that fails either is kept and `skipped` (`unfillable`, `duplicate_recipient`)
with the reason, until its cell is fixed. ``reconcile`` is the one place that
decides it, and every writer that can change the answer — the upload, a cell
edit, a restore, a new variable map — calls it in the same transaction.

**A batch fails only when no resume could work.** A broken credential, a paused
template or an empty wallet pauses it with what to do; `failed` is for a deleted
sender or a creator who may no longer send.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import asyncpg
from fastapi import HTTPException

import db
from api.core.schemas import Page, page_slice, validation_error
from services import jobs, webhooks
from services.email.batch.models import (
    RecipientActionResponse,
    RejectedRecipient,
    SelectRecipientsRequest,
)
from services.email.batch.service import validate_start_at
from services.integrations.models import INTEGRATION_COLUMNS, Integration
from services.integrations.providers.whatsapp import WHATSAPP_MESSAGE_TRIGGER, bsp_client
from services.integrations.providers.whatsapp.bsp import BspError, Template, variable_problem
from services.integrations.providers.whatsapp.twilio import TwilioWhatsApp
from services.scheduling import (
    CapColumns,
    cap_columns,
    completed_days,
    daily_cap_today,
    local_day_bounds,
    local_today,
    ramp_base_days,
)
from services.secrets import load_secrets
from services.telephony.e164 import normalize_e164
from services.user import Context, Tenant
from services.userdata import is_reserved_key
from services.webhooks import events

from .models import (
    BATCH_COLUMNS,
    RECIPIENT_COLUMNS,
    AddWhatsAppRecipientsRequest,
    AddWhatsAppRecipientsResponse,
    CreateWhatsAppBatchRequest,
    PatchWhatsAppBatchRequest,
    PatchWhatsAppRecipientRequest,
    SendWhatsAppBatchRequest,
    SkippedWhatsAppRecipient,
    WhatsAppBatch,
    WhatsAppBatchCounts,
    WhatsAppBatchResponse,
    WhatsAppRecipient,
    WhatsAppRecipientResponse,
    WhatsAppTemplate,
    WhatsAppTemplatesResponse,
    rendered,
    variable_values,
)

# What to do about a stop the batch made itself, said at the end of its reason.
REMEDY = {
    "account": "fix the sender's account or credential, then resume",
    "template": "check the template at your BSP, then resume",
    "wallet": "top up your BSP wallet, then resume",
}


def rate_limit_reason(code: str | None) -> str:
    return (
        f"WhatsApp is limiting this sender{f' (error {code})' if code else ''}. "
        "This is usually the business's daily messaging limit; resume later."
    )


logger = logging.getLogger("talqing.whatsapp.batch")

# Where `null` means "clear it" rather than "leave it alone".
_NULL_MEANS_SOMETHING = frozenset({"start_at", "window", "send_daily_cap"})

# Rows a person may still skip, restore or edit, on a batch still in play. A
# `completed` batch is idle, not over: a row fixed there wakes it.
_OPEN_BATCH = ("draft", "scheduled", "sending", "paused", "completed")


# ── reads ───────────────────────────────────────────────────────────────────


async def load_batch(conn, tenant_id: UUID, batch_id: UUID) -> WhatsAppBatch | None:
    row = await conn.fetchrow(
        f"SELECT {BATCH_COLUMNS} FROM whatsapp_batches WHERE id = $1 AND tenant_id = $2",
        batch_id,
        tenant_id,
    )
    return WhatsAppBatch.model_validate(dict(row)) if row else None


async def _require_batch(ctx: Context, batch_id: UUID) -> WhatsAppBatch:
    batch = await load_batch(await ctx.tenant_pool(), ctx.tenant.id, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="WhatsApp batch not found")
    return batch


async def load_integration(conn, tenant_id: UUID, integration_id: UUID) -> Integration | None:
    row = await conn.fetchrow(
        f"""
        SELECT {INTEGRATION_COLUMNS} FROM integrations
        WHERE id = $1 AND tenant_id = $2 AND provider = 'whatsapp'
        """,
        integration_id,
        tenant_id,
    )
    return Integration.from_row(row) if row else None


async def _responses(
    conn, tenant_id: UUID, batches: list[WhatsAppBatch]
) -> list[WhatsAppBatchResponse]:
    """Serialize batches with their counts, in three queries whatever the page size."""
    if not batches:
        return []
    ids = [b.id for b in batches]
    counts: dict[UUID, Counter[str]] = {b.id: Counter() for b in batches}
    for row in await conn.fetch(
        """
        SELECT batch_id, status, count(*) AS n,
               count(*) FILTER (WHERE replied_at IS NOT NULL) AS replied
        FROM whatsapp_batch_recipients
        WHERE tenant_id = $1 AND batch_id = ANY($2::uuid[])
        GROUP BY batch_id, status
        """,
        tenant_id,
        ids,
    ):
        counts[row["batch_id"]][row["status"]] += row["n"]
        counts[row["batch_id"]]["replied"] += row["replied"]

    # Each batch's "today" is its own timezone's.
    now = datetime.now(UTC)
    bounds = [local_day_bounds(now, b.timezone) for b in batches]
    sent_today = {
        r["batch_id"]: r["n"]
        for r in await conn.fetch(
            """
            SELECT d.batch_id, count(r.id) AS n
            FROM unnest($2::uuid[], $3::timestamptz[], $4::timestamptz[]) AS d(batch_id, lo, hi)
            LEFT JOIN whatsapp_batch_recipients r
              ON r.tenant_id = $1 AND r.batch_id = d.batch_id
             AND r.sent_at >= d.lo AND r.sent_at < d.hi
            GROUP BY d.batch_id
            """,
            tenant_id,
            ids,
            [b[0] for b in bounds],
            [b[1] for b in bounds],
        )
    }

    senders = {
        r["id"]: r
        for r in await conn.fetch(
            """
            SELECT i.id, i.display_name, i.provider_account_info ->> 'sender_e164' AS sender_e164,
                   t.agent_id, a.name AS agent_name
            FROM integrations i
            LEFT JOIN integration_triggers t
              ON t.integration_id = i.id AND t.tenant_id = i.tenant_id
             AND t.trigger_type = $3 AND t.enabled AND t.status = 'active'
            LEFT JOIN agents a ON a.id = t.agent_id AND a.tenant_id = t.tenant_id
            WHERE i.tenant_id = $1 AND i.id = ANY($2::uuid[])
            """,
            tenant_id,
            list({b.integration_id for b in batches if b.integration_id}),
            WHATSAPP_MESSAGE_TRIGGER,
        )
    }

    out: list[WhatsAppBatchResponse] = []
    for batch in batches:
        c = counts[batch.id]
        sender = senders.get(batch.integration_id) if batch.integration_id else None
        out.append(
            WhatsAppBatchResponse(
                id=batch.id,
                name=batch.name,
                integration_id=batch.integration_id,
                integration_name=sender["display_name"] if sender else None,
                sender_e164=sender["sender_e164"] if sender else None,
                agent_id=sender["agent_id"] if sender else None,
                agent_name=sender["agent_name"] if sender else None,
                template=WhatsAppTemplate(
                    id=batch.template_id,
                    name=batch.template_name,
                    language=batch.template_language,
                    category=batch.template_category,
                    header=batch.template_header,
                    body=batch.template_body,
                    footer=batch.template_footer,
                    buttons=batch.template_buttons,
                    variables=batch.template_variables,
                    unsupported_reason=None,
                ),
                to_column=batch.to_column,
                variable_map=batch.variable_map,
                input_columns=batch.input_columns,
                status=batch.status,
                failure_reason=batch.failure_reason,
                start_at=batch.start_at,
                timezone=batch.timezone,
                window=batch.window,
                send_daily_cap=batch.daily_cap,
                send_daily_cap_today=daily_cap_today(
                    batch.daily_cap,
                    base_days=batch.send_ramp_base_days,
                    days=batch.send_days,
                    last_day=batch.last_send_day,
                    today=local_today(now, batch.timezone),
                ),
                send_gap_seconds=batch.send_gap_seconds,
                delivery_attempts=batch.delivery_attempts,
                delivery_retry_after_hours=batch.delivery_retry_after_hours,
                counts=WhatsAppBatchCounts(
                    total=batch.total_recipients,
                    ready=c["ready"],
                    skipped=c["skipped"],
                    sending=c["sending"],
                    queued=c["queued"],
                    sent=c["sent"],
                    delivered=c["delivered"],
                    read=c["read"],
                    undelivered=c["undelivered"],
                    failed=c["failed"],
                    replied=c["replied"],
                ),
                sent_today=sent_today.get(batch.id, 0),
                next_send_at=batch.next_send_at,
                next_send_reason=batch.next_send_reason,
                created_at=batch.created_at,
                started_at=batch.started_at,
                finished_at=batch.finished_at,
            )
        )
    return out


async def batch_response(tenant: Tenant, batch: WhatsAppBatch) -> WhatsAppBatchResponse:
    return (await _responses(await db.tenant_pool(tenant), tenant.id, [batch]))[0]


async def list_batches(
    ctx: Context, *, limit: int, offset: int, status: str | None
) -> Page[WhatsAppBatchResponse]:
    pool = await ctx.tenant_pool()
    rows = await pool.fetch(
        f"""
        SELECT {BATCH_COLUMNS} FROM whatsapp_batches
        WHERE tenant_id = $1 AND ($4::text IS NULL OR status = $4::text)
        ORDER BY created_at DESC
        LIMIT $2 OFFSET $3
        """,
        ctx.tenant.id,
        limit + 1,
        offset,
        status,
    )
    batches = [WhatsAppBatch.model_validate(dict(r)) for r in rows]
    return page_slice(await _responses(pool, ctx.tenant.id, batches), limit=limit, offset=offset)


async def get_batch(ctx: Context, batch_id: UUID) -> WhatsAppBatchResponse:
    return await batch_response(ctx.tenant, await _require_batch(ctx, batch_id))


def _recipient_out(batch: WhatsAppBatch, recipient: WhatsAppRecipient) -> WhatsAppRecipientResponse:
    return WhatsAppRecipientResponse(
        id=recipient.id,
        row_number=recipient.row_number,
        to_e164=recipient.to_e164,
        input=recipient.input,
        overrides=recipient.overrides,
        rendered=rendered(batch, recipient),
        status=recipient.status,
        skip_reason=recipient.skip_reason,
        last_error=recipient.last_error,
        error_code=recipient.error_code,
        send_attempts=recipient.send_attempts,
        delivery_attempts=recipient.delivery_attempts,
        next_attempt_at=recipient.next_attempt_at,
        sent_at=recipient.sent_at,
        delivered_at=recipient.delivered_at,
        read_at=recipient.read_at,
        replied_at=recipient.replied_at,
        conversation_id=recipient.conversation_id,
        updated_at=recipient.updated_at,
    )


async def list_recipients(
    ctx: Context, batch_id: UUID, *, limit: int, offset: int, status: list[str] | None
) -> Page[WhatsAppRecipientResponse]:
    """A batch's rows in file order, matching any of ``status``.

    `replied` filters on `replied_at`, not status.
    """
    batch = await _require_batch(ctx, batch_id)
    pool = await ctx.tenant_pool()
    rows = await pool.fetch(
        f"""
        SELECT {RECIPIENT_COLUMNS} FROM whatsapp_batch_recipients
        WHERE tenant_id = $1 AND batch_id = $2
          AND ($5::text[] IS NULL
               OR ('replied' = ANY($5) AND replied_at IS NOT NULL)
               OR status = ANY($5))
        ORDER BY row_number
        LIMIT $3 OFFSET $4
        """,
        ctx.tenant.id,
        batch_id,
        limit + 1,
        offset,
        status,
    )
    return page_slice(
        [_recipient_out(batch, WhatsAppRecipient.model_validate(dict(r))) for r in rows],
        limit=limit,
        offset=offset,
    )


# ── templates and senders ───────────────────────────────────────────────────


async def _client(tenant: Tenant, integration: Integration):
    try:
        return bsp_client(integration, await load_secrets(tenant))
    except BspError as exc:
        raise validation_error([str(exc)], "this WhatsApp sender cannot send") from None


async def list_templates(ctx: Context, integration_id: UUID) -> WhatsAppTemplatesResponse:
    """The sender's approved templates, read live from the BSP.

    One a batch cannot send is listed too, with why: a template missing from
    the list reads as a broken integration.
    """
    integration = await load_integration(await ctx.tenant_pool(), ctx.tenant.id, integration_id)
    if integration is None:
        raise HTTPException(status_code=404, detail="that WhatsApp sender does not exist")
    client = await _client(ctx.tenant, integration)
    try:
        templates = await client.list_templates()
    except BspError as exc:
        raise validation_error([str(exc)], "could not read this sender's templates") from None
    return WhatsAppTemplatesResponse(
        integration_id=integration_id,
        templates=[WhatsAppTemplate.model_validate(t, from_attributes=True) for t in templates],
    )


# ── create ──────────────────────────────────────────────────────────────────


def _check_variable_map(
    variable_map: dict[str, str], template: Template, columns: list[str], errors: list[str]
) -> None:
    for name in template.variables:
        column = variable_map.get(name)
        if column is None:
            errors.append(f"template variable {{{{{name}}}}} is not mapped to a column")
        elif column not in columns:
            errors.append(f"{{{{{name}}}}} is mapped to {column!r}, which is not a column")
    for name in variable_map:
        if name not in template.variables:
            errors.append(f"the template has no variable {{{{{name}}}}}")


async def create_batch(body: CreateWhatsAppBatchRequest, ctx: Context) -> WhatsAppBatchResponse:
    """Validate the batch's shape, then write it and its rows. Nothing is sent.

    A problem with the batch itself is refused, every one named in one 400. A
    problem with one row's cells never is: ``reconcile`` skips that row with the
    reason, and a person fixes the cell on the batch page.
    """
    errors: list[str] = []
    pool = await ctx.tenant_pool()
    integration = await load_integration(pool, ctx.tenant.id, body.integration_id)
    if integration is None:
        raise validation_error(["that WhatsApp sender does not exist"], "invalid WhatsApp batch")
    if integration.status != "active":
        raise validation_error(
            [f"{integration.display_name} is {integration.status}; activate it first"],
            "invalid WhatsApp batch",
        )
    client = await _client(ctx.tenant, integration)
    template: Template | None = None
    try:
        if isinstance(client, TwilioWhatsApp):
            await client.require_online_sender()
        template = next(
            (t for t in await client.list_templates() if t.id == body.template_id), None
        )
    except BspError as exc:
        raise validation_error([str(exc)], "invalid WhatsApp batch") from None
    if template is None:
        raise validation_error(
            [f"{body.template_id} is not an approved template on this sender"],
            "invalid WhatsApp batch",
        )
    if template.unsupported_reason:
        raise validation_error([template.unsupported_reason], "invalid WhatsApp batch")

    inputs = [dict(r.input) for r in body.recipients]
    columns = _upload_columns(inputs, errors)
    if body.to_column not in columns:
        errors.append(f"the phone column {body.to_column!r} is not in the list")
    _check_variable_map(body.variable_map, template, columns, errors)
    sent = body.model_fields_set
    for field in sorted(
        {"timezone", "send_gap_seconds", "delivery_attempts", "delivery_retry_after_hours"} & sent
    ):
        if getattr(body, field) is None:
            errors.append(f"{field} cannot be null")

    sender_e164 = str(integration.provider_account_info["sender_e164"])
    # Almost always the wrong column picked, so it is the batch's problem.
    if not errors and not any(_e164(row.get(body.to_column, ""), sender_e164) for row in inputs):
        errors.append(f"no row has a valid phone number in `{body.to_column}`")
    if errors:
        raise validation_error(errors, "invalid WhatsApp batch")

    async with pool.acquire() as conn:
        async with conn.transaction():
            batch_id = await conn.fetchval(
                """
                INSERT INTO whatsapp_batches (
                    tenant_id, name, integration_id, template_id, template_name,
                    template_language, template_category, template_body, template_variables,
                    to_column, variable_map, input_columns, total_recipients, created_by_user_id,
                    template_header, template_footer, template_buttons
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::jsonb, $12, $13, $14,
                        $15::jsonb, $16, $17::jsonb)
                RETURNING id
                """,
                ctx.tenant.id,
                body.name or template.name,
                integration.id,
                template.id,
                template.name,
                template.language,
                template.category,
                template.body,
                list(template.variables),
                body.to_column,
                json.dumps(body.variable_map),
                columns,
                len(inputs),
                ctx.user.id,
                json.dumps(asdict(template.header)) if template.header else None,
                template.footer,
                json.dumps([asdict(b) for b in template.buttons]),
            )
            await _insert_rows(conn, ctx.tenant.id, batch_id, inputs, first_row=1)
            # Only what was sent: anything omitted keeps the column's default.
            terms: dict[str, Any] = {}
            if "timezone" in sent:
                terms["timezone"] = body.timezone
            if "window" in sent:
                window = body.window
                terms["window_start_local"] = window.start if window else None
                terms["window_end_local"] = window.end if window else None
                if window:
                    terms["window_days"] = window.days
            if "send_daily_cap" in sent:
                # All five, so a ramp also clears the column's default limit.
                terms.update(
                    zip(
                        (
                            "send_daily_cap",
                            "send_ramp_start",
                            "send_ramp_end",
                            "send_ramp_step",
                            "send_ramp_interval_days",
                        ),
                        cap_columns(body.send_daily_cap),
                        strict=True,
                    )
                )
            for field in ("send_gap_seconds", "delivery_attempts", "delivery_retry_after_hours"):
                if field in sent:
                    terms[field] = getattr(body, field)
            if terms:
                assignments = ", ".join(f"{c} = ${i}" for i, c in enumerate(terms, start=3))
                await conn.execute(
                    f"UPDATE whatsapp_batches SET {assignments} WHERE id = $1 AND tenant_id = $2",
                    batch_id,
                    ctx.tenant.id,
                    *terms.values(),
                )
            batch = await load_batch(conn, ctx.tenant.id, batch_id)
            assert batch is not None
            await reconcile(conn, ctx.tenant.id, batch, sender_e164)
    logger.info("created WhatsApp batch %s (%d rows)", batch_id, len(inputs))
    return await batch_response(ctx.tenant, batch)


def _upload_columns(rows: list[dict[str, str]], errors: list[str]) -> list[str]:
    """Every header the upload carries, in first-seen order, with its problems named."""
    columns: list[str] = []
    for row in rows:
        for header in row:
            if header not in columns:
                columns.append(header)
    for header in columns:
        if not header.strip():
            errors.append("a column has no header")
        elif is_reserved_key(header):
            errors.append(f"column {header!r} uses a reserved name")
    return columns


async def _insert_rows(
    conn: asyncpg.Connection,
    tenant_id: UUID,
    batch_id: UUID,
    rows: list[dict[str, str]],
    *,
    first_row: int,
) -> dict[int, UUID]:
    """Store uploaded rows, numbered from `first_row`. ``{row_number: id}``.

    No number yet: `reconcile` reads it from the phone cell.
    """
    inserted = await conn.fetch(
        """
        INSERT INTO whatsapp_batch_recipients (tenant_id, batch_id, row_number, input)
        SELECT $1, $2, r.row_number, r.input::jsonb
        FROM unnest($3::int[], $4::text[]) AS r(row_number, input)
        RETURNING id, row_number
        """,
        tenant_id,
        batch_id,
        list(range(first_row, first_row + len(rows))),
        [json.dumps(row) for row in rows],
    )
    return {r["row_number"]: r["id"] for r in inserted}


async def add_recipients(
    batch_id: UUID, body: AddWhatsAppRecipientsRequest, ctx: Context
) -> AddWhatsAppRecipientsResponse:
    """Append rows to a batch. **On a live or completed batch this messages people.**

    A number the batch already holds, or one an earlier row of this upload has,
    is dropped and reported, so a re-pushed CRM export is a no-op. A row whose
    cells cannot be sent is stored `skipped` with why, as at create, so a person
    can fix the cell. A `completed` batch with a new `ready` row is woken.

    Under the batch row's lock from the first statement to the commit: the send
    loop decides it is finished under the same lock, so rows never land on a
    batch that has just completed without them.
    """
    inputs = [dict(r.input) for r in body.recipients]
    errors: list[str] = []
    upload_columns = _upload_columns(inputs, errors)
    pool = await ctx.tenant_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                f"SELECT {BATCH_COLUMNS} FROM whatsapp_batches "
                "WHERE id = $1 AND tenant_id = $2 FOR UPDATE",
                batch_id,
                ctx.tenant.id,
            )
            if row is None:
                raise HTTPException(status_code=404, detail="WhatsApp batch not found")
            batch = WhatsAppBatch.model_validate(dict(row))
            if batch.status in ("canceled", "failed"):
                raise HTTPException(
                    status_code=409,
                    detail=f"this batch is {batch.status}, so rows cannot be added to it",
                )
            sender_e164 = await _sender_e164(conn, ctx.tenant.id, batch)
            if sender_e164 is None:
                raise HTTPException(
                    status_code=409, detail="this batch's WhatsApp sender was deleted"
                )
            if errors:
                raise validation_error(errors, "invalid recipients")

            numbers = {
                position: _e164(row.get(batch.to_column, ""), sender_e164)
                for position, row in enumerate(inputs, start=1)
            }
            already = {
                r["to_e164"]
                for r in await conn.fetch(
                    """
                    SELECT to_e164 FROM whatsapp_batch_recipients
                    WHERE tenant_id = $1 AND batch_id = $2 AND to_e164 = ANY($3::text[])
                    """,
                    ctx.tenant.id,
                    batch_id,
                    [n for n in numbers.values() if n],
                )
            }
            skipped: list[SkippedWhatsAppRecipient] = []
            kept: list[tuple[int, dict[str, str]]] = []
            first_seen: dict[str, int] = {}
            for position, row in enumerate(inputs, start=1):
                # No valid number is stored all the same: `reconcile` skips it
                # with why, and the cell can be fixed.
                number = numbers[position]
                if number and number in already:
                    reason = "already in this batch"
                elif number and number in first_seen:
                    reason = f"same number as row {first_seen[number]}"
                else:
                    if number:
                        first_seen[number] = position
                    kept.append((position, row))
                    continue
                skipped.append(
                    SkippedWhatsAppRecipient(row_number=position, reason=reason, recipient_id=None)
                )

            next_row = await conn.fetchval(
                "SELECT COALESCE(MAX(row_number), 0) + 1 FROM whatsapp_batch_recipients "
                "WHERE tenant_id = $1 AND batch_id = $2",
                ctx.tenant.id,
                batch_id,
            )
            stored_ids = await _insert_rows(
                conn, ctx.tenant.id, batch_id, [row for _, row in kept], first_row=next_row
            )
            position_of = {
                stored_ids[next_row + i]: position for i, (position, _) in enumerate(kept)
            }
            updated = await conn.fetchrow(
                f"""
                UPDATE whatsapp_batches
                SET input_columns = $3, total_recipients = total_recipients + $4,
                    updated_at = now()
                WHERE id = $1 AND tenant_id = $2
                RETURNING {BATCH_COLUMNS}
                """,
                batch_id,
                ctx.tenant.id,
                batch.input_columns + [c for c in upload_columns if c not in batch.input_columns],
                len(kept),
            )
            batch = WhatsAppBatch.model_validate(dict(updated))
            await reconcile(conn, ctx.tenant.id, batch, sender_e164, list(position_of))
            for r in await conn.fetch(
                """
                SELECT id, last_error FROM whatsapp_batch_recipients
                WHERE tenant_id = $1 AND id = ANY($2::uuid[]) AND status = 'skipped'
                """,
                ctx.tenant.id,
                list(position_of),
            ):
                skipped.append(
                    SkippedWhatsAppRecipient(
                        row_number=position_of[r["id"]],
                        reason=r["last_error"],
                        recipient_id=r["id"],
                    )
                )
            skipped.sort(key=lambda s: s.row_number)
            # A draft waits for Send, a live loop claims, a paused batch waits.
            woken = await _wake_if_ready(conn, ctx.tenant.id, batch_id)
    logger.info(
        "WhatsApp batch %s: %d row(s) added, %d skipped%s",
        batch_id,
        len(kept),
        len(skipped),
        "; woken" if woken else "",
    )
    return AddWhatsAppRecipientsResponse(
        added=len(kept), skipped=skipped, total_recipients=batch.total_recipients
    )


async def _wake_if_ready(conn: asyncpg.Connection, tenant_id: UUID, batch_id: UUID) -> bool:
    """A `completed` batch with a `ready` row is sending again, with a job.

    `started_at` is kept, so `whatsapp_batch.started` does not fire again.
    """
    woken = await conn.fetchval(
        """
        UPDATE whatsapp_batches b
        SET status = 'sending', finished_at = NULL,
            next_send_at = NULL, next_send_reason = NULL, updated_at = now()
        WHERE b.id = $1 AND b.tenant_id = $2 AND b.status = 'completed'
          AND EXISTS (
            SELECT 1 FROM whatsapp_batch_recipients r
            WHERE r.tenant_id = $2 AND r.batch_id = $1 AND r.status = 'ready'
          )
        RETURNING b.id
        """,
        batch_id,
        tenant_id,
    )
    if woken is None:
        return False
    await jobs.enqueue_once(
        conn,
        tenant_id=tenant_id,
        kind=jobs.JobKind.WHATSAPP_SEND,
        scheduled_at=datetime.now(UTC),
        subject_id=batch_id,
        args={"batch_id": str(batch_id)},
    )
    return True


# ── the one rule ────────────────────────────────────────────────────────────


def _e164(cell: str, sender_e164: str) -> str | None:
    """A phone cell as E.164, or ``None`` when it is not a number.

    National-format numbers are national relative to the sender.
    """
    try:
        return normalize_e164(cell, did=sender_e164)
    except ValueError:
        return None


async def _sender_e164(conn, tenant_id: UUID, batch: WhatsAppBatch) -> str | None:
    """The batch's sender number, or ``None`` once its integration is gone."""
    integration = (
        await load_integration(conn, tenant_id, batch.integration_id)
        if batch.integration_id
        else None
    )
    return str(integration.provider_account_info["sender_e164"]) if integration else None


def unfillable_reason(batch: WhatsAppBatch, recipient: WhatsAppRecipient) -> str | None:
    """Why this row's variables cannot be sent, or ``None`` when they can."""
    values = variable_values(batch, recipient)
    for name in batch.template_variables:
        column = batch.variable_map.get(name, "")
        if problem := variable_problem(values.get(name)):
            return f"{column} {problem}"
    return None


async def reconcile(
    conn: asyncpg.Connection,
    tenant_id: UUID,
    batch: WhatsAppBatch,
    sender_e164: str | None,
    ids: list[UUID] | None = None,
) -> None:
    """Put every row this owns in the status, and on the number, its cells justify.

    It owns `ready` rows and the ones it skipped itself. A row a person skipped,
    or one already `sending` or later, is never written: it holds the number it
    has. Among the rows it owns, the lowest-numbered row with a number holds it.

    ``ids`` narrows it to rows whose phone cell did not change. With no
    ``sender_e164`` (the integration is gone) numbers are left as they are.
    """
    # Locked, so the send loop's claim (`SKIP LOCKED`) passes over them meanwhile.
    recipients = [
        WhatsAppRecipient.model_validate(dict(row))
        for row in await conn.fetch(
            f"""
            SELECT {RECIPIENT_COLUMNS} FROM whatsapp_batch_recipients
            WHERE tenant_id = $1 AND batch_id = $2
              AND (status = 'ready' OR (status = 'skipped' AND skip_reason <> 'operator'))
              AND ($3::uuid[] IS NULL OR id = ANY($3::uuid[]))
            ORDER BY row_number
            FOR UPDATE
            """,
            tenant_id,
            batch.id,
            ids,
        )
    ]
    numbers = {
        r.id: _e164(r.columns().get(batch.to_column, ""), sender_e164) if sender_e164 else r.to_e164
        for r in recipients
    }
    # number -> the row holding it, starting from the rows not being decided here.
    held: dict[str, int] = {
        row["to_e164"]: row["row_number"]
        for row in await conn.fetch(
            """
            SELECT to_e164, row_number FROM whatsapp_batch_recipients
            WHERE tenant_id = $1 AND batch_id = $2
              AND to_e164 = ANY($3::text[]) AND id <> ALL($4::uuid[])
            """,
            tenant_id,
            batch.id,
            [n for n in numbers.values() if n],
            [r.id for r in recipients],
        )
    }
    moves: list[tuple[UUID, str, str | None, str | None, str | None]] = []
    for recipient in recipients:
        number = numbers[recipient.id]
        target: tuple[str, str | None, str | None, str | None]
        if number is None:
            if sender_e164 is None:
                continue
            cell = recipient.columns().get(batch.to_column, "")
            target = (
                "skipped",
                "unfillable",
                None,
                f"{cell!r} is not a valid phone number"
                if cell.strip()
                else f"{batch.to_column} is empty",
            )
        elif (holder := held.setdefault(number, recipient.row_number)) != recipient.row_number:
            target = ("skipped", "duplicate_recipient", None, f"same number as row {holder}")
        elif reason := unfillable_reason(batch, recipient):
            target = ("skipped", "unfillable", number, reason)
        else:
            target = ("ready", None, number, None)
        current = (recipient.status, recipient.skip_reason, recipient.to_e164, recipient.last_error)
        if current != target:
            moves.append((recipient.id, *target))
    if not moves:
        return
    # Two statements, so `uq_whatsapp_recipients_to` never sees one number on
    # two rows: every row giving a number up does so before any row takes one.
    previous = {r.id: r.to_e164 for r in recipients}
    await conn.execute(
        "UPDATE whatsapp_batch_recipients SET to_e164 = NULL "
        "WHERE tenant_id = $1 AND id = ANY($2::uuid[])",
        tenant_id,
        [m[0] for m in moves if previous[m[0]] and previous[m[0]] != m[3]],
    )
    row_ids, status, skip_reason, to_e164, last_error = (list(c) for c in zip(*moves, strict=True))
    await conn.execute(
        """
        UPDATE whatsapp_batch_recipients AS r
        SET status = f.status, skip_reason = f.skip_reason, to_e164 = f.to_e164,
            last_error = f.last_error, updated_at = now()
        FROM unnest($2::uuid[], $3::text[], $4::text[], $5::text[], $6::text[])
            AS f(id, status, skip_reason, to_e164, last_error)
        WHERE r.tenant_id = $1 AND r.id = f.id
        """,
        tenant_id,
        row_ids,
        status,
        skip_reason,
        to_e164,
        last_error,
    )


# ── policy ──────────────────────────────────────────────────────────────────


def _cap_write(
    batch: WhatsAppBatch, body: PatchWhatsAppBatchRequest | SendWhatsAppBatchRequest
) -> tuple[*CapColumns, int]:
    """The cap columns and ramp baseline an edit stores; absent means unchanged.

    An edited ramp keeps its progress unless its start moved; a new one starts
    from the sending days this batch has completed so far.
    """
    if "send_daily_cap" not in body.model_fields_set:
        return *cap_columns(batch.daily_cap), batch.send_ramp_base_days
    base = ramp_base_days(
        body.send_daily_cap,
        batch.daily_cap,
        old_base=batch.send_ramp_base_days,
        completed=completed_days(
            batch.send_days, batch.last_send_day, local_today(datetime.now(UTC), batch.timezone)
        ),
    )
    return *cap_columns(body.send_daily_cap), base


async def patch_batch(
    batch_id: UUID, body: PatchWhatsAppBatchRequest, ctx: Context
) -> WhatsAppBatchResponse:
    """Edit a batch. Pacing reaches a live send on its next turn.

    The variable map freezes once the batch is sent — rows already delivered must
    keep describing what went out. A canceled or failed batch can still be
    renamed; a completed one is idle, so its pacing still applies to rows added
    later.
    """
    batch = await _require_batch(ctx, batch_id)
    sent = body.model_fields_set
    if batch.status in ("canceled", "failed") and sent - {"name"}:
        raise HTTPException(status_code=409, detail=f"a {batch.status} batch cannot be edited")
    errors: list[str] = []
    for field in sorted(sent - _NULL_MEANS_SOMETHING):
        if getattr(body, field) is None:
            errors.append(f"{field} cannot be null")
    if body.variable_map is not None:
        if batch.status != "draft":
            errors.append("the variable map can only change before the batch is sent")
        else:
            _check_variable_map(body.variable_map, batch.template, batch.input_columns, errors)
    if "start_at" in sent:
        if batch.status not in ("draft", "scheduled"):
            errors.append("start_at can only change before sending begins")
        else:
            validate_start_at(body.start_at, errors)
    if errors:
        raise validation_error(errors, "invalid WhatsApp batch")

    def keep(field: str) -> Any:
        return getattr(body, field) if field in sent else getattr(batch, field)

    window = keep("window")
    start_at = keep("start_at")
    pool = await ctx.tenant_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                f"""
                UPDATE whatsapp_batches
                SET name = $3, variable_map = $4::jsonb, start_at = $5, timezone = $6,
                    window_start_local = $7, window_end_local = $8, window_days = $9,
                    send_gap_seconds = $10,
                    delivery_attempts = $11, delivery_retry_after_hours = $12,
                    send_daily_cap = $13, send_ramp_start = $14, send_ramp_end = $15,
                    send_ramp_step = $16, send_ramp_interval_days = $17,
                    send_ramp_base_days = $18,
                    -- While scheduled, the start time IS when it next acts.
                    next_send_at = CASE WHEN status = 'scheduled' THEN $5 ELSE next_send_at END,
                    next_send_reason = CASE
                        WHEN status <> 'scheduled' THEN next_send_reason
                        WHEN $5::timestamptz IS NULL THEN NULL
                        ELSE 'start'
                    END,
                    updated_at = now()
                WHERE id = $1 AND tenant_id = $2
                RETURNING {BATCH_COLUMNS}
                """,
                batch_id,
                ctx.tenant.id,
                keep("name"),
                json.dumps(keep("variable_map")),
                start_at,
                keep("timezone"),
                window.start if window else None,
                window.end if window else None,
                window.days if window else [1, 2, 3, 4, 5, 6, 7],
                keep("send_gap_seconds"),
                keep("delivery_attempts"),
                keep("delivery_retry_after_hours"),
                *_cap_write(batch, body),
            )
            fresh = WhatsAppBatch.model_validate(dict(row))
            if body.variable_map is not None:
                await reconcile(
                    conn, ctx.tenant.id, fresh, await _sender_e164(conn, ctx.tenant.id, fresh)
                )
            if "start_at" in sent and fresh.status == "scheduled":
                # The job IS the schedule; a batch that says Monday and runs
                # today is worse than one that refuses.
                await conn.execute(
                    """
                    UPDATE scheduled_jobs SET scheduled_at = $3, updated_at = now()
                    WHERE tenant_id = $1 AND subject_id = $2 AND kind = $4 AND status = 'pending'
                    """,
                    ctx.tenant.id,
                    batch_id,
                    start_at or datetime.now(UTC),
                    str(jobs.JobKind.WHATSAPP_SEND),
                )
    return await batch_response(ctx.tenant, fresh)


async def send_batch(
    batch_id: UUID, body: SendWhatsAppBatchRequest, ctx: Context
) -> WhatsAppBatchResponse:
    """Start sending — now or at `start_at`. **This messages real people.**"""
    batch = await _require_batch(ctx, batch_id)
    if batch.status != "draft":
        raise HTTPException(status_code=409, detail=f"this batch is already {batch.status}")
    sent = body.model_fields_set
    errors: list[str] = []
    validate_start_at(body.start_at, errors)
    for field in sorted(sent - _NULL_MEANS_SOMETHING):
        if getattr(body, field) is None:
            errors.append(f"{field} cannot be null")
    pool = await ctx.tenant_pool()
    integration = (
        await load_integration(pool, ctx.tenant.id, batch.integration_id)
        if batch.integration_id
        else None
    )
    if integration is None or integration.status != "active":
        errors.append("this batch's WhatsApp sender is disconnected or inactive")
    if errors:
        raise validation_error(errors, "this batch cannot be sent")

    def keep(field: str) -> Any:
        return getattr(body, field) if field in sent else getattr(batch, field)

    window = keep("window")
    async with pool.acquire() as conn:
        async with conn.transaction():
            ready = await conn.fetchval(
                "SELECT count(*) FROM whatsapp_batch_recipients "
                "WHERE tenant_id = $1 AND batch_id = $2 AND status = 'ready'",
                ctx.tenant.id,
                batch_id,
            )
            if not ready:
                raise validation_error(["no row is ready to send"], "this batch cannot be sent")
            row = await conn.fetchrow(
                f"""
                UPDATE whatsapp_batches
                SET status = 'scheduled', start_at = $3, timezone = $4,
                    window_start_local = $5, window_end_local = $6, window_days = $7,
                    send_gap_seconds = $8,
                    delivery_attempts = $9, delivery_retry_after_hours = $10,
                    send_daily_cap = $11, send_ramp_start = $12, send_ramp_end = $13,
                    send_ramp_step = $14, send_ramp_interval_days = $15,
                    send_ramp_base_days = $16,
                    next_send_at = $3,
                    next_send_reason = CASE WHEN $3::timestamptz IS NULL THEN NULL ELSE 'start' END,
                    updated_at = now()
                WHERE id = $1 AND tenant_id = $2 AND status = 'draft'
                RETURNING {BATCH_COLUMNS}
                """,
                batch_id,
                ctx.tenant.id,
                body.start_at,
                keep("timezone"),
                window.start if window else None,
                window.end if window else None,
                window.days if window else [1, 2, 3, 4, 5, 6, 7],
                keep("send_gap_seconds"),
                keep("delivery_attempts"),
                keep("delivery_retry_after_hours"),
                *_cap_write(batch, body),
            )
            if row is None:
                raise HTTPException(status_code=409, detail="this batch was sent already")
            # In the same transaction: a batch `scheduled` with no job behind it
            # would never send.
            await jobs.enqueue_once(
                conn,
                tenant_id=ctx.tenant.id,
                kind=jobs.JobKind.WHATSAPP_SEND,
                scheduled_at=body.start_at or datetime.now(UTC),
                subject_id=batch_id,
                args={"batch_id": str(batch_id)},
            )
    logger.info("WhatsApp batch %s: send requested (%d ready)", batch_id, ready)
    return await batch_response(ctx.tenant, WhatsAppBatch.model_validate(dict(row)))


async def _transition(
    ctx: Context, batch_id: UUID, sql: str, verb: str, allowed: str
) -> WhatsAppBatch:
    await _require_batch(ctx, batch_id)
    pool = await ctx.tenant_pool()
    row = await pool.fetchrow(sql, batch_id, ctx.tenant.id)
    if row is None:
        batch = await _require_batch(ctx, batch_id)
        raise HTTPException(
            status_code=409,
            detail=f"a {batch.status} batch cannot be {verb} (only {allowed} can be)",
        )
    return WhatsAppBatch.model_validate(dict(row))


async def pause_batch(batch_id: UUID, ctx: Context) -> WhatsAppBatchResponse:
    """Stop sending; it takes hold before the next message. Nothing is lost.

    Pausing a `completed` batch makes rows added later wait for a resume.
    """
    batch = await _transition(
        ctx,
        batch_id,
        f"""
        UPDATE whatsapp_batches
        SET status = 'paused', failure_reason = NULL, finished_at = NULL,
            next_send_at = NULL, next_send_reason = NULL, updated_at = now()
        WHERE id = $1 AND tenant_id = $2 AND status IN ('scheduled', 'sending', 'completed')
        RETURNING {BATCH_COLUMNS}
        """,
        "paused",
        "scheduled, sending or completed",
    )
    return await batch_response(ctx.tenant, batch)


async def resume_batch(batch_id: UUID, ctx: Context) -> WhatsAppBatchResponse:
    """Carry on from where it stopped. Resetting both breakers is part of resuming."""
    await _require_batch(ctx, batch_id)
    pool = await ctx.tenant_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                f"""
                UPDATE whatsapp_batches
                SET status = CASE WHEN started_at IS NULL THEN 'scheduled' ELSE 'sending' END,
                    next_send_at = CASE WHEN start_at > now() THEN start_at END,
                    next_send_reason = CASE WHEN start_at > now() THEN 'start' END,
                    failure_reason = NULL, consecutive_send_failures = 0,
                    resumed_at = now(), updated_at = now()
                WHERE id = $1 AND tenant_id = $2 AND status = 'paused'
                RETURNING {BATCH_COLUMNS}
                """,
                batch_id,
                ctx.tenant.id,
            )
            if row is None:
                batch = await _require_batch(ctx, batch_id)
                raise HTTPException(
                    status_code=409, detail=f"a {batch.status} batch cannot be resumed"
                )
            fresh = WhatsAppBatch.model_validate(dict(row))
            await jobs.enqueue_once(
                conn,
                tenant_id=ctx.tenant.id,
                kind=jobs.JobKind.WHATSAPP_SEND,
                scheduled_at=(
                    fresh.start_at
                    if fresh.start_at and fresh.start_at > datetime.now(UTC)
                    else datetime.now(UTC)
                ),
                subject_id=batch_id,
                args={"batch_id": str(batch_id)},
            )
    return await batch_response(ctx.tenant, fresh)


async def cancel_batch(batch_id: UUID, ctx: Context) -> WhatsAppBatchResponse:
    """Stop for good. Messages already sent stay sent and replies still arrive."""
    batch = await _transition(
        ctx,
        batch_id,
        f"""
        UPDATE whatsapp_batches
        SET status = 'canceled', finished_at = now(),
            next_send_at = NULL, next_send_reason = NULL, updated_at = now()
        WHERE id = $1 AND tenant_id = $2 AND status IN ('draft', 'scheduled', 'sending', 'paused')
        RETURNING {BATCH_COLUMNS}
        """,
        "canceled",
        "draft, scheduled, sending or paused",
    )
    return await batch_response(ctx.tenant, batch)


async def delete_batch(batch_id: UUID, ctx: Context) -> None:
    """Delete a batch that is not live, and its rows, for good.

    The conversations it started stay. A receipt or reply arriving afterwards
    still reaches its conversation; it just has no row left to count on.
    """
    await _require_batch(ctx, batch_id)
    pool = await ctx.tenant_pool()
    deleted = await pool.fetchval(
        """
        DELETE FROM whatsapp_batches
        WHERE id = $1 AND tenant_id = $2 AND status IN ('draft', 'completed', 'canceled', 'failed')
        RETURNING id
        """,
        batch_id,
        ctx.tenant.id,
    )
    if deleted is None:
        batch = await _require_batch(ctx, batch_id)
        raise HTTPException(
            status_code=409, detail=f"a {batch.status} batch cannot be deleted; cancel it first"
        )
    logger.info("deleted WhatsApp batch %s", batch_id)


async def stop_batch(
    tenant: Tenant, batch_id: UUID, *, status: str, reason: str
) -> WhatsAppBatch | None:
    """Pause or fail a live batch from the send loop or a receipt, and say so once.

    `paused` whenever a person can fix the cause and resume; `failed` only when
    no resume could work.
    """
    pool = await db.tenant_pool(tenant)
    row = await pool.fetchrow(
        f"""
        UPDATE whatsapp_batches
        SET status = $3, failure_reason = $4,
            finished_at = CASE WHEN $3 = 'failed' THEN now() ELSE finished_at END,
            next_send_at = NULL, next_send_reason = NULL, updated_at = now()
        WHERE id = $1 AND tenant_id = $2 AND status IN ('scheduled', 'sending')
        RETURNING {BATCH_COLUMNS}
        """,
        batch_id,
        tenant.id,
        status,
        reason[:1000],
    )
    if row is None:
        return None
    batch = WhatsAppBatch.model_validate(dict(row))
    logger.error("WhatsApp batch %s %s: %s", batch_id, status, reason)
    await emit(
        tenant,
        events.WHATSAPP_BATCH_FAILED if status == "failed" else events.WHATSAPP_BATCH_PAUSED,
        batch,
    )
    return batch


async def emit(tenant: Tenant, event: str, batch: WhatsAppBatch) -> None:
    payload = await batch_response(tenant, batch)
    await webhooks.dispatch(tenant, event, None, payload.model_dump(mode="json"))


# ── rows ────────────────────────────────────────────────────────────────────


async def patch_recipient(
    batch_id: UUID, recipient_id: UUID, body: PatchWhatsAppRecipientRequest, ctx: Context
) -> WhatsAppRecipientResponse:
    """Edit cells on a row that has not been sent, its phone number included."""
    batch = await _require_batch(ctx, batch_id)
    if batch.status not in _OPEN_BATCH:
        raise HTTPException(status_code=409, detail=f"a {batch.status} batch cannot be edited")
    errors = [
        f"{name!r} is not a column in this batch"
        for name in body.overrides
        if name not in batch.input_columns
    ]
    if errors:
        raise validation_error(errors, "invalid edit")
    pool = await ctx.tenant_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                UPDATE whatsapp_batch_recipients
                SET overrides = jsonb_strip_nulls(overrides || $4::jsonb), updated_at = now()
                WHERE id = $1 AND tenant_id = $2 AND batch_id = $3
                  AND status IN ('ready', 'skipped')
                RETURNING id
                """,
                recipient_id,
                ctx.tenant.id,
                batch_id,
                # A blank value becomes NULL so `jsonb_strip_nulls` drops the key:
                # clearing an edit falls back to the CSV's own cell.
                json.dumps({k: (v if v.strip() else None) for k, v in body.overrides.items()}),
            )
            if row is None:
                existing = await conn.fetchval(
                    "SELECT status FROM whatsapp_batch_recipients "
                    "WHERE id = $1 AND tenant_id = $2 AND batch_id = $3",
                    recipient_id,
                    ctx.tenant.id,
                    batch_id,
                )
                if existing is None:
                    raise HTTPException(status_code=404, detail="that row is not in this batch")
                raise HTTPException(
                    status_code=409, detail=f"this row is {existing} and can no longer be edited"
                )
            # A new phone number can change which row holds which, anywhere in
            # the batch; any other cell only changes this row.
            await reconcile(
                conn,
                ctx.tenant.id,
                batch,
                await _sender_e164(conn, ctx.tenant.id, batch),
                None if batch.to_column in body.overrides else [recipient_id],
            )
            await _wake_if_ready(conn, ctx.tenant.id, batch_id)
            fresh = await conn.fetchrow(
                f"SELECT {RECIPIENT_COLUMNS} FROM whatsapp_batch_recipients "
                "WHERE id = $1 AND tenant_id = $2",
                recipient_id,
                ctx.tenant.id,
            )
    return _recipient_out(batch, WhatsAppRecipient.model_validate(dict(fresh)))


async def act_on_recipients(
    batch_id: UUID, verb: str, body: SelectRecipientsRequest, ctx: Context
) -> RecipientActionResponse:
    """`skip` ready rows, or `restore` the ones a person skipped — named, or `all_eligible`.

    A row ``reconcile`` skipped is not restorable: restoring it would re-run the
    rule that skipped it. It comes back when its cell is fixed.
    """
    if body.recipient_ids is None and body.selection is None:
        raise validation_error(["send either `recipient_ids` or `selection`"], "nothing selected")
    batch = await _require_batch(ctx, batch_id)
    if batch.status not in _OPEN_BATCH:
        raise HTTPException(status_code=409, detail=f"a {batch.status} batch cannot be edited")
    restoring = verb == "restore"
    pool = await ctx.tenant_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            moved = await conn.fetch(
                """
                UPDATE whatsapp_batch_recipients
                SET status = CASE WHEN $4 THEN 'ready' ELSE 'skipped' END,
                    skip_reason = CASE WHEN $4 THEN NULL ELSE 'operator' END,
                    last_error = NULL, updated_at = now()
                WHERE tenant_id = $1 AND batch_id = $2
                  AND ($3::uuid[] IS NULL OR id = ANY($3::uuid[]))
                  AND status = CASE WHEN $4 THEN 'skipped' ELSE 'ready' END
                  AND NOT ($4 AND skip_reason <> 'operator')
                RETURNING id
                """,
                ctx.tenant.id,
                batch_id,
                body.recipient_ids,
                restoring,
            )
            moved_ids = {r["id"] for r in moved}
            if restoring and moved_ids:
                # The whole batch: a restored row's phone cell may have been
                # edited while it was skipped, which reconcile did not follow.
                await reconcile(
                    conn, ctx.tenant.id, batch, await _sender_e164(conn, ctx.tenant.id, batch)
                )
                await _wake_if_ready(conn, ctx.tenant.id, batch_id)
            rejected: list[RejectedRecipient] = []
            if missed := [i for i in body.recipient_ids or [] if i not in moved_ids]:
                found = {
                    r["id"]: r
                    for r in await conn.fetch(
                        "SELECT id, row_number, status, skip_reason, last_error "
                        "FROM whatsapp_batch_recipients "
                        "WHERE tenant_id = $1 AND batch_id = $2 AND id = ANY($3::uuid[])",
                        ctx.tenant.id,
                        batch_id,
                        missed,
                    )
                }
                for rid in missed:
                    r = found.get(rid)
                    if r is None:
                        reason = "not in this batch"
                    elif restoring and r["status"] == "skipped":
                        reason = f"{r['last_error']}; edit the cell instead of restoring the row"
                    else:
                        reason = f"this row is {r['status']}"
                    rejected.append(
                        RejectedRecipient(
                            recipient_id=rid, row_number=r["row_number"] if r else 0, reason=reason
                        )
                    )
    return RecipientActionResponse(affected=len(moved_ids), rejected=rejected)
