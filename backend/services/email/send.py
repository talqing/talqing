"""Sending: one long-lived coroutine per send run, holding it to the last email.

**A send is the same shape drafting is.** One `email.send` job per
`email_send_runs` row, `subject_id = send_run_id`, enqueued with `enqueue_once`
so a run never has two.

**What it sends is a scope, read at claim time.** A standing (`all`) send takes
any `draft` of its batch, including rows drafted after it was created; a
`selected` send takes its members. Nothing marks a row as belonging to a send
until the send claims it, which is why a covered draft stays editable up to that
moment — and why the claim re-resolves the row's fields rather than trusting
what they were when the send was created.

**When there is nothing to take, drafting decides whether that means done.**
A standing send created two minutes after an upload has no drafts at all; it
waits (`next_send_reason = 'drafting'`) for as long as drafting has rows left to
write, and finishes only once it has none. Indefinitely, by decision: a send that
quietly stops while the list is still being drafted is the failure this exists
to prevent, and the operator can cancel it. A standing send that has finished is
not over for good either: rows appended to its batch reopen it, when it is the
batch's most recent send (`service.add_recipients`). That is why the finish asks
"nothing left" under the batch row's lock, the one an append holds.

**Ownership splits by status.** A `scheduled` run is the SCHEDULER's: its job is
deferred to `start_at` and nothing is running. A `sending` run is a COROUTINE's:
the job row is `running`, its lease is renewed every 40 s, and this loop is
inside it. It ends when every row has gone or failed, when a human stops it, or
when a gate it cannot pass turns out to be permanent.

**Every gate is re-evaluated on every turn, and that is the whole point.** The
window, the daily cap and the credential used to be checked once per 60-second
pass, so raising a spent cap did nothing until local midnight and removing a
window did nothing until Monday. Now a waiting loop re-reads the world every
`recheck_seconds` and acts within five minutes of any of them changing — with
nothing to signal and nothing to wake.

**Authorization is the one thing asked once**, in `run_send_job`, before the run
is moved to `sending`: a request is authorized when it arrives, not continuously
while it runs. A send already going out finishes the rows it was given.

**Waiting is a chunked sleep, never one long one.** A shut window, a spent cap, a
retry backoff and a `send_gap_seconds` of an hour are all the same three lines:
work out when the gate opens, write it onto the run as `next_send_at` with the
reason, sleep `min(that, recheck_seconds)`, and go round again.

**`next_send_at` lives on the run, not in `scheduled_jobs`.** A waiting loop holds
its job `running` the whole time, so that table's `scheduled_at` stopped being an
answer to "when does this next act" — and it was never the API's table to read.

**There is no ownership lock and no shared rate limiter.** Who owns a row is
decided by `enqueue_once` (one job per run), `store.claim` (one runner per job)
and a compare-and-set on the row that also names both. **The pace and the daily
cap are the BATCH's**, counted inside the claim against the batch's own
`sent_at`s, so two sends of one batch share `send_gap_seconds` rather than each
sending at it — and the claims of one batch take turns for the few milliseconds
each takes, which is the one lock here (see `_claim`). A cap that is a ramp
reads today's limit off the batch's sending days (`services.scheduling.ramp`). Sends of different batches on one Resend account do overlap, and
the only thing that can cost is *rate*: enough overlap exceeds Resend's 10 rps,
Resend answers `429`, and a `429` is its own case — the row waits out the
provider's own `retry-after`, spends no attempt and does not move the breaker. A
breach is self-correcting and costs latency rather than correctness, which is
precisely why a lock was the wrong price.

**Sending is strictly serial and keeps no in-flight bookkeeping.** There is no
`send_concurrency` and there must not be one: with a 1 s floor on the gap and a
sub-second provider call, the knob would have no observable effect beyond "how
much provider latency do I tolerate", which is not a question to put in front of
an operator. Drafting's concurrency is real — a task run takes 30-120 seconds —
and the asymmetry between the two groups is that difference, not an oversight.

**Resend has a native `scheduled_at` on `POST /emails`, and we deliberately do
not use it.** It cannot be paused, cannot respect a business-hours window or a
daily cap, and would put a row in a state our own review table can neither steer
nor explain. Our scheduling is a `scheduled_jobs` row like everything else.

Every row carries `Idempotency-Key: <recipient row id>`, which Resend honours for
**24 hours**. That key is the crash recovery sending has: a row left `sending` by
a container that died is re-queued at the top of the next invocation and re-sent
under the same key, and Resend replays its original answer rather than mailing
anyone twice. Past 24 hours the key has expired and a retry is a real second
email — rare enough (it needs an ambiguous outcome AND a retry a day later) that
it is accepted rather than guarded, deliberately, on 2026-09-16.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime, timedelta
from uuid import UUID

import asyncpg

import db
from services.jobs import JobContext
from services.scheduling import daily_cap_today, local_day_bounds, local_today, window_state
from services.user import ROLE_ADMIN, ROLE_EDITOR, NotAMember, Tenant, load_context
from services.webhooks import events
from settings import get_settings

from .batch.draft import CONTROL_PLANE_RETRY_SECONDS
from .batch.models import (
    RECIPIENT_COLUMNS_R,
    EmailBatch,
    EmailRecipient,
    EmailSendRun,
    resolve_send_fields,
)
from .batch.sends import emit, load_send_run, release_rows
from .batch.service import load_batch
from .credentials import EmailCredentialError, resolve_batch_credential
from .provider import EmailMessage, EmailProvider, SendReceipt, idempotency_key

logger = logging.getLogger("talqing.email.send")

# Ten failures in a row is not bad luck, it is a broken send. The same number
# drafting uses, and the same shape: rolling, reset by any success, and moved
# only by failures that are about the account or the provider. A row Resend
# refuses by address is a fact about that row's data — ten bad addresses in a row
# must not stop a run whose other 4 990 are fine.
SEND_BREAKER_THRESHOLD = 10

# Where to come back when a provider rate-limits us and sends no `retry-after`.
# Seconds, because the limit it describes is per second: Resend allows 10 rps and
# a burst clears in well under a minute. The 30 MINUTES this used to inherit from
# `send_retry_after_minutes` was a transient-failure backoff wearing a rate
# limit's clothes.
_RATE_LIMIT_FALLBACK_SECONDS = 30

# Below this, a gap is not worth a database write or a "Next" line that appears
# and vanishes between two page polls. A one-second pace is the default.
_GAP_WORTH_ANNOUNCING_SECONDS = 60

# How long an email may sit `sending` before the pace stops counting it as in
# flight. Three times the provider client's own timeout: past that, the row is
# stranded by a worker that died, and its send requeues it on its next check.
_IN_FLIGHT_SECONDS = 60

# How often a send that has caught up with drafting looks for new drafts. A row
# takes 30-120 s to draft, so this keeps a standing send within a row or so of
# the drafting behind it, for one small query per send per interval.
_DRAFTING_POLL_SECONDS = 30


async def run_send_job(tenant: Tenant, args: dict, job: JobContext) -> datetime | None:
    """Send this run to the end. Returns ``None``, or `start_at` if too early.

    The one `datetime` it can return is the scheduler-owned case: a `scheduled`
    run whose moment has not come. Every other reason to wait — a shut window, a
    spent cap, a retry, the gap — is slept through inside the loop.
    """
    run_id = UUID(str(args["send_run_id"]))
    pool = await db.tenant_pool(tenant)
    run = await load_send_run(pool, tenant.id, run_id)
    if run is None:
        logger.info("email send %s is gone; nothing to do", run_id)
        return None
    if run.status in ("paused", "canceled", "sent", "failed"):
        logger.info("email send %s is %s; this job is finished", run_id, run.status)
        return None

    if run.status == "scheduled" and run.start_at is not None and run.start_at > datetime.now(UTC):
        # Hand it back to the scheduler rather than parking a claim until Monday.
        # Reachable when somebody moved `start_at` forward without moving the
        # job, and on the first invocation of a send created with a future start.
        await _announce(pool, run, run.start_at, "start")
        return run.start_at

    batch = await load_batch(pool, tenant.id, run.batch_id)
    if batch is None:
        # The batch was deleted under the run. Nothing to resolve a row against.
        logger.warning("email send %s: batch %s is gone", run.id, run.batch_id)
        return None

    # Drafting has always authorized as its creator; sending never did, and
    # sending is the half that mails people from the company's domain. Asked once
    # per invocation, like the HTTP request it stands in for.
    allowed = await _creator_still_allowed(pool, tenant, run)
    if allowed is False:
        return None
    if allowed is None:
        return datetime.now(UTC) + timedelta(seconds=CONTROL_PLANE_RETRY_SECONDS)

    try:
        provider, credential, integration = await resolve_batch_credential(
            pool, tenant, batch.integration_id
        )
    except EmailCredentialError as exc:
        # The account, not this email. Every remaining row fails identically, so
        # the run stops now, hands its rows back, and the handler returns without
        # raising. The BATCH is untouched: a revoked key must not destroy four
        # thousand reviewed drafts.
        await _fail_run(pool, tenant, run, str(exc))
        return None

    run = await _step_start(pool, tenant, run)
    # A row left `sending` belongs to an invocation that is gone: `enqueue_once`
    # allows one job per run and `store.claim` one runner per job, so nothing
    # else can be holding it. Back to `draft` for a re-send under its original
    # idempotency key — which is sending's crash recovery, and is bounded by
    # `send_attempts` so a row that kills its worker cannot loop.
    await _requeue_abandoned(pool, run)

    logger.info(
        "email send %s: sending from %s via %s", run.id, run.from_email, integration.display_name
    )
    await _send_loop(pool, tenant, run, provider, credential, job)
    return None


# ── the loop ────────────────────────────────────────────────────────────────


async def _send_loop(
    pool: asyncpg.Pool,
    tenant: Tenant,
    run: EmailSendRun,
    provider: EmailProvider,
    credential: str,
    job: JobContext,
) -> None:
    """Send rows until the queue runs out or somebody stops this run.

    **One exit, and it is a compare-and-set.** Every path out goes through the
    status re-read at the top: a breaker pause, an account failure, a finish and
    a human's cancel all write the run row and let the next turn notice. That is
    what makes `_hand_back` the single place a live run could be left with no
    owner, and it refuses to be that place.

    **Order matters among the gates.** The window before the cap, because a run
    outside its hours has no business counting today's sends; the cap before the
    claim, in the claim's own statement. Authorization is not among them — it is
    asked once, in `run_send_job`, the way an HTTP request is authorized when it
    arrives rather than continuously while it runs.
    """
    recheck = float(get_settings().email.recheck_seconds)
    # Monotonic, not wall-clock: both are durations this process measured, and a
    # clock step must not collapse a gap or skip a re-check. Starts now because
    # `run_send_job` has just done everything the tick does.
    checked_at = time.monotonic()
    send_allowed_at = 0.0
    while True:
        fresh = await load_send_run(pool, run.tenant_id, run.id)
        if fresh is None or fresh.status != "sending":
            if await _hand_back(pool, job, run):
                return
            # A resume landed between the write that stopped this run and the
            # line above. It is live again and this job still holds it, so carry
            # on rather than leaving covered rows with nothing scheduled.
            continue
        run = fresh

        batch = await load_batch(pool, run.tenant_id, run.batch_id)
        if batch is None:
            logger.warning("email send %s: batch %s is gone", run.id, run.batch_id)
            return

        if time.monotonic() - checked_at >= recheck:
            # A rotated or deleted secret should stop a multi-day send within
            # five minutes rather than at its next email, which may be tomorrow.
            try:
                provider, credential, _ = await resolve_batch_credential(
                    pool, tenant, batch.integration_id
                )
            except EmailCredentialError as exc:
                await _fail_run(pool, tenant, run, str(exc))
                continue
            # Also the safety net for a row left `sending` by a settle that never
            # landed: without it this loop would find nothing claimable and spin
            # at one query a second for ever.
            await _requeue_abandoned(pool, run)
            checked_at = time.monotonic()

        # Evaluated in the RUN's zone, never the server's. The server runs UTC
        # and the list does not, and a `datetime.now()` without a zone anywhere
        # in this feature is a bug that passes every test run in India.
        may_send, opens_at = window_state(
            now=datetime.now(UTC),
            timezone=run.timezone,
            window_start_local=run.window_start_local,
            window_end_local=run.window_end_local,
            window_days=run.window_days,
            subject=f"email send {run.id}",
        )
        if not may_send:
            assert opens_at is not None
            await _announce(pool, run, opens_at, "window")
            await asyncio.sleep(min(_seconds_until(opens_at), recheck))
            continue

        gap_left = send_allowed_at - time.monotonic()
        if gap_left > 0:
            # The gap is honoured across turns rather than slept in one go: at
            # its 3 600 s ceiling a single `sleep` would hold this run deaf to a
            # pause for an hour. Only announced when it is worth a round trip and
            # worth rendering — a one-second gap is neither.
            if gap_left >= _GAP_WORTH_ANNOUNCING_SECONDS:
                await _announce(pool, run, datetime.now(UTC) + timedelta(seconds=gap_left), "gap")
            await asyncio.sleep(min(gap_left, recheck))
            continue

        claimed = await _claim(pool, run, batch, job)
        if claimed is None:
            delay = await _nothing_to_send(pool, tenant, run, batch, job)
            if delay is None:
                continue  # sent, or no longer ours; the re-read exits
            await asyncio.sleep(min(delay, recheck))
            continue
        recipient, fields = claimed
        if fields is None:
            # Settled inside the claim — back to review, or `skipped` as a
            # duplicate: the row's own data, not the run's fault, so no gap and
            # no breaker.
            continue
        await _announce(pool, run, None, None)

        outcome = await _send_one(pool, tenant, run, batch, provider, credential, recipient, fields)
        if outcome == "account":
            continue  # `_fail_run` already ran; the re-read exits
        if outcome == "transient":
            await _breaker_tripped(pool, tenant, run, recipient)
        # Measured from the moment the send settled, because that is when its
        # `sent_at` was written — and the claim, which is the authority, counts
        # the gap from the batch's latest `sent_at`. This timer is what lets a
        # waiting run say `gap` without asking the database every second.
        send_allowed_at = time.monotonic() + run.send_gap_seconds


def _seconds_until(when: datetime) -> float:
    """How far off `when` is, never negative. Callers clamp it to one re-check."""
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


def _covered(run: EmailSendRun) -> tuple[str, UUID]:
    """The rows this send may take, as a `FROM … WHERE` over `r`, and its key.

    `$1` is the tenant and `$2` the key: the batch for a standing send, the send
    itself for a scoped one. **Two statements chosen on scope, never one with an
    `OR`.** The standing one is a plain `batch_id` predicate the partial send
    index answers — no other send can be live beside it, so it needs no
    exclusion join — and the scoped one walks its members by primary key. An
    `OR` across the two access paths would give up both.
    """
    if run.scope == "all":
        return "email_batch_recipients r WHERE r.tenant_id = $1 AND r.batch_id = $2", run.batch_id
    return (
        "email_send_members m "
        "JOIN email_batch_recipients r ON r.id = m.recipient_id AND r.tenant_id = m.tenant_id "
        "WHERE m.tenant_id = $1 AND m.send_run_id = $2",
        run.id,
    )


async def _nothing_to_send(
    pool: asyncpg.Pool, tenant: Tenant, run: EmailSendRun, batch: EmailBatch, job: JobContext
) -> float | None:
    """The claim took nothing. How long to wait — or ``None`` to leave the loop.

    The reasons are not the same thing: every covered row has gone (so the run
    is `sent`), rows it covers are still being drafted, today's allowance is
    spent, every waiting row is in its backoff, another send of this batch sent
    within the gap, or rows are claimable and this container no longer holds
    the job.

    **"Nothing to take" is not "done" while drafting can still produce a row
    this send covers** — for a standing send, any row of a batch still drafting;
    for a scoped one, a member someone redrafted. That wait has no instant to
    name, so it is announced with no `next_send_at`, and it is indefinite.
    """
    covered, key = _covered(run)
    left = await pool.fetchrow(
        f"""
        SELECT
            count(*) FILTER (WHERE r.status = 'draft') AS waiting,
            count(*) FILTER (
                WHERE r.status = 'draft'
                  AND (r.next_attempt_at IS NULL OR r.next_attempt_at <= now())
            ) AS due,
            min(r.next_attempt_at) FILTER (WHERE r.status = 'draft') AS next_due,
            count(*) FILTER (WHERE r.status IN ('pending', 'drafting')) AS unwritten,
            count(*) FILTER (WHERE r.status = 'sending' AND r.send_run_id = $3) AS in_flight
        FROM {covered}
          AND r.status IN ('draft', 'pending', 'drafting', 'sending')
        """,
        run.tenant_id,
        key,
        run.id,
    )
    if not left["waiting"] and not left["in_flight"]:
        if left["unwritten"] and batch.status in ("scheduled", "drafting", "paused"):
            await _announce(pool, run, None, "drafting")
            return _DRAFTING_POLL_SECONDS
        await _finish(pool, tenant, run)
        return None
    if left["waiting"] and await _cap_spent(pool, run, batch):
        _, next_midnight = local_day_bounds(datetime.now(UTC), run.timezone)
        await _announce(pool, run, next_midnight, "daily_cap")
        return _seconds_until(next_midnight)
    if left["waiting"] and not left["due"] and left["next_due"] is not None:
        await _announce(pool, run, left["next_due"], "retry")
        return _seconds_until(left["next_due"])
    if left["due"] and (gap := await _gap_left(pool, run)) > 0:
        # Another send of this batch went out within the gap. The batch's pace
        # is the one that protects the domain, so this send waits its turn.
        if gap >= _GAP_WORTH_ANNOUNCING_SECONDS:
            await _announce(pool, run, datetime.now(UTC) + timedelta(seconds=gap), "gap")
        return gap
    if left["due"] and not await _still_ours(pool, run.tenant_id, job):
        # Rows are claimable and the claim refused them, so the `WHERE` clause
        # that failed was the ownership one: this container's lease lapsed and
        # somebody else has the job. Stop rather than spinning against it.
        logger.warning("email send %s: job %s is no longer ours; stopping", run.id, job.id)
        return None
    # A claimable row was locked for an instant — a person editing it, or a
    # redraft taking it — or one is stranded `sending` by a settle that never
    # landed, which the re-check tick requeues. Working rather than waiting, so
    # nothing is announced.
    return 1.0


async def _gap_left(pool: asyncpg.Pool, run: EmailSendRun) -> float:
    """Seconds until this batch's pace allows another email, never negative.

    Measured the way the claim measures it: from the latest `sent_at`, or from
    now while another email of the batch is in flight.
    """
    left = await pool.fetchval(
        """
        SELECT extract(epoch FROM greatest(
            (SELECT max(sent_at) FROM email_batch_recipients
             WHERE tenant_id = $1 AND batch_id = $2 AND sent_at IS NOT NULL),
            (SELECT now() FROM email_batch_recipients
             WHERE tenant_id = $1 AND batch_id = $2 AND status = 'sending'
               AND updated_at > now() - make_interval(secs => $4::int)
             LIMIT 1)
        ) + make_interval(secs => $3::int) - now())
        """,
        run.tenant_id,
        run.batch_id,
        run.send_gap_seconds,
        _IN_FLIGHT_SECONDS,
    )
    return max(0.0, float(left)) if left is not None else 0.0


async def _still_ours(pool: asyncpg.Pool, tenant_id: UUID, job: JobContext) -> bool:
    return bool(
        await pool.fetchval(
            """
            SELECT EXISTS (
                SELECT 1 FROM scheduled_jobs
                WHERE id = $1 AND tenant_id = $2 AND claimed_by = $3 AND lease_expires_at > now()
            )
            """,
            job.id,
            tenant_id,
            job.worker,
        )
    )


def _cap_today(run: EmailSendRun, batch: EmailBatch) -> int | None:
    """Today's limit for this run: its own cap, read against the BATCH's sending days."""
    return daily_cap_today(
        run.daily_cap,
        base_days=run.send_ramp_base_days,
        days=batch.send_days,
        last_day=batch.last_send_day,
        today=local_today(datetime.now(UTC), batch.timezone),
    )


async def _cap_spent(pool: asyncpg.Pool, run: EmailSendRun, batch: EmailBatch) -> bool:
    """Has this batch used its whole allowance for the run's local day?

    Only asked when the claim came back empty, to tell "capped until midnight"
    apart from "waiting on a retry". The claim itself counts the cap in its own
    transaction, which is where the guarantee lives (see `_claim`).
    """
    cap = _cap_today(run, batch)
    if cap is None:
        return False
    day_start, next_midnight = local_day_bounds(datetime.now(UTC), run.timezone)
    sent_today = await pool.fetchval(
        """
        SELECT count(*) FROM email_batch_recipients
        WHERE tenant_id = $1 AND batch_id = $2 AND sent_at >= $3 AND sent_at < $4
        """,
        run.tenant_id,
        run.batch_id,
        day_start,
        next_midnight,
    )
    return int(sent_today) >= cap


async def _creator_still_allowed(
    pool: asyncpg.Pool, tenant: Tenant, run: EmailSendRun
) -> bool | None:
    """May this send mail people? ``None`` if we could not find out.

    The same authorization check drafting makes, for the same reason and with the same three-way answer: a control-plane query that
    *errors* is transient, and conflating it with "not a member" would let a
    database blip kill live sends. A send it refuses will not start or restart;
    one already sending finishes the rows it was given.

    ``False`` means the run has already been failed with a reason.
    """
    try:
        ctx = await load_context(tenant, run.created_by_user_id)
    except NotAMember:
        await _fail_run(
            pool, tenant, run, "the person who created this send is no longer in this organization"
        )
        return False
    except Exception:
        logger.exception("email send %s: could not load its creator; waiting", run.id)
        return None
    if ctx.user.role not in (ROLE_ADMIN, ROLE_EDITOR):
        await _fail_run(pool, tenant, run, f"{ctx.user.email} no longer has permission to send")
        return False
    return True


async def _step_start(pool: asyncpg.Pool, tenant: Tenant, run: EmailSendRun) -> EmailSendRun:
    """A scheduled send whose moment has come."""
    row = await pool.fetchrow(
        """
        UPDATE email_send_runs
        SET status = 'sending', started_at = COALESCE(started_at, now()),
            next_send_at = NULL, next_send_reason = NULL, updated_at = now()
        WHERE id = $1 AND tenant_id = $2 AND status = 'scheduled'
        RETURNING id
        """,
        run.id,
        run.tenant_id,
    )
    if row is None:
        return run
    fresh = await load_send_run(pool, run.tenant_id, run.id)
    assert fresh is not None
    logger.info("email send %s: started", run.id)
    await emit(tenant, events.EMAIL_SEND_STARTED, fresh)
    return fresh


async def _requeue_abandoned(pool: asyncpg.Pool, run: EmailSendRun) -> None:
    """Put rows a dead pass left `sending` back as drafts, or give up on them.

    Back as a draft the send still covers, with `send_run_id` cleared — no send
    has delivered it — and its `to_email` kept, so the one-address rule stays
    armed for the retry.
    """
    settled = await pool.fetch(
        """
        UPDATE email_batch_recipients
        SET status = CASE WHEN send_attempts < $3 THEN 'draft' ELSE 'send_failed' END,
            send_run_id = CASE WHEN send_attempts < $3 THEN NULL ELSE send_run_id END,
            next_attempt_at = NULL,
            last_error = 'the worker sending this row stopped before it finished',
            updated_at = now()
        WHERE tenant_id = $1 AND send_run_id = $2 AND status = 'sending'
        RETURNING id
        """,
        run.tenant_id,
        run.id,
        run.send_attempts,
    )
    if settled:
        logger.warning("email send %s: recovered %d row(s) whose worker died", run.id, len(settled))


async def _claim(
    pool: asyncpg.Pool, run: EmailSendRun, batch: EmailBatch, job: JobContext
) -> tuple[EmailRecipient, dict[str, str] | None] | None:
    """Take the next row and record where it is going, in one transaction.

    **One query answers five questions:** is there a row this send covers, is
    this run still `sending`, do we still hold this job, does the batch have
    today's allowance left, and has the batch's gap passed. The second is what makes a pause
    take hold on the very next email; the third is what stops two loops existing
    after a lease lapsed under a container that then recovered.

    **The daily cap and the gap are counted in this same statement**, against the
    whole batch. Per-send copies gave two sends of one batch the whole allowance
    each and N sends N times the pace — and "Send this one" creates a send per
    email, so overlapping sends are what ordinary reviewing produces. Two
    indexed reads per email is the price.

    **Which is why a claim holds a lock, and the only one in this feature.** A
    count read in one transaction is stale the instant another commits, and two
    sends of a batch waiting out the same gap wake at the same computed instant:
    measured without it, every gap let a PAIR of emails out, and the batch sent
    at twice its pace. So the claims of one batch take turns — a transaction-
    scoped advisory lock on the batch id, held for the few milliseconds of the
    claim and never across the provider call. It is keyed on the batch rather
    than taken on its row, so no API verb that writes the batch ever waits on it,
    and it serializes nothing but the sends of one batch, which is exactly what
    a batch-wide pace means.

    ``None`` when there is nothing to take, for any of those reasons.
    ``(recipient, None)`` means the row was settled here — back to
    `draft_failed` because its fields no longer resolve, or `skipped` because
    another row of this batch already has the address.

    ``send_run_id``, ``to_email`` and ``sent_from`` are written by the claim.
    The first is what makes this send the one that delivered the row. The second
    ARMS the one-address-per-batch unique index — the address may not have
    existed before the task ran, so the constraint cannot live at upload time.
    The third is there because "which domain did this go from" is the first
    question asked when a domain gets flagged.
    """
    day_start, next_midnight = local_day_bounds(datetime.now(UTC), run.timezone)
    covered, key = _covered(run)
    async with pool.acquire() as conn:
        async with conn.transaction():
            # One claim at a time per BATCH, for the few milliseconds a claim
            # takes — see the docstring. Transaction-scoped, so it can never
            # outlive the claim or be left behind by a dead worker.
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1::text, 0))", str(run.batch_id)
            )
            row = await conn.fetchrow(
                f"""
                SELECT {RECIPIENT_COLUMNS_R} FROM {covered}
                  AND r.status = 'draft'
                  AND (r.next_attempt_at IS NULL OR r.next_attempt_at <= now())
                  AND EXISTS (
                    SELECT 1 FROM email_send_runs s
                    WHERE s.id = $3 AND s.tenant_id = $1 AND s.status = 'sending'
                  )
                  AND EXISTS (
                    SELECT 1 FROM scheduled_jobs j
                    WHERE j.id = $4 AND j.tenant_id = $1 AND j.claimed_by = $5
                      AND j.lease_expires_at > now()
                  )
                  -- Today's allowance, over the whole BATCH: two sends of one
                  -- batch share one ceiling, or a batch with four sends would
                  -- quietly have four times the number its owner set.
                  AND (
                    $6::int IS NULL
                    OR $6::int > (
                        SELECT count(*) FROM email_batch_recipients c
                        WHERE c.tenant_id = $1 AND c.batch_id = $7
                          AND c.sent_at >= $8 AND c.sent_at < $9
                    )
                  )
                  -- ...and the pace, for the same reason: `send_gap_seconds` is
                  -- the rate the BATCH sends at, so fifty "Send this one" sends
                  -- still go one gap apart rather than fifty at a time. An email
                  -- in flight counts as sent now; one `sending` for longer than
                  -- the provider's timeout is stranded, and must not hold the
                  -- whole batch until its own send comes back for it.
                  AND NOT EXISTS (
                    SELECT 1 FROM email_batch_recipients g
                    WHERE g.tenant_id = $1 AND g.batch_id = $7
                      AND g.sent_at > now() - make_interval(secs => $10::int)
                  )
                  AND NOT EXISTS (
                    SELECT 1 FROM email_batch_recipients g
                    WHERE g.tenant_id = $1 AND g.batch_id = $7 AND g.status = 'sending'
                      AND g.updated_at > now() - make_interval(secs => $11::int)
                  )
                ORDER BY r.next_attempt_at NULLS FIRST, r.row_number
                LIMIT 1
                FOR UPDATE OF r SKIP LOCKED
                """,
                run.tenant_id,
                key,
                run.id,
                job.id,
                job.worker,
                _cap_today(run, batch),
                run.batch_id,
                day_start,
                next_midnight,
                run.send_gap_seconds,
                _IN_FLIGHT_SECONDS,
            )
            if row is None:
                return None
            recipient = EmailRecipient.model_validate(dict(row))
            fields, reason = resolve_send_fields(batch, recipient)
            if fields is None:
                # Every writer keeps a `draft` sendable, so this is a row that
                # broke between its last edit and now. It never left, so it goes
                # back to review — fixable — rather than to terminal `send_failed`.
                await conn.execute(
                    """
                    UPDATE email_batch_recipients
                    SET status = 'draft_failed', last_error = $3, updated_at = now()
                    WHERE id = $1 AND tenant_id = $2 AND status = 'draft'
                    """,
                    recipient.id,
                    run.tenant_id,
                    reason,
                )
                return recipient, None
            try:
                # A savepoint, so a duplicate address rolls back this statement
                # alone and the settle below still commits. `uq_email_recipients_to`
                # firing IS the guarantee working: one address is mailed at most
                # once per batch.
                async with conn.transaction():
                    await conn.execute(
                        """
                        UPDATE email_batch_recipients
                        SET status = 'sending', send_run_id = $5, to_email = $3, sent_from = $4,
                            send_attempts = send_attempts + 1, updated_at = now()
                        WHERE id = $1 AND tenant_id = $2 AND status = 'draft'
                        """,
                        recipient.id,
                        run.tenant_id,
                        fields["to"],
                        run.from_email,
                        run.id,
                    )
            except asyncpg.UniqueViolationError:
                await conn.execute(
                    """
                    UPDATE email_batch_recipients
                    SET status = 'skipped', skip_reason = 'duplicate_recipient', send_run_id = $3,
                        last_error = 'another row in this batch already has this address',
                        updated_at = now()
                    WHERE id = $1 AND tenant_id = $2 AND status = 'draft'
                    """,
                    recipient.id,
                    run.tenant_id,
                    run.id,
                )
                return recipient, None
    # `send_attempts` increments exactly once per attempt, here and never at
    # settlement — and the row this returns carries the value the database now
    # holds, so the retry decision downstream counts THIS attempt. Reading the
    # pre-claim number there gives every row one more go than its run allows.
    return (
        recipient.model_copy(
            update={"status": "sending", "send_attempts": recipient.send_attempts + 1}
        ),
        fields,
    )


async def _send_one(
    pool: asyncpg.Pool,
    tenant: Tenant,
    run: EmailSendRun,
    batch: EmailBatch,
    provider: EmailProvider,
    credential: str,
    recipient: EmailRecipient,
    fields: dict[str, str],
) -> str:
    """Send one claimed row and settle it. Returns `sent` | `row` | `transient` | `account`.

    Never leaves a row non-terminal on a path it controls: only a crash or the
    transient class leaves it `draft` or `sending`, and both of those the send
    picks up again.
    """
    message = EmailMessage(
        to=fields["to"],
        subject=fields["subject"],
        body=fields["body"],
        body_format=batch.body_format,
        from_email=run.from_email,
        from_name=run.from_name,
        reply_to=run.reply_to,
    )
    try:
        receipt: SendReceipt = await provider.send(
            credential=credential,
            message=message,
            idempotency_key=idempotency_key(recipient.id),
        )
    except Exception as exc:  # noqa: BLE001 — every failure is classified below
        failure = provider.classify_error(exc)
        if failure == "account":
            # Deterministic: a revoked key, an unverified domain, an exhausted
            # quota. Every remaining row fails identically, so the run stops on
            # the FIRST one rather than proving it ten times — a rolling counter
            # is the right shape for probabilistic failure and the wrong shape
            # for deterministic failure.
            await _settle(pool, run, recipient, "send_failed", str(exc))
            await _fail_run(pool, tenant, run, str(exc))
            return "account"
        if failure == "row":
            # The breaker is deliberately NOT touched — not tripped and not
            # reset. This address is bad; that is a fact about the row's data,
            # and the run is neither failing nor working because of it. Resetting
            # would let one bad address in every ten 429s hold the breaker open
            # for ever, which is the failure the breaker exists to catch.
            await _settle(pool, run, recipient, "send_failed", str(exc))
            return "row"
        retry_after = getattr(exc, "retry_after_seconds", None)
        if failure == "rate_limited":
            # **A rate limit costs this row nothing.** Not an attempt, not the
            # breaker, not `send_retry_after_minutes`: with no lock and no shared
            # limiter, a `429` IS the backpressure mechanism, and a Resend team
            # shares one 10 rps limit across every key — including the tenant's
            # own application's password-reset emails. Charging a row three
            # strikes for somebody else's traffic put reviewed drafts in
            # `send_failed`, which nothing but a paid redraft recovers.
            #
            # The row comes back at the provider's own `retry-after`, or in a few
            # seconds if it sent none.
            wait = int(retry_after) if retry_after else _RATE_LIMIT_FALLBACK_SECONDS
            await _settle(
                pool,
                run,
                recipient,
                "draft",
                str(exc),
                retry_after_seconds=wait,
                refund_attempt=True,
            )
            logger.info(
                "email send %s: rate limited, row %s back in %ds", run.id, recipient.id, wait
            )
            return "rate_limited"
        # Back to `draft` — still covered — for another attempt under the same
        # idempotency key, or out of the run's hands once its attempts are spent.
        retryable = recipient.send_attempts < run.send_attempts
        await _settle(
            pool,
            run,
            recipient,
            "draft" if retryable else "send_failed",
            str(exc),
            retry_after_seconds=run.send_retry_after_minutes * 60 if retryable else None,
        )
        await _record_outcome(pool, run, failed=True)
        logger.warning("email send %s: row %s deferred — %s", run.id, recipient.id, exc)
        return "transient"

    # The batch's sending-day counter moves in the statement that records the
    # send, so it costs no round trip and cannot disagree with `sent_at`.
    await pool.execute(
        """
        WITH s AS (
            UPDATE email_batch_recipients
            SET status = 'sent', provider_message_id = $3, sent_at = now(),
                last_error = NULL, updated_at = now()
            WHERE id = $1 AND tenant_id = $2 AND status = 'sending'
            RETURNING 1
        )
        UPDATE email_batches SET send_days = send_days + 1, last_send_day = $5
        WHERE id = $4 AND tenant_id = $2 AND EXISTS (SELECT 1 FROM s)
          AND (last_send_day IS NULL OR last_send_day < $5)
        """,
        recipient.id,
        run.tenant_id,
        receipt.id,
        run.batch_id,
        local_today(datetime.now(UTC), batch.timezone),
    )
    await _record_outcome(pool, run, failed=False)
    return "sent"


async def _settle(
    pool: asyncpg.Pool,
    run: EmailSendRun,
    recipient: EmailRecipient,
    status: str,
    error: str | None,
    *,
    retry_after_seconds: int | None = None,
    refund_attempt: bool = False,
) -> None:
    """Move one row out of the sender's hands, whatever happened to it.

    Seconds rather than minutes, because a rate limit comes back in the seconds
    the provider asked for while an ordinary transient failure comes back in
    `send_retry_after_minutes`. `refund_attempt` is the other half of that: a
    429 must not spend one of the row's three goes on somebody else's traffic.

    A row going back to `draft` sheds `send_run_id`: no send has delivered it,
    and the send that retries it writes its own when it claims it again.
    """
    await pool.execute(
        """
        UPDATE email_batch_recipients
        SET status = $3,
            send_run_id = CASE WHEN $3 = 'draft' THEN NULL ELSE send_run_id END,
            next_attempt_at = CASE
                WHEN $5::int IS NULL THEN NULL ELSE now() + make_interval(secs => $5::int)
            END,
            send_attempts = CASE WHEN $6 THEN GREATEST(send_attempts - 1, 0) ELSE send_attempts END,
            last_error = $4,
            updated_at = now()
        WHERE id = $1 AND tenant_id = $2 AND status = 'sending'
        """,
        recipient.id,
        run.tenant_id,
        status,
        (error or "")[:1000] or None,
        retry_after_seconds,
        refund_attempt,
    )


async def _record_outcome(pool: asyncpg.Pool, run: EmailSendRun, *, failed: bool) -> None:
    """Move the rolling breaker counter: a failure increments it, a success resets it.

    A rate limit calls neither: resetting on a 429 would let one in every ten
    hold the breaker open for ever, and incrementing would let somebody else's
    traffic stop this run. `failure_reason` is not written here — a failure the
    next send recovers from is not why a run stopped; `_breaker_tripped` writes
    it when the run actually pauses.
    """
    await pool.execute(
        """
        UPDATE email_send_runs
        SET consecutive_send_failures = CASE WHEN $3 THEN consecutive_send_failures + 1 ELSE 0 END,
            updated_at = now()
        WHERE id = $1 AND tenant_id = $2
        """,
        run.id,
        run.tenant_id,
        failed,
    )


async def _breaker_tripped(
    pool: asyncpg.Pool, tenant: Tenant, run: EmailSendRun, recipient: EmailRecipient
) -> None:
    """PAUSE a run that has failed `SEND_BREAKER_THRESHOLD` times in a row.

    **Not optional, and not belt-and-braces.** Nothing else bounds a run whose
    every row fails: it would go round this loop for ever, quietly, at whatever
    rate its gap allows.

    **Paused, not failed**, and that is the difference between "press Resume" and
    "reselect four thousand rows, re-pick the schedule, the window, the pace and
    the cap, and start again". Ten provider failures in a row is a Resend
    incident far more often than it is a permanently broken send — and a ten
    SECOND incident was enough to kill a send scheduled to run for days.

    Its rows stay covered, because that is what pausing means: nothing is lost,
    and `resume_send` already clears `failure_reason` and this counter, so resume
    IS the reset.

    `failed` is kept for the deterministic stops — a revoked key, a departed
    creator — where trying again cannot help. `failure_reason` is the error
    ``recipient`` — the row that tripped it — was just settled with.
    """
    tripped = await pool.fetchval(
        """
        UPDATE email_send_runs
        SET status = 'paused', next_send_at = NULL, next_send_reason = NULL, updated_at = now(),
            failure_reason = (
                SELECT last_error FROM email_batch_recipients WHERE id = $4 AND tenant_id = $2
            )
        WHERE id = $1 AND tenant_id = $2 AND status = 'sending'
          AND consecutive_send_failures >= $3
        RETURNING id
        """,
        run.id,
        run.tenant_id,
        SEND_BREAKER_THRESHOLD,
        recipient.id,
    )
    if tripped is None:
        return
    fresh = await load_send_run(pool, run.tenant_id, run.id)
    assert fresh is not None
    logger.error("email send %s: paused by the breaker — %s", run.id, fresh.failure_reason)
    await emit(tenant, events.EMAIL_SEND_PAUSED, fresh)


async def _fail_run(pool: asyncpg.Pool, tenant: Tenant, run: EmailSendRun, reason: str) -> None:
    """Stop this send for good. Its unsent rows are simply drafts again.

    Only the deterministic stops reach here — a revoked key, an unverified
    domain, a creator who left. The breaker pauses instead; see
    `_breaker_tripped`.

    **The batch is deliberately untouched.** A revoked key is a fact about the
    account, and the answer to it is fixing the key and creating another send —
    not destroying four thousand drafts the tenant reviewed and paid for.
    """
    stopped = await pool.fetchval(
        """
        UPDATE email_send_runs
        SET status = 'failed', failure_reason = $3, finished_at = now(),
            next_send_at = NULL, next_send_reason = NULL, updated_at = now()
        WHERE id = $1 AND tenant_id = $2 AND status IN ('scheduled', 'sending', 'paused')
        RETURNING id
        """,
        run.id,
        run.tenant_id,
        f"sending stopped: {reason}"[:1000],
    )
    if stopped is None:
        return
    fresh = await load_send_run(pool, run.tenant_id, run.id)
    assert fresh is not None
    await release_rows(pool, fresh)
    logger.error("email send %s: stopped — %s", run.id, reason)
    await emit(tenant, events.EMAIL_SEND_FAILED, fresh)


async def _finish(pool: asyncpg.Pool, tenant: Tenant, run: EmailSendRun) -> None:
    """Every covered row has gone: stamp `finished_at` and stop being scheduled.

    Writes the row and fires the event. The loop's next turn re-reads it, sees
    `sent` and exits through `_hand_back` — which is the point of having one exit.

    "Nothing left" is asked again under the BATCH row's lock, because that is the
    lock an append holds while it adds rows a standing send covers. The caller's
    answer is a statement old.
    """
    covered, key = _covered(run)
    async with pool.acquire() as conn:
        async with conn.transaction():
            batch_status = await conn.fetchval(
                "SELECT status FROM email_batches WHERE id = $1 AND tenant_id = $2 FOR UPDATE",
                run.batch_id,
                run.tenant_id,
            )
            left = await conn.fetchrow(
                f"""
                SELECT
                    count(*) FILTER (WHERE r.status = 'draft') AS waiting,
                    count(*) FILTER (WHERE r.status IN ('pending', 'drafting')) AS unwritten,
                    count(*) FILTER (WHERE r.status = 'sending' AND r.send_run_id = $3) AS in_flight
                FROM {covered}
                  AND r.status IN ('draft', 'pending', 'drafting', 'sending')
                """,
                run.tenant_id,
                key,
                run.id,
            )
            if (
                left["waiting"]
                or left["in_flight"]
                or (left["unwritten"] and batch_status in ("scheduled", "drafting", "paused"))
            ):
                return
            sent = await conn.fetchval(
                """
                UPDATE email_send_runs
                SET status = 'sent', finished_at = now(),
                    next_send_at = NULL, next_send_reason = NULL, updated_at = now()
                WHERE id = $1 AND tenant_id = $2 AND status = 'sending'
                RETURNING id
                """,
                run.id,
                run.tenant_id,
            )
            if sent is None:
                return
            final = await load_send_run(conn, run.tenant_id, run.id)
            assert final is not None
            await release_rows(conn, final)
    logger.info("email send %s: finished", run.id)
    await emit(tenant, events.EMAIL_SEND_COMPLETED, final)


async def _announce(
    pool: asyncpg.Pool, run: EmailSendRun, at: datetime | None, reason: str | None
) -> None:
    """Publish when this send next acts, for anyone reading it.

    Written only when it CHANGES, against the row the loop just read: a send
    pacing at one email a second must not add a round trip per email to rewrite
    the same NULL. `scheduled_jobs` used to answer this and cannot any more — a
    waiting loop holds its job `running` the whole time — and it was never the
    API's table to read.
    """
    if run.next_send_at == at and run.next_send_reason == reason:
        return
    await pool.execute(
        """
        UPDATE email_send_runs
        SET next_send_at = $3, next_send_reason = $4, updated_at = now()
        WHERE id = $1 AND tenant_id = $2
        """,
        run.id,
        run.tenant_id,
        at,
        reason,
    )


async def _hand_back(pool: asyncpg.Pool, job: JobContext, run: EmailSendRun) -> bool:
    """Give this job up, unless the run went live again while we were leaving.

    **The compare-and-set is the point.** Between the loop reading a `paused` run
    and this line, a `resume` can land — and `enqueue_once` would have found this
    job still `running` and inserted nothing, leaving a live send with covered rows
    and nothing scheduled. So the job is only completed while the run is still
    stopped; ``False`` means it is live again and this loop should carry on
    holding it.

    Completing the row here rather than letting the executor do it is what makes
    that atomic: `store.complete` names the same worker and finds the row already
    `done`, so it is a no-op either way. The run row is locked first, because a
    compare-and-set alone reads a snapshot: a resume, or an append reopening a
    finished standing send, committing under it would find this job still
    `running`, enqueue nothing, and be lost.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT 1 FROM email_send_runs WHERE id = $1 AND tenant_id = $2 FOR UPDATE",
                run.id,
                run.tenant_id,
            )
            released = await conn.fetchval(
                """
                UPDATE scheduled_jobs
                SET status = 'done', last_error = NULL,
                    claimed_by = NULL, lease_expires_at = NULL, updated_at = now()
                WHERE id = $1 AND tenant_id = $2 AND status = 'running' AND claimed_by = $3
                  AND EXISTS (
                    SELECT 1 FROM email_send_runs
                    WHERE id = $4 AND tenant_id = $2
                      AND status IN ('paused', 'canceled', 'sent', 'failed')
                  )
                RETURNING id
                """,
                job.id,
                run.tenant_id,
                job.worker,
                run.id,
            )
    if released is not None:
        return True
    # Either the run is live again, or this job is not ours any more. The second
    # is not a reason to keep looping.
    return not await _still_ours(pool, run.tenant_id, job)


__all__ = ["SEND_BREAKER_THRESHOLD", "run_send_job"]
