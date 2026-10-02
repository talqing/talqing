"""Creating, reading and steering an email batch — everything a human does.

The split this file is built on, inherited from batch calling: **the batch row
is policy, the recipient rows are work.** Pause, resume, edit, reschedule and
cancel are all writes to the one policy row and none of them touches a work row.
The drafting loop re-reads policy as it goes, so there is nothing to coordinate
and no in-flight state to reconcile — an edit reaches a live batch within
`recheck_seconds`.

**One thing every verb here owes the reader: `next_draft_at`.** A change to when
drafting next acts is written in the same transaction as the change itself.
Missing it leaves a batch scheduled for Monday showing no "Next" until Monday,
which is worse than what the column replaced.

Sending is deliberately not here: it is a different policy row with a different
lifetime, and it lives in ``sends.py``. What is here is everything a human does
to the **batch** — create it, steer its drafting, edit a cell, skip a row,
redraft.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import asyncpg
from fastapi import HTTPException

import db
from api.core.schemas import Page, page_slice, validation_error
from services import jobs
from services.scheduling import (
    cap_columns,
    completed_days,
    daily_cap_today,
    local_day_bounds,
    local_today,
    ramp_base_days,
)
from services.system_vars import validate_var_name
from services.tasks import TaskConfig
from services.user import Context, Tenant

from ..credentials import (
    EmailCredentialError,
    load_email_integration,
    resolve_email_credential,
)
from .models import (
    BATCH_COLUMNS,
    LIVE_SEND_STATUSES,
    RECIPIENT_COLUMNS,
    RECIPIENT_COLUMNS_R,
    AddEmailRecipientsRequest,
    AddEmailRecipientsResponse,
    CreateEmailBatchRequest,
    DraftedVersion,
    DraftFailure,
    EmailBatch,
    EmailBatchCounts,
    EmailBatchResponse,
    EmailBatchSkips,
    EmailRecipient,
    EmailRecipientResponse,
    FieldMap,
    PatchEmailBatchRequest,
    PatchEmailRecipientRequest,
    RecipientActionResponse,
    RedraftRequest,
    RejectedRecipient,
    SelectRecipientsRequest,
    SkippedEmailRecipient,
    VerifiedSendersResponse,
    email_domain,
    is_email_address,
    resolve_send_fields,
    unfillable_reason,
)

logger = logging.getLogger("talqing.email.batch")

# A schedule further out than this is not a schedule, it is a reminder.
MAX_SCHEDULE_DAYS_AHEAD = 90
# Clock skew between a browser and the server, tolerated on `start_at`.
SCHEDULE_PAST_TOLERANCE = timedelta(minutes=1)
# How many distinct reasons `draft_failures` lists. The long tail is one-off
# provider errors, and the page needs the reasons that hold most of the rows.
_DRAFT_FAILURES_SHOWN = 5

# Statuses in which policy may still be edited at all.
_EDITABLE_STATUSES = ("scheduled", "drafting", "paused", "drafted")
# What each reversible verb may act on. Two entries, and both are their own
# undo. `redraft` is not here — it discards work and revives the batch, so it is
# its own function — and neither is creating a send, which is the one
# irreversible verb in the feature.
_SELECT_VERBS: dict[str, tuple[tuple[str, ...], str]] = {
    # verb: (statuses it acts on, sentence for a row in the wrong state)
    #
    # `skip` accepts `draft_failed` as well as `draft`: a row whose draft came
    # back without a subject is still one the operator may want out of the
    # review table, next to a "Redraft N failed" button inviting them to pay
    # again. A draft a live send covers is a stored `draft` too, so "actually,
    # not this one" works right up until the row is claimed. Skipping is its own
    # undo, so there is nothing to protect against.
    "skip": (("draft", "draft_failed"), "only a drafted row can be skipped"),
    # `restore` puts back what a PERSON skipped, and a duplicate the send found.
    # An `unfillable` row is refused: restoring it re-runs the rule that skipped
    # it, which puts it straight back. Filling in its cell is what reverses it.
    "restore": (("skipped",), "only a skipped row can be restored"),
}

# The fields on which an explicit `null` means something — "no start time", "no
# window", "no ceiling", "no display name", "no reply-to". Everywhere else on a
# PATCH body, null is a mistake and is refused rather than treated as absent.
_NULL_MEANS_NOTHING = frozenset({"start_at", "window", "send_daily_cap", "from_name", "reply_to"})

# What `redraft` may act on: every row that has not left. A draft a live send
# covers is one of them — redrafting a PAUSED send's rows is the point of pausing
# it — and the send then takes the new draft. `drafting`, `sending` and `sent`
# are refused by name.
REDRAFTABLE = ("pending", "draft", "draft_failed", "skipped", "send_failed")
# ...except an `unfillable` row: no draft could be sent, so redrafting one would
# pay for a run to land it straight back where it is. `r` is the recipient, and
# `$n` binds REDRAFTABLE.
_REDRAFTABLE_SQL = "r.status = ANY({}::text[]) AND r.skip_reason IS DISTINCT FROM 'unfillable'"

# Is this row held by a live `selected` send? `r` is the recipient. A live `all`
# send covers every draft of its batch, so that half is answered once per batch
# (`_standing_send`) rather than once per row.
_IN_LIVE_SELECTED_SEND = """
EXISTS (
    SELECT 1 FROM email_send_members m
    JOIN email_send_runs s ON s.id = m.send_run_id AND s.tenant_id = m.tenant_id
    WHERE m.tenant_id = r.tenant_id AND m.recipient_id = r.id
      AND s.status IN ('scheduled', 'sending', 'paused')
)"""


def effective_status(recipient_status: str, batch_status: str, covered: bool) -> str:
    """What a reader sees, which is not always what is stored.

    Two facts about the BATCH read as row statuses, and neither is written to a
    row. A draft that a live send covers is ``queued`` — creating a send writes
    no recipient row and cancelling one writes none back. And cancelling a
    5 000-row batch writes one row, the batch's, so its untouched recipients read
    ``canceled``. ``draft`` rows are deliberately not cancelled with it:
    cancelling stops *drafting*, and drafts already produced are still worth
    sending. Everything else reports itself.
    """
    if recipient_status == "draft" and covered:
        return "queued"
    if recipient_status == "pending" and batch_status in ("canceled", "failed"):
        return "canceled"
    return recipient_status


# ── reads ───────────────────────────────────────────────────────────────────


async def _standing_send(conn, tenant_id: UUID, batch_id: UUID) -> bool:
    """Is a standing (`all`) send live on this batch? Then every draft is queued."""
    return bool(
        await conn.fetchval(
            """
            SELECT EXISTS (
                SELECT 1 FROM email_send_runs
                WHERE tenant_id = $1 AND batch_id = $2 AND scope = 'all'
                  AND status IN ('scheduled', 'sending', 'paused')
            )
            """,
            tenant_id,
            batch_id,
        )
    )


async def load_batch(conn, tenant_id: UUID, batch_id: UUID) -> EmailBatch | None:
    """The policy row. Takes a connection or a pool — a job has both."""
    row = await conn.fetchrow(
        f"SELECT {BATCH_COLUMNS} FROM email_batches WHERE id = $1 AND tenant_id = $2",
        batch_id,
        tenant_id,
    )
    return _batch_row(row) if row else None


# `db.pool` registers a jsonb codec, so every JSONB column arrives as a Python
# object and every jsonb parameter accepts one (or already-serialized text).
# Nothing here parses or serializes JSON around the database boundary.
def _batch_row(row: asyncpg.Record) -> EmailBatch:
    return EmailBatch.model_validate(dict(row))


def _recipient_row(row: asyncpg.Record) -> EmailRecipient:
    return EmailRecipient.model_validate(dict(row))


async def _published_tasks(
    conn, tenant_id: UUID, task_ids: list[UUID]
) -> dict[UUID, tuple[int, list[str]]]:
    """``task id -> (published version, its output field names)``.

    The PUBLISHED definition, not the draft, and not a snapshot on the batch: a
    batch drafts with whatever is published, so that is the only set of columns
    its rows can actually hold, and that version is the one every
    ``drafted_versions`` count is measured against. Reading the draft would grow
    the table a column for an output field nobody has published yet.
    """
    if not task_ids:
        return {}
    out: dict[UUID, tuple[int, list[str]]] = {}
    for row in await conn.fetch(
        "SELECT t.id, v.version, v.config FROM agent_tasks t "
        "JOIN agent_task_versions v ON v.task_id = t.id AND v.tenant_id = t.tenant_id "
        "  AND v.version = t.published_version "
        "WHERE t.tenant_id = $1 AND t.id = ANY($2::uuid[])",
        tenant_id,
        task_ids,
    ):
        out[row["id"]] = (
            row["version"],
            [str(f["name"]) for f in (row["config"].get("output") or [])],
        )
    return out


async def _responses(conn, tenant_id: UUID, batches: list[EmailBatch]) -> list[EmailBatchResponse]:
    """Serialize batches, resolving names, counts and cost in four queries however
    many there are.

    ``task_name`` and ``integration_name`` are joined rather than snapshot, so a
    rename shows through and a deletion shows null — which is also what a
    drafting pass sees when it fails the batch.
    """
    if not batches:
        return []
    batch_ids = [b.id for b in batches]
    task_ids = [b.task_id for b in batches if b.task_id]
    integration_ids = [b.integration_id for b in batches if b.integration_id]

    task_names: dict[UUID, str] = {}
    if task_ids:
        task_names = {
            r["id"]: r["name"]
            for r in await conn.fetch(
                "SELECT id, name FROM agent_tasks WHERE tenant_id = $1 AND id = ANY($2::uuid[])",
                tenant_id,
                task_ids,
            )
        }
    integration_names: dict[UUID, str] = {}
    if integration_ids:
        integration_names = {
            r["id"]: r["display_name"]
            for r in await conn.fetch(
                "SELECT id, display_name FROM integrations "
                "WHERE tenant_id = $1 AND id = ANY($2::uuid[])",
                tenant_id,
                integration_ids,
            )
        }
    published = await _published_tasks(conn, tenant_id, task_ids)

    # Which published version wrote each row. One grouped join for the page, and
    # the whole of what makes "improve the task, redraft the rest" visible.
    #
    # `stale_redraftable` rides along in the same query because it answers the
    # question the banner actually asks: not "how many rows did v2 write" —
    # which stays true for ever, sent rows included — but "how many would the
    # button in front of me rewrite". Those differ the moment anything is sent,
    # and the banner built on the first number never went away and offered to
    # redraft nothing.
    versions: dict[UUID, list[DraftedVersion]] = {b.id: [] for b in batches}
    stale: dict[UUID, int] = dict.fromkeys(batch_ids, 0)
    stale_sent: dict[UUID, int] = dict.fromkeys(batch_ids, 0)
    for row in await conn.fetch(
        f"""
        SELECT r.batch_id, t.task_version AS version, count(*) AS n,
               count(*) FILTER (
                   WHERE {_REDRAFTABLE_SQL.format("$3")}
                     AND t.task_version < k.published_version
               ) AS redraftable,
               count(*) FILTER (
                   WHERE r.status IN ('sending', 'sent') AND t.task_version < k.published_version
               ) AS gone
        FROM email_batch_recipients r
        JOIN task_runs t ON t.id = r.task_run_id AND t.tenant_id = r.tenant_id
        JOIN email_batches b ON b.id = r.batch_id AND b.tenant_id = r.tenant_id
        LEFT JOIN agent_tasks k ON k.id = b.task_id AND k.tenant_id = r.tenant_id
        WHERE r.tenant_id = $1 AND r.batch_id = ANY($2::uuid[]) AND t.task_version IS NOT NULL
        GROUP BY r.batch_id, t.task_version
        ORDER BY t.task_version DESC
        """,
        tenant_id,
        batch_ids,
        list(REDRAFTABLE),
    ):
        versions[row["batch_id"]].append(DraftedVersion(version=row["version"], count=row["n"]))
        stale[row["batch_id"]] += row["redraftable"]
        stale_sent[row["batch_id"]] += row["gone"]

    # One GROUP BY over an indexed partial, for the whole page. Only `total` is
    # stored — it is immutable except by append, so it cannot drift, and it is
    # the denominator of every progress bar.
    tallies: dict[UUID, Counter[str]] = {b.id: Counter() for b in batches}
    for row in await conn.fetch(
        """
        SELECT batch_id, status, count(*) AS n
        FROM email_batch_recipients
        WHERE tenant_id = $1 AND batch_id = ANY($2::uuid[])
        GROUP BY batch_id, status
        """,
        tenant_id,
        batch_ids,
    ):
        tallies[row["batch_id"]][row["status"]] = row["n"]

    # Which drafts a live send covers, and so read `queued`. Driven from the live
    # runs, of which a batch has a handful: a live standing send covers every
    # draft, and otherwise the live `selected` sends' members are counted — a row
    # is a member of at most one live send, so nothing counts twice.
    standing: set[UUID] = set()
    held: dict[UUID, int] = {}
    for row in await conn.fetch(
        """
        SELECT s.batch_id, bool_or(s.scope = 'all') AS standing, count(r.id) AS held
        FROM email_send_runs s
        LEFT JOIN email_send_members m ON m.send_run_id = s.id AND m.tenant_id = s.tenant_id
        LEFT JOIN email_batch_recipients r
            ON r.id = m.recipient_id AND r.tenant_id = m.tenant_id AND r.status = 'draft'
        WHERE s.tenant_id = $1 AND s.batch_id = ANY($2::uuid[])
          AND s.status IN ('scheduled', 'sending', 'paused')
        GROUP BY s.batch_id
        """,
        tenant_id,
        batch_ids,
    ):
        if row["standing"]:
            standing.add(row["batch_id"])
        held[row["batch_id"]] = row["held"]

    # Why rows are skipped, and why drafts failed: the two numbers that decide
    # whether a redraft can help, which a bare count cannot.
    skips: dict[UUID, Counter[str]] = {b.id: Counter() for b in batches}
    failures: dict[UUID, list[DraftFailure]] = {b.id: [] for b in batches}
    for row in await conn.fetch(
        """
        SELECT batch_id, status,
               CASE WHEN status = 'skipped' THEN skip_reason ELSE last_error END AS reason,
               count(*) AS n
        FROM email_batch_recipients
        WHERE tenant_id = $1 AND batch_id = ANY($2::uuid[])
          AND (status = 'skipped' OR (status = 'draft_failed' AND last_error IS NOT NULL))
        GROUP BY batch_id, status, reason
        ORDER BY n DESC
        """,
        tenant_id,
        batch_ids,
    ):
        if row["status"] == "skipped":
            skips[row["batch_id"]][row["reason"]] = row["n"]
        elif len(failures[row["batch_id"]]) < _DRAFT_FAILURES_SHOWN:
            failures[row["batch_id"]].append(DraftFailure(reason=row["reason"], count=row["n"]))

    # What drafting has cost so far. Task spend is not in the Observability
    # charts — those are built on `sessions` — so this is the only place a
    # tenant sees what a batch is costing them, and it is why there is a running
    # total on the batch page rather than a spend cap in front of it.
    costs: dict[UUID, Decimal] = {}
    for row in await conn.fetch(
        """
        SELECT r.batch_id, sum(t.provider_cost) AS cost
        FROM email_batch_recipients r
        JOIN task_runs t ON t.id = r.task_run_id AND t.tenant_id = r.tenant_id
        WHERE r.tenant_id = $1 AND r.batch_id = ANY($2::uuid[])
        GROUP BY r.batch_id
        """,
        tenant_id,
        batch_ids,
    ):
        if row["cost"] is not None:
            costs[row["batch_id"]] = row["cost"]

    now = datetime.now(UTC)
    # What the daily cap has left. Grouped by TIMEZONE rather than asked per
    # batch: "today" is a different pair of instants for every zone on the page,
    # so one shared bound would be wrong — but a page of fifty batches is almost
    # always one or two zones, and a query each would be fifty round trips for
    # one number.
    sent_today: dict[UUID, int] = {b.id: 0 for b in batches}
    by_zone: dict[str, list[UUID]] = {}
    for batch in batches:
        by_zone.setdefault(batch.timezone, []).append(batch.id)
    for zone, ids in by_zone.items():
        day_start, day_end = local_day_bounds(now, zone)
        for row in await conn.fetch(
            """
            SELECT batch_id, count(*) AS n FROM email_batch_recipients
            WHERE tenant_id = $1 AND batch_id = ANY($2::uuid[])
              AND sent_at >= $3 AND sent_at < $4
            GROUP BY batch_id
            """,
            tenant_id,
            ids,
            day_start,
            day_end,
        ):
            sent_today[row["batch_id"]] = row["n"]

    out: list[EmailBatchResponse] = []
    for batch in batches:
        counts: Counter[str] = Counter()
        for status, n in tallies[batch.id].items():
            counts[effective_status(status, batch.status, covered=False)] += n
        queued = counts["draft"] if batch.id in standing else held.get(batch.id, 0)
        counts["draft"] -= queued
        counts["queued"] = queued
        skipped = skips[batch.id]
        out.append(
            EmailBatchResponse(
                id=batch.id,
                name=batch.name,
                task_id=batch.task_id,
                task_name=task_names.get(batch.task_id) if batch.task_id else None,
                published_task_version=(
                    published[batch.task_id][0]
                    if batch.task_id and batch.task_id in published
                    else None
                ),
                integration_id=batch.integration_id,
                integration_name=(
                    integration_names.get(batch.integration_id) if batch.integration_id else None
                ),
                from_email=batch.from_email,
                from_name=batch.from_name,
                reply_to=batch.reply_to,
                body_format=batch.body_format,
                field_map=batch.field_map,
                input_columns=batch.input_columns,
                output_columns=(
                    published[batch.task_id][1]
                    if batch.task_id and batch.task_id in published
                    else []
                ),
                status=batch.status,
                failure_reason=batch.failure_reason,
                start_at=batch.start_at,
                timezone=batch.timezone,
                window=batch.window,
                draft_concurrency=batch.draft_concurrency,
                draft_attempts=batch.draft_attempts,
                draft_retry_after_minutes=batch.draft_retry_after_minutes,
                draft_gap_seconds=batch.draft_gap_seconds,
                send_attempts=batch.send_attempts,
                send_retry_after_minutes=batch.send_retry_after_minutes,
                send_gap_seconds=batch.send_gap_seconds,
                send_daily_cap=batch.daily_cap,
                send_daily_cap_today=daily_cap_today(
                    batch.daily_cap,
                    base_days=batch.send_ramp_base_days,
                    days=batch.send_days,
                    last_day=batch.last_send_day,
                    today=local_today(now, batch.timezone),
                ),
                counts=EmailBatchCounts(
                    total=batch.total_recipients,
                    pending=counts["pending"],
                    drafting=counts["drafting"],
                    draft=counts["draft"],
                    draft_failed=counts["draft_failed"],
                    skipped=counts["skipped"],
                    queued=counts["queued"],
                    sending=counts["sending"],
                    sent=counts["sent"],
                    send_failed=counts["send_failed"],
                    canceled=counts["canceled"],
                ),
                # A skip reason the model does not name is a writer out of step
                # with it, and `extra="forbid"` refuses it here rather than
                # dropping those rows from the breakdown.
                skips=EmailBatchSkips.model_validate(
                    {"operator": 0, "duplicate_recipient": 0, "unfillable": 0, **skipped}
                ),
                draft_failures=failures[batch.id],
                sent_today=sent_today[batch.id],
                drafted_versions=versions[batch.id],
                stale_redraftable=stale[batch.id],
                stale_sent=stale_sent[batch.id],
                next_draft_at=batch.next_draft_at,
                next_draft_reason=batch.next_draft_reason,
                provider_cost=costs.get(batch.id),
                created_at=batch.created_at,
                started_at=batch.started_at,
                drafted_at=batch.drafted_at,
            )
        )
    return out


async def batch_response(tenant: Tenant, batch: EmailBatch) -> EmailBatchResponse:
    """One batch, serialized from a ``Tenant`` — what the webhooks carry."""
    pool = await db.tenant_pool(tenant)
    return (await _responses(pool, tenant.id, [batch]))[0]


async def _require_batch(ctx: Context, batch_id: UUID) -> EmailBatch:
    pool = await ctx.tenant_pool()
    batch = await load_batch(pool, ctx.tenant.id, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="email batch not found")
    return batch


async def list_batches(
    ctx: Context, *, limit: int, offset: int, status: str | None
) -> Page[EmailBatchResponse]:
    pool = await ctx.tenant_pool()
    rows = await pool.fetch(
        f"""
        SELECT {BATCH_COLUMNS} FROM email_batches
        WHERE tenant_id = $1 AND ($4::text IS NULL OR status = $4::text)
        ORDER BY created_at DESC
        LIMIT $2 OFFSET $3
        """,
        ctx.tenant.id,
        limit + 1,
        offset,
        status,
    )
    batches = [_batch_row(r) for r in rows]
    return page_slice(await _responses(pool, ctx.tenant.id, batches), limit=limit, offset=offset)


async def get_batch(ctx: Context, batch_id: UUID) -> EmailBatchResponse:
    return await batch_response(ctx.tenant, await _require_batch(ctx, batch_id))


def _recipient_out(
    batch: EmailBatch, recipient: EmailRecipient, task_version: int | None, covered: bool
) -> EmailRecipientResponse:
    _, reason = resolve_send_fields(batch, recipient)
    return EmailRecipientResponse(
        id=recipient.id,
        row_number=recipient.row_number,
        input=recipient.input,
        output=recipient.output,
        overrides=recipient.overrides,
        columns=recipient.columns(),
        status=effective_status(recipient.status, batch.status, covered),
        not_ready_reason=reason,
        skip_reason=recipient.skip_reason,
        task_run_id=recipient.task_run_id,
        task_version=task_version,
        to_email=recipient.to_email,
        sent_from=recipient.sent_from,
        sent_at=recipient.sent_at,
        send_run_id=recipient.send_run_id,
        attempts=recipient.attempts,
        send_attempts=recipient.send_attempts,
        last_error=recipient.last_error,
        updated_at=recipient.updated_at,
    )


async def list_recipients(
    ctx: Context, batch_id: UUID, *, limit: int, offset: int, status: str | None
) -> Page[EmailRecipientResponse]:
    """One batch's rows, in the operator's own file order.

    ``status`` filters on the **effective** status, which for four of them means
    a predicate on something besides the row: `queued` is a `draft` a live send
    covers and `draft` is one none does, while `canceled` matches nothing on a
    live batch and every untouched row on a cancelled one.
    """
    batch = await _require_batch(ctx, batch_id)
    pool = await ctx.tenant_pool()
    standing = await _standing_send(pool, ctx.tenant.id, batch_id)
    stored: str | None = None
    # Null: either. True: only drafts a live send covers. False: only those none does.
    covered: bool | None = None
    if status is not None:
        canceled_batch = batch.status in ("canceled", "failed")
        if status == "canceled":
            if not canceled_batch:
                return Page(items=[], has_more=False, limit=limit, offset=offset)
            stored = "pending"
        elif status == "pending" and canceled_batch:
            return Page(items=[], has_more=False, limit=limit, offset=offset)
        elif status in ("queued", "draft"):
            stored, covered = "draft", status == "queued"
        else:
            stored = status

    # LEFT JOIN, not a second query: `task_version` is what the page groups by to
    # offer "redraft the rows an older prompt wrote", and a per-row lookup would
    # be fifty round trips for one column. Coverage rides along the same way.
    rows = await pool.fetch(
        f"""
        SELECT * FROM (
            SELECT {RECIPIENT_COLUMNS_R}, t.task_version,
                   r.status = 'draft' AND ($6::boolean OR {_IN_LIVE_SELECTED_SEND}) AS covered
            FROM email_batch_recipients r
            LEFT JOIN task_runs t ON t.id = r.task_run_id AND t.tenant_id = r.tenant_id
            WHERE r.tenant_id = $1 AND r.batch_id = $2
              AND ($5::text IS NULL OR r.status = $5::text)
        ) page
        WHERE $7::boolean IS NULL OR covered = $7::boolean
        ORDER BY row_number
        LIMIT $3 OFFSET $4
        """,
        ctx.tenant.id,
        batch_id,
        limit + 1,
        offset,
        stored,
        standing,
        covered,
    )
    return page_slice(
        [_recipient_out(batch, _recipient_row(r), r["task_version"], r["covered"]) for r in rows],
        limit=limit,
        offset=offset,
    )


async def verified_senders(ctx: Context, integration_id: UUID) -> VerifiedSendersResponse:
    """Which domains this account may send from, asked of the provider now.

    Read live rather than cached: a domain's verification is something a tenant
    changes in someone else's dashboard, and a stale list here would offer a
    sender that fails twenty minutes later.
    """
    pool = await ctx.tenant_pool()
    integration = await load_email_integration(pool, ctx.tenant.id, integration_id)
    if integration is None:
        raise HTTPException(status_code=404, detail="that email account does not exist")
    try:
        provider, credential = await resolve_email_credential(ctx.tenant, integration)
        domains = await provider.verified_senders(credential=credential)
    except EmailCredentialError as exc:
        raise validation_error([str(exc)], "this email account cannot send") from None
    except Exception as exc:
        raise validation_error(
            [f"we could not reach your {integration.display_name} account: {exc}"],
            "this email account could not be reached",
        ) from None
    return VerifiedSendersResponse(integration_id=integration_id, domains=domains)


# ── create ──────────────────────────────────────────────────────────────────


def validate_start_at(start_at: datetime | None, errors: list[str]) -> None:
    if start_at is None:
        return
    now = datetime.now(UTC)
    if start_at < now - SCHEDULE_PAST_TOLERANCE:
        errors.append("start_at is in the past")
    elif start_at > now + timedelta(days=MAX_SCHEDULE_DAYS_AHEAD):
        errors.append(f"start_at cannot be more than {MAX_SCHEDULE_DAYS_AHEAD} days from now")


def _csv_columns(rows: list[dict[str, str]], errors: list[str]) -> list[str]:
    """Every header the upload carries, in the order it first appears.

    Taken from the keys BEFORE empty cells are dropped, so a column that happens
    to be blank on the first row is still a column — which matters, because the
    field map may name it and the review table has to render it.
    """
    columns: list[str] = []
    for row in rows:
        for header in row:
            if header not in columns:
                columns.append(header)
    for header in columns:
        try:
            validate_var_name(header)
        except ValueError as exc:
            # Headers become the task's `{{vars.*}}` names, so a header the
            # resolver could not read back would be a column nothing can use.
            errors.append(f"column {header!r} cannot be a variable name: {exc}")
    return columns


def check_field_map(
    field_map: FieldMap, input_columns: list[str], task_config: TaskConfig
) -> list[str]:
    """Every problem with a mapping, named. Empty means it is sound.

    **Re-run whenever the task is published again**, not only at create.
    `publish_task` and `rollback_task_version` refuse the common breakages at the
    source, but a publish mid-batch can still move ground they cannot see, and
    the alternative is four thousand rows that generate fine and turn out to be
    unsendable at the review gate.
    """
    errors: list[str] = []
    outputs = {f.name: f for f in task_config.output}
    for field, column in field_map.columns().items():
        if column in input_columns:
            continue
        declared = outputs.get(column)
        if declared is None:
            errors.append(
                f"field_map.{field} names {column!r}, which is neither a column in the list "
                f"nor something the task produces"
            )
        elif declared.type != "string":
            article = "an" if declared.type[0] in "aeiou" else "a"
            errors.append(
                f"field_map.{field} names the task's {column!r}, which is {article} "
                f"{declared.type} — an email's {field} has to be text"
            )
    return errors


def _check_columns_against_task(
    input_columns: list[str], task_config: TaskConfig, errors: list[str]
) -> None:
    """The two rules that keep the merged column space readable.

    A collision means one of the two values is unreachable, and a required
    variable with no column and no default means every row fails before the
    model is ever reached.
    """
    output_names = {f.name for f in task_config.output}
    collisions = sorted(set(input_columns) & output_names)
    if collisions:
        errors.append(
            f"these columns are also fields the task produces: {', '.join(collisions)} - "
            "a row holds both side by side, so one of the two would be unreachable. "
            "Rename the column, or rename the output field"
        )
    uploaded = set(input_columns)
    for var in task_config.vars:
        if var.required and var.name not in uploaded and var.default is None:
            errors.append(
                f"the task requires a {var.name!r} variable and the list has no such column - "
                "add it, give the variable a default, or make it optional"
            )


async def check_sender(
    tenant: Tenant, integration_id: UUID, from_email: str, errors: list[str]
) -> None:
    """The address parses, and its domain is verified on that account right now.

    Checked live rather than trusted, because "that domain is not verified" is a
    sentence an operator can act on at the moment they are typing it, and fifty
    `send_failed` rows twenty minutes later is not.
    """
    if not is_email_address(from_email):
        errors.append(f"{from_email!r} is not an email address")
        return
    pool = await db.tenant_pool(tenant)
    integration = await load_email_integration(pool, tenant.id, integration_id)
    if integration is None:
        errors.append("that email account does not exist")
        return
    try:
        provider, credential = await resolve_email_credential(tenant, integration)
        verified = await provider.verified_senders(credential=credential)
    except EmailCredentialError as exc:
        errors.append(str(exc))
        return
    except Exception as exc:
        # Never swallowed: an operator who cannot tell "your domain is not
        # verified" from "we could not reach Resend" will go looking in the
        # wrong dashboard.
        logger.warning(
            "could not list verified senders for integration %s: %s", integration.id, exc
        )
        errors.append(f"we could not reach your {integration.display_name} account: {exc}")
        return
    if email_domain(from_email) not in verified:
        errors.append(
            f"{from_email} cannot send: {email_domain(from_email)} is not a verified domain on "
            f"{integration.display_name}"
            + (f" (verified: {', '.join(verified)})" if verified else " (no domains verified yet)")
        )


async def _require_task_config(conn, tenant_id: UUID, task_id: UUID) -> TaskConfig | None:
    """The task's PUBLISHED config, or None if it has no task or no publish.

    The published one and not the draft, because that is what a drafting pass
    runs: validating a mapping against a definition nobody has approved would
    pass at create and break at the first row.
    """
    config = await conn.fetchval(
        "SELECT v.config FROM agent_tasks t "
        "JOIN agent_task_versions v ON v.task_id = t.id AND v.tenant_id = t.tenant_id "
        "  AND v.version = t.published_version "
        "WHERE t.id = $1 AND t.tenant_id = $2",
        task_id,
        tenant_id,
    )
    if config is None:
        return None
    return TaskConfig.model_validate(config)


async def create_batch(body: CreateEmailBatchRequest, ctx: Context) -> EmailBatchResponse:
    """Validate everything, then write the batch, its rows and its drafting job.

    All-or-nothing on purpose: a partial accept would leave an operator
    reconciling which of their 5 000 rows made it.
    """
    errors: list[str] = []
    pool = await ctx.tenant_pool()
    task_config = await _require_task_config(pool, ctx.tenant.id, body.task_id)
    if task_config is None:
        # Two different problems and two different next actions, so they are two
        # different sentences: a batch drafts with the published definition, and
        # scheduling one against a task nobody has approved is how five thousand
        # rows get drafted by a half-written prompt.
        name = await pool.fetchval(
            "SELECT name FROM agent_tasks WHERE id = $1 AND tenant_id = $2",
            body.task_id,
            ctx.tenant.id,
        )
        errors.append(
            f"publish '{name}' before drafting a batch with it"
            if name
            else "that task does not exist"
        )

    inputs = [dict(r.input) for r in body.recipients]
    input_columns = _csv_columns(inputs, errors)
    if task_config is not None:
        _check_columns_against_task(input_columns, task_config, errors)
        errors.extend(check_field_map(body.field_map, input_columns, task_config))
    validate_start_at(body.start_at, errors)
    await check_sender(ctx.tenant, body.integration_id, body.from_email, errors)
    if body.reply_to and not is_email_address(body.reply_to):
        errors.append(f"reply_to {body.reply_to!r} is not an email address")
    if errors:
        raise validation_error(errors, "invalid email batch")

    window = body.window
    scheduled_at = body.start_at or datetime.now(UTC)
    async with pool.acquire() as conn:
        async with conn.transaction():
            batch_id = await conn.fetchval(
                """
                INSERT INTO email_batches (
                    tenant_id, name, task_id, integration_id, from_email, from_name, reply_to,
                    body_format, field_map, input_columns, status,
                    start_at, timezone, window_start_local, window_end_local, window_days,
                    draft_concurrency, draft_attempts, draft_retry_after_minutes,
                    draft_gap_seconds,
                    send_attempts, send_retry_after_minutes, send_gap_seconds,
                    total_recipients, created_by_user_id,
                    send_daily_cap, send_ramp_start, send_ramp_end, send_ramp_step,
                    send_ramp_interval_days,
                    -- A batch scheduled for Monday must say so from the moment
                    -- it exists, not from whenever its loop first runs. Null
                    -- when it starts now: there is nothing to wait for.
                    next_draft_at, next_draft_reason
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7,
                        $8, $9::jsonb, $10, 'scheduled',
                        $11, $12, $13, $14, $15,
                        $16, $17, $18, $19,
                        $20, $21, $22,
                        $23, $24,
                        $25, $26, $27, $28, $29,
                        $11, CASE WHEN $11::timestamptz IS NULL THEN NULL ELSE 'start' END)
                RETURNING id
                """,
                ctx.tenant.id,
                body.name,
                body.task_id,
                body.integration_id,
                body.from_email.strip(),
                body.from_name,
                body.reply_to,
                body.body_format,
                body.field_map.model_dump_json(),
                input_columns,
                body.start_at,
                body.timezone,
                window.start if window else None,
                window.end if window else None,
                window.days if window else [1, 2, 3, 4, 5, 6, 7],
                body.draft_concurrency,
                body.draft_attempts,
                body.draft_retry_after_minutes,
                body.draft_gap_seconds,
                body.send_attempts,
                body.send_retry_after_minutes,
                body.send_gap_seconds,
                len(inputs),
                ctx.user.id,
                *cap_columns(body.send_daily_cap),
            )
            await _insert_rows(conn, ctx.tenant.id, batch_id, inputs, first_row=1)
            batch = await load_batch(conn, ctx.tenant.id, batch_id)
            assert batch is not None
            # Before a single row is drafted, and in the same transaction: a row
            # whose address column is empty and which the task cannot fill is
            # skipped now, at no cost — the moment the operator can still go
            # back to their source for it. Later is after it has been paid for.
            await reconcile_drafts(conn, ctx.tenant.id, batch)
            # In the same transaction as the rows: a batch that exists with no
            # job behind it is a list that silently never drafts.
            await jobs.enqueue_once(
                conn,
                tenant_id=ctx.tenant.id,
                kind=jobs.JobKind.EMAIL_BATCH_DRAFT,
                scheduled_at=scheduled_at,
                subject_id=batch_id,
                args={"batch_id": str(batch_id)},
            )
    logger.info("created email batch %s (%d rows, task %s)", batch.id, len(inputs), body.task_id)
    return await batch_response(ctx.tenant, batch)


def _stored_input(row: dict[str, str]) -> dict[str, str]:
    """An uploaded row as it is stored: empty cells dropped rather than kept as "".

    A required variable then reads as absent and fails that ONE row with
    `missing_vars` before a token is spent, instead of reaching the model as an
    empty string and producing plausible nonsense.
    """
    return {k: v for k, v in row.items() if v.strip()}


async def _insert_rows(
    conn: asyncpg.Connection,
    tenant_id: UUID,
    batch_id: UUID,
    rows: list[dict[str, str]],
    *,
    first_row: int,
) -> dict[int, UUID]:
    """Store uploaded rows, numbered from `first_row`, in one round trip.

    ``{row_number: id}`` for every row stored.
    """
    inserted = await conn.fetch(
        """
        INSERT INTO email_batch_recipients (tenant_id, batch_id, row_number, input)
        SELECT $1, $2, r.row_number, r.input::jsonb
        FROM unnest($3::int[], $4::text[]) AS r(row_number, input)
        RETURNING id, row_number
        """,
        tenant_id,
        batch_id,
        list(range(first_row, first_row + len(rows))),
        [json.dumps(_stored_input(row)) for row in rows],
    )
    return {r["row_number"]: r["id"] for r in inserted}


async def add_recipients(
    batch_id: UUID, body: AddEmailRecipientsRequest, ctx: Context
) -> AddEmailRecipientsResponse:
    """Append rows to a batch, at any point before it is canceled or failed.

    **Duplicates are dropped and reported, before anything is drafted**, so a
    re-pushed CRM export costs nothing. With an uploaded `to` column a duplicate
    is a row whose address the batch or this upload already has; when the task
    produces `to`, it is a row whose uploaded cells repeat one exactly. The
    send-time one-address rule stays the guarantee that nobody is mailed twice.

    **A drafted batch is idle, not over**: new rows to draft wake it. And when
    the batch's most recent send is a send of every row that finished on its
    own, the new rows reopen it — they are rows it would have covered had they
    arrived a day earlier. Otherwise they are drafted and wait for review.

    Under the batch row's lock from the first statement to the commit: the
    drafting and sending loops ask "is anything left" under the same lock, so
    rows never land on a batch that has just finished without them.
    """
    inputs = [dict(r.input) for r in body.recipients]
    errors: list[str] = []
    upload_columns = _csv_columns(inputs, errors)
    pool = await ctx.tenant_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                f"SELECT {BATCH_COLUMNS} FROM email_batches "
                "WHERE id = $1 AND tenant_id = $2 FOR UPDATE",
                batch_id,
                ctx.tenant.id,
            )
            if row is None:
                raise HTTPException(status_code=404, detail="email batch not found")
            batch = _batch_row(row)
            if batch.status in ("canceled", "failed"):
                raise HTTPException(
                    status_code=409,
                    detail=f"this batch is {batch.status}, so rows cannot be added to it",
                )
            task_config = (
                await _require_task_config(conn, ctx.tenant.id, batch.task_id)
                if batch.task_id
                else None
            )
            if task_config is None:
                raise HTTPException(
                    status_code=409,
                    detail="this batch's task has been deleted, so it cannot draft new rows",
                )
            columns = batch.input_columns + [
                c for c in upload_columns if c not in batch.input_columns
            ]
            _check_columns_against_task(columns, task_config, errors)
            if errors:
                raise validation_error(errors, "invalid recipients")

            skipped: list[SkippedEmailRecipient] = []
            kept: list[tuple[int, dict[str, str]]] = []
            to_column = batch.field_map.to
            if to_column in columns:
                # The address is uploaded, so it is known now. Blank is never a
                # duplicate: `reconcile_drafts` makes that row `unfillable`.
                addresses = {
                    position: row.get(to_column, "").strip().lower()
                    for position, row in enumerate(inputs, start=1)
                }
                already = {
                    r["address"]
                    for r in await conn.fetch(
                        """
                        SELECT lower(btrim(coalesce(
                            r.to_email, r.overrides->>$3, r.output->>$3, r.input->>$3
                        ))) AS address
                        FROM email_batch_recipients r
                        WHERE r.tenant_id = $1 AND r.batch_id = $2
                          AND lower(btrim(coalesce(
                              r.to_email, r.overrides->>$3, r.output->>$3, r.input->>$3
                          ))) = ANY($4::text[])
                        """,
                        ctx.tenant.id,
                        batch_id,
                        to_column,
                        [a for a in addresses.values() if a],
                    )
                }
                first_seen: dict[str, int] = {}
                for position, row in enumerate(inputs, start=1):
                    address = addresses[position]
                    if address and address in already:
                        reason = "already in this batch"
                    elif address and address in first_seen:
                        reason = f"same address as row {first_seen[address]}"
                    else:
                        if address:
                            first_seen[address] = position
                        kept.append((position, row))
                        continue
                    skipped.append(
                        SkippedEmailRecipient(row_number=position, reason=reason, recipient_id=None)
                    )
            else:
                # The task finds the address, so the only duplicate knowable now
                # is a row whose uploaded cells repeat one exactly, as stored.
                stored = {
                    position: json.dumps(_stored_input(row), sort_keys=True)
                    for position, row in enumerate(inputs, start=1)
                }
                already = {
                    json.dumps(r["input"], sort_keys=True)
                    for r in await conn.fetch(
                        """
                        SELECT r.input FROM email_batch_recipients r
                        WHERE r.tenant_id = $1 AND r.batch_id = $2
                          AND r.input = ANY(SELECT x::jsonb FROM unnest($3::text[]) AS x)
                        """,
                        ctx.tenant.id,
                        batch_id,
                        list(set(stored.values())),
                    )
                }
                first_seen = {}
                for position, row in enumerate(inputs, start=1):
                    key = stored[position]
                    if key in already:
                        reason = "already in this batch"
                    elif key in first_seen:
                        reason = f"same cells as row {first_seen[key]}"
                    else:
                        first_seen[key] = position
                        kept.append((position, row))
                        continue
                    skipped.append(
                        SkippedEmailRecipient(row_number=position, reason=reason, recipient_id=None)
                    )

            next_row = await conn.fetchval(
                "SELECT COALESCE(MAX(row_number), 0) + 1 FROM email_batch_recipients "
                "WHERE tenant_id = $1 AND batch_id = $2",
                ctx.tenant.id,
                batch_id,
            )
            stored_ids = await _insert_rows(
                conn, ctx.tenant.id, batch_id, [row for _, row in kept], first_row=next_row
            )
            # Request position of each stored row, which is what a caller reads.
            position_of = {
                stored_ids[next_row + i]: position for i, (position, _) in enumerate(kept)
            }
            updated = await conn.fetchrow(
                f"""
                UPDATE email_batches
                SET input_columns = $3, total_recipients = total_recipients + $4,
                    updated_at = now()
                WHERE id = $1 AND tenant_id = $2
                RETURNING {BATCH_COLUMNS}
                """,
                batch_id,
                ctx.tenant.id,
                columns,
                len(kept),
            )
            batch = _batch_row(updated)
            # At no cost, before anything is drafted: a row whose mapped column
            # the task never writes is blank is skipped now, while the operator
            # can still go back to their source for it.
            settled = await reconcile_drafts(conn, ctx.tenant.id, batch, list(position_of))
            # New rows are `pending`, so the only move open to them is `unfillable`.
            assert set(settled.values()) <= {"skipped"}, settled
            if settled:
                for r in await conn.fetch(
                    "SELECT id, last_error FROM email_batch_recipients "
                    "WHERE tenant_id = $1 AND id = ANY($2::uuid[])",
                    ctx.tenant.id,
                    list(settled),
                ):
                    skipped.append(
                        SkippedEmailRecipient(
                            row_number=position_of[r["id"]],
                            reason=r["last_error"],
                            recipient_id=r["id"],
                        )
                    )
            skipped.sort(key=lambda s: s.row_number)

            standing_send_id: UUID | None = None
            if len(settled) < len(position_of):
                if batch.status == "drafted":
                    await _revive_drafted(conn, ctx.tenant.id, batch_id)
                standing_send_id = await _standing_send_for_new_rows(conn, ctx.tenant.id, batch_id)
    logger.info(
        "email batch %s: %d row(s) added, %d skipped%s",
        batch_id,
        len(kept),
        len(skipped),
        f", sent by {standing_send_id}" if standing_send_id else "",
    )
    return AddEmailRecipientsResponse(
        added=len(kept),
        skipped=skipped,
        total_recipients=batch.total_recipients,
        standing_send_id=standing_send_id,
    )


async def _standing_send_for_new_rows(
    conn: asyncpg.Connection, tenant_id: UUID, batch_id: UUID
) -> UUID | None:
    """The send of every row that will mail newly added drafts, or ``None``.

    A live one covers them already. Otherwise the batch's MOST RECENT send, if it
    is a send of every row that finished on its own, is reopened — no send was
    created after it, so nothing else can be live beside it. A send someone
    canceled, one that failed, or one of selected rows is left alone, and the new
    rows wait for review. Called under the batch row's lock; the run row is
    locked second, the order `send._finish` takes them in.
    """
    latest = await conn.fetchrow(
        """
        SELECT id, scope, status FROM email_send_runs
        WHERE tenant_id = $1 AND batch_id = $2
        ORDER BY created_at DESC
        LIMIT 1
        FOR UPDATE
        """,
        tenant_id,
        batch_id,
    )
    if latest is None or latest["scope"] != "all":
        return None
    if latest["status"] in LIVE_SEND_STATUSES:
        return latest["id"]
    if latest["status"] != "sent":
        return None
    # `started_at` is kept, so `email_batch.send.started` does not fire again.
    await conn.execute(
        """
        UPDATE email_send_runs
        SET status = 'sending', finished_at = NULL,
            next_send_at = NULL, next_send_reason = NULL, updated_at = now()
        WHERE id = $1 AND tenant_id = $2 AND status = 'sent'
        """,
        latest["id"],
        tenant_id,
    )
    await jobs.enqueue_once(
        conn,
        tenant_id=tenant_id,
        kind=jobs.JobKind.EMAIL_SEND,
        scheduled_at=datetime.now(UTC),
        subject_id=latest["id"],
        args={"send_run_id": str(latest["id"])},
    )
    logger.info("email send %s: reopened by rows added to batch %s", latest["id"], batch_id)
    return latest["id"]


# ── policy writes ───────────────────────────────────────────────────────────


async def _anything_sent(conn, tenant_id: UUID, batch_id: UUID) -> bool:
    """Has this batch mailed anyone, or is a send about to?"""
    return bool(
        await conn.fetchval(
            """
            SELECT EXISTS (
                SELECT 1 FROM email_batch_recipients
                WHERE tenant_id = $1 AND batch_id = $2
                  AND status IN ('sending', 'sent', 'send_failed')
            ) OR EXISTS (
                SELECT 1 FROM email_send_runs
                WHERE tenant_id = $1 AND batch_id = $2
                  AND status IN ('scheduled', 'sending', 'paused')
            )
            """,
            tenant_id,
            batch_id,
        )
    )


async def patch_batch(
    batch_id: UUID, body: PatchEmailBatchRequest, ctx: Context
) -> EmailBatchResponse:
    """Edit policy. A live batch picks it up within `recheck_seconds`.

    Pacing — both groups, the window and the daily cap — stays editable at any
    time: it is exactly what an operator watching a batch wants to change.
    Identity — the sender, the body format and the field map — is frozen once a
    send exists or anything has gone, because a batch that changed those halfway
    would be two batches with one name.

    **The sending half here is the default for the NEXT send.** A send already
    created carries its own copy of all four values, and `patch_send` is what
    re-steers that one. Editing a batch mid-send and watching nothing change
    would otherwise be the obvious reading.

    `start_at` is the one field that also moves the JOB, and only while the batch
    is still `scheduled` — that is the case the scheduler owns. Everything else a
    live batch re-reads for itself.
    """
    batch = await _require_batch(ctx, batch_id)
    if batch.status not in _EDITABLE_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=f"this batch is {batch.status} and can no longer be edited",
        )
    sent = body.model_fields_set
    errors: list[str] = []
    pool = await ctx.tenant_pool()

    identity_fields = sorted(
        {"from_email", "from_name", "reply_to", "body_format", "field_map"} & sent
    )
    if identity_fields and await _anything_sent(pool, ctx.tenant.id, batch_id):
        errors.append(
            f"{', '.join(identity_fields)} cannot change once this batch has a send or has "
            "sent anything - the rows delivered would no longer describe what they were sent as"
        )
    if "from_email" in sent and body.from_email is not None:
        await check_sender(ctx.tenant, batch.integration_id, body.from_email, errors)
    if "reply_to" in sent and body.reply_to and not is_email_address(body.reply_to):
        errors.append(f"reply_to {body.reply_to!r} is not an email address")
    if "field_map" in sent and body.field_map is not None:
        task_config = (
            await _require_task_config(pool, ctx.tenant.id, batch.task_id)
            if batch.task_id
            else None
        )
        if task_config is None:
            errors.append("this batch's task has been deleted, so its field map cannot be changed")
        else:
            errors.extend(check_field_map(body.field_map, batch.input_columns, task_config))
    if "start_at" in sent:
        if batch.status != "scheduled":
            errors.append("start_at can only be changed while the batch is still scheduled")
        else:
            validate_start_at(body.start_at, errors)
    # An explicit `null` on a field that has no "unset" meaning is a mistake, and
    # a silent no-op would hide it until somebody wondered why their edit did
    # nothing. The five fields that DO take null are listed once, beside the
    # request model that documents them.
    for field in sorted(sent - _NULL_MEANS_NOTHING):
        if getattr(body, field) is None:
            errors.append(f"{field} cannot be null")
    if errors:
        raise validation_error(errors, "invalid email batch")

    window = body.window if "window" in sent else batch.window
    field_map = body.field_map if body.field_map is not None else batch.field_map
    start_at = body.start_at if "start_at" in sent else batch.start_at
    daily_cap = body.send_daily_cap if "send_daily_cap" in sent else batch.daily_cap
    # An edited ramp keeps its progress unless its start moved; a new one starts
    # from the sending days this batch has completed so far.
    ramp_base = ramp_base_days(
        daily_cap,
        batch.daily_cap,
        old_base=batch.send_ramp_base_days,
        completed=completed_days(
            batch.send_days, batch.last_send_day, local_today(datetime.now(UTC), batch.timezone)
        ),
    )

    def keep(field: str) -> Any:
        """Absent means unchanged; present means exactly what was sent.

        `body.field or batch.field` is what this replaces, and **it would now be
        a live bug rather than a latent one**: `draft_gap_seconds` may be 0, so
        `or` would read a deliberate "no wait between rows" as "not sent" and
        silently keep the old value. It was always wrong on principle — a falsy
        value is not an absent one — and it used to be safe only by the accident
        that every numeric field had `ge=1`. `_NULL_MEANS_NOTHING` above keeps the
        `null` case out of this function entirely.
        """
        return getattr(body, field) if field in sent else getattr(batch, field)

    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                f"""
                UPDATE email_batches
                SET name = $3,
                    -- While the batch is `scheduled` its start time IS when it
                    -- next drafts, so the two move together. Once it is
                    -- `drafting` the loop owns these columns.
                    next_draft_at = CASE WHEN status = 'scheduled' THEN $9 ELSE next_draft_at END,
                    next_draft_reason = CASE
                        WHEN status <> 'scheduled' THEN next_draft_reason
                        WHEN $9::timestamptz IS NULL THEN NULL
                        ELSE 'start'
                    END,
                    from_email = $4,
                    from_name = $5,
                    reply_to = $6,
                    body_format = $7,
                    field_map = $8::jsonb,
                    start_at = $9,
                    timezone = $10,
                    window_start_local = $11,
                    window_end_local = $12,
                    window_days = $13,
                    draft_concurrency = $14,
                    draft_attempts = $15,
                    draft_retry_after_minutes = $16,
                    draft_gap_seconds = $17,
                    send_attempts = $18,
                    send_retry_after_minutes = $19,
                    send_gap_seconds = $20,
                    send_daily_cap = $21,
                    send_ramp_start = $22,
                    send_ramp_end = $23,
                    send_ramp_step = $24,
                    send_ramp_interval_days = $25,
                    send_ramp_base_days = $26,
                    updated_at = now()
                WHERE id = $1 AND tenant_id = $2
                RETURNING {BATCH_COLUMNS}
                """,
                batch_id,
                ctx.tenant.id,
                keep("name"),
                str(keep("from_email")).strip(),
                keep("from_name"),
                keep("reply_to"),
                keep("body_format"),
                field_map.model_dump_json(),
                start_at,
                keep("timezone"),
                window.start if window else None,
                window.end if window else None,
                window.days if window else [1, 2, 3, 4, 5, 6, 7],
                keep("draft_concurrency"),
                keep("draft_attempts"),
                keep("draft_retry_after_minutes"),
                keep("draft_gap_seconds"),
                keep("send_attempts"),
                keep("send_retry_after_minutes"),
                keep("send_gap_seconds"),
                *cap_columns(daily_cap),
                ramp_base,
            )
            assert row is not None
            fresh = _batch_row(row)
            if "field_map" in sent:
                # A new mapping re-decides every reviewable row at once: the
                # column that now holds `to` may be filled on rows that were
                # unsendable and empty on rows that were fine. The whole batch,
                # but this is a rare deliberate edit.
                await reconcile_drafts(conn, ctx.tenant.id, fresh)
            if "start_at" in sent and fresh.status == "scheduled":
                # The job IS the schedule. Moving `start_at` without moving the
                # job would leave a batch that says one thing and does another.
                await conn.execute(
                    """
                    UPDATE scheduled_jobs SET scheduled_at = $3, updated_at = now()
                    WHERE tenant_id = $1 AND subject_id = $2
                      AND kind = $4 AND status = 'pending'
                    """,
                    ctx.tenant.id,
                    batch_id,
                    start_at or datetime.now(UTC),
                    str(jobs.JobKind.EMAIL_BATCH_DRAFT),
                )
    return await batch_response(ctx.tenant, fresh)


async def _transition(
    ctx: Context, batch_id: UUID, *, sql: str, allowed: tuple[str, ...], verb: str
) -> EmailBatch:
    """One guarded compare-and-set on the policy row, or a 409 naming the status."""
    pool = await ctx.tenant_pool()
    row = await pool.fetchrow(sql, batch_id, ctx.tenant.id)
    if row is None:
        batch = await _require_batch(ctx, batch_id)
        raise HTTPException(
            status_code=409,
            detail=f"a {batch.status} batch cannot be {verb} (only {', '.join(allowed)} can be)",
        )
    return _batch_row(row)


async def pause_batch(batch_id: UUID, ctx: Context) -> EmailBatchResponse:
    """Stop claiming new rows. Runs already in flight finish.

    The loop notices on its next turn — at most one `draft_gap_seconds` or one
    `recheck_seconds` away — and **completes its job rather than deferring**:
    nothing in this feature stays alive across a human decision, so a paused
    batch has no scheduled job at all until somebody resumes it.

    Also used by the drafting breaker, which is why `resume` clears
    `failure_reason` and the counter: there, resume IS the reset. Pausing a
    `drafted` batch makes rows added later wait for a resume.
    """
    batch = await _transition(
        ctx,
        batch_id,
        sql=f"""
        UPDATE email_batches
        SET status = 'paused', next_draft_at = NULL, next_draft_reason = NULL, updated_at = now()
        WHERE id = $1 AND tenant_id = $2 AND status IN ('scheduled', 'drafting', 'drafted')
        RETURNING {BATCH_COLUMNS}
        """,
        allowed=("scheduled", "drafting", "drafted"),
        verb="paused",
    )
    return await batch_response(ctx.tenant, batch)


async def resume_batch(batch_id: UUID, ctx: Context) -> EmailBatchResponse:
    """Undo a pause, and give the batch a drafting job again.

    Back to `scheduled` rather than `drafting` when the start time is still in
    the future — pausing and resuming a batch that has not begun must not be a
    way to make it begin early.
    """
    pool = await ctx.tenant_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                f"""
                UPDATE email_batches
                SET status = CASE
                        WHEN start_at IS NOT NULL AND start_at > now() THEN 'scheduled'
                        ELSE 'drafting'
                    END,
                    -- A resumed batch drafts as soon as its job is claimed, so
                    -- there is nothing to announce unless its start time is
                    -- still ahead. Clearing the counter and the reason is what
                    -- makes resume the reset a breaker pause needs.
                    next_draft_at = CASE
                        WHEN start_at IS NOT NULL AND start_at > now() THEN start_at ELSE NULL
                    END,
                    next_draft_reason = CASE
                        WHEN start_at IS NOT NULL AND start_at > now() THEN 'start' ELSE NULL
                    END,
                    failure_reason = NULL,
                    consecutive_draft_failures = 0,
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
            fresh = _batch_row(row)
            await _enqueue_draft_job(conn, fresh)
    return await batch_response(ctx.tenant, fresh)


