"""Credits: this region's balance, its ledger, and buying more.

The balance is what the platform charges against — the per-minute platform fee on
a voice or video call, and nothing else. Provider spend is the tenant's own, on
their own key, and never passes through here.

**A workspace has one balance per region.** It is a data-plane table, because the
debit shares a transaction with `sessions.platform_fee` on the per-call
settlement path, and moving it to the control plane would make one shared
database a synchronous dependency of every region's call settlement. So a
customer with $50 in India and $0 in the US has calls refused in the US, and the
dashboard labels every balance, ledger and pack list with the region it belongs
to rather than showing a bare number.

Only the payment itself is control's: the Dodo key lives in one place, and the
processor's callback arrives with no region context.
"""

from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query

from api.core.schemas import Page
from api.dataplane.deps import AdminCtxDep, Context, CtxDep
from services import credits
from services.control import client as control
from services.credits import (
    CreditBalanceResponse,
    CreditCheckoutRequest,
    CreditCheckoutResponse,
    CreditLedgerEntryResponse,
)

logger = logging.getLogger("talqing.api.billing")

router = APIRouter(prefix="/billing", tags=["billing"])


@router.get("/credits", response_model=CreditBalanceResponse)
async def get_credits(
    ctx: Context = CtxDep,
    topup: UUID | None = Query(
        default=None,
        description="A top-up to report the payment processor's own status for.",
    ),
) -> CreditBalanceResponse:
    """This workspace's credit balance in this region, what it buys, and what a
    top-up costs.

    Readable by every role: a VIEWER can start a web call, so a VIEWER must be
    able to see why one was refused.

    This is also where credit the control plane could not deliver lands. Anything
    it has issued for this workspace here — a purchase, a signup grant, a
    reversal — that this region has not written yet is applied before the balance
    is read. That is the backstop for a failed push, and it is a reconcile on
    demand rather than a timer: the moment someone cares is the moment this page
    loads, whether they arrived from the payment processor or from the "out of
    credits" banner. It costs nothing when there is nothing to apply, which is
    almost always.
    """
    topup_status = await credits.reconcile_pending(ctx.tenant, topup)
    summary = await credits.balance_summary(ctx.tenant)
    summary.topup_status = topup_status
    return summary


@router.get("/credits/ledger", response_model=Page[CreditLedgerEntryResponse])
async def list_credit_ledger(
    ctx: Context = CtxDep,
    limit: int = Query(200, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> Page[CreditLedgerEntryResponse]:
    """Every movement of this region's balance, newest first, with what caused each."""
    return await credits.list_ledger(ctx.tenant, limit, offset)


@router.post("/credits/checkout", response_model=CreditCheckoutResponse)
async def create_credit_checkout(
    body: CreditCheckoutRequest, ctx: Context = AdminCtxDep
) -> CreditCheckoutResponse:
    """Start a credit purchase and return the hosted checkout URL to open.

    Organization admins only — spending the workspace's money is an admin act,
    the same as storing a secret or a provider key.

    The credit lands in THIS region, and that is decided by which region's API
    you called rather than by anything in the request. A slug in a body could be
    pointed at the other region, where the money would then be stuck: balances
    are per region and there is no transfer between them.
    """
    try:
        session = await control.start_checkout(
            tenant_id=ctx.tenant.id,
            user_id=ctx.user.id,
            user_email=ctx.user.email,
            user_name=ctx.user.name,
            pack=body.pack,
        )
    except control.ControlRefused as exc:
        # Control refusing this region's own credential is a deployment fault,
        # not the buyer's — so it reads as unavailable rather than unauthorized.
        logger.error("credit checkout refused by the control plane: %s", exc)
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except control.ControlPlaneError as exc:
        raise HTTPException(
            status_code=503,
            detail=(
                "Credits cannot be purchased right now. Try again in a moment — if it "
                "keeps happening, email hello@talqing.com and we will sort it out."
            ),
        ) from exc
    return CreditCheckoutResponse.model_validate(session)
