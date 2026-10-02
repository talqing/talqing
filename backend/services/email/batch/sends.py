"""Send runs: everything a human does to ONE send.

A send used to be a `scheduled_jobs` row and nothing else, which is why nothing
could steer it — there was no row to write. Now it is `email_send_runs`, and
every ask falls out of that one change as a plain UPDATE:

| schedule it | `start_at` becomes the job's `scheduled_at` |
| pause it    | `status = 'paused'`; the loop claims nothing and ends |
| resume it   | `status = 'sending'` and `enqueue_once` |
| business hours | `window_*` on the run, read by `window_state` |
| its own pace | the run carries its own copy of all four settings |

**A send is a SCOPE, not a set of row writes.** `scope = 'all'` is a standing
send: every row of the batch that reaches `draft`, including rows not drafted
yet. `scope = 'selected'` is a fixed set, recorded once in `email_send_members`.
Neither writes a recipient row to exist or to stop: a draft a live send covers
is still stored `draft` and only READS `queued`, so it stays editable,
skippable and redraftable until the send actually claims it. The claim writes
`send_run_id`, which therefore means "the send that delivered this".

Three rules this module and ``services.email.send`` hold between them:

1. **A standing send and any other send are never live on one batch together**,
   and at most one standing send is (a partial unique index). That is what lets
   the standing claim be a plain `batch_id` predicate with no exclusion join.
   Several `selected` sends may be live together, but a row belongs to at most
   one of them.
2. **A send's scope never changes.** Widening one is another send; narrowing
   one has no defensible answer to "which rows does it keep?". Stopping one
   hands nothing back, because nothing was taken — its unsent rows simply stop
   being covered.
3. **A run's lifecycle is independent of the batch's.** Pausing, cancelling or
   failing the *batch* stops DRAFTING. It must not touch a send somebody has
   created: stopping the spend is not the same act as throwing away the drafts
   you already read.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import asyncpg
from fastapi import HTTPException

import db
from api.core.schemas import Page, page_slice, validation_error
from services import jobs, webhooks
from services.scheduling import (
    cap_columns,
    completed_days,
    daily_cap_today,
    local_today,
    ramp_base_days,
)
from services.user import Context, Tenant

from .models import (
    LIVE_SEND_STATUSES,
    RECIPIENT_COLUMNS,
    SEND_RUN_COLUMNS,
    CreateEmailSendRequest,
    CreateEmailSendResponse,
    EmailBatch,
    EmailSendCounts,
    EmailSendResponse,
    EmailSendRun,
    PatchEmailSendRequest,
    RejectedRecipient,
    is_email_address,
    resolve_send_fields,
)
from .service import (
    _recipient_row,
    _require_batch,
    check_sender,
    validate_start_at,
)

logger = logging.getLogger("talqing.email.send")

# Where an explicit `null` means something rather than "leave it alone" — "no
# start time", "any hour", "no ceiling", "no display name", "no reply-to" — plus
# `recipient_ids`, which an `all` send leaves null.
_NULL_MEANS_SOMETHING = frozenset(
    {"recipient_ids", "start_at", "window", "send_daily_cap", "from_name", "reply_to"}
)

# How many refusals an all-or-nothing 400 spells out before it stops counting.
# A selection where everything is refused has one cause, and the first few name
# it; five thousand sentences in an error body name nothing.
_MAX_REPORTED_REASONS = 10


# ── reads ───────────────────────────────────────────────────────────────────


async def load_send_run(conn, tenant_id: UUID, run_id: UUID) -> EmailSendRun | None:
    """One run's policy row. Takes a connection or a pool — the pass has both."""
    row = await conn.fetchrow(
        f"SELECT {SEND_RUN_COLUMNS} FROM email_send_runs WHERE id = $1 AND tenant_id = $2",
        run_id,
        tenant_id,
    )
    return EmailSendRun.model_validate(dict(row)) if row else None


