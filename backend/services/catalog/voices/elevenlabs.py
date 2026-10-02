"""ElevenLabs voice gallery: the workspace's own account + the shared library.

Two rosters, because they are two different things to a caller. A voice already
in the account is selectable as-is; a shared-library voice has to be saved into
an account first (`add_shared_voice`), which is why only the latter carries an
`owner_id` for the picker to act on.

The account half only exists when the request runs on the workspace's own key —
on Talqing's key it is Talqing's roster, which is nobody's business.
"""

from __future__ import annotations

import asyncio
import logging

import httpx
from fastapi import HTTPException

from ..models import (
    AddVoiceRequest,
    AddVoiceResponse,
    VoiceItemResponse,
    VoiceSettingsResponse,
    VoicesResponse,
)
from .common import (
    HTTP_TIMEOUT_SECONDS,
    ProviderCredential,
    cached,
    catalog_language_options,
    tts_catalog_entries,
)

logger = logging.getLogger("talqing.services.catalog")

ELEVENLABS_API_URL = "https://api.elevenlabs.io"
_PAGE_SIZE = 100
_GENDERS = ["male", "female"]
# Badge on a voice that is already in the workspace's own account, so it needs
# no save step — the one thing that separates the two halves of this gallery.
_OWNED_TIER = "Your account"
# Categories from GET /v1/shared-voices (Voice Library).
_CATEGORY_LABELS = {
    "generated": "Generated",
    "cloned": "Cloned",
    "premade": "Premade",
    "professional": "Professional",
    "famous": "Famous",
    "high_quality": "Studio quality",
}
# ElevenLabs models India's regional languages as ACCENTS under Hindi.
_HI_ACCENTS = [
    "standard",
    "marathi",
    "punjabi",
    "gujarati",
    "bengali",
    "bhojpuri",
    "awadhi",
    "bihari",
    "haryanvi",
    "rajasthani",
    "tamil",
    "telegu",
]


def _account_voice(v: dict) -> VoiceItemResponse:
    raw_voice_id = v.get("voice_id")
    if not isinstance(raw_voice_id, str) or not raw_voice_id.strip():
        raise ValueError("ElevenLabs voice payload is missing 'voice_id'")
    vid = raw_voice_id.strip()
    labels = v.get("labels") or {}
    language = labels.get("language")
    # `verified_languages` is the model's coverage, not the voice's identity —
    # a Hindi voice verifies in twenty of them — so it is read for the locale of
    # the voice's own language only, and never for the language itself.
    locale = next(
        (
            item.get("locale")
            for item in v.get("verified_languages") or []
            if item.get("language") == language and item.get("locale")
        ),
        None,
    )
    return VoiceItemResponse(
        id=vid,
        name=v.get("name") or vid,
        provider="elevenlabs",
        gender=labels.get("gender"),
        language=language,
        accent=labels.get("accent"),
        country=locale.split("-")[1] if locale and "-" in locale else None,
        locale=locale,
        description=v.get("description"),
        sample_url=v.get("preview_url"),
        # Deliberately no owner_id: the picker reads its absence as "already in
        # the account, select it directly" and skips the save round-trip.
        tier=_OWNED_TIER,
    )


async def _fetch_account_voices(api_key: str) -> list[VoiceItemResponse]:
    """GET /v2/voices — every voice in the account, followed to the last page.

    Filtered in Python afterwards: this endpoint takes a `search` but no
    language, accent or gender, and a workspace roster is tens of voices.
    """
    out: list[VoiceItemResponse] = []
    params: dict = {"page_size": _PAGE_SIZE}
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
        while True:
            r = await client.get(
                f"{ELEVENLABS_API_URL}/v2/voices",
                params=params,
                headers={"xi-api-key": api_key},
            )
            r.raise_for_status()
            raw = r.json()
            out.extend(_account_voice(v) for v in (raw.get("voices") or []))
            token = raw.get("next_page_token")
            if not raw.get("has_more") or not token:
                return out
            params = {"page_size": _PAGE_SIZE, "next_page_token": token}