async def cancel_batch(batch_id: UUID, ctx: Context) -> EmailBatchResponse:
    """Stop drafting for good — one row, whatever the batch's size.

    Deliberately does not touch a single recipient: rows already drafted stay
    `draft` and are still worth reviewing and sending, and rows never reached are
    reported `canceled` on read rather than rewritten. Runs already in flight
    finish; the next pass sees the cancel and completes its job without
    deferring.
    """
    batch = await _transition(
        ctx,
        batch_id,
        sql=f"""
        UPDATE email_batches
        SET status = 'canceled', next_draft_at = NULL, next_draft_reason = NULL, updated_at = now()
        WHERE id = $1 AND tenant_id = $2 AND status IN ('scheduled', 'drafting', 'paused')
        RETURNING {BATCH_COLUMNS}
        """,
        allowed=("scheduled", "drafting", "paused"),
        verb="canceled",
    )
    return await batch_response(ctx.tenant, batch)


async def delete_batch(batch_id: UUID, ctx: Context) -> None:
    """Delete a batch that is no longer drafting or sending, with its rows and sends."""
    await _require_batch(ctx, batch_id)
    pool = await ctx.tenant_pool()
    deleted = await pool.fetchval(
        """
        DELETE FROM email_batches b
        WHERE b.id = $1 AND b.tenant_id = $2 AND b.status IN ('drafted', 'canceled', 'failed')
          AND NOT EXISTS (
            SELECT 1 FROM email_send_runs s
            WHERE s.batch_id = b.id AND s.tenant_id = b.tenant_id AND s.status = ANY($3::text[])
          )
        RETURNING b.id
        """,
        batch_id,
        ctx.tenant.id,
        list(LIVE_SEND_STATUSES),
    )
    if deleted is None:
        batch = await _require_batch(ctx, batch_id)
        raise HTTPException(
            status_code=409,
            detail=(
                "this batch has a send that has not finished; cancel the send first"
                if batch.status in ("drafted", "canceled", "failed")
                else f"a {batch.status} batch cannot be deleted; cancel it first"
            ),
        )
    logger.info("deleted email batch %s", batch_id)


