"""Deepgram voice list: Aura from GET /v1/models, Flux TTS from GET /v2/models."""

from __future__ import annotations

import logging

import httpx
from fastapi import HTTPException

from ..models import VoiceItemResponse, VoicesResponse
from .common import (
    HTTP_TIMEOUT_SECONDS,
    ProviderCredential,
    cached,
    order_languages,
)

logger = logging.getLogger("talqing.services.catalog")

DEEPGRAM_API_URL = "https://api.deepgram.com"
# Two endpoints, two halves of one picker: /v1/models lists no Flux voice and
# /v2/models lists no Aura one, so the roster is the union of both.
_MODEL_LIST_PATHS = ("/v1/models", "/v2/models")
_ARCHITECTURE_TO_CATALOG_MODEL = {
    "aura": "aura-1",
    "aura-2": "aura-2",
    "flux-tts": "flux",
}


async def _fetch_voices(api_key: str) -> list[VoiceItemResponse]:
    """Each voice id is the Deepgram synthesis model string; VoiceItemResponse.model
    is the catalog family (flux / aura-1 / aura-2)."""
    entries: list[dict] = []
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
        for path in _MODEL_LIST_PATHS:
            r = await client.get(
                f"{DEEPGRAM_API_URL}{path}",
                headers={"Authorization": f"Token {api_key}"},
            )
            r.raise_for_status()
            entries.extend(r.json().get("tts") or [])
    out: list[VoiceItemResponse] = []
    for v in entries:
        catalog_model = _ARCHITECTURE_TO_CATALOG_MODEL.get(v.get("architecture"))
        if not catalog_model:
            continue
        raw_canonical_name = v.get("canonical_name")
        if not isinstance(raw_canonical_name, str) or not raw_canonical_name.strip():
            raise ValueError("Deepgram voice payload is missing 'canonical_name'")
        cn = raw_canonical_name.strip()
        meta = v.get("metadata") or {}
        tags = meta.get("tags") or []
        gender = "male" if "masculine" in tags else "female" if "feminine" in tags else None
        langs = v.get("languages") or []
        language = langs[0] if langs else None
        locale = next((code for code in langs[1:] if code != language), None)
        out.append(
            VoiceItemResponse(
                id=cn,
                name=(v.get("name") or cn).title(),
                provider="deepgram",
                model=catalog_model,
                gender=gender,
                language=language,
                locale=locale,
                accent=meta.get("accent"),
                description=", ".join(meta.get("use_cases") or []) or None,
                sample_url=meta.get("sample"),
                image_url=meta.get("image"),
                tags=tags,
            )
        )
    return out


async def list_voices(
    cred: ProviderCredential,
    *,
    model: str | None,
    search: str,
    language: str,
    accent: str,
    gender: str,
) -> VoicesResponse:
    """Fetch the roster, scope to catalog model, filter locally."""
    search = search.strip()
    language = language.strip()
    accent = accent.strip()
    gender = gender.strip().lower()
    model = model.strip() if model else None

    async def fetch() -> list[VoiceItemResponse]:
        try:
            return await _fetch_voices(cred.api_key)
        except Exception:
            logger.exception("deepgram voices fetch failed")
            raise HTTPException(status_code=502, detail="deepgram voices unavailable")

    voices = await cached(cred, ("voices", "deepgram", "index"), fetch)
    if model:
        voices = [v for v in voices if v.model == model]

    # Facet options from the (model-scoped) index so dropdowns stay stable while filtering.
    # Accents only when a language is selected — empty list hides the accents dropdown.
    languages = order_languages(
        list({code for v in voices for code in (v.language, v.locale) if code})
    )
    if language:
        accents = sorted(
            {
                v.accent
                for v in voices
                if v.accent and language in {code for code in (v.language, v.locale) if code}
            }
        )
    else:
        accents = []
    genders = sorted({v.gender.lower() for v in voices if v.gender})

    q = search.lower()
    out: list[VoiceItemResponse] = []
    for v in voices:
        codes = {code for code in (v.language, v.locale) if code}
        if language and language not in codes:
            continue
        if accent and accents and v.accent != accent:
            continue
        if gender and genders and (v.gender or "").lower() != gender:
            continue
        if (
            q
            and q not in " ".join(value for value in (v.name, v.description, v.id) if value).lower()
        ):
            continue
        out.append(v)

    return VoicesResponse(
        voices=out,
        languages=languages,
        accents=accents,
        genders=genders,
        has_more=False,
        total_count=len(out),
        page=0,
        workspace_key=cred.is_workspace_key,
    )