async def _responses(conn, tenant_id: UUID, runs: list[EmailSendRun]) -> list[EmailSendResponse]:
    """Serialize runs, with their counts, in at most three queries.

    Two halves, read two ways. What a send has HANDLED is on the rows it claimed,
    by `send_run_id`. What a live send still COVERS — drafts ready to go, and
    rows still being written — is its scope read now: the batch's rows for a
    standing send, its members for a `selected` one. A send that has ended
    covers nothing.

    ``next_send_at`` and its reason are columns on the run. **No response may
    read `scheduled_jobs`**: it is our machinery, and "when does this next act"
    is a property of the send.
    """
    if not runs:
        return []

    handled: dict[UUID, Counter[str]] = {r.id: Counter() for r in runs}
    for row in await conn.fetch(
        """
        SELECT send_run_id, status, count(*) AS n
        FROM email_batch_recipients
        WHERE tenant_id = $1 AND send_run_id = ANY($2::uuid[])
        GROUP BY send_run_id, status
        """,
        tenant_id,
        [r.id for r in runs],
    ):
        handled[row["send_run_id"]][row["status"]] = row["n"]

    # `pending` rows count as still drafting only while drafting can reach them:
    # on a cancelled batch they never will.
    coverage = """
        count(*) FILTER (WHERE r.status = 'draft') AS ready,
        count(*) FILTER (
            WHERE r.status = 'drafting'
               OR (r.status = 'pending' AND b.status IN ('scheduled', 'drafting', 'paused'))
        ) AS drafting
    """
    covering: dict[UUID, tuple[int, int]] = {}
    live = [r for r in runs if r.status in LIVE_SEND_STATUSES]
    if selected := [r.id for r in live if r.scope == "selected"]:
        for row in await conn.fetch(
            f"""
            SELECT m.send_run_id AS key, {coverage}
            FROM email_send_members m
            JOIN email_batch_recipients r ON r.id = m.recipient_id AND r.tenant_id = m.tenant_id
            JOIN email_batches b ON b.id = r.batch_id AND b.tenant_id = r.tenant_id
            WHERE m.tenant_id = $1 AND m.send_run_id = ANY($2::uuid[])
              AND r.status IN ('draft', 'pending', 'drafting')
            GROUP BY m.send_run_id
            """,
            tenant_id,
            selected,
        ):
            covering[row["key"]] = (row["ready"], row["drafting"])
    if standing := {r.batch_id: r.id for r in live if r.scope == "all"}:
        for row in await conn.fetch(
            f"""
            SELECT r.batch_id AS key, {coverage}
            FROM email_batch_recipients r
            JOIN email_batches b ON b.id = r.batch_id AND b.tenant_id = r.tenant_id
            WHERE r.tenant_id = $1 AND r.batch_id = ANY($2::uuid[])
              AND r.status IN ('draft', 'pending', 'drafting')
            GROUP BY r.batch_id
            """,
            tenant_id,
            list(standing),
        ):
            covering[standing[row["key"]]] = (row["ready"], row["drafting"])

    # A run's limit is measured against its BATCH's sending days, in the batch's
    # own zone — the same reading the claim takes.
    now = datetime.now(UTC)
    sending_days = {
        row["id"]: (row["send_days"], row["last_send_day"], local_today(now, row["timezone"]))
        for row in await conn.fetch(
            """
            SELECT id, send_days, last_send_day, timezone FROM email_batches
            WHERE tenant_id = $1 AND id = ANY($2::uuid[])
            """,
            tenant_id,
            list({r.batch_id for r in runs}),
        )
    }

    out: list[EmailSendResponse] = []
    for run in runs:
        counts = handled[run.id]
        days, last_day, today = sending_days[run.batch_id]
        ready, drafting = covering.get(run.id, (0, 0))
        out.append(
            EmailSendResponse(
                id=run.id,
                batch_id=run.batch_id,
                scope=run.scope,
                status=run.status,
                failure_reason=run.failure_reason,
                from_email=run.from_email,
                from_name=run.from_name,
                reply_to=run.reply_to,
                start_at=run.start_at,
                timezone=run.timezone,
                window=run.window,
                send_attempts=run.send_attempts,
                send_retry_after_minutes=run.send_retry_after_minutes,
                send_gap_seconds=run.send_gap_seconds,
                send_daily_cap=run.daily_cap,
                send_daily_cap_today=daily_cap_today(
                    run.daily_cap,
                    base_days=run.send_ramp_base_days,
                    days=days,
                    last_day=last_day,
                    today=today,
                ),
                counts=EmailSendCounts(
                    total=run.total_recipients,
                    queued=ready,
                    drafting=drafting,
                    sending=counts["sending"],
                    sent=counts["sent"],
                    send_failed=counts["send_failed"],
                    skipped=counts["skipped"],
                ),
                next_send_at=run.next_send_at,
                next_send_reason=run.next_send_reason,
                created_at=run.created_at,
                started_at=run.started_at,
                finished_at=run.finished_at,
            )
        )
    return out