async def _enqueue_draft_job(conn: asyncpg.Connection, batch: EmailBatch) -> None:
    """Give this batch a drafting job — unless it already has one.

    Guarded against duplicates, in the caller's transaction, because `create`,
    `resume` and `redraft` all want one and three presses in quick succession
    would otherwise mean three loops claiming rows from one batch and triple its
    `draft_concurrency`. Nothing would be drafted twice — the claim is still a
    compare-and-set naming both the batch and the job — but the one limit its
    owner set would be ignored.
    """
    scheduled_at = (
        batch.start_at if batch.status == "scheduled" and batch.start_at else datetime.now(UTC)
    )
    job_id = await jobs.enqueue_once(
        conn,
        tenant_id=batch.tenant_id,
        kind=jobs.JobKind.EMAIL_BATCH_DRAFT,
        scheduled_at=scheduled_at,
        subject_id=batch.id,
        args={"batch_id": str(batch.id)},
    )
    if job_id is None:
        logger.info("email batch %s already has a drafting job; not enqueuing another", batch.id)


# ── the review verbs ────────────────────────────────────────────────────────


async def reconcile_drafts(
    conn: asyncpg.Connection, tenant_id: UUID, batch: EmailBatch, ids: list[UUID] | None = None
) -> dict[UUID, str]:
    """Put every reviewable row in the status its own cells justify.

    ``{row id: status it moved to}`` for every row it moved.

    **Two invariants, and every count, label and bulk selection in this feature
    rests on them.** `draft` means a draft that can be SENT. And a row that no
    run of the task could ever make sendable — a mapped column the task does not
    write is blank — is not work: it is `skipped` with `skip_reason =
    'unfillable'`, and it never costs a task run. Both are decided by
    ``resolve_send_fields`` and ``unfillable_reason``, never by a second
    implementation in SQL.

    Drafting settles its own rows (see ``draft._run_one``). Everything else that
    can change the answer comes through here: the upload itself, a person editing
    a cell, a person restoring or redrafting a row, and the field map moving.

    ``last_error`` follows a draft that is still unsendable only when the row
    HAS output — exactly the rows whose sentence this function wrote, since
    every drafting failure leaves ``output`` null. So filling in an address on a
    row also missing its subject re-reads "subject is empty", while a row that
    failed with `provider_error: 502` keeps the sentence that says why.

    A row that stops being unfillable goes back to `pending` if it was never
    drafted — it is work now — and a batch that had finished drafting is revived
    to draft it. One already drafted goes back to whichever of `draft` and
    `draft_failed` its cells justify, at no cost.
    """
    output_fields: set[str] | None = None
    if batch.task_id is not None:
        published = await _published_tasks(conn, tenant_id, [batch.task_id])
        if batch.task_id in published:
            output_fields = set(published[batch.task_id][1])

    rows = await conn.fetch(
        f"""
        SELECT {RECIPIENT_COLUMNS} FROM email_batch_recipients
        WHERE tenant_id = $1 AND batch_id = $2
          AND (status IN ('pending', 'draft', 'draft_failed')
               OR (status = 'skipped' AND skip_reason = 'unfillable'))
          AND ($3::uuid[] IS NULL OR id = ANY($3::uuid[]))
        """,
        tenant_id,
        batch.id,
        ids,
    )
    # (id, status it is in, status it moves to, skip_reason, last_error)
    moves: list[tuple[UUID, str, str, str | None, str | None]] = []
    for row in rows:
        recipient = _recipient_row(row)
        # Unknowable once the task is gone — and a batch with no task drafts
        # nothing more, so there is no spend left to save.
        gap = (
            unfillable_reason(batch, recipient, output_fields)
            if output_fields is not None
            else None
        )
        target: tuple[str, str | None, str | None]
        if gap is not None:
            target = ("skipped", "unfillable", gap)
        elif recipient.status == "pending":
            continue
        elif recipient.status == "skipped" and recipient.task_run_id is None:
            target = ("pending", None, None)
        else:
            _, reason = resolve_send_fields(batch, recipient)
            if reason is None:
                if recipient.status == "draft":
                    continue
                target = ("draft", None, None)
            elif recipient.status == "draft_failed" and recipient.output is None:
                continue
            else:
                target = ("draft_failed", None, reason)
        if (recipient.status, recipient.skip_reason, recipient.last_error) != target:
            moves.append((recipient.id, recipient.status, *target))
    if not moves:
        return {}

    row_ids, was, status, skip_reason, last_error = (list(column) for column in zip(*moves))
    moved = await conn.fetch(
        """
        UPDATE email_batch_recipients AS r
        SET status = f.status, skip_reason = f.skip_reason, last_error = f.last_error,
            updated_at = now()
        FROM unnest($2::uuid[], $3::text[], $4::text[], $5::text[], $6::text[])
            AS f(id, was, status, skip_reason, last_error)
        -- A compare-and-set per row: a drafting claim may have taken one since
        -- it was read.
        WHERE r.tenant_id = $1 AND r.id = f.id AND r.status = f.was
        RETURNING r.id, r.status
        """,
        tenant_id,
        row_ids,
        was,
        status,
        skip_reason,
        last_error,
    )
    outcome = {r["id"]: r["status"] for r in moved}
    if "pending" in outcome.values():
        await _revive_drafted(conn, tenant_id, batch.id)
    return outcome


