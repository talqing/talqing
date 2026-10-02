"""Integration triggers — runtime lookup, validation + CRUD, adapter lifecycle.

Internal code uses ``IntegrationTrigger``; public APIs return
``IntegrationTriggerResponse``.
"""

from __future__ import annotations

import json
import logging
from uuid import UUID

from fastapi import HTTPException

import db
from api.core.schemas import OkResponse, Page, coalesce, page_slice
from services.agents import (
    ConversationContext,
    ConversationSpec,
    VarDeclaration,
    missing_required_vars,
)
from services.user import Context, Tenant

from .catalog import IMPLEMENTED_TRIGGER_TYPES, agent_channel_for_trigger
from .channel import get_channel_adapter
from .definitions import provider_supports_trigger
from .models import (
    INTEGRATION_COLUMNS,
    INTEGRATION_TRIGGER_COLUMNS,
    CreateIntegrationTriggerRequest,
    Integration,
    IntegrationTrigger,
    IntegrationTriggerResponse,
    PatchIntegrationTriggerRequest,
    TriggerStatus,
)

logger = logging.getLogger("talqing.api.integration_triggers")


async def get_active_trigger(
    tenant: Tenant,
    integration_id: UUID,
    trigger_type: str,
) -> IntegrationTrigger | None:
    """Return the enabled, active trigger for this integration and type, if any.

    Used by inbound channel handlers (Telegram messages) to resolve which agent
    should handle a provider event.
    """
    pool = await db.tenant_pool(tenant)
    row = await pool.fetchrow(
        f"""
        SELECT {INTEGRATION_TRIGGER_COLUMNS}
        FROM integration_triggers
        WHERE tenant_id = $1
            AND integration_id = $2
            AND trigger_type = $3
            AND enabled = true
            AND status = 'active'
        ORDER BY updated_at DESC
        LIMIT 1
        """,
        tenant.id,
        integration_id,
        trigger_type,
    )
    return IntegrationTrigger.from_row(row) if row else None


async def trigger_conversation_context(conn, tenant: Tenant, agent_id: UUID) -> ConversationContext:
    """The trigger agent's past-conversation setting, from its published version.

    A frozen config always carries `conversation` — `AgentConfig` gives it a
    default and dumps every field — so a null here means either the agent is
    unpublished (the caller fails a few lines later anyway) or the version was
    written by something that is not `AgentConfig`. Neither is a state to invent
    defaults for.
    """
    config = await conn.fetchval(
        """
        SELECT av.config->'conversation'
        FROM agents a
        JOIN agent_versions av
            ON av.agent_id = a.id
            AND av.version = a.published_version
            AND av.tenant_id = a.tenant_id
        WHERE a.id = $1 AND a.tenant_id = $2
        """,
        agent_id,
        tenant.id,
    )
    if config is None:
        raise RuntimeError(f"agent {agent_id} has no published conversation settings")
    return ConversationSpec.model_validate(config).context


async def _require_integration(pool, tenant_id: UUID, integration_id: UUID) -> Integration:
    row = await pool.fetchrow(
        f"""
        SELECT {INTEGRATION_COLUMNS}
        FROM integrations
        WHERE id = $1 AND tenant_id = $2
        """,
        integration_id,
        tenant_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="integration not found")
    return Integration.from_row(row)


