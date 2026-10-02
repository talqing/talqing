"""HubSpot hosted MCP — constants, identity, and OAuth flow.

MCP URL: https://mcp.hubspot.com
Auth: MCP Auth App (static client id/secret) + OAuth 2.1 with PKCE required.
Refresh tokens are single-use (rotation); credentials layer stores the new token.
Scopes are determined by MCP tools + user grant at install time (discovery).
"""

# Docs: https://developers.hubspot.com/docs/apps/developer-platform/build-apps/integrate-with-the-remote-hubspot-mcp-server

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx

from settings import get_settings

from ..oauth.protocol import (
    OAuthConnectResult,
    OAuthCredentialError,
    RefreshParams,
    authorize_redirect,
    discover_oauth_server,
    make_pkce,
    metadata_str,
    oauth_http_client,
    oauth_http_error,
    recover_pkce_verifier,
    redirect_or_explicit,
    require_tokens,
    signed_oauth_state,
    static_config,
    token_expiry,
    utcnow,
)

HUBSPOT_MCP_URL = "https://mcp.hubspot.com/"
HUBSPOT_MCP_RESOURCE = "https://mcp.hubspot.com"
HUBSPOT_PROTECTED_RESOURCE_METADATA_URL = (
    "https://mcp.hubspot.com/.well-known/oauth-protected-resource"
)
HUBSPOT_TOKEN_URL = "https://mcp.hubspot.com/oauth/v3/token"
HUBSPOT_REVOKE_URL = "https://mcp.hubspot.com/oauth/v3/token/revoke"


def _hubspot_scopes(tokens: dict[str, Any], fallback: str | None = None) -> list[str]:
    raw_scopes = tokens.get("scopes")
    if isinstance(raw_scopes, list):
        return [str(scope) for scope in raw_scopes]
    return [s for s in str(tokens.get("scope") or fallback or "").replace(",", " ").split() if s]


def _hubspot_account_ids(*, tokens: dict[str, Any]) -> dict[str, object]:
    """Identity bag. Uniqueness key is portal (hub_id), not HubSpot user id.

    One integration per HubSpot portal per tenant.
    """
    hub_id = str(tokens.get("hub_id") or "").strip()
    user_id = str(tokens.get("user_id") or "").strip()
    # "Portal 12345" is a real identity — a HubSpot connection IS a portal.
    # Only the no-portal case is filler, and that gets nothing.
    account_name = f"Portal {hub_id}" if hub_id else ""
    # Prefer portal id for uniqueness; fall back to user when hub_id is absent.
    provider_subject = hub_id or user_id or None
    # `hub_id` and `user_id` used to be stored here too, read by nothing —
    # `account_id` already carries the portal and `provider_subject` keys on it.
    # They are outside OAUTH_ACCOUNT_KEYS, which made every patch of a HubSpot
    # row fail with "unknown config field" while connect kept succeeding.
    return {
        "account_name": account_name,
        "account_id": hub_id or None,
        "provider_subject": provider_subject,
        "connected_at": utcnow().isoformat(),
    }