async def send_response(tenant: Tenant, run: EmailSendRun) -> EmailSendResponse:
    """One run, serialized from a ``Tenant`` — what the webhooks carry."""
    pool = await db.tenant_pool(tenant)
    return (await _responses(pool, tenant.id, [run]))[0]


async def _require_run(ctx: Context, batch_id: UUID, send_id: UUID) -> EmailSendRun:
    pool = await ctx.tenant_pool()
    run = await load_send_run(pool, ctx.tenant.id, send_id)
    if run is None or run.batch_id != batch_id:
        raise HTTPException(status_code=404, detail="that send is not on this batch")
    return run


async def list_sends(
    ctx: Context, batch_id: UUID, *, limit: int, offset: int
) -> Page[EmailSendResponse]:
    await _require_batch(ctx, batch_id)
    pool = await ctx.tenant_pool()
    rows = await pool.fetch(
        f"""
        SELECT {SEND_RUN_COLUMNS} FROM email_send_runs
        WHERE tenant_id = $1 AND batch_id = $2
        ORDER BY created_at DESC
        LIMIT $3 OFFSET $4
        """,
        ctx.tenant.id,
        batch_id,
        limit + 1,
        offset,
    )
    runs = [EmailSendRun.model_validate(dict(r)) for r in rows]
    return page_slice(await _responses(pool, ctx.tenant.id, runs), limit=limit, offset=offset)


async def get_send(ctx: Context, batch_id: UUID, send_id: UUID) -> EmailSendResponse:
    return await send_response(ctx.tenant, await _require_run(ctx, batch_id, send_id))


# ── create ──────────────────────────────────────────────────────────────────


