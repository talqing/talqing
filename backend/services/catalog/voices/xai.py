"""xAI live voice library (GET /v1/tts/voices)."""

from __future__ import annotations

import logging

import httpx
from fastapi import HTTPException

from settings import get_settings

from ..models import VoiceItemResponse, VoicesResponse
from .common import (
    HTTP_TIMEOUT_SECONDS,
    ProviderCredential,
    cached,
    order_languages,
)

logger = logging.getLogger("talqing.services.catalog")

# What xAI puts in a voice's `language` when the voice is not tied to one. Today
# that is every voice it ships: the model carries the language, the voice only
# carries the timbre.
MULTILINGUAL = "multilingual"


async def _fetch_voices(api_key: str) -> list[VoiceItemResponse]:
    """Map GET /v1/tts/voices → VoiceItemResponse.

    Live payload fields: voice_id, name, language, gender.
    `language` is a code or the literal "multilingual".

    `base_url` stays a platform setting — it is which xAI deployment we talk to,
    not something a tenant configures alongside their key.
    """
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
        r = await client.get(
            f"{get_settings().providers.xai.base_url.rstrip('/')}/tts/voices",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        r.raise_for_status()
    raw = r.json()
    voices: list[VoiceItemResponse] = []
    for v in raw.get("voices") or []:
        raw_voice_id = v.get("voice_id")
        if not isinstance(raw_voice_id, str) or not raw_voice_id.strip():
            raise ValueError("xAI voice payload is missing 'voice_id'")
        voice_id = raw_voice_id.strip()
        voices.append(
            VoiceItemResponse(
                id=voice_id,
                name=v.get("name") or voice_id,
                provider="xai",
                language=v.get("language"),
                gender=v.get("gender"),
            )
        )
    return voices


async def list_voices(
    cred: ProviderCredential, *, search: str, language: str, gender: str
) -> VoicesResponse:
    """Fetch the roster; languages come from the live voice list."""
    search = search.strip()
    language = language.strip()
    gender = gender.strip().lower()

    async def fetch() -> list[VoiceItemResponse]:
        try:
            return await _fetch_voices(cred.api_key)
        except Exception:
            logger.exception("xai voices fetch failed")
            raise HTTPException(status_code=502, detail="xai voices unavailable")

    voices = await cached(cred, ("voices", "xai", "index"), fetch)

    # The picker seeds this filter with the agent's language — a real code like
    # "en" — so a multilingual voice has to match it rather than be excluded by
    # it: the voice speaks whatever it is asked to, and matching by equality
    # emptied the entire gallery for every agent not set to Auto. By the same
    # logic a facet whose only value is "multilingual" narrows nothing and is
    # not offered; the dropdown reappears if xAI ever ships a language-bound
    # voice.
    languages = order_languages(
        list({v.language for v in voices if v.language and v.language != MULTILINGUAL})
    )
    genders = sorted({v.gender.lower() for v in voices if v.gender})

    q = search.lower()
    out: list[VoiceItemResponse] = []
    for v in voices:
        if language and v.language != MULTILINGUAL and v.language != language:
            continue
        if gender and genders and (v.gender or "").lower() != gender:
            continue
        if q and q not in f"{v.name or ''} {v.id}".lower():
            continue
        out.append(v)

    return VoicesResponse(
        voices=out,
        languages=languages,
        genders=genders,
        has_more=False,
        total_count=len(out),
        page=0,
        workspace_key=cred.is_workspace_key,
    )
