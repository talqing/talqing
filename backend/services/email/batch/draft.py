"""Drafting: one long-lived coroutine that holds a batch until its last row.

**There is no discovery loop and no lock of any kind here.** The two questions a
bespoke loop exists to answer — *when does this run* and *which container runs
it* — are the two `scheduled_jobs` already answers: `scheduled_at` decides when,
Kafka delivers to one consumer, and `store.claim` makes a duplicate delivery a
no-op.

**Ownership splits by status, and everything else falls out of that rule.**
A `scheduled` batch is the SCHEDULER's: its job is deferred to `start_at` and
nothing is running. A `drafting` batch is a COROUTINE's: the job row is
`running`, its lease is renewed every 40 s, and this loop is inside it. The loop
ends in exactly one of three ways:

- **drafted** — the last row landed, and nothing about this batch is scheduled
  any more. A 10 000-row upload is one job that runs to the end and stops
  existing;
- **stopped** — a human paused or cancelled it, or it failed. Nothing stays alive
  across a human decision; `resume` is what brings it back;
- **deferred to `start_at`** — the one case the scheduler should own rather than
  a coroutine sleeping until Monday.

**The loop body always re-derives what to do now.** Nothing is held across an
iteration except the in-flight task runs. Every iteration re-reads the batch row;
every `recheck_seconds` it also re-reads the task's published version, re-running
`check_field_map` only when that number moved. So a pause, a publish or a pacing
edit reaches a batch mid-list within five minutes, with nothing to signal and
nothing to wake.

**Authorization is asked once, at the start.** `run_draft_job` checks that the
creator is still an ADMIN or EDITOR before the batch is moved to `drafting` —
the same check an HTTP request makes, and asked the same way a request is: when
the work arrives, not continuously while it runs. A batch that is already
drafting finishes the list it began.

**Waiting is a chunked sleep, never one long one.** A `draft_gap_seconds` of an
hour used to hold a claimed job through a single `sleep`, where a pause was
invisible until it ended. Now every wait is `min(delay, recheck_seconds)`
followed by another turn of the loop.

**Drafting has no business-hours window**, and adding one back would be a
mistake rather than a feature: the window on a batch means "when may email
leave", it is carried by a send run, and nobody receives a draft. Restricting
the hours a draft is *written* in gates nothing and only makes a list take a
week.

Three rules this holds, each of which is a way it could quietly go wrong:

1. **Every state change is a compare-and-set** whose ``WHERE`` names the state it
   expects — including the exit, which re-checks that the batch is still stopped
   so a `resume` landing in the same millisecond cannot leave a live batch with
   no owner. The finish and the exit both run under the batch row's lock, the
   one an append holds while it adds rows.
2. **The row claim asks three questions in one `WHERE`:** is there work, is the
   batch still `drafting`, and do I still hold this job. The last is what stops
   two loops existing after a lease lapse under a container that then recovers.
3. **The claim and the `task_runs` insert are one transaction, and `run_task` is
   outside it.** A row is `drafting` if and only if it has a `task_run_id` — a
   CHECK constraint, not a promise — which is why there is no orphan sweep.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from datetime import UTC, datetime, timedelta
from typing import NamedTuple
from uuid import UUID, uuid4

import asyncpg

import db
from services import webhooks
from services.jobs import JobContext
from services.tasks import TaskConfig, open_task_run, run_task
from services.user import ROLE_ADMIN, ROLE_EDITOR, NotAMember, Tenant, load_context
from services.webhooks import events
from settings import get_settings

from .models import RECIPIENT_COLUMNS, EmailBatch, EmailRecipient, resolve_send_fields, task_vars
from .service import batch_response, check_field_map, load_batch

logger = logging.getLogger("talqing.email.batch")

# Ten failures in a row is not bad luck, it is a broken batch. Rolling rather
# than cumulative and rolling rather than prefix: a wrong prompt or a flaky MCP
# server fails mid-batch at least as often as at the start.
BREAKER_THRESHOLD = 10

# A `drafting` row whose run started longer ago than the task's own timeout plus
# this margin belongs to a process that died. Not a guess: `run_task` bounds
# itself with `asyncio.wait_for`, so a live run cannot exceed its timeout, and
# anything that has is gone.
DEAD_RUN_MARGIN_SECONDS = 60

# Outcomes worth another go. `no_output` and `step_limit` produce the same
# non-answer on a second run; `missing_vars` and `configuration` describe
# something no retry can change. `canceled` is here for completeness rather than
# for reach — `_run_one` catches the cancellation before it ever reads an
# outcome — but a set that named the retryable failures and left one out would be
# wrong on its own terms.
_RETRYABLE = frozenset({"provider_error", "timeout", "platform", "canceled"})
# The one failure that must not touch the breaker: a blank cell in a required
# column is a fact about THAT ROW'S DATA, not about the batch. Ten blank cells
# in a row would otherwise stop a batch whose other 9 990 rows are fine — and it
# costs no tokens, because the check runs before the model is reached.
_NOT_THE_BATCHS_FAULT = frozenset({"missing_vars"})

# How long to wait when the CONTROL PLANE, not the batch, is what failed: the
# membership lookup errored, so we know nothing about whether this batch may run
# at all. Deferred rather than failed — a database blip must not kill live
# batches — and deferred rather than slept, because a claim held while we know
# nothing is a claim another container could be using.
CONTROL_PLANE_RETRY_SECONDS = 60


async def run_draft_job(tenant: Tenant, args: dict, job: JobContext) -> datetime | None:
    """Draft this batch to the end. Returns ``None``, or `start_at` if too early.

    The one `datetime` it can return is the scheduler-owned case: a `scheduled`
    batch whose moment has not come. Everything else — a retry backoff, the gap
    between two claims — is slept through here, in chunks, so a policy change
    reaches the loop within `recheck_seconds`.
    """
    batch_id = UUID(str(args["batch_id"]))
    pool = await db.tenant_pool(tenant)
    batch = await load_batch(pool, tenant.id, batch_id)
    if batch is None:
        logger.info("email batch %s is gone; nothing to draft", batch_id)
        return None
    if batch.status in ("paused", "canceled", "failed", "drafted"):
        logger.info("email batch %s is %s; this job is finished", batch_id, batch.status)
        return None

    if batch.status == "scheduled":
        if batch.start_at is not None and batch.start_at > datetime.now(UTC):
            # Hand it back to the scheduler rather than sleeping until Monday:
            # `scheduled` means the scheduler owns it, and a coroutine parked for
            # three days is a claim held for nothing. Reachable when somebody
            # moved `start_at` forward without moving the job, and on the first
            # invocation of a batch created with a future start.
            await _announce(pool, batch, batch.start_at, "start")
            return batch.start_at

    # Before the batch is moved to `drafting`, so a control-plane blip leaves it
    # `scheduled` rather than `drafting` with nobody on it.
    allowed = await _creator_still_allowed(tenant, batch)
    if allowed is False:
        return None
    if allowed is None:
        return datetime.now(UTC) + timedelta(seconds=CONTROL_PLANE_RETRY_SECONDS)

    if batch.status == "scheduled":
        batch = await _step_start(tenant, batch)

    # There is deliberately NO business-hours check here. The window on a batch
    # means "when may email leave", it belongs to a send run, and drafting is not
    # gated by it: nobody receives a draft, so restricting the hours one is
    # written in gates nothing and would only make a list take a week.

    resolved = await _resolve_task(tenant, pool, batch)
    if resolved is None:
        return None
    await _settle_dead_runs(pool, batch, resolved.config.timeout_seconds)
    await _draft_loop(tenant, pool, batch, resolved, job)
    return None


# ── the steps ───────────────────────────────────────────────────────────────


async def _step_start(tenant: Tenant, batch: EmailBatch) -> EmailBatch:
    """A scheduled batch whose moment has come."""
    pool = await db.tenant_pool(tenant)
    row = await pool.fetchrow(
        """
        UPDATE email_batches
        SET status = 'drafting', started_at = COALESCE(started_at, now()),
            next_draft_at = NULL, next_draft_reason = NULL, updated_at = now()
        WHERE id = $1 AND tenant_id = $2 AND status = 'scheduled'
          AND (start_at IS NULL OR start_at <= now())
        RETURNING id
        """,
        batch.id,
        batch.tenant_id,
    )
    if row is None:
        return batch
    fresh = await load_batch(pool, batch.tenant_id, batch.id)
    assert fresh is not None
    logger.info("email batch %s: started drafting", batch.id)
    await _emit(tenant, events.EMAIL_BATCH_STARTED, fresh)
    return fresh


class PublishedTask(NamedTuple):
    """The definition this loop drafts with, and which version it is."""

    config: TaskConfig
    version: int


async def _creator_still_allowed(tenant: Tenant, batch: EmailBatch) -> bool | None:
    """May this batch spend the workspace's money? ``None`` if we cannot tell.

    **The membership join is the authorization check** — the same one an HTTP
    request makes, and asked ONCE per invocation for the same reason: a request
    is authorized when it arrives, not continuously while it runs. A batch whose
    creator has left the organization, or been demoted to VIEWER, will not start
    or restart — a departed employee's list must not keep spending the
    workspace's money on the workspace's Resend account — and one already
    drafting finishes the list it began.

    A control-plane query that *errors* is the opposite verdict — transient — and
    conflating the two would let a database blip kill live batches, so it returns
    ``None`` and the caller defers.

    ``False`` means the batch has already been failed with a reason.
    """
    try:
        ctx = await load_context(tenant, batch.created_by_user_id)
    except NotAMember:
        await _fail_batch(
            tenant, batch, "the person who created this batch is no longer in this organization"
        )
        return False
    except Exception:
        logger.exception("email batch %s: could not load its creator; waiting", batch.id)
        return None
    if ctx.user.role not in (ROLE_ADMIN, ROLE_EDITOR):
        await _fail_batch(
            tenant, batch, f"{ctx.user.email} no longer has permission to run this batch"
        )
        return False
    return True


async def _resolve_task(
    tenant: Tenant, pool: asyncpg.Pool, batch: EmailBatch
) -> PublishedTask | None:
    """The published task to draft with, or ``None`` once the batch has been failed.

    The PUBLISHED version: a batch follows the task's current published
    definition, the way a call batch follows the agent's.

    `check_field_map` runs here and nowhere else in this file. `publish_task` and
    `rollback_task_version` refuse the common case at the source — they will not
    make a variable required that this batch has no column for, or drop an output
    field its mapping names — but a publish mid-batch can still move ground they
    cannot see, and the alternative is four thousand rows that draft fine and turn
    out to be unsendable at the review gate.
    """
    if batch.task_id is None:
        await _fail_batch(tenant, batch, "the task that drafts this batch has been deleted")
        return None
    row = await pool.fetchrow(
        "SELECT v.version, v.config FROM agent_tasks t "
        "JOIN agent_task_versions v ON v.task_id = t.id AND v.tenant_id = t.tenant_id "
        "  AND v.version = t.published_version "
        "WHERE t.id = $1 AND t.tenant_id = $2",
        batch.task_id,
        batch.tenant_id,
    )
    if row is None:
        await _fail_batch(tenant, batch, "the task that drafts this batch has been deleted")
        return None
    task_config = TaskConfig.model_validate(row["config"])
    if problems := check_field_map(batch.field_map, batch.input_columns, task_config):
        await _fail_batch(tenant, batch, problems[0])
        return None
    return PublishedTask(task_config, row["version"])


async def _republished(pool: asyncpg.Pool, batch: EmailBatch, holding: PublishedTask) -> bool:
    """Has the task been published or rolled back since we read it?

    One integer off an indexed row, asked on the recheck tick. The config and the
    field-map re-check only follow when the answer is yes — a batch that drafts
    for six hours against one unchanged version should not re-validate a mapping
    nothing has touched every five minutes. ``True`` also covers "the task is
    gone", which `_resolve_task` then turns into a failed batch with a sentence.
    """
    version = await pool.fetchval(
        "SELECT published_version FROM agent_tasks WHERE id = $1 AND tenant_id = $2",
        batch.task_id,
        batch.tenant_id,
    )
    return version != holding.version


async def _settle_dead_runs(
    pool: asyncpg.Pool, batch: EmailBatch, task_timeout_seconds: int
) -> None:
    """Free rows whose worker was killed mid-run.

    The only crash tail drafting has, and it covers two shapes. A container that
    was SIGKILLed leaves the `task_runs` row `running` and no longer advancing,
    so a row whose run started longer ago than the task could possibly take
    belongs to a process that is gone. A kill that landed *between* `run_task`
    writing the run row and `_run_one` writing the recipient leaves a settled run
    behind a `drafting` recipient, which no API verb can move and which would
    otherwise hold the batch short of `drafted` for ever.

    **The row comes back UNSPENT**, matching `store.release` and the cancel path
    in `_run_one`: a worker dying is our infrastructure failing, not the tenant's
    task failing. A graceful shutdown never reaches here at all — the runs cancel
    and settle themselves.
    """
    settled = await pool.fetch(
        """
        UPDATE email_batch_recipients r
        SET status = 'pending', attempts = GREATEST(r.attempts - 1, 0),
            next_attempt_at = NULL,
            last_error = 'the worker drafting this row stopped before it finished',
            updated_at = now()
        FROM task_runs t
        WHERE r.tenant_id = $1 AND r.batch_id = $2 AND r.status = 'drafting'
          AND t.id = r.task_run_id AND t.tenant_id = r.tenant_id
          AND (
            -- The run settled and the row did not: a kill landed between
            -- `run_task` writing `task_runs` and `_run_one` writing this row.
            t.status <> 'running'
            -- Or the run is older than it could possibly still be: `run_task`
            -- bounds itself with `wait_for`, so anything past that is gone.
            OR t.started_at < now() - make_interval(secs => $3)
          )
        RETURNING r.id
        """,
        batch.tenant_id,
        batch.id,
        # Read off the task's own bound rather than assumed: a task with a
        # 600-second timeout must not have its live runs declared dead.
        float(task_timeout_seconds + DEAD_RUN_MARGIN_SECONDS),
    )
    if settled:
        logger.warning(
            "email batch %s: released %d row(s) whose drafting worker died", batch.id, len(settled)
        )


async def _draft_loop(
    tenant: Tenant,
    pool: asyncpg.Pool,
    batch: EmailBatch,
    task: PublishedTask,
    job: JobContext,
) -> None:
    """Draft rows until the list is done or somebody stops it.

    **One exit, and it is a compare-and-set.** Every path out of this loop goes
    through the status re-read at the top: a failure, a finish and a human's
    pause all write the batch row and then let the next turn notice. That is what
    makes `_hand_back` the single place a live batch can be left without an owner,
    and it refuses to be that place.

    **A slot is refilled the moment it frees.** `draft_concurrency` is the only
    limit in the system and ten is not arbitrary: a research task fans out to a
    web-search MCP and to RocketReach, both of which rate-limit and one of which
    spends credits per lookup. It is held by the claim and by nothing else —
    `enqueue_once` allows one job per batch and `store.claim` one runner per job,
    so exactly one loop is counting.

    **A shutdown cancels the runs rather than waiting them out.** Each settles
    its own `task_runs` row to `canceled` and hands its recipient back unspent,
    so the batch resumes on the next container with nothing stranded.
    """
    recheck = float(get_settings().email.recheck_seconds)
    running: set[asyncio.Task[None]] = set()
    # Monotonic, not wall-clock: both are about durations this process measured,
    # and a clock step must not collapse a gap or skip a re-check. Starts now
    # because `run_draft_job` has just done everything the tick does.
    checked_at = time.monotonic()
    claim_allowed_at = 0.0
    try:
        while True:
            fresh = await load_batch(pool, batch.tenant_id, batch.id)
            if fresh is None or fresh.status != "drafting":
                if await _hand_back(pool, job, batch):
                    return
                # A resume or a redraft landed between the write that stopped
                # this batch and the line above. It is live again and this job is
                # still the one holding it, so carry on rather than leaving work
                # with nothing scheduled.
                continue
            batch = fresh

            if time.monotonic() - checked_at >= recheck:
                if await _republished(pool, batch, task):
                    refreshed = await _resolve_task(tenant, pool, batch)
                    if refreshed is None:
                        continue  # failed; ditto
                    logger.info(
                        "email batch %s: task moved v%d -> v%d mid-list",
                        batch.id,
                        task.version,
                        refreshed.version,
                    )
                    task = refreshed
                await _settle_dead_runs(pool, batch, task.config.timeout_seconds)
                checked_at = time.monotonic()

            delay = recheck
            if len(running) < batch.draft_concurrency:
                # The gap is a PERIOD between two STARTS, and it is honoured
                # across iterations rather than slept in one go: at its 3 600 s
                # ceiling a single `sleep` would hold this batch deaf to a pause
                # for an hour.
                gap_left = claim_allowed_at - time.monotonic()
                if gap_left > 0:
                    delay = min(gap_left, recheck)
                else:
                    recipient = await _claim(pool, batch, task, job)
                    if recipient is not None:
                        await _announce(pool, batch, None, None)
                        running.add(
                            asyncio.create_task(_run_one(tenant, pool, batch, task, recipient))
                        )
                        claim_allowed_at = time.monotonic() + batch.draft_gap_seconds
                        continue
                    delay = await _nothing_claimable(
                        tenant, pool, batch, job, working=bool(running)
                    )
                    if delay is None:
                        continue  # drafted, or no longer ours; the re-read exits
                    delay = min(delay, recheck)
            running = await _wait(running, max(delay, 0.0))
    except asyncio.CancelledError:
        # A shutdown. `task.cancel()` on the coroutine that spawned these does
        # NOT reach them, which is why this is here and not implied by the cancel
        # that delivered us this exception.
        for in_flight in running:
            in_flight.cancel()
        raise
    finally:
        if running:
            # Both paths land here: cancelled just above, or a clean exit with
            # runs still settling. Either way these were started and must be
            # allowed to put their rows down — a `drafting` row whose settlement
            # never ran is one `_settle_dead_runs` has to clean up a task-timeout
            # later. Awaiting in a `finally` after a cancellation is what actually
            # runs: the delivered `CancelledError` clears the `_must_cancel` flag.
            await asyncio.gather(*running, return_exceptions=True)


async def _wait(running: set[asyncio.Task[None]], seconds: float) -> set[asyncio.Task[None]]:
    """Wait for a run to finish, or for `seconds`, whichever comes first.

    The loop's one waiting primitive. Bounding it by the caller's delay is what
    keeps `recheck_seconds` an actual promise: with five 300-second runs in
    flight, an unbounded `FIRST_COMPLETED` would be five minutes deaf to a pause.
    """
    if not running:
        await asyncio.sleep(seconds)
        return running
    _, pending = await asyncio.wait(running, timeout=seconds, return_when=asyncio.FIRST_COMPLETED)
    return pending


async def _nothing_claimable(
    tenant: Tenant, pool: asyncpg.Pool, batch: EmailBatch, job: JobContext, *, working: bool
) -> float | None:
    """Nothing to take right now. How long to wait — or ``None`` to leave the loop.

    Four ways to have nothing claimable, and they are not the same thing: every
    row is finished (so the batch is drafted), every remaining row is waiting out
    its `next_attempt_at`, rows are claimable and this container no longer holds
    the job, or runs are simply still in flight and there is nothing to add.
    """
    left = await pool.fetchrow(
        """
        SELECT
            EXISTS (
                SELECT 1 FROM email_batch_recipients
                WHERE tenant_id = $1 AND batch_id = $2 AND status = 'drafting'
            ) AS in_flight,
            EXISTS (
                SELECT 1 FROM email_batch_recipients
                WHERE tenant_id = $1 AND batch_id = $2 AND status = 'pending'
            ) AS pending,
            (
                SELECT min(next_attempt_at) FROM email_batch_recipients
                WHERE tenant_id = $1 AND batch_id = $2 AND status = 'pending'
            ) AS next_due
        """,
        batch.tenant_id,
        batch.id,
    )
    if not left["in_flight"] and not left["pending"]:
        await _finish(tenant, pool, batch)
        # Drafted, or an append landed first and there is work again; the
        # re-read decides which.
        return None
    if left["pending"] and left["next_due"] is not None:
        await _announce(pool, batch, left["next_due"], "retry")
        return (left["next_due"] - datetime.now(UTC)).total_seconds()
    if left["pending"] and not await _still_ours(pool, batch.tenant_id, job):
        # Rows are claimable and the claim refused them, so the `WHERE` clause
        # that failed was the ownership one: this container's lease lapsed and
        # somebody else has the job. Stop rather than spinning against it.
        logger.warning("email batch %s: job %s is no longer ours; stopping", batch.id, job.id)
        return None
    # Runs are in flight and there is nothing to add, or a claimable row was
    # locked for an instant. Working rather than waiting, so nothing is
    # announced; the caller clamps this to `recheck_seconds` and wakes on the
    # first completion anyway.
    return float("inf") if working else 1.0


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


async def _claim(
    pool: asyncpg.Pool, batch: EmailBatch, task: PublishedTask, job: JobContext
) -> EmailRecipient | None:
    """Take one row and record the run that will draft it, in one transaction.

    **One query answers three questions:** is there work, is this batch still
    `drafting`, and do we still hold this job. The second is what makes a pause
    take hold on the next row rather than at the end of something; the third is
    what stops two loops existing after a lease lapsed under a container that
    then recovered — a 60-second window when a pass was 60 seconds long, and an
    hours-long one now.

    ``attempts`` increments exactly once per attempt, here and never at
    settlement — getting that wrong in either direction gives you an infinite
    retry loop or a person who is silently never contacted.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                f"""
                SELECT {RECIPIENT_COLUMNS} FROM email_batch_recipients r
                WHERE r.tenant_id = $1 AND r.batch_id = $2 AND r.status = 'pending'
                  AND (r.next_attempt_at IS NULL OR r.next_attempt_at <= now())
                  AND EXISTS (
                    SELECT 1 FROM email_batches b
                    WHERE b.id = $2 AND b.tenant_id = $1 AND b.status = 'drafting'
                  )
                  AND EXISTS (
                    SELECT 1 FROM scheduled_jobs j
                    WHERE j.id = $3 AND j.tenant_id = $1 AND j.claimed_by = $4
                      AND j.lease_expires_at > now()
                  )
                ORDER BY r.next_attempt_at NULLS FIRST, r.row_number
                LIMIT 1
                FOR UPDATE OF r SKIP LOCKED
                """,
                batch.tenant_id,
                batch.id,
                job.id,
                job.worker,
            )
            if row is None:
                return None
            recipient = EmailRecipient.model_validate(dict(row))
            run_id = uuid4()
            await open_task_run(
                conn,
                run_id=run_id,
                tenant_id=batch.tenant_id,
                task_id=batch.task_id,
                task_name=task.config.name,
                task_version=task.version,
                vars=task_vars(batch, recipient),
                created_by=batch.created_by_user_id,
            )
            await conn.execute(
                """
                UPDATE email_batch_recipients
                SET status = 'drafting', task_run_id = $3, attempts = attempts + 1,
                    next_attempt_at = NULL, updated_at = now()
                WHERE id = $1 AND tenant_id = $2 AND status = 'pending'
                """,
                recipient.id,
                batch.tenant_id,
                run_id,
            )
    return recipient.model_copy(
        update={"status": "drafting", "task_run_id": run_id, "attempts": recipient.attempts + 1}
    )