async def create_send(
    batch_id: UUID, body: CreateEmailSendRequest, ctx: Context
) -> CreateEmailSendResponse:
    """Create a send over a scope, and the job that will work through it.

    **The run, its members and its job are ONE transaction.** Split them and you
    get either a run with nothing to send it (silently stuck for ever) or a job
    naming a run that does not exist. ``jobs.enqueue_once`` takes a connection
    specifically so this is possible. No recipient row is written: a covered
    draft only READS `queued`.

    **A `selected` send refuses before it is created, not after.** A row whose
    `to`, `subject` or `body` does not resolve, or that another live send already
    holds, comes back in ``rejected`` and stays a draft — the operator is standing
    right there and can fix it. If NOTHING in the selection can be sent there is
    no run to return, so that is a 400 naming the reasons.

    **An `all` send needs nothing to exist yet.** Created two minutes after the
    upload it has no drafts at all, and waits for them. It is refused only when
    there is nothing it could ever send: no drafts, and drafting is over.

    The batch's own status is deliberately not a gate otherwise. Cancelling a
    batch stops its DRAFTING; the drafts it already produced were reviewed and
    paid for and are still the operator's to send.
    """
    batch = await _require_batch(ctx, batch_id)
    if body.recipient_ids is not None and len(set(body.recipient_ids)) != len(body.recipient_ids):
        raise validation_error(["the same row is named more than once"], "invalid selection")

    sent = body.model_fields_set
    errors: list[str] = []
    if body.from_email is not None:
        # Validated on the same terms as the batch's, and refused here rather
        # than discovered by four thousand `send_failed` rows tomorrow morning.
        # The INTEGRATION is deliberately not overridable: a different Resend
        # account is a different credential, a different rate limit and a
        # different suppression list, which is a different batch.
        await check_sender(ctx.tenant, batch.integration_id, body.from_email, errors)
    if body.reply_to and not is_email_address(body.reply_to):
        errors.append(f"reply_to {body.reply_to!r} is not an email address")
    validate_start_at(body.start_at, errors)
    # An explicit `null` on a field with no "unset" meaning is a mistake, and
    # inheriting the batch's value silently would hide it.
    for field in sorted(sent - _NULL_MEANS_SOMETHING):
        if getattr(body, field) is None:
            errors.append(f"{field} cannot be null")
    if errors:
        raise validation_error(errors, "this send was refused")

    window = body.window if "window" in sent else batch.window
    # Absent inherits the batch's limit WITH its ramp progress, so "Send this
    # one" during a ramp gets the limit the standing send has. A cap named here
    # is read as an edit of the batch's: the same ramp keeps that progress, and
    # one with another `start` begins there, from the batch's sending days so far.
    if "send_daily_cap" in sent:
        daily_cap = body.send_daily_cap
        ramp_base = ramp_base_days(
            daily_cap,
            batch.daily_cap,
            old_base=batch.send_ramp_base_days,
            completed=completed_days(
                batch.send_days,
                batch.last_send_day,
                local_today(datetime.now(UTC), batch.timezone),
            ),
        )
    else:
        daily_cap = batch.daily_cap
        ramp_base = batch.send_ramp_base_days

    def inherit(field: str) -> Any:
        """Absent means "take the batch's"; present means exactly what was sent.

        `body.field or batch.field` is what this replaces. It is banned on
        principle rather than because it breaks here — it reads a legitimate
        falsy value as "not sent", and being safe only by the accident that
        every one of these has `ge=1` is not a property worth relying on.
        """
        return getattr(body, field) if field in sent else getattr(batch, field)

    pool = await ctx.tenant_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Serializes the sends of one batch, so "what is live" below and the
            # insert after it are one decision. Two presses of "Send this one" on
            # the same row would otherwise both see it free.
            batch_status = await conn.fetchval(
                "SELECT status FROM email_batches WHERE id = $1 AND tenant_id = $2 FOR UPDATE",
                batch_id,
                ctx.tenant.id,
            )
            live = [
                EmailSendRun.model_validate(dict(r))
                for r in await conn.fetch(
                    f"""
                    SELECT {SEND_RUN_COLUMNS} FROM email_send_runs
                    WHERE tenant_id = $1 AND batch_id = $2
                      AND status IN ('scheduled', 'sending', 'paused')
                    ORDER BY created_at
                    """,
                    ctx.tenant.id,
                    batch_id,
                )
            ]
            _refuse_beside(body.scope, live)

            rejected: list[RejectedRecipient] = []
            members: list[UUID] = []
            if body.scope == "all":
                has_draft = await conn.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM email_batch_recipients "
                    "WHERE tenant_id = $1 AND batch_id = $2 AND status = 'draft')",
                    ctx.tenant.id,
                    batch_id,
                )
                if not has_draft and batch_status in ("drafted", "canceled", "failed"):
                    raise validation_error(
                        ["there are no drafts to send, and this batch is not drafting any more"],
                        "nothing in this batch can be sent",
                    )
            else:
                assert body.recipient_ids is not None
                members, rejected = await _sendable(conn, ctx.tenant.id, batch, body.recipient_ids)

            run_id = await conn.fetchval(
                """
                INSERT INTO email_send_runs (
                    tenant_id, batch_id, scope, from_email, from_name, reply_to, status,
                    start_at, timezone, window_start_local, window_end_local, window_days,
                    send_attempts, send_retry_after_minutes, send_gap_seconds,
                    total_recipients, created_by_user_id,
                    send_daily_cap, send_ramp_start, send_ramp_end, send_ramp_step,
                    send_ramp_interval_days, send_ramp_base_days,
                    -- A send scheduled for Monday must say so from the moment it
                    -- exists, not from whenever its loop first runs. Null when it
                    -- starts now: there is nothing to wait for.
                    next_send_at, next_send_reason
                )
                VALUES ($1, $2, $3, $4, $5, $6, 'scheduled',
                        $7, $8, $9, $10, $11,
                        $12, $13, $14,
                        $15, $16,
                        $17, $18, $19, $20, $21, $22,
                        $7, CASE WHEN $7::timestamptz IS NULL THEN NULL ELSE 'start' END)
                RETURNING id
                """,
                ctx.tenant.id,
                batch_id,
                body.scope,
                str(inherit("from_email")).strip(),
                inherit("from_name"),
                inherit("reply_to"),
                body.start_at,
                inherit("timezone"),
                window.start if window else None,
                window.end if window else None,
                window.days if window else [1, 2, 3, 4, 5, 6, 7],
                inherit("send_attempts"),
                inherit("send_retry_after_minutes"),
                inherit("send_gap_seconds"),
                # A standing send cannot know how many rows it will send.
                len(members) if body.scope == "selected" else None,
                ctx.user.id,
                *cap_columns(daily_cap),
                ramp_base,
            )
            if members:
                await conn.execute(
                    """
                    INSERT INTO email_send_members (send_run_id, recipient_id, tenant_id)
                    SELECT $1, unnest($2::uuid[]), $3
                    """,
                    run_id,
                    members,
                    ctx.tenant.id,
                )
            await jobs.enqueue_once(
                conn,
                tenant_id=ctx.tenant.id,
                kind=jobs.JobKind.EMAIL_SEND,
                scheduled_at=body.start_at or datetime.now(UTC),
                subject_id=run_id,
                args={"send_run_id": str(run_id)},
            )
            run = await load_send_run(conn, ctx.tenant.id, run_id)
    assert run is not None
    logger.info(
        "email batch %s: send %s created over %s",
        batch_id,
        run_id,
        f"{len(members)} row(s)" if body.scope == "selected" else "every row",
    )
    return CreateEmailSendResponse(send=await send_response(ctx.tenant, run), rejected=rejected)


