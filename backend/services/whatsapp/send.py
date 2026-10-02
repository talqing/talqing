"""Sending: one long-lived coroutine per batch, holding it to the last row.

The shape of `email.send`, simplified by what WhatsApp templates are: one send
per batch, so the batch row IS the send's policy, and one job per batch
(`enqueue_once`, `subject_id = batch_id`).

**Every gate is re-read on every turn** — the batch's status, its window, its
daily cap and its gap — so a pause takes hold before the next message and an
edited cap takes hold within one turn. Waiting is a chunked sleep, never one long
one, and every wait is announced on the batch as `next_send_at` + reason.

**A claim commits before the BSP is called.** Neither BSP has an idempotency
key, so a row is `sending` for exactly as long as its outcome is unknown.

**A deploy does not interrupt a send.** The executor cancels every job on
shutdown; the one message in flight is shielded and given time to finish and
settle, so its outcome is always recorded. Only a hard kill strands a row
`sending`, and that row is never resent blind: on Twilio it is looked up and
adopted, on Gupshup (no lookup API) it is failed with a sentence saying to
check. A duplicate WhatsApp message to a prospect is worse than a missed one.

**It stops itself, resumably.** Anything a person can fix — a credential, a
paused template, an empty wallet, a messaging limit — pauses the batch with what
to do. Only a deleted sender or a creator who may no longer send fails it.

**A lapsed lease stops the loop.** If rows are claimable and the claim refused
them, the refusal was the ownership test: somebody else holds the job, so this
loop exits rather than spinning against it.

**`completed` is idle, not over.** An append wakes the batch back to `sending`,
so the finish and the exit both run under the batch row's lock, the one an
append holds while it adds rows.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

import asyncpg

import db
from services.conversations.keys import whatsapp_conversation_key
from services.conversations.refs import ConversationRefConflict, ensure_ref, open_conversation
from services.integrations.models import Integration
from services.integrations.providers.whatsapp import (
    WHATSAPP_MESSAGE_TRIGGER,
    WhatsAppBsp,
    bsp_client,
    webhook_url,
)
from services.integrations.providers.whatsapp.bsp import BSP_TIMEOUT_SECONDS, BspError
from services.integrations.providers.whatsapp.twilio import TwilioWhatsApp
from services.integrations.triggers import get_active_trigger, trigger_conversation_context
from services.jobs import JobContext
from services.messaging import inbound
from services.scheduling import daily_cap_today, local_day_bounds, local_today, window_state
from services.secrets import load_secrets
from services.user import ROLE_ADMIN, ROLE_EDITOR, NotAMember, Tenant, load_context
from services.webhooks import events
from settings import get_settings

from .models import (
    RECIPIENT_COLUMNS,
    WhatsAppBatch,
    WhatsAppRecipient,
    message_text,
    userdata_seed,
    variable_values,
)
from .service import (
    REMEDY,
    emit,
    load_batch,
    load_integration,
    rate_limit_reason,
    stop_batch,
    unfillable_reason,
)

logger = logging.getLogger("talqing.whatsapp.send")

# Ten transient failures in a row is not bad luck. Pauses, never fails: a BSP
# incident is far more often the cause than a broken batch.
SEND_BREAKER_THRESHOLD = 10

# The same idea for receipts: Twilio accepts almost every send and refuses it
# later. Not on WhatsApp and Meta's per-person limit are left out of the count
# (`expected_refusals`): a cold list lost 88% of its sends to them on 2026-10-01
# and tripped this every twenty messages while its template was delivering fine.
DELIVERY_BREAKER_THRESHOLD = 20

# A BSP limiting the sender this many sends in a row is its daily messaging
# limit, not a burst: pause rather than retry until midnight.
RATE_LIMIT_PAUSE_AFTER = 3

# How long a row may sit `sending` before it counts as stranded rather than in
# flight. Three times the BSP client's timeout.
_IN_FLIGHT_SECONDS = 3 * BSP_TIMEOUT_SECONDS

# How long a cancelled loop waits for the message in flight to finish and
# settle. Inside the executor's `SETTLE_GRACE_SECONDS` (15).
_SETTLE_SECONDS = BSP_TIMEOUT_SECONDS + 2

# Where to come back when a BSP rate-limits us and names no `retry-after`.
_RATE_LIMIT_FALLBACK_SECONDS = 30

# How long the batch stays open after its last send for a held-back receipt to
# put that row back in line. MEASURED on prod-in 2026-10-01: Meta's 63049
# receipts arrived 9-16 s after the send, every one of ~40. A receipt later than
# this leaves its row undelivered.
_RECEIPT_SETTLE_SECONDS = 300

# A gap shorter than this is not worth announcing.
_GAP_WORTH_ANNOUNCING_SECONDS = 60

_UNKNOWN_OUTCOME = (
    "outcome unknown after a restart; check the recipient's WhatsApp before resending"
)

# `_nothing_to_send` telling the loop this job is not ours any more.
_STOP: Literal["stop"] = "stop"


async def run_send_job(tenant: Tenant, args: dict, job: JobContext) -> datetime | None:
    """Send the batch to the end. Returns ``start_at`` if it is too early."""
    batch_id = UUID(str(args["batch_id"]))
    pool = await db.tenant_pool(tenant)
    batch = await load_batch(pool, tenant.id, batch_id)
    if batch is None or batch.status not in ("scheduled", "sending"):
        return None
    if batch.status == "scheduled" and batch.start_at and batch.start_at > datetime.now(UTC):
        await _announce(pool, batch, batch.start_at, "start")
        return batch.start_at

    # Authorized once, like the HTTP request it stands in for.
    allowed = await _creator_still_allowed(tenant, batch)
    if allowed is None:
        return datetime.now(UTC) + timedelta(seconds=60)
    if not allowed:
        return None

    sender = await _sender(pool, tenant, batch)
    if sender is None:
        return None
    integration, client = sender
    batch = await _step_start(pool, tenant, batch)
    await _recover_abandoned(pool, tenant, batch, integration, client)
    await _send_loop(pool, tenant, batch, integration, client, job)
    return None


async def _send_loop(
    pool: asyncpg.Pool,
    tenant: Tenant,
    batch: WhatsAppBatch,
    integration: Integration,
    client: WhatsAppBsp,
    job: JobContext,
) -> None:
    """Send rows until none are left or somebody stops the batch.

    One exit for a stopped batch — the status re-read at the top — so a pause, a
    cancel, a breaker and a finish all leave the same way, through `_hand_back`.
    """
    # How often a live loop re-resolves its credential and looks for stranded
    # rows, and the longest single sleep: the most a policy change waits.
    recheck = float(get_settings().email.recheck_seconds)
    checked_at = time.monotonic()
    send_allowed_at = 0.0
    limited_in_a_row = 0
    while True:
        fresh = await load_batch(pool, tenant.id, batch.id)
        if fresh is None:
            return
        if fresh.status != "sending":
            if await _hand_back(pool, job, fresh):
                return
            # Resumed while we were leaving; this job still holds it.
            continue
        batch = fresh

        if time.monotonic() - checked_at >= recheck:
            # A rotated key or a disconnected sender stops a multi-day send
            # within one re-check rather than at its next message.
            sender = await _sender(pool, tenant, batch)
            if sender is None:
                continue
            integration, client = sender
            await _recover_abandoned(pool, tenant, batch, integration, client)
            checked_at = time.monotonic()

        # In the batch's zone, never the server's.
        may_send, opens_at = window_state(
            now=datetime.now(UTC),
            timezone=batch.timezone,
            window_start_local=batch.window_start_local,
            window_end_local=batch.window_end_local,
            window_days=batch.window_days,
            subject=f"WhatsApp batch {batch.id}",
        )
        if not may_send:
            assert opens_at is not None
            await _announce(pool, batch, opens_at, "window")
            await asyncio.sleep(min(_seconds_until(opens_at), recheck))
            continue

        gap_left = send_allowed_at - time.monotonic()
        if gap_left > 0:
            if gap_left >= _GAP_WORTH_ANNOUNCING_SECONDS:
                await _announce(pool, batch, datetime.now(UTC) + timedelta(seconds=gap_left), "gap")
            await asyncio.sleep(min(gap_left, recheck))
            continue

        recipient = await _claim(pool, batch, job)
        if recipient is None:
            wait = await _nothing_to_send(pool, tenant, batch, integration, client, job)
            if wait == _STOP:
                return
            if wait is not None:
                await asyncio.sleep(min(wait, recheck))
            continue
        await _announce(pool, batch, None, None)

        # Shielded, because the claim is already durable and neither BSP has an
        # idempotency key: a shutdown that abandoned the call now would leave
        # the row `sending` with its outcome unknown.
        sending = asyncio.ensure_future(
            _send_one(pool, tenant, batch, integration, client, recipient)
        )
        try:
            limited = await asyncio.shield(sending)
        except asyncio.CancelledError:
            # `shield` protects the inner future, but this `await` still raises
            # and would leave it running detached. The bounded re-await is what
            # holds the job open until the row is settled.
            with contextlib.suppress(Exception):
                await asyncio.wait_for(sending, _SETTLE_SECONDS)
            raise

        if limited is None:
            limited_in_a_row = 0
            send_allowed_at = time.monotonic() + batch.send_gap_seconds
            continue
        # The limit is on the sender, so nothing else is sent until it lifts.
        limited_in_a_row += 1
        if limited_in_a_row >= RATE_LIMIT_PAUSE_AFTER:
            await stop_batch(
                tenant, batch.id, status="paused", reason=rate_limit_reason(limited.code)
            )
        send_allowed_at = time.monotonic() + max(
            batch.send_gap_seconds, limited.retry_after_seconds or _RATE_LIMIT_FALLBACK_SECONDS
        )


def _seconds_until(when: datetime) -> float:
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


async def _sender(
    pool: asyncpg.Pool, tenant: Tenant, batch: WhatsAppBatch
) -> tuple[Integration, WhatsAppBsp] | None:
    """The batch's sender and its BSP client, or ``None`` after stopping the batch."""
    integration = (
        await load_integration(pool, tenant.id, batch.integration_id)
        if batch.integration_id
        else None
    )
    if integration is None:
        await stop_batch(
            tenant, batch.id, status="failed", reason="the WhatsApp sender was disconnected"
        )
        return None
    if integration.status != "active":
        await stop_batch(
            tenant,
            batch.id,
            status="paused",
            reason=f"the WhatsApp sender is {integration.status} — reactivate it, then resume",
        )
        return None
    try:
        return integration, bsp_client(integration, await load_secrets(tenant))
    except BspError as exc:
        await stop_batch(tenant, batch.id, status="paused", reason=f"{exc}, then resume")
        return None


