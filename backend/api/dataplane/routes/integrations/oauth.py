"""Generic OAuth start/callback routes for all OAuth integration providers.

Provider-specific logic lives under ``services.integrations.providers`` (e.g. ``asana.AsanaOAuthFlow``).
URL shape is unchanged: ``/oauth/{provider}/start|callback``.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Query

from api.dataplane.deps import Context, WriteCtxDep
from services.integrations.oauth.registry import require_oauth_provider_path
from services.integrations.oauth.session import complete_oauth, start_oauth

router = APIRouter()


@router.get("/oauth/{provider}/start", include_in_schema=False)
async def oauth_start(
    provider: str,
    integration_id: UUID | None = Query(default=None),
    ctx: Context = WriteCtxDep,
):
    provider = require_oauth_provider_path(provider)
    return await start_oauth(provider, integration_id=integration_id, ctx=ctx)


@router.get("/oauth/{provider}/callback", include_in_schema=False)
async def oauth_callback(
    provider: str,
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
    error_description: str | None = Query(default=None),
    ctx: Context = WriteCtxDep,
):
    provider = require_oauth_provider_path(provider)
    return await complete_oauth(
        provider,
        code=code,
        state=state,
        error=error,
        error_description=error_description,
        ctx=ctx,
    )
