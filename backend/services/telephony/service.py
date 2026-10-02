"""Telephony accounts, phone numbers, and outbound call orchestration."""

from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID

import asyncpg
from fastapi import HTTPException
from livekit.api import ServerError as LiveKitServerError

from api.core.schemas import ErrorBody, OkResponse, Page, page_slice, validation_error
from services import credits, webhooks
from services.agents import VarDeclaration, missing_required_vars
from services.agents.plan import resolve_call_plan
from services.secrets import (
    MissingSecretError,
    create_secret,
    list_secret_names,
    load_secrets,
    materialize_secret_input,
    parse_secret_ref,
    resolve_secretish,
)
from services.secrets.service import CreateSecretRequest
from services.user import Context
from services.webhooks import events
from settings import get_settings
from utils.bg import spawn

from . import dial, livekit_sip
from .catalog import get_provider_spec, list_provider_specs
from .e164 import normalize_e164
from .models import (
    PHONE_NUMBER_COLUMNS,
    TELEPHONY_ACCOUNT_COLUMNS,
    AssignPhoneNumberRequest,
    CreateTelephonyAccountRequest,
    ImportedNumberResult,
    ImportPhoneNumbersRequest,
    ImportPhoneNumbersResponse,
    OutboundCallRequest,
    OutboundCallResponse,
    PatchPhoneNumberRequest,
    PatchTelephonyAccountRequest,
    PhoneNumber,
    PhoneNumberResponse,
    RemoteNumber,
    TelephonyAccount,
    TelephonyAccountResponse,
    TelephonyProviderSpec,
)
from .providers import (
    ProviderError,
    exotel_inbound_console_ready,
    get_adapter,
    set_exotel_console_setup,
)

logger = logging.getLogger("talqing.telephony")


def _account_response(account: TelephonyAccount) -> TelephonyAccountResponse:
    """Serialize an account with its carrier setup steps resolved.

    Lives here rather than on the model because the steps come from the
    adapter, and ``models`` must not import ``providers`` — providers already
    import the models.
    """
    return TelephonyAccountResponse.from_account(
        account,
        setup_steps=get_adapter(account.provider).setup_steps(account),
        supports_refer=get_provider_spec(account.provider).supports_refer,
    )


def _validate_credentials_map(
    credentials: dict[str, Any],
    *,
    required_keys: list[str] | tuple[str, ...],
    secret_names: set[str],
) -> list[str]:
    """Validate post-materialization credentials (must be existing secret refs)."""
    errors: list[str] = []
    for key in required_keys:
        raw = credentials.get(key)
        if not isinstance(raw, str) or not raw.strip():
            errors.append(f"credentials.{key} is required")
            continue
        name = parse_secret_ref(raw)
        if name is None:
            errors.append(f"credentials.{key} must be a secret reference like {{{{secrets.NAME}}}}")
            continue
        if name not in secret_names:
            errors.append(f"secret '{name}' does not exist")
    for key, raw in credentials.items():
        if key in required_keys:
            continue
        if raw is None or raw == "":
            continue
        name = parse_secret_ref(raw) if isinstance(raw, str) else None
        if name is None:
            errors.append(f"credentials.{key} must be a secret reference")
        elif name not in secret_names:
            errors.append(f"secret '{name}' does not exist")
    return errors


async def _materialize_credentials(
    ctx: Context,
    *,
    provider: str,
    credentials: dict[str, Any],
) -> dict[str, Any]:
    """Convert plaintext credential values into ``{{secrets.NAME}}`` refs."""
    existing_names = set(await list_secret_names(ctx.tenant))
    out: dict[str, Any] = {}
    for key, raw in credentials.items():
        if not isinstance(raw, str) or not raw.strip():
            continue
        preferred = f"{provider.upper()}_{key.upper()}"
        out[key] = await materialize_secret_input(
            ctx,
            raw,
            preferred_name=preferred,
            existing_names=existing_names,
        )
    return out


async def catalog(limit: int = 200, offset: int = 0) -> Page[TelephonyProviderSpec]:
    items = list_provider_specs()
    return page_slice(items[offset : offset + limit + 1], limit=limit, offset=offset)


async def list_accounts(
    ctx: Context, limit: int = 200, offset: int = 0
) -> Page[TelephonyAccountResponse]:
    pool = await ctx.tenant_pool()
    rows = await pool.fetch(
        f"""
        SELECT {TELEPHONY_ACCOUNT_COLUMNS}
        FROM telephony_accounts
        WHERE tenant_id = $1
        ORDER BY created_at DESC
        LIMIT $2 OFFSET $3
        """,
        ctx.tenant.id,
        limit + 1,
        offset,
    )
    return page_slice(
        [_account_response(TelephonyAccount.from_row(r)) for r in rows],
        limit=limit,
        offset=offset,
    )


async def get_account(ctx: Context, account_id: UUID) -> TelephonyAccountResponse:
    account = await _require_account(ctx, account_id)
    return _account_response(account)


