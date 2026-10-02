"""Integration catalog and CRUD (HTTP + CoPilot).

Request/response shapes: ``services.integrations.models``.
Provider catalog: ``services.integrations.catalog``.

Internal code uses ``Integration``; public APIs return ``IntegrationResponse``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any
from uuid import UUID

import asyncpg
from fastapi import HTTPException

from api.core.schemas import OkResponse, Page, coalesce, page_slice, validation_error
from services.secrets import (
    list_secret_names,
    load_secrets,
    materialize_secret_input,
    resolve_secretish,
)
from services.user import Context

from .catalog import (
    INTEGRATION_PROVIDERS,
    OAUTH_PROVIDER_SET,
    PROVIDERS,
    TOOL_PROVIDER_SET,
    SetupField,
    auth_type_for_provider,
    default_capabilities_for_provider,
    get_provider,
)
from .definitions import validate_integration_definition
from .models import (
    INTEGRATION_COLUMNS,
    CreateIntegrationRequest,
    Integration,
    IntegrationCapabilities,
    IntegrationCatalogItem,
    IntegrationResponse,
    IntegrationSetupField,
    PatchIntegrationRequest,
)
from .oauth.credentials import load_oauth_credential, revoke_provider_tokens
from .oauth.registry import oauth_provider_configured
from .providers.resend import ResendApiError, fetch_account_identity
from .providers.telegram import (
    TelegramBotApiError,
    delete_bot_webhook,
    fetch_bot_identity,
    merge_bot_identity,
    validate_webhook_secret_token,
)
from .providers.whatsapp import disconnect_number
from .providers.whatsapp.bsp import BspError
from .providers.whatsapp.gupshup import GupshupWhatsApp
from .providers.whatsapp.twilio import TwilioWhatsApp
from .triggers import sync_integration_triggers

Row = asyncpg.Record | Mapping[str, Any]


def capabilities_for_provider(provider: str) -> IntegrationCapabilities:
    """Typed complete capabilities from the provider registry."""
    return IntegrationCapabilities.model_validate(default_capabilities_for_provider(provider))


def oauth_integration_label(provider: str) -> str:
    from .catalog import INTEGRATION_PROVIDER_LABELS

    return INTEGRATION_PROVIDER_LABELS.get(provider, provider)


async def validate_definition(ctx: Context, definition: Mapping[str, Any]) -> None:
    secret_names = await list_secret_names(ctx.tenant)
    errors = validate_integration_definition(definition, secret_names=secret_names)
    if errors:
        raise validation_error(errors, "this integration has validation errors")


def _preferred_secret_name(provider: str, field: SetupField) -> str:
    if field.default_secret_name:
        return field.default_secret_name
    key = field.key.upper() if field.key != "credentials_ref" else "CREDENTIALS"
    return f"{provider.upper()}_{key}"


async def _materialize_secret_fields(
    ctx: Context,
    *,
    provider: str,
    credentials_ref: str | None,
    webhook_config: dict[str, Any],
) -> tuple[str | None, dict[str, Any]]:
    """Convert plaintext secret_ref inputs into ``{{secrets.NAME}}`` refs.

    Existing refs are left as-is (after existence check). custom_mcp headers are
    not auto-materialized — users wire ``{{secrets.X}}`` there intentionally.
    """
    spec = get_provider(provider)
    if spec is None:
        return credentials_ref, webhook_config

    secret_fields = [f for f in spec.setup_fields if f.type == "secret_ref"]
    if not secret_fields:
        return credentials_ref, webhook_config

    existing_names = set(await list_secret_names(ctx.tenant))
    next_credentials = credentials_ref
    next_webhook = dict(webhook_config)

    for field in secret_fields:
        preferred = _preferred_secret_name(provider, field)
        if field.target == "credentials_ref":
            if isinstance(next_credentials, str) and next_credentials.strip():
                next_credentials = await materialize_secret_input(
                    ctx,
                    next_credentials,
                    preferred_name=preferred,
                    existing_names=existing_names,
                )
            continue
        if field.target == "webhook_config":
            raw = next_webhook.get(field.key)
            if isinstance(raw, str) and raw.strip():
                next_webhook[field.key] = await materialize_secret_input(
                    ctx,
                    raw,
                    preferred_name=preferred,
                    existing_names=existing_names,
                )

    return next_credentials, next_webhook


def integration_from_row(row: Row) -> Integration:
    """Map a DB row to the internal ``Integration`` domain model."""
    return Integration.from_row(row)


async def _require_integration(
    ctx: Context,
    integration_id: UUID,
) -> Integration:
    pool = await ctx.tenant_pool()
    row = await pool.fetchrow(
        f"""
        SELECT {INTEGRATION_COLUMNS}
        FROM integrations
        WHERE id = $1 AND tenant_id = $2
        """,
        integration_id,
        ctx.tenant.id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="integration not found")
    return Integration.from_row(row)


async def _telegram_provider_account_info(
    ctx: Context,
    *,
    credentials_ref: object,
    existing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve bot token, call getMe, and return provider_account_info."""
    secrets = await load_secrets(ctx.tenant)
    token = resolve_secretish(credentials_ref, secrets)
    if not token:
        raise HTTPException(
            status_code=400,
            detail="Telegram credentials_ref did not resolve to a bot token",
        )
    try:
        identity = await fetch_bot_identity(token)
    except TelegramBotApiError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return merge_bot_identity(existing, identity)