def _refuse_beside(scope: str, live: list[EmailSendRun]) -> None:
    """409 if a send of this scope may not be live beside the ones that are.

    A standing send covers every draft, so it can share the batch with nothing,
    and nothing can share the batch with it. Several `selected` sends are fine.
    """
    standing = next((r for r in live if r.scope == "all"), None)
    if standing is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"send {standing.id} is {standing.status} and already sends every row of this "
                "batch as it is drafted - cancel it first to send rows by hand"
            ),
        )
    if scope == "all" and live:
        named = ", ".join(f"send {r.id} is {r.status}" for r in live)
        raise HTTPException(
            status_code=409,
            detail=(
                f"{named} - a send of every row cannot run beside another send of this "
                "batch. Cancel it or let it finish first"
            ),
        )


async def _sendable(
    conn, tenant_id: UUID, batch: EmailBatch, ids: list[UUID]
) -> tuple[list[UUID], list[RejectedRecipient]]:
    """Split named rows into what a `selected` send may hold and what it refuses.

    Walked in the order they were named, so the operator can line the
    rejections up with what they sent. Refused: a row not in this batch, one
    that is not a draft, one another live send already holds, and one whose
    fields do not resolve. All-or-nothing only in the degenerate case — if
    nothing is left there is no send to describe, so that is a 400.
    """
    found = {
        r["id"]: _recipient_row(r)
        for r in await conn.fetch(
            f"""
            SELECT {RECIPIENT_COLUMNS} FROM email_batch_recipients
            WHERE tenant_id = $1 AND batch_id = $2 AND id = ANY($3::uuid[])
            """,
            tenant_id,
            batch.id,
            ids,
        )
    }
    held = {
        r["recipient_id"]: r["send_run_id"]
        for r in await conn.fetch(
            """
            SELECT m.recipient_id, m.send_run_id FROM email_send_members m
            JOIN email_send_runs s ON s.id = m.send_run_id AND s.tenant_id = m.tenant_id
            WHERE m.tenant_id = $1 AND m.recipient_id = ANY($2::uuid[])
              AND s.status IN ('scheduled', 'sending', 'paused')
            """,
            tenant_id,
            ids,
        )
    }
    members: list[UUID] = []
    rejected: list[RejectedRecipient] = []
    for recipient_id in ids:
        recipient = found.get(recipient_id)
        reason: str | None
        if recipient is None:
            reason = "this row is not in this batch"
        elif recipient.status != "draft":
            reason = f"this row is {recipient.status}, not a reviewable draft"
        elif recipient_id in held:
            reason = f"this row is already in send {held[recipient_id]}, which has not finished"
        else:
            _, reason = resolve_send_fields(batch, recipient)
        if reason is None:
            members.append(recipient_id)
        else:
            rejected.append(
                RejectedRecipient(
                    recipient_id=recipient_id,
                    row_number=recipient.row_number if recipient else 0,
                    reason=reason,
                )
            )
    if not members:
        reasons = [f"row {r.row_number}: {r.reason}" for r in rejected]
        if len(reasons) > _MAX_REPORTED_REASONS:
            extra = len(reasons) - _MAX_REPORTED_REASONS
            reasons = reasons[:_MAX_REPORTED_REASONS] + [f"…and {extra} more"]
        raise validation_error(reasons, "nothing in this selection can be sent")
    return members, rejected