async def create_account(
    body: CreateTelephonyAccountRequest, ctx: Context
) -> TelephonyAccountResponse:
    try:
        spec = get_provider_spec(body.provider)
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not spec.implemented:
        raise HTTPException(
            status_code=400,
            detail=f"{spec.label} telephony is not implemented yet",
        )

    account_info = dict(body.account_info)
    credentials = await _materialize_credentials(
        ctx, provider=body.provider, credentials=dict(body.credentials)
    )
    secret_names = await list_secret_names(ctx.tenant)
    errors = _validate_credentials_map(
        credentials,
        required_keys=spec.credential_keys,
        secret_names=secret_names,
    )
    for key in spec.account_info_keys:
        val = account_info.get(key)
        if not isinstance(val, str) or not val.strip():
            errors.append(f"account_info.{key} is required")
    if errors:
        raise validation_error(errors, "invalid telephony account")

    pool = await ctx.tenant_pool()
    try:
        row = await pool.fetchrow(
            f"""
            INSERT INTO telephony_accounts (
                tenant_id, provider, display_name, account_info, credentials,
                sip_uri, status
            )
            VALUES ($1, $2, $3, $4::jsonb, $5::jsonb, $6, 'pending')
            RETURNING {TELEPHONY_ACCOUNT_COLUMNS}
            """,
            ctx.tenant.id,
            body.provider,
            body.display_name,
            json.dumps(account_info),
            json.dumps(credentials),
            livekit_sip.assign_sip_uri(),
        )
    except asyncpg.UniqueViolationError as exc:
        raise HTTPException(
            status_code=409,
            detail=_unique_account_detail(exc),
        ) from exc
    assert row is not None
    account = TelephonyAccount.from_row(row)

    # Validate credentials immediately
    try:
        secrets = await load_secrets(ctx.tenant)
        adapter = get_adapter(account.provider)
        await adapter.validate_credentials(account, secrets)
        account = await _set_account_status(ctx, account.id, "connected", None)
    except ProviderError as exc:
        logger.exception(
            "credential validation failed for %s account %s", body.provider, account.id
        )
        account = await _set_account_status(ctx, account.id, "error", exc.message)

    return _account_response(account)


async def patch_account(
    account_id: UUID, body: PatchTelephonyAccountRequest, ctx: Context
) -> TelephonyAccountResponse:
    account = await _require_account(ctx, account_id)
    display_name = body.display_name if body.display_name is not None else account.display_name
    account_info = (
        dict(body.account_info) if body.account_info is not None else dict(account.account_info)
    )
    if body.credentials is not None:
        # Merge so a partial credentials patch can rotate one key without
        # dropping the others (e.g. Exotel api_key + api_token).
        merged = dict(account.credentials)
        materialized = await _materialize_credentials(
            ctx, provider=account.provider, credentials=dict(body.credentials)
        )
        merged.update(materialized)
        credentials = merged
        spec = get_provider_spec(account.provider)
        secret_names = await list_secret_names(ctx.tenant)
        errors = _validate_credentials_map(
            credentials,
            required_keys=spec.credential_keys,
            secret_names=secret_names,
        )
        if errors:
            raise validation_error(errors, "invalid credentials")
    else:
        credentials = dict(account.credentials)

    provider_state = dict(account.provider_state)
    if body.console_setup_confirmed is not None:
        if account.provider != "exotel":
            raise HTTPException(
                status_code=400,
                detail=(
                    f"{account.provider} needs no carrier-console setup, "
                    "so there is nothing to confirm"
                ),
            )
        provider_state = set_exotel_console_setup(provider_state, body.console_setup_confirmed)

    status = account.status
    status_message = account.status_message
    disabling = body.status == "disabled" and account.status != "disabled"
    if body.status == "disabled":
        status = "disabled"
        status_message = None
    elif body.status == "connected" and account.status == "disabled":
        status = "connected"

    # Tear down inbound LiveKit routing before flipping the account to disabled so
    # PSTN cannot keep ringing after the dashboard shows the carrier as off.
    if disabling:
        await _teardown_account_inbound_routing(ctx, account_id)

    pool = await ctx.tenant_pool()
    try:
        row = await pool.fetchrow(
            f"""
            UPDATE telephony_accounts
            SET display_name = $3,
                account_info = $4::jsonb,
                credentials = $5::jsonb,
                provider_state = $6::jsonb,
                status = $7,
                status_message = $8,
                updated_at = now()
            WHERE id = $1 AND tenant_id = $2
            RETURNING {TELEPHONY_ACCOUNT_COLUMNS}
            """,
            account_id,
            ctx.tenant.id,
            display_name,
            json.dumps(account_info),
            json.dumps(credentials),
            json.dumps(provider_state),
            status,
            status_message,
        )
    except asyncpg.UniqueViolationError as exc:
        raise HTTPException(status_code=409, detail=_unique_account_detail(exc)) from exc
    if not row:
        raise HTTPException(status_code=404, detail="telephony account not found")

    updated = TelephonyAccount.from_row(row)
    if body.console_setup_confirmed is not None and updated.provider == "exotel":
        await _sync_exotel_number_inbound_capability(ctx, updated)

    return _account_response(updated)


async def delete_account(account_id: UUID, ctx: Context) -> OkResponse:
    pool = await ctx.tenant_pool()
    active = await pool.fetchval(
        """
        SELECT COUNT(*) FROM phone_numbers
        WHERE tenant_id = $1 AND telephony_account_id = $2
          AND status IN ('pending', 'provisioning', 'active', 'error')
        """,
        ctx.tenant.id,
        account_id,
    )
    if active and int(active) > 0:
        raise HTTPException(
            status_code=400,
            detail="disable all phone numbers on this account before deleting it",
        )

    # Delete the carrier-side objects we created before dropping the row that
    # records their ids. Doing it the other way round would strand a working
    # SIP credential on the tenant's account with nothing left pointing at it.
    account = await _require_account(ctx, account_id)
    secrets = await load_secrets(ctx.tenant)
    try:
        await get_adapter(account.provider).teardown_account(account, secrets)
    except ProviderError as exc:
        logger.exception("carrier teardown failed for account %s", account_id)
        raise HTTPException(
            status_code=502,
            detail=(
                f"failed to remove {account.provider} SIP objects: {exc.message}. "
                "Fix the provider credentials and retry the delete."
            ),
        ) from exc

    result = await pool.execute(
        "DELETE FROM telephony_accounts WHERE id = $1 AND tenant_id = $2",
        account_id,
        ctx.tenant.id,
    )
    if result == "DELETE 0":
        raise HTTPException(status_code=404, detail="telephony account not found")
    return OkResponse()