async def _revive_drafted(conn: asyncpg.Connection, tenant_id: UUID, batch_id: UUID) -> None:
    """A `drafted` batch has rows to draft again: back to `drafting`, with a job."""
    revived = await conn.fetchrow(
        f"""
        UPDATE email_batches
        SET status = 'drafting', drafted_at = NULL,
            next_draft_at = NULL, next_draft_reason = NULL, updated_at = now()
        WHERE id = $1 AND tenant_id = $2 AND status = 'drafted'
        RETURNING {BATCH_COLUMNS}
        """,
        batch_id,
        tenant_id,
    )
    if revived is not None:
        await _enqueue_draft_job(conn, _batch_row(revived))


async def patch_recipient(
    batch_id: UUID, recipient_id: UUID, body: PatchEmailRecipientRequest, ctx: Context
) -> EmailRecipientResponse:
    """Edit cells on one row.

    Merged over `input ∪ output` at send time and stored apart from both, so
    "what the model wrote" and "what we actually sent" stay separately
    answerable. An empty value clears the override rather than sending an empty
    cell — the way to send nothing is to skip the row.

    Typing an address into a row the task could not find one for is the point of
    the review gate, so the edit and the row's own status move together: this is
    what turns that row back into a sendable draft, and what takes a draft out of
    the sendable set when somebody clears its `to`.

    A draft a live send covers is still editable — the send re-resolves the row
    when it claims it, so it goes out as corrected. Refused once a send has
    claimed the row: what has left cannot be recalled, and an edit to it would
    describe an email that does not exist.
    """
    batch = await _require_batch(ctx, batch_id)
    for name in body.overrides:
        try:
            validate_var_name(name)
        except ValueError as exc:
            raise validation_error([str(exc)], "invalid column name") from None
    pool = await ctx.tenant_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                f"""
                UPDATE email_batch_recipients
                SET overrides = jsonb_strip_nulls(coalesce(overrides, '{{}}'::jsonb) || $4::jsonb),
                    updated_at = now()
                WHERE id = $1 AND tenant_id = $2 AND batch_id = $3
                  AND status IN ('pending', 'drafting', 'draft', 'draft_failed', 'skipped')
                RETURNING {RECIPIENT_COLUMNS}
                """,
                recipient_id,
                ctx.tenant.id,
                batch_id,
                # A blank value becomes SQL NULL so `jsonb_strip_nulls` removes
                # the key entirely, which is what "clear this edit" has to mean —
                # leaving "" in the bag would shadow the model's value with an
                # empty string forever.
                json.dumps({k: (v if v.strip() else None) for k, v in body.overrides.items()}),
            )
            if row is None:
                existing = await conn.fetchval(
                    "SELECT status FROM email_batch_recipients "
                    "WHERE id = $1 AND tenant_id = $2 AND batch_id = $3",
                    recipient_id,
                    ctx.tenant.id,
                    batch_id,
                )
                if existing is None:
                    raise HTTPException(status_code=404, detail="that row is not in this batch")
                raise HTTPException(
                    status_code=409,
                    detail=f"this row is {existing} and can no longer be edited",
                )
            # The edit may have just made this row sendable, or just broken it.
            # In the same transaction, so a reader never sees the cell and the
            # status disagree.
            await reconcile_drafts(conn, ctx.tenant.id, batch, [recipient_id])
            row = await conn.fetchrow(
                f"""
                SELECT {RECIPIENT_COLUMNS_R},
                       r.status = 'draft' AND ($3::boolean OR {_IN_LIVE_SELECTED_SEND}) AS covered
                FROM email_batch_recipients r
                WHERE r.id = $1 AND r.tenant_id = $2
                """,
                recipient_id,
                ctx.tenant.id,
                await _standing_send(conn, ctx.tenant.id, batch_id),
            )
            assert row is not None
    recipient = _recipient_row(row)
    task_version = (
        await pool.fetchval(
            "SELECT task_version FROM task_runs WHERE id = $1 AND tenant_id = $2",
            recipient.task_run_id,
            ctx.tenant.id,
        )
        if recipient.task_run_id
        else None
    )
    return _recipient_out(batch, recipient, task_version, row["covered"])


