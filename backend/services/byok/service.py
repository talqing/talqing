"""Per-tenant AI provider API keys — bring your own key.

Talqing runs every agent on the tenant's own provider credentials: there is no
platform key behind an agent run, so a provider with no key here cannot be
published (``services.agents.validate``) and cannot be compiled
(``compiler.factories``). The CoPilots keep using the platform keys in
settings — they are ours to pay for, not the tenant's.

The voice/avatar galleries sit between the two: they browse the tenant's own key
when there is one (so a voice they cloned in their own provider account is
pickable, and a library voice they save lands in the account that will
synthesize it) and fall back to the platform key otherwise, so the editor still
has a gallery before the tenant has any key at all.

A key is proved against its provider before it is stored (``verify``), so every
row here is one that worked at least once and the account it belongs to can be
named on screen.

Stored Fernet-encrypted at rest in the tenant DB; the value is write-only over
the API (reads return a stored masked hint, never the plaintext).

This package owns all access to the ``provider_keys`` table and all encrypt/
decrypt of those values. Which providers exist, and how they are labelled and
pictured, stays with the catalog — this list carries credential state only.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import HTTPException
from pydantic import BaseModel, StringConstraints

import db
from api.core.schemas import OkResponse, mask_tail
from services.catalog import get_catalog
from services.user import Context, Tenant
from utils.crypto import decrypt, encrypt

from .verify import verify_provider_key


class SetProviderKeyRequest(BaseModel):
    api_key: Annotated[str, StringConstraints(min_length=1, max_length=16384)]


class ProviderKeyResponse(BaseModel):
    provider: str
    api_key_hint: str
    # What the provider called the account when it accepted the key, or "" for
    # the providers that publish no account identity at all. Display only.
    account_label: str
    created_at: datetime
    updated_at: datetime


class ProviderKeysResponse(BaseModel):
    """Only the providers the tenant has a key for. The full provider list (with
    labels and logos) comes from GET /catalog; clients join the two on
    ``provider``."""

    items: list[ProviderKeyResponse]


def _require_catalog_provider(provider: str) -> str:
    """Normalize to the catalog's lowercase provider id, or 404."""
    key = provider.strip().lower()
    if not get_catalog().provider_enabled(key):
        raise HTTPException(status_code=404, detail=f"unknown provider '{provider}'")
    return key


def _out(row) -> ProviderKeyResponse:
    return ProviderKeyResponse(
        provider=row["provider"],
        api_key_hint=row["api_key_hint"],
        account_label=row["account_label"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


# ── Runtime / internal loaders (decrypted values stay in-process) ────────────


async def load_provider_keys(tenant: Tenant) -> dict[str, str]:
    """Decrypt the tenant's provider keys into ``{provider: api_key}``.

    This is what the compiler builds LLM/STT/TTS/avatar plugins from.
    In-process only — never log or return plaintext over the API.
    """
    pool = await db.tenant_pool(tenant)
    rows = await pool.fetch(
        "SELECT provider, api_key_encrypted FROM provider_keys WHERE tenant_id = $1",
        tenant.id,
    )
    return {row["provider"]: decrypt(row["api_key_encrypted"]) for row in rows}


async def load_provider_key(tenant: Tenant, provider: str) -> str:
    """Decrypt this tenant's key for one provider, or ``""`` if they have none.

    For acting on the tenant's behalf against a provider API where the whole key
    map would be overkill — the catalog galleries load exactly the one provider
    being browsed. Unknown provider names are not rejected here; the caller
    already validated the name against the catalog.

    In-process only, like ``load_provider_keys``. The plaintext never leaves the
    process, which is why any member may trigger this while *writing* a key
    stays admin-only.
    """
    pool = await db.tenant_pool(tenant)
    row = await pool.fetchrow(
        "SELECT api_key_encrypted FROM provider_keys WHERE tenant_id = $1 AND provider = $2",
        tenant.id,
        provider.strip().lower(),
    )
    return decrypt(row["api_key_encrypted"]) if row else ""


async def list_configured_providers(ctx: Context) -> set[str]:
    """Providers the tenant holds a key for (for validation). Does not decrypt."""
    pool = await ctx.tenant_pool()
    rows = await pool.fetch(
        "SELECT provider FROM provider_keys WHERE tenant_id = $1",
        ctx.tenant.id,
    )
    return {row["provider"] for row in rows}


# ── HTTP / CoPilot CRUD (write-only values; reads return hints only) ─────────


async def list_provider_keys(ctx: Context) -> ProviderKeysResponse:
    pool = await ctx.tenant_pool()
    rows = await pool.fetch(
        "SELECT provider, api_key_hint, account_label, created_at, updated_at "
        "FROM provider_keys WHERE tenant_id = $1 ORDER BY provider",
        ctx.tenant.id,
    )
    return ProviderKeysResponse(items=[_out(r) for r in rows])


async def set_provider_key(
    provider: str,
    body: SetProviderKeyRequest,
    ctx: Context,
) -> ProviderKeyResponse:
    """Save or rotate the tenant's key for one provider.

    The provider is called first (``verify.verify_provider_key``) and a key it
    refuses is never written, so a stored key is a key that worked. Rotating
    onto a bad key therefore leaves the working one in place rather than
    replacing it with one that cannot run.
    """
    key = _require_catalog_provider(provider)
    api_key = body.api_key.strip()
    if not api_key:
        raise HTTPException(status_code=400, detail="api_key must not be blank")
    account = await verify_provider_key(key, api_key)
    pool = await ctx.tenant_pool()
    row = await pool.fetchrow(
        """
        INSERT INTO provider_keys (
            tenant_id, provider, api_key_encrypted, api_key_hint, account_label
        )
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (tenant_id, provider) DO UPDATE SET
            api_key_encrypted = EXCLUDED.api_key_encrypted,
            api_key_hint = EXCLUDED.api_key_hint,
            account_label = EXCLUDED.account_label,
            updated_at = now()
        RETURNING provider, api_key_hint, account_label, created_at, updated_at
        """,
        ctx.tenant.id,
        key,
        encrypt(api_key),
        mask_tail(api_key),
        account.label,
    )
    return _out(row)


async def delete_provider_key(provider: str, ctx: Context) -> OkResponse:
    """Remove the key. Agents already published on this provider keep their
    published version but will fail their next run — removing a key is the
    tenant saying they no longer want Talqing calling that provider."""
    key = _require_catalog_provider(provider)
    pool = await ctx.tenant_pool()
    row = await pool.fetchrow(
        "DELETE FROM provider_keys WHERE provider = $1 AND tenant_id = $2 RETURNING provider",
        key,
        ctx.tenant.id,
    )
    if not row:
        raise HTTPException(status_code=404, detail=f"no API key stored for '{key}'")
    return OkResponse()