# ── steering ────────────────────────────────────────────────────────────────


async def patch_send(
    batch_id: UUID, send_id: UUID, body: PatchEmailSendRequest, ctx: Context
) -> EmailSendResponse:
    """Re-steer one send. The loop reads the new row within `recheck_seconds`.

    Never the sender, and never the scope — see rule 2 in this module's
    docstring. `start_at` moves only while the send has not begun, and moving it
    moves the job with it, because a run that says one thing and does another is
    worse than one that refuses.
    """
    run = await _require_run(ctx, batch_id, send_id)
    if run.status in ("sent", "canceled", "failed"):
        raise HTTPException(
            status_code=409, detail=f"this send is {run.status} and can no longer be edited"
        )
    sent = body.model_fields_set
    errors: list[str] = []
    if "start_at" in sent:
        if run.status != "scheduled":
            errors.append("start_at can only be changed before the send has begun")
        else:
            validate_start_at(body.start_at, errors)
    for field in sorted(sent - _NULL_MEANS_SOMETHING):
        if getattr(body, field) is None:
            errors.append(f"{field} cannot be null")
    if errors:
        raise validation_error(errors, "invalid send")

    window = body.window if "window" in sent else run.window
    start_at = body.start_at if "start_at" in sent else run.start_at
    daily_cap = body.send_daily_cap if "send_daily_cap" in sent else run.daily_cap

    def keep(field: str) -> Any:
        """Absent means unchanged; present means exactly what was sent."""
        return getattr(body, field) if field in sent else getattr(run, field)

    pool = await ctx.tenant_pool()
    batch = await _require_batch(ctx, batch_id)
    # An edited ramp keeps its progress unless its start moved; a new one starts
    # from the sending days the batch has completed so far.
    ramp_base = ramp_base_days(
        daily_cap,
        run.daily_cap,
        old_base=run.send_ramp_base_days,
        completed=completed_days(
            batch.send_days, batch.last_send_day, local_today(datetime.now(UTC), batch.timezone)
        ),
    )
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                f"""
                UPDATE email_send_runs
                SET start_at = $3,
                    -- While the send is `scheduled` its start time IS when it
                    -- next acts, so the two move together. Once it is `sending`
                    -- the loop owns these columns and this must not touch them.
                    next_send_at = CASE WHEN status = 'scheduled' THEN $3 ELSE next_send_at END,
                    next_send_reason = CASE
                        WHEN status <> 'scheduled' THEN next_send_reason
                        WHEN $3::timestamptz IS NULL THEN NULL
                        ELSE 'start'
                    END,
                    timezone = $4,
                    window_start_local = $5,
                    window_end_local = $6,
                    window_days = $7,
                    send_attempts = $8,
                    send_retry_after_minutes = $9,
                    send_gap_seconds = $10,
                    send_daily_cap = $11,
                    send_ramp_start = $12,
                    send_ramp_end = $13,
                    send_ramp_step = $14,
                    send_ramp_interval_days = $15,
                    send_ramp_base_days = $16,
                    updated_at = now()
                WHERE id = $1 AND tenant_id = $2
                RETURNING {SEND_RUN_COLUMNS}
                """,
                send_id,
                ctx.tenant.id,
                start_at,
                keep("timezone"),
                window.start if window else None,
                window.end if window else None,
                window.days if window else [1, 2, 3, 4, 5, 6, 7],
                keep("send_attempts"),
                keep("send_retry_after_minutes"),
                keep("send_gap_seconds"),
                *cap_columns(daily_cap),
                ramp_base,
            )
            assert row is not None
            fresh = EmailSendRun.model_validate(dict(row))
            if "start_at" in sent and fresh.status == "scheduled":
                # The job IS the schedule. Moving `start_at` without moving the
                # job would leave a send that says one thing and does another.
                await conn.execute(
                    """
                    UPDATE scheduled_jobs SET scheduled_at = $3, updated_at = now()
                    WHERE tenant_id = $1 AND subject_id = $2
                      AND kind = $4 AND status = 'pending'
                    """,
                    ctx.tenant.id,
                    send_id,
                    start_at or datetime.now(UTC),
                    str(jobs.JobKind.EMAIL_SEND),
                )
    return await send_response(ctx.tenant, fresh)


