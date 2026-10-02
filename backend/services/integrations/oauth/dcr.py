"""Dynamic Client Registration cache, held in the control plane.

Calendly (and Atlassian Rovo MCP) treat DCR ``client_id`` as a stable, long-lived
identifier. Register once per (provider, redirect_uri) and reuse for every tenant
connect.

``oauth_dcr_clients`` is a control-plane table, so a region reaches it over the
internal API rather than with a DSN. The key is (provider, redirect_uri), and
every region has its own redirect URI — so each region registers once and two
regions can never collide on a row. The table is shared; the rows are not.

Both calls are on the very first connect of one provider in one region, ever, so
the round trip is invisible.
"""

from __future__ import annotations

from typing import Any

import httpx

from services.control import client as control
from services.integrations.oauth.protocol import OAuthCredentialError, oauth_http_error


async def get_or_register_dcr_client(
    client: httpx.AsyncClient,
    *,
    provider: str,
    label: str,
    registration_endpoint: str,
    redirect_uri: str,
    client_name: str,
    registration_body: dict[str, Any] | None = None,
) -> str:
    """Return a stable DCR client_id for this deployment + redirect URI."""
    cached = await control.dcr_client_id(provider=provider, redirect_uri=redirect_uri)
    if cached:
        return cached

    body = {
        "client_name": client_name,
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }
    if registration_body:
        body.update(registration_body)

    response = await client.post(
        registration_endpoint,
        json=body,
        headers={"Accept": "application/json"},
    )
    if response.status_code >= 400:
        raise OAuthCredentialError(
            f"{label} Dynamic Client Registration failed: {oauth_http_error(response)}"
        )
    registration = response.json()
    if not isinstance(registration, dict):
        raise OAuthCredentialError(f"{label} DCR returned a non-object response")
    client_id = str(registration.get("client_id") or "").strip()
    if not client_id:
        raise OAuthCredentialError(f"{label} did not return an OAuth client id")

    await control.store_dcr_client(
        provider=provider,
        redirect_uri=redirect_uri,
        client_id=client_id,
        metadata={
            "client_name": client_name,
            "registration_endpoint": registration_endpoint,
            "scopes": registration.get("scopes"),
        },
    )
    return client_id
