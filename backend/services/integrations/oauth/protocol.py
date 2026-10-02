"""OAuth protocol surface used by provider flow classes.

Leaf module: types, PKCE, HTTP helpers, discovery, and shared flow utilities.
Provider modules under ``providers/`` import from here only — never credentials
or session — so the catalog can load without full OAuth stack cycles.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from urllib.parse import urlencode

import httpx

from api.core import security
from settings import get_settings

# ── Types ──────────────────────────────────────────────────────────────────


class OAuthCredentialError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class OAuthConnectResult:
    """Everything needed to finalize an OAuth integration after code exchange."""

    access_token: str
    refresh_token: str
    token_type: str
    expires_at: datetime
    scopes: list[str]
    # Only keys in `definitions.OAUTH_ACCOUNT_KEYS`. Connect writes this bag
    # straight to the DB while every later patch validates it, so anything else
    # connects cleanly and then fails every edit; `oauth.session` rejects it.
    provider_account_info: dict[str, Any]
    account_email: str
    provider_subject: str | None
    # `"<Provider> - <who>"`, or the bare provider name when the provider names
    # nobody. Never invent a stand-in like "Asana account": the dashboard shows
    # an absent identity as a dash, and a filler puts the provider's own name in
    # the Account column, which the Provider column beside it already gives.
    display_name_base: str
    oauth_client_id: str | None = None
    # Connection-time discovery / endpoints for refresh and revoke.
    # Keys: token_endpoint, resource, revocation_endpoint (all optional strings).
    oauth_metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RefreshParams:
    label: str
    token_url: str
    client_id: str
    client_secret: str | None
    json_body: bool = False
    extra: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class OAuthServerDiscovery:
    """RFC 9728 protected-resource + RFC 8414 authorization-server metadata."""

    resource: str
    authorization_endpoint: str
    token_endpoint: str
    registration_endpoint: str | None = None
    revocation_endpoint: str | None = None
    scopes: list[str] = field(default_factory=list)


class OAuthFlow(Protocol):
    """Per-provider OAuth connect + lifecycle hooks."""

    provider: str
    label: str

    def is_configured(self) -> bool: ...

    async def build_authorize_url(
        self,
        *,
        user_id: str,
        tenant_id: str,
        integration_id: str | None,
    ) -> str: ...

    async def exchange_code(
        self,
        *,
        code: str,
        oauth_state: dict[str, str | None],
    ) -> OAuthConnectResult: ...

    def refresh_params(
        self,
        *,
        oauth_client_id: str | None,
        refresh_token: str,
        oauth_metadata: Mapping[str, str] | None = None,
    ) -> RefreshParams: ...

    async def revoke(
        self,
        *,
        access_token: str,
        refresh_token: str,
        oauth_client_id: str | None,
        oauth_metadata: Mapping[str, str] | None = None,
    ) -> None: ...


# ── Time / tokens ───────────────────────────────────────────────────────────


def utcnow() -> datetime:
    return datetime.now(UTC)


def token_expiry(expires_in: int | str | None, *, default_seconds: int = 3600) -> datetime:
    """Parse expires_in; fall back to default_seconds when missing or invalid."""
    try:
        seconds = int(expires_in or 0)
    except (TypeError, ValueError):
        seconds = 0
    if seconds <= 0:
        seconds = default_seconds
    return utcnow() + timedelta(seconds=seconds)


# ── PKCE ───────────────────────────────────────────────────────────────────


def _pkce_nonce() -> str:
    return secrets.token_urlsafe(32)


def _pkce_verifier(seed: str) -> str:
    # The same key that signs the OAuth state this verifier belongs to, and for
    # the same reason: both are minted and read back by this one region, and
    # neither crosses a host.
    digest = hmac.new(
        get_settings().security.oauth_state_key.encode("utf-8"),
        seed.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _pkce_seed(user_id: str, tenant_id: str, integration_id: str | None, nonce: str) -> str:
    return f"{user_id}:{tenant_id}:{integration_id or ''}:{nonce}"


def make_pkce(user_id: str, tenant_id: str, integration_id: str | None) -> tuple[str, str, str]:
    """Return (nonce, code_verifier, code_challenge)."""
    nonce = _pkce_nonce()
    verifier = _pkce_verifier(_pkce_seed(user_id, tenant_id, integration_id, nonce))
    return nonce, verifier, _pkce_challenge(verifier)


def recover_pkce_verifier(oauth_state: dict[str, str | None]) -> str:
    nonce = str(oauth_state.get("pkce_nonce") or "").strip()
    if not nonce:
        raise OAuthCredentialError("OAuth state is incomplete. Try connecting again.")
    return _pkce_verifier(
        _pkce_seed(
            str(oauth_state["user_id"]),
            str(oauth_state["tenant_id"]),
            oauth_state.get("integration_id"),
            nonce,
        )
    )


# ── HTTP helpers ───────────────────────────────────────────────────────────


def signed_oauth_state(
    *,
    user_id: str,
    tenant_id: str,
    provider: str,
    integration_id: str | None,
    extra: dict[str, str] | None = None,
) -> str:
    return security.create_oauth_state(
        user_id=user_id,
        tenant_id=tenant_id,
        provider=provider,
        integration_id=integration_id,
        extra=extra,
    )


def oauth_http_client(*, timeout: float = 15) -> httpx.AsyncClient:
    """The client every provider OAuth call is made through.

    ``http2=True`` is the whole reason this exists. httpx speaks HTTP/1.1 unless
    told otherwise, and RocketReach sits behind a Cloudflare rule that answers
    *every* HTTP/1.1 request — discovery, token, MCP — with a "Just a moment..."
    challenge page instead of the document. HTTP/2 is negotiated over ALPN, so a
    provider that only speaks HTTP/1.1 still gets HTTP/1.1: this is a superset of
    the previous behaviour, and it is the OAuth layer's default rather than one
    provider's workaround because the next vendor to turn bot protection on
    should not cost us another outage.
    """
    return httpx.AsyncClient(timeout=timeout, http2=True)


def authorize_redirect(auth_url: str, params: dict[str, str]) -> str:
    return f"{auth_url}?{urlencode(params)}"


async def post_form_or_json(
    client: httpx.AsyncClient,
    url: str,
    payload: dict[str, Any],
    *,
    json_body: bool,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    if json_body:
        return await client.post(url, json=payload, headers=headers)
    return await client.post(url, data=payload, headers=headers)


# ── Discovery (RFC 9728 + RFC 8414) ────────────────────────────────────────


def oauth_http_error(response: httpx.Response) -> str:
    """Best-effort human message from an OAuth/token JSON error body."""
    try:
        body: Any = response.json()
    except ValueError:
        return response.text[:300] or f"HTTP {response.status_code}"
    if not isinstance(body, dict):
        return f"HTTP {response.status_code}"
    err = body.get("error")
    if isinstance(err, dict):
        message = err.get("message") or err.get("error_description")
        if message:
            return str(message)
    if isinstance(err, str):
        description = body.get("error_description")
        if description:
            return f"{err}: {description}"
        return err
    message = body.get("message")
    if message:
        return str(message)
    errors = body.get("errors")
    if errors:
        return str(errors)[:300]
    return f"HTTP {response.status_code}"


async def discover_oauth_server(
    client: httpx.AsyncClient,
    *,
    protected_resource_metadata_url: str,
    default_resource: str,
    label: str,
    require_registration_endpoint: bool = False,
    authorization_server_fallback: str | None = None,
    default_token_endpoint: str | None = None,
    default_revocation_endpoint: str | None = None,
    default_scopes: list[str] | None = None,
) -> OAuthServerDiscovery:
    """Fetch protected-resource metadata, then authorization-server metadata."""
    resource_response = await client.get(
        protected_resource_metadata_url,
        headers={"Accept": "application/json"},
    )
    if resource_response.status_code >= 400:
        raise OAuthCredentialError(
            f"{label} OAuth resource discovery failed: {oauth_http_error(resource_response)}"
        )
    resource_payload = resource_response.json()
    if not isinstance(resource_payload, dict):
        raise OAuthCredentialError(f"{label} OAuth resource discovery returned a non-object")

    authorization_servers = resource_payload.get("authorization_servers")
    authorization_server = ""
    if isinstance(authorization_servers, list) and authorization_servers:
        authorization_server = str(authorization_servers[0]).rstrip("/")
    if not authorization_server and authorization_server_fallback:
        authorization_server = authorization_server_fallback.rstrip("/")
    if not authorization_server:
        raise OAuthCredentialError(
            f"{label} OAuth discovery did not return an authorization server"
        )

    metadata_url = f"{authorization_server}/.well-known/oauth-authorization-server"
    metadata_response = await client.get(metadata_url, headers={"Accept": "application/json"})
    if metadata_response.status_code >= 400:
        raise OAuthCredentialError(
            f"{label} OAuth server discovery failed: {oauth_http_error(metadata_response)}"
        )
    metadata_payload = metadata_response.json()
    if not isinstance(metadata_payload, dict):
        raise OAuthCredentialError(f"{label} OAuth server discovery returned a non-object")

    resource = str(resource_payload.get("resource") or default_resource).strip()
    authorization_endpoint = str(metadata_payload.get("authorization_endpoint") or "").strip()
    token_endpoint = str(
        metadata_payload.get("token_endpoint") or default_token_endpoint or ""
    ).strip()
    registration_endpoint = str(metadata_payload.get("registration_endpoint") or "").strip() or None
    revocation_endpoint = (
        str(
            metadata_payload.get("revocation_endpoint") or default_revocation_endpoint or ""
        ).strip()
        or None
    )

    scopes_payload = resource_payload.get("scopes_supported")
    if isinstance(scopes_payload, list) and scopes_payload:
        scopes = [str(scope) for scope in scopes_payload]
    else:
        scopes = list(default_scopes or [])

    if not resource or not authorization_endpoint or not token_endpoint:
        raise OAuthCredentialError(f"{label} OAuth discovery did not return required endpoints")
    if require_registration_endpoint and not registration_endpoint:
        raise OAuthCredentialError(
            f"{label} OAuth discovery did not return a registration_endpoint"
        )

    return OAuthServerDiscovery(
        resource=resource,
        authorization_endpoint=authorization_endpoint,
        token_endpoint=token_endpoint,
        registration_endpoint=registration_endpoint,
        revocation_endpoint=revocation_endpoint,
        scopes=scopes,
    )


def metadata_str(metadata: Mapping[str, str] | None, key: str) -> str:
    if not metadata:
        return ""
    value = metadata.get(key)
    return str(value).strip() if value else ""


# ── Flow helpers ───────────────────────────────────────────────────────────


def require_tokens(
    tokens: dict[str, Any],
    *,
    label: str,
    missing_message: str,
    require_refresh: bool = True,
) -> tuple[str, str, str]:
    access_token = str(tokens.get("access_token") or "")
    refresh_token = str(tokens.get("refresh_token") or "")
    token_type = str(tokens.get("token_type") or "Bearer")
    if not access_token:
        raise OAuthCredentialError(missing_message)
    if require_refresh and not refresh_token:
        raise OAuthCredentialError(missing_message)
    _ = label
    return access_token, refresh_token, token_type


def scopes_from_token(tokens: dict[str, Any], fallback: str = "") -> list[str]:
    return [s for s in str(tokens.get("scope") or fallback or "").replace(",", " ").split() if s]


def redirect_or_explicit(explicit: str, path_suffix: str) -> str:
    if explicit.strip():
        return explicit.strip()
    api_public_url = get_settings().app.api_public_url.strip().rstrip("/")
    if not api_public_url:
        raise OAuthCredentialError(f"API_PUBLIC_URL is required for OAuth ({path_suffix})")
    return f"{api_public_url}/v1/integrations/oauth/{path_suffix}/callback"


def static_config(
    *,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    label: str,
    id_env: str,
    secret_env: str,
) -> tuple[str, str, str]:
    missing = [
        name
        for name, value in (
            (id_env, client_id),
            (secret_env, client_secret),
            (f"{label} OAuth redirect URI", redirect_uri),
        )
        if not value
    ]
    if missing:
        raise OAuthCredentialError(f"{label} OAuth is not configured: {', '.join(missing)}")
    return client_id, client_secret, redirect_uri


def jwt_unverified_claims(token: str) -> dict[str, Any]:
    """Decode JWT payload without verifying signature (identity hints only)."""
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except (ValueError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}