async def list_voices(
    cred: ProviderCredential,
    *,
    model: str | None,
    search: str,
    language: str,
    accent: str,
    gender: str,
    page: int,
) -> VoicesResponse:
    """The account's own voices, then the shared library. Key never reaches the
    browser."""
    languages = catalog_language_options(
        [lang for entry in tts_catalog_entries("elevenlabs", model) for lang in entry.languages]
    )
    search = search.strip()
    language = language.strip()
    accent = accent.strip()
    gender = gender.strip().lower()
    page = max(0, page)
    cache_key = (
        "voices",
        "elevenlabs",
        "" if model is None else model.strip(),
        search.lower(),
        language,
        accent.lower(),
        gender,
        page,
    )

    async def fetch() -> VoicesResponse:
        params: dict = {"page": page, "page_size": _PAGE_SIZE, "sort": "trending"}
        if search:
            params["search"] = search
        if language:
            params["language"] = language
        if accent:
            params["accent"] = accent
        if gender:
            params["gender"] = gender
        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
                r = await client.get(
                    f"{ELEVENLABS_API_URL}/v1/shared-voices",
                    params=params,
                    headers={"xi-api-key": cred.api_key},
                )
                r.raise_for_status()
                raw = r.json()
        except Exception:
            logger.exception("ElevenLabs library fetch failed")
            raise HTTPException(status_code=502, detail="elevenlabs voices unavailable")
        voices: list[VoiceItemResponse] = []
        accents_seen: list[str] = []
        for v in raw.get("voices") or []:
            raw_voice_id = v.get("voice_id")
            if not isinstance(raw_voice_id, str) or not raw_voice_id.strip():
                raise ValueError("ElevenLabs voice payload is missing 'voice_id'")
            vid = raw_voice_id.strip()
            loc = v.get("locale") or ""
            country = loc.split("-")[1] if "-" in loc else None
            voices.append(
                VoiceItemResponse(
                    id=vid,
                    name=v.get("name") or vid,
                    provider="elevenlabs",
                    gender=v.get("gender"),
                    language=v.get("language"),
                    accent=v.get("accent"),
                    country=country,
                    locale=v.get("locale"),
                    description=v.get("description"),
                    sample_url=v.get("preview_url"),
                    image_url=v.get("image_url") or None,
                    tier=_CATEGORY_LABELS.get(v.get("category") or ""),
                    owner_id=v.get("public_owner_id"),
                )
            )
            a = v.get("accent")
            if a and a not in accents_seen:
                accents_seen.append(a)
        # Accents only make sense once a language is selected; empty list
        # keeps the frontend accents dropdown hidden until then.
        if not language:
            accents_out: list[str] = []
        elif language == "hi":
            accents_out = _HI_ACCENTS + [a for a in accents_seen if a not in _HI_ACCENTS]
        else:
            accents_out = accents_seen
        return VoicesResponse(
            voices=voices,
            languages=languages,
            accents=accents_out,
            genders=_GENDERS,
            has_more=bool(raw.get("has_more")),
            total_count=raw.get("total_count"),
            page=page,
        )

    # On Talqing's key there is only the shared library, and it looks the same to
    # everyone, so it stays cached for the process lifetime.
    if not cred.is_workspace_key:
        return await cached(cred, cache_key, fetch)

    async def owned() -> list[VoiceItemResponse]:
        try:
            roster = await _fetch_account_voices(cred.api_key)
        except Exception:
            logger.exception("ElevenLabs account roster fetch failed")
            raise HTTPException(status_code=502, detail="elevenlabs voices unavailable")
        q = search.lower()
        a = accent.lower()
        return [
            v
            for v in roster
            if (not language or v.language == language)
            and (not a or (v.accent or "").lower() == a)
            and (not gender or (v.gender or "").lower() == gender)
            and (not q or q in f"{v.name or ''} {v.id} {v.description or ''}".lower())
        ]

    owned_voices, shared = await asyncio.gather(owned(), fetch())

    # A saved library voice keeps its library id, so without this it shows up in
    # both halves — and on every page, not only the one the roster leads.
    owned_ids = {v.id for v in owned_voices}
    library = [v for v in shared.voices if v.id not in owned_ids]
    accents = list(shared.accents)
    if language:
        for v in owned_voices:
            if v.accent and v.accent not in accents:
                accents.append(v.accent)
    return shared.model_copy(
        update={
            # The roster is complete and unpaginated, so it leads page 0 — the
            # voices that need no save step come before the ones that do.
            "voices": [*owned_voices, *library] if page == 0 else library,
            "accents": accents,
            "total_count": (shared.total_count or 0) + len(owned_voices),
            "workspace_key": True,
        }
    )