async def _creator_still_allowed(tenant: Tenant, batch: WhatsAppBatch) -> bool | None:
    """May this batch message people? ``None`` when the control plane did not answer."""
    try:
        ctx = await load_context(tenant, batch.created_by_user_id)
    except NotAMember:
        await stop_batch(
            tenant,
            batch.id,
            status="failed",
            reason="the person who created this batch is no longer in this organization",
        )
        return False
    except Exception:
        logger.exception("WhatsApp batch %s: could not load its creator; waiting", batch.id)
        return None
    if ctx.user.role not in (ROLE_ADMIN, ROLE_EDITOR):
        await stop_batch(
            tenant,
            batch.id,
            status="failed",
            reason=f"{ctx.user.email} no longer has permission to send",
        )
        return False
    return True


async def _step_start(pool: asyncpg.Pool, tenant: Tenant, batch: WhatsAppBatch) -> WhatsAppBatch:
    row = await pool.fetchrow(
        """
        UPDATE whatsapp_batches
        SET status = 'sending', started_at = COALESCE(started_at, now()),
            next_send_at = NULL, next_send_reason = NULL, updated_at = now()
        WHERE id = $1 AND tenant_id = $2 AND status = 'scheduled'
        RETURNING id
        """,
        batch.id,
        tenant.id,
    )
    if row is None:
        return batch
    fresh = await load_batch(pool, tenant.id, batch.id)
    assert fresh is not None
    logger.info("WhatsApp batch %s: started", batch.id)
    await emit(tenant, events.WHATSAPP_BATCH_STARTED, fresh)
    return fresh