async def act_on_recipients(
    batch_id: UUID, verb: str, body: SelectRecipientsRequest, ctx: Context
) -> RecipientActionResponse:
    """`skip` or `restore`, over named ids or everything eligible.

    Both take a bulk shorthand while creating a send is the one verb that asks
    for confirmation, and the asymmetry is the point: the rule is about
    irreversibility, not about bulk. Skipping four thousand rows is undone by
    restoring them; mailing four thousand strangers is undone by nothing.

    A restored row goes back to `pending` if it was never drafted — there is no
    draft to restore it to — and otherwise to whichever of `draft` and
    `draft_failed` its cells justify.
    """
    from_statuses, wrong_state = _SELECT_VERBS[verb]
    if body.recipient_ids is None and body.selection is None:
        raise validation_error(
            ["send either `recipient_ids` or `selection`"], "nothing was selected"
        )
    restoring = verb == "restore"
    batch = await _require_batch(ctx, batch_id)
    pool = await ctx.tenant_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            moved = await conn.fetch(
                """
                UPDATE email_batch_recipients
                SET status = CASE
                        WHEN NOT $5 THEN 'skipped'
                        WHEN task_run_id IS NULL THEN 'pending'
                        ELSE 'draft'
                    END,
                    skip_reason = CASE WHEN $5 THEN NULL ELSE 'operator' END,
                    updated_at = now()
                WHERE tenant_id = $1 AND batch_id = $2 AND status = ANY($4::text[])
                  AND ($3::uuid[] IS NULL OR id = ANY($3::uuid[]))
                  -- Restoring an `unfillable` row re-runs the rule that skipped
                  -- it. The bulk form restores only what a person skipped: a
                  -- duplicate the send found stays down unless named.
                  AND NOT ($5 AND skip_reason = 'unfillable')
                  AND NOT ($5 AND $3::uuid[] IS NULL AND skip_reason <> 'operator')
                RETURNING id
                """,
                ctx.tenant.id,
                batch_id,
                body.recipient_ids,
                list(from_statuses),
                restoring,
            )
            moved_ids = {r["id"] for r in moved}
            if restoring and moved_ids:
                # Skipping a row does not change what it holds, so `skip` needs
                # nothing here.
                await reconcile_drafts(conn, ctx.tenant.id, batch, list(moved_ids))
            rejected = await _rejections(
                conn,
                ctx.tenant.id,
                batch_id,
                [i for i in body.recipient_ids or [] if i not in moved_ids],
                wrong_state,
            )
    return RecipientActionResponse(affected=len(moved_ids), rejected=rejected)


