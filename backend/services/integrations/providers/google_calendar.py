"""Google Calendar — constants, identity, and OAuth flow.

Auth: static OAuth web client + offline refresh tokens. The tools themselves are
ours, built on the Calendar REST API (``compiler.native.google_calendar``);
nothing about connecting differs because of that.
"""

# Docs: https://developers.google.com/workspace/calendar/api/v3/reference

from __future__ import annotations

from collections.abc import Mapping

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

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
GOOGLE_REVOKE_URL = "https://oauth2.googleapis.com/revoke"

# openid/email/profile: account identity for the dashboard.
# The four calendar scopes are the narrowest set that serves all nine tools —
# verified against the "Authorization scopes" block on each REST reference page:
#   calendarlist.readonly → calendarList.list
#   events.readonly       → events.list / events.get
#   events.freebusy       → freebusy.query
#   events.owned          → events.insert / patch / delete
# `events.owned` is "calendars you own", where `calendar.events` — the only write
# scope Google's own Calendar MCP server accepts — is "all your calendars".
# Widening it is a consent change that forces every tenant to reconnect, so it is
# a product decision and never a fix for a 403.
GOOGLE_CALENDAR_SCOPES = [
    "openid",
    "email",
    "profile",
    "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
    "https://www.googleapis.com/auth/calendar.events.freebusy",
    "https://www.googleapis.com/auth/calendar.events.readonly",
    "https://www.googleapis.com/auth/calendar.events.owned",
]


def _calendar_account_ids(
    *,
    email: str,
    provider_subject: str | None,
) -> dict[str, object]:
    return {
        "account_email": email,
        "provider_subject": provider_subject,
        "connected_at": utcnow().isoformat(),
    }


class GoogleCalendarOAuthFlow:
    provider = "google_calendar"
    label = "Google Calendar"

    def redirect_uri(self) -> str:
        settings = get_settings()
        return redirect_or_explicit(settings.oauth.google.redirect_uri, "google_calendar")

    def _static(self) -> tuple[str, str, str]:
        settings = get_settings()
        return static_config(
            client_id=settings.oauth.google.client_id.strip(),
            client_secret=settings.oauth.google.client_secret.strip(),
            redirect_uri=self.redirect_uri(),
            label=self.label,
            id_env="GOOGLE_OAUTH_CLIENT_ID",
            secret_env="GOOGLE_OAUTH_CLIENT_SECRET",
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
        return authorize_redirect(
            GOOGLE_AUTH_URL,
            {
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": " ".join(GOOGLE_CALENDAR_SCOPES),
                "access_type": "offline",
                "prompt": "consent",
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
        code_verifier = recover_pkce_verifier(oauth_state)
        async with oauth_http_client() as client:
            token_response = await client.post(
                GOOGLE_TOKEN_URL,
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "code": code,
                    "code_verifier": code_verifier,
                    "grant_type": "authorization_code",
                    "redirect_uri": redirect_uri,
                },
            )
            if token_response.status_code >= 400:
                raise OAuthCredentialError(oauth_http_error(token_response))
            tokens = token_response.json()
            # Re-consent may omit refresh_token; upsert keeps the previous one.
            is_reconnect = bool(oauth_state.get("integration_id"))
            access_token, refresh_token, token_type = require_tokens(
                tokens,
                label=self.label,
                missing_message="Google did not return offline access. Try connecting again.",
                require_refresh=not is_reconnect,
            )
            userinfo_response = await client.get(
                GOOGLE_USERINFO_URL,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if userinfo_response.status_code >= 400:
                raise OAuthCredentialError(oauth_http_error(userinfo_response))
            userinfo = userinfo_response.json()

        email = str(userinfo.get("email") or "").strip()
        provider_subject = str(userinfo.get("sub") or "").strip() or None
        if not email:
            raise OAuthCredentialError("Google account email was not returned")
        scopes = scopes_from_token(tokens, " ".join(GOOGLE_CALENDAR_SCOPES))
        return OAuthConnectResult(
            access_token=access_token,
            refresh_token=refresh_token,
            token_type=token_type,
            expires_at=token_expiry(tokens.get("expires_in")),
            scopes=scopes,
            provider_account_info=_calendar_account_ids(
                email=email, provider_subject=provider_subject
            ),
            account_email=email,
            provider_subject=provider_subject,
            display_name_base=f"Google Calendar - {email}",
            oauth_client_id=client_id,
            oauth_metadata={"token_endpoint": GOOGLE_TOKEN_URL},
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
        token_url = ((oauth_metadata or {}).get("token_endpoint") or GOOGLE_TOKEN_URL).strip()
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
        _ = oauth_client_id, oauth_metadata
        token = refresh_token or access_token
        if not token:
            return
        try:
            async with oauth_http_client(timeout=10) as client:
                await client.post(GOOGLE_REVOKE_URL, params={"token": token})
        except httpx.HTTPError:
            return
