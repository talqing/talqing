"""Webhook config CRUD + test-fire + delivery log.

Webhooks live in the tenant data DB and are tenant-wide: one subscription
receives events for every agent. The signing secret is stored Fernet-encrypted
at rest and returned in plaintext only at create / regenerate time (shown once,
like Stripe); reads return a masked hint.

Durability note (deliberate): delivery is best-effort and in-process (see
delivery.py — dispatched via FastAPI BackgroundTasks from the mutating routes).
Each attempt is logged to webhook_deliveries so a miss is visible and
re-testable, but there's no durable retry queue. That's an intentional choice
for lifecycle notifications (unlike the Redis queues consumed by the background
worker); a delivery job type can be added later without changing this contract."""

from __future__ import annotations

import secrets as pysecrets
from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import HTTPException, Query
from pydantic import BaseModel

from api.core.schemas import OkResponse, Page, coalesce, mask_tail, page_slice
from services.tools import check_url_async
from services.user import Context
from utils.crypto import decrypt, encrypt

from . import events
from .delivery import deliver_one


async def _validate_url(url: str) -> None:
    """Reject a non-http(s) or private/internal destination at config time, so the
    user gets immediate feedback rather than a silently-failing 'blocked' delivery
    (delivery is also pinned — this is fail-fast UX, not the security boundary)."""
    if not url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="url must be http(s)")
    try:
        await check_url_async(url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"url not allowed: {e}")


def _check_events(subscribed: list[str]) -> None:
    bad = [e for e in subscribed if e not in events.ALL]
    if bad:
        raise HTTPException(status_code=400, detail=f"unknown event types: {bad}")


class CreateWebhookRequest(BaseModel):
    url: str
    subscribed_events: list[str] = []  # empty = all
    secret: str | None = None  # generated if omitted


class PatchWebhookRequest(BaseModel):
    url: str | None = None
    subscribed_events: list[str] | None = None
    status: Literal["active", "disabled"] | None = None


class WebhookResponse(BaseModel):
    id: UUID
    url: str
    subscribed_events: list[str]
    status: str
    last_status: str | None = None
    last_delivered_at: datetime | None = None
    secret_hint: str
    secret: str | None = None  # full secret only on create/regenerate
    created_at: datetime
    updated_at: datetime


class EventTypesResponse(BaseModel):
    all: list[str]
    live: list[str]


class DeliveryResponse(BaseModel):
    event_type: str
    event_id: str | None = None
    status: str
    status_code: int | None = None
    error: str | None = None
    duration_ms: int | None = None
    created_at: datetime


class DeliveryResultResponse(BaseModel):
    webhook_id: str
    status: str
    status_code: int | None = None
    error: str | None = None


