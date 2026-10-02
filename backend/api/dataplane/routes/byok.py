"""BYOK provider-key HTTP adapter over services.byok.

Writes are ADMIN-only. A provider key is the blast-radius case: removing one
stops every agent in the organization that needs that provider from running and
from being published, which is a production outage rather than an edit.
"""

from __future__ import annotations

from fastapi import APIRouter

from api.core.schemas import OkResponse
from api.dataplane.deps import AdminCtxDep, Context, CtxDep
from services import byok as svc
from services.byok import (
    ProviderKeyResponse,
    ProviderKeysResponse,
    SetProviderKeyRequest,
)

router = APIRouter(prefix="/byok", tags=["byok"])


@router.get("", response_model=ProviderKeysResponse)
async def list_provider_keys(ctx: Context = CtxDep) -> ProviderKeysResponse:
    """List every AI provider and whether this workspace has an API key for it.

    Talqing is strict BYOK: agents run on the workspace's own provider keys.
    Publishing fails if the agent needs a provider with no key configured.
    Every key listed here was accepted by its provider when it was saved.
    """
    return await svc.list_provider_keys(ctx)


@router.put("/{provider}", response_model=ProviderKeyResponse)
async def set_provider_key(
    provider: str,
    body: SetProviderKeyRequest,
    ctx: Context = AdminCtxDep,
) -> ProviderKeyResponse:
    """Store this workspace's API key for one provider, replacing any existing key.

    The key is called against the provider first and only stored if accepted: a
    rejected key is a 400 that leaves any existing key untouched, and a provider
    that cannot be reached is a 502. Encrypted at rest and never returned.
    Organization admins only.
    """
    return await svc.set_provider_key(provider, body, ctx)


@router.delete("/{provider}", response_model=OkResponse)
async def delete_provider_key(provider: str, ctx: Context = AdminCtxDep) -> OkResponse:
    """Remove this organization's API key for one provider. Agents that need it
    will stop working and can no longer be published. Organization admins only."""
    return await svc.delete_provider_key(provider, ctx)