async def provision_account(account_id: UUID, ctx: Context) -> TelephonyAccountResponse:
    account = await _require_account(ctx, account_id)
    if account.status == "disabled":
        raise HTTPException(status_code=400, detail="telephony account is disabled")
    await _set_account_status(ctx, account_id, "provisioning", None)
    try:
        secrets = await load_secrets(ctx.tenant)
        adapter = get_adapter(account.provider)
        platform_uri = livekit_sip.platform_sip_uri_for_provider(account.sip_uri)
        new_state, generated_sip_password = await adapter.ensure_account_sip(
            account,
            secrets,
            platform_sip_uri=platform_uri,
        )
        credentials = dict(account.credentials)
        if generated_sip_password:
            secret_name = f"telephony_sip_out_{account.provider}_{account.id.hex[:6]}"
            await create_secret(
                CreateSecretRequest(name=secret_name, value=generated_sip_password), ctx
            )
            credentials["sip_outbound_password"] = f"{{{{secrets.{secret_name}}}}}"
            secrets = await load_secrets(ctx.tenant)
        if adapter.outbound_sip_configured(new_state) and not _sip_password_resolves(
            credentials, secrets
        ):
            raise ProviderError(
                "Outbound SIP password secret is missing. "
                "Disable all phone numbers on this account, delete the account, "
                "then reconnect and provision again for a fresh setup."
            )

        pool = await ctx.tenant_pool()
        row = await pool.fetchrow(
            f"""
            UPDATE telephony_accounts
            SET provider_state = $3::jsonb,
                credentials = $4::jsonb,
                status = 'ready',
                status_message = NULL,
                last_synced_at = now(),
                updated_at = now()
            WHERE id = $1 AND tenant_id = $2
            RETURNING {TELEPHONY_ACCOUNT_COLUMNS}
            """,
            account_id,
            ctx.tenant.id,
            json.dumps(new_state),
            json.dumps(credentials),
        )
        assert row is not None
        updated = TelephonyAccount.from_row(row)
        # Re-provision may fix edge host / checklist notes — refresh number inbound flags.
        if updated.provider == "exotel":
            await _sync_exotel_number_inbound_capability(ctx, updated)
        return _account_response(updated)
    except (ProviderError, RuntimeError) as exc:
        msg = exc.message if isinstance(exc, ProviderError) else str(exc)
        account = await _set_account_status(ctx, account_id, "error", msg)
        raise HTTPException(status_code=400, detail=msg) from exc


async def list_remote_numbers(
    account_id: UUID,
    ctx: Context,
    *,
    limit: int = 200,
    offset: int = 0,
) -> Page[RemoteNumber]:
    account = await _require_account(ctx, account_id)
    try:
        secrets = await load_secrets(ctx.tenant)
        adapter = get_adapter(account.provider)
        remote = await adapter.list_remote_numbers(account, secrets)
    except ProviderError as exc:
        logger.exception("listing %s numbers failed for account %s", account.provider, account_id)
        raise HTTPException(status_code=400, detail=exc.message) from exc

    pool = await ctx.tenant_pool()
    imported = {
        r["e164"]
        for r in await pool.fetch(
            """
            SELECT e164 FROM phone_numbers
            WHERE tenant_id = $1 AND telephony_account_id = $2
              AND status <> 'disabled'
            """,
            ctx.tenant.id,
            account_id,
        )
    }
    items = [
        RemoteNumber(
            e164=n.e164,
            provider_number_id=n.provider_number_id,
            label=n.label,
            already_imported=n.e164 in imported,
        )
        for n in remote
    ]
    return page_slice(items[offset : offset + limit + 1], limit=limit, offset=offset)


async def import_numbers(
    body: ImportPhoneNumbersRequest, ctx: Context
) -> ImportPhoneNumbersResponse:
    account = await _require_account(ctx, body.telephony_account_id)
    # Only DIDs the tenant already owns on this carrier account may be imported.
    try:
        secrets = await load_secrets(ctx.tenant)
        adapter = get_adapter(account.provider)
        remote_items = await adapter.list_remote_numbers(account, secrets)
    except ProviderError as exc:
        logger.exception(
            "listing %s numbers failed for import on account %s", account.provider, account.id
        )
        raise HTTPException(
            status_code=400,
            detail=(
                f"could not list numbers from {account.provider}: {exc.message}. "
                "Fix carrier credentials/API access and retry."
            ),
        ) from exc

    remote_by_e164: dict[str, RemoteNumber] = {
        item.e164: RemoteNumber(
            e164=item.e164,
            provider_number_id=item.provider_number_id,
            label=item.label,
        )
        for item in remote_items
    }

    # Provision the account once up front rather than letting the first number
    # trigger it: a bad credential would otherwise be reported N times, once per
    # number, with the real cause buried in the repetition.
    if body.provision and account.status == "connected":
        await provision_account(account.id, ctx)
        account = await _require_account(ctx, account.id)
    if body.provision and account.status != "ready":
        raise HTTPException(
            status_code=400,
            detail="this carrier account is not set up yet; finish its setup first",
        )

    # Exotel inbound needs console steps — start with can_inbound=false until confirmed.
    can_inbound = True
    if account.provider == "exotel":
        can_inbound = exotel_inbound_console_ready(account.provider_state)

    pool = await ctx.tenant_pool()
    results: list[ImportedNumberResult] = []
    for e164 in body.e164s:
        remote = remote_by_e164.get(e164)
        if remote is None:
            results.append(
                ImportedNumberResult(
                    e164=e164,
                    ok=False,
                    error=(f"not found on this {account.provider} account"),
                )
            )
            continue
        try:
            row = await pool.fetchrow(
                f"""
                INSERT INTO phone_numbers (
                    tenant_id, telephony_account_id, e164, provider,
                    provider_number_id, label, can_inbound, status, created_by
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, 'pending', $8)
                RETURNING {PHONE_NUMBER_COLUMNS}
                """,
                ctx.tenant.id,
                account.id,
                e164,
                account.provider,
                remote.provider_number_id,
                remote.label,
                can_inbound,
                ctx.user.id,
            )
        except asyncpg.UniqueViolationError:
            results.append(
                ImportedNumberResult(
                    e164=e164,
                    ok=False,
                    error="already registered on this platform",
                )
            )
            continue
        assert row is not None
        number = PhoneNumber.from_row(row)
        if not body.provision:
            results.append(
                ImportedNumberResult(
                    e164=e164, ok=True, phone_number=PhoneNumberResponse.from_number(number)
                )
            )
            continue
        try:
            provisioned = await provision_number(number.id, ctx)
        except HTTPException as exc:
            # provision_number has already written status='error' with this same
            # message, so return the row too — the number exists and is retryable.
            results.append(
                ImportedNumberResult(
                    e164=e164,
                    ok=False,
                    phone_number=await get_number(ctx, number.id),
                    error=str(exc.detail),
                )
            )
            continue
        results.append(ImportedNumberResult(e164=e164, ok=True, phone_number=provisioned))
    return ImportPhoneNumbersResponse(items=results)


