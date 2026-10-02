"""Reading, moving and gating a workspace's prepaid credit balance.

Two rules govern this file. Both are easy to violate by accident and neither is
recoverable once it has reached a customer.

**The debit is ``sessions.platform_fee``, never ``total_charge``.** Under strict
BYOK the provider cost on a session is the tenant's own spend on their own key —
money that never passes through us. Debiting it from a balance we hold charges
them a second time for what they already paid their provider.

**A pack credits exactly what it says.** The credited amount is
``credit_topups.amount``, written by us from config before the buyer ever reaches
Dodo. FX, tax, Dodo's fee and ``settlement_amount`` all move the money we
*receive*; none of them touch the credit, and there is no code path here that
credits a partial amount.

**A balance is per region**, because it is a data-plane table and every region
has its own. A customer with $50 in India and $0 in the US has calls refused in
the US, so nothing in the product may show a bare number: the dashboard labels
every balance, ledger, pack list and low-balance banner with the region it
belongs to. Credit that control decides on — a purchase, a signup grant, a
reversal — arrives through :func:`apply_issuance`, pushed by control or pulled by
:func:`reconcile_pending`; nothing else crosses a plane.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import get_args
from uuid import UUID

import asyncpg

import db
from api.core.schemas import Page, page_slice
from services.catalog import get_catalog
from services.control import client as control
from services.user import Tenant
from settings import get_settings

from .models import (
    CreditBalanceResponse,
    CreditLedgerEntryResponse,
    CreditPackResponse,
    IssuedKind,
    LedgerKind,
)

# The kinds control may issue across the plane boundary, read off the type that
# already names them rather than written a second time — a list like this one
# pasted twice is what `services/user/tenants.py` exists to warn about. Each has
# a partial unique index on `credit_ledger`; see `apply_issuance`.
ISSUED_KINDS: tuple[str, ...] = get_args(IssuedKind)

logger = logging.getLogger("talqing.credits")

_ZERO = Decimal("0")


async def _apply(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    amount: Decimal,
    kind: LedgerKind,
    session_id: UUID | None = None,
    segment_id: UUID | None = None,
    topup_id: UUID | None = None,
    note: str | None = None,
) -> Decimal | None:
    """Move the balance and record why, on the caller's connection.

    Every movement in either direction goes through here, so there is exactly one
    place that knows how a balance changes.

    Returns the new balance, or ``None`` when a unique index refused the entry —
    which is the idempotency guarantee, not an error: a redelivered webhook, a
    re-priced session and a retried grant all land here and all must be no-ops.

    The UPDATE runs before the INSERT because its ``RETURNING`` is
    ``balance_after``, and because it takes the row lock that serializes
    concurrent movements on one organization. The INSERT then either commits with
    it or takes it down — which is why it carries no ``ON CONFLICT DO NOTHING``.
    Swallowing the conflict while leaving the UPDATE committed would be a silent
    double-move, the one bug here that stays invisible until a customer's ledger
    disagrees with their balance.

    asyncpg nests transactions as savepoints, so when ``debit_session_fee`` calls
    this from inside ``bill_session``'s transaction a duplicate debit rolls back
    only the ledger attempt and leaves the session's money columns intact.
    """
    try:
        async with conn.transaction():
            # Upsert first so no caller has to guarantee the account row exists.
            await conn.execute(
                "INSERT INTO credit_accounts (tenant_id) VALUES ($1) ON CONFLICT DO NOTHING",
                tenant_id,
            )
            balance_after = await conn.fetchval(
                """
                UPDATE credit_accounts
                SET balance = balance + $2, updated_at = now()
                WHERE tenant_id = $1
                RETURNING balance
                """,
                tenant_id,
                amount,
            )
            await conn.execute(
                """
                INSERT INTO credit_ledger
                    (tenant_id, kind, amount, balance_after, session_id, segment_id,
                     topup_id, note)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                """,
                tenant_id,
                kind,
                amount,
                balance_after,
                session_id,
                segment_id,
                topup_id,
                note,
            )
    except asyncpg.UniqueViolationError:
        logger.info("credits: %s entry for tenant %s already exists; no movement", kind, tenant_id)
        return None
    return balance_after


async def balance(tenant: Tenant) -> Decimal:
    """This organization's spendable balance, in USD.

    A missing row means the organization has never had a balance, which is a
    zero — not an error, and never an "allow".
    """
    pool = await db.tenant_pool(tenant)
    current = await pool.fetchval(
        "SELECT balance FROM credit_accounts WHERE tenant_id = $1", tenant.id
    )
    return _ZERO if current is None else current


async def has_credit(tenant: Tenant) -> bool:
    """True when this organization may START a new billable call.

    ``balance > 0``, and nothing more. No reservation, no floor, no forecast of
    what the call might cost: a call already running is never interrupted, so the
    only question at the door is whether there is anything left at all. The
    balance can therefore end a call negative by that call's own fee, and the
    next top-up pays the debt off first because it is a signed ledger.

    Deliberately not cached. A top-up must take effect on the very next call —
    the same reason the auth context is re-read on every request
    rather than trusting the JWT.

    **Nothing on a call's path may scan a growing table.** This is a primary-key
    lookup on a one-row-per-tenant table, and the debit is a primary-key UPDATE
    plus one INSERT. Neither aggregates over ``credit_ledger`` and neither touches
    ``sessions`` in aggregate — that is the whole reason the balance is
    materialized. A later change that makes this read anything growing with call
    volume is wrong.
    """
    return await balance(tenant) > _ZERO


async def apply_issuance(
    tenant_id: UUID,
    *,
    kind: IssuedKind,
    amount: Decimal,
    topup_id: UUID | None = None,
    note: str | None = None,
) -> Decimal | None:
    """Write one credit issuance from the control plane into this region's ledger.

    The single entry point for every movement control decides on — a paid
    top-up, a signup grant, a reversal — whether it arrives as a push from
    control or is pulled back by :func:`reconcile_pending`. Both routes land here,
    which is what makes a reconcile racing a late push credit exactly once.

    A tenant ID and not a ``Tenant``, unlike everything else in this module: a
    push arrives from control with an id and nothing else, and the routing this
    needs is by id anyway (``db.tenant_pool_for_id``). Taking a ``Tenant`` would
    mean the region fetching one — a round trip BACK to control for a row control
    already had, on the login path, once per region.

    ``amount`` is always positive and ``kind`` decides the sign: a reversal is a
    ``refund``, and the balance may go negative as a result. That is correct —
    money we will not receive must not stay spendable, and the next top-up pays
    the debt off first because it is a signed ledger.

    Idempotency is a unique index per kind, and there is one for each of the
    three: ``uq_credit_ledger_purchase``, ``uq_credit_ledger_refund`` and
    ``uq_credit_ledger_signup``. ``None`` back means an index refused it, which is
    the guarantee working rather than an error.
    """
    if kind not in ISSUED_KINDS:
        # `adjustment` is the one that would be unsafe: nothing on `credit_ledger`
        # makes a second one a no-op, so a re-pushed promotional credit would
        # credit twice. Making it safe is a `grant_id` column, and that is a
        # data-plane migration nobody is owed until there is a promotion to run.
        raise ValueError(
            f"credit kind {kind!r} cannot be issued across planes — only "
            f"{', '.join(ISSUED_KINDS)} have a unique index that makes the push idempotent"
        )
    if amount <= _ZERO:
        raise ValueError("an issued credit amount must be positive; the kind decides the sign")
    pool = await db.tenant_pool_for_id(tenant_id)
    async with pool.acquire() as conn:
        return await _apply(
            conn,
            tenant_id=tenant_id,
            amount=-amount if kind == "refund" else amount,
            kind=kind,
            topup_id=topup_id,
            note=note,
        )


async def reconcile_pending(tenant: Tenant, topup_id: UUID | None = None) -> str | None:
    """Apply anything control has issued here that this region has not written.

    The backstop for a push that failed — a region restarting, a network blip —
    and it is a demand reconcile rather than a timer. The moment someone cares is
    the moment the billing page loads: Dodo's return URL sends a buyer straight
    there, and a tenant whose calls are being refused follows the credit banner's
    own link. Free when there is nothing to apply, which is almost always.

    Returns the control-side status of ``topup_id`` when the caller named one, so
    the page can say which of two things it is waiting for rather than one vague
    sentence.

    Never raises. A control plane that cannot be reached must not turn "here is
    your balance" into an error — the balance below is this region's own row and
    is perfectly readable without it.
    """
    try:
        pending = await control.pending_credits(tenant_id=tenant.id, topup_id=topup_id)
    except Exception:
        logger.exception("credits: could not ask the control plane what is unapplied")
        return None

    applied: list[dict[str, str]] = []
    for issuance in pending.issuances:
        try:
            await apply_issuance(
                tenant.id,
                kind=issuance.kind,
                amount=issuance.amount,
                topup_id=issuance.topup_id,
                note=issuance.note,
            )
        except Exception:
            logger.exception(
                "credits: could not apply issuance %s (%s) for tenant %s",
                issuance.issuance_id,
                issuance.kind,
                tenant.id,
            )
            continue
        applied.append({"issuance_id": str(issuance.issuance_id), "kind": issuance.kind})
    if applied:
        try:
            await control.confirm_applied(applied)
        except Exception:
            # The credit landed; only the stamp did not. The next reconcile
            # re-applies into the unique index and confirms again — which is
            # exactly why idempotency is on the region's side.
            logger.exception("credits: applied %d issuance(s) but could not confirm", len(applied))
    return pending.topup_status


async def debit_session_fee(
    conn: asyncpg.Connection,
    tenant_id: UUID,
    session_id: UUID,
    fee: Decimal,
    *,
    segment_id: UUID | None = None,
) -> None:
    """Take a session's PLATFORM FEE out of the balance, on the caller's open
    transaction.

    The platform fee, never ``total_charge``: under BYOK the provider cost beside
    it is the tenant's own spend on their own key, already paid to the provider
    directly, and debiting it here would charge them twice for it.

    A zero fee writes nothing. A failed call has it waived
    (``services/billing/pricing.py::is_failed_close_reason``) and a chat window
    that answered nothing owes nothing — a row saying "we took nothing" is noise
    in a ledger a customer reads.

    A call is debited once: idempotent per session by ``uq_credit_ledger_session``,
    so re-pricing it through ``backfill_call_analysis`` cannot debit it twice. A
    chat is debited once per ``segment_id`` — see ``billing.settle_chat``, which
    passes the difference it still owes.
    """
    if fee <= _ZERO:
        return
    await _apply(
        conn,
        tenant_id=tenant_id,
        amount=-fee,
        kind="usage",
        session_id=session_id,
        segment_id=segment_id,
    )


async def adjust(tenant: Tenant, amount: Decimal, note: str) -> Decimal | None:
    """A human moving a balance, with a reason. Signed.

    The repair path, not a product feature — which is why there is no endpoint
    for it and no CoPilot operation. It exists so a correction (a signup grant
    whose write failed, a goodwill credit, draining a balance to test the gate)
    goes through ``_apply`` like everything else, rather than being hand-written
    SQL that leaves the balance and the ledger disagreeing.
    """
    if not note.strip():
        raise ValueError("an adjustment must say why")
    pool = await db.tenant_pool(tenant)
    async with pool.acquire() as conn:
        return await _apply(conn, tenant_id=tenant.id, amount=amount, kind="adjustment", note=note)


async def list_ledger(tenant: Tenant, limit: int, offset: int) -> Page[CreditLedgerEntryResponse]:
    """One page of the ledger, newest first.

    The page a customer opens when they think they were overcharged, so every row
    carries the balance it produced and the pointer that explains it. Ordered by
    id as well as time, because a top-up and the call it paid for can land in the
    same microsecond and the order they are read in must not vary.
    """
    pool = await db.tenant_pool(tenant)
    rows = await pool.fetch(
        """
        SELECT id, kind, amount, balance_after, session_id, topup_id, note, created_at
        FROM credit_ledger
        WHERE tenant_id = $1
        ORDER BY created_at DESC, id DESC
        LIMIT $2 OFFSET $3
        """,
        tenant.id,
        limit + 1,
        offset,
    )
    return page_slice(
        [CreditLedgerEntryResponse.model_validate(dict(r)) for r in rows],
        limit=limit,
        offset=offset,
    )


def _minutes_at(amount: Decimal, rate_per_minute: float) -> int:
    """How many minutes ``amount`` buys at this channel's rate.

    A rate of zero means the channel is free, and a free channel does not draw on
    this balance at all — so it has no minutes to report rather than infinitely
    many.
    """
    if rate_per_minute <= 0 or amount <= _ZERO:
        return 0
    return int(amount / Decimal(str(rate_per_minute)))


async def balance_summary(tenant: Tenant) -> CreditBalanceResponse:
    """The balance, what it buys at today's rates, and what a top-up costs.

    The minutes are served rather than computed in the browser for the same
    reason the catalog's languages are: the dashboard and the API must not be
    able to disagree about what a dollar of credit is worth.
    """
    settings = get_settings().billing
    rates = get_catalog().platform_fee_per_minute
    current = await balance(tenant)
    return CreditBalanceResponse(
        balance=float(current),
        voice_minutes_remaining=_minutes_at(current, rates.voice),
        video_minutes_remaining=_minutes_at(current, rates.video),
        low_balance_threshold=float(settings.low_balance_warning_usd),
        packs=[
            CreditPackResponse(
                id=pack.id,
                amount=float(pack.amount),
                voice_minutes=_minutes_at(pack.amount, rates.voice),
            )
            for pack in settings.packs
        ],
    )
