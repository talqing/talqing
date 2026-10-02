"""Calling a region from the control plane.

One endpoint, in one direction, for one reason: the credit balance is regional —
the debit shares a transaction with ``sessions.platform_fee`` on the per-call
settlement path — while Dodo has one business and knows nothing about our
regions. So control owns the payment and pushes the result to whichever region
the ``credit_topups`` row names.

**The push is the fast path, not the guarantee.** What catches a push that failed
is the billing page asking control what is still unapplied (``credits/pending``),
which is immediate, correlated with the harm, and free when there is nothing to
apply. There is no timer and no sweep here, and adding one would be a
forever-running cross-region call for an event that essentially never happens.
So a failure is logged and left: ``applied_at IS NULL`` is an operator's list.
"""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal
from typing import Any
from uuid import UUID

import httpx

from settings import RegionConfig, get_settings

logger = logging.getLogger("talqing.regions.client")

# Longer than the auth hot path's, and for the opposite reason: nothing is
# waiting on this, and a push that times out costs the customer a delay rather
# than an error.
_TIMEOUT_SECONDS = 15.0


class RegionUnreachable(RuntimeError):
    """The push did not land. The issuance stays unapplied and reconciles later."""


_clients: dict[str, httpx.AsyncClient] = {}
_lock = asyncio.Lock()


def region_or_none(slug: str) -> RegionConfig | None:
    return get_settings().region_by_slug(slug)


async def _http(region: RegionConfig) -> httpx.AsyncClient:
    client = _clients.get(region.slug)
    if client is None:
        async with _lock:
            client = _clients.get(region.slug)
            if client is None:
                client = httpx.AsyncClient(
                    base_url=region.control_url,
                    timeout=_TIMEOUT_SECONDS,
                    http2=True,
                    headers={"authorization": f"Bearer {region.internal_token}"},
                )
                _clients[region.slug] = client
    return client


async def aclose() -> None:
    for client in list(_clients.values()):
        await client.aclose()
    _clients.clear()


async def apply_credit(
    *,
    region_slug: str,
    tenant_id: UUID,
    kind: str,
    amount: Decimal,
    topup_id: UUID | None = None,
    note: str | None = None,
) -> None:
    """Write one issuance into a region's ledger. Idempotent on the far side.

    ``kind`` is the ledger kind — ``signup_grant``, ``purchase`` or ``refund`` —
    and each of those has a partial unique index on ``credit_ledger``, which is
    what makes a reconcile racing a late push credit exactly once. ``amount`` is
    always positive: a reversal is a ``refund`` kind, and the region signs it.
    """
    region = region_or_none(region_slug)
    if region is None:
        # A slug naming a region that is not in this deployment's config: a
        # retired region, or a row written by a build that knew a region this one
        # does not. Loud, and NOT a retry — nothing here can fix it.
        raise RegionUnreachable(
            f"top-up names region {region_slug!r}, which is not in this control plane's region list"
        )
    payload: dict[str, Any] = {
        "tenant_id": str(tenant_id),
        "kind": kind,
        # A string, not a float: this is money, and the region parses it back
        # into a Decimal for a NUMERIC(12,6) column.
        "amount": str(amount),
        "topup_id": str(topup_id) if topup_id else None,
        "note": note,
    }
    client = await _http(region)
    try:
        response = await client.post("/internal/v1/credits/apply", json=payload)
    except httpx.HTTPError as exc:
        raise RegionUnreachable(f"region {region.slug} unreachable: {exc}") from exc
    if response.status_code >= 400:
        raise RegionUnreachable(
            f"region {region.slug} refused the credit push with {response.status_code}: "
            f"{response.text[:300]}"
        )
    logger.info("applied %s of %s credit to tenant %s in %s", amount, kind, tenant_id, region.slug)