async def list_numbers(
    ctx: Context,
    *,
    account_id: UUID | None = None,
    limit: int = 200,
    offset: int = 0,
) -> Page[PhoneNumberResponse]:
    pool = await ctx.tenant_pool()
    rows = await pool.fetch(
        f"""
        SELECT {PHONE_NUMBER_COLUMNS}
        FROM phone_numbers
        WHERE tenant_id = $1
          AND ($2::uuid IS NULL OR telephony_account_id = $2::uuid)
        ORDER BY created_at DESC
        LIMIT $3 OFFSET $4
        """,
        ctx.tenant.id,
        account_id,
        limit + 1,
        offset,
    )
    return page_slice(
        [PhoneNumberResponse.from_number(PhoneNumber.from_row(r)) for r in rows],
        limit=limit,
        offset=offset,
    )


async def get_number(ctx: Context, number_id: UUID) -> PhoneNumberResponse:
    number = await _require_number(ctx, number_id)
    return PhoneNumberResponse.from_number(number)


async def patch_number(
    number_id: UUID, body: PatchPhoneNumberRequest, ctx: Context
) -> PhoneNumberResponse:
    number = await _require_number(ctx, number_id)
    pool = await ctx.tenant_pool()

    # Disable: unmap at the carrier, then tear down LiveKit objects, then write
    # the row. Carrier first so the DID stops arriving rather than arriving at a
    # trunk that no longer exists; LiveKit before the row so uniqueness cannot
    # free the E.164 while an orphan trunk still accepts the DID.
    #
    # Both paths below keep ``inbound_agent_id``: a disabled number is out of
    # service, not unassigned, and re-enabling it should put the same agent back
    # on the line. The LiveKit rule is what actually dispatches, and that is
    # dropped either way, so a retained id can never leave a disabled number
    # answering.
    if body.status == "disabled":
        account = await _require_account(ctx, number.telephony_account_id)
        number_state = dict(number.provider_state)
        unbind_message: str | None = None
        try:
            secrets = await load_secrets(ctx.tenant)
            number_state = await get_adapter(account.provider).unbind_number(
                account,
                secrets,
                e164=number.e164,
                number_provider_state=number_state,
            )
        except (ProviderError, MissingSecretError) as exc:
            # Not fatal: the LiveKit teardown below is what stops us answering,
            # and blocking disable on a carrier API would leave the number live
            # on both sides. Record it so the row cannot claim a clean release.
            number_state = dict(number.provider_state)
            unbind_message = (
                f"Disabled here, but {account.provider} may still be routing this "
                f"number to us — unmap it in the {account.provider} console. ({exc})"
            )
            logger.warning("carrier unbind failed while disabling %s: %s", number.e164, exc)
        try:
            await livekit_sip.teardown_number_livekit(
                dispatch_rule_id=number.livekit_dispatch_rule_id,
                inbound_trunk_id=number.livekit_inbound_trunk_id,
            )
        except Exception as exc:
            logger.exception("LiveKit teardown failed while disabling %s", number.e164)
            raise HTTPException(
                status_code=502,
                detail=f"failed to remove LiveKit SIP objects: {exc}",
            ) from exc
        row = await pool.fetchrow(
            f"""
            UPDATE phone_numbers
            SET label = COALESCE($3, label),
                can_inbound = COALESCE($4, can_inbound),
                can_outbound = COALESCE($5, can_outbound),
                status = 'disabled',
                status_message = $6,
                provider_state = $7::jsonb,
                livekit_dispatch_rule_id = NULL,
                livekit_inbound_trunk_id = NULL,
                updated_at = now()
            WHERE id = $1 AND tenant_id = $2
            RETURNING {PHONE_NUMBER_COLUMNS}
            """,
            number_id,
            ctx.tenant.id,
            body.label,
            body.can_inbound,
            body.can_outbound,
            unbind_message,
            json.dumps(number_state),
        )
        if not row:
            raise HTTPException(status_code=404, detail="phone number not found")
        return PhoneNumberResponse.from_number(PhoneNumber.from_row(row))

    # Re-open a disabled number for re-provision (frees uniqueness for this row).
    if body.status == "pending":
        if number.status not in ("disabled", "error", "pending"):
            raise HTTPException(
                status_code=400,
                detail="only disabled or error numbers can be reset to pending for re-provision",
            )
        # Ensure no other live row holds this E.164 for this tenant.
        # (Platform-wide uniqueness is still enforced by uq_phone_numbers_e164_live.)
        conflict = await pool.fetchval(
            """
            SELECT id FROM phone_numbers
            WHERE tenant_id = $1 AND e164 = $2 AND id <> $3
              AND status IN ('pending', 'provisioning', 'active', 'error')
            LIMIT 1
            """,
            ctx.tenant.id,
            number.e164,
            number_id,
        )
        if conflict:
            raise HTTPException(
                status_code=409,
                detail=f"{number.e164} is already registered on another live number row",
            )
        account = await _require_account(ctx, number.telephony_account_id)
        can_inbound = body.can_inbound
        if can_inbound is None and account.provider == "exotel":
            can_inbound = exotel_inbound_console_ready(account.provider_state)
        elif can_inbound is None:
            can_inbound = number.can_inbound
        try:
            row = await pool.fetchrow(
                f"""
                UPDATE phone_numbers
                SET label = COALESCE($3, label),
                    can_inbound = $4,
                    can_outbound = COALESCE($5, can_outbound),
                    status = 'pending',
                    status_message = NULL,
                    livekit_dispatch_rule_id = NULL,
                    livekit_inbound_trunk_id = NULL,
                    updated_at = now()
                WHERE id = $1 AND tenant_id = $2
                RETURNING {PHONE_NUMBER_COLUMNS}
                """,
                number_id,
                ctx.tenant.id,
                body.label,
                can_inbound,
                body.can_outbound,
            )
        except asyncpg.UniqueViolationError as exc:
            raise HTTPException(
                status_code=409,
                detail=f"{number.e164} is already registered on another live number row",
            ) from exc
        if not row:
            raise HTTPException(status_code=404, detail="phone number not found")
        return PhoneNumberResponse.from_number(PhoneNumber.from_row(row))

    row = await pool.fetchrow(
        f"""
        UPDATE phone_numbers
        SET label = COALESCE($3, label),
            can_inbound = COALESCE($4, can_inbound),
            can_outbound = COALESCE($5, can_outbound),
            status = COALESCE($6, status),
            updated_at = now()
        WHERE id = $1 AND tenant_id = $2
        RETURNING {PHONE_NUMBER_COLUMNS}
        """,
        number_id,
        ctx.tenant.id,
        body.label,
        body.can_inbound,
        body.can_outbound,
        body.status,
    )
    if not row:
        raise HTTPException(status_code=404, detail="phone number not found")
    return PhoneNumberResponse.from_number(PhoneNumber.from_row(row))


