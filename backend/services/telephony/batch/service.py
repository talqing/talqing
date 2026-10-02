"""Creating, reading and steering a batch — everything a human does to one.

The split this file is built on: **the batch row is policy, the recipient rows
are work.** Pause, resume, edit, reschedule, change the agent and cancel are all
writes to the one policy row, and none of them touches a work row. The
dispatcher re-reads policy on every pass, so there is nothing to coordinate and
no in-flight state to reconcile.

The one consequence worth knowing before reading further: because cancel writes
one row, a recipient's status as a *reader* sees it is not always the status
stored. :func:`effective_status` is the only place that mapping exists — the
counts, the recipients endpoint and any future export all go through it.

**A batch that can dial has exactly one `call.batch.dial` job, and it is created
here.** `create`, `resume` and an append that wakes a completed batch are the
verbs that need one, and each enqueues inside the same transaction as the status
write: a batch that exists with no job
behind it is a campaign that silently never dials, and nothing sweeps for one any
more. `jobs.enqueue_once` takes a connection specifically so this is possible.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import UTC, datetime, timedelta
from uuid import UUID

import asyncpg
from fastapi import HTTPException

import db
from api.core.schemas import Page, page_slice, validation_error
from services import jobs
from services.agents.plan import resolve_call_plan
from services.close_reasons import VOICEMAIL, describe
from services.scheduling import (
    cap_columns,
    completed_days,
    daily_cap_today,
    local_day_bounds,
    local_today,
    ramp_base_days,
    window_state,
)
from services.telephony import dial
from services.telephony.e164 import normalize_e164
from services.user import Context, Tenant
from services.userdata import is_reserved_key

from .models import (
    BATCH_COLUMNS,
    AddRecipientsRequest,
    AddRecipientsResponse,
    BatchRecipientInput,
    CallBatch,
    CallBatchCounts,
    CallBatchRecipientResponse,
    CallBatchResponse,
    CreateCallBatchRequest,
    NextDialReason,
    PatchCallBatchRequest,
    SkippedRecipient,
)

logger = logging.getLogger("talqing.telephony.batch")

# A schedule further out than this is not a schedule, it is a reminder. Long
# enough for "the first Monday of next quarter", short enough that a typo in the
# year is refused rather than dialled.
MAX_SCHEDULE_DAYS_AHEAD = 90
# Clock skew between a browser and the server, tolerated on `start_at` so
# "start now" typed as an instant does not fail by two seconds.
SCHEDULE_PAST_TOLERANCE = timedelta(minutes=1)

# Policy edits and appends. `completed` is idle rather than over: everyone added
# so far was handled, and an append wakes it.
_OPEN_STATUSES = ("scheduled", "running", "paused", "completed")


def effective_status(recipient_status: str, batch_status: str) -> str:
    """What a reader sees, which is not always what is stored.

    Cancelling a 10 000-row batch writes one row — the batch's — so its untouched
    recipients are reported as cancelled rather than rewritten. ``dialing``
    deliberately survives a cancel: that call is real and still on the phone. A
    batch the circuit breaker stopped reports the same way, because "not
    attempted, and never will be" is the same fact from the recipient's side.
    """
    if recipient_status in ("completed", "failed", "dialing"):
        return recipient_status
    if batch_status in ("canceled", "failed"):
        return "canceled"
    return "pending"


def dial_cap_today(batch: CallBatch, now: datetime) -> int | None:
    """Today's limit on calls placed: the fixed one, or where the ramp has got to."""
    return daily_cap_today(
        batch.daily_cap,
        base_days=batch.dial_ramp_base_days,
        days=batch.dial_days,
        last_day=batch.last_dial_day,
        today=local_today(now, batch.timezone),
    )


async def dialed_today(
    conn, tenant_id: UUID, batches: list[CallBatch], now: datetime
) -> dict[UUID, int]:
    """Calls each batch placed in its own local day. A retry is a call.

    A dial is a committed claim, which is a `sessions` row. Grouped by timezone:
    "today" is a different pair of instants per zone, and a page of batches is
    almost always one or two zones.
    """
    placed = {b.id: 0 for b in batches}
    by_zone: dict[str, list[UUID]] = {}
    for batch in batches:
        by_zone.setdefault(batch.timezone, []).append(batch.id)
    for zone, ids in by_zone.items():
        day_start, day_end = local_day_bounds(now, zone)
        for row in await conn.fetch(
            """
            SELECT batch_id, count(*) AS n FROM sessions
            WHERE tenant_id = $1 AND batch_id = ANY($2::uuid[])
              AND COALESCE(started_at, created_at) >= $3
              AND COALESCE(started_at, created_at) < $4
            GROUP BY batch_id
            """,
            tenant_id,
            ids,
            day_start,
            day_end,
        ):
            placed[row["batch_id"]] = row["n"]
    return placed