async def _resend_provider_account_info(
    ctx: Context,
    *,
    credentials_ref: object,
) -> dict[str, Any]:
    """Resolve the API key, list domains, and return provider_account_info.

    Same shape as the Telegram resolver above, and a rejected key fails the
    write for the same reason: an integration saved with a credential the
    provider will not accept is one that looks connected and cannot send.
    """
    secrets = await load_secrets(ctx.tenant)
    api_key = resolve_secretish(credentials_ref, secrets)
    if not api_key:
        raise HTTPException(
            status_code=400,
            detail="Resend credentials_ref did not resolve to an API key",
        )
    try:
        return await fetch_account_identity(api_key)
    except ResendApiError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


async def _whatsapp_provider_account_info(
    ctx: Context,
    *,
    credentials_ref: object,
    info: dict[str, Any],
) -> dict[str, Any]:
    """Check the sender against the live BSP account and return what is stored.

    A sender saved without this is one that looks connected and cannot send:
    a Twilio sender that is not ONLINE on that account, or a Gupshup app id and
    key that do not belong together.
    """
    # Local: `services.telephony` imports this package back at load.
    from services.telephony.e164 import normalize_e164

    bsp = info.get("bsp")
    try:
        sender_e164 = normalize_e164(str(info.get("sender_e164") or ""))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"whatsapp: sender_e164 {exc}") from exc
    credential = resolve_secretish(credentials_ref, await load_secrets(ctx.tenant))
    if not credential:
        raise HTTPException(
            status_code=400, detail="whatsapp: credentials_ref did not resolve to a secret"
        )
    try:
        if bsp == "twilio":
            account_sid = str(info.get("account_sid") or "").strip()
            twilio = TwilioWhatsApp(
                account_sid=account_sid, auth_token=credential, sender_e164=sender_e164
            )
            sender = await twilio.require_online_sender()
            return {
                "bsp": "twilio",
                "account_sid": account_sid,
                "sender_e164": sender_e164,
                "sender_sid": sender["sid"],
            }
        if bsp == "gupshup":
            app_id = str(info.get("app_id") or "").strip()
            app_name = str(info.get("app_name") or "").strip()
            gupshup = GupshupWhatsApp(
                api_key=credential, app_id=app_id, app_name=app_name, sender_e164=sender_e164
            )
            # Listing the app's templates is the one self-serve read that proves
            # the key and the app id belong together.
            await gupshup.list_templates()
            return {
                "bsp": "gupshup",
                "app_id": app_id,
                "app_name": app_name,
                "sender_e164": sender_e164,
            }
    except BspError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    raise HTTPException(status_code=400, detail="whatsapp: bsp must be `twilio` or `gupshup`")


async def list_twilio_whatsapp_senders(
    ctx: Context, *, account_sid: str, credentials_ref: str
) -> list[dict[str, str]]:
    """The WhatsApp senders on a Twilio account, before an integration exists.

    Takes a plaintext auth token or a `{{secrets.NAME}}` reference; nothing is
    stored.
    """
    credential = resolve_secretish(credentials_ref, await load_secrets(ctx.tenant))
    if not credential:
        raise HTTPException(status_code=400, detail="the auth token did not resolve")
    client = TwilioWhatsApp(account_sid=account_sid, auth_token=credential, sender_e164="")
    try:
        return await client.list_senders()
    except BspError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _validate_telegram_webhook_secret(
    webhook_config: dict[str, Any], secrets: dict[str, str]
) -> None:
    """Ensure resolved webhook secret_token matches Telegram setWebhook rules."""
    raw = webhook_config.get("secret_token") if isinstance(webhook_config, dict) else None
    secret_token = resolve_secretish(raw, secrets)
    if not secret_token:
        raise HTTPException(
            status_code=400,
            detail="Telegram webhook_config.secret_token did not resolve",
        )
    try:
        validate_webhook_secret_token(secret_token)
    except TelegramBotApiError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _unique_violation_detail(exc: asyncpg.UniqueViolationError) -> str:
    constraint = getattr(exc, "constraint_name", None) or ""
    if "whatsapp_sender" in constraint:
        return "a WhatsApp integration for that number already exists"
    if "telegram_bot" in constraint:
        return "a Telegram integration with that bot already exists"
    if "oauth_subject" in constraint:
        return "an integration for this account already exists"
    if "display_name" in constraint:
        return "an integration with that name already exists"
    return "this integration conflicts with an existing connection"