async def provision_number(number_id: UUID, ctx: Context) -> PhoneNumberResponse:
    number = await _require_number(ctx, number_id)
    if number.status == "disabled":
        raise HTTPException(
            status_code=400,
            detail="number is disabled; set status to pending to re-open, then provision",
        )
    account = await _require_account(ctx, number.telephony_account_id)
    # Credentials validated but SIP not built yet — provision the account first.
    if account.status == "connected":
        await provision_account(account.id, ctx)
        account = await _require_account(ctx, account.id)
    if account.status != "ready":
        raise HTTPException(
            status_code=400,
            detail="this carrier account is not set up yet; finish its setup first",
        )

    pool = await ctx.tenant_pool()
    await pool.execute(
        """
        UPDATE phone_numbers
        SET status = 'provisioning', status_message = NULL, updated_at = now()
        WHERE id = $1 AND tenant_id = $2
        """,
        number_id,
        ctx.tenant.id,
    )
    created_trunk_id: str | None = None
    created_rule_id: str | None = None
    try:
        secrets = await load_secrets(ctx.tenant)
        adapter = get_adapter(account.provider)
        # Reload account after possible auto-provision above.
        account = await _require_account(ctx, account.id)
        number_state = await adapter.bind_number(
            account,
            secrets,
            e164=number.e164,
            number_provider_state=dict(number.provider_state),
        )
        alias = number_state.pop("set_trunk_external_alias", None)
        if isinstance(alias, str) and alias.strip():
            acct_state = dict(account.provider_state)
            if not str(acct_state.get("trunk_external_alias") or "").strip():
                acct_state["trunk_external_alias"] = alias.strip()
                await pool.execute(
                    """
                    UPDATE telephony_accounts
                    SET provider_state = $3::jsonb, updated_at = now()
                    WHERE id = $1 AND tenant_id = $2
                    """,
                    account.id,
                    ctx.tenant.id,
                    json.dumps(acct_state),
                )
        # LiveKit objects if agent assigned
        trunk_id = number.livekit_inbound_trunk_id
        rule_id = number.livekit_dispatch_rule_id
        if not trunk_id:
            trunk_id = await livekit_sip.ensure_inbound_trunk(
                name=f"tq_trunk_{number.e164}",
                e164=number.e164,
            )
            created_trunk_id = trunk_id
        if number.inbound_agent_id and not rule_id:
            meta = livekit_sip.inbound_dispatch_metadata(
                tenant_id=ctx.tenant.id,
                agent_id=number.inbound_agent_id,
                phone_number_id=number.id,
                telephony_account_id=account.id,
                did_e164=number.e164,
            )
            rule_id = await livekit_sip.ensure_individual_dispatch_rule(
                name=f"tq_rule_{number.e164}",
                trunk_id=trunk_id,
                agent_name=get_settings().livekit.agent_name,
                metadata=meta,
            )
            created_rule_id = rule_id

        can_inbound = True
        status_message: str | None = None
        if account.provider == "exotel":
            can_inbound = exotel_inbound_console_ready(account.provider_state)
            if not can_inbound:
                status_message = (
                    "Outgoing calls work. Incoming calls need the Exotel setup finished."
                )
        if can_inbound and not number.inbound_agent_id and not rule_id:
            status_message = "Ready. Assign a published voice agent to answer calls."

        row = await pool.fetchrow(
            f"""
            UPDATE phone_numbers
            SET provider_state = $3::jsonb,
                livekit_inbound_trunk_id = $4,
                livekit_dispatch_rule_id = $5,
                can_inbound = $6,
                status = 'active',
                status_message = $7,
                last_synced_at = now(),
                updated_at = now()
            WHERE id = $1 AND tenant_id = $2
            RETURNING {PHONE_NUMBER_COLUMNS}
            """,
            number_id,
            ctx.tenant.id,
            json.dumps(number_state),
            trunk_id,
            rule_id,
            can_inbound,
            status_message,
        )
        assert row is not None
        return PhoneNumberResponse.from_number(PhoneNumber.from_row(row))
    except (ProviderError, RuntimeError, LiveKitServerError) as exc:
        # `LiveKitServerError` is the one this clause exists for and used to
        # miss: every LiveKit call here raises it, so a LiveKit failure both
        # escaped as an unhandled 500 and skipped the cleanup below — leaving
        # exactly the orphan trunk that `ensure_inbound_trunk` now has to adopt.
        #
        # Best-effort cleanup of LiveKit objects created in this attempt.
        if created_rule_id or created_trunk_id:
            try:
                await livekit_sip.teardown_number_livekit(
                    dispatch_rule_id=created_rule_id,
                    inbound_trunk_id=created_trunk_id,
                )
            except Exception:
                logger.exception(
                    "orphan LiveKit cleanup failed for number %s (trunk=%s rule=%s)",
                    number.e164,
                    created_trunk_id,
                    created_rule_id,
                )
        msg = exc.message if isinstance(exc, ProviderError) else str(exc)
        await pool.execute(
            """
            UPDATE phone_numbers
            SET status = 'error', status_message = $3, updated_at = now()
            WHERE id = $1 AND tenant_id = $2
            """,
            number_id,
            ctx.tenant.id,
            msg,
        )
        raise HTTPException(status_code=400, detail=msg) from exc