async def _validate_trigger(
    pool,
    *,
    tenant: Tenant,
    integration: Integration,
    trigger_type: str,
    agent_id: UUID | None,
    enabled: bool,
) -> TriggerStatus:
    if trigger_type not in IMPLEMENTED_TRIGGER_TYPES:
        raise HTTPException(
            status_code=400, detail=f"trigger {trigger_type} is not implemented yet"
        )
    if not provider_supports_trigger(integration, trigger_type):
        raise HTTPException(
            status_code=400,
            detail=(
                f"integration provider {integration.provider} does not support "
                f"trigger {trigger_type}"
            ),
        )
    if (
        trigger_type == "whatsapp.call.inbound"
        and integration.provider_account_info.get("bsp") != "twilio"
    ):
        raise HTTPException(
            status_code=400, detail="WhatsApp calls work only on numbers connected through Twilio"
        )
    if enabled and integration.status != "active":
        raise HTTPException(
            status_code=400,
            detail="activate or reconnect this integration before enabling a trigger",
        )
    if agent_id is None:
        if enabled:
            raise HTTPException(
                status_code=400, detail="select an agent before enabling this trigger"
            )
        return "needs_setup"
    # TODO: don't query agents directly, expose a function in services/agents/
    agent = await pool.fetchrow(
        """
        SELECT a.id, a.published_version, av.config
        FROM agents a
        LEFT JOIN agent_versions av
            ON av.agent_id = a.id
            AND av.version = a.published_version
            AND av.tenant_id = a.tenant_id
        WHERE a.id = $1 AND a.tenant_id = $2
        """,
        agent_id,
        tenant.id,
    )
    if not agent:
        raise HTTPException(status_code=404, detail="agent not found")
    if not agent["published_version"]:
        if enabled:
            raise HTTPException(
                status_code=400, detail="publish the agent before enabling this trigger"
            )
        return "needs_setup"
    # Triggers run the published snapshot — channel/hooks come from that freeze.
    config = agent["config"]
    if not isinstance(config, dict):
        raise HTTPException(status_code=400, detail="published agent version is missing config")
    channel = config["channel"]
    required_channel = agent_channel_for_trigger(trigger_type)
    if required_channel is None:
        raise HTTPException(
            status_code=400, detail=f"trigger {trigger_type} is not implemented yet"
        )
    if channel != required_channel:
        raise HTTPException(
            status_code=400,
            detail=f"{trigger_type} requires a published {required_channel} agent",
        )
    if trigger_type == "whatsapp.call.inbound":
        declared = [VarDeclaration.model_validate(v) for v in config["vars"]]
        missing = missing_required_vars(declared, {})
        if missing:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"'{missing[0]}' is required and has no default, and a WhatsApp call "
                    "carries no request to supply it - give it a default or make it optional"
                ),
            )
    return "active" if enabled else "disabled"


async def _ensure_no_other_enabled_trigger(
    conn,
    *,
    tenant_id: UUID,
    integration_id: UUID,
    trigger_type: str,
    trigger_id: UUID | None = None,
) -> None:
    existing = await conn.fetchrow(
        """
        SELECT id
        FROM integration_triggers
        WHERE tenant_id = $1
            AND integration_id = $2
            AND trigger_type = $3
            AND enabled = true
            AND ($4::uuid IS NULL OR id <> $4)
        LIMIT 1
        """,
        tenant_id,
        integration_id,
        trigger_type,
        trigger_id,
    )
    if existing:
        raise HTTPException(
            status_code=400,
            detail="this integration already has an enabled trigger of that type",
        )


async def _maybe_enable_adapter(
    ctx: Context,
    *,
    integration: Integration,
    trigger: IntegrationTrigger,
) -> IntegrationTrigger:
    """Run managed subscription when a trigger is active+enabled."""
    if not (trigger.enabled and trigger.status == "active"):
        return trigger
    adapter = get_channel_adapter(integration.provider)
    if adapter is None:
        return trigger
    try:
        updated = await adapter.on_trigger_enabled(
            ctx,
            integration=integration,
            trigger=trigger,
        )
    except HTTPException:
        # The provider refused, so the trigger is off: left enabled it would
        # read as on while answering nothing, and every re-activation of the
        # integration would try it again. `error` says why until the next save.
        pool = await ctx.tenant_pool()
        await pool.execute(
            """
            UPDATE integration_triggers
            SET enabled = false, status = 'error', updated_at = now()
            WHERE id = $1 AND tenant_id = $2
            """,
            trigger.id,
            ctx.tenant.id,
        )
        raise
    if updated is not None:
        return updated
    return trigger


