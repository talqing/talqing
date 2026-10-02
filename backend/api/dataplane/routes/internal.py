"""What the control plane asks this region.

One route, and there is only ever one: applying a credit issuance. The balance is
a data-plane table because the debit shares a transaction with
``sessions.platform_fee`` on the per-call settlement path, and control owns the
payment because Dodo has one business and knows nothing about our regions — so
this is where those two facts meet.

Authenticated by ``control.internal_token`` — the one secret this region shares
with the control plane, used in both directions: the region sends it when it
calls control, and accepts it here when control calls back. It must equal
control's ``regions[<this region>].internal_token``; ``settings._internal_token``
is where that rule is written down. Not in any document, SDK or MCP tool list.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from hmac import compare_digest
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from api.core.schemas import OkResponse
from services import credits
from services.credits import IssuedKind
from settings import get_settings

logger = logging.getLogger("talqing.api.internal")


async def _from_control(request: Request) -> None:
    """Constant-time, and it accepts ``previous_internal_token`` too — which is
    what makes rotating the shared secret two ordered deploys rather than a 401
    storm for the minutes between them."""
    cfg = get_settings().control
    authz = request.headers.get("authorization", "")
    token = authz[7:].strip() if authz.lower().startswith("bearer ") else ""
    if not cfg or not token or not any(compare_digest(token, k) for k in cfg.accepted_tokens):
        raise HTTPException(status_code=401, detail="not the control plane")


router = APIRouter(
    prefix="/internal/v1", include_in_schema=False, dependencies=[Depends(_from_control)]
)


class ApplyCreditRequest(BaseModel):
    tenant_id: UUID
    # `signup_grant`, `purchase` or `refund` — the three kinds with a partial
    # unique index on `credit_ledger`. That index is what makes a control push
    # racing a billing-page reconcile credit exactly once, which is why the type
    # refuses anything else rather than trusting the caller.
    kind: IssuedKind
    # A string on the wire, parsed into Decimal here: this is money, and a JSON
    # float would round it on the way to a NUMERIC(12,6) column. Always positive
    # — `kind` decides the sign.
    amount: Decimal
    topup_id: UUID | None = None
    note: str | None = None


@router.post("/credits/apply", response_model=OkResponse)
async def apply_credit(body: ApplyCreditRequest) -> OkResponse:
    """Write one issuance from control into this region's ledger.

    Idempotent, and deliberately answers 200 when the ledger's unique index
    refuses a duplicate: a redelivered push and a reconcile that raced it are
    both successes, not conflicts.

    **The tenant id is taken as given, and there is no existence check.** Looking
    one up would be a round trip back to the very process making this request,
    for a row it read to get here — and it would guard against nothing: the id
    comes off control's own `credit_topups` or `credit_grants` row, both of which
    cascade from `tenants`, so a deleted organization takes its issuances with
    it. What routing needs is the id alone (`db.tenant_pool_for_id`), and the
    ledger row this writes carries no foreign key to anything a lookup could
    confirm.
    """
    await credits.apply_issuance(
        body.tenant_id,
        kind=body.kind,
        amount=body.amount,
        topup_id=body.topup_id,
        note=body.note,
    )
    return OkResponse()
