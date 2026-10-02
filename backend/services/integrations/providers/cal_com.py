"""Cal.com hosted MCP — constants and OAuth flow.

MCP URL: https://mcp.cal.com/mcp (Streamable HTTP)
Auth: OAuth 2.1 Authorization Code + PKCE, Dynamic Client Registration (RFC 7591).

**Not Cal.com's Platform OAuth.** ``mcp.cal.com`` is its own authorization
server, unrelated to the one behind ``app.cal.com`` / ``api.cal.com``, and it
rejects a platform token outright. This module used to run the platform flow —
``app.cal.com/auth/oauth2/authorize`` + ``api.cal.com/v2/auth/oauth2/token``
with ``PROFILE_READ``-style scopes — which minted a token that ``api.cal.com``
accepted (``/v2/me`` answered 200) and ``mcp.cal.com`` answered 401 to, every
time, including immediately after a successful refresh. Reconnecting could never
have fixed it: the two servers issue different tokens and only one of them opens
the MCP server.

Verified against the live discovery documents on 2026-08-28::

    GET https://mcp.cal.com/.well-known/oauth-protected-resource/mcp
      {"resource": "https://mcp.cal.com/mcp",
       "authorization_servers": ["https://mcp.cal.com"],
       "bearer_methods_supported": ["header"]}

    GET https://mcp.cal.com/.well-known/oauth-authorization-server
      authorization_endpoint  https://mcp.cal.com/oauth/authorize
      token_endpoint          https://mcp.cal.com/oauth/token
      registration_endpoint   https://mcp.cal.com/oauth/register
      revocation_endpoint     https://mcp.cal.com/oauth/revoke
      grant_types_supported   ["authorization_code", "refresh_token"]
      code_challenge_methods_supported       ["S256"]
      token_endpoint_auth_methods_supported  ["none"]   (public client + PKCE)

Note the ``/mcp`` suffix on the protected-resource URL: unlike RocketReach's,
Cal.com's metadata lives under the resource path, not at the host root.

No ``scopes_supported`` is advertised, so none is sent — the authorization
server decides what the grant covers.
"""

# Docs (MCP): https://cal.com/docs/mcp-server

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

CAL_COM_MCP_URL = "https://mcp.cal.com/mcp"
CAL_COM_MCP_RESOURCE = "https://mcp.cal.com/mcp"
CAL_COM_PROTECTED_RESOURCE_METADATA_URL = (
    "https://mcp.cal.com/.well-known/oauth-protected-resource/mcp"
)
CAL_COM_AUTHORIZATION_SERVER_FALLBACK = "https://mcp.cal.com"
CAL_COM_TOKEN_URL = "https://mcp.cal.com/oauth/token"
CAL_COM_REVOKE_URL = "https://mcp.cal.com/oauth/revoke"
CAL_COM_CLIENT_NAME = "Talqing"


def _cal_com_identity(access_token: str) -> tuple[dict[str, object], str, str | None]:
    """Whatever the token says about the account, which may be nothing.

    The old platform flow read ``api.cal.com/v2/me`` for a name, username and
    email. That endpoint is not open to this token — it belongs to a different
    authorization server — and there is no equivalent on ``mcp.cal.com``, so the
    token's own claims are what is left. A token carrying none still connects:
    ``provider_subject`` is nullable behind a partial uniqueness index, so a
    second connection lands beside the first and is told apart by name rather
    than refused.
    """
    claims = jwt_unverified_claims(access_token)
    subject = str(claims.get("sub") or claims.get("user_id") or "").strip() or None
    email = str(claims.get("email") or "").strip()
    # Deliberately no "Cal.com account" filler when the claims are empty, which
    # is what this token actually carries today. The dashboard's Account column
    # renders an absent identity as a dash; a placeholder would fill that column
    # with the provider's own name, which the Provider column already gives.
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


class CalComOAuthFlow:
    provider = "cal_com"
    label = "Cal.com"

    def redirect_uri(self) -> str:
        settings = get_settings()
        return redirect_or_explicit(settings.oauth.cal_com.redirect_uri, "cal_com")

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
                protected_resource_metadata_url=CAL_COM_PROTECTED_RESOURCE_METADATA_URL,
                default_resource=CAL_COM_MCP_RESOURCE,
                label=self.label,
                require_registration_endpoint=True,
                authorization_server_fallback=CAL_COM_AUTHORIZATION_SERVER_FALLBACK,
                default_token_endpoint=CAL_COM_TOKEN_URL,
                default_revocation_endpoint=CAL_COM_REVOKE_URL,
            )
            assert discovery.registration_endpoint is not None
            client_id = await get_or_register_dcr_client(
                client,
                provider=self.provider,
                label=self.label,
                registration_endpoint=discovery.registration_endpoint,
                redirect_uri=redirect_uri,
                client_name=CAL_COM_CLIENT_NAME,
            )

        scopes = discovery.scopes
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
        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "resource": discovery.resource,
        }
        # Cal.com advertises no `scopes_supported`; sending an empty `scope` is
        # not the same as omitting it, and some servers reject the empty string.
        if scopes:
            params["scope"] = " ".join(scopes)
        return authorize_redirect(discovery.authorization_endpoint, params)

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
            raise OAuthCredentialError("Cal.com OAuth state is incomplete. Try connecting again.")
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
                    "Cal.com did not return refreshable OAuth credentials. Try connecting again."
                ),
            )
        account_info, email, provider_subject = _cal_com_identity(access_token)

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
            display_name_base=f"Cal.com - {display}" if display else "Cal.com",
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
                "Cal.com OAuth client registration is missing. Reconnect the integration."
            )
        token_url = metadata_str(oauth_metadata, "token_endpoint") or CAL_COM_TOKEN_URL
        resource = metadata_str(oauth_metadata, "resource") or CAL_COM_MCP_RESOURCE
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
        revoke_url = metadata_str(oauth_metadata, "revocation_endpoint") or CAL_COM_REVOKE_URL
        try:
            async with oauth_http_client(timeout=10) as client:
                await client.post(revoke_url, data={"client_id": oauth_client_id, "token": token})
        except httpx.HTTPError:
            return