async def _claim(
    pool: asyncpg.Pool, batch: WhatsAppBatch, job: JobContext
) -> WhatsAppRecipient | None:
    """Take the next row, in one statement that answers every gate.

    Is a row ready and due, is the batch still `sending`, do we still hold the
    job, is today's allowance left, and has the gap since the last message
    passed. No lock: a batch has one job and one runner, and the statement is
    atomic on its own.
    """
    day_start, next_midnight = local_day_bounds(datetime.now(UTC), batch.timezone)
    row = await pool.fetchrow(
        f"""
        UPDATE whatsapp_batch_recipients
        SET status = 'sending', send_attempts = send_attempts + 1,
            sending_started_at = now(), updated_at = now()
        WHERE id = (
            SELECT r.id FROM whatsapp_batch_recipients r
            WHERE r.tenant_id = $1 AND r.batch_id = $2
              -- Never sent, or held back by Meta with a resend booked: a held-back
              -- row stays `undelivered` until it goes again, because it is.
              AND (r.status = 'ready' OR (r.status = 'undelivered' AND r.next_attempt_at IS NOT NULL))
              AND (r.next_attempt_at IS NULL OR r.next_attempt_at <= now())
              AND EXISTS (
                SELECT 1 FROM whatsapp_batches b
                WHERE b.id = $2 AND b.tenant_id = $1 AND b.status = 'sending'
              )
              AND EXISTS (
                SELECT 1 FROM scheduled_jobs j
                WHERE j.id = $3 AND j.tenant_id = $1 AND j.claimed_by = $4
                  AND j.lease_expires_at > now()
              )
              AND (
                $5::int IS NULL
                OR $5::int > (
                    SELECT count(*) FROM whatsapp_batch_recipients c
                    WHERE c.tenant_id = $1 AND c.batch_id = $2
                      AND c.sent_at >= $6 AND c.sent_at < $7
                )
              )
              AND NOT EXISTS (
                SELECT 1 FROM whatsapp_batch_recipients g
                WHERE g.tenant_id = $1 AND g.batch_id = $2
                  AND g.sent_at > now() - make_interval(secs => $8::int)
              )
            ORDER BY r.next_attempt_at NULLS FIRST, r.row_number
            LIMIT 1
            FOR UPDATE SKIP LOCKED
        )
        RETURNING {RECIPIENT_COLUMNS}
        """,
        batch.tenant_id,
        batch.id,
        job.id,
        job.worker,
        _cap_today(batch),
        day_start,
        next_midnight,
        batch.send_gap_seconds,
    )
    return WhatsAppRecipient.model_validate(dict(row)) if row else None


