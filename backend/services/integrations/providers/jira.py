"""Jira / Atlassian Rovo MCP — constants, identity, and OAuth flow.

Recommended MCP URL: https://mcp.atlassian.com/v1/mcp/authv2
Auth: OAuth 2.1 + PKCE + Dynamic Client Registration (public client).
"""

# Docs (setup): https://support.atlassian.com/atlassian-rovo-mcp-server/docs/getting-started-with-the-atlassian-remote-mcp-server/
# Docs (OAuth 2.1): https://support.atlassian.com/atlassian-rovo-mcp-server/docs/configuring-oauth-2-1/

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

# Atlassian recommends authv2 for OAuth clients (not the legacy /v1/mcp path).
JIRA_MCP_URL = "https://mcp.atlassian.com/v1/mcp/authv2"
JIRA_MCP_RESOURCE = "https://mcp.atlassian.com/v1/mcp/authv2"
JIRA_PROTECTED_RESOURCE_METADATA_URL = (
    "https://mcp.atlassian.com/.well-known/oauth-protected-resource/v1/mcp/authv2"
)
JIRA_CLIENT_NAME = "Talqing"
JIRA_SCOPES = [
    "read:me",
    "read:account",
    "offline_access",
    "read:jira-work",
    "write:jira-work",
]
ATLASSIAN_TOKEN_URL = "https://auth.atlassian.com/oauth/token"
ATLASSIAN_REVOKE_URL = "https://auth.atlassian.com/oauth/revoke"
ATLASSIAN_ME_URL = "https://api.atlassian.com/me"


async def _jira_identity(
    client: httpx.AsyncClient, access_token: str
) -> tuple[dict[str, object], str, str | None]:
    """Resolve Atlassian account identity for uniqueness + display."""
    try:
        response = await client.get(
            ATLASSIAN_ME_URL,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
            },
        )
        if response.status_code < 400:
            payload = response.json()
            if isinstance(payload, dict):
                account_id = str(payload.get("account_id") or "").strip()
                email = str(payload.get("email") or "").strip()
                name = str(payload.get("name") or payload.get("nickname") or "").strip()
                if account_id or email:
                    info: dict[str, object] = {
                        "account_email": email,
                        "account_name": name,
                        "account_id": account_id or None,
                        "provider_subject": account_id or email or None,
                        "connected_at": utcnow().isoformat(),
                    }
                    return (
                        info,
                        email,
                        str(info["provider_subject"]) if info["provider_subject"] else None,
                    )
    except httpx.HTTPError:
        pass

    claims = jwt_unverified_claims(access_token)
    subject = str(claims.get("sub") or claims.get("accountId") or "").strip() or None
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


def _jira_scopes(discovery_scopes: list[str]) -> list[str]:
    supported = set(discovery_scopes) if discovery_scopes else set()
    scopes = [scope for scope in JIRA_SCOPES if not supported or scope in supported]
    for required in JIRA_SCOPES:
        if required not in scopes:
            scopes.append(required)
    return scopes


class JiraOAuthFlow:
    provider = "jira"
    label = "Jira"

    def redirect_uri(self) -> str:
        settings = get_settings()
        return redirect_or_explicit(settings.oauth.jira.redirect_uri, "jira")

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
                protected_resource_metadata_url=JIRA_PROTECTED_RESOURCE_METADATA_URL,
                default_resource=JIRA_MCP_RESOURCE,
                label=self.label,
                require_registration_endpoint=True,
                default_token_endpoint=ATLASSIAN_TOKEN_URL,
                default_revocation_endpoint=ATLASSIAN_REVOKE_URL,
                default_scopes=JIRA_SCOPES,
            )
            scopes = _jira_scopes(discovery.scopes)
            assert discovery.registration_endpoint is not None
            client_id = await get_or_register_dcr_client(
                client,
                provider=self.provider,
                label=self.label,
                registration_endpoint=discovery.registration_endpoint,
                redirect_uri=redirect_uri,
                client_name=JIRA_CLIENT_NAME,
                registration_body={"scope": " ".join(scopes)},
            )

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
            raise OAuthCredentialError("Jira OAuth state is incomplete. Try connecting again.")
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
                    "Jira did not return refreshable OAuth credentials. Try connecting again."
                ),
            )
            account_info, email, provider_subject = await _jira_identity(client, access_token)

        scopes = scopes_from_token(tokens, str(oauth_state.get("scopes") or ""))
        display = email or str(account_info.get("account_name") or "")
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
            display_name_base=f"Jira - {display}" if display else "Jira",
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
                "Jira OAuth client registration is missing. Reconnect the integration."
            )
        token_url = metadata_str(oauth_metadata, "token_endpoint") or ATLASSIAN_TOKEN_URL
        resource = metadata_str(oauth_metadata, "resource") or JIRA_MCP_RESOURCE
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
        revoke_url = metadata_str(oauth_metadata, "revocation_endpoint") or ATLASSIAN_REVOKE_URL
        try:
            async with oauth_http_client(timeout=10) as client:
                await client.post(
                    revoke_url,
                    data={"client_id": oauth_client_id, "token": token},
                )
        except httpx.HTTPError:
            return
