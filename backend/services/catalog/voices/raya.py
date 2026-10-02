"""Raya (Bakbak) voice list from GET /v1/voices.

The thinnest live gallery here: Raya returns id, name, language and model, and
nothing else — no gender, no accent, no sample audio. So the picker for a Raya
agent shows names filtered by language, with no preview button and no gender
filter, and `list_voices` below returns empty facet lists to say so.

`model` is the one field that must not be dropped. Every voice belongs to either
`m1` or `standard`, and passing one model's voice id to the other is a 404 at
synthesis time — so the response is scoped to the catalog entry the agent is on.
"""

from __future__ import annotations

import logging

import httpx
from fastapi import HTTPException

from ..models import VoiceItemResponse, VoicesResponse
from .common import HTTP_TIMEOUT_SECONDS, ProviderCredential, cached, order_languages

logger = logging.getLogger("talqing.services.catalog")

RAYA_API_URL = "https://hub.getraya.app"


async def _fetch_voices(api_key: str) -> list[VoiceItemResponse]:
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
        r = await client.get(
            f"{RAYA_API_URL}/v1/voices",
            headers={"X-API-Key": api_key},
        )
        r.raise_for_status()
        raw = r.json()

    out: list[VoiceItemResponse] = []
    for v in raw.get("voices") or []:
        # A voice with no model cannot be offered under either catalog entry, and
        # one with no id cannot be synthesized with. Both are Raya breaking their
        # own published schema, so say which voice rather than dropping it.
        voice_id = v.get("id")
        model = v.get("model")
        if not voice_id or not model:
            raise ValueError(f"Raya voice payload is missing 'id' or 'model': {v!r}")
        out.append(
            VoiceItemResponse(
                id=voice_id,
                name=v.get("name") or voice_id,
                provider="raya",
                model=model,
                language=v.get("language"),
            )
        )
    return out


async def list_voices(
    cred: ProviderCredential, *, model: str | None, search: str, language: str
) -> VoicesResponse:
    """Fetch the roster, scope to the catalog model, filter locally."""
    search = search.strip().lower()
    language = language.strip()
    model = model.strip() if model else None

    async def fetch() -> list[VoiceItemResponse]:
        try:
            return await _fetch_voices(cred.api_key)
        except Exception:
            logger.exception("raya voices fetch failed")
            raise HTTPException(status_code=502, detail="raya voices unavailable")

    voices = await cached(cred, ("voices", "raya", "index"), fetch)
    if model:
        voices = [v for v in voices if v.model == model]

    # Facets from the model-scoped index, so the language dropdown stays put
    # while the user filters with it.
    languages = order_languages([v.language for v in voices if v.language])

    out = [
        v
        for v in voices
        if (not language or v.language == language)
        and (not search or search in f"{v.name or ''} {v.id}".lower())
    ]
    return VoicesResponse(
        voices=out,
        languages=languages,
        has_more=False,
        total_count=len(out),
        page=0,
        workspace_key=cred.is_workspace_key,
    )