def next_dial_at(
    batch: CallBatch, now: datetime, placed_today: int
) -> tuple[datetime | None, NextDialReason | None]:
    """When this batch will next place a call and why it waits, or ``(None, None)``.

    Nothing means one of three things a reader can already see from the status:
    it is dialling now, a human paused it, or it is over. Everything else is the
    clock — a start time not yet reached, business hours that are closed, or
    today's limit spent — and that is what the UI renders as "waiting until
    Monday 10:00".
    """
    if batch.status in ("completed", "canceled", "failed", "paused"):
        return None, None
    at = now
    if batch.start_at is not None and batch.start_at > now:
        at = batch.start_at
    open_now, opens_at = window_state(
        now=at,
        timezone=batch.timezone,
        window_start_local=batch.window_start_local,
        window_end_local=batch.window_end_local,
        window_days=batch.window_days,
        subject=f"call batch {batch.id}",
    )
    if not open_now:
        return opens_at, "window"
    if at > now:
        return at, "start"
    cap = dial_cap_today(batch, now)
    if cap is not None and placed_today >= cap:
        return local_day_bounds(now, batch.timezone)[1], "daily_cap"
    return None, None


# ── reads ───────────────────────────────────────────────────────────────────


async def load_batch(conn, tenant_id: UUID, batch_id: UUID) -> CallBatch | None:
    """The policy row. Takes a connection or a pool — the dispatcher has both."""
    row = await conn.fetchrow(
        f"SELECT {BATCH_COLUMNS} FROM call_batches WHERE id = $1 AND tenant_id = $2",
        batch_id,
        tenant_id,
    )
    return CallBatch.model_validate(dict(row)) if row else None


def _batch_agent_name(batch: CallBatch, names: dict[UUID, str]) -> str | None:
    """What to call the agent that answers for this batch.

    The joined name for a stored agent, so a rename shows through. For an inline
    or overridden entry the plan's member name is the only name there is — and
    it is the same string the calls list shows for those runs.
    """
    if batch.agent_plan:
        members = batch.agent_plan.get("members") or []
        if members:
            return members[0].get("name")
    return names.get(batch.agent_id) if batch.agent_id else None


async def _responses(conn, tenant_id: UUID, batches: list[CallBatch]) -> list[CallBatchResponse]:
    """Serialize batches, resolving names and counts in two queries however many.

    ``agent_name`` and ``from_e164`` are joined rather than snapshot, so a
    renamed agent shows its current name and a deleted one shows null — which is
    also what the dispatcher sees when it fails the batch.
    """
    if not batches:
        return []
    agent_ids = [b.agent_id for b in batches if b.agent_id]
    number_ids = [b.from_phone_number_id for b in batches if b.from_phone_number_id]
    names: dict[UUID, str] = {}
    numbers: dict[UUID, str] = {}
    if agent_ids:
        names = {
            r["id"]: r["name"]
            for r in await conn.fetch(
                "SELECT id, name FROM agents WHERE tenant_id = $1 AND id = ANY($2::uuid[])",
                tenant_id,
                agent_ids,
            )
        }
    if number_ids:
        numbers = {
            r["id"]: r["e164"]
            for r in await conn.fetch(
                "SELECT id, e164 FROM phone_numbers WHERE tenant_id = $1 AND id = ANY($2::uuid[])",
                tenant_id,
                number_ids,
            )
        }
    # One GROUP BY over an indexed partial, for the whole page. Only `total` is
    # stored (it is immutable except by append, so it cannot drift and it is the
    # denominator of every progress bar); four more stored counters would be four
    # more things to keep in step with two writers.
    tallies: dict[UUID, Counter[str]] = {b.id: Counter() for b in batches}
    voicemail: Counter[UUID] = Counter()
    for row in await conn.fetch(
        """
        SELECT batch_id, status, count(*) AS n,
               count(*) FILTER (WHERE last_close_reason = $3) AS voicemail
        FROM call_batch_recipients
        WHERE tenant_id = $1 AND batch_id = ANY($2::uuid[])
        GROUP BY batch_id, status
        """,
        tenant_id,
        [b.id for b in batches],
        VOICEMAIL,
    ):
        tallies[row["batch_id"]][row["status"]] = row["n"]
        voicemail[row["batch_id"]] += row["voicemail"]

    now = datetime.now(UTC)
    placed = await dialed_today(conn, tenant_id, batches, now)
    out: list[CallBatchResponse] = []
    for batch in batches:
        counts: Counter[str] = Counter()
        for status, n in tallies[batch.id].items():
            counts[effective_status(status, batch.status)] += n
        next_at, next_reason = next_dial_at(batch, now, placed[batch.id])
        out.append(
            CallBatchResponse(
                id=batch.id,
                name=batch.name,
                agent_id=batch.agent_id,
                agent_name=_batch_agent_name(batch, names),
                agent_plan=batch.agent_plan,
                vars=batch.vars,
                from_phone_number_id=batch.from_phone_number_id,
                from_e164=(
                    numbers.get(batch.from_phone_number_id) if batch.from_phone_number_id else None
                ),
                status=batch.status,
                failure_reason=batch.failure_reason,
                start_at=batch.start_at,
                timezone=batch.timezone,
                calling_window=batch.calling_window,
                max_concurrency=batch.max_concurrency,
                max_attempts=batch.max_attempts,
                retry_after_minutes=batch.retry_after_minutes,
                dial_gap_seconds=batch.dial_gap_seconds,
                dial_daily_cap=batch.daily_cap,
                dial_daily_cap_today=dial_cap_today(batch, now),
                dialed_today=placed[batch.id],
                counts=CallBatchCounts(
                    total=batch.total_recipients,
                    pending=counts["pending"],
                    dialing=counts["dialing"],
                    completed=counts["completed"],
                    failed=counts["failed"],
                    canceled=counts["canceled"],
                    voicemail=voicemail[batch.id],
                ),
                next_dial_at=next_at,
                next_dial_reason=next_reason,
                created_at=batch.created_at,
                started_at=batch.started_at,
                ended_at=batch.ended_at,
            )
        )
    return out