async def assign_number(
    number_id: UUID, body: AssignPhoneNumberRequest, ctx: Context
) -> PhoneNumberResponse:
    number = await _require_number(ctx, number_id)
    if number.status != "active":
        raise HTTPException(
            status_code=400,
            detail="provision the phone number (status active) before assigning an agent",
        )
    if not number.can_inbound:
        raise HTTPException(
            status_code=400,
            detail=("this number cannot take incoming calls yet — finish the carrier setup first"),
        )
    account = await _require_account(ctx, number.telephony_account_id)
    if account.status != "ready":
        raise HTTPException(
            status_code=400,
            detail="this carrier account is not set up yet; finish its setup first",
        )
    pool = await ctx.tenant_pool()
    agent = await pool.fetchrow(
        """
        SELECT id, channel, published_version
        FROM agents
        WHERE id = $1 AND tenant_id = $2
        """,
        body.agent_id,
        ctx.tenant.id,
    )
    if not agent:
        raise HTTPException(status_code=404, detail="agent not found")
    if agent["published_version"] is None:
        raise HTTPException(status_code=400, detail="publish the agent before assigning a number")
    # Resolve channel from published version config
    version = await pool.fetchrow(
        """
        SELECT config FROM agent_versions
        WHERE agent_id = $1 AND version = $2 AND tenant_id = $3
        """,
        body.agent_id,
        agent["published_version"],
        ctx.tenant.id,
    )
    if not version or not isinstance(version["config"], dict):
        raise HTTPException(status_code=400, detail="published agent version is missing")
    if version["config"].get("channel") != "voice":
        raise HTTPException(status_code=400, detail="only published voice agents can answer SIP")
    # An inbound call carries no request of ours, so a required variable with no
    # default could never be supplied on one. Refused here rather than discovered
    # on a customer's call; publishing and rolling back refuse the same state
    # from the other side (`services.agents.service`). Parsed off the stored
    # config rather than through `AgentConfig`, which is more work than this
    # check needs and can raise on a config that is not its business.
    declared = [VarDeclaration.model_validate(v) for v in version["config"].get("vars") or []]
    if missing := missing_required_vars(declared, {}):
        raise validation_error(
            [
                f"'{name}' is required and has no default, and an inbound call carries no "
                f"request to supply it - give the variable a default, make it optional, or "
                f"leave this number unassigned"
                for name in missing
            ],
            "this agent cannot answer inbound calls",
        )
    trunk_id = number.livekit_inbound_trunk_id
    created_trunk_id: str | None = None
    created_rule_id: str | None = None
    try:
        if not trunk_id:
            trunk_id = await livekit_sip.ensure_inbound_trunk(
                name=f"tq_trunk_{number.e164}",
                e164=number.e164,
            )
            created_trunk_id = trunk_id
        meta = livekit_sip.inbound_dispatch_metadata(
            tenant_id=ctx.tenant.id,
            agent_id=body.agent_id,
            phone_number_id=number.id,
            telephony_account_id=account.id,
            did_e164=number.e164,
        )
        s = get_settings()
        if number.livekit_dispatch_rule_id:
            await livekit_sip.update_dispatch_rule_metadata(
                rule_id=number.livekit_dispatch_rule_id,
                trunk_id=trunk_id,
                name=f"tq_rule_{number.e164}",
                agent_name=s.livekit.agent_name,
                metadata=meta,
            )
            rule_id = number.livekit_dispatch_rule_id
        else:
            rule_id = await livekit_sip.ensure_individual_dispatch_rule(
                name=f"tq_rule_{number.e164}",
                trunk_id=trunk_id,
                agent_name=s.livekit.agent_name,
                metadata=meta,
            )
            created_rule_id = rule_id
    except Exception as exc:
        if created_rule_id or created_trunk_id:
            try:
                await livekit_sip.teardown_number_livekit(
                    dispatch_rule_id=created_rule_id,
                    inbound_trunk_id=created_trunk_id
                    if not number.livekit_inbound_trunk_id
                    else None,
                )
            except Exception:
                logger.exception("orphan LiveKit cleanup failed during assign of %s", number.e164)
        raise HTTPException(
            status_code=502,
            detail=f"failed to configure LiveKit inbound routing: {exc}",
        ) from exc

    row = await pool.fetchrow(
        f"""
        UPDATE phone_numbers
        SET inbound_agent_id = $3,
            livekit_inbound_trunk_id = $4,
            livekit_dispatch_rule_id = $5,
            status_message = NULL,
            updated_at = now()
        WHERE id = $1 AND tenant_id = $2
        RETURNING {PHONE_NUMBER_COLUMNS}
        """,
        number_id,
        ctx.tenant.id,
        body.agent_id,
        trunk_id,
        rule_id,
    )
    assert row is not None
    return PhoneNumberResponse.from_number(PhoneNumber.from_row(row))