async def pause_send(batch_id: UUID, send_id: UUID, ctx: Context) -> EmailSendResponse:
    """Stop sending. Its rows stay covered — still `queued` — and nothing is lost.

    They also stay editable, skippable and redraftable, because nothing about a
    covered draft is stored on it: pausing a send to fix what it is about to
    send is the point of pausing it.

    The claim reads the run's status in the same query that takes a row, so a
    pause takes hold on the very next email rather than at the end of the pass —
    which matters here in a way it does not for drafting, because the thing that
    would otherwise keep happening is a stranger's inbox.
    """
    run = await _transition(
        ctx,
        batch_id,
        send_id,
        sql=f"""
        UPDATE email_send_runs
        SET status = 'paused', failure_reason = NULL,
            next_send_at = NULL, next_send_reason = NULL, updated_at = now()
        WHERE id = $1 AND tenant_id = $2 AND status IN ('scheduled', 'sending')
        RETURNING {SEND_RUN_COLUMNS}
        """,
        allowed=("scheduled", "sending"),
        verb="paused",
    )
    return await send_response(ctx.tenant, run)


async def resume_send(batch_id: UUID, send_id: UUID, ctx: Context) -> EmailSendResponse:
    """Undo a pause and give the send a job again.

    Back to `scheduled` rather than `sending` when the start time is still in
    the future — pausing and resuming a send that has not begun must not be a way
    to make it begin early.
    """
    await _require_run(ctx, batch_id, send_id)
    pool = await ctx.tenant_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                f"""
                UPDATE email_send_runs
                SET status = CASE
                        WHEN start_at IS NOT NULL AND start_at > now() THEN 'scheduled'
                        ELSE 'sending'
                    END,
                    -- A resumed send acts as soon as its job is claimed, so
                    -- there is nothing to announce unless its start time is
                    -- still ahead. Clearing the counter and the reason is what
                    -- makes resume the reset a breaker pause needs.
                    next_send_at = CASE
                        WHEN start_at IS NOT NULL AND start_at > now() THEN start_at ELSE NULL
                    END,
                    next_send_reason = CASE
                        WHEN start_at IS NOT NULL AND start_at > now() THEN 'start' ELSE NULL
                    END,
                    failure_reason = NULL,
                    consecutive_send_failures = 0,
                    updated_at = now()
                WHERE id = $1 AND tenant_id = $2 AND status = 'paused'
                RETURNING {SEND_RUN_COLUMNS}
                """,
                send_id,
                ctx.tenant.id,
            )
            if row is None:
                run = await _require_run(ctx, batch_id, send_id)
                raise HTTPException(
                    status_code=409,
                    detail=f"a {run.status} send cannot be resumed (only paused can be)",
                )
            fresh = EmailSendRun.model_validate(dict(row))
            job_id = await jobs.enqueue_once(
                conn,
                tenant_id=ctx.tenant.id,
                kind=jobs.JobKind.EMAIL_SEND,
                scheduled_at=(
                    fresh.start_at
                    if fresh.status == "scheduled" and fresh.start_at
                    else datetime.now(UTC)
                ),
                subject_id=send_id,
                args={"send_run_id": str(send_id)},
            )
            if job_id is None:
                # A send paused before its start time keeps its pending job, and
                # that job is still pointed at the right instant.
                logger.info("email send %s already has a job; not enqueuing another", send_id)
    return await send_response(ctx.tenant, fresh)