async def batch_response(tenant: Tenant, batch: CallBatch) -> CallBatchResponse:
    """One batch, serialized from a ``Tenant`` — what the webhooks carry."""
    pool = await db.tenant_pool(tenant)
    return (await _responses(pool, tenant.id, [batch]))[0]


async def _require_batch(ctx: Context, batch_id: UUID) -> CallBatch:
    pool = await ctx.tenant_pool()
    batch = await load_batch(pool, ctx.tenant.id, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="call batch not found")
    return batch


async def list_batches(
    ctx: Context, *, limit: int, offset: int, status: str | None
) -> Page[CallBatchResponse]:
    pool = await ctx.tenant_pool()
    rows = await pool.fetch(
        f"""
        SELECT {BATCH_COLUMNS} FROM call_batches
        WHERE tenant_id = $1 AND ($4::text IS NULL OR status = $4::text)
        ORDER BY created_at DESC
        LIMIT $2 OFFSET $3
        """,
        ctx.tenant.id,
        limit + 1,
        offset,
        status,
    )
    batches = [CallBatch.model_validate(dict(r)) for r in rows]
    return page_slice(await _responses(pool, ctx.tenant.id, batches), limit=limit, offset=offset)


async def get_batch(ctx: Context, batch_id: UUID) -> CallBatchResponse:
    batch = await _require_batch(ctx, batch_id)
    return await batch_response(ctx.tenant, batch)


async def list_recipients(
    ctx: Context, batch_id: UUID, *, limit: int, offset: int, status: str | None
) -> Page[CallBatchRecipientResponse]:
    """One batch's recipients, in the operator's own CSV order.

    ``status`` filters on the **effective** status, which for `pending` and
    `canceled` means a predicate on the batch as well as on the row: a request
    for `canceled` matches nothing on a running batch and every untouched row on
    a cancelled one.
    """
    batch = await _require_batch(ctx, batch_id)
    stored: str | None = None
    if status is not None:
        if status in ("completed", "failed", "dialing"):
            stored = status
        elif status == "pending" and batch.status not in ("canceled", "failed"):
            stored = "pending"
        elif status == "canceled" and batch.status in ("canceled", "failed"):
            stored = "pending"
        else:
            return Page(items=[], has_more=False, limit=limit, offset=offset)

    pool = await ctx.tenant_pool()
    rows = await pool.fetch(
        """
        SELECT id, row_number, to_e164, userdata, status, attempts,
               last_close_reason, session_id, updated_at
        FROM call_batch_recipients
        WHERE tenant_id = $1 AND batch_id = $2
          AND ($5::text IS NULL OR status = $5::text)
        ORDER BY row_number
        LIMIT $3 OFFSET $4
        """,
        ctx.tenant.id,
        batch_id,
        limit + 1,
        offset,
        stored,
    )
    return page_slice(
        [
            CallBatchRecipientResponse(
                id=r["id"],
                row_number=r["row_number"],
                to=r["to_e164"],
                userdata=r["userdata"],
                status=effective_status(r["status"], batch.status),
                attempts=r["attempts"],
                last_close_reason=r["last_close_reason"],
                last_close_reason_label=(
                    describe(r["last_close_reason"]) if r["last_close_reason"] else None
                ),
                session_id=r["session_id"],
                updated_at=r["updated_at"],
            )
            for r in rows
        ],
        limit=limit,
        offset=offset,
    )