def _out(row, *, reveal_secret: str | None = None) -> WebhookResponse:
    return WebhookResponse(
        id=row["id"],
        url=row["url"],
        subscribed_events=list(row["subscribed_events"]),
        status=row["status"],
        last_status=row["last_status"],
        last_delivered_at=row["last_delivered_at"],
        secret_hint=mask_tail(decrypt(row["secret_encrypted"]), "whsec_"),
        secret=reveal_secret,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


async def _get_webhook(webhook_id: UUID, ctx: Context) -> object:
    pool = await ctx.tenant_pool()
    row = await pool.fetchrow(
        "SELECT id, url, secret_encrypted, subscribed_events, status, last_status, "
        "last_delivered_at, created_at, updated_at, tenant_id "
        "FROM webhooks WHERE id = $1 AND tenant_id = $2",
        webhook_id,
        ctx.tenant.id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="webhook not found")
    return row


async def event_types(ctx: Context):
    """All subscribable event types + which are emitted today (UI multi-select)."""
    return EventTypesResponse(all=events.ALL, live=events.LIVE)


async def list_webhooks(
    ctx: Context,
    limit: int = 200,
    offset: int = 0,
) -> Page[WebhookResponse]:
    pool = await ctx.tenant_pool()
    rows = await pool.fetch(
        "SELECT id, url, secret_encrypted, subscribed_events, status, last_status, "
        "last_delivered_at, created_at, updated_at, tenant_id "
        "FROM webhooks WHERE tenant_id = $1 ORDER BY created_at DESC "
        "LIMIT $2 OFFSET $3",
        ctx.tenant.id,
        limit + 1,
        offset,
    )
    return page_slice([_out(r) for r in rows], limit=limit, offset=offset)


async def create_webhook(body: CreateWebhookRequest, ctx: Context):
    await _validate_url(body.url)
    _check_events(body.subscribed_events)

    secret = body.secret or ("whsec_" + pysecrets.token_urlsafe(32))
    pool = await ctx.tenant_pool()
    row = await pool.fetchrow(
        """
        INSERT INTO webhooks (url, secret_encrypted, subscribed_events, tenant_id, created_by)
        VALUES ($1, $2, $3, $4, $5)
        RETURNING *
        """,
        body.url,
        encrypt(secret),
        body.subscribed_events,
        ctx.tenant.id,
        ctx.user.id,
    )
    return _out(row, reveal_secret=secret)


async def patch_webhook(webhook_id: UUID, body: PatchWebhookRequest, ctx: Context):
    pool = await ctx.tenant_pool()
    row = await _get_webhook(webhook_id, ctx)
    if body.url is not None:
        await _validate_url(body.url)
    if body.subscribed_events is not None:
        _check_events(body.subscribed_events)

    url = coalesce(body.url, row["url"])
    subs = coalesce(body.subscribed_events, list(row["subscribed_events"]))
    status = coalesce(body.status, row["status"])
    updated = await pool.fetchrow(
        """
        UPDATE webhooks SET url=$2, subscribed_events=$3, status=$4, updated_at=now()
        WHERE id=$1 AND tenant_id=$5 RETURNING *
        """,
        webhook_id,
        url,
        subs,
        status,
        ctx.tenant.id,
    )
    return _out(updated)


async def delete_webhook(webhook_id: UUID, ctx: Context):
    pool = await ctx.tenant_pool()
    row = await pool.fetchrow(
        "DELETE FROM webhooks WHERE id = $1 AND tenant_id = $2 RETURNING id",
        webhook_id,
        ctx.tenant.id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="webhook not found")
    return OkResponse()


async def rotate_webhook_secret(webhook_id: UUID, ctx: Context):
    """Regenerate the signing secret. Returned in plaintext exactly once."""
    pool = await ctx.tenant_pool()
    secret = "whsec_" + pysecrets.token_urlsafe(32)
    row = await pool.fetchrow(
        "UPDATE webhooks SET secret_encrypted = $2, updated_at = now() "
        "WHERE id = $1 AND tenant_id = $3 RETURNING *",
        webhook_id,
        encrypt(secret),
        ctx.tenant.id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="webhook not found")
    return _out(row, reveal_secret=secret)


async def test_webhook(webhook_id: UUID, ctx: Context):
    """Fire a test event at one webhook.

    Only active webhooks can be tested — disabled hooks must not receive traffic.
    Subscription filters do not apply: webhook.test is a probe of the endpoint,
    not a production event type tenants subscribe to.
    """
    row = await _get_webhook(webhook_id, ctx)
    if row["status"] != "active":
        raise HTTPException(
            status_code=400,
            detail="webhook is disabled; enable it before testing",
        )
    result = await deliver_one(
        ctx.tenant,
        row,
        events.WEBHOOK_TEST,
        {"message": "talqing test event", "webhook_id": str(webhook_id)},
    )
    return DeliveryResultResponse.model_validate(result)


async def webhook_deliveries(
    webhook_id: UUID,
    ctx: Context,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    pool = await ctx.tenant_pool()
    await _get_webhook(webhook_id, ctx)
    # fetch one extra row to learn there's a next page without a count(*)
    rows = await pool.fetch(
        "SELECT event_type, event_id, status, status_code, error, duration_ms, created_at "
        "FROM webhook_deliveries WHERE tenant_id = $1 AND webhook_id = $2 "
        "ORDER BY created_at DESC LIMIT $3 OFFSET $4",
        ctx.tenant.id,
        webhook_id,
        limit + 1,
        offset,
    )
    has_more = len(rows) > limit
    items = [DeliveryResponse.model_validate(dict(r)) for r in rows[:limit]]
    return Page[DeliveryResponse](items=items, has_more=has_more, limit=limit, offset=offset)