async def cancel_send(batch_id: UUID, send_id: UUID, ctx: Context) -> EmailSendResponse:
    """Stop this send for good. Every row it has not sent is simply a draft again.

    **This is not a destructive action and the UI must not present it as one.**
    It is how an operator changes what a send covers: nothing was taken from the
    review table, so nothing has to be handed back — the rows stop being covered
    and are ready for another send, exactly as they were.

    A row that is mid-send when this lands is left alone and finishes — it may
    already be in a stranger's inbox, and marking it anything else here would
    either lose the receipt or invite a second copy of the same email.
    """
    await _require_run(ctx, batch_id, send_id)
    pool = await ctx.tenant_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                f"""
                UPDATE email_send_runs
                SET status = 'canceled', finished_at = now(),
                    next_send_at = NULL, next_send_reason = NULL, updated_at = now()
                WHERE id = $1 AND tenant_id = $2
                  AND status IN ('scheduled', 'sending', 'paused')
                RETURNING {SEND_RUN_COLUMNS}
                """,
                send_id,
                ctx.tenant.id,
            )
            if row is None:
                run = await _require_run(ctx, batch_id, send_id)
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"a {run.status} send cannot be canceled "
                        "(only scheduled, sending or paused can be)"
                    ),
                )
            fresh = EmailSendRun.model_validate(dict(row))
            await release_rows(conn, fresh)
    logger.info("email send %s: canceled", send_id)
    return await send_response(ctx.tenant, fresh)


async def release_rows(conn: asyncpg.Connection | asyncpg.Pool, run: EmailSendRun) -> None:
    """Clear what an ENDED send left on the rows it did not send.

    The one recipient write a send's end makes, shared by cancel, finish and an
    account-level failure — and bounded by what went wrong, not by the send's
    size. A row that failed transiently or waited out a rate limit carries a
    spent `send_attempts`, a backoff, the error and the address its claim armed.
    Left there, the next send would start it with a spent budget, wait out a
    backoff that means nothing any more, and refuse another row with that
    address as a duplicate of an email that never went.

    Two statements selected on scope rather than one with an `OR`. A standing
    send's rows are the whole batch — no other send can be live beside it — and
    `sending`, `sent` and `send_failed` are untouched: those are what happened.
    """
    if run.scope == "all":
        await conn.execute(
            """
            UPDATE email_batch_recipients r
            SET send_attempts = 0, next_attempt_at = NULL, to_email = NULL,
                last_error = NULL, updated_at = now()
            WHERE r.tenant_id = $1 AND r.batch_id = $2
              AND r.status NOT IN ('sending', 'sent', 'send_failed')
              AND (r.send_attempts > 0 OR r.to_email IS NOT NULL)
            """,
            run.tenant_id,
            run.batch_id,
        )
        return
    await conn.execute(
        """
        UPDATE email_batch_recipients r
        SET send_attempts = 0, next_attempt_at = NULL, to_email = NULL,
            last_error = NULL, updated_at = now()
        FROM email_send_members m
        WHERE m.tenant_id = $1 AND m.send_run_id = $2
          AND r.id = m.recipient_id AND r.tenant_id = m.tenant_id
          AND r.status NOT IN ('sending', 'sent', 'send_failed')
          AND (r.send_attempts > 0 OR r.to_email IS NOT NULL)
        """,
        run.tenant_id,
        run.id,
    )


async def _transition(
    ctx: Context, batch_id: UUID, send_id: UUID, *, sql: str, allowed: tuple[str, ...], verb: str
) -> EmailSendRun:
    """One guarded compare-and-set on the run row, or a 409 naming the status."""
    await _require_run(ctx, batch_id, send_id)
    pool = await ctx.tenant_pool()
    row = await pool.fetchrow(sql, send_id, ctx.tenant.id)
    if row is None:
        run = await _require_run(ctx, batch_id, send_id)
        raise HTTPException(
            status_code=409,
            detail=f"a {run.status} send cannot be {verb} (only {', '.join(allowed)} can be)",
        )
    return EmailSendRun.model_validate(dict(row))


async def emit(tenant: Tenant, event: str, run: EmailSendRun) -> None:
    """Dispatch one send lifecycle event. Shared with the pass, which fires two.

    The third argument is the envelope's AGENT id, and an email batch has none —
    the same `None` the three `email_batch.*` events pass. The send and its batch
    are named in the payload.
    """
    payload = await send_response(tenant, run)
    await webhooks.dispatch(tenant, event, None, payload.model_dump(mode="json"))


__all__ = [
    "cancel_send",
    "create_send",
    "emit",
    "get_send",
    "list_sends",
    "load_send_run",
    "patch_send",
    "pause_send",
    "release_rows",
    "resume_send",
    "send_response",
]