# ── writes ──────────────────────────────────────────────────────────────────


def _clean_recipients(
    recipients: list[BatchRecipientInput], *, did: str | None, skip_repeats: bool
) -> tuple[list[tuple[int, str, dict[str, str]]], list[str], list[SkippedRecipient]]:
    """Normalize a whole list, collecting every bad row rather than the first.

    Returns ``(rows, errors, skipped)``, each row numbered by its 1-based
    position in ``recipients``. An operator uploading 10 000 lines gets told
    about rows 12, 47 and 88 in one response; raising on row 12 would mean three
    round trips through the same file.

    A number repeated within the list is an error at create and, with
    ``skip_repeats``, a skip on append — where re-pushing a CRM export must be a
    harmless no-op.

    ``did`` is the number this batch dials FROM, and a national-format row is
    national relative to it — the same rule as every other door
    (``services/telephony/e164``). It matters most here: a batch bakes the
    normalized number into 10 000 rows at upload time, so a wrong country is
    wrong 10 000 times and stays that way.
    """
    rows: list[tuple[int, str, dict[str, str]]] = []
    errors: list[str] = []
    skipped: list[SkippedRecipient] = []
    seen: dict[str, int] = {}
    for row_number, recipient in enumerate(recipients, start=1):
        try:
            to_e164 = normalize_e164(recipient.to, did=did)
        except ValueError:
            errors.append(f"row {row_number}: {recipient.to!r} is not a valid phone number")
            continue
        first_seen = seen.get(to_e164)
        if first_seen is not None:
            if skip_repeats:
                skipped.append(
                    SkippedRecipient(
                        row_number=row_number, to=to_e164, reason=f"same number as row {first_seen}"
                    )
                )
            else:
                errors.append(f"row {row_number}: {to_e164} is already on row {first_seen}")
            continue
        seen[to_e164] = row_number
        bad_key = False
        for key in recipient.userdata:
            if not key.strip():
                errors.append(f"row {row_number}: userdata keys must be non-empty strings")
                bad_key = True
            elif is_reserved_key(key):
                errors.append(f"row {row_number}: userdata key {key!r} is reserved")
                bad_key = True
        if bad_key:
            continue
        # Empty values are dropped rather than stored as "": `{{userdata.name}}`
        # then resolves to nothing and the greeting still reads as written,
        # instead of saying "Hi , how are you".
        rows.append((row_number, to_e164, {k: v for k, v in recipient.userdata.items() if v}))
    return rows, errors, skipped


def _validate_start_at(start_at: datetime | None, errors: list[str]) -> None:
    if start_at is None:
        return
    now = datetime.now(UTC)
    if start_at < now - SCHEDULE_PAST_TOLERANCE:
        errors.append("start_at is in the past")
    elif start_at > now + timedelta(days=MAX_SCHEDULE_DAYS_AHEAD):
        errors.append(f"start_at cannot be more than {MAX_SCHEDULE_DAYS_AHEAD} days from now")


async def _insert_recipients(
    conn,
    *,
    tenant_id: UUID,
    batch_id: UUID,
    rows: list[tuple[int, str, dict[str, str]]],
) -> int:
    """Insert in one round trip, skipping numbers the batch already holds.

    ``ON CONFLICT DO NOTHING`` on ``uq_batch_recipients_number`` is the backstop
    for never calling anyone twice: a number already called cannot be re-added,
    whatever the caller checked first.
    """
    inserted = await conn.fetch(
        """
        INSERT INTO call_batch_recipients (tenant_id, batch_id, row_number, to_e164, userdata)
        SELECT $1, $2, r.row_number, r.to_e164, r.userdata::jsonb
        FROM unnest($3::int[], $4::text[], $5::text[]) AS r(row_number, to_e164, userdata)
        ON CONFLICT (batch_id, to_e164) DO NOTHING
        RETURNING id
        """,
        tenant_id,
        batch_id,
        [r[0] for r in rows],
        [r[1] for r in rows],
        [json.dumps(r[2]) for r in rows],
    )
    return len(inserted)


