"""Getting credit control has decided on into the region that holds the balance.

**Control plane only.**

The balance is regional and stays regional: the debit shares a transaction with
``sessions.platform_fee`` on the per-call settlement path, and its comment earns
that placement — *"a call cannot be priced without being debited, or debited
without being priced, and there is nothing for a sweep to reconcile afterwards."*
Moving the balance here would put a cross-region write on every settled call and
make one shared database a synchronous dependency of every region's settlement.
It is the coupling that is unacceptable, not the row count.

So credit that control decides on — a purchase Dodo confirmed, a signup grant,
a reversal — is issued here and *pushed* there. Two mechanisms, and only two:

1. **Push.** Control stamps the issuance as applied when the region acks.
2. **Reconcile.** ``GET /v1/billing/credits`` on a region asks control what is
   still unapplied for that tenant *here*, applies it, and confirms. Idempotent
   in both directions through the partial unique indexes that already exist on
   ``credit_ledger``, so a reconcile racing a late push credits exactly once.

**Nothing sweeps and there is no timer.** The billing page is the right trigger
and a loop is not: Dodo's return URL sends a buyer straight to it, and a tenant
whose calls are being refused arrives there by the shortest path there is — the
credit banner's own link. A 60-second loop on the control droplet would be a
forever-running cross-region call for an event that essentially never happens,
and it would need its own monitoring and its own story for being wedged. That is
the same trade ``scheduled_jobs`` already makes with no lease and no reclaim
loop.

The residual gap, stated rather than papered over: a **reversal** whose push
failed sits unapplied until the tenant loads the billing page — and a tenant
whose credit is being taken away has no reason to go there. Exposure is one pack,
the row is visible in control, and a human is already in the loop for a refund.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel

import db
from services.regions import client as regions
from settings import get_settings

logger = logging.getLogger("talqing.billing.issuance")


class PendingIssuance(BaseModel):
    """One issuance control believes a region has not applied yet."""

    issuance_id: UUID
    kind: str
    amount: Decimal
    topup_id: UUID | None = None
    note: str | None = None


async def push_grant_or_topup(
    *,
    issuance_id: UUID,
    kind: str,
    tenant_id: UUID,
    region: str,
    amount: Decimal,
    topup_id: UUID | None = None,
    note: str | None = None,
) -> bool:
    """Apply one issuance in its region and stamp it here. Never raises.

    A failed push is logged and left unapplied, because that is exactly the state
    the reconcile and the operator's list are looking for. Raising would turn a
    delayed credit into a failed webhook — which Dodo would redeliver, running
    the whole amount check again for a payment we have already accepted.
    """
    try:
        await regions.apply_credit(
            region_slug=region,
            tenant_id=tenant_id,
            kind=kind,
            amount=amount,
            topup_id=topup_id,
            note=note,
        )
    except regions.RegionUnreachable:
        logger.exception(
            "could not apply %s of %s credit to tenant %s in %s — it stays unapplied and "
            "lands when that tenant's billing page reconciles",
            amount,
            kind,
            tenant_id,
            region,
        )
        return False
    await mark_applied([(issuance_id, kind)], region=region)
    return True


async def grant_signup_credit(tenant_id: UUID) -> None:
    """Give a newly created organization its grant — in EVERY region.

    Every region serves every organization, so a grant in one of them would be a
    balance the customer cannot spend in the other. The cost to hold consciously
    is that one signup now costs ``signup_grant_usd × regions``.

    A region added later does NOT retroactively grant to organizations that
    already exist: grants are written for the regions that exist at signup. Doing
    it for everyone afterwards is a deliberate backfill, not a rule.

    Called from exactly one place — first login — and ``POST /v1/orgs``
    deliberately does not call it. That is the whole implementation of "one
    grant, and only for an organization we created at signup", and it is why
    there is no ``created_via`` column: the one caller that knows the answer is
    the one that acts on it.

    Never raises. The accepted outcome of a failure is an organization that
    starts at $0 in one region, visible as an unapplied row; the alternative is
    costing someone their login over a promotional balance.
    """
    settings = get_settings()
    amount = settings.billing.signup_grant_usd
    if amount <= Decimal("0"):
        return
    pool = await db.control_pool()
    for region in settings.regions:
        try:
            grant_id = await pool.fetchval(
                """
                INSERT INTO credit_grants (tenant_id, region, kind, amount)
                VALUES ($1, $2, 'signup_grant', $3)
                ON CONFLICT DO NOTHING
                RETURNING id
                """,
                tenant_id,
                region.slug,
                amount,
            )
        except Exception:
            logger.exception(
                "signup grant for organization %s in %s could not be recorded — it starts at $0 there",
                tenant_id,
                region.slug,
            )
            continue
        if grant_id is None:
            # `uq_credit_grants_signup` refused it: this organization already has
            # one here. A normal outcome on a retried login, not an error.
            continue
        await push_grant_or_topup(
            issuance_id=grant_id,
            kind="signup_grant",
            tenant_id=tenant_id,
            region=region.slug,
            amount=amount,
        )


async def pending_for(*, tenant_id: UUID, region: str) -> list[PendingIssuance]:
    """Everything control has issued for this tenant HERE that is not applied.

    Two partial indexes serve this and both are near-empty, so it costs nothing
    on a page load. It is asked for on demand, by the region, and never swept.
    """
    pool = await db.control_pool()
    rows = await pool.fetch(
        """
        SELECT id, 'purchase' AS kind, amount, id AS topup_id, NULL::text AS note
        FROM credit_topups
        WHERE tenant_id = $1 AND region = $2 AND status = 'paid' AND applied_at IS NULL
        UNION ALL
        SELECT id, 'refund' AS kind, amount, id AS topup_id,
               'reversed by the payment processor' AS note
        FROM credit_topups
        WHERE tenant_id = $1 AND region = $2 AND status = 'refunded' AND reversed_at IS NULL
        UNION ALL
        -- No note: a grant's kind IS its explanation while `signup_grant` is the
        -- only one the CHECK admits, so there is no column to read.
        SELECT id, kind, amount, NULL::uuid AS topup_id, NULL::text AS note
        FROM credit_grants
        WHERE tenant_id = $1 AND region = $2 AND applied_at IS NULL
        """,
        tenant_id,
        region,
    )
    return [
        PendingIssuance(
            issuance_id=row["id"],
            kind=row["kind"],
            amount=row["amount"],
            topup_id=row["topup_id"],
            note=row["note"],
        )
        for row in rows
    ]


async def mark_applied(applied: list[tuple[UUID, str]], *, region: str) -> None:
    """Stamp issuances a region has confirmed writing.

    The kind decides the stamp, which is why the region echoes it back: a top-up
    has two — ``applied_at`` for the credit and ``reversed_at`` for the reversal —
    because both directions are a push and both can fail independently.

    ``region`` is the calling region, from its own token, and every UPDATE is
    scoped by it: a region may only stamp what was issued to IT. Nothing here is
    guarding against a hostile region — they are our own droplets — but
    ``pending_for`` above is scoped the same way, and a write that is looser than
    the read it answers is the asymmetry that becomes a bug the day a slug is
    passed through by mistake.
    """
    if not applied:
        return
    pool = await db.control_pool()
    purchases = [i for i, kind in applied if kind == "purchase"]
    refunds = [i for i, kind in applied if kind == "refund"]
    grants = [i for i, kind in applied if kind not in ("purchase", "refund")]
    if purchases:
        await pool.execute(
            "UPDATE credit_topups SET applied_at = now() WHERE id = ANY($1::uuid[]) "
            "AND region = $2 AND applied_at IS NULL",
            purchases,
            region,
        )
    if refunds:
        await pool.execute(
            "UPDATE credit_topups SET reversed_at = now() WHERE id = ANY($1::uuid[]) "
            "AND region = $2 AND reversed_at IS NULL",
            refunds,
            region,
        )
    if grants:
        await pool.execute(
            "UPDATE credit_grants SET applied_at = now() WHERE id = ANY($1::uuid[]) "
            "AND region = $2 AND applied_at IS NULL",
            grants,
            region,
        )


async def topup_status(topup_id: UUID, *, tenant_id: UUID) -> str | None:
    """The control-side status of one top-up, for the page waiting on it.

    It is what splits the billing page's single vague "we have not seen it" into
    two true sentences — *"still waiting on the payment processor"* when control
    has heard nothing from Dodo, and *"paid — landing now"* when control has the
    payment and the region is applying it. Those send a worried customer to two
    different places.
    """
    pool = await db.control_pool()
    return await pool.fetchval(
        "SELECT status FROM credit_topups WHERE id = $1 AND tenant_id = $2",
        topup_id,
        tenant_id,
    )