async def list_integrations(
    ctx: Context,
    limit: int = 200,
    offset: int = 0,
) -> Page[IntegrationResponse]:
    pool = await ctx.tenant_pool()
    rows = await pool.fetch(
        f"""
        SELECT {INTEGRATION_COLUMNS}
        FROM integrations
        WHERE tenant_id = $1
        ORDER BY updated_at DESC
        LIMIT $2 OFFSET $3
        """,
        ctx.tenant.id,
        limit + 1,
        offset,
    )
    return page_slice(
        [Integration.from_row(row).to_response() for row in rows],
        limit=limit,
        offset=offset,
    )


async def integration_catalog(
    limit: int = 200,
    offset: int = 0,
) -> Page[IntegrationCatalogItem]:
    """Build catalog from the provider registry (single source of truth)."""
    items: list[IntegrationCatalogItem] = []
    for spec in PROVIDERS.values():
        enabled = True
        if spec.auth_type == "oauth":
            enabled = oauth_provider_configured(spec.provider)
        items.append(
            IntegrationCatalogItem(
                provider=spec.provider,  # type: ignore[arg-type]
                name=spec.label,
                description=spec.description,
                category=spec.category,
                auth_type=spec.auth_type,
                logo_url=spec.logo_url,
                enabled=enabled,
                capabilities=IntegrationCapabilities.model_validate(spec.capabilities()),
                setup_fields=[
                    IntegrationSetupField(
                        key=f.key,
                        label=f.label,
                        type=f.type,
                        target=f.target,
                        required=f.required,
                        placeholder=f.placeholder,
                        hint=f.hint,
                    )
                    for f in spec.setup_fields
                ],
            )
        )
    return page_slice(items[offset : offset + limit + 1], limit=limit, offset=offset)


async def create_integration(
    body: CreateIntegrationRequest,
    ctx: Context,
) -> IntegrationResponse:
    if body.provider not in INTEGRATION_PROVIDERS:
        raise HTTPException(
            status_code=400, detail=f"unknown integration provider: {body.provider}"
        )
    if body.provider in OAUTH_PROVIDER_SET:
        raise HTTPException(
            status_code=400,
            detail=f"connect {oauth_integration_label(body.provider)} with OAuth",
        )
    if body.allowed_tools is not None and body.provider not in TOOL_PROVIDER_SET:
        raise HTTPException(
            status_code=400,
            detail=f"{body.provider} integrations do not expose MCP tools to approve",
        )
    if body.tools_namespace is not None and body.provider not in TOOL_PROVIDER_SET:
        raise HTTPException(
            status_code=400,
            detail=f"{body.provider} integrations expose no tools to namespace",
        )
    # The provider key is already a legal namespace for every hosted provider in
    # the catalog, and it is the name the tenant thinks in. `custom_mcp` has no
    # such name — a server the tenant brought themselves gets no prefix unless
    # they type one.
    tools_namespace = body.tools_namespace
    if tools_namespace is None:
        tools_namespace = "" if body.provider == "custom_mcp" else body.provider

    credentials_ref, webhook_config = await _materialize_secret_fields(
        ctx,
        provider=body.provider,
        credentials_ref=body.credentials_ref,
        webhook_config=dict(body.webhook_config),
    )

    provider_account_info = dict(body.provider_account_info)
    if body.provider == "telegram":
        secrets = await load_secrets(ctx.tenant)
        _validate_telegram_webhook_secret(webhook_config, secrets)
        provider_account_info = await _telegram_provider_account_info(
            ctx,
            credentials_ref=credentials_ref,
            existing=None,
        )
    elif body.provider == "resend":
        provider_account_info = await _resend_provider_account_info(
            ctx, credentials_ref=credentials_ref
        )
    elif body.provider == "whatsapp":
        provider_account_info = await _whatsapp_provider_account_info(
            ctx, credentials_ref=credentials_ref, info=provider_account_info
        )
    # Pre-persist draft for config validation (not a full Integration yet).
    definition = {
        "id": "draft",
        "display_name": body.display_name,
        "provider": body.provider,
        "auth_type": auth_type_for_provider(body.provider),
        "credentials_ref": credentials_ref,
        "provider_account_info": provider_account_info,
        "mcp_config": body.mcp_config,
        "webhook_config": webhook_config,
        "metadata": body.metadata,
    }
    await validate_definition(ctx, definition)

    pool = await ctx.tenant_pool()
    try:
        row = await pool.fetchrow(
            f"""
            INSERT INTO integrations (
                tenant_id, display_name, provider, credentials_ref,
                provider_account_info, mcp_config, webhook_config, allowed_tools,
                tools_namespace, metadata, created_by
            )
            VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb, $7::jsonb, $8, $9, $10::jsonb, $11)
            RETURNING {INTEGRATION_COLUMNS}
            """,
            ctx.tenant.id,
            body.display_name,
            body.provider,
            credentials_ref,
            json.dumps(provider_account_info),
            json.dumps(body.mcp_config),
            json.dumps(webhook_config),
            body.allowed_tools,
            tools_namespace,
            json.dumps(body.metadata),
            ctx.user.id,
        )
    except asyncpg.UniqueViolationError as exc:
        raise HTTPException(status_code=409, detail=_unique_violation_detail(exc)) from exc
    return Integration.from_row(row).to_response()


