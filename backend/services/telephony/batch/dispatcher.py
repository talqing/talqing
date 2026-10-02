"""The batch dispatcher: one job per batch, and everything a pass may not do.

A batch is a `scheduled_jobs` row of kind `call.batch.dial`. `job-scheduler`
publishes it once `scheduled_at` comes round, one executor claims it with a
conditional `UPDATE`, and `run_dial_pass` takes exactly one pass and then says
when to come back. There is no discovery loop, no lock and no long-lived
coroutine: a batch between passes has nothing running anywhere, and a batch
scheduled for next Tuesday is genuinely asleep until then rather than waking
every 30 s to find it is not time yet.

Three rules hold the whole design up. Every reader and every future editor of
this file has to keep all three true:

1. **Nothing durable is cached.** Policy and work are re-read on every pass. That
   was always the rule; now the world enforces it too, because consecutive
   passes routinely run in different containers.
2. **Every state change is a compare-and-set**, an ``UPDATE`` whose ``WHERE``
   names the state it expects to find. Never read-then-write. That is what makes
   a repeated pass harmless and a duplicated delivery unable to do damage. The
   one decision that needs a read first — "nothing is left, so the batch is
   done" — is made under the batch row's lock, the one an append takes.
3. **Claiming a recipient and recording its call are one transaction**, and the
   LiveKit calls happen outside it. So a recipient is ``dialing`` if and only if
   it has a ``session_id`` — a ``CHECK`` constraint, not a promise — and there is
   no half-claimed state, no orphan sweep, and no window in which a crash could
   lose or duplicate a dial.

**``max_concurrency`` is held by the claim and by nothing else.** ``enqueue_once``
allows one live job per batch and ``store.claim`` allows one runner per job, so
exactly one pass computes ``slots = max_concurrency - dialing`` at a time. That is
the property the Redis lock used to supply. Nothing above consults it either, and
the three rules are still what make an overlap a briefly-exceeded capacity limit
rather than a double dial — so a change that would be unsafe if two passes ever
overlapped is still a wrong change.

**The pace is the batch's, and the claim holds it.** ``dial_gap_seconds`` is a
compare-and-set on ``last_dial_at`` inside the claim transaction, so a gap is
honoured exactly however passes fall; because dialling is pass-based, a call
starts up to one pass late. The daily limit (fixed or a ramp, see
``services.scheduling.ramp``) is counted once per pass against the batch's
``sessions`` for its local day, and spent one claim at a time.

**What an interrupted pass costs.** A deploy cancels it, `_dispatch` releases the
job row, and the next scheduler tick republishes it — the recipient it was
committed to dialling is shielded through, and everything else is simply not
claimed. A container that is *killed* leaves the row ``running`` with a lease
nobody renews, and `store.due_jobs` republishes it within ``LEASE_SECONDS``. No
operator, and no lock.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

import db
from services import credits, webhooks
from services.jobs import JobContext
from services.scheduling import local_today, window_state
from services.telephony import dial, livekit_sip
from services.user import (
    ROLE_ADMIN,
    ROLE_EDITOR,
    Context,
    NotAMember,
    Tenant,
    load_context,
)
from services.webhooks import events
from settings import get_settings

from . import settle
from .models import CallBatch, ClaimedRecipient
from .service import batch_response, dial_cap_today, dialed_today, load_batch

logger = logging.getLogger("talqing.telephony.batch")


# ── one pass ────────────────────────────────────────────────────────────────


async def run_dial_pass(tenant: Tenant, args: dict, job: JobContext) -> datetime | None:
    """One pass over one batch. Returns when to come back, or ``None`` when done.

    `job` is only used to give the job up when the batch ends (`_hand_back`):
    dialling works in bounded passes, so it has no long-held claim to guard a row
    claim against.

    Four steps, in this order and for this reason:

    A. **start** — a scheduled batch whose start time has arrived becomes running.
    B. **reconcile** — settle recipients whose calls have already ended. Before C
       so slots freed by finished calls are reused in the same pass.
    C. **dial** — only while running and inside business hours.
    D. **finish** — nothing dialling and nothing left that could ever be claimed.

    **The breaker is checked twice, and both are load-bearing.** Before C, so a
    broken batch stops dialling; and again before D, so one that broke on its
    last rows is not reported as having completed. The second check is not
    belt-and-braces: settlements arrive from the voice worker *between* the two,
    and without it a batch whose every call failed lands on `completed` — which
    would make "we reached everyone" and "nothing got through" the same word.

    Both checks read a freshly loaded policy row, because both the reconcile and
    the dials just moved the counter they are reading.

    There is no recovery step. A claim either committed with its session or never
    happened, so there are no orphans to find; what is left of that problem is a
    process that crashes while a call is live, which the worker hook cannot see
    because it never ran — and which step B resolves itself, from the stale
    branch of ``settle.reconcile_batch``'s ``WHERE``.
    """
    batch_id = UUID(str(args["batch_id"]))
    batch = await _reload(tenant, batch_id)
    if batch is None:
        return None
    if batch.ended_at is not None:
        return await _hand_back(tenant, job, batch_id)

    now = datetime.now(UTC)
    if batch.status == "scheduled" and batch.start_at is not None and batch.start_at > now:
        # Only reachable when somebody moved `start_at` forward while a pass was
        # running, so `patch_batch` could not move the job with it. Honour the
        # row, not the schedule — a batch must never dial before its own time.
        return batch.start_at

    batch = await _step_start(tenant, batch)
    await settle.reconcile_batch(tenant, batch)
    batch = await _reload(tenant, batch_id)
    if batch is None:
        return None
    batch = await _trip_breaker_if_due(tenant, batch)
    await _step_dial(tenant, batch)

    batch = await _reload(tenant, batch_id)
    if batch is None:
        return None
    batch = await _trip_breaker_if_due(tenant, batch)
    if await _step_finish(tenant, batch):
        return await _hand_back(tenant, job, batch_id)
    return _next_pass()


def _next_pass() -> datetime:
    return datetime.now(UTC) + timedelta(
        seconds=get_settings().telephony.batch.pass_interval_seconds
    )


async def _reload(tenant: Tenant, batch_id: UUID) -> CallBatch | None:
    """The policy row as it is now. One indexed read, and the alternative is a
    step acting on facts the step before it already invalidated."""
    return await load_batch(await db.tenant_pool(tenant), tenant.id, batch_id)


async def _step_start(tenant: Tenant, batch: CallBatch) -> CallBatch:
    """A scheduled batch whose moment has come."""
    if batch.status != "scheduled":
        return batch
    pool = await db.tenant_pool(tenant)
    started = await pool.fetchval(
        """
        UPDATE call_batches
        SET status = 'running', started_at = COALESCE(started_at, now()), updated_at = now()
        WHERE id = $1 AND tenant_id = $2 AND status = 'scheduled'
          AND (start_at IS NULL OR start_at <= now())
        RETURNING id
        """,
        batch.id,
        batch.tenant_id,
    )
    if started is None:
        return batch
    fresh = await load_batch(pool, batch.tenant_id, batch.id)
    assert fresh is not None
    logger.info("batch %s: started", batch.id)
    await _emit(tenant, events.BATCH_STARTED, fresh)
    return fresh


async def _trip_breaker_if_due(tenant: Tenant, batch: CallBatch) -> CallBatch:
    """Stop a batch that has failed ``BREAKER_THRESHOLD`` times in a row.

    Rolling rather than cumulative, and rolling rather than prefix: an expired
    carrier credential or an unpublished agent fails every row identically and
    happens mid-batch at least as often as at the start. ``failure_reason`` is
    already on the row — whoever recorded the failure wrote it — so this only
    flips the status.
    """
    if batch.consecutive_setup_failures < settle.BREAKER_THRESHOLD:
        return batch
    if batch.status not in ("scheduled", "running", "paused"):
        return batch
    pool = await db.tenant_pool(tenant)
    tripped = await pool.fetchval(
        """
        UPDATE call_batches
        SET status = 'failed', updated_at = now()
        WHERE id = $1 AND tenant_id = $2
          AND status IN ('scheduled', 'running', 'paused')
          AND consecutive_setup_failures >= $3
        RETURNING id
        """,
        batch.id,
        batch.tenant_id,
        settle.BREAKER_THRESHOLD,
    )
    if tripped is None:
        return batch
    fresh = await load_batch(pool, batch.tenant_id, batch.id)
    assert fresh is not None
    logger.error("batch %s: stopped by the breaker — %s", batch.id, fresh.failure_reason)
    await _emit(tenant, events.BATCH_FAILED, fresh)
    return fresh


async def _step_dial(tenant: Tenant, batch: CallBatch) -> None:
    """Fill the batch's free concurrency slots, one claim at a time."""
    if batch.status != "running":
        return
    may_dial, _ = window_state(
        now=datetime.now(UTC),
        timezone=batch.timezone,
        window_start_local=batch.window_start_local,
        window_end_local=batch.window_end_local,
        window_days=batch.window_days,
        subject=f"call batch {batch.id}",
    )
    if not may_dial:
        return

    pool = await db.tenant_pool(tenant)
    if not await credits.has_credit(tenant):
        # Same shape as the calling-window check above: this pass does nothing
        # and the next one reconsiders. The batch stays `running` so a top-up
        # resumes it with no button to press, and `failure_reason` is what the
        # batch page shows meanwhile — a campaign that quietly stops dialling
        # with no explanation is the worst version of this.
        #
        # Gated per PASS, never per recipient: refusing a recipient would burn an
        # `attempts` on a row that was never dialled, and eventually exhaust
        # `max_attempts` on a campaign that only needed a payment.
        await settle.note_pause_reason(
            pool,
            batch_id=batch.id,
            tenant_id=batch.tenant_id,
            reason=credits.INSUFFICIENT_CREDITS_MESSAGE,
        )
        return

    dialing = await pool.fetchval(
        """
        SELECT count(*) FROM call_batch_recipients
        WHERE tenant_id = $1 AND batch_id = $2 AND status = 'dialing'
        """,
        batch.tenant_id,
        batch.id,
    )
    slots = batch.max_concurrency - dialing
    if slots <= 0:
        return

    # Today's allowance, counted once and spent per claim below, so ten free
    # slots cannot overshoot a limit of three.
    now = datetime.now(UTC)
    cap = dial_cap_today(batch, now)
    if cap is not None:
        placed = await dialed_today(pool, batch.tenant_id, [batch], now)
        slots = min(slots, cap - placed[batch.id])
        if slots <= 0:
            return
    if (
        batch.last_dial_at is not None
        and batch.last_dial_at + timedelta(seconds=batch.dial_gap_seconds) > now
    ):
        return

    # Two per-pass facts, resolved once and reused across every claim below.
    # Neither may be re-read per dial — four wasted round trips per recipient in
    # a 10 000-row batch — and neither may be cached across passes: reading the
    # published version every pass is what makes republishing a prompt reach the
    # calls that follow.
    resolved = await _resolve_pass(tenant, batch)
    if resolved is None:
        return
    ctx, target = resolved

    cfg = get_settings().telephony.batch
    pause = max(cfg.dial_interval_seconds, batch.dial_gap_seconds)
    for _ in range(slots):
        try:
            async with asyncio.timeout(cfg.dial_timeout_seconds):
                claimed = await _claim_and_dial(tenant, ctx, batch, target)
        except TimeoutError:
            # Last resort: LiveKit's own deadline fires well inside this, so
            # reaching here means something hung that neither it nor Postgres
            # covered. The recipient is left exactly as the transaction left it.
            logger.error("batch %s: a dial exceeded %ss", batch.id, cfg.dial_timeout_seconds)
            return
        if claimed is None:
            return
        if pause >= cfg.pass_interval_seconds:
            # A gap a pass or longer: one call now, and the next pass places the
            # next one, rather than this pass sleeping through it.
            return
        await asyncio.sleep(pause)


