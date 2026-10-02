"""Buying credits: the ``credit_topups`` row, and what Dodo tells us about it.

**Control plane only.** ``credit_topups`` lives in the control plane — one row
per purchase, never per call, carrying the payment identifiers and who paid — and
so does the Dodo API key, because there is exactly one webhook endpoint and it
arrives with no region context. A region forwards a checkout here and never holds
the key.

**The row is written before the buyer is redirected, and that is what makes it
the authority on the credited amount.** A payment whose checkout session we did
not create credits nobody, and a payment for a pack whose price has since moved
in the Dodo dashboard credits nobody either — the amount check is a REFUSAL gate,
never an adjustment. There is no path here that credits a partial amount.

**The row is also what decides WHERE the money lands.** ``region`` is stamped
from the credential of the region whose API started the checkout, before the
buyer ever reaches Dodo — never from a request body someone could point at the
other region, where the money would then be stuck, since there is no transfer.
Dodo's ``metadata`` carries it too, for a support reader looking at their
dashboard, and it is never read back: a dropped metadata field must not be able
to strand a payment.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from uuid import UUID, uuid4

import db
from services.credits import CreditCheckoutResponse, CreditPackId
from settings import get_settings
from settings.settings import CreditPackConfig

from . import dodo
from .issuance import push_grant_or_topup

logger = logging.getLogger("talqing.billing.topups")

# Dodo's `IntentStatus` for a payment that went through. Everything else on a
# `payment.succeeded` would be a contradiction, and is refused as one.
_SUCCEEDED = "succeeded"


class TopupError(RuntimeError):
    """The purchase cannot be started. Carries a message meant for the BUYER.

    Deliberately never the provider's own text. A `401` from Dodo means our API
    key is wrong, which is ours to fix and nothing the person pressing the button
    can act on — so the status and body go to the log and they get a sentence
    about what to do next.
    """


# One sentence for every way starting a purchase can fail, because the buyer's
# next move is the same in all of them and none of the causes are theirs.
_CHECKOUT_UNAVAILABLE = (
    "Credits cannot be purchased right now. Try again in a moment — if it keeps "
    "happening, email hello@talqing.com and we will sort it out."
)


def _pack(pack_id: CreditPackId) -> CreditPackConfig:
    for pack in get_settings().billing.packs:
        if pack.id == pack_id:
            return pack
    # The request model is a Literal over every pack the product sells, so this
    # is not a malformed request — it is an environment that does not sell this
    # one, which is a configuration fact and not the buyer's problem.
    logger.error("credit checkout: pack %s is not configured in this environment", pack_id)
    raise TopupError(_CHECKOUT_UNAVAILABLE)


async def create_credit_checkout(
    *,
    pack_id: CreditPackId,
    tenant_id: UUID,
    user_id: UUID,
    user_email: str,
    user_name: str | None,
    region: str,
) -> CreditCheckoutResponse:
    """Start a credit purchase and return the hosted checkout URL to open.

    Order matters. The Dodo session is created BEFORE our row, because a
    ``credit_topups`` row holding a placeholder session id is a row the webhook
    cannot find. If the insert then fails, the buyer holds a checkout URL we
    cannot resolve — and the webhook refuses to credit it, correctly, leaving it
    in an operator's lap with the session id in the log. A compensating cancel
    for a control-plane failure in the millisecond after a successful HTTPS round
    trip is not worth building.

    Both halves stay in this one process for exactly that ordering, which is why
    a region forwards the whole operation here rather than calling Dodo itself.

    The return URL goes to the DASHBOARD, not to either API, and carries the
    region. The dashboard normally picks its region from `localStorage`, which
    survives the round trip to Dodo — but a buyer who finishes checkout in a
    different browser, or after clearing site data, would otherwise land on the
    default region, poll *its* ledger, find nothing, and be told the payment was
    not seen while the money sits correctly where they bought it.
    """
    pack = _pack(pack_id)
    # Generated here so the Dodo metadata and our row agree on it.
    topup_id = uuid4()
    dashboard = get_settings().app.dashboard_public_url

    try:
        session = await dodo.create_checkout_session(
            dodo.CheckoutSessionRequest(
                product_cart=[dodo.ProductItem(product_id=pack.dodo_product_id)],
                customer=dodo.NewCustomer(email=user_email, name=user_name),
                return_url=(f"{dashboard}/settings?tab=billing&topup={topup_id}&region={region}"),
                metadata={
                    "tenant_id": str(tenant_id),
                    "topup_id": str(topup_id),
                    # Readable in the Dodo dashboard, NOT routing — `region` on
                    # our row is what decides where the credit lands.
                    "region": region,
                },
            )
        )
    except dodo.DodoError as exc:
        logger.exception("credit checkout could not be created for tenant %s: %s", tenant_id, exc)
        raise TopupError(_CHECKOUT_UNAVAILABLE) from exc

    pool = await db.control_pool()
    try:
        await pool.execute(
            """
            INSERT INTO credit_topups (
                id, tenant_id, region, created_by, amount,
                dodo_product_id, dodo_checkout_session_id
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            """,
            topup_id,
            tenant_id,
            region,
            user_id,
            pack.amount,
            pack.dodo_product_id,
            session.session_id,
        )
    except Exception:
        logger.exception(
            "checkout session %s was created at Dodo but its credit_topups row was not — "
            "a payment against it will be refused and needs an operator",
            session.session_id,
        )
        raise

    return CreditCheckoutResponse(checkout_url=session.checkout_url, topup_id=topup_id)


# ── the webhook ──────────────────────────────────────────────────────────────


async def receive_dodo_webhook(headers: dict[str, str], raw_body: bytes) -> None:
    """Verify a Dodo callback and act on it.

    Unauthenticated by URL and authenticated by signature, so verification is the
    first thing that happens and `raw_body` must be the exact bytes off the wire.
    A missing header, a stale timestamp or a signature matching none of the
    offered candidates raises :class:`dodo.WebhookSignatureError`, which the route
    turns into a 401 — not a 400, and never a silent ignore.

    ONE endpoint serves the whole deployment, and there cannot be more than one:
    Dodo has one business and a payment carries no notion of our regions, so
    per-region endpoints are not merely unnecessary — they are not expressible.

    Everything past that point is done inline. It is a handful of statements, not
    a job, and Dodo retries a non-2xx eight times over about ten hours — so a
    failure here is a redelivery rather than a lost payment.
    """
    secret = get_settings().billing.dodo.webhook_secret
    if not secret:
        raise dodo.WebhookSignatureError(
            "no Dodo webhook signing secret is configured, so no callback can be trusted"
        )
    dodo.verify_webhook_signature(secret=secret, headers=headers, raw_body=raw_body)
    envelope = dodo.DodoWebhookEnvelope.model_validate_json(raw_body)
    await _handle_event(envelope)


async def _handle_event(envelope: dodo.DodoWebhookEnvelope) -> None:
    """Act on one verified event.

    The signature was checked, so an event type this file has never heard of is
    not an attack: it is logged and ignored. A subscription that picks up a new
    Dodo event type must not start returning errors into that retry queue.

    Of the five events subscribed, only three reach a region at all: a purchase
    to apply, and a refund or a lost dispute to reverse. `failed` and `cancelled`
    are a control-plane UPDATE and nothing more.
    """
    match envelope.type:
        case "payment.succeeded":
            await _on_payment_succeeded(dodo.DodoPayment.model_validate(envelope.data))
        case "payment.failed":
            await _on_payment_failed(dodo.DodoPayment.model_validate(envelope.data))
        case "payment.cancelled":
            await _on_payment_cancelled(dodo.DodoPayment.model_validate(envelope.data))
        case "refund.succeeded":
            await _on_refund_succeeded(dodo.DodoRefund.model_validate(envelope.data))
        case "dispute.lost":
            await _on_dispute_lost(dodo.DodoDispute.model_validate(envelope.data))
        case _:
            logger.info("dodo webhook: ignoring event type %r", envelope.type)


async def _on_payment_succeeded(payment: dodo.DodoPayment) -> None:
    """Credit the top-up this payment paid for, once, in full or not at all."""
    if payment.status != _SUCCEEDED:
        logger.error(
            "dodo webhook: payment.succeeded for %s carries status %r; crediting nothing",
            payment.payment_id,
            payment.status,
        )
        return

    if not payment.checkout_session_id:
        # A payment link someone found, a storefront purchase, or a payment for
        # another brand on this business. This is the point of writing our row
        # first: none of them can manufacture credits.
        logger.warning(
            "dodo webhook: payment %s (brand %s) has no checkout session; crediting nobody",
            payment.payment_id,
            payment.brand_id,
        )
        return

    pool = await db.control_pool()
    row = await pool.fetchrow(
        """
        SELECT id, tenant_id, region, amount, status, dodo_product_id
        FROM credit_topups
        WHERE dodo_checkout_session_id = $1
        """,
        payment.checkout_session_id,
    )
    if row is None:
        logger.warning(
            "dodo webhook: payment %s names checkout session %s, which is not ours; "
            "crediting nobody",
            payment.payment_id,
            payment.checkout_session_id,
        )
        return
    if row["status"] == "paid":
        # A redelivery. The ledger's unique index would refuse the second credit
        # anyway; this just saves the work.
        return

    refusal = _amount_mismatch(payment, expected=row["amount"], product_id=row["dodo_product_id"])
    if refusal is not None:
        logger.error(
            "dodo webhook: refusing to credit top-up %s from payment %s — %s",
            row["id"],
            payment.payment_id,
            refusal,
        )
        await pool.execute(
            "UPDATE credit_topups SET status = 'failed', dodo_payment_id = $2, updated_at = now() "
            "WHERE id = $1",
            row["id"],
            payment.payment_id,
        )
        return

    # Mark paid BEFORE pushing, and note this is the opposite order from the
    # single-plane version — for the same reason. The push can fail; the payment
    # is a fact either way, and `status = 'paid' AND applied_at IS NULL` is
    # precisely what the reconcile and the operator's list look for. Marking
    # after a failed push would leave a paid payment recorded as `pending`, which
    # nothing looks for at all.
    await pool.execute(
        """
        UPDATE credit_topups
        SET status = 'paid',
            dodo_payment_id = $2,
            paid_amount_minor = $3,
            paid_currency = $4,
            updated_at = now()
        WHERE id = $1
        """,
        row["id"],
        payment.payment_id,
        payment.total_amount,
        payment.currency,
    )
    await push_grant_or_topup(
        issuance_id=row["id"],
        kind="purchase",
        tenant_id=row["tenant_id"],
        region=row["region"],
        amount=row["amount"],
        topup_id=row["id"],
    )
    logger.info(
        "credited %s to tenant %s in %s for top-up %s (paid %s %s, settled %s %s)",
        row["amount"],
        row["tenant_id"],
        row["region"],
        row["id"],
        payment.total_amount,
        payment.currency,
        payment.settlement_amount,
        payment.settlement_currency,
    )


def _amount_mismatch(
    payment: dodo.DodoPayment, *, expected: Decimal, product_id: str
) -> str | None:
    """Why this payment must not be credited, or ``None`` when it may be.

    A refusal gate, never an adjustment: it credits the full pack amount or it
    credits nothing. A mismatch means the product's price moved in the Dodo
    dashboard without config following, and crediting a guess is worse than
    refusing — the operator puts the price back and the customer is refunded or
    re-charged, both of which are recoverable. A wrong credit is not.
    """
    cart = payment.product_cart or []
    if not any(item.product_id == product_id for item in cart):
        return f"its cart {[i.product_id for i in cart]} does not contain {product_id}"
    if payment.currency != "USD":
        # Adaptive currency: the buyer paid in their own currency at Dodo's rate,
        # so there is no honest numeric comparison to make here and the product
        # id above is the whole check. What they paid never becomes the credit in
        # either branch, so this costs nothing.
        return None
    net_minor = payment.total_amount - (payment.tax or 0)
    expected_minor = int((expected * 100).to_integral_value())
    if net_minor != expected_minor:
        return f"it paid {net_minor} USD cents ex-tax where the pack grants {expected_minor}"
    return None


async def _on_payment_failed(payment: dodo.DodoPayment) -> None:
    """Mark the attempt failed. The row stays — a failed attempt is a support answer."""
    await _close_unpaid(payment, status="failed")


async def _on_payment_cancelled(payment: dodo.DodoPayment) -> None:
    """Mark the attempt cancelled: the buyer backed out before completing it.

    Its own status rather than `failed`, because the two send a support reader
    somewhere different — `failed` means a payment was refused and is worth
    chasing, `cancelled` means nobody tried to charge anything.

    Cannot move money either way. Credit is only ever granted against
    `payment.succeeded`, so this is a label on a row, not a decision about a
    balance, and it reaches no region.
    """
    await _close_unpaid(payment, status="cancelled")


async def _close_unpaid(payment: dodo.DodoPayment, *, status: str) -> None:
    """Land a terminal not-paid outcome on a top-up that is still `pending`.

    ``AND status = 'pending'`` is doing real work, not defensive padding: Dodo
    states events may arrive out of order, so a late `payment.cancelled` can land
    behind the `payment.succeeded` for the same payment. Without the guard that
    would relabel a paid, credited top-up as cancelled — the ledger would still
    be right, and the row beside it would be a lie.
    """
    if not payment.checkout_session_id:
        return
    pool = await db.control_pool()
    await pool.execute(
        """
        UPDATE credit_topups
        SET status = $3, dodo_payment_id = $2, updated_at = now()
        WHERE dodo_checkout_session_id = $1 AND status = 'pending'
        """,
        payment.checkout_session_id,
        payment.payment_id,
        status,
    )


async def _on_refund_succeeded(refund: dodo.DodoRefund) -> None:
    """Reverse a refunded top-up.

    Partial refunds are out of scope: we only ever refund a whole pack, and there
    is no defensible way to reverse part of a credit that has already been spent.
    One arriving is a loud error and no movement.
    """
    if refund.is_partial:
        logger.error(
            "dodo webhook: refund %s on payment %s is PARTIAL; no credit was reversed",
            refund.refund_id,
            refund.payment_id,
        )
        return
    await _reverse(
        payment_id=refund.payment_id,
        note=f"refunded by Dodo refund {refund.refund_id}",
        subject=f"refund {refund.refund_id}",
    )


async def _on_dispute_lost(dispute: dodo.DodoDispute) -> None:
    """Reverse a charged-back top-up. Money we will not receive must not stay spendable."""
    await _reverse(
        payment_id=dispute.payment_id,
        note=f"chargeback lost — Dodo dispute {dispute.dispute_id}",
        subject=f"dispute {dispute.dispute_id}",
    )


async def _reverse(*, payment_id: str, note: str, subject: str) -> None:
    """Take a top-up's credit back, in the region it was granted in.

    A reversal is a region push exactly as a purchase is — the half most likely
    to be left untested — and it needs its own stamp, because both directions can
    fail independently.

    There is no `load_tenant` here and nothing that touches a data plane: the
    row's `region` says where to push, which is the whole of what the tenant
    lookup used to be for.

    **A purchase the region never applied has nothing to take back**, and pushing
    the reversal anyway would be worse than doing nothing: `pending_for` only
    re-offers a purchase while `status = 'paid'`, so flipping the status to
    `refunded` retires it — the tenant would be left at minus one pack for credit
    they were never given, with no row left that could restore it. So the status
    flip and the "was it applied?" read are ONE statement, and when it was not,
    both stamps land and nothing is pushed.

    That single statement is also what makes the common case race-free against a
    billing page reconciling the same top-up. What it cannot cover is a reconcile
    that has already written the region's ledger row and not yet confirmed: this
    then retires the purchase without reversing it, and the customer keeps a pack
    we refunded. Sub-second, in the customer's favour, and the warning below is
    the trail a human follows — a lease or an outbox to close it costs more than
    the pack does.
    """
    pool = await db.control_pool()
    row = await pool.fetchrow(
        """
        UPDATE credit_topups
        SET status = 'refunded',
            updated_at = now(),
            -- Stamped HERE only when there is nothing to reverse. Otherwise it
            -- is the region's ack that sets it, through `mark_applied`.
            reversed_at = CASE WHEN applied_at IS NULL THEN now() ELSE reversed_at END
        WHERE dodo_payment_id = $1 AND status <> 'refunded'
        RETURNING id, tenant_id, region, amount, applied_at
        """,
        payment_id,
    )
    if row is None:
        # Either not ours, or already refunded — and a redelivered refund is a
        # no-op rather than a second reversal, which is why `status <> 'refunded'`
        # is in the statement and not in a branch above it.
        logger.warning(
            "dodo webhook: %s names payment %s, which is not ours or is already reversed",
            subject,
            payment_id,
        )
        return
    if row["applied_at"] is None:
        logger.warning(
            "%s reverses top-up %s, whose credit never reached %s — nothing to take back, "
            "and the purchase is retired unapplied",
            subject,
            row["id"],
            row["region"],
        )
        return
    await push_grant_or_topup(
        issuance_id=row["id"],
        kind="refund",
        tenant_id=row["tenant_id"],
        region=row["region"],
        amount=row["amount"],
        topup_id=row["id"],
        note=note,
    )
    logger.info(
        "reversed %s of credit on tenant %s in %s (%s)",
        row["amount"],
        row["tenant_id"],
        row["region"],
        subject,
    )
