"""Calendly hosted MCP — constants, identity, and OAuth flow.

MCP URL: https://mcp.calendly.com
Auth: OAuth 2.1 Authorization Code + PKCE, Dynamic Client Registration (RFC 7591).
client_id is stable/long-lived — cached deployment-wide (not re-registered per connect).
"""

# Docs: https://developer.calendly.com/calendly-mcp-server

from __future__ import annotations

import json
from collections.abc import Mapping

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import TextContent

from settings import get_settings

from ..oauth.dcr import get_or_register_dcr_client
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
    scopes_from_token,
    signed_oauth_state,
    token_expiry,
    utcnow,
)

CALENDLY_MCP_URL = "https://mcp.calendly.com"
CALENDLY_MCP_RESOURCE = "https://mcp.calendly.com/"
CALENDLY_TOKEN_URL = "https://calendly.com/oauth/token"
CALENDLY_PROTECTED_RESOURCE_METADATA_URL = (
    "https://mcp.calendly.com/.well-known/oauth-protected-resource"
)
CALENDLY_AUTHORIZATION_SERVER_FALLBACK = "https://calendly.com"
CALENDLY_CLIENT_NAME = "Talqing"
CALENDLY_SCOPES = ["mcp:scheduling:read", "mcp:scheduling:write"]
CALENDLY_CURRENT_USER_TOOL = "users-get_current_user"
CALENDLY_REVOKE_URL = "https://auth.calendly.com/oauth/revoke"


async def _calendly_identity(access_token: str) -> tuple[dict[str, object], str, str]:
    """The connected account, for uniqueness + display.

    Read through the MCP server's own `users-get_current_user` tool: the token is
    scoped `mcp:scheduling:*`, so REST `/users/me` answers 403 (needs
    `users:read`), and the JWT carries no email or name.
    """
    async with (
        httpx.AsyncClient(
            http2=True, timeout=15, headers={"Authorization": f"Bearer {access_token}"}
        ) as http_client,
        streamable_http_client(CALENDLY_MCP_URL, http_client=http_client) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        result = await session.call_tool(CALENDLY_CURRENT_USER_TOOL, {})
    if result.isError or not result.content or not isinstance(result.content[0], TextContent):
        raise OAuthCredentialError("Calendly did not return the connected account. Try again.")
    resource = json.loads(result.content[0].text)["resource"]
    email = resource["email"]
    # User URIs look like https://api.calendly.com/users/<uuid>.
    subject = resource["uri"].rsplit("/", 1)[-1]
    return (
        {
            "account_email": email,
            "account_name": resource["name"],
            "provider_subject": subject,
            "connected_at": utcnow().isoformat(),
        },
        email,
        subject,
    )


class CalendlyOAuthFlow:
    provider = "calendly"
    label = "Calendly"

    def redirect_uri(self) -> str:
        settings = get_settings()
        return redirect_or_explicit(settings.oauth.calendly.redirect_uri, "calendly")

    def is_configured(self) -> bool:
        try:
            self.redirect_uri()
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
        redirect_uri = self.redirect_uri()
        async with oauth_http_client() as client:
            discovery = await discover_oauth_server(
                client,
                protected_resource_metadata_url=CALENDLY_PROTECTED_RESOURCE_METADATA_URL,
                default_resource=CALENDLY_MCP_RESOURCE,
                label=self.label,
                require_registration_endpoint=True,
                authorization_server_fallback=CALENDLY_AUTHORIZATION_SERVER_FALLBACK,
                default_token_endpoint=CALENDLY_TOKEN_URL,
                default_scopes=CALENDLY_SCOPES,
            )
            assert discovery.registration_endpoint is not None
            client_id = await get_or_register_dcr_client(
                client,
                provider=self.provider,
                label=self.label,
                registration_endpoint=discovery.registration_endpoint,
                redirect_uri=redirect_uri,
                client_name=CALENDLY_CLIENT_NAME,
            )

        scopes = discovery.scopes or CALENDLY_SCOPES
        nonce, _, code_challenge = make_pkce(user_id, tenant_id, integration_id)
        state = signed_oauth_state(
            user_id=user_id,
            tenant_id=tenant_id,
            provider=self.provider,
            integration_id=integration_id,
            extra={
                "client_id": client_id,
                "pkce_nonce": nonce,
                "redirect_uri": redirect_uri,
                "resource": discovery.resource,
                "token_endpoint": discovery.token_endpoint,
                "scopes": " ".join(scopes),
                **(
                    {"revocation_endpoint": discovery.revocation_endpoint}
                    if discovery.revocation_endpoint
                    else {}
                ),
            },
        )
        return authorize_redirect(
            discovery.authorization_endpoint,
            {
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": " ".join(scopes),
                "state": state,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
                "resource": discovery.resource,
            },
        )

    async def exchange_code(
        self,
        *,
        code: str,
        oauth_state: dict[str, str | None],
    ) -> OAuthConnectResult:
        client_id = str(oauth_state.get("client_id") or "").strip()
        redirect_uri = str(oauth_state.get("redirect_uri") or "").strip()
        token_endpoint = str(oauth_state.get("token_endpoint") or "").strip()
        resource = str(oauth_state.get("resource") or "").strip()
        if not client_id or not redirect_uri or not token_endpoint or not resource:
            raise OAuthCredentialError("Calendly OAuth state is incomplete. Try connecting again.")
        code_verifier = recover_pkce_verifier(oauth_state)
        async with oauth_http_client() as client:
            token_response = await client.post(
                token_endpoint,
                data={
                    "client_id": client_id,
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
            access_token, refresh_token, token_type = require_tokens(
                tokens,
                label=self.label,
                missing_message=(
                    "Calendly did not return refreshable OAuth credentials. Try connecting again."
                ),
            )
            account_info, email, provider_subject = await _calendly_identity(access_token)

        scopes = scopes_from_token(tokens, str(oauth_state.get("scopes") or ""))
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
            expires_at=token_expiry(tokens.get("expires_in")),
            scopes=scopes,
            provider_account_info=account_info,
            account_email=email,
            provider_subject=provider_subject,
            display_name_base=f"Calendly - {email}",
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
        if not oauth_client_id:
            raise OAuthCredentialError(
                "Calendly OAuth client registration is missing. Reconnect the integration."
            )
        token_url = metadata_str(oauth_metadata, "token_endpoint") or CALENDLY_TOKEN_URL
        resource = metadata_str(oauth_metadata, "resource") or CALENDLY_MCP_RESOURCE
        return RefreshParams(
            label=self.label,
            token_url=token_url,
            client_id=oauth_client_id,
            client_secret=None,
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
        if not token or not oauth_client_id:
            return
        revoke_url = metadata_str(oauth_metadata, "revocation_endpoint") or CALENDLY_REVOKE_URL
        try:
            async with oauth_http_client(timeout=10) as client:
                await client.post(
                    revoke_url,
                    data={"client_id": oauth_client_id, "token": token},
                )
        except httpx.HTTPError:
            return