async def create_batch(body: CreateCallBatchRequest, ctx: Context) -> CallBatchResponse:
    """Validate everything, then write the batch and its recipients in one go.

    All-or-nothing on purpose: a partial accept would leave an operator
    reconciling which of their 10 000 rows made it.
    """
    errors: list[str] = []
    did: str | None = None
    try:
        number, _ = await dial.resolve_dial_number(ctx, body.from_phone_number_id)
        did = number.e164
    except dial.DialError as exc:
        errors.append(exc.message)
    _validate_start_at(body.start_at, errors)
    rows, row_errors, _ = _clean_recipients(body.recipients, did=did, skip_repeats=False)
    errors.extend(row_errors)
    if errors:
        raise validation_error(errors, "invalid call batch")
    # Last, and on its own: it raises with its own list of config problems, which
    # would read as noise mixed in with "row 47's number is unparseable".
    # Validated ONCE, here — not 10 000 times at dial. Everything the plan
    # carries is immutable afterwards, which is also what makes a republish
    # under a running campaign unable to change what it dials with.
    plan = await resolve_call_plan(ctx, body, channels=("voice",))
    stored_plan = plan.stored()

    window = body.calling_window
    pool = await ctx.tenant_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            batch_id = await conn.fetchval(
                """
                INSERT INTO call_batches (
                    tenant_id, name, agent_id, agent_plan, vars, from_phone_number_id, status,
                    start_at, timezone, window_start_local, window_end_local, window_days,
                    max_concurrency, max_attempts, retry_after_minutes,
                    total_recipients, created_by_user_id,
                    dial_gap_seconds, dial_daily_cap, dial_ramp_start, dial_ramp_end,
                    dial_ramp_step, dial_ramp_interval_days
                )
                VALUES ($1, $2, $3, $15::jsonb, $16::jsonb, $4, 'scheduled',
                        $5, $6, $7, $8, $9,
                        $10, $11, $12,
                        $13, $14,
                        $17, $18, $19, $20, $21, $22)
                RETURNING id
                """,
                ctx.tenant.id,
                body.name,
                UUID(plan.entry.agent_id) if plan.entry.agent_id else None,
                body.from_phone_number_id,
                body.start_at,
                body.timezone,
                window.start if window else None,
                window.end if window else None,
                window.days if window else [1, 2, 3, 4, 5, 6, 7],
                body.max_concurrency,
                body.max_attempts,
                body.retry_after_minutes,
                len(rows),
                ctx.user.id,
                json.dumps(stored_plan) if stored_plan is not None else None,
                json.dumps(plan.vars) if plan.vars else None,
                body.dial_gap_seconds,
                *cap_columns(body.dial_daily_cap),
            )
            added = await _insert_recipients(
                conn, tenant_id=ctx.tenant.id, batch_id=batch_id, rows=rows
            )
            if added != len(rows):
                # Unreachable: `_clean_recipients` already rejected duplicates
                # within the payload, and the batch is new so nothing else can
                # hold these numbers. Loud rather than a batch that silently
                # calls fewer people than the operator uploaded.
                raise HTTPException(
                    status_code=500,
                    detail=f"only {added} of {len(rows)} recipients were stored",
                )
            batch = await load_batch(conn, ctx.tenant.id, batch_id)
            assert batch is not None
            # In the same transaction as the rows: a batch that exists with no
            # job behind it is a list that silently never dials.
            await _enqueue_dial_job(conn, batch)
    logger.info(
        "created call batch %s (%d recipients, agent %s)",
        batch.id,
        len(rows),
        plan.entry.agent_id or plan.entry.name,
    )
    return await batch_response(ctx.tenant, batch)


