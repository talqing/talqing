"""Soniox voices: the account's cloned voices from GET /v1/voices, then the
built-ins from GET /v1/tts-models.

The built-ins are not published on a voices endpoint of their own — they are
nested inside the TTS model list, one roster per model. `/v1/voices` returns the
*cloned* voices belonging to whoever's key is asking, which is why it is read
only on a workspace key: on Talqing's key it is Talqing's clones, and a tenant
cannot synthesize with those.

Live rather than transcribed into `catalog.yaml` for one reason: a voice belongs
to exactly one model and the other rejects it outright ("Invalid voice 'Maya'
for model 'tts-rt-v2'", measured 2026-08-12). Reading the roster from the same
document that defines it is what makes a wrong-model voice unpickable instead of
a 400 on a live call. A cloned voice carries the same rule as an explicit
per-model status, and is only offered under the models it is `ready` for.

Every Soniox voice speaks all 64 of the model's languages — the language is a
separate synthesis parameter, not a property of the voice — so there is no
language facet here, unlike ElevenLabs or Raya. Gender and Soniox's own one-line
character description are the two things that tell 70 first names apart, and
both come back on every built-in (a cloned voice has neither: it has the name
its owner gave it).
"""

from __future__ import annotations

import logging

import httpx
from fastapi import HTTPException

from ..models import VoiceItemResponse, VoicesResponse
from .common import HTTP_TIMEOUT_SECONDS, ProviderCredential, cached

logger = logging.getLogger("talqing.services.catalog")

SONIOX_API_URL = "https://api.soniox.com"
# Badge on a voice cloned in the workspace's own Soniox project.
_OWNED_TIER = "Your account"


async def _fetch_cloned_voices(api_key: str) -> list[VoiceItemResponse]:
    """GET /v1/voices — the account's cloned voices, one row per ready model.

    `models[].status` has to be `ready` for that model to accept the voice; the
    other states mean it is unprepared, still computing, or failed, so offering
    it there would only produce a synthesis error on a live call.
    """
    out: list[VoiceItemResponse] = []
    params: dict = {}
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
        while True:
            r = await client.get(
                f"{SONIOX_API_URL}/v1/voices",
                params=params,
                headers={"Authorization": f"Bearer {api_key}"},
            )
            r.raise_for_status()
            raw = r.json()
            for v in raw.get("voices") or []:
                voice_id = v.get("id")
                if not voice_id:
                    raise ValueError(f"Soniox cloned voice payload is missing 'id': {v!r}")
                for entry in v.get("models") or []:
                    if entry.get("status") != "ready":
                        continue
                    out.append(
                        VoiceItemResponse(
                            id=voice_id,
                            name=v.get("name") or voice_id,
                            provider="soniox",
                            model=entry.get("model"),
                            tier=_OWNED_TIER,
                        )
                    )
            cursor = raw.get("next_page_cursor")
            if not cursor:
                return out
            params = {"cursor": cursor}


async def _fetch_voices(api_key: str) -> list[VoiceItemResponse]:
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
        r = await client.get(
            f"{SONIOX_API_URL}/v1/tts-models",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        r.raise_for_status()
        raw = r.json()

    out: list[VoiceItemResponse] = []
    for entry in raw.get("models") or []:
        # `aliased_model_id` marks a deprecated spelling of another model
        # (tts-rt-v1-preview → tts-rt-v1) and repeats its whole roster. Keeping
        # it would list every one of those voices twice under a model id the
        # catalog does not carry.
        model = entry.get("id")
        if not model or entry.get("aliased_model_id"):
            continue
        for v in entry.get("voices") or []:
            # Soniox's voice id IS its display name ("Priya"), so a voice with
            # no id has nothing to show and nothing to synthesize with — their
            # own schema broken, worth naming rather than skipping past.
            voice_id = v.get("id")
            if not voice_id:
                raise ValueError(f"Soniox voice payload for {model} is missing 'id': {v!r}")
            out.append(
                VoiceItemResponse(
                    id=voice_id,
                    name=voice_id,
                    provider="soniox",
                    model=model,
                    gender=v.get("gender"),
                    description=v.get("description"),
                )
            )
    return out


async def list_voices(
    cred: ProviderCredential, *, model: str | None, search: str, gender: str
) -> VoicesResponse:
    """Fetch the roster, scope it to the catalog model, filter locally."""
    search = search.strip().lower()
    gender = gender.strip().lower()
    model = model.strip() if model else None

    async def fetch() -> list[VoiceItemResponse]:
        try:
            builtin = await _fetch_voices(cred.api_key)
            if not cred.is_workspace_key:
                return builtin
            # Cloned first: a voice someone in this workspace made is what they
            # came looking for, and there are 70 built-in first names after it.
            return [*await _fetch_cloned_voices(cred.api_key), *builtin]
        except Exception:
            logger.exception("soniox voices fetch failed")
            raise HTTPException(status_code=502, detail="soniox voices unavailable")

    voices = await cached(cred, ("voices", "soniox", "index"), fetch)
    if model:
        voices = [v for v in voices if v.model == model]

    # Facet from the model-scoped index, so the dropdown stays put while it is
    # being used to filter.
    genders = sorted({v.gender.lower() for v in voices if v.gender})

    # A built-in's id is its display name; a cloned voice's id is a uuid and its
    # name is the only thing anyone would type, so both are searched.
    out = [
        v
        for v in voices
        if (not gender or (v.gender or "").lower() == gender)
        and (not search or search in f"{v.name or ''} {v.id} {v.description or ''}".lower())
    ]
    return VoicesResponse(
        voices=out,
        genders=genders,
        has_more=False,
        total_count=len(out),
        page=0,
        workspace_key=cred.is_workspace_key,
    )
