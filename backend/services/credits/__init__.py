"""Prepaid credits: the balance, the ledger behind it, and the gate on a new call.

Import what you need from here::

    from services import credits
    if not await credits.has_credit(ctx.tenant): ...

Only this package may query ``credit_accounts`` or ``credit_ledger``. Submodules
are package-internal; external callers should not import them.

The balance is USD, and it is drawn down by ``sessions.platform_fee`` alone —
under BYOK the provider cost beside it is the tenant's own spend on their own
key. It is also PER REGION, because this is a data-plane table: taking a payment
is control's (``services/billing/topups.py``), and what control decides arrives
here through ``apply_issuance`` or ``reconcile_pending``. Everything that reads or
moves the balance is here.
"""

from __future__ import annotations

from .models import (  # noqa: F401
    INSUFFICIENT_CREDITS_CLOSE_REASON,
    INSUFFICIENT_CREDITS_MESSAGE,
    CreditBalanceResponse,
    CreditCheckoutRequest,
    CreditCheckoutResponse,
    CreditLedgerEntryResponse,
    CreditPackId,
    CreditPackResponse,
    IssuedKind,
    LedgerKind,
    TopupStatus,
)
from .service import (  # noqa: F401
    adjust,
    apply_issuance,
    balance,
    balance_summary,
    debit_session_fee,
    has_credit,
    list_ledger,
    reconcile_pending,
)

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
    "adjust",
    "apply_issuance",
    "balance",
    "balance_summary",
    "debit_session_fee",
    "has_credit",
    "list_ledger",
    "reconcile_pending",
]
