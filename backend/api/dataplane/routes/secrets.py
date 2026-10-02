"""Secrets HTTP adapter over services.secrets.

Every member can see which secrets exist; only an ADMIN can add or remove one.
A secret is a credential into somebody else's system, so one that something
still references cannot be deleted.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Query

from api.core.schemas import OkResponse, Page
from api.dataplane.deps import AdminCtxDep, Context, CtxDep
from services import secrets as svc
from services.secrets import CreateSecretRequest, SecretResponse

router = APIRouter(prefix="/secrets", tags=["secrets"])


@router.get("", response_model=Page[SecretResponse])
async def list_secrets(
    ctx: Context = CtxDep,
    limit: int = Query(200, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> Page[SecretResponse]:
    """List the workspace's secrets by name, with a masked hint of each value.

    Values are write-only: once stored, a secret can only be read by the runtime
    through a `{{secrets.NAME}}` reference.
    """
    return await svc.list_secrets(ctx, limit, offset)


@router.post("", status_code=201, response_model=SecretResponse)
async def create_secret(body: CreateSecretRequest, ctx: Context = AdminCtxDep):
    """Store a secret, replacing any existing one with the same name.

    `name` must be a valid identifier and is how tool operations and integration
    config refer to the value, as `{{secrets.NAME}}`. Organization admins only.
    """
    return await svc.create_secret(body, ctx)


@router.delete("/{secret_id}", response_model=OkResponse)
async def delete_secret(secret_id: UUID, ctx: Context = AdminCtxDep):
    """Delete a secret permanently. Refused with 409 while a tool, integration or
    carrier account references it. Organization admins only."""
    return await svc.delete_secret(secret_id, ctx)