async def unassign_number(number_id: UUID, ctx: Context) -> PhoneNumberResponse:
    number = await _require_number(ctx, number_id)
    # Keep the inbound trunk (number still provisioned) but remove the rule so
    # no agent is dispatched. Trunk is removed only on disable.
    if number.livekit_dispatch_rule_id:
        try:
            await livekit_sip.delete_dispatch_rule(number.livekit_dispatch_rule_id)
        except Exception as exc:
            logger.exception("failed to delete dispatch rule on unassign")
            raise HTTPException(
                status_code=502,
                detail=f"failed to remove LiveKit dispatch rule: {exc}",
            ) from exc
    pool = await ctx.tenant_pool()
    row = await pool.fetchrow(
        f"""
        UPDATE phone_numbers
        SET inbound_agent_id = NULL,
            livekit_dispatch_rule_id = NULL,
            status_message = 'Ready. Assign a published voice agent to answer calls.',
            updated_at = now()
        WHERE id = $1 AND tenant_id = $2
        RETURNING {PHONE_NUMBER_COLUMNS}
        """,
        number_id,
        ctx.tenant.id,
    )
    assert row is not None
    return PhoneNumberResponse.from_number(PhoneNumber.from_row(row))


async def create_outbound_call(body: OutboundCallRequest, ctx: Context) -> OutboundCallResponse:
    """Dial one number now. The database half commits before anything is dialled.

    Structurally identical to what it always did — resolve, write, dispatch —
    but the three steps now live in ``dial.py`` so the batch dispatcher can put
    the middle one inside a transaction of its own.

    ``userdata`` is about the PERSON and is merged onto their contact record, so
    it is there again on their next call. ``vars`` is about THIS call: read-only,
    gone when it ends, and never written onto anybody's record.
    """
    try:
        # The number first, so "this number cannot dial out" is answered before
        # a possibly-large plan is resolved and validated.
        number, account = await dial.resolve_dial_number(ctx, body.from_phone_number_id)
    except dial.DialError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    # Now, and not in the request model: a bare national number is national to
    # OUR DID, which is the row just resolved. Strict — there is money and a
    # carrier at the other end of this, so a typo is refused rather than dialled.
    try:
        to_e164 = normalize_e164(body.to, did=number.e164)
    except ValueError:
        raise validation_error(
            [f"to: {body.to!r} is not a valid number to dial from {number.e164}"],
            "invalid destination number",
        ) from None
    plan = await resolve_call_plan(ctx, body, channels=("voice",))
    target = dial.dial_target_for_plan(number, account, plan)
    # Before anything is written and before any carrier is contacted. The gate
    # lives here rather than inside `dial.py` because the batch dispatcher needs
    # a different answer to the same question — it stops the pass, not the
    # recipient — and must not receive a per-recipient exception.
    if not await credits.has_credit(ctx.tenant):
        raise HTTPException(
            status_code=402,
            detail=ErrorBody(message=credits.INSUFFICIENT_CREDITS_MESSAGE, errors=[]).model_dump(),
        )
    try:
        userdata = dial.validate_userdata(body.userdata)
        pool = await ctx.tenant_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                recorded = await dial.record_outbound_call(
                    conn,
                    ctx=ctx,
                    target=target,
                    to_e164=to_e164,
                    userdata=userdata,
                    session_vars=plan.vars,
                )
        await dial.dispatch_outbound_call(ctx=ctx, target=target, recorded=recorded)
    except dial.DialError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

    # session.queued — dial is pending agent accept / callee answer.
    spawn(
        _dispatch_session_queued(
            ctx,
            agent_id=target.agent_id,
            session_id=recorded.session_id,
            conversation_id=recorded.conversation_id,
            phone_number_id=target.number.id,
            from_e164=target.number.e164,
            to_e164=recorded.to_e164,
        )
    )

    return OutboundCallResponse(
        session_id=recorded.session_id,
        warnings=list(plan.warnings),
    )


# ── internals ───────────────────────────────────────────────────────────────


def _sip_password_resolves(credentials: dict[str, Any], secrets: dict[str, str]) -> bool:
    try:
        return bool(resolve_secretish(credentials.get("sip_outbound_password"), secrets))
    except MissingSecretError:
        return False