async def _resolve_pass(tenant: Tenant, batch: CallBatch) -> tuple[Context, dial.DialTarget] | None:
    """The batch's identity and its dial target, or ``None`` after recording why.

    The dispatcher acts as the person who created the batch, and **the membership
    join is the authorization check** — the same one ``api.dataplane.deps`` makes for
    an HTTP request and ``load_context`` makes for a queued CoPilot turn. A batch
    whose creator has left the organization, or been demoted to VIEWER, stops:
    a departed employee's campaign must not keep spending the workspace's money,
    and a batch may not do through a background loop what its owner can no longer
    do through the API.

    A control-plane query that *errors* is a different verdict entirely — it is
    transient, and conflating it with "not a member" would let a database blip
    kill live campaigns. Those propagate, and the pass is skipped and retried.
    """
    try:
        ctx = await load_context(tenant, batch.created_by_user_id)
    except NotAMember:
        await _setup_failed(
            tenant, batch, "the person who created this batch is no longer in this organization"
        )
        return None
    if ctx.user.role not in (ROLE_ADMIN, ROLE_EDITOR):
        await _setup_failed(
            tenant, batch, f"{ctx.user.email} no longer has permission to place calls"
        )
        return None
    # A batch with a plan carries its own cast and may have no `agent_id` at all
    # — an inline entry agent has no row to point at.
    if batch.from_phone_number_id is None or (batch.agent_id is None and batch.agent_plan is None):
        await _setup_failed(tenant, batch, "this batch's agent or phone number has been deleted")
        return None
    try:
        target = await dial.resolve_dial_target(
            ctx,
            agent_id=batch.agent_id,
            from_phone_number_id=batch.from_phone_number_id,
            session_vars=batch.vars or {},
            agent_plan=batch.agent_plan,
        )
    except dial.DialError as exc:
        await _setup_failed(tenant, batch, exc.message)
        return None
    return ctx, target