async def get_integration(integration_id: UUID, ctx: Context) -> IntegrationResponse:
    return (await _require_integration(ctx, integration_id)).to_response()


async def patch_integration(
    integration_id: UUID,
    body: PatchIntegrationRequest,
    ctx: Context,
) -> IntegrationResponse:
    current = await _require_integration(ctx, integration_id)
    pool = await ctx.tenant_pool()

    display_name = coalesce(body.display_name, current.display_name)
    # Only materialize fields the client actually sent; empty/omit keeps current.
    incoming_credentials = body.credentials_ref
    incoming_webhook = dict(body.webhook_config) if body.webhook_config is not None else {}
    if incoming_credentials is not None or incoming_webhook:
        materialized_credentials, materialized_webhook = await _materialize_secret_fields(
            ctx,
            provider=current.provider,
            credentials_ref=incoming_credentials,
            webhook_config=incoming_webhook,
        )
        if incoming_credentials is not None:
            credentials_ref = materialized_credentials
        else:
            credentials_ref = current.credentials_ref
        webhook_config = dict(current.webhook_config)
        webhook_config.update(materialized_webhook)
    else:
        credentials_ref = current.credentials_ref
        webhook_config = dict(current.webhook_config)

    provider_account_info = dict(current.provider_account_info)
    if body.provider_account_info is not None:
        provider_account_info.update(body.provider_account_info)
    mcp_config = dict(current.mcp_config)
    if body.mcp_config is not None:
        mcp_config.update(body.mcp_config)
    metadata = dict(current.metadata)
    if body.metadata is not None:
        metadata.update(body.metadata)
    status = coalesce(body.status, current.status)

    if body.allowed_tools is not None and current.provider not in TOOL_PROVIDER_SET:
        raise HTTPException(
            status_code=400,
            detail=f"{current.provider} integrations do not expose MCP tools to approve",
        )
    if body.tools_namespace is not None and current.provider not in TOOL_PROVIDER_SET:
        raise HTTPException(
            status_code=400,
            detail=f"{current.provider} integrations expose no tools to namespace",
        )
    # Replaces the list wholesale — approval is a set, not an accumulation.
    allowed_tools = coalesce(body.allowed_tools, current.allowed_tools)
    # `coalesce` would be wrong here: `""` is a real value that clears the
    # prefix, and only an omitted field keeps the current one.
    tools_namespace = (
        current.tools_namespace if body.tools_namespace is None else body.tools_namespace
    )

    if current.provider in OAUTH_PROVIDER_SET and status == "active":
        credential_exists = await pool.fetchval(
            """
            SELECT EXISTS (
                SELECT 1 FROM integration_oauth_credentials
                WHERE tenant_id = $1 AND integration_id = $2 AND provider = $3
            )
            """,
            ctx.tenant.id,
            integration_id,
            current.provider,
        )
        if not credential_exists:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"reconnect {oauth_integration_label(current.provider)} "
                    "before activating this integration"
                ),
            )

    if current.provider == "telegram":
        secrets = await load_secrets(ctx.tenant)
        _validate_telegram_webhook_secret(webhook_config, secrets)
        provider_account_info = await _telegram_provider_account_info(
            ctx,
            credentials_ref=credentials_ref,
            existing=provider_account_info,
        )
    elif current.provider == "whatsapp":
        # The number names every thread on it, so it is fixed for the life of
        # the integration; a different number is a different integration.
        if body.provider_account_info is not None:
            raise HTTPException(
                status_code=400,
                detail="a WhatsApp sender cannot be changed; connect the other number instead",
            )
        if body.credentials_ref is not None:
            provider_account_info = await _whatsapp_provider_account_info(
                ctx, credentials_ref=credentials_ref, info=provider_account_info
            )
    elif current.provider == "resend" and body.credentials_ref is not None:
        # Only on a key rotation. Every other patch of a Resend row — the tool
        # approval modal saves one on each tick — must not call Resend.
        provider_account_info = await _resend_provider_account_info(
            ctx, credentials_ref=credentials_ref
        )

    definition = {
        "id": str(current.id),
        "display_name": display_name,
        "provider": current.provider,
        "auth_type": current.auth_type,
        "credentials_ref": credentials_ref,
        "provider_account_info": provider_account_info,
        "mcp_config": mcp_config,
        "webhook_config": webhook_config,
        "metadata": metadata,
    }
    await validate_definition(ctx, definition)

    try:
        row = await pool.fetchrow(
            f"""
            UPDATE integrations
            SET display_name = $2,
                credentials_ref = $3,
                provider_account_info = $4::jsonb,
                mcp_config = $5::jsonb,
                webhook_config = $6::jsonb,
                allowed_tools = $7,
                tools_namespace = $8,
                metadata = $9::jsonb,
                status = $10,
                updated_at = now()
            WHERE id = $1 AND tenant_id = $11
            RETURNING {INTEGRATION_COLUMNS}
            """,
            integration_id,
            display_name,
            credentials_ref,
            json.dumps(provider_account_info),
            json.dumps(mcp_config),
            json.dumps(webhook_config),
            allowed_tools,
            tools_namespace,
            json.dumps(metadata),
            status,
            ctx.tenant.id,
        )
    except asyncpg.UniqueViolationError as exc:
        raise HTTPException(status_code=409, detail=_unique_violation_detail(exc)) from exc
    updated = Integration.from_row(row)
    await sync_integration_triggers(ctx, integration=updated, was_active=current.status == "active")
    return updated.to_response()