async def add_recipients(
    batch_id: UUID, body: AddRecipientsRequest, ctx: Context
) -> AddRecipientsResponse:
    """Append to a batch, skipping numbers it already holds or the upload repeats.

    Skipping rather than rejecting is correct: a list the operator re-exported
    from their CRM will contain the people already called, and refusing the whole
    upload over that would make the guarantee feel like a bug.

    **The batch row's lock is what makes an append to a live batch safe.** It is
    taken first and held to the commit, and `dispatcher._step_finish` takes the
    same one before deciding the batch is done — so rows never land on a batch
    that has just finished without them. An append that adds anyone to a
    `completed` batch wakes it: `running` again, with a dialling job.
    """
    pool = await ctx.tenant_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                f"SELECT {BATCH_COLUMNS} FROM call_batches "
                "WHERE id = $1 AND tenant_id = $2 FOR UPDATE",
                batch_id,
                ctx.tenant.id,
            )
            if row is None:
                raise HTTPException(status_code=404, detail="call batch not found")
            batch = CallBatch.model_validate(dict(row))
            if batch.status not in _OPEN_STATUSES:
                raise HTTPException(
                    status_code=409,
                    detail=f"this batch is {batch.status}, so nobody can be added to it",
                )
            # The batch's own from-number decides how a national-format row is
            # read, so rows added later land in the same country as the ones
            # uploaded with it. `None` once the number is deleted, which falls
            # back to this region's configured default.
            did = (
                await conn.fetchval(
                    "SELECT e164 FROM phone_numbers WHERE id = $1 AND tenant_id = $2",
                    batch.from_phone_number_id,
                    ctx.tenant.id,
                )
                if batch.from_phone_number_id
                else None
            )
            rows, errors, skipped = _clean_recipients(body.recipients, did=did, skip_repeats=True)
            if errors:
                raise validation_error(errors, "invalid recipients")

            already = {
                r["to_e164"]
                for r in await conn.fetch(
                    """
                    SELECT to_e164 FROM call_batch_recipients
                    WHERE tenant_id = $1 AND batch_id = $2 AND to_e164 = ANY($3::text[])
                    """,
                    ctx.tenant.id,
                    batch_id,
                    [r[1] for r in rows],
                )
            }
            skipped.extend(
                SkippedRecipient(row_number=position, to=to_e164, reason="already in this batch")
                for position, to_e164, _ in rows
                if to_e164 in already
            )
            skipped.sort(key=lambda s: s.row_number)
            first_row = await conn.fetchval(
                """
                SELECT COALESCE(MAX(row_number), 0) + 1 FROM call_batch_recipients
                WHERE tenant_id = $1 AND batch_id = $2
                """,
                ctx.tenant.id,
                batch_id,
            )
            # Stored rows continue the batch's own sequence, contiguous after the
            # skips, so `row_number` keeps meaning "position in the batch".
            fresh = [
                (first_row + offset, to_e164, userdata)
                for offset, (_, to_e164, userdata) in enumerate(
                    [r for r in rows if r[1] not in already]
                )
            ]
            added = (
                await _insert_recipients(
                    conn, tenant_id=ctx.tenant.id, batch_id=batch_id, rows=fresh
                )
                if fresh
                else 0
            )
            if added != len(fresh):
                # Unreachable while the batch row is locked: nothing else can add
                # a number between the read above and this insert.
                raise HTTPException(
                    status_code=500, detail=f"only {added} of {len(fresh)} recipients were stored"
                )
            woken = added > 0 and batch.status == "completed"
            updated = await conn.fetchrow(
                f"""
                UPDATE call_batches
                SET total_recipients = total_recipients + $3,
                    -- Idle, not over: new people to call wake it. `started_at`
                    -- is kept, so `batch.started` does not fire a second time.
                    status = CASE WHEN $4 THEN 'running' ELSE status END,
                    ended_at = CASE WHEN $4 THEN NULL ELSE ended_at END,
                    updated_at = now()
                WHERE id = $1 AND tenant_id = $2
                RETURNING {BATCH_COLUMNS}
                """,
                batch_id,
                ctx.tenant.id,
                added,
                woken,
            )
            if woken:
                await _enqueue_dial_job(conn, CallBatch.model_validate(dict(updated)))
    if woken:
        logger.info("call batch %s: woken by %d new recipient(s)", batch_id, added)
    return AddRecipientsResponse(
        added=added, skipped=skipped, total_recipients=updated["total_recipients"]
    )


