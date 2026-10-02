"""Resend hosted MCP constants, and the one identity call the API allows."""

# Docs: https://resend.com/docs/mcp-server
# The API key travels as ``Authorization: Bearer re_…`` — there is no keyless
# mode, so credentials_ref is required on this provider.

from __future__ import annotations

from typing import Any

import httpx

RESEND_MCP_URL = "https://mcp.resend.com/mcp"
RESEND_API_BASE = "https://api.resend.com"


class ResendApiError(RuntimeError):
    """Resend rejected the API key, or could not be reached."""


# Resend has SIX domain states — `pending`, `verified`, `failed`, `not_started`,
# `partially_verified`, `partially_failed` — read off the enum in
# `resend/resend-openapi`, not from the one value a live account happened to
# return. Two of them can send: `verified`, and `partially_verified`, where some
# records (a second region, DMARC) are still outstanding but mail goes out. A
# rule that matched only `verified` would rank a domain that sends below one
# that cannot.
_DOMAIN_RANK = {"verified": 2, "partially_verified": 1}


def _account_name(domains: list[dict[str, Any]]) -> str:
    """What to call an account that is identified only by what it sends as.

    Ranked rather than filtered: an account mid-setup has nothing but `pending`
    domains and still deserves a name on the row. Ties keep Resend's own order.

    The `+N` is not decoration. Resend accounts routinely hold several domains,
    and which of them is "the" one is our arbitrary choice — so an account with
    five rendered as a bare `talqing.com` would read as an account with one.
    """
    named = [d for d in domains if str(d.get("name") or "")]
    if not named:
        return ""
    best = max(named, key=lambda d: _DOMAIN_RANK.get(str(d.get("status") or ""), 0))
    others = len(named) - 1
    name = str(best["name"])
    return f"{name} +{others}" if others else name


async def fetch_account_identity(api_key: str) -> dict[str, str]:
    """Identify a Resend account by the domain it sends from.

    Resend publishes no ``/me``, ``/account``, ``/team`` or ``/users/me`` — the
    whole API is resources rather than identity, checked against
    ``resend/resend-openapi`` and confirmed live (all four answer 405). Its
    sending domain is the closest thing to a name the account has, and it is the
    more useful fact anyway: a Resend connection is only worth as much as the
    domain it can send as.

    Resend has no team resource either, despite Teams existing in its dashboard:
    the word appears once in the whole spec, in prose. An API key belongs to a
    team and the API never says which, so there is nothing better to ask for.

    An account with no domain is legitimate — Resend lets it send from
    ``onboarding@resend.dev`` to the signup address only — so that is an empty
    name, not an error. A rejected key IS an error: this call is the one moment
    we can tell the tenant their key is wrong, rather than letting them discover
    it when an agent first tries to send.
    """
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(
                f"{RESEND_API_BASE}/domains",
                headers={"Authorization": f"Bearer {api_key}"},
            )
    except httpx.RequestError as exc:
        raise ResendApiError(f"could not reach Resend: {exc}") from exc

    if not response.is_success:
        # A bad key is a 400 with {"message": "API key is invalid"}, NOT a 401 —
        # measured, and the opposite of what the status code convention implies.
        # 401 is reserved for a missing header. So the code cannot be the story,
        # and Resend's own sentence beats anything we would write over it.
        detail = ""
        try:
            body = response.json()
        except ValueError:
            body = None
        if isinstance(body, dict):
            detail = str(body.get("message") or "")
        raise ResendApiError(
            f"Resend: {detail}" if detail else f"Resend returned HTTP {response.status_code}"
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise ResendApiError("Resend returned invalid JSON listing domains") from exc
    if not isinstance(payload, dict):
        raise ResendApiError("Resend returned a non-object response listing domains")

    data = payload.get("data")
    domains = [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []
    # `account_name` rather than a key of its own: it is what the dashboard's
    # Account column reads for every provider, and a domain is this account's
    # name in the only sense Resend offers one.
    return {"account_name": _account_name(domains)}