async def _run_one(
    tenant: Tenant,
    pool: asyncpg.Pool,
    batch: EmailBatch,
    task: PublishedTask,
    recipient: EmailRecipient,
) -> None:
    """Run the task for one row and settle it. Raises only when cancelled.

    Settlement is in-process — unlike batch calling's, where a dial's outcome
    arrives minutes later on another worker — so the pass that started the run is
    the one that finishes it, and there is no second writer to reconcile with.

    **A cancellation propagates and nothing else does.** `_draft_loop` gathers
    these without reading their results, so anything else escaping would be an
    exception nobody retrieves; the settling writes are inside the guard for
    exactly that reason, and a row a failed settle leaves `drafting` is one
    `_settle_dead_runs` picks up on a later tick.
    """
    assert recipient.task_run_id is not None
    try:
        # Nested so that the machinery-failed branch stays specific to
        # `run_task`: the outer guard covers this row's settling writes too.
        try:
            outcome = await run_task(
                tenant=tenant,
                cfg=task.config,
                # The same variables `_claim` recorded on the run row, so the
                # trace modal shows what this run was actually given — see
                # `task_vars` for why an edited cell belongs in here.
                vars=task_vars(batch, recipient),
                run_id=recipient.task_run_id,
                task_id=batch.task_id,
                task_version=task.version,
                created_by=batch.created_by_user_id,
                # The claim already opened it, in the transaction that took the row.
                run_opened=True,
            )
        except Exception as exc:
            # `run_task` classifies everything it can and returns; reaching here
            # means the machinery around it failed. Ours, and retryable.
            logger.exception("email batch %s row %s: the run itself failed", batch.id, recipient.id)
            await _settle_failure(
                pool, batch, recipient, "platform", f"{type(exc).__name__}: {exc}"
            )
            await _record_draft_outcome(
                pool, batch, outcome="failed", reason="the run could not be made"
            )
            return

        if outcome.status == "completed" and outcome.output is not None:
            # **`draft` means a draft that can be SENT**, and this is the one
            # place the invariant is established at scale. A task that ran
            # perfectly and found no address — the enrichment provider has never
            # heard of this number — produced a row nobody can email, and calling
            # it a draft makes every count, label and bulk selection downstream
            # say a number the operator cannot act on. So the row settles by what
            # it holds rather than by whether the run finished.
            #
            # The output is stored either way: the other columns, the trace and
            # the operator's chance to type the address in themselves all depend
            # on it, and `service.reconcile_drafts` turns the row back into a
            # draft the moment a cell makes it sendable.
            _, unsendable = resolve_send_fields(
                batch, recipient.model_copy(update={"output": outcome.output})
            )
            await pool.execute(
                """
                UPDATE email_batch_recipients
                SET status = CASE WHEN $4::text IS NULL THEN 'draft' ELSE 'draft_failed' END,
                    output = $3::jsonb, last_error = $4,
                    next_attempt_at = NULL, updated_at = now()
                WHERE id = $1 AND tenant_id = $2 AND status = 'drafting'
                """,
                recipient.id,
                batch.tenant_id,
                json.dumps(outcome.output),
                unsendable,
            )
            # Not a failure of the BATCH, on the same reasoning as
            # `_NOT_THE_BATCHS_FAULT`: an enrichment list where two thirds of the
            # numbers are unlisted is the task working, and tripping the breaker
            # on the tenth in a row would stop a batch whose other rows are fine.
            # There is deliberately no retry either — the run answered, and
            # asking it the same question again costs the tenant another run to
            # get the same nothing. `redraft` is how a human asks for that.
            await _record_draft_outcome(pool, batch, outcome="ok", reason=None)
            return

        error_type = outcome.error.type if outcome.error else "platform"
        message = outcome.error.message if outcome.error else "the run produced no result"
        await _settle_failure(pool, batch, recipient, error_type, message)
        if error_type == "configuration":
            # Deterministic: a deleted tool, a missing BYOK key, an integration
            # whose credential no longer resolves. It will fail identically on row
            # 4 001, so the batch stops now rather than proving it four thousand
            # times.
            await _fail_batch(tenant, batch, message)
            return
        await _record_draft_outcome(
            pool,
            batch,
            outcome="neither" if error_type in _NOT_THE_BATCHS_FAULT else "failed",
            reason=message,
        )
        await _trip_breaker_if_due(tenant, batch)
    except asyncio.CancelledError:
        # Torn down mid-run. `run_task` has already settled the `task_runs` row
        # as `canceled`; this hands the recipient back UNSPENT, which is the same
        # distinction `store.release` draws against `record_failure` — a worker
        # dying is our infrastructure failing, not this row's task failing, and
        # it must not spend the tenant's `draft_attempts`.
        #
        # `status = 'drafting'` is not decoration: every state change here is a
        # compare-and-set, and this one must not overwrite a row a later pass has
        # already moved. Suppressed rather than guarded loosely, because the
        # `raise` has to happen even if the pools are already closing — the row
        # is then recovered by `_settle_dead_runs`.
        #
        # The breaker is deliberately untouched: a deploy is neither a success
        # nor a failure of the batch, and moving `consecutive_draft_failures` in
        # either direction would make a deploy either mask a real fault or
        # manufacture one.
        with contextlib.suppress(Exception):
            await pool.execute(
                """
                UPDATE email_batch_recipients
                SET status = 'pending', attempts = GREATEST(attempts - 1, 0),
                    next_attempt_at = NULL, last_error = NULL, updated_at = now()
                WHERE id = $1 AND tenant_id = $2 AND status = 'drafting'
                """,
                recipient.id,
                batch.tenant_id,
            )
        raise
    except Exception:
        logger.exception("email batch %s row %s: could not be settled", batch.id, recipient.id)


