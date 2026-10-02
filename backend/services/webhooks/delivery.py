"""Webhook delivery.

Best-effort delivery: load a tenant's matching active webhooks from its data DB,
sign the JSON body with HMAC-SHA256 (the per-webhook secret), POST it, and record
one `webhook_deliveries` row per attempt. No retry / dead-letter (that's a later
pass) — a failed POST is logged, never re-sent.

Webhooks are tenant-wide: every active subscription receives events for every
agent. Receivers filter on the payload's `agent_id` when needed.

Signature scheme (what a receiver verifies):
    X-Talqing-Signature: sha256=<hex(hmac_sha256(secret, raw_body))>
    X-Talqing-Event:     <event type>
    X-Talqing-Delivery:  <event id>
The signed bytes are the exact request body.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

import asyncpg
import httpx

import db
from services.tools import resolve_and_pin
from services.user import Tenant
from utils.crypto import decrypt

logger = logging.getLogger("talqing.webhooks")

DELIVERY_TIMEOUT = 5.0  # seconds; best-effort, so keep it short
BOUNDED_DISPATCH_TIMEOUT = DELIVERY_TIMEOUT + 2.0


def sign(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _envelope(
    event_type: str, tenant_id: str, agent_id: str | None, data: dict
) -> tuple[str, bytes]:
    event_id = uuid.uuid4().hex
    payload = {
        "id": event_id,
        "type": event_type,
        "created_at": datetime.now(UTC).isoformat(),
        "tenant_id": tenant_id,
        "agent_id": agent_id,
        "data": data,
    }
    return event_id, json.dumps(payload, default=str).encode()


async def _deliver(pool, tenant_id, hook, event_type: str, event_id: str, body: bytes) -> dict:
    """POST one event to one webhook and record the attempt. Never raises."""
    secret = decrypt(hook["secret_encrypted"])
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "talqing-webhooks/1",
        "X-Talqing-Event": event_type,
        "X-Talqing-Delivery": event_id,
        "X-Talqing-Signature": f"sha256={sign(secret, body)}",
    }
    status, code, err = "failed", None, None
    t0 = time.monotonic()
    try:
        # SSRF guard: a tenant-controlled URL must not reach cloud-metadata or any
        # private/internal address. resolve_and_pin rejects those and pins the
        # connection to the vetted IP (no redirect-following → no rebind past it).
        pinned_url, pin_headers, pin_ext = await resolve_and_pin(hook["url"])
        headers.update(pin_headers)
        async with httpx.AsyncClient(timeout=DELIVERY_TIMEOUT, follow_redirects=False) as client:
            req = client.build_request(
                "POST",
                pinned_url,
                content=body,
                headers=headers,
                extensions=pin_ext or None,
            )
            resp = await client.send(req)
        code = resp.status_code
        status = "delivered" if resp.is_success else "failed"
    except ValueError as e:  # SSRF-blocked (private/internal/unresolvable) — never fetch
        err = f"blocked destination: {str(e)[:400]}"
    except Exception as e:  # network/timeout/DNS — best-effort, just log
        err = str(e)[:500]
    dur_ms = int((time.monotonic() - t0) * 1000)

    try:
        await pool.execute(
            "INSERT INTO webhook_deliveries "
            "(tenant_id, webhook_id, event_type, event_id, status, status_code, error, duration_ms) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8)",
            tenant_id,
            hook["id"],
            event_type,
            event_id,
            status,
            code,
            err,
            dur_ms,
        )
        await pool.execute(
            "UPDATE webhooks SET last_status = $2, last_delivered_at = now() "
            "WHERE id = $1 AND tenant_id = $3",
            hook["id"],
            status,
            tenant_id,
        )
    except Exception:
        logger.exception("failed to record webhook delivery")
    return {
        "webhook_id": str(hook["id"]),
        "status": status,
        "status_code": code,
        "error": err,
    }


async def load_subscriptions(tenant: Tenant) -> list[asyncpg.Record]:
    """This tenant's active webhooks — what ``dispatch`` matches an event against.

    Public so a caller that will dispatch several events for one tenant can read
    the table once and hand the rows back in. Raises; ``dispatch`` is the one
    that swallows.
    """
    pool = await db.tenant_pool(tenant)
    return await pool.fetch(
        "SELECT id, url, secret_encrypted, subscribed_events "
        "FROM webhooks WHERE status = 'active' AND tenant_id = $1",
        tenant.id,
    )


async def dispatch(
    tenant: Tenant,
    event_type: str,
    agent_id: str | None,
    data: dict,
    *,
    hooks: Sequence[asyncpg.Record] | None = None,
) -> None:
    """Fan an event out to every matching active webhook for the tenant.

    A webhook matches when it is active and it subscribes to the type
    (empty subscribed_events = all). Best-effort; swallows its own errors so a
    webhook problem never breaks the call/agent path that triggered it.

    ``hooks`` skips the subscription read for a caller that already holds it —
    a voice job process serves ONE tenant for ONE call and dispatches up to
    three events, and this is a tiny table it was reading each time. Omit it and
    every dispatch re-reads, which is what `background-worker` needs: it is
    long-lived and must see subscription changes.
    """
    try:
        pool = await db.tenant_pool(tenant)
        if hooks is None:
            hooks = await load_subscriptions(tenant)
    except Exception:
        logger.exception("webhook dispatch: could not load webhooks for tenant %s", tenant.id)
        return

    targets = [
        h for h in hooks if not h["subscribed_events"] or event_type in h["subscribed_events"]
    ]
    if not targets:
        return

    event_id, body = _envelope(event_type, str(tenant.id), agent_id, data)
    await asyncio.gather(
        *(_deliver(pool, tenant.id, h, event_type, event_id, body) for h in targets)
    )


async def dispatch_bounded(
    tenant: Tenant,
    event_type: str,
    agent_id: str | None,
    data: dict,
    *,
    hooks: Sequence[asyncpg.Record] | None = None,
    timeout: float = BOUNDED_DISPATCH_TIMEOUT,
) -> None:
    """Attempt dispatch without allowing a terminating process to wait forever."""

    try:
        await asyncio.wait_for(
            dispatch(tenant, event_type, agent_id, data, hooks=hooks), timeout=timeout
        )
    except TimeoutError:
        logger.error(
            "webhook dispatch for %s exceeded %.1fs and was canceled",
            event_type,
            timeout,
        )
    except Exception:
        logger.exception("webhook dispatch for %s failed", event_type)


async def deliver_one(
    tenant: Tenant,
    hook,
    event_type: str,
    data: dict,
    *,
    agent_id: str | None = None,
) -> dict:
    """Send a single event to one specific webhook (the test-fire button)."""
    pool = await db.tenant_pool(tenant)
    event_id, body = _envelope(event_type, str(tenant.id), agent_id, data)
    return await _deliver(pool, tenant.id, hook, event_type, event_id, body)
