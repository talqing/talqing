"""Session pricing, end-of-session billing, and buying credits.

Import what you need from here::

    from services.billing import bill_session, price_session, SessionUsage
    from services import billing
    await billing.bill_session(tenant, session_id)

Submodules are package-internal; external callers should not import them.

Two halves that meet at the balance, and they run on different nodes.

``pricing``, ``session``, ``usage`` and ``models`` are REGIONAL: they turn a
finished session into money and debit ``services.credits`` in the same
transaction. ``dodo``, ``topups`` and ``issuance`` are CONTROL PLANE: they open a
checkout, take the processor's callback, and push the result to whichever region
the purchase names. Reading and gating the balance itself is ``services.credits``
and never here.
"""

from __future__ import annotations

from .dodo import WebhookSignatureError  # noqa: F401
from .issuance import (  # noqa: F401
    PendingIssuance,
    grant_signup_credit,
    mark_applied,
    pending_for,
    push_grant_or_topup,
    topup_status,
)
from .models import (  # noqa: F401
    AvatarPriceLine,
    AvatarUsage,
    LLMPriceLine,
    LLMUsage,
    PriceBreakdown,
    PriceLine,
    RealtimePriceLine,
    RealtimeUsage,
    SessionUsage,
    STTPriceLine,
    STTUsage,
    TTSPriceLine,
    TTSUsage,
)
from .pricing import (  # noqa: F401
    FAILED_CLOSE_REASONS,
    UnpriceableUsageError,
    is_failed_close_reason,
    normalize_close_reason,
    price_llm_usage,
    price_session,
    session_status_for_close_reason,
)
from .reported_cost import (  # noqa: F401
    USERDATA_REPORTED_COST,
    ReportedCost,
    collector_in,
)
from .session import bill_session, settle_chat  # noqa: F401
from .topups import (  # noqa: F401
    TopupError,
    create_credit_checkout,
    receive_dodo_webhook,
)
from .usage import llm_usage_lines  # noqa: F401

__all__ = [
    "USERDATA_REPORTED_COST",
    "AvatarPriceLine",
    "AvatarUsage",
    "FAILED_CLOSE_REASONS",
    "LLMPriceLine",
    "LLMUsage",
    "PendingIssuance",
    "PriceBreakdown",
    "PriceLine",
    "RealtimePriceLine",
    "ReportedCost",
    "RealtimeUsage",
    "STTPriceLine",
    "STTUsage",
    "SessionUsage",
    "TTSPriceLine",
    "TTSUsage",
    "TopupError",
    "UnpriceableUsageError",
    "WebhookSignatureError",
    "bill_session",
    "collector_in",
    "create_credit_checkout",
    "grant_signup_credit",
    "is_failed_close_reason",
    "llm_usage_lines",
    "mark_applied",
    "normalize_close_reason",
    "pending_for",
    "price_llm_usage",
    "push_grant_or_topup",
    "price_session",
    "receive_dodo_webhook",
    "session_status_for_close_reason",
    "settle_chat",
    "topup_status",
]