class HubSpotOAuthFlow:
    provider = "hubspot"
    label = "HubSpot"

    def redirect_uri(self) -> str:
        settings = get_settings()
        return redirect_or_explicit(settings.oauth.hubspot.redirect_uri, "hubspot")

    def _static(self) -> tuple[str, str, str]:
        settings = get_settings()
        return static_config(
            client_id=settings.oauth.hubspot.client_id.strip(),
            client_secret=settings.oauth.hubspot.client_secret.strip(),
            redirect_uri=self.redirect_uri(),
            label=self.label,
            id_env="HUBSPOT_OAUTH_CLIENT_ID",
            secret_env="HUBSPOT_OAUTH_CLIENT_SECRET",
        )

    def is_configured(self) -> bool:
        try:
            self._static()
        except OAuthCredentialError:
            return False
        return True

    async def build_authorize_url(
        self,
        *,
        user_id: str,
        tenant_id: str,
        integration_id: str | None,
    ) -> str:
        client_id, _, redirect_uri = self._static()
        async with oauth_http_client() as client:
            discovery = await discover_oauth_server(
                client,
                protected_resource_metadata_url=HUBSPOT_PROTECTED_RESOURCE_METADATA_URL,
                default_resource=HUBSPOT_MCP_RESOURCE,
                label=self.label,
                default_token_endpoint=HUBSPOT_TOKEN_URL,
                default_revocation_endpoint=HUBSPOT_REVOKE_URL,
            )
        nonce, _, code_challenge = make_pkce(user_id, tenant_id, integration_id)
        state = signed_oauth_state(
            user_id=user_id,
            tenant_id=tenant_id,
            provider=self.provider,
            integration_id=integration_id,
            extra={
                "pkce_nonce": nonce,
                "redirect_uri": redirect_uri,
                "resource": discovery.resource,
                "token_endpoint": discovery.token_endpoint,
                "scopes": " ".join(discovery.scopes),
                **(
                    {"revocation_endpoint": discovery.revocation_endpoint}
                    if discovery.revocation_endpoint
                    else {}
                ),
            },
        )
        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "resource": discovery.resource,
        }
        if discovery.scopes:
            params["scope"] = " ".join(discovery.scopes)
        return authorize_redirect(discovery.authorization_endpoint, params)

    async def exchange_code(
        self,
        *,
        code: str,
        oauth_state: dict[str, str | None],
    ) -> OAuthConnectResult:
        client_id, client_secret, configured_redirect = self._static()
        redirect_uri = str(oauth_state.get("redirect_uri") or configured_redirect).strip()
        token_endpoint = str(oauth_state.get("token_endpoint") or "").strip()
        resource = str(oauth_state.get("resource") or "").strip()
        if not redirect_uri or not token_endpoint or not resource:
            raise OAuthCredentialError("HubSpot OAuth state is incomplete. Try connecting again.")
        code_verifier = recover_pkce_verifier(oauth_state)
        async with oauth_http_client() as client:
            token_response = await client.post(
                token_endpoint,
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "code": code,
                    "code_verifier": code_verifier,
                    "grant_type": "authorization_code",
                    "redirect_uri": redirect_uri,
                    "resource": resource,
                },
                headers={"Accept": "application/json"},
            )
            if token_response.status_code >= 400:
                raise OAuthCredentialError(oauth_http_error(token_response))
            tokens = token_response.json()
            if not isinstance(tokens, dict):
                raise OAuthCredentialError("HubSpot returned an invalid token response.")

        access_token, refresh_token, token_type = require_tokens(
            tokens,
            label=self.label,
            missing_message=(
                "HubSpot did not return refreshable OAuth credentials. Try connecting again."
            ),
        )
        scopes = _hubspot_scopes(tokens, str(oauth_state.get("scopes") or ""))
        account_ids = _hubspot_account_ids(tokens=tokens)
        account_name = str(account_ids.get("account_name") or "")
        provider_subject = account_ids.get("provider_subject")
        oauth_metadata = {
            "token_endpoint": token_endpoint,
            "resource": resource,
        }
        rev = str(oauth_state.get("revocation_endpoint") or "").strip()
        if rev:
            oauth_metadata["revocation_endpoint"] = rev
        return OAuthConnectResult(
            access_token=access_token,
            refresh_token=refresh_token,
            token_type=token_type,
            expires_at=token_expiry(tokens.get("expires_in"), default_seconds=1800),
            scopes=scopes,
            provider_account_info=account_ids,
            account_email="",
            provider_subject=str(provider_subject) if provider_subject else None,
            display_name_base=f"HubSpot - {account_name}" if account_name else "HubSpot",
            oauth_client_id=client_id,
            oauth_metadata=oauth_metadata,
        )

    def refresh_params(
        self,
        *,
        oauth_client_id: str | None,
        refresh_token: str,
        oauth_metadata: Mapping[str, str] | None = None,
    ) -> RefreshParams:
        _ = refresh_token
        client_id, client_secret, _ = self._static()
        token_url = metadata_str(oauth_metadata, "token_endpoint") or HUBSPOT_TOKEN_URL
        resource = metadata_str(oauth_metadata, "resource") or HUBSPOT_MCP_RESOURCE
        return RefreshParams(
            label=self.label,
            token_url=token_url,
            client_id=oauth_client_id or client_id,
            client_secret=client_secret,
            extra={"resource": resource},
        )

    async def revoke(
        self,
        *,
        access_token: str,
        refresh_token: str,
        oauth_client_id: str | None,
        oauth_metadata: Mapping[str, str] | None = None,
    ) -> None:
        token = refresh_token or access_token
        if not token:
            return
        try:
            client_id, client_secret, _ = self._static()
        except OAuthCredentialError:
            return
        revoke_url = metadata_str(oauth_metadata, "revocation_endpoint") or HUBSPOT_REVOKE_URL
        try:
            async with oauth_http_client(timeout=10) as client:
                await client.post(
                    revoke_url,
                    data={
                        "client_id": oauth_client_id or client_id,
                        "client_secret": client_secret,
                        "token": token,
                        "token_type_hint": "refresh_token",
                    },
                )
        except httpx.HTTPError:
            return