async def _nothing_to_send(
    pool: asyncpg.Pool,
    tenant: Tenant,
    batch: WhatsAppBatch,
    integration: Integration,
    client: WhatsAppBsp,
    job: JobContext,
) -> float | Literal["stop"] | None:
    """The claim took nothing: seconds to wait, ``None`` to re-read, or stop."""
    left = await pool.fetchrow(
        """
        SELECT
            count(*) FILTER (WHERE status <> 'sending') AS waiting,
            count(*) FILTER (
                WHERE status <> 'sending' AND (next_attempt_at IS NULL OR next_attempt_at <= now())
            ) AS due,
            min(next_attempt_at) FILTER (WHERE status <> 'sending') AS next_due,
            count(*) FILTER (WHERE status = 'sending') AS in_flight
        FROM whatsapp_batch_recipients
        WHERE tenant_id = $1 AND batch_id = $2
          AND (status IN ('ready', 'sending')
               OR (status = 'undelivered' AND next_attempt_at IS NOT NULL))
        """,
        tenant.id,
        batch.id,
    )
    if not left["waiting"]:
        if left["in_flight"]:
            # Only a stranded row is left; settle it rather than wait a re-check.
            await _recover_abandoned(pool, tenant, batch, integration, client)
            return 5.0
        if settling := await _receipts_settling(pool, batch):
            return settling
        await _finish(pool, tenant, batch)
        return None
    if await _cap_spent(pool, batch):
        _, next_midnight = local_day_bounds(datetime.now(UTC), batch.timezone)
        await _announce(pool, batch, next_midnight, "daily_cap")
        return _seconds_until(next_midnight)
    if not left["due"] and left["next_due"] is not None:
        await _announce(pool, batch, left["next_due"], "retry")
        return _seconds_until(left["next_due"])
    if not await _still_ours(pool, tenant.id, job):
        logger.warning("WhatsApp batch %s: job %s is no longer ours; stopping", batch.id, job.id)
        return _STOP
    # The gap since the last message, or a row locked for an instant by an edit.
    return 1.0