async def patch_batch(
    batch_id: UUID, body: PatchCallBatchRequest, ctx: Context
) -> CallBatchResponse:
    """Edit policy on a live or completed batch. The next dispatcher pass reads it.

    Recipients already dialled are never revisited — by construction, since the
    claim only ever selects `pending` — and calls already in flight finish under
    the policy they were dispatched with.
    """
    batch = await _require_batch(ctx, batch_id)
    if batch.status not in _OPEN_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=f"this batch is {batch.status} and can no longer be edited",
        )
    sent = body.model_fields_set
    errors: list[str] = []

    agent_id = body.agent_id or batch.agent_id
    number_id = body.from_phone_number_id or batch.from_phone_number_id
    # Pointing a batch at a different agent replaces whatever cast it had: the
    # calls that follow run THAT agent's current published version. A campaign
    # plan is decided at create — there is no partial edit of one, and silently
    # keeping the old override on a new agent would be the worse surprise.
    agent_plan = None if body.agent_id is not None else batch.agent_plan
    if body.agent_id is not None or body.from_phone_number_id is not None:
        if number_id is None or (agent_plan is None and agent_id is None):
            errors.append("this batch's agent or phone number was deleted; set both to continue")
        else:
            try:
                # `batch.vars`, because the bag is stored once at create and this
                # request cannot change it: re-pointing a campaign at an agent
                # that requires something the bag does not carry is refused here,
                # while the operator is looking at it.
                await dial.resolve_dial_target(
                    ctx,
                    agent_id=agent_id,
                    from_phone_number_id=number_id,
                    session_vars=batch.vars or {},
                    agent_plan=agent_plan,
                )
            except dial.DialError as exc:
                errors.append(exc.message)

    if "start_at" in sent:
        if batch.status != "scheduled":
            # A running batch has started; moving its start time means nothing.
            errors.append("start_at can only be changed while the batch is still scheduled")
        else:
            _validate_start_at(body.start_at, errors)
    if errors:
        raise validation_error(errors, "invalid call batch")

    window = body.calling_window if "calling_window" in sent else batch.calling_window
    daily_cap = body.dial_daily_cap if "dial_daily_cap" in sent else batch.daily_cap
    # An edited ramp keeps its progress unless its start moved; a new one starts
    # from the dialling days this batch has completed so far.
    ramp_base = ramp_base_days(
        daily_cap,
        batch.daily_cap,
        old_base=batch.dial_ramp_base_days,
        completed=completed_days(
            batch.dial_days, batch.last_dial_day, local_today(datetime.now(UTC), batch.timezone)
        ),
    )
    pool = await ctx.tenant_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                f"""
                UPDATE call_batches
                SET name = $3,
                    agent_id = $4,
                    agent_plan = $14::jsonb,
                    from_phone_number_id = $5,
                    start_at = $6,
                    timezone = $7,
                    window_start_local = $8,
                    window_end_local = $9,
                    window_days = $10,
                    max_concurrency = $11,
                    max_attempts = $12,
                    retry_after_minutes = $13,
                    dial_gap_seconds = $15,
                    dial_daily_cap = $16,
                    dial_ramp_start = $17,
                    dial_ramp_end = $18,
                    dial_ramp_step = $19,
                    dial_ramp_interval_days = $20,
                    dial_ramp_base_days = $21,
                    updated_at = now()
                WHERE id = $1 AND tenant_id = $2
                RETURNING {BATCH_COLUMNS}
                """,
                batch_id,
                ctx.tenant.id,
                body.name or batch.name,
                agent_id,
                number_id,
                body.start_at if "start_at" in sent else batch.start_at,
                body.timezone or batch.timezone,
                window.start if window else None,
                window.end if window else None,
                window.days if window else [1, 2, 3, 4, 5, 6, 7],
                body.max_concurrency or batch.max_concurrency,
                body.max_attempts or batch.max_attempts,
                body.retry_after_minutes or batch.retry_after_minutes,
                json.dumps(agent_plan) if agent_plan is not None else None,
                # 0 is a real value here, so absence is asked for by name.
                batch.dial_gap_seconds if body.dial_gap_seconds is None else body.dial_gap_seconds,
                *cap_columns(daily_cap),
                ramp_base,
            )
            assert row is not None
            fresh = CallBatch.model_validate(dict(row))
            if "start_at" in sent:
                # The job IS the schedule. Moving `start_at` without moving the
                # job would leave a batch that says one thing and does another —
                # and moving it *earlier* would otherwise not start any sooner.
                # `status = 'pending'` is the guard: a pass running right now owns
                # the row, and `run_dial_pass` honours `start_at` itself for
                # exactly that case.
                await conn.execute(
                    """
                    UPDATE scheduled_jobs SET scheduled_at = $3, updated_at = now()
                    WHERE tenant_id = $1 AND subject_id = $2
                      AND kind = $4 AND status = 'pending'
                    """,
                    ctx.tenant.id,
                    batch_id,
                    fresh.start_at or datetime.now(UTC),
                    str(jobs.JobKind.CALL_BATCH_DIAL),
                )
    return await batch_response(ctx.tenant, fresh)


async def _transition(
    ctx: Context, batch_id: UUID, *, sql: str, allowed: tuple[str, ...], verb: str
) -> CallBatchResponse:
    """One guarded compare-and-set on the policy row, or a 409 naming the status."""
    pool = await ctx.tenant_pool()
    row = await pool.fetchrow(sql, batch_id, ctx.tenant.id)
    if row is None:
        batch = await _require_batch(ctx, batch_id)
        raise HTTPException(
            status_code=409,
            detail=f"a {batch.status} batch cannot be {verb} (only {', '.join(allowed)} can be)",
        )
    return await batch_response(ctx.tenant, CallBatch.model_validate(dict(row)))


async def pause_batch(batch_id: UUID, ctx: Context) -> CallBatchResponse:
    """Stop claiming new recipients. Calls already in flight run to their end.

    Pausing a `completed` batch makes later appends wait for a resume. It clears
    `ended_at`, without which a resumed batch's pass would see it as over.
    """
    return await _transition(
        ctx,
        batch_id,
        sql=f"""
        UPDATE call_batches SET status = 'paused', ended_at = NULL, updated_at = now()
        WHERE id = $1 AND tenant_id = $2 AND status IN ('scheduled', 'running', 'completed')
        RETURNING {BATCH_COLUMNS}
        """,
        allowed=("scheduled", "running", "completed"),
        verb="paused",
    )