async def _maybe_disable_adapter(
    ctx: Context,
    *,
    integration: Integration,
    trigger_type: str,
    was_enabled: bool,
    now_enabled: bool,
) -> None:
    if not (was_enabled and not now_enabled):
        return
    adapter = get_channel_adapter(integration.provider)
    if adapter is None:
        return
    await adapter.on_trigger_disabled(
        ctx,
        integration=integration,
        trigger_type=trigger_type,
    )


async def sync_integration_triggers(
    ctx: Context, *, integration: Integration, was_active: bool
) -> None:
    """Disarm or re-arm the provider side of every enabled trigger.

    A disabled integration answers nothing, so the provider must stop sending:
    a WhatsApp call button nobody answers counts against the number, and a
    message delivered to a disabled integration is dropped for good. Every
    trigger is attempted before the first failure is raised.
    """
    now_active = integration.status == "active"
    if was_active == now_active:
        return
    adapter = get_channel_adapter(integration.provider)
    if adapter is None:
        return
    pool = await ctx.tenant_pool()
    rows = await pool.fetch(
        f"""
        SELECT {INTEGRATION_TRIGGER_COLUMNS}
        FROM integration_triggers
        WHERE integration_id = $1 AND tenant_id = $2 AND enabled
        """,
        integration.id,
        ctx.tenant.id,
    )
    failures: list[HTTPException] = []
    for row in rows:
        trigger = IntegrationTrigger.from_row(row)
        try:
            if now_active:
                await _maybe_enable_adapter(ctx, integration=integration, trigger=trigger)
            else:
                await adapter.on_trigger_disabled(
                    ctx, integration=integration, trigger_type=trigger.trigger_type
                )
        except HTTPException as exc:
            failures.append(exc)
    if failures:
        raise failures[0]


async def list_integration_triggers(
    integration_id: UUID,
    ctx: Context,
    limit: int = 200,
    offset: int = 0,
) -> Page[IntegrationTriggerResponse]:
    pool = await ctx.tenant_pool()
    await _require_integration(pool, ctx.tenant.id, integration_id)
    rows = await pool.fetch(
        f"""
        SELECT {INTEGRATION_TRIGGER_COLUMNS}
        FROM integration_triggers
        WHERE integration_id = $1 AND tenant_id = $2
        ORDER BY updated_at DESC
        LIMIT $3 OFFSET $4
        """,
        integration_id,
        ctx.tenant.id,
        limit + 1,
        offset,
    )
    return page_slice(
        [IntegrationTrigger.from_row(row).to_response() for row in rows],
        limit=limit,
        offset=offset,
    )


async def create_integration_trigger(
    integration_id: UUID,
    body: CreateIntegrationTriggerRequest,
    ctx: Context,
) -> IntegrationTriggerResponse:
    pool = await ctx.tenant_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            integration = await _require_integration(conn, ctx.tenant.id, integration_id)
            status = await _validate_trigger(
                conn,
                tenant=ctx.tenant,
                integration=integration,
                trigger_type=body.trigger_type,
                agent_id=body.agent_id,
                enabled=body.enabled,
            )
            if body.enabled:
                await _ensure_no_other_enabled_trigger(
                    conn,
                    tenant_id=ctx.tenant.id,
                    integration_id=integration_id,
                    trigger_type=body.trigger_type,
                )
            row = await conn.fetchrow(
                f"""
                INSERT INTO integration_triggers (
                    tenant_id, integration_id, trigger_type, agent_id, enabled, status,
                    reply_mode, provider_subscription_ref, metadata
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb)
                RETURNING {INTEGRATION_TRIGGER_COLUMNS}
                """,
                ctx.tenant.id,
                integration_id,
                body.trigger_type,
                body.agent_id,
                body.enabled,
                status,
                body.reply_mode,
                body.provider_subscription_ref,
                json.dumps(body.metadata),
            )
    trigger = await _maybe_enable_adapter(
        ctx,
        integration=integration,
        trigger=IntegrationTrigger.from_row(row),
    )
    return trigger.to_response()