async def _receipts_settling(pool: asyncpg.Pool, batch: WhatsAppBatch) -> float | None:
    """Seconds until the last send that Meta could still hold back has its receipt."""
    last = await pool.fetchval(
        """
        SELECT max(sent_at) FROM whatsapp_batch_recipients
        WHERE tenant_id = $1 AND batch_id = $2 AND status IN ('queued', 'sent')
          AND delivery_attempts < $3
        """,
        batch.tenant_id,
        batch.id,
        batch.delivery_attempts,
    )
    if last is None:
        return None
    left = _RECEIPT_SETTLE_SECONDS - (datetime.now(UTC) - last).total_seconds()
    return left if left > 0 else None


def _cap_today(batch: WhatsAppBatch) -> int | None:
    """Today's limit: the fixed one, or where the ramp has got to."""
    return daily_cap_today(
        batch.daily_cap,
        base_days=batch.send_ramp_base_days,
        days=batch.send_days,
        last_day=batch.last_send_day,
        today=local_today(datetime.now(UTC), batch.timezone),
    )


async def _cap_spent(pool: asyncpg.Pool, batch: WhatsAppBatch) -> bool:
    cap = _cap_today(batch)
    if cap is None:
        return False
    day_start, next_midnight = local_day_bounds(datetime.now(UTC), batch.timezone)
    sent_today = await pool.fetchval(
        """
        SELECT count(*) FROM whatsapp_batch_recipients
        WHERE tenant_id = $1 AND batch_id = $2 AND sent_at >= $3 AND sent_at < $4
        """,
        batch.tenant_id,
        batch.id,
        day_start,
        next_midnight,
    )
    return int(sent_today) >= cap


async def _still_ours(pool: asyncpg.Pool, tenant_id: UUID, job: JobContext) -> bool:
    return bool(
        await pool.fetchval(
            """
            SELECT EXISTS (
                SELECT 1 FROM scheduled_jobs
                WHERE id = $1 AND tenant_id = $2 AND claimed_by = $3 AND lease_expires_at > now()
            )
            """,
            job.id,
            tenant_id,
            job.worker,
        )
    )


async def _send_one(
    pool: asyncpg.Pool,
    tenant: Tenant,
    batch: WhatsAppBatch,
    integration: Integration,
    client: WhatsAppBsp,
    recipient: WhatsAppRecipient,
) -> BspError | None:
    """Send one claimed row and settle it, whatever happens.

    Returns the rate limit the BSP answered with, if it did: the loop holds off.
    """
    assert recipient.to_e164 is not None  # `ready` rows have a number
    if reason := unfillable_reason(batch, recipient):
        # Every writer keeps a `ready` row fillable, so this is an edit that
        # raced the claim. It never left.
        await _settle(pool, recipient, "skipped", reason, skip_reason="unfillable", refund=True)
        return None
    try:
        message_id = await client.send_template(
            to_e164=recipient.to_e164,
            template=batch.template,
            values=variable_values(batch, recipient),
            status_callback=webhook_url(tenant.id, integration.id),
        )
    except BspError as exc:
        await _settle_failure(pool, tenant, batch, recipient, exc)
        return exc if exc.kind == "rate_limited" else None
    await _record_sent(pool, tenant, batch, integration, client, recipient, message_id)
    await pool.execute(
        """
        UPDATE whatsapp_batches SET consecutive_send_failures = 0, updated_at = now()
        WHERE id = $1 AND tenant_id = $2 AND consecutive_send_failures <> 0
        """,
        batch.id,
        tenant.id,
    )
    return None


