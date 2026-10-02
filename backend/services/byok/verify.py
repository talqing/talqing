"""Prove a provider key works before it is stored.

``set_provider_key`` calls the provider once with the key an admin just pasted
and refuses to write the row unless the provider accepts it. That keeps "this
workspace has a key for OpenAI" and "this workspace can run an OpenAI agent" the
same statement, which is what agent publish validation already assumes when it
checks only that a row exists (``services.agents.validate``). A key that is
never stored also never has to be un-stored.

Every call below was picked to be free and to be about the *credential* rather
than about a model: a key-info read where the provider publishes one, an
account-scoped list otherwise. Sarvam is the one exception. It sells nothing but
inference, and its only unauthenticated-looking endpoint, ``GET /v1/models``,
answers identically with a valid key, a garbage key and no key at all (measured
2026-08-17) — so it proves nothing. Its verifier pays for one character of
language identification instead: ₹3.5 per 10 000 characters, i.e. ₹0.00035 for
each key saved.

Four providers name the account behind the key and seven say nothing about it, so
``VerifiedAccount.label`` is ``""`` more often than not and every surface that
shows it has to read as finished without it.

Two verifiers do more than prove the key works, because two providers issue keys
that are real, answer, and still cannot run an agent: xAI reports a key it has
blocked, and OpenRouter issues management and provisioning keys alongside
inference ones. Both are refused here rather than stored to fail on a live turn.

A provider with no verifier here is stored unchecked rather than refused, so
adding one to catalog.yaml never waits on this file.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx
from fastapi import HTTPException

from services.catalog import get_catalog
from services.catalog.voices.anam import ANAM_API_URL

logger = logging.getLogger("talqing.services.byok")

# A human pasted a key and is watching a spinner: long enough for a cold
# provider edge, short enough that a stuck provider is not a stuck dashboard.
VERIFY_TIMEOUT_SECONDS = 10.0

# ai-coustics ships a license key, not an API key: a signed
# "<base64 payload>.<signature>" whose payload carries the API key their token
# endpoint authenticates. The SDK wants the whole license (that is what
# `compiler.factories` stores and passes), so we decode a copy to verify and
# keep the original.
AICOUSTICS_TOKEN_URL = "https://api.ai-coustics.io/v1/sdk/tokens"
# The OpenAI-compatible surface, which is the one our agents actually run on
# (`catalog.yaml` points every Gemini entry at it through `base_url`).
GEMINI_MODELS_URL = "https://generativelanguage.googleapis.com/v1beta/openai/models"


@dataclass(frozen=True)
class VerifiedAccount:
    """What the provider said about the key we just proved works.

    ``label`` is whatever that provider calls the account behind the key — the
    key's own name at xAI, a person at ElevenLabs and Deepgram, the API account
    at OpenAI. Most providers publish nothing of the sort, and an empty label
    means exactly that: nothing was hidden and nothing failed.
    """

    label: str = ""


def _rejected(provider: str, status: int) -> HTTPException:
    """The provider refused the key. 4xx covers more than 401 here: xAI and
    Gemini both answer a bad key with 400, and a 403 is a real key without the
    permissions an agent will need — all of them mean this key cannot run."""
    return HTTPException(
        status_code=400,
        detail=f"{_provider_label(provider)} rejected this API key (HTTP {status}).",
    )


def _unreachable(provider: str) -> HTTPException:
    """The provider did not answer. Deliberately not stored-anyway: a key we
    could not check is a key we cannot promise anything about, and a retry
    costs the admin one click."""
    return HTTPException(
        status_code=502,
        detail=f"Could not reach {_provider_label(provider)} to verify this API key. Try again.",
    )


def _provider_label(provider: str) -> str:
    """The catalog's display name — "ElevenLabs", not "elevenlabs". These
    messages go to a person."""
    return get_catalog().providers[provider].label


def _text(payload: dict, field: str) -> str:
    """A display field off a verified response.

    Only ever reached after the provider has already accepted the key, so a
    missing or non-string value is a provider that stopped publishing an
    optional name — not a failure, and not something to guess around.
    """
    value = payload.get(field)
    return value.strip() if isinstance(value, str) else ""


async def _request(
    provider: str,
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
    json_body: dict | None = None,
    auth: httpx.Auth | None = None,
) -> dict:
    """One verification request, with the whole point of it in the status code.

    The request is a constant, so a 4xx can only be about the credential in it.
    Anything else — a transport failure, a 5xx — is the provider having a bad
    day and must not be reported to the admin as a bad key.
    """
    try:
        async with httpx.AsyncClient(timeout=VERIFY_TIMEOUT_SECONDS) as client:
            response = await client.request(
                method, url, headers=headers, params=params, json=json_body, auth=auth
            )
    except httpx.HTTPError as exc:
        logger.warning("%s key verification could not reach the provider: %s", provider, exc)
        raise _unreachable(provider) from exc

    if response.status_code >= 500:
        logger.warning(
            "%s key verification got HTTP %s: %s",
            provider,
            response.status_code,
            response.text[:500],
        )
        raise _unreachable(provider)
    if response.status_code >= 400:
        logger.info(
            "%s refused a key with HTTP %s: %s",
            provider,
            response.status_code,
            response.text[:500],
        )
        raise _rejected(provider, response.status_code)
    return response.json()


# ── one verifier per catalog provider ───────────────────────────────────────


async def _verify_openai(api_key: str) -> VerifiedAccount:
    """GET /v1/me — the account the key bills to, by name.

    Undocumented (the published reference has no whoami; `/v1/organization/*`
    needs an admin key with `api.management.read`, which a normal project or
    service-account key does not carry). Taken deliberately, so an OpenAI tile
    can say whose account it is: if OpenAI retires it we will see every OpenAI
    key save start failing at once, which is the loud way to find out.
    """
    payload = await _request(
        "openai",
        "GET",
        "https://api.openai.com/v1/me",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    return VerifiedAccount(label=_text(payload, "name"))


async def _verify_xai(api_key: str) -> VerifiedAccount:
    """GET /v1/api-key — xAI's own description of the key being saved.

    A key xAI still recognizes but will not serve reads as valid on the status
    code alone, so the flags are checked as carefully as it is: storing one would
    look connected and fail on the first turn.
    """
    payload = await _request(
        "xai",
        "GET",
        "https://api.x.ai/v1/api-key",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    blocked = {
        "api_key_blocked": "this key is blocked",
        "api_key_disabled": "this key is disabled",
        "team_blocked": "the team it belongs to is blocked",
    }
    for field, reason in blocked.items():
        if payload.get(field) is True:
            raise HTTPException(
                status_code=400,
                detail=f"xAI accepted this API key but {reason}.",
            )
    return VerifiedAccount(label=_text(payload, "name"))


async def _verify_gemini(api_key: str) -> VerifiedAccount:
    """GET /v1beta/openai/models — free, and authenticated (a bad key is a 400).

    Google publishes no account read for a Generative Language API key: the key
    is a project credential with no name of its own.
    """
    await _request(
        "gemini",
        "GET",
        GEMINI_MODELS_URL,
        headers={"Authorization": f"Bearer {api_key}"},
    )
    return VerifiedAccount()


async def _verify_sarvam(api_key: str) -> VerifiedAccount:
    """POST /text-lid on a single character — the cheapest authenticated call
    Sarvam has. See the module docstring for why this one is not free."""
    await _request(
        "sarvam",
        "POST",
        "https://api.sarvam.ai/text-lid",
        headers={"api-subscription-key": api_key},
        json_body={"input": "a"},
    )
    return VerifiedAccount()


async def _verify_openrouter(api_key: str) -> VerifiedAccount:
    """GET /v1/key — what OpenRouter says about the key being saved.

    Two of the three key kinds OpenRouter issues would pass a bare status check
    and must not be stored. A *management* key reads and writes the account
    itself and a *provisioning* key mints and revokes other keys; neither runs
    inference, and holding one on a tenant's behalf is a far larger thing than
    holding an inference key. Refused for the same reason `_verify_xai` refuses
    a blocked key: storing it would look connected and behave wrongly.

    The balance is deliberately NOT checked. Measured: a key reporting no credit
    still serves paid models, because an account can be postpaid or draw on an
    organization's pool — so refusing on balance would reject a working key.
    `data.label` is a masked copy of the key itself ("sk-or-v1-dbe...271"), not
    the name of anything, so the tile reads as finished without one.
    """
    payload = await _request(
        "openrouter",
        "GET",
        "https://openrouter.ai/api/v1/key",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    data = payload.get("data") or {}
    wrong_kind = {
        "is_management_key": "a management key, which administers the account rather than "
        "running inference",
        "is_provisioning_key": "a provisioning key, which can mint and revoke other API keys",
    }
    for field, description in wrong_kind.items():
        if data.get(field) is True:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"This is {description}. Paste an inference key from "
                    "openrouter.ai/settings/keys instead."
                ),
            )
    return VerifiedAccount()


async def _verify_raya(api_key: str) -> VerifiedAccount:
    """GET /v1/voices — the same roster the voice picker browses on this key."""
    await _request(
        "raya",
        "GET",
        "https://hub.getraya.app/v1/voices",
        headers={"X-API-Key": api_key},
    )
    return VerifiedAccount()


async def _verify_soniox(api_key: str) -> VerifiedAccount:
    """GET /v1/voices — the account's own cloned voices, so it is scoped to the
    key rather than to a public catalog. Usually an empty list, which is a pass."""
    await _request(
        "soniox",
        "GET",
        "https://api.soniox.com/v1/voices",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    return VerifiedAccount()


async def _verify_elevenlabs(api_key: str) -> VerifiedAccount:
    """GET /v1/user — the subscriber behind the key."""
    payload = await _request(
        "elevenlabs",
        "GET",
        "https://api.elevenlabs.io/v1/user",
        headers={"xi-api-key": api_key},
    )
    return VerifiedAccount(label=_text(payload, "first_name"))


async def _verify_deepgram(api_key: str) -> VerifiedAccount:
    """GET /v1/auth/token — who the key belongs to and what it may do."""
    payload = await _request(
        "deepgram",
        "GET",
        "https://api.deepgram.com/v1/auth/token",
        headers={"Authorization": f"Token {api_key}"},
    )
    name = f"{_text(payload, 'first_name')} {_text(payload, 'last_name')}".strip()
    return VerifiedAccount(label=name or _text(payload, "email"))


async def _verify_anam(api_key: str) -> VerifiedAccount:
    """GET /v1/avatars — one page of one, the smallest authenticated read Anam
    has. `POST /v1/auth/session-token` would also prove the key but opens a
    session, and a verification must not start anything."""
    await _request(
        "anam",
        "GET",
        f"{ANAM_API_URL}/v1/avatars",
        headers={"Authorization": f"Bearer {api_key}"},
        params={"perPage": "1", "page": "1"},
    )
    return VerifiedAccount()


async def _verify_aicoustics(license_key: str) -> VerifiedAccount:
    """POST /v1/sdk/tokens — mint and discard a short-lived SDK token.

    ai-coustics runs on the worker as a native SDK rather than over HTTP, so
    this is the only way to ask them anything. The credential a tenant pastes is
    a license — `<base64 payload>.<signature>` — and the token endpoint
    authenticates the `api_key` *inside* the payload, so an unreadable license
    fails here, locally, with the one message that actually helps.
    """
    payload_segment = license_key.split(".")[0]
    try:
        # No base64 padding is guaranteed on the segment; restore it first.
        decoded = base64.b64decode(payload_segment + "=" * (-len(payload_segment) % 4))
        api_key = json.loads(decoded)["api_key"]
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError):
        raise HTTPException(
            status_code=400,
            detail=(
                "This is not an ai-coustics SDK license key. Copy the whole key "
                "from developers.ai-coustics.com — it looks like <payload>.<signature>."
            ),
        ) from None

    # Basic auth with the API key as the username and no password, which is how
    # ai-coustics documents the token exchange.
    await _request(
        "aicoustics",
        "POST",
        AICOUSTICS_TOKEN_URL,
        auth=httpx.BasicAuth(api_key, ""),
    )
    return VerifiedAccount()


_VERIFIERS: dict[str, Callable[[str], Awaitable[VerifiedAccount]]] = {
    "aicoustics": _verify_aicoustics,
    "anam": _verify_anam,
    "deepgram": _verify_deepgram,
    "elevenlabs": _verify_elevenlabs,
    "gemini": _verify_gemini,
    "openai": _verify_openai,
    "openrouter": _verify_openrouter,
    "raya": _verify_raya,
    "sarvam": _verify_sarvam,
    "soniox": _verify_soniox,
    "xai": _verify_xai,
}


async def verify_provider_key(provider: str, api_key: str) -> VerifiedAccount:
    """Call `provider` with `api_key`; raise unless it is a key we can run on.

    400 means the provider refused it, 502 means the provider never answered.

    A provider with no verifier above passes unchecked, so enabling one in
    catalog.yaml is never blocked on writing its verifier first — the key is
    stored and a bad one surfaces on the agent's first turn, which is where all
    of them surfaced before this module existed.
    """
    verify = _VERIFIERS.get(provider)
    if verify is None:
        return VerifiedAccount()
    return await verify(api_key)