async def _settle_failure(
    pool: asyncpg.Pool,
    batch: EmailBatch,
    recipient: EmailRecipient,
    error_type: str,
    message: str,
) -> None:
    """Put a failed row back for another attempt, or leave it for a human."""
    retry = error_type in _RETRYABLE and recipient.attempts < batch.draft_attempts
    await pool.execute(
        """
        UPDATE email_batch_recipients
        SET status = CASE WHEN $4 THEN 'pending' ELSE 'draft_failed' END,
            next_attempt_at = CASE
                WHEN $4 THEN now() + make_interval(mins => $5) ELSE NULL
            END,
            last_error = $3,
            updated_at = now()
        WHERE id = $1 AND tenant_id = $2 AND status = 'drafting'
        """,
        recipient.id,
        batch.tenant_id,
        f"{error_type}: {message}"[:1000],
        retry,
        batch.draft_retry_after_minutes,
    )


async def _record_draft_outcome(
    pool: asyncpg.Pool, batch: EmailBatch, *, outcome: str, reason: str | None
) -> None:
    """Move the rolling breaker counter, and record why if this was a failure.

    Three outcomes, not two, and the third is the interesting one. `ok` resets
    the counter, `failed` increments it, and **`neither` leaves it exactly where
    it is** — for a row that failed on its own data, such as a blank cell in a
    required column. Resetting there would let one bad row in every ten hold the
    breaker open for ever, which is precisely the failure the breaker exists to
    catch; the send side has always stated that reasoning for its row-class
    failures, and this is drafting's half of it.
    """
    await pool.execute(
        """
        UPDATE email_batches
        SET consecutive_draft_failures = CASE $3
                WHEN 'failed' THEN consecutive_draft_failures + 1
                WHEN 'ok' THEN 0
                ELSE consecutive_draft_failures
            END,
            failure_reason = CASE WHEN $3 = 'failed' THEN $4 ELSE failure_reason END,
            updated_at = now()
        WHERE id = $1 AND tenant_id = $2
        """,
        batch.id,
        batch.tenant_id,
        outcome,
        (reason or "")[:1000] or None,
    )