async def _settle_failure(
    pool: asyncpg.Pool,
    tenant: Tenant,
    batch: WhatsAppBatch,
    recipient: WhatsAppRecipient,
    exc: BspError,
) -> None:
    error = str(exc)
    if exc.kind in ("row", "held_back"):
        # This number, not the batch: the breaker is not touched.
        await _settle(pool, recipient, "failed", error, code=exc.code)
        return
    if exc.kind == "rate_limited":
        # Costs the row nothing — not an attempt, not the breaker.
        wait = int(exc.retry_after_seconds or _RATE_LIMIT_FALLBACK_SECONDS)
        await _settle(pool, recipient, "ready", error, retry_in=wait, refund=True)
        return
    if exc.kind in ("account", "template", "wallet"):
        # Every remaining row would fail the same way, so the batch stops on the
        # first one. This row never left and goes back untouched. Paused, not
        # failed: once the cause is fixed, resume carries on from this row.
        await _settle(pool, recipient, "ready", None, refund=True)
        await stop_batch(tenant, batch.id, status="paused", reason=f"{error} — {REMEDY[exc.kind]}")
        return
    retryable = recipient.send_attempts < batch.send_attempts
    await _settle(
        pool,
        recipient,
        "ready" if retryable else "failed",
        error,
        code=exc.code,
        retry_in=batch.send_retry_after_minutes * 60 if retryable else None,
    )
    tripped = await pool.fetchval(
        """
        UPDATE whatsapp_batches
        SET consecutive_send_failures = consecutive_send_failures + 1, updated_at = now()
        WHERE id = $1 AND tenant_id = $2
        RETURNING consecutive_send_failures >= $3
        """,
        batch.id,
        tenant.id,
        SEND_BREAKER_THRESHOLD,
    )
    if tripped:
        await stop_batch(
            tenant,
            batch.id,
            status="paused",
            reason=f"{SEND_BREAKER_THRESHOLD} sends failed in a row — last: {error}",
        )


async def _settle(
    pool: asyncpg.Pool,
    recipient: WhatsAppRecipient,
    status: str,
    error: str | None,
    *,
    code: str | None = None,
    skip_reason: str | None = None,
    retry_in: int | None = None,
    refund: bool = False,
) -> None:
    await pool.execute(
        """
        UPDATE whatsapp_batch_recipients
        SET status = $3, last_error = $4, error_code = $5, skip_reason = $6,
            next_attempt_at = CASE WHEN $7::int IS NULL THEN NULL
                                   ELSE now() + make_interval(secs => $7::int) END,
            send_attempts = CASE WHEN $8 THEN GREATEST(send_attempts - 1, 0) ELSE send_attempts END,
            sending_started_at = NULL, updated_at = now()
        WHERE id = $1 AND tenant_id = $2 AND status = 'sending'
        """,
        recipient.id,
        recipient.tenant_id,
        status,
        (error or "")[:1000] or None,
        code,
        skip_reason,
        retry_in,
        refund,
    )


