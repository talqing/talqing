"""OAuth credential storage and runtime token access.

Access/refresh tokens are Fernet-encrypted at rest (same secrets_fernet_key).
Connect flows live under ``providers/``; this module owns DB rows + refresh/revoke.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
import httpx

import db
from services.user import Tenant
from utils.crypto import decrypt, encrypt

from .protocol import (
    OAuthCredentialError,
    oauth_http_client,
    post_form_or_json,
    token_expiry,
)
from .registry import get_oauth_flow

TOKEN_REFRESH_SKEW = timedelta(seconds=600)


@dataclass(frozen=True, slots=True)
class OAuthAccessToken:
    """A usable access token, and when it stops being one.

    The expiry rides along so a caller that will make many requests on one token
    — an ``httpx.Auth`` runs per request — can hold it for as long as it is good
    for instead of asking the database again each time.
    """

    token: str
    expires_at: datetime

    def usable(self, now: datetime) -> bool:
        """True while there is more than the refresh skew left on it."""
        return self.expires_at > now + TOKEN_REFRESH_SKEW


@dataclass(frozen=True)
class OAuthCredential:
    id: str
    integration_id: str
    provider: str
    account_email: str
    provider_subject: str | None
    scopes: list[str]
    access_token: str
    refresh_token: str
    expires_at: datetime
    token_type: str
    oauth_client_id: str | None
    oauth_metadata: dict[str, str]


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _metadata_from_row(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for key, value in raw.items():
        if isinstance(key, str) and value is not None and str(value).strip():
            out[key] = str(value).strip()
    return out


def _credential_from_row(row: asyncpg.Record) -> OAuthCredential:
    return OAuthCredential(
        id=str(row["id"]),
        integration_id=str(row["integration_id"]),
        provider=row["provider"],
        account_email=row["account_email"],
        provider_subject=row["provider_subject"],
        scopes=list(row["scopes"] or []),
        access_token=decrypt(row["access_token"]),
        refresh_token=decrypt(row["refresh_token"]),
        expires_at=row["expires_at"],
        token_type=row["token_type"] or "Bearer",
        oauth_client_id=row["oauth_client_id"],
        oauth_metadata=_metadata_from_row(row["oauth_metadata"]),
    )


_CREDENTIAL_SELECT = """
id, integration_id, provider, account_email, provider_subject,
scopes, access_token, refresh_token, expires_at, token_type,
oauth_client_id, oauth_metadata
"""


async def upsert_oauth_credential(
    conn: asyncpg.Connection,
    *,
    tenant_id: str,
    integration_id: str,
    provider: str,
    account_email: str,
    provider_subject: str | None,
    scopes: list[str],
    access_token: str,
    refresh_token: str,
    expires_at: datetime,
    token_type: str,
    oauth_client_id: str | None = None,
    oauth_metadata: Mapping[str, str] | None = None,
) -> None:
    """Insert or update OAuth tokens.

    Empty ``refresh_token`` keeps the previous refresh token (providers that omit
    refresh on re-consent). Empty ``oauth_metadata`` keeps previous metadata.
    """
    import json

    metadata = dict(oauth_metadata or {})
    existing = await conn.fetchrow(
        """
        SELECT refresh_token, oauth_metadata, oauth_client_id
        FROM integration_oauth_credentials
        WHERE tenant_id = $1 AND integration_id = $2
        """,
        tenant_id,
        integration_id,
    )
    final_refresh = refresh_token
    if not final_refresh and existing is not None:
        final_refresh = decrypt(existing["refresh_token"])
    if not final_refresh:
        raise OAuthCredentialError(
            f"{provider} did not return a refresh token and none is stored. Connect again."
        )

    if not metadata and existing is not None:
        metadata = _metadata_from_row(existing["oauth_metadata"])

    final_client_id = oauth_client_id
    if not final_client_id and existing is not None:
        final_client_id = existing["oauth_client_id"]

    await conn.execute(
        """
        INSERT INTO integration_oauth_credentials (
            tenant_id, integration_id, provider, account_email, provider_subject,
            scopes, access_token, refresh_token, expires_at,
            token_type, oauth_client_id, oauth_metadata
        )
        VALUES ($1, $2, $3, $4, $5, $6::text[], $7, $8, $9, $10, $11, $12::jsonb)
        ON CONFLICT (tenant_id, integration_id) DO UPDATE
        SET provider = EXCLUDED.provider,
            account_email = EXCLUDED.account_email,
            provider_subject = EXCLUDED.provider_subject,
            scopes = EXCLUDED.scopes,
            access_token = EXCLUDED.access_token,
            refresh_token = EXCLUDED.refresh_token,
            expires_at = EXCLUDED.expires_at,
            token_type = EXCLUDED.token_type,
            oauth_client_id = EXCLUDED.oauth_client_id,
            oauth_metadata = EXCLUDED.oauth_metadata,
            updated_at = now()
        """,
        tenant_id,
        integration_id,
        provider,
        account_email,
        provider_subject,
        scopes,
        encrypt(access_token),
        encrypt(final_refresh),
        expires_at,
        token_type,
        final_client_id,
        json.dumps(metadata),
    )


async def load_oauth_credential(
    tenant: Tenant,
    *,
    integration_id: str,
    provider: str,
) -> OAuthCredential | None:
    pool = await db.tenant_pool(tenant)
    row = await pool.fetchrow(
        f"""
        SELECT {_CREDENTIAL_SELECT}
        FROM integration_oauth_credentials
        WHERE tenant_id = $1 AND integration_id = $2 AND provider = $3
        """,
        tenant.id,
        integration_id,
        provider,
    )
    if not row:
        return None
    return _credential_from_row(row)


async def mark_integration_needs_reconnect(
    tenant: Tenant,
    *,
    integration_id: str,
) -> None:
    pool = await db.tenant_pool(tenant)
    await pool.execute(
        """
        UPDATE integrations
        SET status = 'needs_reconnect', updated_at = now()
        WHERE tenant_id = $1 AND id = $2
        """,
        tenant.id,
        integration_id,
    )


async def get_oauth_access_token(
    tenant: Tenant,
    *,
    integration_id: str,
    provider: str,
    force_refresh: bool = False,
) -> OAuthAccessToken:
    """Load (and refresh if needed) an encrypted OAuth access token.

    No database transaction is open while the provider's token endpoint is
    called. Holding one would pin a connection — a pgbouncer *server* connection
    once the proxy is in front of Postgres — for the duration of a third party's
    latency, on a path that runs per MCP tool call.

    Concurrent rotation is still safe without the row lock: the new token is
    written only if the stored one is still the token it was refreshed from.
    Ciphertext differs on every write (Fernet is randomized), so the stored
    ``access_token`` is a usable compare-and-set key. A refresh that loses the
    race returns the winner's token rather than overwriting it, which is what
    matters for providers whose refresh tokens are single-use (HubSpot MCP).
    """
    flow = get_oauth_flow(provider)
    if flow is None:
        raise OAuthCredentialError(f"unsupported OAuth provider: {provider}")

    pool = await db.tenant_pool(tenant)
    row = await pool.fetchrow(
        f"""
        SELECT {_CREDENTIAL_SELECT}
        FROM integration_oauth_credentials
        WHERE tenant_id = $1 AND integration_id = $2 AND provider = $3
        """,
        tenant.id,
        integration_id,
        provider,
    )
    if not row:
        await mark_integration_needs_reconnect(tenant, integration_id=integration_id)
        raise OAuthCredentialError(f"{provider} is not connected. Reconnect the integration.")
    credential = _credential_from_row(row)
    stored = OAuthAccessToken(credential.access_token, credential.expires_at)
    if not force_refresh and stored.usable(_utcnow()):
        return stored

    try:
        params = flow.refresh_params(
            oauth_client_id=credential.oauth_client_id,
            refresh_token=credential.refresh_token,
            oauth_metadata=credential.oauth_metadata,
        )
    except OAuthCredentialError:
        await mark_integration_needs_reconnect(tenant, integration_id=integration_id)
        raise

    payload: dict[str, Any] = {
        "client_id": params.client_id,
        "refresh_token": credential.refresh_token,
        "grant_type": "refresh_token",
    }
    if params.client_secret is not None:
        payload["client_secret"] = params.client_secret
    if params.extra:
        payload.update(params.extra)

    try:
        async with oauth_http_client() as client:
            response = await post_form_or_json(
                client,
                params.token_url,
                payload,
                json_body=params.json_body,
            )
    except httpx.HTTPError as exc:
        raise OAuthCredentialError(f"{params.label} token refresh failed") from exc

    if response.status_code >= 400:
        await mark_integration_needs_reconnect(tenant, integration_id=integration_id)
        raise OAuthCredentialError(
            f"{params.label} authorization expired. Reconnect the integration."
        )

    body = response.json()
    if not isinstance(body, dict):
        await mark_integration_needs_reconnect(tenant, integration_id=integration_id)
        raise OAuthCredentialError(f"{params.label} token refresh returned an invalid response.")
    access_token = str(body.get("access_token") or "")
    if not access_token:
        await mark_integration_needs_reconnect(tenant, integration_id=integration_id)
        raise OAuthCredentialError(f"{params.label} token refresh returned an invalid token.")

    # Prefer provider expires_in; fall back so missing expires_in is not fatal.
    expires_at = token_expiry(body.get("expires_in"), default_seconds=3600)
    token_type = str(body.get("token_type") or credential.token_type or "Bearer")
    # HubSpot MCP: refresh tokens are single-use — always store rotation.
    refresh_token = str(body.get("refresh_token") or credential.refresh_token)

    written = await pool.fetchval(
        """
        UPDATE integration_oauth_credentials
        SET access_token = $4,
            expires_at = $5,
            token_type = $6,
            refresh_token = $7,
            updated_at = now()
        WHERE tenant_id = $1 AND integration_id = $2 AND provider = $3
          AND access_token = $8
        RETURNING 1
        """,
        tenant.id,
        integration_id,
        provider,
        encrypt(access_token),
        expires_at,
        token_type,
        encrypt(refresh_token),
        row["access_token"],
    )
    if written is not None:
        return OAuthAccessToken(access_token, expires_at)

    # Another refresh committed first. Its token is the current one, and for a
    # rotating-refresh provider it is also the only one still valid.
    current = await pool.fetchrow(
        """
        SELECT access_token, expires_at
        FROM integration_oauth_credentials
        WHERE tenant_id = $1 AND integration_id = $2 AND provider = $3
        """,
        tenant.id,
        integration_id,
        provider,
    )
    if current is None:
        raise OAuthCredentialError(f"{provider} is not connected. Reconnect the integration.")
    return OAuthAccessToken(decrypt(current["access_token"]), current["expires_at"])


async def revoke_provider_tokens(
    *,
    provider: str,
    access_token: str,
    refresh_token: str,
    oauth_client_id: str | None,
    oauth_metadata: Mapping[str, str] | None = None,
) -> None:
    flow = get_oauth_flow(provider)
    if flow is None:
        return
    await flow.revoke(
        access_token=access_token,
        refresh_token=refresh_token,
        oauth_client_id=oauth_client_id,
        oauth_metadata=oauth_metadata,
    )