async def _teardown_account_inbound_routing(ctx: Context, account_id: UUID) -> None:
    """Remove LiveKit inbound trunks/rules for every number on this account.

    Carrier-side SIP objects stay (account can be re-enabled). Without LiveKit
    routing, inbound PSTN cannot dispatch an agent for this tenant.

    ``inbound_agent_id`` is deliberately left alone. It is not what dispatches a
    call — the LiveKit rule is, and that is gone — so keeping it costs nothing,
    and nothing anywhere records who used to answer a number, so clearing it
    here destroyed that answer for every number at once. Re-enabling still needs
    ``provision_number`` per number to rebuild the rules; what this preserves is
    the knowledge of who to put back on, not the routing itself.
    """
    pool = await ctx.tenant_pool()
    rows = await pool.fetch(
        """
        SELECT id, e164, livekit_inbound_trunk_id, livekit_dispatch_rule_id
        FROM phone_numbers
        WHERE tenant_id = $1 AND telephony_account_id = $2
          AND status <> 'disabled'
          AND (
            livekit_inbound_trunk_id IS NOT NULL
            OR livekit_dispatch_rule_id IS NOT NULL
            OR inbound_agent_id IS NOT NULL
          )
        """,
        ctx.tenant.id,
        account_id,
    )
    for row in rows:
        try:
            await livekit_sip.teardown_number_livekit(
                dispatch_rule_id=row["livekit_dispatch_rule_id"],
                inbound_trunk_id=row["livekit_inbound_trunk_id"],
            )
        except Exception as exc:
            logger.exception(
                "LiveKit teardown failed while disabling account %s number %s",
                account_id,
                row["e164"],
            )
            raise HTTPException(
                status_code=502,
                detail=(
                    f"failed to remove LiveKit SIP objects for {row['e164']}: {exc}. "
                    "Fix LiveKit connectivity and retry disable."
                ),
            ) from exc

    await pool.execute(
        """
        UPDATE phone_numbers
        SET livekit_inbound_trunk_id = NULL,
            livekit_dispatch_rule_id = NULL,
            status_message = CASE
                WHEN status = 'active'
                    THEN 'Carrier disabled. Enable it and run setup again to start answering.'
                ELSE status_message
            END,
            updated_at = now()
        WHERE tenant_id = $1 AND telephony_account_id = $2
          AND status <> 'disabled'
        """,
        ctx.tenant.id,
        account_id,
    )


async def _dispatch_session_queued(
    ctx: Context,
    *,
    agent_id: UUID | None,
    session_id: UUID,
    conversation_id: UUID,
    phone_number_id: UUID,
    from_e164: str,
    to_e164: str,
) -> None:
    """`agent_id` is null when the call runs an agent defined in the request —
    there is no row to name, and inventing one would send a subscriber to an
    agent they cannot fetch."""
    try:
        await webhooks.dispatch(
            ctx.tenant,
            events.SESSION_QUEUED,
            str(agent_id) if agent_id else None,
            {
                "session_id": str(session_id),
                "conversation_id": str(conversation_id),
                "agent_id": str(agent_id) if agent_id else None,
                "type": "SIP_OUTBOUND",
                "channel": "voice",
                "phone_number_id": str(phone_number_id),
                "from_e164": from_e164,
                "to_e164": to_e164,
            },
        )
    except Exception:
        logger.exception("session.queued webhook dispatch failed for %s", session_id)


async def _sync_exotel_number_inbound_capability(ctx: Context, account: TelephonyAccount) -> None:
    """Propagate Exotel checklist readiness onto phone_numbers.can_inbound."""
    ready = exotel_inbound_console_ready(account.provider_state)
    pool = await ctx.tenant_pool()
    if ready:
        await pool.execute(
            """
            UPDATE phone_numbers
            SET can_inbound = true,
                status_message = CASE
                    WHEN inbound_agent_id IS NULL AND status = 'active'
                        THEN 'Ready. Assign a published voice agent to answer calls.'
                    WHEN status = 'active' THEN NULL
                    ELSE status_message
                END,
                updated_at = now()
            WHERE tenant_id = $1 AND telephony_account_id = $2
              AND status IN ('pending', 'provisioning', 'active', 'error')
            """,
            ctx.tenant.id,
            account.id,
        )
    else:
        await pool.execute(
            """
            UPDATE phone_numbers
            SET can_inbound = false,
                status_message = CASE
                    WHEN status = 'active'
                        THEN 'Outgoing calls work. Incoming calls need the '
                             'Exotel setup finished.'
                    ELSE status_message
                END,
                updated_at = now()
            WHERE tenant_id = $1 AND telephony_account_id = $2
              AND status IN ('pending', 'provisioning', 'active', 'error')
            """,
            ctx.tenant.id,
            account.id,
        )


async def _require_account(ctx: Context, account_id: UUID) -> TelephonyAccount:
    pool = await ctx.tenant_pool()
    row = await pool.fetchrow(
        f"""
        SELECT {TELEPHONY_ACCOUNT_COLUMNS}
        FROM telephony_accounts
        WHERE id = $1 AND tenant_id = $2
        """,
        account_id,
        ctx.tenant.id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="telephony account not found")
    return TelephonyAccount.from_row(row)


async def _require_number(ctx: Context, number_id: UUID) -> PhoneNumber:
    pool = await ctx.tenant_pool()
    row = await pool.fetchrow(
        f"""
        SELECT {PHONE_NUMBER_COLUMNS}
        FROM phone_numbers
        WHERE id = $1 AND tenant_id = $2
        """,
        number_id,
        ctx.tenant.id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="phone number not found")
    return PhoneNumber.from_row(row)


async def _set_account_status(
    ctx: Context,
    account_id: UUID,
    status: str,
    status_message: str | None,
) -> TelephonyAccount:
    pool = await ctx.tenant_pool()
    row = await pool.fetchrow(
        f"""
        UPDATE telephony_accounts
        SET status = $3, status_message = $4, updated_at = now()
        WHERE id = $1 AND tenant_id = $2
        RETURNING {TELEPHONY_ACCOUNT_COLUMNS}
        """,
        account_id,
        ctx.tenant.id,
        status,
        status_message,
    )
    assert row is not None
    return TelephonyAccount.from_row(row)


def _unique_account_detail(exc: asyncpg.UniqueViolationError) -> str:
    constraint = getattr(exc, "constraint_name", None) or ""
    if "plivo" in constraint:
        return "a Plivo account with that Auth ID already exists"
    if "exotel" in constraint:
        return "an Exotel account with that Account SID already exists"
    if "vobiz" in constraint:
        return "a Vobiz account with that Auth ID already exists"
    if "twilio" in constraint:
        return "a Twilio account with that Account SID already exists"
    if "display_name" in constraint:
        return "a telephony account with that name already exists"
    return "this telephony account conflicts with an existing connection"