async def _redraft_ids(
    conn, tenant_id: UUID, batch: EmailBatch, body: RedraftRequest
) -> tuple[list[UUID], bool]:
    """``(ids to act on, whether rows outside REDRAFTABLE were asked for)``.

    The second value is what separates `all` from `not_sent`: both move the same
    rows, and only `all` reports the ones it would not touch. A bulk press that
    quietly does less than its label is exactly what `rejected` exists to
    prevent.
    """
    if body.recipient_ids is not None:
        return body.recipient_ids, True
    if body.selection is None:
        raise validation_error(
            ["send either `recipient_ids` or `selection`"], "nothing was selected"
        )
    if body.selection == "all":
        rows = await conn.fetch(
            "SELECT id FROM email_batch_recipients "
            "WHERE tenant_id = $1 AND batch_id = $2 ORDER BY row_number",
            tenant_id,
            batch.id,
        )
        return [r["id"] for r in rows], True
    if body.selection == "stale_version":
        # Every row whose run used a version older than the one published now.
        # `<` rather than `<>`: a row drafted by a NEWER version than the current
        # publish is one the tenant rolled back to, and re-drafting it would
        # quietly undo their rollback.
        rows = await conn.fetch(
            f"""
            SELECT r.id FROM email_batch_recipients r
            JOIN task_runs t ON t.id = r.task_run_id AND t.tenant_id = r.tenant_id
            JOIN agent_tasks k ON k.id = $3 AND k.tenant_id = r.tenant_id
            WHERE r.tenant_id = $1 AND r.batch_id = $2
              AND {_REDRAFTABLE_SQL.format("$4")}
              AND t.task_version IS NOT NULL AND t.task_version < k.published_version
            ORDER BY r.row_number
            """,
            tenant_id,
            batch.id,
            batch.task_id,
            list(REDRAFTABLE),
        )
        return [r["id"] for r in rows], False
    statuses = ["draft_failed"] if body.selection == "failed" else list(REDRAFTABLE)
    rows = await conn.fetch(
        f"""
        SELECT r.id FROM email_batch_recipients r
        WHERE r.tenant_id = $1 AND r.batch_id = $2 AND {_REDRAFTABLE_SQL.format("$3")}
        ORDER BY r.row_number
        """,
        tenant_id,
        batch.id,
        statuses,
    )
    return [r["id"] for r in rows], False


