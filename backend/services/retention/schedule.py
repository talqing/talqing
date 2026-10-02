"""Scheduling a call's own deletion, at the moment it ends.

One row per session, and only for a workspace that has a policy — the default is
unlimited, so the common case costs nothing. The deadline is computed once, from
the tenant's policy as it stands when the call ends, read fresh rather than from
the tenant the call started with, and then belongs to the job:
changing `retention_days` later moves nothing that has already happened. That is
deliberate (D5), and it is what makes the promise on the settings page — "applies
to calls from now on" — literally true rather than approximately true.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from uuid import UUID

import db
from services import jobs
from services.user import Tenant, load_tenant

logger = logging.getLogger("talqing.retention")


async def schedule_session_purge(
    tenant: Tenant, session_id: str | UUID, *, ended_at: datetime
) -> datetime | None:
    """Schedule this call's content deletion. Returns when, or None if never.

    Never raises: a call that has just ended must not fail its finalize because
    a job row would not insert. The cost of losing one is that this call is kept
    longer than the policy says — visible, and recoverable by the backfill job
    when it exists — where the cost of raising is a call that never gets billed.
    """
    # Fresh, not the caller's copy: that was read when the call started, and
    # through the tenant cache, so it can predate a policy change by an hour. The
    # copy is still the better answer when control cannot be reached — a policy
    # an hour old beats keeping the call for ever.
    try:
        current = await load_tenant(tenant.id, fresh=True)
    except Exception:
        logger.warning(
            "could not read the retention policy fresh; using the call's copy", exc_info=True
        )
        current = tenant
    if current is None or current.retention_days is None:
        return None
    at = ended_at + timedelta(days=current.retention_days)
    try:
        pool = await db.tenant_pool(tenant)
        await jobs.enqueue(
            pool,
            tenant_id=tenant.id,
            kind=jobs.JobKind.SESSION_PURGE,
            subject_id=UUID(str(session_id)),
            scheduled_at=at,
            args={"session_id": str(session_id)},
        )
    except Exception:
        logger.exception("could not schedule the purge of session %s", session_id)
        return None
    return at