async def _setup_failed(tenant: Tenant, batch: CallBatch, reason: str) -> None:
    """Record a failure that happened before any recipient could be claimed.

    Nothing is written to a work row — no recipient was touched — so this is
    purely a policy write: the breaker counter and the reason. Ten of these in a
    row is how "somebody unpublished the agent an hour into the batch" surfaces
    as a stopped batch with an explanation, rather than 9 000 identical dial
    failures.
    """
    pool = await db.tenant_pool(tenant)
    count = await settle.record_setup_outcome(
        pool, batch_id=batch.id, tenant_id=batch.tenant_id, failed=True, reason=reason
    )
    logger.warning("batch %s: cannot dial (%d in a row) — %s", batch.id, count, reason)
    return None


async def _claim_and_dial(
    tenant: Tenant, ctx: Context, batch: CallBatch, target: dial.DialTarget
) -> ClaimedRecipient | None:
    """Take one recipient and place its call. ``None`` when nothing is claimable.

    The claim also takes the batch's turn: one ``UPDATE`` that stamps
    ``last_dial_at`` only while the batch is still ``running`` and its gap has
    passed, and counts today as a dialling day. So a pause or a longer gap takes
    hold on the very next call, not at the end of the pass.

    The transaction holds the claim, the conversation thread and the ``sessions``
    row together, and commits **before** LiveKit is asked for anything and long
    before a carrier is contacted. Every failure therefore has exactly one
    outcome:

    - **anything raises before the commit** — Postgres rolls all of it back. The
      row is ``pending``, ``attempts`` unchanged, no session, and no carrier was
      contacted. The compensating write below then spends the attempt, because
      nothing else on this path increments the counter and without it the row
      would retry forever.
    - **the LiveKit calls fail** — the session is already marked
      ``sip_dispatch_failed`` by ``dispatch_outbound_call``, so settling it here
      frees the slot immediately instead of waiting for the next pass.
    - **the process dies after the commit but before the dispatch** — the row is
      ``dialing`` and its session sits ``queued`` with no job behind it. That is
      the one residual tail, and the reconcile's stale branch is what ends it,
      ``CALL_STALE_HOURS`` later. A *shutdown* no longer opens it: the dispatch
      below is shielded, so a deploy that lands in this window still places the
      call it has already committed to.
    """
    pool = await db.tenant_pool(tenant)
    claimed: ClaimedRecipient | None = None
    recorded: dial.RecordedCall | None = None
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    SELECT id, row_number, to_e164, userdata, attempts
                    FROM call_batch_recipients
                    WHERE tenant_id = $1 AND batch_id = $2 AND status = 'pending'
                      AND (next_attempt_at IS NULL OR next_attempt_at <= now())
                    ORDER BY next_attempt_at NULLS FIRST, row_number
                    LIMIT 1
                    FOR UPDATE SKIP LOCKED
                    """,
                    batch.tenant_id,
                    batch.id,
                )
                if row is None:
                    return None
                turn = await conn.fetchval(
                    """
                    UPDATE call_batches
                    SET last_dial_at = now(),
                        dial_days = dial_days + CASE
                            WHEN last_dial_day IS NULL OR last_dial_day < $3 THEN 1 ELSE 0
                        END,
                        last_dial_day = GREATEST(last_dial_day, $3)
                    WHERE id = $1 AND tenant_id = $2 AND status = 'running'
                      AND (
                        last_dial_at IS NULL
                        OR last_dial_at + make_interval(secs => dial_gap_seconds) <= now()
                      )
                    RETURNING id
                    """,
                    batch.id,
                    batch.tenant_id,
                    local_today(datetime.now(UTC), batch.timezone),
                )
                if turn is None:
                    return None
                claimed = ClaimedRecipient.model_validate(dict(row))
                recorded = await dial.record_outbound_call(
                    conn,
                    ctx=ctx,
                    target=target,
                    to_e164=claimed.to_e164,
                    userdata=claimed.userdata,
                    # The campaign's bag, copied onto every call it places.
                    # Checked at create and again by this pass's
                    # `resolve_dial_target`, so there is nothing left to check.
                    session_vars=batch.vars or {},
                    batch_id=batch.id,
                )
                await conn.execute(
                    """
                    UPDATE call_batch_recipients
                    SET status = 'dialing', attempts = attempts + 1,
                        session_id = $3, next_attempt_at = NULL, updated_at = now()
                    WHERE id = $1 AND tenant_id = $2 AND status = 'pending'
                    """,
                    claimed.id,
                    batch.tenant_id,
                    recorded.session_id,
                )
    except Exception:
        logger.exception("batch %s: could not record a dial", batch.id)
        if claimed is not None:
            await _spend_attempt_without_dialing(tenant, batch, claimed)
        return claimed
    assert recorded is not None  # the only path out of the block above without one returns

    # Shielded, because the claim and the session above are already durable: a
    # shutdown that abandoned the dispatch now would strand this recipient
    # `dialing` behind a `queued` session until the reconcile's stale branch
    # frees it CALL_STALE_HOURS later. This is the one place in the executor
    # where cancelling at t=0 is not safe on its own.
    dispatch = asyncio.ensure_future(
        dial.dispatch_outbound_call(ctx=ctx, target=target, recorded=recorded)
    )
    try:
        await asyncio.shield(dispatch)
    except asyncio.CancelledError:
        # `shield` alone is not enough: it protects the inner future, but the
        # `await` still raises here and would leave the dispatch running
        # detached — so `settle_recipient` below would never run for a dial that
        # fails. This bounded re-await is what actually holds the pass open for
        # it, bounded because a shutdown must not be held by a hung LiveKit call.
        #
        # `_step_dial`'s own `asyncio.timeout(dial_timeout_seconds)` is
        # indistinguishable from the executor's cancel down here, so a dial that
        # genuinely hangs may wait that and then this. Bounded and rare, and
        # `SETTLE_GRACE_SECONDS` cuts it off either way.
        with contextlib.suppress(Exception):
            await asyncio.wait_for(dispatch, livekit_sip.LIVEKIT_HTTP_TIMEOUT_SECONDS)
        raise
    except dial.DialError:
        # The session is already `failed` / `sip_dispatch_failed`. Settling it
        # now rather than on the next pass is only a latency win — the reconcile
        # would find it either way — but it is the difference between a slot
        # freeing in a second and in half a minute.
        await settle.settle_recipient(tenant, recorded.session_id)
    return claimed


async def _spend_attempt_without_dialing(
    tenant: Tenant, batch: CallBatch, claimed: ClaimedRecipient
) -> None:
    """A dial we could not even place still counts as an attempt.

    Its own small transaction, because the one that would have recorded the call
    has already rolled back. Without this the row keeps its old ``attempts`` and
    is claimed again on the very next pass, forever — ``max_attempts`` can never
    be reached on a path that never increments it.
    """
    attempts = claimed.attempts + 1
    final = attempts >= batch.max_attempts
    pool = await db.tenant_pool(tenant)
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    UPDATE call_batch_recipients
                    SET attempts = $3,
                        status = CASE WHEN $4 THEN 'failed' ELSE 'pending' END,
                        next_attempt_at = CASE
                            WHEN $4 THEN NULL ELSE now() + make_interval(mins => $5)
                        END,
                        last_close_reason = 'sip_dispatch_failed',
                        updated_at = now()
                    WHERE id = $1 AND tenant_id = $2 AND status = 'pending'
                    """,
                    claimed.id,
                    batch.tenant_id,
                    attempts,
                    final,
                    batch.retry_after_minutes,
                )
                await settle.record_setup_outcome(
                    conn,
                    batch_id=batch.id,
                    tenant_id=batch.tenant_id,
                    failed=True,
                    reason="the call could not be placed",
                )
    except Exception:
        logger.exception("batch %s: could not record a failed dial attempt", batch.id)


