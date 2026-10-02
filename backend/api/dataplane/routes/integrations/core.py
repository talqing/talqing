"""Integrations core HTTP adapter over services.integrations."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, HTTPException, Query

from api.core.schemas import OkResponse, Page
from api.dataplane.deps import Context, CtxDep, WriteCtxDep
from compiler.integrations import IntegrationCompileError, list_mcp_tools
from services import integrations
from services.integrations import (
    CreateIntegrationRequest,
    IntegrationCatalogItem,
    IntegrationMcpTool,
    IntegrationMcpToolsResponse,
    IntegrationResponse,
    PatchIntegrationRequest,
)

router = APIRouter()


@router.get("", response_model=Page[IntegrationResponse])
async def list_integrations(
    ctx: Context = CtxDep,
    limit: int = Query(200, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> Page[IntegrationResponse]:
    """List the workspace's integrations with their provider, status and config.

    Secret values are never returned — credential fields come back as
    `{{secrets.NAME}}` references.
    """
    return await integrations.list_integrations(ctx, limit, offset)


@router.get("/catalog", response_model=Page[IntegrationCatalogItem])
async def integration_catalog(
    limit: int = Query(200, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> Page[IntegrationCatalogItem]:
    """List the providers that can be integrated, and how to connect each one.

    `auth_type` says which path a provider takes: `manual` through this API,
    `oauth` from the dashboard. `setup_fields` is the authoritative list of what
    each manual provider requires — read it rather than guessing. `capabilities`
    says whether it serves tools (MCP) or a messaging channel (triggers).
    """
    return await integrations.integration_catalog(limit, offset)


@router.post("", status_code=201, response_model=IntegrationResponse)
async def create_integration(
    body: CreateIntegrationRequest,
    ctx: Context = WriteCtxDep,
) -> IntegrationResponse:
    """Create a manual integration, fully configured.

    Which of `credentials_ref`, `provider_account_info`, `mcp_config` and
    `webhook_config` a provider needs is described by its `setup_fields` in the
    integration catalog. Secret-bearing fields take a plaintext value — stored
    as a workspace secret — or an existing `{{secrets.NAME}}` reference.

    Leave `allowed_tools` unset to expose every tool the server lists. OAuth
    providers are connected from the dashboard instead.
    """
    return await integrations.create_integration(body, ctx)


@router.get("/{integration_id}", response_model=IntegrationResponse)
async def get_integration(integration_id: UUID, ctx: Context = CtxDep) -> IntegrationResponse:
    """Fetch one integration: provider, status, structured config, trigger
    capabilities and inbound webhook setup state."""
    return await integrations.get_integration(integration_id, ctx)


@router.get("/{integration_id}/mcp-tools", response_model=IntegrationMcpToolsResponse)
async def list_integration_mcp_tools(
    integration_id: UUID, ctx: Context = CtxDep
) -> IntegrationMcpToolsResponse:
    """Connect to the integration's MCP server and list every tool it offers.

    A live call to the provider, so it also proves the connection works. The
    list is unfiltered — it is what tool approval is chosen from. Which of these
    an attached agent can actually call is the integration's `allowed_tools`
    (null there means all of them). Fails if the integration is not active or
    the server is unreachable.
    """
    integration, secrets = await integrations.load_active_mcp_integration(integration_id, ctx)
    try:
        descriptors = await list_mcp_tools(integration, secrets, tenant=ctx.tenant)
    except IntegrationCompileError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return IntegrationMcpToolsResponse(
        tools=[
            IntegrationMcpTool(name=d.name, exposed_name=d.exposed_name, description=d.description)
            for d in descriptors
        ]
    )


@router.patch("/{integration_id}", response_model=IntegrationResponse)
async def patch_integration(
    integration_id: UUID,
    body: PatchIntegrationRequest,
    ctx: Context = WriteCtxDep,
) -> IntegrationResponse:
    """Update an integration. Omitted fields are left unchanged.

    Sending `credentials_ref` rotates the credential. Only `active` integrations
    can be attached to agents or serve triggers.

    `allowed_tools` replaces the approved list wholesale: exactly those tools
    become callable by every agent this integration is attached to. It cannot be
    emptied — an agent that should call nothing is an integration that should be
    disabled.
    """
    return await integrations.patch_integration(integration_id, body, ctx)


@router.delete("/{integration_id}", response_model=OkResponse)
async def delete_integration(integration_id: UUID, ctx: Context = WriteCtxDep) -> OkResponse:
    """Delete an integration and its triggers permanently."""
    return await integrations.delete_integration(integration_id, ctx)
