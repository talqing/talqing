"""Delivery receipts and replies, from the sender's webhook onto batch rows.

Receipts arrive out of order and more than once, so a row only moves FORWARD:
`queued < sent < delivered < read`, with `undelivered` and `failed` reachable
only before delivery. A receipt for a message no batch sent — an agent's reply,
or something the user sent from elsewhere — matches nothing and is ignored.

The one way back is a template Meta held back under the recipient's per-person
marketing limit: it stays `undelivered` with a `next_attempt_at`
`delivery_retry_after_hours` later, which puts it back in the send loop's line,
until the batch's `delivery_attempts` are spent.
"""

from __future__ import annotations

import logging

import db
from services.integrations.models import Integration
from services.integrations.providers.whatsapp import expected_refusals, failure_kind
from services.integrations.providers.whatsapp.bsp import StatusEvent
from services.telephony.e164 import region_of_did
from services.user import Tenant

from .models import LIVE_STATUSES
from .send import DELIVERY_BREAKER_THRESHOLD
from .service import REMEDY, rate_limit_reason, stop_batch

logger = logging.getLogger("talqing.whatsapp.status")

# The statuses a row may be in for each receipt to move it.
_MOVES_FROM: dict[str, tuple[str, ...]] = {
    "sent": ("queued",),
    "delivered": ("queued", "sent"),
    "read": ("queued", "sent", "delivered"),
    "undelivered": ("queued", "sent"),
    "failed": ("queued", "sent"),
}


async def record_status(tenant: Tenant, integration: Integration, event: StatusEvent) -> None:
    allowed = _MOVES_FROM.get(event.status)
    if allowed is None:
        return
    pool = await db.tenant_pool(tenant)
    row = await pool.fetchrow(
        """
        UPDATE whatsapp_batch_recipients r
        SET status = $4,
            error_code = COALESCE($5, r.error_code),
            last_error = COALESCE($6, r.last_error),
            delivered_at = CASE WHEN $4 IN ('delivered', 'read')
                                THEN COALESCE(r.delivered_at, now()) ELSE r.delivered_at END,
            read_at = CASE WHEN $4 = 'read' THEN now() ELSE r.read_at END,
            updated_at = now()
        FROM whatsapp_batches b
        WHERE r.tenant_id = $1 AND r.provider_message_id = $2
          AND b.id = r.batch_id AND b.tenant_id = r.tenant_id AND b.integration_id = $3
          AND r.status = ANY($7::text[])
        RETURNING r.id, r.batch_id, r.to_e164, r.delivery_attempts AS attempts,
                  b.status AS batch_status, b.delivery_attempts AS max_attempts,
                  b.delivery_retry_after_hours AS retry_after_hours
        """,
        tenant.id,
        event.message_id,
        integration.id,
        event.status,
        event.error_code,
        event.error,
        list(allowed),
    )
    if row is None or event.status not in ("failed", "undelivered"):
        return
    # A failure that is about the sender rather than this recipient stops the
    # batch: every remaining row would fail the same way.
    kind = failure_kind(integration, event.error_code)
    if (
        kind == "held_back"
        and row["attempts"] < row["max_attempts"]
        and row["batch_status"] in LIVE_STATUSES
        # Meta never delivers a marketing template to a US number.
        and region_of_did(row["to_e164"]) != "US"
    ):
        await pool.execute(
            """
            UPDATE whatsapp_batch_recipients
            -- `undelivered` from Gupshup's `failed` too: it was held back, not refused.
            SET status = 'undelivered', send_attempts = 0,
                next_attempt_at = now() + make_interval(hours => $4::int), updated_at = now()
            WHERE id = $1 AND tenant_id = $2 AND status = $3
            """,
            row["id"],
            tenant.id,
            event.status,
            row["retry_after_hours"],
        )
        return
    if kind in REMEDY:
        reason = f"WhatsApp refused a message (error {event.error_code})"
        if event.error:
            reason += f": {event.error}"
        await stop_batch(
            tenant, row["batch_id"], status="paused", reason=f"{reason} — {REMEDY[kind]}"
        )
    elif kind == "rate_limited":
        await stop_batch(
            tenant, row["batch_id"], status="paused", reason=rate_limit_reason(event.error_code)
        )
    elif kind in ("row", "held_back") and event.error_code not in expected_refusals(integration):
        # The delivery breaker, over what was sent since the last resume. Not on
        # WhatsApp and held back by Meta are expected of any list, so neither
        # trips it nor counts toward it.
        recent = await pool.fetch(
            """
            SELECT r.status FROM whatsapp_batch_recipients r
            JOIN whatsapp_batches b ON b.id = r.batch_id AND b.tenant_id = r.tenant_id
            WHERE r.tenant_id = $1 AND r.batch_id = $2
              AND r.status IN ('delivered', 'read', 'undelivered', 'failed')
              AND r.sent_at IS NOT NULL
              AND (b.resumed_at IS NULL OR r.sent_at > b.resumed_at)
              AND (r.error_code IS NULL OR r.error_code <> ALL($4::text[]))
            ORDER BY r.sent_at DESC
            LIMIT $3
            """,
            tenant.id,
            row["batch_id"],
            DELIVERY_BREAKER_THRESHOLD,
            list(expected_refusals(integration)),
        )
        if len(recent) == DELIVERY_BREAKER_THRESHOLD and not any(
            r["status"] in ("delivered", "read") for r in recent
        ):
            last = event.error or f"error {event.error_code}"
            await stop_batch(
                tenant,
                row["batch_id"],
                status="paused",
                reason=(
                    f"The last {DELIVERY_BREAKER_THRESHOLD} messages were all refused — "
                    f"last: {last}. Check the template and the list, then resume."
                ),
            )


async def record_reply(tenant: Tenant, integration: Integration, peer: str) -> None:
    """Mark the latest template this sender sent ``peer`` as replied to."""
    pool = await db.tenant_pool(tenant)
    await pool.execute(
        """
        UPDATE whatsapp_batch_recipients
        SET replied_at = now(), updated_at = now()
        WHERE id = (
            SELECT r.id FROM whatsapp_batch_recipients r
            JOIN whatsapp_batches b ON b.id = r.batch_id AND b.tenant_id = r.tenant_id
            WHERE r.tenant_id = $1 AND r.to_e164 = $2 AND b.integration_id = $3
              AND r.sent_at IS NOT NULL AND r.replied_at IS NULL
            ORDER BY r.sent_at DESC
            LIMIT 1
        )
        """,
        tenant.id,
        peer,
        integration.id,
    )