async def delete_integration(integration_id: UUID, ctx: Context) -> OkResponse:
    integration = await _require_integration(ctx, integration_id)
    pool = await ctx.tenant_pool()

    if integration.provider == "telegram":
        secrets = await load_secrets(ctx.tenant)
        token = resolve_secretish(integration.credentials_ref, secrets)
        if token:
            await delete_bot_webhook(token)

    if integration.provider == "whatsapp":
        await disconnect_number(ctx.tenant, integration)

    credential = None
    if integration.provider in OAUTH_PROVIDER_SET:
        try:
            credential = await load_oauth_credential(
                ctx.tenant,
                integration_id=str(integration_id),
                provider=integration.provider,
            )
        except Exception:
            credential = None

    deleted = await pool.fetchrow(
        "DELETE FROM integrations WHERE id = $1 AND tenant_id = $2 RETURNING id",
        integration_id,
        ctx.tenant.id,
    )
    if not deleted:
        raise HTTPException(status_code=404, detail="integration not found")

    if credential:
        await revoke_provider_tokens(
            provider=credential.provider,
            access_token=credential.access_token,
            refresh_token=credential.refresh_token,
            oauth_client_id=credential.oauth_client_id,
            oauth_metadata=credential.oauth_metadata,
        )
    return OkResponse()


async def load_active_mcp_integration(
    integration_id: UUID,
    ctx: Context,
) -> tuple[Integration, dict[str, str]]:
    """Load an active MCP-capable integration + secrets for tool discovery.

    Session-time MCP discovery (list tools) lives in ``compiler.integrations``;
    the API route composes this load with that compiler helper.
    """
    integration = await _require_integration(ctx, integration_id)
    if integration.provider not in TOOL_PROVIDER_SET:
        raise HTTPException(
            status_code=400,
            detail="this integration does not expose MCP tools",
        )
    if integration.status != "active":
        raise HTTPException(
            status_code=400,
            detail="activate or reconnect this integration before listing tools",
        )
    secrets = await load_secrets(ctx.tenant)
    return integration, secrets
