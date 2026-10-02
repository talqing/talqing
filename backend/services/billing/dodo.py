"""Dodo Payments: create a hosted checkout, read a payment, verify a callback.

Dodo is our Merchant of Record. It shows the buyer a checkout page, calculates
and remits tax, converts currency, issues the invoice, and tells us what
happened over a signed webhook. Two endpoints are all we call:

  create checkout  https://docs.dodopayments.com/api-reference/checkout-sessions/create
  payment object   https://docs.dodopayments.com/api-reference/payments/get-payments-1
  webhooks         https://docs.dodopayments.com/developer-resources/webhooks

One outbound call — the checkout — plus a Standard Webhooks signature check on
the way back. Hand-rolled `httpx` rather than the `dodopayments` SDK, because
that is all of it and because every other provider here is hand-rolled the same
way. There is deliberately no payment re-fetch: `payment.succeeded` carries the
whole Payment object, and a refund or a dispute carries the `payment_id` our own
row is already keyed on.

**None of the money on a payment ever becomes a credit.** `total_amount` includes
tax and, under adaptive currency, is denominated in whatever the buyer paid;
`settlement_amount` is what lands in the Dodo balance after conversion and fees.
The credited amount is `credit_topups.amount`, which we wrote from config before
the buyer ever reached Dodo. What the payment's amounts are for is the refusal
check in `topups.py` and the support record beside it.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import time

import httpx
from pydantic import BaseModel, ConfigDict

from settings import get_settings

logger = logging.getLogger("talqing.billing.dodo")

# Generous next to a call that normally answers well under a second, and short
# enough that a hung provider does not hold a request open for minutes.
_HTTP_TIMEOUT_SECONDS = 15.0

# Standard Webhooks' recommended replay window. A callback signed further from
# now than this is refused whatever its signature says.
_WEBHOOK_TOLERANCE_SECONDS = 300


class DodoError(RuntimeError):
    """Dodo answered, and said no — or could not be reached at all."""


# ── request / response shapes ────────────────────────────────────────────────


class ProductItem(BaseModel):
    product_id: str
    quantity: int = 1


class NewCustomer(BaseModel):
    email: str
    name: str | None = None


class CheckoutSessionRequest(BaseModel):
    """The body we send, and nothing more.

    Deliberately absent:

    - `billing_currency`, which Dodo ignores unless adaptive currency is on and
      picks for itself when it is. The platform prices in USD and lets Dodo do FX.
    - `confirm: true`, which drops the session's life from 24 hours to 15 minutes
      and returns a client secret for an embedded SDK we do not use.
    """

    product_cart: list[ProductItem]
    customer: NewCustomer
    return_url: str
    # Readable in the Dodo dashboard, NOT routing: `credit_topups` finds the row
    # by `checkout_session_id`, so a dropped metadata field cannot strand a
    # payment. Keys cap at 40 chars and values at 500; two UUIDs fit.
    metadata: dict[str, str]
    # `credit` and `debit` are the documented fallback and must always be present
    # — a session whose every method is unavailable fails outright. `upi_collect`
    # is what puts UPI on the sheet for an Indian buyer.
    allowed_payment_method_types: list[str] = ["credit", "debit", "upi_collect"]


class CheckoutSessionResponse(BaseModel):
    """Dodo's answer. `checkout_url` is null only when `payment_method_id` was
    sent, which we never do — so a null here means the API changed under us."""

    model_config = ConfigDict(extra="ignore")

    session_id: str
    checkout_url: str


class DodoProductCartItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    product_id: str
    quantity: int


class DodoPayment(BaseModel):
    """The Payment object, as `GET /payments/{id}` and `payment.*` both carry it.

    Only the fields we act on. `extra="ignore"` because Dodo adds fields to this
    object routinely and a webhook that 500s on a new one would sit in their
    retry queue for ten hours.
    """

    model_config = ConfigDict(extra="ignore")

    payment_id: str
    # What finds our `credit_topups` row. Nullable: a payment link somebody found,
    # or a storefront purchase, has no checkout session of ours behind it.
    checkout_session_id: str | None = None
    status: str | None = None
    # Smallest unit of `currency`, INCLUDING tax.
    total_amount: int
    tax: int | None = None
    currency: str
    # What lands in the Dodo balance after conversion. Recorded, never decisive.
    settlement_amount: int | None = None
    settlement_currency: str | None = None
    product_cart: list[DodoProductCartItem] | None = None
    # Logged, so a payment for the other brand on this business arriving here is
    # visible rather than mysterious.
    brand_id: str | None = None


class DodoRefund(BaseModel):
    """The Refund object carried by `refund.succeeded`."""

    model_config = ConfigDict(extra="ignore")

    refund_id: str
    payment_id: str
    status: str
    is_partial: bool
    amount: int | None = None
    currency: str | None = None


class DodoDispute(BaseModel):
    """The Dispute object carried by `dispute.*`.

    `amount` is a STRING here and an integer on a payment — Dodo's own choice,
    and the reason this is not the same model. We never do arithmetic on it.
    """

    model_config = ConfigDict(extra="ignore")

    dispute_id: str
    payment_id: str
    amount: str
    currency: str
    dispute_status: str


class DodoWebhookEnvelope(BaseModel):
    """`{business_id, type, timestamp, data}`, the shape every event shares."""

    model_config = ConfigDict(extra="ignore")

    business_id: str
    type: str
    data: dict


# ── the one call ─────────────────────────────────────────────────────────────


def _auth_headers() -> dict[str, str]:
    api_key = get_settings().billing.dodo.api_key
    if not api_key:
        raise DodoError("Dodo Payments is not configured (billing.dodo.api_key is blank)")
    return {"Authorization": f"Bearer {api_key}"}


async def create_checkout_session(body: CheckoutSessionRequest) -> CheckoutSessionResponse:
    """Open a hosted checkout and return where to send the buyer.

    **Never retried.** A retried create is a second checkout session for one
    top-up, and `credit_topups.dodo_checkout_session_id` is UNIQUE — so a retry
    either fails the insert or leaves a payable link nothing can resolve. Let the
    user press the button again.
    """
    base = get_settings().billing.dodo.base_url
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
        try:
            response = await client.post(
                f"{base}/checkouts",
                headers=_auth_headers(),
                json=body.model_dump(exclude_none=True),
            )
        except httpx.HTTPError as exc:
            raise DodoError(f"could not reach Dodo Payments: {exc}") from exc
    if response.status_code >= 400:
        raise DodoError(f"Dodo Payments refused the checkout: {_error_text(response)}")
    return CheckoutSessionResponse.model_validate(response.json())


def _error_text(response: httpx.Response) -> str:
    return f"HTTP {response.status_code} {response.text[:300]}".strip()


# ── signature verification ───────────────────────────────────────────────────


class WebhookSignatureError(Exception):
    """This callback is not one of ours, or not intact."""


def verify_webhook_signature(
    *,
    secret: str,
    headers: dict[str, str],
    raw_body: bytes,
    now: float | None = None,
) -> None:
    """Raise unless one of the offered signatures is ours.

    Dodo signs with Standard Webhooks (their SDK's `unwrap` is
    `standardwebhooks.Webhook(key).verify`), which is the Svix scheme: the signed
    content is `{id}.{timestamp}.{body}`, HMAC-SHA256 under the base64-decoded
    half of a `whsec_`-prefixed secret, and the header carries a space-separated
    list of `v1,<base64>` because a secret being rotated means two are valid at
    once.

    Shared with anything else Svix-shaped — this is the same function we wrote
    for Resend delivery webhooks and deleted with them in `73c49e6`. It is also
    why hand-rolling Dodo needs no new dependency.

    `raw_body` must be the exact bytes off the wire: the signature is over them,
    not over anything a parser has been through.
    """
    webhook_id = headers.get("webhook-id") or ""
    timestamp = headers.get("webhook-timestamp") or ""
    signature_header = headers.get("webhook-signature") or ""
    if not webhook_id or not timestamp or not signature_header:
        raise WebhookSignatureError("this callback is missing its Standard Webhooks headers")

    try:
        sent_at = int(timestamp)
    except ValueError as exc:
        raise WebhookSignatureError("this callback's timestamp is not a number") from exc
    if abs((time.time() if now is None else now) - sent_at) > _WEBHOOK_TOLERANCE_SECONDS:
        raise WebhookSignatureError("this callback's timestamp is too far from now")

    key = secret.split("_", 1)[1] if secret.startswith("whsec_") else secret
    try:
        secret_bytes = base64.b64decode(key, validate=True)
    except (ValueError, TypeError) as exc:
        raise WebhookSignatureError(
            "the configured Dodo webhook signing secret is not a Standard Webhooks secret — "
            "paste the whsec_… value from Dodo's webhook page"
        ) from exc

    signed = f"{webhook_id}.{timestamp}.".encode() + raw_body
    expected = base64.b64encode(hmac.new(secret_bytes, signed, hashlib.sha256).digest()).decode()
    for part in signature_header.split():
        version, _, candidate = part.partition(",")
        if version != "v1" or not candidate:
            continue
        if hmac.compare_digest(candidate, expected):
            return
    raise WebhookSignatureError("this callback's signature does not match")
