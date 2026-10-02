"""`scheduled_jobs` reads and writes. Every state change is a compare-and-set.

Nothing here reads a row and then writes it back. Each `UPDATE` names the state
it expects to find, so a repeated Kafka message, a rebalanced consumer and a
scheduler pass that overlaps an executor all end up doing the work once.

**A claim carries a lease.** `claim` stamps `lease_expires_at`, the executor
renews it from a heartbeat while the handler runs, and `due_jobs` returns a
`running` row whose lease has lapsed alongside the `pending` ones. That is the
whole reclaim mechanism: no sweep, no second loop, and still exactly one
compare-and-set deciding who owns a job. It rests on the contract the executor
already states — every kind must be safe to re-run **from the start** — because
a lapsed lease means the previous holder is gone, not that it agreed to stop.

**Every terminal write names the worker that holds the claim.** `complete`,
`defer`, `release` and `record_failure` all carry `AND claimed_by = $worker`,
because "the lease lapsed" and "the container died" are not the same event: a
container whose renewals stalled long enough to lose its claim, and which then
recovers, would otherwise complete or defer a job somebody else is now running.
That window was 60 seconds while every handler worked in short passes. With
`email.batch.draft` and `email.send` holding a claim for hours it is hours long,
so the clause stopped being theoretical.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

import asyncpg

from .models import JobKind

# After this many failed attempts a job stops retrying and lands on `failed`,
# where the scheduler reports it (`failed_jobs`) for an operator to reset. Five
# attempts span 15 minutes of backoff — 1 + 2 + 4 + 8 — which rides out a
# restart or a brief bucket outage without a genuinely broken job still trying
# tomorrow. Anything longer than that is not a blip and wants a person.
MAX_ATTEMPTS = 5

# Backoff between attempts: 1, 2, 4, 8… minutes, capped. Minutes rather than
# seconds because every kind here is deferred work nobody is waiting on.
_BACKOFF_BASE_MINUTES = 1
_BACKOFF_MAX_MINUTES = 30

# How long a claim is good for without a renewal. Comfortably longer than the
# heartbeat interval (a third of this) times any plausible number of missed
# beats, and short enough that a killed container's work is back in circulation
# within a couple of minutes. It is NOT a deadline on the handler: a live
# handler renews, and the email kinds legitimately run for hours.
LEASE_SECONDS = 120


async def enqueue(
    conn: asyncpg.Connection | asyncpg.Pool,
    *,
    tenant_id: UUID,
    kind: JobKind,
    scheduled_at: datetime,
    subject_id: UUID | None = None,
    args: dict[str, Any] | None = None,
) -> UUID:
    """Schedule one job. Takes a connection so it can join a caller's transaction.

    Nothing is published here. The scheduler finds it when it comes due, which
    is what keeps a purge ninety days out in Postgres instead of in a topic.
    """
    return await conn.fetchval(
        """
        INSERT INTO scheduled_jobs (tenant_id, kind, args, subject_id, scheduled_at)
        VALUES ($1, $2, $3, $4, $5)
        RETURNING id
        """,
        tenant_id,
        str(kind),
        args or {},
        subject_id,
        scheduled_at,
    )


async def enqueue_once(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    kind: JobKind,
    scheduled_at: datetime,
    subject_id: UUID,
    args: dict[str, Any] | None = None,
) -> UUID | None:
    """Schedule one job for this subject, unless one is already live.

    Returns the new job's id, or ``None`` when a `pending` or `running` job of
    this kind already exists for this subject.

    For kinds where a subject may have **at most one** job at a time: an email
    batch's drafting pass and a call batch's dialling pass, both of which several
    buttons can ask for. Without the guard, resume-resume-retry gives one batch
    three passes, each claiming rows the others are working on — nothing is
    drafted or dialled twice (the claim is still a compare-and-set), but
    `max_concurrency` is tripled, which is the one limit its owner asked us to
    respect.

    **Not airtight under truly simultaneous callers.** `INSERT … WHERE NOT
    EXISTS` does not block on a concurrent uncommitted insert under
    `READ COMMITTED`, and no unique index backs it. Both current callers are safe
    because the status write beside them is a compare-and-set on the batch row,
    so Postgres serialises the pair on a real row lock. A future caller without
    one wants a partial unique index — per kind, because `email.send`
    deliberately allows several live jobs per batch.

    Takes a connection, not a pool, and it must be the caller's transaction: the
    status write and this insert commit together or neither does.
    """
    return await conn.fetchval(
        """
        INSERT INTO scheduled_jobs (tenant_id, kind, args, subject_id, scheduled_at)
        SELECT $1, $2, $3, $4, $5
        WHERE NOT EXISTS (
            SELECT 1 FROM scheduled_jobs
            WHERE tenant_id = $1 AND kind = $2 AND subject_id = $4
              AND status IN ('pending', 'running')
        )
        RETURNING id
        """,
        tenant_id,
        str(kind),
        args or {},
        subject_id,
        scheduled_at,
    )


async def due_jobs(
    pool: asyncpg.Pool, *, limit: int, republish_after_s: int
) -> list[asyncpg.Record]:
    """Work that is due and has not been claimed, across every tenant here.

    **The one deliberately cross-tenant query left in the platform.** It returns
    the ids and the arguments and nothing else, and every query the executor
    makes afterwards is scoped by the `tenant_id` it read here.

    `published_at` is what stops each tick republishing the same due job while an
    executor is on its way to claiming it, and what republishes one whose
    executor died between consuming and claiming.
    """
    return await pool.fetch(
        """
        SELECT id, tenant_id, kind, args
        FROM scheduled_jobs
        WHERE scheduled_at <= now()
          AND (
            (status = 'pending'
             AND (published_at IS NULL OR published_at < now() - make_interval(secs => $2)))
            -- A claim whose holder is gone. Republished rather than swept:
            -- `claim` accepts a lapsed lease, so this needs no second mechanism
            -- and a still-live holder simply renews before we ever see it.
            OR (status = 'running' AND lease_expires_at < now())
          )
        ORDER BY scheduled_at
        LIMIT $1
        """,
        limit,
        republish_after_s,
    )


async def mark_published(pool: asyncpg.Pool, job_ids: list[UUID]) -> None:
    """Record that these went to Kafka — unless somebody has already moved on.

    `status = 'pending'` is the guard, and it is not decoration: publishing is a
    Kafka round trip, and an executor routinely consumes and claims a job before
    the send that carried it has even returned — locally these rows come out
    with `started_at` a few milliseconds *before* `published_at`. Stamping a row
    that is already `running` or `done` is writing history onto somebody else's
    work. A republished lapsed lease is `running`, so it takes no stamp and may
    be published on the next tick too; the claim makes the second copy a no-op,
    and a stamp there would land on the row of whoever has since taken it.

    Not guarded any harder than that. A job the executor claimed, failed and put
    back inside this same pass is `pending` again and will take the stamp, which
    holds its retry back by at most `REPUBLISH_AFTER_SECONDS`; distinguishing it
    would mean carrying each row's previous `published_at` through the publish,
    and a job whose backoff is measured in minutes does not care about a minute.
    """
    await pool.execute(
        "UPDATE scheduled_jobs SET published_at = now(), updated_at = now() "
        "WHERE id = ANY($1::uuid[]) AND status = 'pending'",
        job_ids,
    )


async def claim(pool: asyncpg.Pool, job_id: UUID, tenant_id: UUID, *, worker: str) -> bool:
    """Take the job, or report that somebody already has it.

    This — not Kafka — is what makes at-least-once delivery safe: the broker
    distributes, the claim decides. A `False` here is the ordinary outcome of a
    duplicate message and is not worth a log line above debug.

    Still ONE compare-and-set, and therefore still one owner, even though it now
    accepts two states: `pending`, or `running` with a lease that has lapsed.
    Two containers racing to reclaim the same abandoned job both name
    `lease_expires_at < now()`, and only the one that gets there first finds it.
    """
    claimed = await pool.fetchval(
        """
        UPDATE scheduled_jobs
        SET status = 'running', started_at = now(), attempts = attempts + 1,
            claimed_by = $3, lease_expires_at = now() + make_interval(secs => $4),
            updated_at = now()
        WHERE id = $1 AND tenant_id = $2
          AND (status = 'pending' OR (status = 'running' AND lease_expires_at < now()))
        RETURNING id
        """,
        job_id,
        tenant_id,
        worker,
        float(LEASE_SECONDS),
    )
    return claimed is not None


async def renew_leases(pool: asyncpg.Pool, job_ids: list[UUID]) -> None:
    """Push every live claim's lease out, in one statement.

    One `UPDATE` for every job this container holds on this pool, from one
    heartbeat task — not a task per job. `status = 'running'` is the guard: a job
    that finished, deferred or was reclaimed between the heartbeat reading its id
    and this landing is somebody else's row now.
    """
    if not job_ids:
        return
    await pool.execute(
        """
        UPDATE scheduled_jobs
        SET lease_expires_at = now() + make_interval(secs => $2), updated_at = now()
        WHERE id = ANY($1::uuid[]) AND status = 'running'
        """,
        job_ids,
        float(LEASE_SECONDS),
    )


async def complete(pool: asyncpg.Pool, job_id: UUID, tenant_id: UUID, *, worker: str) -> None:
    await pool.execute(
        """
        UPDATE scheduled_jobs
        SET status = 'done', last_error = NULL,
            claimed_by = NULL, lease_expires_at = NULL, updated_at = now()
        WHERE id = $1 AND tenant_id = $2 AND status = 'running' AND claimed_by = $3
        """,
        job_id,
        tenant_id,
        worker,
    )


async def release(pool: asyncpg.Pool, job_id: UUID, tenant_id: UUID, *, worker: str) -> None:
    """Put a job back because we are shutting down, not because it failed.

    The attempt is not spent and `scheduled_at` is untouched, so the next
    scheduler pass republishes it immediately. This is why every kind must be
    safe to re-run *from the start* rather than merely safe to call twice: a
    graceful release can abandon one mid-work.
    """
    await pool.execute(
        """
        UPDATE scheduled_jobs
        SET status = 'pending', started_at = NULL, published_at = NULL,
            claimed_by = NULL, lease_expires_at = NULL,
            attempts = GREATEST(attempts - 1, 0), updated_at = now()
        WHERE id = $1 AND tenant_id = $2 AND status = 'running' AND claimed_by = $3
        """,
        job_id,
        tenant_id,
        worker,
    )


async def defer(
    pool: asyncpg.Pool, job_id: UUID, tenant_id: UUID, until: datetime, *, worker: str
) -> None:
    """Put a running job back for later without spending its attempt.

    `release` already does this for shutdown; `defer` is the same UPDATE plus a
    new `scheduled_at` AND `attempts = 0`. It is how a job that works in passes
    says "again in 30 seconds", "again when the calling window opens", and
    "again after the retry backoff" — not a failure, not a retry, just not yet.

    **Resetting `attempts` is the one thing it does not inherit from `release`,
    and it is deliberate:** a pass that ran and asked to come back has
    demonstrated the job works, so five transient failures spread across a
    three-hour batch must not eventually strand it at MAX_ATTEMPTS. A reader
    comparing the two functions would otherwise take the difference for an
    oversight.
    """
    await pool.execute(
        """
        UPDATE scheduled_jobs
        SET status = 'pending', started_at = NULL, published_at = NULL,
            claimed_by = NULL, lease_expires_at = NULL,
            attempts = 0, scheduled_at = $3, last_error = NULL, updated_at = now()
        WHERE id = $1 AND tenant_id = $2 AND status = 'running' AND claimed_by = $4
        """,
        job_id,
        tenant_id,
        until,
        worker,
    )


async def record_failure(
    pool: asyncpg.Pool, job_id: UUID, tenant_id: UUID, error: str, *, worker: str
) -> None:
    """Back off and try again, or give up and leave it for a person.

    "No lease" must not be read as "no retry" — this is the ordinary path and
    the one that will actually fire.
    """
    await pool.execute(
        f"""
        UPDATE scheduled_jobs
        SET status = CASE WHEN attempts >= $3 THEN 'failed' ELSE 'pending' END,
            scheduled_at = CASE
                WHEN attempts >= $3 THEN scheduled_at
                ELSE now() + make_interval(mins => LEAST(
                    {_BACKOFF_BASE_MINUTES} * power(2, attempts - 1)::int, {_BACKOFF_MAX_MINUTES}
                ))
            END,
            started_at = NULL,
            published_at = NULL,
            claimed_by = NULL,
            lease_expires_at = NULL,
            last_error = $4,
            updated_at = now()
        WHERE id = $1 AND tenant_id = $2 AND status = 'running' AND claimed_by = $5
        """,
        job_id,
        tenant_id,
        MAX_ATTEMPTS,
        error[:1000],
        worker,
    )


async def failed_jobs(pool: asyncpg.Pool, *, limit: int) -> list[asyncpg.Record]:
    """Jobs that gave up. Nothing will retry these without a person.

    Worth saying out loud rather than leaving in a row nobody queries, because
    for `session.purge` a `failed` job is not a stalled chore — it is content a
    tenant's retention policy says should be gone and is not. The object delete
    runs first, so a bucket outage that outlasts the backoff burns all
    `MAX_ATTEMPTS` and leaves the whole call intact, permanently.
    """
    return await pool.fetch(
        """
        SELECT id, tenant_id, kind, subject_id, attempts, last_error, updated_at
        FROM scheduled_jobs
        WHERE status = 'failed'
        ORDER BY updated_at DESC
        LIMIT $1
        """,
        limit,
    )