async def _trip_breaker_if_due(tenant: Tenant, batch: EmailBatch) -> None:
    """PAUSE a batch that has failed `BREAKER_THRESHOLD` times in a row.

    **Paused, not failed**, and that is the difference between "come back and
    press Resume" and "reselect four thousand rows and start again". Ten failures
    in a row is a broken batch, but almost never a permanently broken one: a
    flaky MCP server, a provider having a bad ten minutes, a prompt that needs one
    edit. `resume_batch` already clears `failure_reason` and this counter, so
    resume IS the reset and nothing new is needed there.

    `failed` is kept for the deterministic stops — a deleted task, a broken field
    map, a departed creator — where trying again cannot help.

    `failure_reason` is already on the row; whoever recorded the failure wrote it.
    """
    pool = await db.tenant_pool(tenant)
    tripped = await pool.fetchval(
        """
        UPDATE email_batches
        SET status = 'paused', next_draft_at = NULL, next_draft_reason = NULL, updated_at = now()
        WHERE id = $1 AND tenant_id = $2
          AND status IN ('scheduled', 'drafting')
          AND consecutive_draft_failures >= $3
        RETURNING id
        """,
        batch.id,
        batch.tenant_id,
        BREAKER_THRESHOLD,
    )
    if tripped is None:
        return
    fresh = await load_batch(pool, batch.tenant_id, batch.id)
    assert fresh is not None
    logger.error("email batch %s: paused by the breaker — %s", batch.id, fresh.failure_reason)
    await _emit(tenant, events.EMAIL_BATCH_PAUSED, fresh)