async def redraft_recipients(
    batch_id: UUID, body: RedraftRequest, ctx: Context
) -> RecipientActionResponse:
    """Draft these rows again, and revive the batch so that something does it.

    One verb for "write this row again", whatever put it in the state it is in.
    It replaced a `retry` that only ever moved `draft_failed` rows, because they
    are the same action over a different selection and two endpoints for one
    transition is one more state machine than the feature needs.

    **It discards a draft the tenant has already paid for**, and each row it
    moves will run the task again and be billed again. The API cannot make that
    safe — the dashboard is what puts a row count and a cost in front of the
    destructive form of it — but it can refuse to do it silently, so a row that
    has left or is leaving comes back in `rejected` by name. So does a row no
    draft could ever send (`unfillable`), which is never paid for.

    A draft a live send covers is redrafted like any other, and the send takes
    the new draft once it lands — redrafting a paused send's rows is what pausing
    it is for.

    `overrides` survives unless `clear_overrides`: it is merged last, is never
    written by a job, and is the one thing on the row a person typed themselves.

    Reviving a finished batch is the one transition in this feature that runs
    backwards, and it is deliberate: the alternative is telling an operator that
    their twelve failed rows are unrecoverable because the other 9 988 finished
    first.
    """
    batch = await _require_batch(ctx, batch_id)
    if batch.status == "canceled":
        # Refused rather than performed, because performing it DESTROYS work.
        # The reset clears `output` and `task_run_id`, and a canceled batch never
        # drafts again — `resume` accepts only `paused` — so the rows would read
        # "Canceled" for ever with the paid-for drafts gone and no action able to
        # bring them back. Reopening the batch instead would contradict what
        # cancelling means.
        raise HTTPException(
            status_code=409,
            detail="this batch was canceled, so its rows cannot be drafted again",
        )
    if batch.task_id is None:
        raise HTTPException(
            status_code=409,
            detail="this batch's task has been deleted, so its rows cannot be drafted again",
        )
    pool = await ctx.tenant_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            ids, report_rejections = await _redraft_ids(conn, ctx.tenant.id, batch, body)
            if not ids:
                return RecipientActionResponse(affected=0)
            moved = await conn.fetch(
                f"""
                UPDATE email_batch_recipients AS r
                SET status = 'pending',
                    output = NULL,
                    task_run_id = NULL,
                    -- The budget starts over: the operator has just changed
                    -- something — a blank cell, the task itself — so the count
                    -- from before that change describes a different row.
                    attempts = 0,
                    next_attempt_at = NULL,
                    last_error = NULL,
                    skip_reason = NULL,
                    overrides = CASE WHEN $4 THEN '{{}}'::jsonb ELSE overrides END,
                    updated_at = now()
                WHERE r.tenant_id = $1 AND r.batch_id = $2 AND r.id = ANY($3::uuid[])
                  AND {_REDRAFTABLE_SQL.format("$5")}
                RETURNING r.id
                """,
                ctx.tenant.id,
                batch_id,
                ids,
                body.clear_overrides,
                list(REDRAFTABLE),
            )
            # `clear_overrides` can take away the very cell that made a row
            # fillable, and a row that no draft could send must not be paid for.
            settled = await reconcile_drafts(conn, ctx.tenant.id, batch, [r["id"] for r in moved])
            moved_ids = {r["id"] for r in moved} - {
                i for i, status in settled.items() if status == "skipped"
            }
            rejected = await _rejections(
                conn,
                ctx.tenant.id,
                batch_id,
                [
                    i
                    for i in ids
                    if i not in moved_ids and (report_rejections or settled.get(i) == "skipped")
                ],
                "a row being drafted, being sent or already sent cannot be drafted again",
            )
            if moved_ids:
                row = await conn.fetchrow(
                    f"""
                    UPDATE email_batches
                    SET status = 'drafting', drafted_at = NULL,
                        -- Revived, so it drafts as soon as its job is claimed.
                        next_draft_at = NULL, next_draft_reason = NULL,
                        failure_reason = NULL, consecutive_draft_failures = 0, updated_at = now()
                    WHERE id = $1 AND tenant_id = $2
                      AND status IN ('drafted', 'drafting', 'failed')
                    RETURNING {BATCH_COLUMNS}
                    """,
                    batch_id,
                    ctx.tenant.id,
                )
                revived = _batch_row(row) if row else batch
                if revived.status == "drafting":
                    await _enqueue_draft_job(conn, revived)
    logger.info("email batch %s: redrafting %d row(s)", batch_id, len(moved_ids))
    return RecipientActionResponse(affected=len(moved_ids), rejected=rejected)