async def _record_sent(
    pool: asyncpg.Pool,
    tenant: Tenant,
    batch: WhatsAppBatch,
    integration: Integration,
    client: WhatsAppBsp,
    recipient: WhatsAppRecipient,
    message_id: str,
) -> None:
    """Mark the row sent and put the template into the recipient's thread.

    One transaction. The whole template — header, body, footer, buttons —
    becomes the agent's own first message, and the row's cells become
    `{{userdata.<column>}}`, so when the recipient replies the agent knows what
    it "said" and who it is talking to.
    """
    assert recipient.to_e164 is not None
    trigger = await get_active_trigger(tenant, integration.id, WHATSAPP_MESSAGE_TRIGGER)
    agent_id = trigger.agent_id if trigger else None
    sender_e164 = str(integration.provider_account_info["sender_e164"])
    async with pool.acquire() as conn:
        async with conn.transaction():
            try:
                ref = await ensure_ref(
                    conn,
                    tenant_id=tenant.id,
                    conversation_key=whatsapp_conversation_key(
                        sender_e164=sender_e164, peer=recipient.to_e164
                    ),
                    kind="integration",
                    bind_id=integration.id,
                    customer_metadata={
                        "peer_address": client.peer_address(recipient.to_e164),
                        "sender_e164": sender_e164,
                        "phone": recipient.to_e164,
                    },
                    userdata_seed=userdata_seed(recipient),
                )
            except ConversationRefConflict as exc:
                raise RuntimeError(exc.message) from exc
            # The template goes where its reply will be answered, so the agent
            # sees what it is replying to. A contact with an open chat replies
            # into that chat. Otherwise the reply starts a chat, which joins this
            # conversation (`services.chats.open`): a `transcript` agent's
            # thread, else one of the template's own.
            conversation_id = await conn.fetchval(
                """
                SELECT conversation_id FROM sessions
                WHERE tenant_id = $1 AND conversation_ref_id = $2
                    AND channel = 'text' AND status IN ('queued', 'running')
                """,
                tenant.id,
                ref.id,
            ) or await open_conversation(
                conn,
                tenant_id=tenant.id,
                ref=ref,
                context=(
                    await trigger_conversation_context(conn, tenant, agent_id)
                    if agent_id
                    else "transcript"
                ),
                source="whatsapp",
                outbound=True,
            )
            agent_version = (
                await conn.fetchval(
                    "SELECT published_version FROM agents WHERE id = $1 AND tenant_id = $2",
                    agent_id,
                    tenant.id,
                )
                if agent_id
                else None
            )
            item = await conn.fetchrow(
                """
                INSERT INTO conversation_items (
                    tenant_id, conversation_id, direction, type, role, agent_id, agent_version,
                    text, provider_message_id, source, visibility, delivery_status, metadata
                )
                VALUES ($1, $2, 'outbound', 'message', 'assistant', $3, $4,
                        $5, $6, 'whatsapp/template', 'customer_visible', 'sent', $7::jsonb)
                ON CONFLICT DO NOTHING
                RETURNING *
                """,
                tenant.id,
                conversation_id,
                agent_id,
                agent_version,
                message_text(batch, recipient),
                message_id,
                json.dumps(
                    {
                        "whatsapp_batch_id": str(batch.id),
                        "whatsapp_recipient_id": str(recipient.id),
                        "template_id": batch.template_id,
                    }
                ),
            )
            if item is not None:
                await conn.execute(
                    """
                    UPDATE conversations SET last_activity_at = GREATEST(last_activity_at, $3)
                    WHERE id = $1 AND tenant_id = $2
                    """,
                    conversation_id,
                    tenant.id,
                    item["created_at"],
                )
            # The batch's sending-day counter moves in the statement that
            # records the send, so it cannot disagree with `sent_at`.
            await conn.execute(
                """
                WITH s AS (
                    UPDATE whatsapp_batch_recipients
                    SET status = 'queued', provider_message_id = $3, sent_at = now(),
                        delivery_attempts = delivery_attempts + 1,
                        conversation_id = $4, last_error = NULL, error_code = NULL,
                        next_attempt_at = NULL, sending_started_at = NULL, updated_at = now()
                    WHERE id = $1 AND tenant_id = $2 AND status = 'sending'
                    RETURNING 1
                )
                UPDATE whatsapp_batches SET send_days = send_days + 1, last_send_day = $6
                WHERE id = $5 AND tenant_id = $2 AND EXISTS (SELECT 1 FROM s)
                  AND (last_send_day IS NULL OR last_send_day < $6)
                """,
                recipient.id,
                tenant.id,
                message_id,
                conversation_id,
                batch.id,
                local_today(datetime.now(UTC), batch.timezone),
            )
    if item is not None:
        await inbound.publish_inbound_item_created(
            tenant_id=tenant.id, conversation_id=conversation_id, item=item
        )