async def _step_finish(tenant: Tenant, batch: CallBatch) -> bool:
    """Is this batch done? If so, stamp ``ended_at`` and stop.

    Two questions, both bounded: is anything in flight, and is anything left that
    could ever be claimed. A cancelled or breaker-failed batch answers the second
    with "no" immediately — its remaining ``pending`` rows will never be claimed —
    which is what lets it exit as soon as its last live call lands rather than
    sitting on rows nothing will touch.

    Asked under the batch row's lock, which an append holds while it inserts:
    asked outside it, an append landing between the question and the write would
    leave its rows on a finished batch, never dialled.
    """
    pool = await db.tenant_pool(tenant)
    async with pool.acquire() as conn:
        async with conn.transaction():
            status = await conn.fetchval(
                "SELECT status FROM call_batches WHERE id = $1 AND tenant_id = $2 FOR UPDATE",
                batch.id,
                batch.tenant_id,
            )
            if status is None:
                return True
            unfinished = await conn.fetchrow(
                """
                SELECT
                    EXISTS (
                        SELECT 1 FROM call_batch_recipients
                        WHERE tenant_id = $1 AND batch_id = $2 AND status = 'dialing'
                    ) AS in_flight,
                    EXISTS (
                        SELECT 1 FROM call_batch_recipients
                        WHERE tenant_id = $1 AND batch_id = $2 AND status = 'pending'
                    ) AS claimable
                """,
                batch.tenant_id,
                batch.id,
            )
            if unfinished["in_flight"]:
                return False
            if unfinished["claimable"] and status not in ("canceled", "failed"):
                return False
            ended = await conn.fetchval(
                """
                UPDATE call_batches
                SET ended_at = now(),
                    status = CASE
                        WHEN status IN ('scheduled', 'running', 'paused') THEN 'completed'
                        ELSE status
                    END,
                    updated_at = now()
                WHERE id = $1 AND tenant_id = $2 AND ended_at IS NULL
                RETURNING id
                """,
                batch.id,
                batch.tenant_id,
            )
    if ended is None:
        return True
    fresh = await load_batch(pool, batch.tenant_id, batch.id)
    assert fresh is not None
    logger.info("batch %s: finished as %s", batch.id, fresh.status)
    if fresh.status == "completed":
        # Only a batch that ran to the end reports completion. A cancelled or
        # failed one already said what happened, and a second event claiming it
        # completed would contradict the first.
        await _emit(tenant, events.BATCH_COMPLETED, fresh)
    return True


