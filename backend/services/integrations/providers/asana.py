"""Asana hosted MCP — constants, identity, and OAuth flow.

MCP URL: https://mcp.asana.com/v2/mcp
Auth: pre-registered MCP app (client id/secret). DCR is not supported.
Authorize with resource=https://mcp.asana.com/v2 and PKCE; do not send scope.
"""

# Docs: https://developers.asana.com/docs/integrating-with-asanas-mcp-server

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
    make_pkce,
    oauth_http_client,
    oauth_http_error,
    recover_pkce_verifier,
    redirect_or_explicit,
    require_tokens,
    scopes_from_token,
    signed_oauth_state,
    static_config,
    token_expiry,
    utcnow,
)

ASANA_MCP_URL = "https://mcp.asana.com/v2/mcp"
ASANA_MCP_RESOURCE = "https://mcp.asana.com/v2"
ASANA_AUTH_URL = "https://app.asana.com/-/oauth_authorize"
ASANA_TOKEN_URL = "https://app.asana.com/-/oauth_token"
ASANA_REVOKE_URL = "https://app.asana.com/-/oauth_revoke"


def _asana_account_ids(*, profile: dict[str, Any] | None) -> dict[str, object]:
    profile = profile or {}
    provider_subject = str(profile.get("gid") or profile.get("id") or "").strip() or None
    return {
        "account_email": str(profile.get("email") or ""),
        "account_name": str(profile.get("name") or ""),
        "provider_subject": provider_subject,
        "connected_at": utcnow().isoformat(),
    }


class AsanaOAuthFlow:
    provider = "asana"
    label = "Asana"

    def redirect_uri(self) -> str:
        settings = get_settings()
        return redirect_or_explicit(settings.oauth.asana.redirect_uri, "asana")

    def _static(self) -> tuple[str, str, str]:
        settings = get_settings()
        return static_config(
            client_id=settings.oauth.asana.client_id.strip(),
            client_secret=settings.oauth.asana.client_secret.strip(),
            redirect_uri=self.redirect_uri(),
            label=self.label,
            id_env="ASANA_OAUTH_CLIENT_ID",
            secret_env="ASANA_OAUTH_CLIENT_SECRET",
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
        nonce, _, code_challenge = make_pkce(user_id, tenant_id, integration_id)
        state = signed_oauth_state(
            user_id=user_id,
            tenant_id=tenant_id,
            provider=self.provider,
            integration_id=integration_id,
            extra={"pkce_nonce": nonce, "redirect_uri": redirect_uri},
        )
        # MCP apps: do not send scope (Asana returns "Invalid scope(s) requested").
        return authorize_redirect(
            ASANA_AUTH_URL,
            {
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "resource": ASANA_MCP_RESOURCE,
                "state": state,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            },
        )

    async def exchange_code(
        self,
        *,
        code: str,
        oauth_state: dict[str, str | None],
    ) -> OAuthConnectResult:
        client_id, client_secret, configured_redirect = self._static()
        redirect_uri = str(oauth_state.get("redirect_uri") or configured_redirect).strip()
        if not redirect_uri:
            raise OAuthCredentialError("Asana OAuth state is incomplete. Try connecting again.")
        code_verifier = recover_pkce_verifier(oauth_state)
        async with oauth_http_client() as client:
            token_response = await client.post(
                ASANA_TOKEN_URL,
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "code": code,
                    "code_verifier": code_verifier,
                    "grant_type": "authorization_code",
                    "redirect_uri": redirect_uri,
                },
                headers={"Accept": "application/json"},
            )
            if token_response.status_code >= 400:
                raise OAuthCredentialError(oauth_http_error(token_response))
            tokens = token_response.json()

        access_token, refresh_token, token_type = require_tokens(
            tokens,
            label=self.label,
            missing_message=(
                "Asana did not return refreshable OAuth credentials. Try connecting again."
            ),
        )
        profile = tokens.get("data") if isinstance(tokens.get("data"), dict) else None
        account_ids = _asana_account_ids(profile=profile)
        email = str(account_ids.get("account_email") or "")
        provider_subject = account_ids.get("provider_subject")
        label = email or str(account_ids.get("account_name") or "")
        return OAuthConnectResult(
            access_token=access_token,
            refresh_token=refresh_token,
            token_type=token_type,
            expires_at=token_expiry(tokens.get("expires_in")),
            scopes=scopes_from_token(tokens),
            provider_account_info=account_ids,
            account_email=email,
            provider_subject=str(provider_subject) if provider_subject else None,
            display_name_base=f"Asana - {label}" if label else "Asana",
            oauth_client_id=client_id,
            oauth_metadata={
                "token_endpoint": ASANA_TOKEN_URL,
                "resource": ASANA_MCP_RESOURCE,
                "revocation_endpoint": ASANA_REVOKE_URL,
            },
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
        token_url = ((oauth_metadata or {}).get("token_endpoint") or ASANA_TOKEN_URL).strip()
        return RefreshParams(
            label=self.label,
            token_url=token_url,
            client_id=oauth_client_id or client_id,
            client_secret=client_secret,
        )

    async def revoke(
        self,
        *,
        access_token: str,
        refresh_token: str,
        oauth_client_id: str | None,
        oauth_metadata: Mapping[str, str] | None = None,
    ) -> None:
        _ = oauth_client_id
        token = refresh_token or access_token
        if not token:
            return
        try:
            client_id, client_secret, _ = self._static()
        except OAuthCredentialError:
            return
        revoke_url = ((oauth_metadata or {}).get("revocation_endpoint") or ASANA_REVOKE_URL).strip()
        try:
            async with oauth_http_client(timeout=10) as client:
                await client.post(
                    revoke_url,
                    data={
                        "client_id": client_id,
                        "client_secret": client_secret,
                        "token": token,
                    },
                )
        except httpx.HTTPError:
            return