async def patch_integration_trigger(
    integration_id: UUID,
    trigger_id: UUID,
    body: PatchIntegrationTriggerRequest,
    ctx: Context,
) -> IntegrationTriggerResponse:
    pool = await ctx.tenant_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            integration = await _require_integration(conn, ctx.tenant.id, integration_id)
            current_row = await conn.fetchrow(
                f"""
                SELECT {INTEGRATION_TRIGGER_COLUMNS}
                FROM integration_triggers
                WHERE id = $1 AND integration_id = $2 AND tenant_id = $3
                FOR UPDATE
                """,
                trigger_id,
                integration_id,
                ctx.tenant.id,
            )
            if not current_row:
                raise HTTPException(status_code=404, detail="integration trigger not found")
            current = IntegrationTrigger.from_row(current_row)

            trigger_type = coalesce(body.trigger_type, current.trigger_type)
            agent_id = coalesce(body.agent_id, current.agent_id)
            enabled = coalesce(body.enabled, current.enabled)
            computed_status = await _validate_trigger(
                conn,
                tenant=ctx.tenant,
                integration=integration,
                trigger_type=trigger_type,
                agent_id=agent_id,
                enabled=enabled,
            )
            requested_status = body.status
            status = requested_status if requested_status in {"error"} else computed_status
            if requested_status == "disabled" and not enabled:
                status = "disabled"
            if enabled:
                await _ensure_no_other_enabled_trigger(
                    conn,
                    tenant_id=ctx.tenant.id,
                    integration_id=integration_id,
                    trigger_type=trigger_type,
                    trigger_id=trigger_id,
                )

            row = await conn.fetchrow(
                f"""
                UPDATE integration_triggers
                SET trigger_type = $4,
                    agent_id = $5,
                    enabled = $6,
                    status = $7,
                    reply_mode = $8,
                    provider_subscription_ref = $9,
                    metadata = $10::jsonb,
                    updated_at = now()
                WHERE id = $1 AND integration_id = $2 AND tenant_id = $3
                RETURNING {INTEGRATION_TRIGGER_COLUMNS}
                """,
                trigger_id,
                integration_id,
                ctx.tenant.id,
                trigger_type,
                agent_id,
                enabled,
                status,
                coalesce(body.reply_mode, current.reply_mode),
                coalesce(body.provider_subscription_ref, current.provider_subscription_ref),
                json.dumps(coalesce(body.metadata, current.metadata)),
            )
    updated = IntegrationTrigger.from_row(row)
    await _maybe_disable_adapter(
        ctx,
        integration=integration,
        trigger_type=updated.trigger_type,
        was_enabled=current.enabled,
        now_enabled=updated.enabled,
    )
    trigger = await _maybe_enable_adapter(
        ctx,
        integration=integration,
        trigger=updated,
    )
    return trigger.to_response()


async def delete_integration_trigger(
    integration_id: UUID,
    trigger_id: UUID,
    ctx: Context,
) -> OkResponse:
    pool = await ctx.tenant_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            integration = await _require_integration(conn, ctx.tenant.id, integration_id)
            row = await conn.fetchrow(
                f"""
                DELETE FROM integration_triggers
                WHERE id = $1 AND integration_id = $2 AND tenant_id = $3
                RETURNING {INTEGRATION_TRIGGER_COLUMNS}
                """,
                trigger_id,
                integration_id,
                ctx.tenant.id,
            )
    if not row:
        raise HTTPException(status_code=404, detail="integration trigger not found")
    deleted = IntegrationTrigger.from_row(row)
    await _maybe_disable_adapter(
        ctx,
        integration=integration,
        trigger_type=deleted.trigger_type,
        was_enabled=deleted.enabled,
        now_enabled=False,
    )
    return OkResponse()
