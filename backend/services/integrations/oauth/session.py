"""OAuth connect HTTP path: start / complete / finalize / redirects.

Routes call ``start_oauth`` / ``complete_oauth``. Provider-specific authorize
and token exchange live under ``providers/`` (``providers.asana.AsanaOAuthFlow``, …).
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from urllib.parse import urlencode
from uuid import UUID

import asyncpg
from fastapi import HTTPException
from starlette.responses import RedirectResponse

from api.core import security
from services.user import Context
from settings import get_settings

from ..definitions import OAUTH_ACCOUNT_KEYS
from .credentials import upsert_oauth_credential
from .protocol import OAuthCredentialError
from .registry import require_oauth_flow


def _dashboard_redirect(path: str, params: dict[str, str] | None = None) -> str:
    base = get_settings().app.dashboard_public_url.rstrip("/")
    query = f"?{urlencode(params)}" if params else ""
    return f"{base}{path}{query}"


def _oauth_setup_error(exc: Exception) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


async def _require_oauth_reconnect_row(
    ctx: Context,
    *,
    integration_id: UUID | None,
    provider: str,
    label: str,
) -> None:
    """When reconnecting an existing row, ensure it exists and matches provider."""
    if integration_id is None:
        return
    pool = await ctx.tenant_pool()
    row = await pool.fetchrow(
        "SELECT id, provider FROM integrations WHERE id = $1 AND tenant_id = $2",
        integration_id,
        ctx.tenant.id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="integration not found")
    if row["provider"] != provider:
        raise HTTPException(
            status_code=400,
            detail=f"only {label} integrations can reconnect with this flow",
        )


async def _unique_integration_name(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    base: str,
) -> str:
    names = {
        r["display_name"]
        for r in await conn.fetch(
            "SELECT display_name FROM integrations WHERE tenant_id = $1",
            tenant_id,
        )
    }
    if base not in names:
        return base
    for n in range(2, 100):
        candidate = f"{base} ({n})"
        if candidate not in names:
            return candidate
    raise HTTPException(status_code=400, detail="too many integrations with the same name")


async def _finalize_oauth_integration(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    created_by: UUID,
    provider: str,
    integration_id: str | None,
    display_name_base: str,
    provider_account_info: dict[str, Any],
    account_email: str,
    provider_subject: str | None,
    scopes: list[str],
    access_token: str,
    refresh_token: str,
    expires_at: datetime,
    token_type: str,
    oauth_client_id: str | None = None,
    oauth_metadata: dict[str, str] | None = None,
) -> str:
    """Upsert the integration row + encrypted OAuth credential.

    Identity goes in ``provider_account_info``. ``mcp_config`` stays empty for
    OAuth providers. ``allowed_tools`` is left alone: a new connection starts
    with every tool exposed until it is approved, and a reconnect must not throw
    away the approval that is already there. auth_type is not stored — it is
    derived from the provider registry.

    ``tools_namespace`` is the provider key, flat. Deliberately NOT deduped the
    way ``display_name`` is just above: a second Asana connection gets ``asana``
    again, and agent publish names both integrations in one sentence the tenant
    can act on — better than a silent ``asana_2`` they have to notice.
    """
    account_payload = dict(provider_account_info)
    # Connect writes this bag straight to the DB; every later patch runs it
    # through `validate_integration_definition`. A provider emitting a key
    # outside the allowed set therefore connects cleanly and then fails EVERY
    # edit with "unknown config field" — which is how HubSpot shipped
    # un-patchable (hub_id, user_id) with Calendly one branch behind it
    # (account_uri). A developer error, so it fails here rather than reaching a
    # tenant as a broken Save button.
    unknown = sorted(set(account_payload) - OAUTH_ACCOUNT_KEYS)
    if unknown:
        raise OAuthCredentialError(
            f"{provider} identity carries {unknown}, which integration validation "
            "rejects. Store it under an allowed key or add it to OAUTH_ACCOUNT_KEYS."
        )

    if integration_id:
        current = await conn.fetchrow(
            """
            SELECT id
            FROM integrations
            WHERE id = $1 AND tenant_id = $2 AND provider = $3
            """,
            UUID(str(integration_id)),
            tenant_id,
            provider,
        )
        if not current:
            raise HTTPException(status_code=404, detail="integration not found")
        row = await conn.fetchrow(
            """
            UPDATE integrations
            SET provider_account_info = $3::jsonb,
                mcp_config = '{}'::jsonb,
                status = 'active',
                updated_at = now()
            WHERE id = $1 AND tenant_id = $2 AND provider = $4
            RETURNING id
            """,
            UUID(str(integration_id)),
            tenant_id,
            json.dumps(account_payload),
            provider,
        )
        final_integration_id = str(row["id"])
    else:
        display_name = await _unique_integration_name(
            conn,
            tenant_id=tenant_id,
            base=display_name_base,
        )
        try:
            row = await conn.fetchrow(
                """
                INSERT INTO integrations (
                    tenant_id, display_name, provider, tools_namespace,
                    provider_account_info, mcp_config, status, created_by
                )
                VALUES ($1, $2, $3, $3, $4::jsonb, '{}'::jsonb, 'active', $5)
                RETURNING id
                """,
                tenant_id,
                display_name,
                provider,
                json.dumps(account_payload),
                created_by,
            )
        except asyncpg.UniqueViolationError as exc:
            raise HTTPException(
                status_code=409,
                detail="an integration for this account already exists",
            ) from exc
        final_integration_id = str(row["id"])

    await upsert_oauth_credential(
        conn,
        tenant_id=str(tenant_id),
        integration_id=final_integration_id,
        provider=provider,
        account_email=account_email or "",
        provider_subject=provider_subject,
        scopes=scopes,
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=expires_at,
        token_type=token_type,
        oauth_client_id=oauth_client_id,
        oauth_metadata=oauth_metadata,
    )
    return final_integration_id


def _oauth_error_redirect(message: str) -> RedirectResponse:
    return RedirectResponse(
        _dashboard_redirect("/integrations", {"oauth_error": message}),
        status_code=303,
    )


def _oauth_success_redirect(provider: str, integration_id: str) -> RedirectResponse:
    return RedirectResponse(
        _dashboard_redirect(
            "/integrations",
            {"connected": provider, "integration_id": integration_id},
        ),
        status_code=303,
    )


async def start_oauth(
    provider: str,
    *,
    integration_id: UUID | None,
    ctx: Context,
) -> RedirectResponse:
    flow = require_oauth_flow(provider)
    if not flow.is_configured():
        raise _oauth_setup_error(
            OAuthCredentialError(f"{flow.label} OAuth is not configured on this deployment")
        )
    await _require_oauth_reconnect_row(
        ctx,
        integration_id=integration_id,
        provider=provider,
        label=flow.label,
    )
    try:
        url = await flow.build_authorize_url(
            user_id=str(ctx.user.id),
            tenant_id=str(ctx.tenant.id),
            integration_id=str(integration_id) if integration_id else None,
        )
    except (OAuthCredentialError, RuntimeError) as exc:
        # Browser start flow: always land on the dashboard with a clear error.
        return _oauth_error_redirect(str(exc))
    return RedirectResponse(url, status_code=303)


async def complete_oauth(
    provider: str,
    *,
    code: str | None,
    state: str | None,
    error: str | None,
    error_description: str | None,
    ctx: Context,
) -> RedirectResponse:
    flow = require_oauth_flow(provider)
    if not state:
        raise HTTPException(status_code=400, detail="missing OAuth state")
    oauth_state = security.read_oauth_state(state, provider=provider)
    if not oauth_state:
        raise HTTPException(status_code=400, detail="invalid or expired OAuth state")
    if oauth_state["tenant_id"] != str(ctx.tenant.id) or oauth_state["user_id"] != str(ctx.user.id):
        raise HTTPException(
            status_code=400, detail="OAuth state does not match the current session"
        )
    if error:
        return _oauth_error_redirect(error_description or error)
    if not code:
        return _oauth_error_redirect("missing authorization code")

    try:
        result = await flow.exchange_code(code=code, oauth_state=oauth_state)
    except OAuthCredentialError as exc:
        return _oauth_error_redirect(str(exc))
    except RuntimeError as exc:
        return _oauth_error_redirect(str(exc))

    pool = await ctx.tenant_pool()
    integration_id = oauth_state.get("integration_id")
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                final_integration_id = await _finalize_oauth_integration(
                    conn,
                    tenant_id=ctx.tenant.id,
                    created_by=ctx.user.id,
                    provider=provider,
                    integration_id=str(integration_id) if integration_id else None,
                    display_name_base=result.display_name_base,
                    provider_account_info=result.provider_account_info,
                    account_email=result.account_email,
                    provider_subject=result.provider_subject,
                    scopes=result.scopes,
                    access_token=result.access_token,
                    refresh_token=result.refresh_token,
                    expires_at=result.expires_at,
                    token_type=result.token_type,
                    oauth_client_id=result.oauth_client_id,
                    oauth_metadata=result.oauth_metadata,
                )
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, str) else "OAuth connection failed"
        return _oauth_error_redirect(detail)
    except OAuthCredentialError as exc:
        return _oauth_error_redirect(str(exc))

    return _oauth_success_redirect(provider, final_integration_id)
