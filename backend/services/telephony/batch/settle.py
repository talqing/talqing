"""Landing a finished call on its batch recipient.

**Exactly two writers settle an attempt, and both run the code in this file.**

- The **voice worker**, at finalize — the fast path. The row leaves ``dialing``
  the moment the call does, so an operator watching a batch sees it land rather
  than waiting for a dispatcher pass.
- **The dispatcher's reconcile step**, every pass — the one that depends on
  nothing. It reads ``sessions.status``, ``close_reason`` and ``created_at`` and
  nothing else: not on a process surviving, not on billing having succeeded, not
  on any other sweep having run first.

They are safe together because every transition is a compare-and-set on
``status = 'dialing'``: whichever arrives second matches zero rows and does
nothing.

The reconcile covers two things the worker hook cannot, and both are
non-negotiable — without either, a recipient holds a concurrency slot for ever,
the batch never gets an ``ended_at``, and its job defers itself for the life of
the deployment:

- a session that **is** terminal whose settlement did not happen, because the
  worker hook is required to swallow its own errors so it can never break a
  call's billing. Such a recipient does not settle late; it never settles;
- a session whose worker **died**, which the hook cannot report because it never
  ran. That leaves the session ``running`` — or ``queued``, when the dispatcher
  itself died between committing a claim and dispatching it — so the reconcile
  fails it ``stale`` itself, in the same transaction as the settlement.

The second used to belong to a platform-wide ``billing.close_stale_sessions``
sweep. It is a predicate here instead because the batch owns its recipients and
should not need a cross-tenant sweep to learn that one of them is dead. It is
also the backstop a LiveKit webhook receiver would need: LiveKit states
plainly that webhook delivery has no guarantees, so a dropped ``room_finished``
costs a batch latency rather than correctness.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from uuid import UUID

import db
from services.close_reasons import VOICEMAIL, bucket_for, describe
from services.user import Tenant

from .models import BATCH_COLUMNS, CallBatch

logger = logging.getLogger("talqing.telephony.batch")

# Consecutive setup failures that stop a batch. Rolling, not cumulative: an
# expired carrier credential or an unpublished agent fails every row identically
# and happens mid-batch at least as often as at the start, so a breaker that only
# inspected the first ten attempts would let a batch run cleanly for 5 000 rows
# and burn the other 5 000 after a credential expired at lunchtime.
BREAKER_THRESHOLD = 10

# A voice call cannot outlive this, so a batch recipient still `dialing` against
# a session that has not gone terminal in this long has lost its worker.
#
# MUST stay above `livekit.sip.max_call_duration_seconds` (10800s = 3h), or this
# fails calls that are still on the phone: at 2h it marked a legitimate 2h+ call
# `stale` mid-conversation, and the worker only put that right when the call
# finally ended. 4h leaves an hour of margin. Raising the call cap without
# raising this reintroduces the same bug.
CALL_STALE_HOURS = 4

# Statuses in which a batch may still place calls, and therefore the only ones in
# which re-arming a retry makes sense. A cancelled or breaker-failed batch
# resolves a retryable outcome to `failed` instead — otherwise its job would
# keep deferring over a row nothing will ever claim, and never finish.
DIALABLE_BATCH_STATUSES = ("scheduled", "running", "paused")

# Buckets that mean something is systematically wrong rather than one person
# being unavailable. `caller_unreachable` is deliberately absent: a run of busy
# signals is a phone list, not a fault, and stopping the batch over it would be
# the breaker firing on the thing it exists to keep working.
_BREAKER_BUCKETS = frozenset({"carrier_fault", "configuration", "platform", "other"})

# Reached the carrier, the carrier could not reach the person — and never will.
# Everything else in `caller_unreachable` is worth another attempt.
_TERMINAL_UNREACHABLE = frozenset({"sip_unallocated_failed", "sip_declined_failed"})


@dataclass(frozen=True, slots=True)
class Outcome:
    """What one finished attempt means for its recipient and its batch."""

    # The person was reached. Terminal `completed`, forever — the never-call-
    # twice invariant outranks every retry rule.
    reached: bool
    # A property of the outcome, not a decision: whether the row actually goes
    # back to `pending` also depends on `max_attempts` and on the batch still
    # being able to dial.
    retryable: bool
    counts_toward_breaker: bool


def classify(session_status: str, close_reason: str | None) -> Outcome | None:
    """What a terminal session means for the recipient it was placed for.

    ``None`` when the session is not terminal — a call still queued or running.
    A call whose worker died is never handed here in that state: the reconcile
    fails it ``stale`` first, and passes the ``('failed', 'stale')`` pair.

    A ``completed`` session means the person was reached, and that is the end of
    it forever: the never-call-twice invariant outranks every retry rule. The one
    exception is a call that completed on a machine — ``voicemail`` connected and
    was billed, but nobody was reached, so it is retried like a missed call.
    """
    if close_reason == VOICEMAIL:
        return Outcome(reached=False, retryable=True, counts_toward_breaker=False)
    if session_status == "completed":
        return Outcome(reached=True, retryable=False, counts_toward_breaker=False)
    if session_status not in ("failed", "canceled"):
        return None

    bucket = bucket_for(close_reason)
    if bucket in ("normal", "transferred"):
        # A terminal-failed session cannot carry one of these reasons today
        # (`is_failed_close_reason` and the buckets are built from the same
        # naming rule), but classifying by bucket rather than by exception keeps
        # this total if that ever changes.
        return Outcome(reached=True, retryable=False, counts_toward_breaker=False)
    if bucket == "caller_unreachable":
        return Outcome(
            reached=False,
            retryable=(close_reason or "").strip().lower() not in _TERMINAL_UNREACHABLE,
            counts_toward_breaker=False,
        )
    # carrier_fault is retryable; configuration / platform / other are final.
    # `other` is an ending no version of `close_reasons` has mapped, on a session
    # that failed — ten of those in a row is a systemic problem whatever it turns
    # out to be, so it counts toward the breaker like the rest.
    return Outcome(
        reached=False,
        retryable=bucket == "carrier_fault",
        counts_toward_breaker=bucket in _BREAKER_BUCKETS,
    )


async def apply_outcome(
    conn,
    *,
    batch: CallBatch,
    recipient_id: UUID,
    attempts: int,
    outcome: Outcome,
    close_reason: str | None,
) -> bool:
    """Write one attempt's result, on ``conn``, inside the caller's transaction.

    Returns whether it matched — ``False`` means the other writer got there
    first, and the caller must not double-count anything on the batch row.

    Five rules, all consequences of having two writers:

    1. ``WHERE status = 'dialing'`` is what makes the second writer a no-op.
    2. The breaker counter moves in this same transaction, and only if the
       recipient update matched. Otherwise two settlements of one call would
       count two failures.
    3. It never writes batch *status*. The dispatcher owns policy: this may push
       ``consecutive_setup_failures`` to the threshold, and the dispatcher's next
       pass is what fails the batch and fires ``batch.failed``.
    4. ``attempts`` is never touched here. It is incremented exactly once per
       attempt, at claim time.
    5. It never reads or waits on billing. A recipient's outcome is a function of
       the session's close reason alone.
    """
    retry = (
        outcome.retryable
        and attempts < batch.max_attempts
        and batch.status in DIALABLE_BATCH_STATUSES
    )
    status = "pending" if retry else ("completed" if outcome.reached else "failed")
    matched = await conn.fetchval(
        """
        UPDATE call_batch_recipients
        SET status = $3,
            next_attempt_at = CASE
                WHEN $3 = 'pending' THEN now() + make_interval(mins => $4)
                ELSE NULL
            END,
            last_close_reason = $5,
            updated_at = now()
        WHERE id = $1 AND tenant_id = $2 AND status = 'dialing'
        RETURNING id
        """,
        recipient_id,
        batch.tenant_id,
        status,
        batch.retry_after_minutes,
        close_reason,
    )
    if matched is None:
        return False
    await record_setup_outcome(
        conn,
        batch_id=batch.id,
        tenant_id=batch.tenant_id,
        failed=outcome.counts_toward_breaker,
        reason=describe(close_reason),
    )
    return True


async def record_setup_outcome(
    conn, *, batch_id: UUID, tenant_id: UUID, failed: bool, reason: str
) -> int:
    """Move the batch's rolling breaker counter, and say why. Returns the count.

    Called from :func:`apply_outcome` for a settled attempt, and from the
    dispatcher for a failure that happened *before* any recipient was claimed —
    an unpublished agent, a deleted number, a creator who has left the
    organization. Both are "the batch could not do its job", and both must move
    one counter, or the breaker would only notice half of them.

    ``failure_reason`` says what most recently went wrong and is cleared by any
    good outcome — except on a batch the breaker has already stopped, where it is
    the verdict and must survive a late settlement landing behind it.

    Not for a pass that stopped on a state which fixes itself — see
    :func:`note_pause_reason`, which writes the same column and deliberately
    leaves the counter alone.
    """
    return await conn.fetchval(
        """
        UPDATE call_batches
        SET consecutive_setup_failures =
                CASE WHEN $3 THEN consecutive_setup_failures + 1 ELSE 0 END,
            failure_reason = CASE
                WHEN $3 THEN $4
                WHEN status = 'failed' THEN failure_reason
                ELSE NULL
            END,
            updated_at = now()
        WHERE id = $1 AND tenant_id = $2
        RETURNING consecutive_setup_failures
        """,
        batch_id,
        tenant_id,
        failed,
        reason,
    )


async def note_pause_reason(conn, *, batch_id: UUID, tenant_id: UUID, reason: str) -> None:
    """Say why this pass dialled nothing, without moving the breaker counter.

    Deliberately not :func:`record_setup_outcome`. Every failure that one records
    is a configuration problem that will repeat identically for ever — an
    unpublished agent, a dead credential — which is precisely what the breaker
    exists to stop after ten. Being out of credits is a state that fixes itself
    with a payment, and failing the batch permanently would mean a top-up could
    not resume it.

    ``failure_reason`` is cleared by any good outcome, so a resumed batch clears
    this on its next successful dial with no extra code.
    """
    await conn.execute(
        """
        UPDATE call_batches
        SET failure_reason = $3, updated_at = now()
        WHERE id = $1 AND tenant_id = $2
        """,
        batch_id,
        tenant_id,
        reason,
    )


async def settle_recipient(tenant: Tenant, session_id: UUID) -> None:
    """Land a finished call on its batch recipient. Never raises into its caller.

    Called from the voice worker's finalize, immediately after
    ``finalize_session`` — not after billing. ``finalize_session`` writes the
    session's terminal ``status`` and ``close_reason``, and those two facts are
    the whole of what settlement reads, so putting it there frees the recipient's
    concurrency slot without waiting on the recording upload, the analysis LLM
    call and the pricing pass that follow.

    A call that is not part of a batch costs one indexed lookup and returns.
    """
    try:
        pool = await db.tenant_pool(tenant)
        async with pool.acquire() as conn:
            async with conn.transaction():
                session = await conn.fetchrow(
                    """
                    SELECT batch_id, status, close_reason
                    FROM sessions
                    WHERE id = $1 AND tenant_id = $2
                    """,
                    session_id,
                    tenant.id,
                )
                if session is None or session["batch_id"] is None:
                    return
                outcome = classify(session["status"], session["close_reason"])
                if outcome is None:
                    return
                recipient = await conn.fetchrow(
                    """
                    SELECT id, attempts
                    FROM call_batch_recipients
                    WHERE tenant_id = $1 AND session_id = $2 AND status = 'dialing'
                    """,
                    tenant.id,
                    session_id,
                )
                if recipient is None:
                    return
                batch_row = await conn.fetchrow(
                    f"SELECT {BATCH_COLUMNS} FROM call_batches WHERE id = $1 AND tenant_id = $2",
                    session["batch_id"],
                    tenant.id,
                )
                if batch_row is None:
                    return
                await apply_outcome(
                    conn,
                    batch=CallBatch.model_validate(dict(batch_row)),
                    recipient_id=recipient["id"],
                    attempts=recipient["attempts"],
                    outcome=outcome,
                    close_reason=session["close_reason"],
                )
    except Exception:
        # A problem in a batch table must never fail a call's finalize — and it
        # does not have to, because the dispatcher's reconcile settles the row on
        # its next pass. That is the whole reason the reconcile exists.
        logger.exception("settling batch recipient for session %s failed", session_id)


async def reconcile_batch(tenant: Tenant, batch: CallBatch) -> int:
    """Settle every `dialing` recipient of one batch whose call is over.

    Usually a no-op: the worker hook above normally gets there first. Bounded by
    ``max_concurrency`` rows, scoped to one batch, and served by
    ``idx_batch_recipients_batch_status`` — the step that would silently become a
    full scan if it were ever written tenant-wide.

    "Over" is either branch of the ``WHERE``: the session went terminal, or it
    has sat un-terminal past ``CALL_STALE_HOURS`` and its worker is gone.
    ``created_at`` rather than ``started_at`` is safe *for these rows only* —
    ``dial.record_outbound_call`` mints the session ``queued`` inside the claim
    transaction and a worker promotes it within seconds, so the two are the same
    instant here. They are not the same instant in general: a web call's
    ``started_at`` is stamped when the browser redeems its token.
    """
    pool = await db.tenant_pool(tenant)
    settled = 0
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT r.id, r.attempts, r.session_id, s.status AS session_status, s.close_reason
            FROM call_batch_recipients r
            JOIN sessions s ON s.id = r.session_id AND s.tenant_id = r.tenant_id
            WHERE r.tenant_id = $1 AND r.batch_id = $2 AND r.status = 'dialing'
              AND (
                    s.status IN ('completed', 'failed', 'canceled')      -- the call ended
                 OR (s.status IN ('running', 'queued')                   -- …or its worker died
                     AND s.created_at < now() - make_interval(hours => $3))
                  )
            """,
            tenant.id,
            batch.id,
            CALL_STALE_HOURS,
        )
        for row in rows:
            async with conn.transaction():
                status, close_reason = row["session_status"], row["close_reason"]
                if status not in ("completed", "failed", "canceled"):
                    if not await _fail_stale_session(conn, tenant, row["session_id"]):
                        # It went terminal between the SELECT and here. Rule 2:
                        # never read-then-write — the CAS refused, so believe it
                        # and let the next pass settle the real outcome rather
                        # than reporting a finished call as abandoned.
                        continue
                    # `classify` and `apply_outcome` are unchanged and receive
                    # exactly the pair they always did for an abandoned call.
                    status, close_reason = "failed", "stale"
                outcome = classify(status, close_reason)
                if outcome is None:
                    continue
                if await apply_outcome(
                    conn,
                    batch=batch,
                    recipient_id=row["id"],
                    attempts=row["attempts"],
                    outcome=outcome,
                    close_reason=close_reason,
                ):
                    settled += 1
    if settled:
        logger.info("batch %s: reconciled %d finished call(s)", batch.id, settled)
    return settled


async def _fail_stale_session(conn, tenant: Tenant, session_id: UUID) -> bool:
    """End a call whose worker died, so its recipient can be settled.

    ``False`` means it was already terminal — the caller must then not treat it
    as abandoned.

    Deliberately does **not** price it or announce ``session.completed``: the
    worker that would have is gone, and re-deriving both from a sweep is what
    used to mis-price healthy calls and announce them twice. Losing our platform
    fee on a crashed call is the accepted cost.
    """
    failed = await conn.fetchval(
        """
        UPDATE sessions
        SET status = 'failed', close_reason = 'stale',
            ended_at = COALESCE(ended_at, now()), updated_at = now()
        WHERE id = $1 AND tenant_id = $2 AND status IN ('running', 'queued')
        RETURNING id
        """,
        session_id,
        tenant.id,
    )
    if failed is None:
        return False
    logger.warning("session %s lost its worker; failed as stale", session_id)
    return True
