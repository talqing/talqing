"""What a credit balance, a ledger entry and a pack look like to a reader.

Response models live here rather than in the router for the same reason they do
in ``services/calls`` and ``services/secrets``: the shape is the domain's, and
the router is one of several things that may render it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from settings.settings import CreditPackId

__all__ = [
    "INSUFFICIENT_CREDITS_CLOSE_REASON",
    "INSUFFICIENT_CREDITS_MESSAGE",
    "CreditBalanceResponse",
    "CreditCheckoutRequest",
    "CreditCheckoutResponse",
    "CreditLedgerEntryResponse",
    "CreditPackId",
    "CreditPackResponse",
    "IssuedKind",
    "LedgerKind",
    "TopupStatus",
]

# One sentence, said by the API, the dashboard and the batch page alike. A
# workspace that hits this reads it in three places and must not find three
# different explanations of the same state.
INSUFFICIENT_CREDITS_MESSAGE = (
    "This workspace is out of credits. Add credits in Settings → Billing to "
    "start new calls. Calls already in progress are unaffected."
)

# `sessions.close_reason` for an inbound call refused at the door. Bucketed as
# `configuration` in services/close_reasons.py — the fix is the tenant's — and
# listed in FAILED_CLOSE_REASONS so a call refused for want of credit is never
# charged the fee it was refused for lacking.
INSUFFICIENT_CREDITS_CLOSE_REASON = "insufficient_credits"

# Mirrors the CHECK on control.credit_topups.status. 'pending' means the
# processor has told us nothing yet; the rest are terminal.
TopupStatus = Literal["pending", "paid", "failed", "cancelled", "refunded"]

# Mirrors the CHECK on credit_ledger.kind.
LedgerKind = Literal["signup_grant", "purchase", "usage", "refund", "adjustment"]

# The subset the CONTROL plane may issue across the plane boundary. Each of the
# three has a partial unique index on `credit_ledger`, which is what makes a push
# and a reconcile racing each other credit exactly once. `usage` is the region's
# own debit and `adjustment` is a human repairing one balance in place; neither
# crosses.
IssuedKind = Literal["signup_grant", "purchase", "refund"]


class CreditLedgerEntryResponse(BaseModel):
    """A ledger row, as the billing page reads it.

    Money is `float` here, matching `CostResponse` — the API has spoken JSON
    numbers for every other money field since it existed, and one column in one
    string would only make a client parse two shapes.
    """

    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    id: UUID
    kind: LedgerKind
    amount: float
    balance_after: float
    # Exactly one of these explains the entry: a session for `usage`, a top-up
    # for `purchase` and `refund`, a note for `adjustment`. `signup_grant` has
    # none — its kind is the whole explanation.
    session_id: UUID | None = None
    topup_id: UUID | None = None
    note: str | None = None
    created_at: datetime


class CreditPackResponse(BaseModel):
    """One pack the workspace may buy, priced in USD."""

    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    id: CreditPackId
    amount: float
    # What the pack buys at today's voice rate, so a buyer picks a size against
    # their own usage rather than against a dollar figure.
    voice_minutes: int


class CreditBalanceResponse(BaseModel):
    """This region's balance for the workspace, what it buys, and top-up prices.

    Per region, because the balance is a data-plane table. A caller reading this
    is reading the balance of whichever region's API they called, and the
    dashboard labels it with that region's name — a bare number is what makes a
    customer with $50 in one region and $0 in another think the money vanished.
    """

    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    balance: float
    currency: Literal["USD"] = "USD"
    # What the balance buys at today's rates, so the number means something
    # without the reader doing arithmetic against the catalog.
    voice_minutes_remaining: int
    video_minutes_remaining: int
    # Below this, the dashboard warns without blocking. Served rather than
    # hard-coded in the browser so the threshold has one definition.
    low_balance_threshold: float
    packs: list[CreditPackResponse]
    # Only set when the caller named a `topup` it is waiting on. It is the
    # PROCESSOR's view of that payment, which is a different question from
    # whether the credit has reached this region — and the difference is what
    # lets the billing page say "still waiting on the payment processor" or
    # "paid — landing now" instead of one sentence that covers both.
    topup_status: TopupStatus | None = None


class CreditCheckoutRequest(BaseModel):
    """Which pack to buy. Never an arbitrary amount — the packs are the prices."""

    pack: CreditPackId


class CreditCheckoutResponse(BaseModel):
    """Where to send the buyer, and the top-up that is waiting on them."""

    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    checkout_url: str
    topup_id: UUID
