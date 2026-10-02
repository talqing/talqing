"""Reading an organization, from a region.

One place that knows how a region gets a ``Tenant``. It used to be one place that
knew the shape of a ``control.tenants`` row, after having been seven: the same
SELECT was pasted into the API's request context, two billing sweeps, the public
webhook router and both workers. Two of those copies had drifted onto a
``status = 'active'`` filter for a column the schema does not have, so the
CoPilot's text worker and every inbound integration webhook died on
``UndefinedColumnError`` while the five other copies kept working — the failure
mode a duplicated query invites, since nothing forces the copies to agree and
only the cold paths had drifted.

The query is now an HTTP call to the control plane, cached in this region's
Redis. The round trip is ~150 ms from `blr1` to `fra1` on a warm connection and
~440 ms on a cold one, and the paths that read a tenant are mostly cold: a
webhook arriving after a quiet spell, and every call's job process, which is
new. An inbound call paid it twice before it could answer. Caching is safe
because nothing a region does with the row depends on it being current, except
the retention policy, which is read fresh (``fresh=True``) where it is applied.
Organizations are never deleted, so a cached row never outlives its tenant.
"""

from __future__ import annotations

import logging
from uuid import UUID

from pydantic import ValidationError
from redis.exceptions import RedisClusterException, RedisError

from iredis import get_redis
from services.control import client as control

from .models import Tenant

logger = logging.getLogger("talqing.user.tenants")

# Bounds how long a rename lingers in this region; nothing else here goes stale.
_CACHE_TTL_SECONDS = 3600


async def load_tenant(tenant_id: UUID | str, *, fresh: bool = False) -> Tenant | None:
    """The organization with this id, or None if there is no such row.

    Callers that hold a membership row can treat None as impossible — the
    membership FK cascades — but a webhook URL or a queued job names a tenant
    that may not exist, so they must handle it. Absence is never cached.

    A control plane that is *unreachable* raises ``ControlPlaneError`` instead,
    and that distinction is load-bearing: "this organization is gone" and "we
    could not ask" are opposite verdicts, and only one of them is final.

    ``fresh`` skips the cache, for a reader that must see a change made a moment
    ago. Redis failing is logged and read through: the cache only saves time.
    """
    key = f"tenant:{tenant_id}"
    if not fresh:
        try:
            cached = await get_redis().get(key)
        except (RedisError, RedisClusterException):
            logger.warning("tenant cache read failed; asking the control plane", exc_info=True)
            cached = None
        if cached is not None:
            try:
                return Tenant.model_validate_json(cached)
            except ValidationError:
                # Written by a release whose `Tenant` had other fields; refreshed below.
                pass
    tenant = await control.load_tenant(tenant_id)
    if tenant is not None:
        try:
            await get_redis().set(key, tenant.model_dump_json(), ex=_CACHE_TTL_SECONDS)
        except (RedisError, RedisClusterException):
            logger.warning("tenant cache write failed", exc_info=True)
    return tenant