async def _hand_back(tenant: Tenant, job: JobContext, batch_id: UUID) -> datetime | None:
    """Give the job up, unless an append woke the batch while this pass was ending.

    ``None`` once the job is released; otherwise when to come back. Completed
    here rather than by the executor, under the batch row's lock: an append that
    wakes the batch first is seen as `ended_at IS NULL` and the job keeps going,
    and one that lands after finds the job `done` and enqueues a new one. Either
    way a live batch is never left without a job.
    """
    pool = await db.tenant_pool(tenant)
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT 1 FROM call_batches WHERE id = $1 AND tenant_id = $2 FOR UPDATE",
                batch_id,
                tenant.id,
            )
            released = await conn.fetchval(
                """
                UPDATE scheduled_jobs
                SET status = 'done', last_error = NULL,
                    claimed_by = NULL, lease_expires_at = NULL, updated_at = now()
                WHERE id = $1 AND tenant_id = $2 AND status = 'running' AND claimed_by = $3
                  AND NOT EXISTS (
                    SELECT 1 FROM call_batches
                    WHERE id = $4 AND tenant_id = $2 AND ended_at IS NULL
                  )
                RETURNING id
                """,
                job.id,
                tenant.id,
                job.worker,
                batch_id,
            )
    if released is not None:
        return None
    logger.info("batch %s: woken while finishing; carrying on", batch_id)
    return _next_pass()


async def _emit(tenant: Tenant, event: str, batch: CallBatch) -> None:
    payload = await batch_response(tenant, batch)
    await webhooks.dispatch(
        tenant,
        event,
        str(batch.agent_id) if batch.agent_id else None,
        payload.model_dump(mode="json"),
    )