async def _rejections(
    conn, tenant_id: UUID, batch_id: UUID, ids: list[UUID], reason: str
) -> list[RejectedRecipient]:
    """Name every row that did not move, and why. Never a silent partial accept."""
    if not ids:
        return []
    rows = await conn.fetch(
        """
        SELECT id, row_number, status, skip_reason, last_error FROM email_batch_recipients
        WHERE tenant_id = $1 AND batch_id = $2 AND id = ANY($3::uuid[])
        ORDER BY row_number
        """,
        tenant_id,
        batch_id,
        ids,
    )
    found = {r["id"] for r in rows}
    out = [
        RejectedRecipient(
            recipient_id=r["id"],
            row_number=r["row_number"],
            # The one refusal whose remedy is not in the verb's own sentence.
            reason=(
                f"this row can never be sent: {r['last_error']}, a column the task does not "
                "write. Fill that cell in instead"
                if r["skip_reason"] == "unfillable"
                else f"this row is {r['status']}: {reason}"
            ),
        )
        for r in rows
    ]
    out.extend(
        RejectedRecipient(recipient_id=i, row_number=0, reason="this row is not in this batch")
        for i in ids
        if i not in found
    )
    return out


# ── cross-service guards ────────────────────────────────────────────────────


async def batches_blocking_task_delete(conn, tenant_id: UUID, task_id: UUID) -> list[str]:
    """Names of the live batches that still need this task.

    A batch whose drafting is over does not block: its `task_id` goes null, the
    run history keeps the task's name, and the batch still renders. A batch that
    is still drafting does, because deleting the task under it would fail every
    remaining row with `configuration` rather than telling the person doing the
    deleting.

    `paused` is in the list, which now also covers a batch the drafting breaker
    stopped by itself. That is right: it is resumable, so its task is still
    needed.
    """
    return [
        r["name"]
        for r in await conn.fetch(
            """
            SELECT name FROM email_batches
            WHERE tenant_id = $1 AND task_id = $2
              AND status IN ('scheduled', 'drafting', 'paused')
            ORDER BY created_at
            """,
            tenant_id,
            task_id,
        )
    ]


async def batches_broken_by_task_config(
    conn, tenant_id: UUID, task_id: UUID, cfg: TaskConfig
) -> list[str]:
    """Why this config cannot become what a live batch drafts with, if it cannot.

    The email twin of ``services.agents.service._inbound_number_errors``. A batch
    already scheduled or drafting carries a fixed set of CSV columns and a fixed
    `field_map`, and both are validated at `create_batch` against the task as it
    was published then. Publishing and rolling back are the other two doors into
    the same broken state — a variable this config makes required that the list
    has no column for, or an output field it removes that the mapping points at
    — so both refuse here rather than failing four thousand rows one at a time.
    """
    rows = await conn.fetch(
        """
        SELECT name, input_columns, field_map FROM email_batches
        WHERE tenant_id = $1 AND task_id = $2
          AND status IN ('scheduled', 'drafting', 'paused')
        ORDER BY created_at
        """,
        tenant_id,
        task_id,
    )
    errors: list[str] = []
    for row in rows:
        problems: list[str] = []
        columns = list(row["input_columns"])
        _check_columns_against_task(columns, cfg, problems)
        problems.extend(check_field_map(FieldMap.model_validate(row["field_map"]), columns, cfg))
        errors.extend(f"the email batch {row['name']!r} would break: {p}" for p in problems)
    return errors


__all__ = [
    "REDRAFTABLE",
    "act_on_recipients",
    "add_recipients",
    "batch_response",
    "batches_blocking_task_delete",
    "batches_broken_by_task_config",
    "cancel_batch",
    "check_field_map",
    "check_sender",
    "create_batch",
    "effective_status",
    "get_batch",
    "list_batches",
    "list_recipients",
    "load_batch",
    "patch_batch",
    "patch_recipient",
    "pause_batch",
    "redraft_recipients",
    "resume_batch",
    "validate_start_at",
    "verified_senders",
]
