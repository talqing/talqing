"""Webhooks HTTP adapter over services.webhooks."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Query

from api.core.schemas import OkResponse, Page
from api.dataplane.deps import Context, CtxDep, WriteCtxDep
from services import webhooks as svc
from services.webhooks import (
    CreateWebhookRequest,
    DeliveryResponse,
    DeliveryResultResponse,
    EventTypesResponse,
    PatchWebhookRequest,
    WebhookResponse,
)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.get("/event-types", response_model=EventTypesResponse)
async def event_types(ctx: Context = CtxDep):
    """List every event a webhook can subscribe to, and which are emitted today."""
    return await svc.event_types(ctx)


@router.get("", response_model=Page[WebhookResponse])
async def list_webhooks(
    ctx: Context = CtxDep,
    limit: int = Query(200, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> Page[WebhookResponse]:
    """List the workspace's webhooks with their subscriptions and last delivery
    result. Signing secrets are returned only as a masked hint."""
    return await svc.list_webhooks(ctx, limit, offset)


@router.post("", status_code=201, response_model=WebhookResponse)
async def create_webhook(body: CreateWebhookRequest, ctx: Context = WriteCtxDep):
    """Create a webhook. Webhooks are workspace-wide — they fire for every agent.

    An empty `subscribed_events` subscribes to all events. A signing secret is
    generated when none is supplied; either way the plaintext secret is returned
    once here and never again.
    """
    return await svc.create_webhook(body, ctx)


@router.patch("/{webhook_id}", response_model=WebhookResponse)
async def patch_webhook(webhook_id: UUID, body: PatchWebhookRequest, ctx: Context = WriteCtxDep):
    """Update a webhook's URL, subscriptions or status. Omitted fields are left
    unchanged; a disabled webhook stops receiving deliveries."""
    return await svc.patch_webhook(webhook_id, body, ctx)


@router.delete("/{webhook_id}", response_model=OkResponse)
async def delete_webhook(webhook_id: UUID, ctx: Context = WriteCtxDep):
    """Delete a webhook permanently."""
    return await svc.delete_webhook(webhook_id, ctx)


@router.post("/{webhook_id}/rotate-secret", response_model=WebhookResponse)
async def rotate_webhook_secret(webhook_id: UUID, ctx: Context = WriteCtxDep):
    """Issue a new signing secret, invalidating the old one immediately.

    The new plaintext secret is returned once here; the receiver must be updated
    or signature checks will start failing.
    """
    return await svc.rotate_webhook_secret(webhook_id, ctx)


@router.post("/{webhook_id}/test", response_model=DeliveryResultResponse)
async def test_webhook(webhook_id: UUID, ctx: Context = WriteCtxDep):
    """Send a signed test event and return the endpoint's response, so delivery
    and signature verification can be checked end to end."""
    return await svc.test_webhook(webhook_id, ctx)


@router.get("/{webhook_id}/deliveries", response_model=Page[DeliveryResponse])
async def webhook_deliveries(
    webhook_id: UUID,
    ctx: Context = CtxDep,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """List recent delivery attempts for one webhook — event, response status
    and error — newest first. The place to look when a webhook seems silent."""
    return await svc.webhook_deliveries(webhook_id, ctx, limit, offset)
