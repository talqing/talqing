"""Integration triggers HTTP adapter over services.integrations."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Query

from api.core.schemas import OkResponse, Page
from api.dataplane.deps import Context, CtxDep, WriteCtxDep
from services import integrations
from services.integrations import (
    CreateIntegrationTriggerRequest,
    IntegrationTriggerResponse,
    PatchIntegrationTriggerRequest,
)

router = APIRouter()


@router.get("/{integration_id}/triggers", response_model=Page[IntegrationTriggerResponse])
async def list_integration_triggers(
    integration_id: UUID,
    ctx: Context = CtxDep,
    limit: int = Query(200, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> Page[IntegrationTriggerResponse]:
    """List the trigger deployments under an integration.

    A trigger binds an inbound provider event — a Telegram message — to a
    published agent version.
    """
    return await integrations.list_integration_triggers(integration_id, ctx, limit, offset)


@router.post(
    "/{integration_id}/triggers",
    status_code=201,
    response_model=IntegrationTriggerResponse,
)
async def create_integration_trigger(
    integration_id: UUID,
    body: CreateIntegrationTriggerRequest,
    ctx: Context = WriteCtxDep,
) -> IntegrationTriggerResponse:
    """Deploy an agent to an inbound provider event.

    `reply_mode` decides what happens with the agent's answer: `public_reply`
    sends it to the customer, `internal_note` posts it privately where the
    provider supports that, `none` generates without sending.

    Enabling validates that the integration is active and the assigned agent is
    a published text agent, so create disabled while still wiring things up.
    """
    return await integrations.create_integration_trigger(integration_id, body, ctx)


@router.patch(
    "/{integration_id}/triggers/{trigger_id}",
    response_model=IntegrationTriggerResponse,
)
async def patch_integration_trigger(
    integration_id: UUID,
    trigger_id: UUID,
    body: PatchIntegrationTriggerRequest,
    ctx: Context = WriteCtxDep,
) -> IntegrationTriggerResponse:
    """Update a trigger: reassign the agent, change reply mode, enable or
    disable it.

    Omitted fields are left unchanged, and enabling one runs the same checks
    creating it did.
    """
    return await integrations.patch_integration_trigger(integration_id, trigger_id, body, ctx)


@router.delete(
    "/{integration_id}/triggers/{trigger_id}",
    response_model=OkResponse,
)
async def delete_integration_trigger(
    integration_id: UUID,
    trigger_id: UUID,
    ctx: Context = WriteCtxDep,
) -> OkResponse:
    """Delete a trigger permanently. The agent stops receiving that event."""
    return await integrations.delete_integration_trigger(integration_id, trigger_id, ctx)
