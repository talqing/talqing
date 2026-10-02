"""RocketReach hosted MCP — constants, identity, and OAuth flow.

MCP URL: https://mcp.rocketreach.co/mcp (Streamable HTTP)
Auth: OAuth 2.1 Authorization Code + PKCE, Dynamic Client Registration (RFC 7591).
There is **no API-key mode** — DCR is the only way in, which is why this is one
provider module rather than a `credentials_ref` on the catalog entry.

Verified against the live discovery documents on 2026-08-23::

    GET https://mcp.rocketreach.co/.well-known/oauth-protected-resource
      {"resource": "https://mcp.rocketreach.co",
       "authorization_servers": ["https://rocketreach.co"]}

    GET https://rocketreach.co/.well-known/oauth-authorization-server
      authorization_endpoint  https://rocketreach.co/mcp-oauth/authorize
      token_endpoint          https://rocketreach.co/mcp-oauth/token
      registration_endpoint   https://rocketreach.co/mcp-oauth/register
      revocation_endpoint     https://rocketreach.co/mcp-oauth/revoke
      scopes_supported        ["rocketreach:read"]
      grant_types_supported   ["authorization_code", "refresh_token"]
      token_endpoint_auth_methods_supported  ["none"]   (public client + PKCE)

Every host here — ``rocketreach.co`` and ``mcp.rocketreach.co`` — is behind a
Cloudflare rule that answers an **HTTP/1.1** request with a "Just a moment..."
challenge page and a 403, whatever the User-Agent. Only HTTP/2 gets a response,
which is why the flow below and the MCP client both go out over HTTP/2 (see
``oauth.protocol.oauth_http_client`` and ``compiler.integrations._MCPServerHTTP``).

**Its lookup tools spend export credits** — `person_lookup`, `company_lookup`
and `profile_company_lookup` cost 1–3 each, while `person_search`,
`company_search` and `account` are free. A task that looks a person up for every
row of a five-thousand-row list spends five thousand times that, and nothing in
Talqing caps it. The catalog description says so, because an invoice is a bad
place to learn it.
"""

# Docs: https://docs.rocketreach.co/reference/mcp

from __future__ import annotations

from collections.abc import Mapping

import httpx

from settings import get_settings

from ..oauth.dcr import get_or_register_dcr_client
from ..oauth.protocol import (
    OAuthConnectResult,
    OAuthCredentialError,
    RefreshParams,
    authorize_redirect,
    discover_oauth_server,
    jwt_unverified_claims,
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

ROCKETREACH_MCP_URL = "https://mcp.rocketreach.co/mcp"
ROCKETREACH_MCP_RESOURCE = "https://mcp.rocketreach.co"
ROCKETREACH_PROTECTED_RESOURCE_METADATA_URL = (
    "https://mcp.rocketreach.co/.well-known/oauth-protected-resource"
)
ROCKETREACH_AUTHORIZATION_SERVER_FALLBACK = "https://rocketreach.co"
ROCKETREACH_TOKEN_URL = "https://rocketreach.co/mcp-oauth/token"
ROCKETREACH_REVOKE_URL = "https://rocketreach.co/mcp-oauth/revoke"
ROCKETREACH_CLIENT_NAME = "Talqing"
ROCKETREACH_SCOPES = ["rocketreach:read"]


def _rocketreach_identity(access_token: str) -> tuple[dict[str, object], str, str | None]:
    """Account identity for uniqueness + display, read from the token itself.

    Unlike Calendly and Jira there is no REST endpoint this OAuth token opens —
    RocketReach's own v2 API authenticates with an API key, not a bearer token,
    and the free `account` MCP tool would mean standing up an MCP session inside
    the connect flow. The token's own claims are what is left, and a token that
    carries none still connects: `provider_subject` is nullable and its
    uniqueness index is partial, so the workspace simply gets two connections it
    can tell apart by name rather than a failed connect.
    """
    claims = jwt_unverified_claims(access_token)
    subject = str(claims.get("sub") or claims.get("user_id") or "").strip() or None
    email = str(claims.get("email") or "").strip()
    name = str(claims.get("name") or "").strip()
    return (
        {
            "account_email": email,
            "account_name": name,
            "provider_subject": subject,
            "connected_at": utcnow().isoformat(),
        },
        email,
        subject,
    )


class RocketReachOAuthFlow:
    provider = "rocketreach"
    label = "RocketReach"

    def redirect_uri(self) -> str:
        settings = get_settings()
        return redirect_or_explicit(settings.oauth.rocketreach.redirect_uri, "rocketreach")

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
                protected_resource_metadata_url=ROCKETREACH_PROTECTED_RESOURCE_METADATA_URL,
                default_resource=ROCKETREACH_MCP_RESOURCE,
                label=self.label,
                require_registration_endpoint=True,
                authorization_server_fallback=ROCKETREACH_AUTHORIZATION_SERVER_FALLBACK,
                default_token_endpoint=ROCKETREACH_TOKEN_URL,
                default_revocation_endpoint=ROCKETREACH_REVOKE_URL,
                default_scopes=ROCKETREACH_SCOPES,
            )
            assert discovery.registration_endpoint is not None
            client_id = await get_or_register_dcr_client(
                client,
                provider=self.provider,
                label=self.label,
                registration_endpoint=discovery.registration_endpoint,
                redirect_uri=redirect_uri,
                client_name=ROCKETREACH_CLIENT_NAME,
            )

        scopes = discovery.scopes or ROCKETREACH_SCOPES
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
            raise OAuthCredentialError(
                "RocketReach OAuth state is incomplete. Try connecting again."
            )
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
                    "RocketReach did not return refreshable OAuth credentials. "
                    "Try connecting again."
                ),
            )
        account_info, email, provider_subject = _rocketreach_identity(access_token)

        scopes = scopes_from_token(tokens, str(oauth_state.get("scopes") or ""))
        display = email or str(account_info.get("account_name") or "")
        oauth_metadata = {"token_endpoint": token_endpoint, "resource": resource}
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
            display_name_base=f"RocketReach - {display}" if display else "RocketReach",
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
                "RocketReach OAuth client registration is missing. Reconnect the integration."
            )
        token_url = metadata_str(oauth_metadata, "token_endpoint") or ROCKETREACH_TOKEN_URL
        resource = metadata_str(oauth_metadata, "resource") or ROCKETREACH_MCP_RESOURCE
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
        revoke_url = metadata_str(oauth_metadata, "revocation_endpoint") or ROCKETREACH_REVOKE_URL
        try:
            async with oauth_http_client(timeout=10) as client:
                await client.post(revoke_url, data={"client_id": oauth_client_id, "token": token})
        except httpx.HTTPError:
            return
