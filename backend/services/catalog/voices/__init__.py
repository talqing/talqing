"""Voice catalog: live provider libraries + static config-declared voices.

Live galleries run on the workspace's own provider key when it has one and on
Talqing's otherwise — `resolve_credential` decides, every module here is handed
the answer.

To add a live voice provider:
  1. Add a module with `list_voices(cred, ...) -> VoicesResponse`.
  2. Add the provider to `LIVE_PROVIDERS` and branch on it in `list_voices`.
"""

from __future__ import annotations

from fastapi import HTTPException

from ..loader import get_catalog
from ..models import (
    AddVoiceRequest,
    AddVoiceResponse,
    VoiceSettingsResponse,
    VoicesResponse,
)
from . import deepgram, elevenlabs, raya, soniox, xai
from .common import VoiceKind, list_static_voices, required_query, resolve_credential

# Providers whose roster is read from their API rather than declared in
# catalog.yaml — the ones that need a key at all.
LIVE_PROVIDERS = frozenset({"elevenlabs", "deepgram", "xai", "raya", "soniox"})


async def list_voices(
    *,
    provider: str,
    workspace_key: str,
    model: str | None = None,
    kind: VoiceKind = "tts",
    search: str = "",
    language: str = "",
    accent: str = "",
    gender: str = "",
    page: int = 0,
) -> VoicesResponse:
    p = required_query(provider, "provider").lower()
    if not get_catalog().provider_enabled(p):
        raise HTTPException(status_code=404, detail=f"provider '{p}' is not enabled")

    if p not in LIVE_PROVIDERS:
        # Static enum providers (OpenAI, Sarvam, Gemini) — fixed list on the
        # entry, so no provider call and no key of anyone's involved.
        return list_static_voices(kind, p, model, search=search, gender=gender)

    cred = resolve_credential(p, workspace_key)

    # Live galleries are provider-wide, not per-kind: xAI serves one voice roster
    # for its TTS and its speech-to-speech model alike, and the other four
    # providers here have no realtime entry at all.
    if p == "elevenlabs":
        return await elevenlabs.list_voices(
            cred,
            model=model,
            search=search,
            language=language,
            accent=accent,
            gender=gender,
            page=page,
        )
    if p == "deepgram":
        return await deepgram.list_voices(
            cred,
            model=model,
            search=search,
            language=language,
            accent=accent,
            gender=gender,
        )
    if p == "xai":
        return await xai.list_voices(cred, search=search, language=language, gender=gender)
    if p == "raya":
        # Scoped by model, unlike the four above: a Raya voice belongs to one of
        # `m1`/`standard` and 404s on the other. No gender — Raya ships none.
        return await raya.list_voices(cred, model=model, search=search, language=language)
    # Soniox, also scoped by model — a voice belongs to one TTS model and the
    # other rejects it. No language facet: every Soniox voice speaks all 64, the
    # language being a synthesis parameter rather than a property of a voice.
    return await soniox.list_voices(cred, model=model, search=search, gender=gender)


async def add_elevenlabs_shared_voice(
    body: AddVoiceRequest, workspace_key: str
) -> AddVoiceResponse:
    return await elevenlabs.add_shared_voice(body, resolve_credential("elevenlabs", workspace_key))


async def elevenlabs_voice_settings(voice_id: str, workspace_key: str) -> VoiceSettingsResponse:
    return await elevenlabs.voice_settings(
        voice_id, resolve_credential("elevenlabs", workspace_key)
    )