async def add_shared_voice(body: AddVoiceRequest, cred: ProviderCredential) -> AddVoiceResponse:
    """Save a shared-library voice into the ElevenLabs account that will speak it.

    With a workspace key that is the tenant's own account, and the voice slot is
    billed to them — which is the point: an agent on their key cannot synthesize
    with a voice sitting in ours.
    """
    cache_key = ("voices", "elevenlabs", "add", body.owner_id, body.voice_id)

    async def add_voice() -> AddVoiceResponse:
        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
                r = await client.post(
                    f"{ELEVENLABS_API_URL}/v1/voices/add/{body.owner_id}/{body.voice_id}",
                    json={"new_name": body.name or body.voice_id},
                    headers={"xi-api-key": cred.api_key},
                )
            if r.status_code == 200:
                return AddVoiceResponse(voice_id=r.json().get("voice_id", body.voice_id))
            logger.warning("ElevenLabs add-voice failed status=%s body=%s", r.status_code, r.text)
            raise HTTPException(status_code=502, detail="elevenlabs add voice failed")
        except HTTPException:
            raise
        except Exception:
            logger.exception("ElevenLabs add-voice failed")
            raise HTTPException(status_code=502, detail="elevenlabs add voice failed")

    return await cached(cred, cache_key, add_voice)


async def voice_settings(voice_id: str, cred: ProviderCredential) -> VoiceSettingsResponse:
    """The voice's own stability / similarity_boost, as its author tuned them.

    Only works for a voice that is IN the account this key belongs to: a
    shared-library voice answers `voice_not_found` until `add_shared_voice` has
    saved it, which is why the picker reads this only after that step and with
    the id that call returned.

    Not cached. It is one request per voice PICK, not per render, and a tenant
    who retunes a voice in the ElevenLabs UI and re-picks it should get the new
    numbers rather than a cached copy of the old ones.
    """
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
            r = await client.get(
                f"{ELEVENLABS_API_URL}/v1/voices/{voice_id}/settings",
                headers={"xi-api-key": cred.api_key},
            )
    except Exception:
        logger.exception("ElevenLabs voice settings fetch failed voice_id=%s", voice_id)
        raise HTTPException(status_code=502, detail="elevenlabs voice settings unavailable")
    # ElevenLabs answers an unknown voice with 400, not 404, and the id is
    # "unknown" for any voice outside this key's account — which a shared-library
    # voice is until it has been saved. Reported as the caller's mistake it is,
    # with the fix, rather than as an upstream outage.
    if r.status_code == 400 and "voice_not_found" in r.text:
        raise HTTPException(
            status_code=404,
            detail=(
                f"voice '{voice_id}' is not in this workspace's ElevenLabs account - "
                "save it first with add_elevenlabs_voice, then read the settings of "
                "the voice id that returns"
            ),
        )
    if r.status_code != 200:
        logger.warning(
            "ElevenLabs voice settings failed voice_id=%s status=%s body=%s",
            voice_id,
            r.status_code,
            r.text,
        )
        raise HTTPException(status_code=502, detail="elevenlabs voice settings unavailable")
    raw = r.json()

    def knob(field: str) -> float | None:
        """One setting, distinguishing "no override" from a changed API.

        ElevenLabs declares both fields nullable, so an explicit null is a
        legitimate answer meaning the voice stores no override — pass it through
        and the agent sends no key, which is what ElevenLabs then reads as its
        own default. A MISSING key is different: it means this response is not
        the shape we parse, and guessing a number there would flatten the voice
        silently, which is the exact bug this endpoint exists to prevent.

        No null has been observed in practice (46 account voices and a fresh
        clone all return numbers), but a null must not be able to block a voice pick.
        """
        if field not in raw:
            raise HTTPException(
                status_code=502,
                detail=f"elevenlabs voice settings response has no '{field}'",
            )
        value = raw[field]
        if value is None:
            return None
        if not isinstance(value, int | float):
            raise HTTPException(
                status_code=502,
                detail=f"elevenlabs voice settings '{field}' is not a number",
            )
        return float(value)

    return VoiceSettingsResponse(
        stability=knob("stability"),
        similarity_boost=knob("similarity_boost"),
    )