async def _recover_abandoned(
    pool: asyncpg.Pool,
    tenant: Tenant,
    batch: WhatsAppBatch,
    integration: Integration,
    client: WhatsAppBsp,
) -> None:
    """Settle rows a killed worker left `sending`. Never resent blind."""
    rows = await pool.fetch(
        f"""
        SELECT {RECIPIENT_COLUMNS} FROM whatsapp_batch_recipients
        WHERE tenant_id = $1 AND batch_id = $2 AND status = 'sending'
          AND sending_started_at < now() - make_interval(secs => $3::int)
        """,
        tenant.id,
        batch.id,
        _IN_FLIGHT_SECONDS,
    )
    for row in rows:
        recipient = WhatsAppRecipient.model_validate(dict(row))
        assert recipient.sending_started_at is not None and recipient.to_e164 is not None
        if not isinstance(client, TwilioWhatsApp):
            # Gupshup has no lookup API.
            await _settle(pool, recipient, "failed", _UNKNOWN_OUTCOME)
            continue
        try:
            sid = await client.find_sent_template(
                to_e164=recipient.to_e164, since=recipient.sending_started_at
            )
        except BspError:
            logger.warning(
                "WhatsApp batch %s: could not look up row %s; retrying later",
                batch.id,
                recipient.id,
                exc_info=True,
            )
            continue
        if sid is not None:
            logger.warning("WhatsApp batch %s: adopted %s for row %s", batch.id, sid, recipient.id)
            await _record_sent(pool, tenant, batch, integration, client, recipient, sid)
        elif recipient.send_attempts < batch.send_attempts:
            await _settle(pool, recipient, "ready", "the worker sending this row stopped")
        else:
            await _settle(pool, recipient, "failed", "the worker sending this row stopped")


async def _finish(pool: asyncpg.Pool, tenant: Tenant, batch: WhatsAppBatch) -> None:
    """Nothing is left to send: mark the batch `completed`.

    "Nothing left" is asked again under the batch row's lock, which an append
    holds while it adds rows: the caller's answer is a statement old.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT 1 FROM whatsapp_batches WHERE id = $1 AND tenant_id = $2 FOR UPDATE",
                batch.id,
                tenant.id,
            )
            row = await conn.fetchrow(
                """
                UPDATE whatsapp_batches
                SET status = 'completed', finished_at = now(),
                    next_send_at = NULL, next_send_reason = NULL, updated_at = now()
                WHERE id = $1 AND tenant_id = $2 AND status = 'sending'
                  AND NOT EXISTS (
                    SELECT 1 FROM whatsapp_batch_recipients
                    WHERE tenant_id = $2 AND batch_id = $1
                      AND (status IN ('ready', 'sending')
                           OR (status = 'undelivered' AND next_attempt_at IS NOT NULL))
                  )
                RETURNING id
                """,
                batch.id,
                tenant.id,
            )
    if row is None:
        return
    final = await load_batch(pool, tenant.id, batch.id)
    assert final is not None
    logger.info("WhatsApp batch %s: completed", batch.id)
    await emit(tenant, events.WHATSAPP_BATCH_COMPLETED, final)


async def _announce(
    pool: asyncpg.Pool, batch: WhatsAppBatch, at: datetime | None, reason: str | None
) -> None:
    """Publish when the batch next acts — written only when it changes."""
    if batch.next_send_at == at and batch.next_send_reason == reason:
        return
    await pool.execute(
        """
        UPDATE whatsapp_batches SET next_send_at = $3, next_send_reason = $4, updated_at = now()
        WHERE id = $1 AND tenant_id = $2
        """,
        batch.id,
        batch.tenant_id,
        at,
        reason,
    )


async def _hand_back(pool: asyncpg.Pool, job: JobContext, batch: WhatsAppBatch) -> bool:
    """Give the job up unless the batch went live again while we were leaving.

    Completed here, as a compare-and-set against the batch's status, so a resume
    or an append that lands in between cannot leave a live batch with no job. The
    batch row is locked first: the compare-and-set alone reads a snapshot, which
    a commit under it would not change.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT 1 FROM whatsapp_batches WHERE id = $1 AND tenant_id = $2 FOR UPDATE",
                batch.id,
                batch.tenant_id,
            )
            released = await conn.fetchval(
                """
                UPDATE scheduled_jobs
                SET status = 'done', last_error = NULL,
                    claimed_by = NULL, lease_expires_at = NULL, updated_at = now()
                WHERE id = $1 AND tenant_id = $2 AND status = 'running' AND claimed_by = $3
                  AND EXISTS (
                    SELECT 1 FROM whatsapp_batches
                    WHERE id = $4 AND tenant_id = $2
                      AND status IN ('paused', 'canceled', 'completed', 'failed')
                  )
                RETURNING id
                """,
                job.id,
                batch.tenant_id,
                job.worker,
                batch.id,
            )
    if released is not None:
        return True
    return not await _still_ours(pool, batch.tenant_id, job)