async def resume_batch(batch_id: UUID, ctx: Context) -> CallBatchResponse:
    """Undo a pause, and make sure the batch still has a dialling job.

    Back to `scheduled` rather than `running` when the start time is still in the
    future — pausing and resuming a batch that has not begun must not be a way to
    make it begin early.

    Hand-rolled rather than routed through :func:`_transition`, which is a bare
    single statement no second write can join. **This is the failure the job
    migration exists to prevent:** resume commits, the enqueue then fails, and
    the batch sits `running` with no job behind it — stalled for ever, with no
    discovery loop left to notice. `pause` and `cancel` enqueue nothing and keep
    the simpler helper.
    """
    pool = await ctx.tenant_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                f"""
                UPDATE call_batches
                SET status = CASE
                        WHEN start_at IS NOT NULL AND start_at > now() THEN 'scheduled'
                        ELSE 'running'
                    END,
                    updated_at = now()
                WHERE id = $1 AND tenant_id = $2 AND status = 'paused'
                RETURNING {BATCH_COLUMNS}
                """,
                batch_id,
                ctx.tenant.id,
            )
            if row is None:
                batch = await _require_batch(ctx, batch_id)
                raise HTTPException(
                    status_code=409,
                    detail=f"a {batch.status} batch cannot be resumed (only paused can be)",
                )
            fresh = CallBatch.model_validate(dict(row))
            await _enqueue_dial_job(conn, fresh)
    return await batch_response(ctx.tenant, fresh)


async def _enqueue_dial_job(conn: asyncpg.Connection, batch: CallBatch) -> None:
    """Give this batch a dialling job — unless it already has one.

    Guarded against duplicates, in the caller's transaction. Two concurrent
    resumes serialise on the batch row's own lock and only one reaches here, so
    `enqueue_once`'s weakness under truly simultaneous inserts cannot bite; what
    it is really for is the ordinary sequence of presses, where a second live job
    would double the one limit the batch's owner asked us to respect.

    `scheduled_at` is the batch's own `start_at`, so a batch scheduled for
    tomorrow is asleep until tomorrow instead of being woken every 30 s to find
    out it is not time yet.
    """
    scheduled_at = (
        batch.start_at if batch.status == "scheduled" and batch.start_at else datetime.now(UTC)
    )
    job_id = await jobs.enqueue_once(
        conn,
        tenant_id=batch.tenant_id,
        kind=jobs.JobKind.CALL_BATCH_DIAL,
        scheduled_at=scheduled_at,
        subject_id=batch.id,
        args={"batch_id": str(batch.id)},
    )
    if job_id is None:
        logger.info("call batch %s already has a dialling job; not enqueuing another", batch.id)


async def delete_batch(batch_id: UUID, ctx: Context) -> None:
    """Delete a finished batch and its recipient rows for good. Its calls stay.

    Refused while a call of it is still in progress: cancelling leaves those to
    run to their natural end, and each one still has a recipient row to settle.
    """
    await _require_batch(ctx, batch_id)
    pool = await ctx.tenant_pool()
    deleted = await pool.fetchval(
        """
        DELETE FROM call_batches b
        WHERE b.id = $1 AND b.tenant_id = $2 AND b.status IN ('completed', 'canceled', 'failed')
          AND NOT EXISTS (
            SELECT 1 FROM call_batch_recipients r
            WHERE r.batch_id = b.id AND r.tenant_id = b.tenant_id AND r.status = 'dialing'
          )
        RETURNING b.id
        """,
        batch_id,
        ctx.tenant.id,
    )
    if deleted is None:
        batch = await _require_batch(ctx, batch_id)
        raise HTTPException(
            status_code=409,
            detail=(
                "calls from this batch are still in progress; delete it once they end"
                if batch.status in ("completed", "canceled", "failed")
                else f"a {batch.status} batch cannot be deleted; cancel it first"
            ),
        )
    logger.info("deleted call batch %s", batch_id)


async def cancel_batch(batch_id: UUID, ctx: Context) -> CallBatchResponse:
    """Stop the batch for good — one row, whatever its size.

    Deliberately does **not** set `ended_at`, and deliberately does not touch a
    single recipient. Calls already ringing run to their natural end; the
    dispatcher stops claiming on its next pass, keeps settling what is in flight,
    and writes `ended_at` once nothing is left. Dropping a human mid-sentence to
    honour a button is worse than the ninety seconds it saves.

    The one exception is a batch with no job — paused after it had completed —
    where no pass is coming to write it, and nothing is in flight.
    """
    return await _transition(
        ctx,
        batch_id,
        sql=f"""
        UPDATE call_batches b SET status = 'canceled', updated_at = now(),
            ended_at = CASE WHEN EXISTS (
                SELECT 1 FROM scheduled_jobs j
                WHERE j.tenant_id = b.tenant_id AND j.subject_id = b.id
                  AND j.kind = '{jobs.JobKind.CALL_BATCH_DIAL}'
                  AND j.status IN ('pending', 'running')
            ) THEN b.ended_at ELSE now() END
        WHERE b.id = $1 AND b.tenant_id = $2 AND b.status IN ('scheduled', 'running', 'paused')
        RETURNING {BATCH_COLUMNS}
        """,
        allowed=("scheduled", "running", "paused"),
        verb="canceled",
    )