async def _fail_batch(tenant: Tenant, batch: EmailBatch, reason: str) -> None:
    """Stop the batch for good, with a sentence somebody can act on.

    Only the deterministic stops reach here — the ones where trying again on row
    4 001 fails identically. The breaker pauses instead; see
    `_trip_breaker_if_due`.
    """
    pool = await db.tenant_pool(tenant)
    stopped = await pool.fetchval(
        """
        UPDATE email_batches
        SET status = 'failed', failure_reason = $3,
            next_draft_at = NULL, next_draft_reason = NULL, updated_at = now()
        WHERE id = $1 AND tenant_id = $2 AND status IN ('scheduled', 'drafting', 'paused')
        RETURNING id
        """,
        batch.id,
        batch.tenant_id,
        reason[:1000],
    )
    if stopped is None:
        return
    fresh = await load_batch(pool, batch.tenant_id, batch.id)
    assert fresh is not None
    logger.error("email batch %s: stopped — %s", batch.id, reason)
    await _emit(tenant, events.EMAIL_BATCH_FAILED, fresh)


async def _finish(tenant: Tenant, pool: asyncpg.Pool, batch: EmailBatch) -> None:
    """The last row landed: stamp `drafted_at` and stop being scheduled.

    Writes the row and fires the event. The loop's next turn re-reads it, sees
    `drafted` and exits through `_hand_back` — which is the whole point of having
    one exit.

    "Nothing left" is asked again under the batch row's lock, which an append
    holds while it inserts: the caller's answer is a statement old, and rows an
    append added since would otherwise sit `pending` on a drafted batch.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT 1 FROM email_batches WHERE id = $1 AND tenant_id = $2 FOR UPDATE",
                batch.id,
                batch.tenant_id,
            )
            drafted = await conn.fetchval(
                """
                UPDATE email_batches
                SET status = 'drafted', drafted_at = now(),
                    next_draft_at = NULL, next_draft_reason = NULL, updated_at = now()
                WHERE id = $1 AND tenant_id = $2 AND status = 'drafting'
                  AND NOT EXISTS (
                    SELECT 1 FROM email_batch_recipients
                    WHERE tenant_id = $2 AND batch_id = $1 AND status IN ('pending', 'drafting')
                  )
                RETURNING id
                """,
                batch.id,
                batch.tenant_id,
            )
    if drafted is None:
        return
    fresh = await load_batch(pool, batch.tenant_id, batch.id)
    assert fresh is not None
    logger.info("email batch %s: drafted (%d rows)", batch.id, fresh.total_recipients)
    # The moment the product needs a human back. Nothing about this batch is
    # scheduled from here until somebody presses send or adds rows.
    await _emit(tenant, events.EMAIL_BATCH_DRAFTED, fresh)


async def _announce(
    pool: asyncpg.Pool, batch: EmailBatch, at: datetime | None, reason: str | None
) -> None:
    """Publish when drafting next acts, for anyone reading the batch.

    Written only when it CHANGES, against the row we just read: a loop pacing at
    one row a second must not add a round trip per row to rewrite the same NULL.
    `scheduled_jobs` used to answer this and cannot any more — a waiting loop
    holds its job `running` the whole time — and it was never the API's table to
    read.
    """
    if batch.next_draft_at == at and batch.next_draft_reason == reason:
        return
    await pool.execute(
        """
        UPDATE email_batches
        SET next_draft_at = $3, next_draft_reason = $4, updated_at = now()
        WHERE id = $1 AND tenant_id = $2
        """,
        batch.id,
        batch.tenant_id,
        at,
        reason,
    )


async def _hand_back(pool: asyncpg.Pool, job: JobContext, batch: EmailBatch) -> bool:
    """Give this job up, unless the batch went live again while we were leaving.

    **The compare-and-set is the point.** Between the loop reading a `paused`
    batch and this line, a `resume` can land — and `enqueue_once` would have
    found this job still `running` and inserted nothing, leaving a live batch
    with nothing scheduled. So the job is only completed while the batch is
    still stopped; ``False`` means it is live again and this loop should carry on
    holding it.

    Completing the row here rather than letting the executor do it is what makes
    that atomic: `store.complete` names the same worker and finds the row already
    `done`, so it is a no-op either way. The batch row is locked first, because a
    compare-and-set alone reads a snapshot: a resume or an append committing
    under it would find this job still `running`, enqueue nothing, and be lost.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT 1 FROM email_batches WHERE id = $1 AND tenant_id = $2 FOR UPDATE",
                batch.id,
                batch.tenant_id,
            )
            released = await conn.fetchval(
                """
                UPDATE scheduled_jobs
                SET status = 'done', last_error = NULL,
                    claimed_by = NULL, lease_expires_at = NULL, updated_at = now()
                WHERE id = $1 AND tenant_id = $2 AND status = 'running' AND claimed_by = $3
                  AND NOT EXISTS (
                    SELECT 1 FROM email_batches
                    WHERE id = $4 AND tenant_id = $2 AND status IN ('scheduled', 'drafting')
                  )
                RETURNING id
                """,
                job.id,
                batch.tenant_id,
                job.worker,
                batch.id,
            )
    if released is not None:
        return True
    # Either the batch is live again, or this job is not ours any more. The
    # second is not a reason to keep looping.
    return not await _still_ours(pool, batch.tenant_id, job)


async def _emit(tenant: Tenant, event: str, batch: EmailBatch) -> None:
    payload = await batch_response(tenant, batch)
    await webhooks.dispatch(tenant, event, None, payload.model_dump(mode="json"))
