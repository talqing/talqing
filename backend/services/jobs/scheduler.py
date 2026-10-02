"""The scheduler loop: find what is due, and hand it to Kafka.

One pass every tick, one indexed query per distinct data-plane DSN — not per
tenant, because pools are keyed by DSN and a platform that got slower as it
succeeded would be the wrong shape. Each pass returns ids and arguments only;
everything the executor touches afterwards is scoped by the `tenant_id` it read
here.

A pass publishes two things: work that is due and unclaimed, and work whose
holder's lease has lapsed. Reclaim is therefore the same mechanism as discovery
rather than a second loop — see `store.due_jobs`.

This runs in its own container rather than as another loop inside
`background-worker` because it *replaced* a mechanism rather than sitting beside
one: the batch dispatcher's discovery loop, its Redis lock and its lock-TTL
reasoning were bespoke answers to "find work that is due, give it to exactly one
worker", and they are gone — batch dialling is a `call.batch.dial` row here like
everything else.
"""

from __future__ import annotations

import asyncio
import logging

import db

from . import store
from .models import ScheduledJob
from .queue import publish_job

logger = logging.getLogger("talqing.jobs.scheduler")

# A general mechanism wants a short tick; retention's granularity is days, so
# this is irrelevant to it either way. The cost is one indexed query per DSN per
# tick, which normally returns nothing.
TICK_SECONDS = 5.0

# How many due jobs one pass will publish. A bound, not a pace: the next tick is
# five seconds away, and a backlog that needs more than this is one an operator
# should see in the logs rather than have flushed into Kafka in one burst.
BATCH_SIZE = 500

# How long a published job may sit `pending` before it is published again. Longer
# than a healthy consume-and-claim (milliseconds) by a wide margin, so the normal
# case publishes once; short enough that an executor which died between
# consuming and claiming is covered within a minute.
REPUBLISH_AFTER_SECONDS = 60

# How many `failed` jobs one report names. A bound on the log line, not on the
# problem: a plane with more failures than this has one cause, and the first few
# name it.
FAILED_REPORT_LIMIT = 20
# …on its own slower cadence, because the same failed job is still failed on the
# next tick and an error every five seconds is noise, not a signal.
REPORT_EVERY_TICKS = 60


async def discovery_loop(stop: asyncio.Event) -> None:
    """Publish every due job, forever."""
    tick = 0
    while not stop.is_set():
        tick += 1
        try:
            await _pass(report=tick % REPORT_EVERY_TICKS == 0)
        except Exception:
            # Fail closed. A blip costs one tick; failing open would mean every
            # replica publishing every job during exactly the outage nobody is
            # watching.
            logger.exception("scheduler pass failed; retrying in %.0fs", TICK_SECONDS)
        try:
            await asyncio.wait_for(stop.wait(), timeout=TICK_SECONDS)
        except TimeoutError:
            pass


async def _pass(*, report: bool) -> None:
    # This region's own DSNs, never a control-plane query. `tenants` is shared by
    # every region, so a pass built from it would publish another region's due
    # jobs onto this region's Kafka, where this region's background-worker would
    # run them against a database it cannot reach — and because the pass fails
    # closed, the symptom is no jobs running anywhere.
    #
    # `data_dsns()` rather than `data_pool()`: a tenant on an override would
    # otherwise have its due jobs silently never published, which is the quietest
    # failure available here.
    for dsn in db.data_dsns():
        pool = await db.data_pool_for_dsn(dsn)
        rows = await store.due_jobs(
            pool, limit=BATCH_SIZE, republish_after_s=REPUBLISH_AFTER_SECONDS
        )
        published = []
        for row in rows:
            try:
                job = ScheduledJob(
                    id=row["id"],
                    tenant_id=row["tenant_id"],
                    kind=row["kind"],
                    args=row["args"] or {},
                )
                await publish_job(job)
            except Exception:
                # One unpublishable row — a kind this build does not know, most
                # likely mid-rollback — must not stop the rest of the pass. It
                # stays `pending` and is tried again on the next tick.
                logger.exception("could not publish job %s (%s)", row["id"], row["kind"])
                continue
            published.append(job.id)
        if published:
            # After the sends, so a broker that refuses the message leaves the
            # job looking unpublished and the next tick tries again.
            await store.mark_published(pool, published)
            logger.info("published %d due job(s)", len(published))

        if not report:
            continue
        # There is deliberately NO liveness check here any more. "Running for
        # more than 900 seconds" meant something when every handler worked in
        # bounded passes; now `email.send` legitimately holds a claim for days
        # and `email.batch.draft` for hours, so the same query would warn about
        # every healthy long job and about a wedged one in the same sentence.
        #
        # Nothing replaced it — a wedged handler is invisible, accepted for now.
        # To look for one by hand, ask the PRODUCT rows rather than this table:
        #
        #     SELECT id, next_send_at FROM email_send_runs
        #     WHERE status = 'sending' AND next_send_at < now() - interval '15 minutes';
        #     SELECT id, next_draft_at FROM email_batches
        #     WHERE status = 'drafting' AND next_draft_at < now() - interval '15 minutes';
        #
        # Never reset a `running` row by hand: if its holder is alive you get a
        # second copy beside the one still going.
        #
        # Jobs that GAVE UP are still reported, and at error rather than warning:
        # a `session.purge` that failed is content a tenant's retention policy
        # says is deleted and which is still there. Nothing retries it on its
        # own, so if this line is not written the only trace is one exception in
        # a log that has since rolled over.
        for row in await store.failed_jobs(pool, limit=FAILED_REPORT_LIMIT):
            logger.error(
                "job %s (%s, tenant %s, subject %s) FAILED after %d attempts and will not "
                "retry: %s — fix the cause, then set it back to 'pending'",
                row["id"],
                row["kind"],
                row["tenant_id"],
                row["subject_id"],
                row["attempts"],
                row["last_error"],
            )
