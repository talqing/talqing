"""Running due jobs: consume, claim, dispatch to a handler.

Kafka distributes; **the claim decides**. Two consumers can still receive one
message — at-least-once delivery, a rebalance, a scheduler republish — and the
conditional `UPDATE` in `store.claim` is what turns the second one into a no-op.
Nothing here depends on the broker delivering exactly once, because nothing can.

Every kind must be **safe to re-run from the start**, not merely safe to call
twice: a shutdown puts an in-flight job back mid-work, and so does a lapsed
lease. `session.purge` satisfies that trivially — an absent object is a no-op and
every write it makes is a redaction to a fixed value. A long-running kind must
make its progress durable so a re-run resumes rather than repeats, the way batch
dialling does through `call_batch_recipients.status` and both email jobs do
through `email_batch_recipients.status`.

**Every kind unwinds promptly on a cancel, and that is a requirement.** Three of
them — `email.send`, `call.batch.dial`, `session.purge` — are serial loops, so a
cancel lands between rows. Drafting spawns a task run per row and cancels them
(`services/email/batch/draft.py`), each of which settles its own `task_runs` row
and hands its recipient back unspent.

**A handler may hold its job for hours, and two of them routinely do.** Both
email kinds are long-lived loops that sleep through the gates holding them —
`email.send` through a shut window or a spent daily cap, `email.batch.draft`
through a retry backoff — re-reading policy on a 300-second tick. The lease and
the heartbeat already support that; what it changes is that a claim is no longer short, so every terminal
write in `store` names the worker that holds it.

**Each consumed job runs in its own coroutine.** Awaiting them one at a time
would put one tenant's send in front of every other tenant's work on this
consumer, `session.purge` included. On `stop` the consumer stops pulling and
CANCELS what is in flight immediately — the cancel is the point, because it is
what reaches `store.release` in `_dispatch` — and then allows
`SETTLE_GRACE_SECONDS` for the rows to be written.

**A handler may say "not yet".** Returning a `datetime` defers the job to that
instant instead of completing it. Both email kinds use it for exactly one thing
now — a subject whose `start_at` has not arrived, which the SCHEDULER should own
rather than a coroutine sleeping until Monday. The scheduling table stays this
module's business: a handler expresses what it wants, never writes
`scheduled_jobs` itself.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
from collections.abc import Awaitable, Callable
from datetime import datetime
from uuid import UUID

import asyncpg
from aiokafka import AIOKafkaConsumer

import db
from services.retention import purge_session
from services.user import Tenant, load_tenant
from settings import get_settings

from . import store
from .models import JobContext, JobKind, ScheduledJob

logger = logging.getLogger("talqing.jobs.executor")

# How long CANCELLED work may take to put its rows down. Not a period in which
# we hope a job will finish — nothing here waits for that any more.
#
# Fifteen is derived from the longest settle path in the system: the batch
# dispatcher's shielded dispatch, one LiveKit RPC bounded by
# `LIVEKIT_HTTP_TIMEOUT_SECONDS` (10), plus the `settle_recipient` write after
# it. Everything else is one or two `UPDATE`s. Moving this number without moving
# that one is picking a rounder number for its own sake.
SETTLE_GRACE_SECONDS = 15

# Which container holds a claim, for the log line and for `scheduled_jobs.claimed_by`.
_WORKER_ID = f"{socket.gethostname()}:{os.getpid()}"

# kind → handler. One dict, so adding a kind is one line and a function, and
# so an unknown kind is a loud failure rather than a silently dropped message.
#
# A handler returns `None` when the job is finished with, or the instant it
# wants to run again — see the module docstring. The third argument is which
# claim it is running under; a handler that holds its job for hours puts those
# two values in the `WHERE` of every claim it makes, and the short ones ignore it.
JobHandler = Callable[[Tenant, dict, JobContext], Awaitable[datetime | None]]

_HANDLERS: dict[JobKind, JobHandler] = {
    JobKind.SESSION_PURGE: purge_session,
}


def _register_feature_handlers() -> None:
    """Import the kinds that live outside this package.

    Local rather than top-level: `services.email` reaches back into
    `services.jobs` to enqueue, and importing it from module scope here is a
    cycle. Called once at consumer start, so an unknown kind is still a startup
    failure rather than a message dropped at 3 a.m.
    """
    from services.email import run_draft_job, run_send_job
    from services.telephony.batch import run_dial_pass
    from services.whatsapp import run_send_job as run_whatsapp_send

    _HANDLERS[JobKind.CALL_BATCH_DIAL] = run_dial_pass
    _HANDLERS[JobKind.EMAIL_BATCH_DRAFT] = run_draft_job
    _HANDLERS[JobKind.EMAIL_SEND] = run_send_job
    _HANDLERS[JobKind.WHATSAPP_SEND] = run_whatsapp_send

    missing = sorted(set(JobKind) - set(_HANDLERS))
    if missing:
        raise RuntimeError(f"scheduled-job kinds with no handler: {missing}")


# Every claim this container currently holds: job id → the pool and tenant it
# lives in. The heartbeat renews exactly these, and `_run` is the only writer.
_Live = dict[UUID, tuple[asyncpg.Pool, UUID]]


async def consume(stop: asyncio.Event) -> None:
    """Run due jobs until asked to stop, then hand back whatever is in flight."""
    settings = get_settings()
    _register_feature_handlers()
    consumer = AIOKafkaConsumer(
        settings.kafka.jobs_topic,
        bootstrap_servers=settings.kafka.bootstrap_servers,
        group_id=settings.kafka.jobs_consumer_group,
        # A job message is work, not a live turn: one published while every
        # executor was down must still run. Replaying an already-run job is
        # harmless — the claim refuses it — so `earliest` costs nothing and
        # `latest` would silently drop work across a deploy.
        auto_offset_reset="earliest",
        # NOTE: offsets commit ahead of completion, so a job still running when
        # its offset commits is gone from Kafka's point of view if the process
        # dies. Kafka is not what recovers it, and do not assume it is — the
        # LEASE is. A graceful stop is covered by the drain below, which cancels
        # and so reaches `store.release`; an ungraceful one leaves the row
        # `running` with a lease nobody renews, and `store.due_jobs` explicitly
        # republishes `running AND lease_expires_at < now()` within
        # `LEASE_SECONDS`. Nothing needs resetting by hand.
        enable_auto_commit=True,
    )
    await consumer.start()
    logger.info("job executor %s ready on %s", _WORKER_ID, settings.kafka.jobs_topic)
    live: _Live = {}
    running: set[asyncio.Task[None]] = set()
    heartbeat = asyncio.create_task(_renew_leases(live))
    try:
        while not stop.is_set():
            batch = await consumer.getmany(timeout_ms=1000, max_records=50)
            for records in batch.values():
                for record in records:
                    try:
                        job = ScheduledJob.model_validate_json(record.value)
                    except Exception:
                        # A message we cannot parse names no job, so there is
                        # nothing to release and nothing to retry. The row stays
                        # `pending` and the scheduler republishes it.
                        logger.exception("dropping malformed scheduled-job Kafka event")
                        continue
                    # Nothing bounds this, and that is a decision rather than
                    # an oversight: a tuning knob with no traffic to tune it
                    # against is a number invented from nothing. What it costs,
                    # so whoever hits it recognises it in one read: a backlog of
                    # due jobs becomes that many concurrent coroutines and, far
                    # enough out, an exhausted connection pool. Both email kinds
                    # are now long-lived, so a coroutine here tracks a
                    # workspace's outstanding sends rather than what is happening
                    # this second — which is what will make the number start
                    # mattering. When a ceiling is wanted it goes HERE, before
                    # the claim: a task parked on a semaphore it acquired after
                    # claiming holds a claim it is not using.
                    task = asyncio.create_task(_guarded(job, live))
                    running.add(task)
                    task.add_done_callback(running.discard)
    finally:
        await _drain(running)
        heartbeat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat
        await consumer.stop()


async def _drain(running: set[asyncio.Task[None]]) -> None:
    """Cancel everything in flight and give it a moment to put its rows down.

    **There is no period in which we hope a job will finish.** Every kind
    releases its `scheduled_jobs` row on cancel and is republished within a
    scheduler tick, so waiting only makes the deploy slower — and none of the
    long kinds would have finished anyway: both email handlers hold their job
    until the list is done, and a dial pass paces itself at
    `dial_interval_seconds`, so a wait was consumed in full on every deploy that
    caught a live batch and cancelled the work at the end of it regardless.

    `wait_for` over a `gather` rather than a bare `asyncio.wait`, so
    `return_exceptions=True` is kept: `_guarded` re-raises `CancelledError` after
    releasing, and gathering without it would leave unretrieved exceptions at
    loop close.
    """
    if not running:
        return
    logger.info("cancelling %d job(s) in flight", len(running))
    for task in running:
        task.cancel()
    try:
        await asyncio.wait_for(
            asyncio.gather(*running, return_exceptions=True), SETTLE_GRACE_SECONDS
        )
    except TimeoutError:
        # Nothing left to do here: the lease is what covers a job whose settle
        # never landed, and it expires within LEASE_SECONDS.
        stuck = [task for task in running if not task.done()]
        logger.warning("%d job(s) did not settle within %ds", len(stuck), SETTLE_GRACE_SECONDS)


async def _renew_leases(live: _Live) -> None:
    """Push out the lease on every claim this container holds, forever.

    One task for the process and one `UPDATE` per pool, rather than a task per
    job: a container running fifty jobs would otherwise run fifty timers to write
    fifty rows. Grouped by pool because a claim is a row in its tenant's data
    plane and several tenants share one.

    A renewal that fails is logged and retried on the next beat. The lease is
    three beats long, so a single blip costs nothing; an outage long enough to
    matter is one where the work should be handed on anyway.
    """
    while True:
        await asyncio.sleep(store.LEASE_SECONDS / 3)
        by_pool: dict[int, tuple[asyncpg.Pool, list[UUID]]] = {}
        for job_id, (pool, _) in list(live.items()):
            by_pool.setdefault(id(pool), (pool, []))[1].append(job_id)
        for pool, job_ids in by_pool.values():
            try:
                await store.renew_leases(pool, job_ids)
            except Exception:
                logger.exception("could not renew the lease on %d job(s)", len(job_ids))


async def _guarded(job: ScheduledJob, live: _Live) -> None:
    """Run one job so that its failure stays its own.

    `_run` already classifies everything a *handler* raises; what this catches is
    the surrounding machinery — a control-plane blip in `load_tenant`, a pool
    that cannot be opened. One tenant's database hiccup must not abandon every
    other job in flight. The row is left `pending` and the scheduler republishes
    it.
    """
    try:
        await _run(job, live)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("job %s (%s) could not be started", job.id, job.kind)


async def _run(job: ScheduledJob, live: _Live) -> None:
    tenant = await load_tenant(job.tenant_id)
    if tenant is None:
        # The organization was deleted after the job was scheduled. Its data
        # went with it, so there is nothing left to do and nowhere to write the
        # outcome — the row lives in that tenant's own data plane.
        logger.warning("job %s: tenant %s is gone; skipping", job.id, job.tenant_id)
        return

    pool = await db.tenant_pool(tenant)
    if not await store.claim(pool, job.id, tenant.id, worker=_WORKER_ID):
        # The ordinary outcome of a duplicate message.
        logger.debug("job %s: already claimed", job.id)
        return
    # From here the row is ours and its lease is ticking, so it goes on the
    # heartbeat's list before anything that can take time.
    live[job.id] = (pool, tenant.id)
    try:
        await _dispatch(job, tenant, pool)
    finally:
        live.pop(job.id, None)


async def _dispatch(job: ScheduledJob, tenant: Tenant, pool: asyncpg.Pool) -> None:
    handler = _HANDLERS.get(job.kind)
    if handler is None:
        await store.record_failure(
            pool, job.id, tenant.id, f"no handler for kind '{job.kind}'", worker=_WORKER_ID
        )
        logger.error("job %s: no handler for kind '%s'", job.id, job.kind)
        return

    try:
        again_at = await handler(tenant, job.args, JobContext(id=job.id, worker=_WORKER_ID))
    except asyncio.CancelledError:
        # Torn down mid-work. Put the job back — unspent — before the
        # cancellation propagates, so the next scheduler pass republishes it.
        # This is what makes a deploy safe, and it runs here rather than in
        # `consume` precisely because each job holds its own coroutine.
        await store.release(pool, job.id, tenant.id, worker=_WORKER_ID)
        raise
    except Exception as exc:
        # An exception is not a shutdown: back off and try again, and after
        # `MAX_ATTEMPTS` leave it `failed` for a person.
        logger.exception("job %s (%s) failed", job.id, job.kind)
        await store.record_failure(
            pool, job.id, tenant.id, f"{type(exc).__name__}: {exc}", worker=_WORKER_ID
        )
        return

    if again_at is not None:
        # Not yet. Not a retry either: the attempt is given back.
        await store.defer(pool, job.id, tenant.id, again_at, worker=_WORKER_ID)
        logger.info("job %s (%s) deferred to %s", job.id, job.kind, again_at.isoformat())
        return

    await store.complete(pool, job.id, tenant.id, worker=_WORKER_ID)
    logger.info("job %s (%s) done", job.id, job.kind)
