"""Shared helpers for the voice catalog: credentials, cache, entry lookup.

Language display names come from catalog.yaml language lists first; only codes
that live APIs surface without a catalog label fall back to a short map.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Hashable
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal, TypeVar, cast

from fastapi import HTTPException

from settings import get_settings
from utils.cache import AsyncMemoryCache

from ..loader import get_catalog
from ..models import (
    CatalogLanguageOption,
    LanguageOption,
    RealtimeEntry,
    TTSEntry,
    VoiceItemResponse,
    VoicesResponse,
)

HTTP_TIMEOUT_SECONDS = 10

# Which catalog kind a voice selection belongs to: a TTS model's voice, or a
# speech-to-speech model's own voice.
VoiceKind = Literal["tts", "realtime"]

CatalogCacheKey = tuple[Hashable, ...]
CATALOG_CACHE: AsyncMemoryCache[CatalogCacheKey] = AsyncMemoryCache()

V = TypeVar("V")


@dataclass(frozen=True)
class ProviderCredential:
    """Whose provider account a gallery request browses on."""

    api_key: str
    is_workspace_key: bool


def resolve_credential(provider: str, workspace_key: str) -> ProviderCredential:
    """The workspace's own key if they have stored one, else Talqing's.

    The workspace key wins because the agent will synthesize on it: a gallery
    browsed on Talqing's account can show a voice the tenant does not own, and
    saving a library voice would land it in the wrong account entirely.
    """
    if workspace_key:
        return ProviderCredential(workspace_key, is_workspace_key=True)
    platform = get_settings().provider_secrets.get(provider, "")
    if not platform:
        # Checked here rather than left to the request. An empty key makes a
        # header like `Bearer ` with nothing after it, which httpx rejects as
        # malformed before it ever reaches the provider — so the unconfigured
        # case would surface as an h11 protocol error under a generic
        # "unavailable", naming neither the provider setting nor the fact that
        # it is simply missing.
        raise HTTPException(status_code=503, detail=f"{provider} API key is not configured")
    return ProviderCredential(platform, is_workspace_key=False)


async def cached(
    cred: ProviderCredential, key: CatalogCacheKey, fetch: Callable[[], Awaitable[V]]
) -> V:
    """Cache platform-key results for the process lifetime; never a workspace's.

    A workspace's roster is theirs — it changes from their own provider
    dashboard, and one process serves every tenant, so a shared cache would hand
    it to the wrong one.
    """
    if cred.is_workspace_key:
        return await fetch()
    return await CATALOG_CACHE.get_or_set(key, fetch)


# Only for live-API codes not declared on any catalog entry (keep small).
_EXTRA_LANG_NAMES = {
    "multilingual": "Multilingual",
    "multi": "Multilingual",
    "auto": "Auto detect",
    "ceb": "Cebuano",
    "ny": "Chichewa",
    "ha": "Hausa",
    "ga": "Irish",
    "jv": "Javanese",
    "ky": "Kyrgyz",
    "ln": "Lingala",
    "lb": "Luxembourgish",
    "ps": "Pashto",
    "sd": "Sindhi",
    "so": "Somali",
    "sv-SE": "Swedish",
    "zh-CN": "Chinese",
}


@lru_cache
def _catalog_language_names() -> dict[str, str]:
    """code → display name from every STT/TTS/realtime catalog language list."""
    names: dict[str, str] = {}
    cat = get_catalog()
    for entry in (*cat.stt, *cat.tts, *cat.realtime):
        for item in entry.languages:
            names.setdefault(item.code, item.name)
    return names


def _language_display_name(code: str) -> str:
    return _catalog_language_names().get(code) or _EXTRA_LANG_NAMES.get(code) or code.upper()


def required_query(value: str | None, name: str) -> str:
    if value is None:
        raise HTTPException(status_code=400, detail=f"{name} is required")
    cleaned = value.strip()
    if not cleaned:
        raise HTTPException(status_code=400, detail=f"{name} is required")
    return cleaned


def order_languages(codes: list[str]) -> list[LanguageOption]:
    """Build language options sorted by display name."""
    return [
        LanguageOption(code=code, name=_language_display_name(code))
        for code in sorted(set(codes), key=_language_display_name)
    ]


def catalog_language_options(
    items: list[CatalogLanguageOption],
) -> list[LanguageOption]:
    out: list[LanguageOption] = []
    seen: set[str] = set()
    for item in items:
        if item.code in seen:
            continue
        seen.add(item.code)
        out.append(LanguageOption(code=item.code, name=item.name))
    return out


def voice_catalog_entries(
    kind: VoiceKind, provider: str, model: str | None = None
) -> list[TTSEntry] | list[RealtimeEntry]:
    """Entries of `kind` that own a voice selection — TTS or speech-to-speech."""
    p = required_query(provider, "provider").lower()
    m = "" if model is None else model.strip()
    label = "TTS" if kind == "tts" else "realtime"
    entries: list[TTSEntry] | list[RealtimeEntry] = (
        get_catalog().tts if kind == "tts" else get_catalog().realtime
    )
    provider_entries = [entry for entry in entries if entry.provider == p]
    if not provider_entries:
        raise HTTPException(
            status_code=404, detail=f"provider '{p}' has no {label} catalog entries"
        )
    if not m:
        return provider_entries  # type: ignore[return-value]
    model_entries = [entry for entry in provider_entries if entry.model == m]
    if not model_entries:
        raise HTTPException(
            status_code=404, detail=f"{label} model '{p}/{m}' is not in the catalog"
        )
    return model_entries  # type: ignore[return-value]


def tts_catalog_entries(provider: str, model: str | None = None) -> list[TTSEntry]:
    return cast(list[TTSEntry], voice_catalog_entries("tts", provider, model))


def list_static_voices(
    kind: VoiceKind,
    provider: str,
    model: str | None,
    *,
    search: str = "",
    gender: str = "",
) -> VoicesResponse:
    """Voices declared on the catalog entry (OpenAI, Sarvam, Gemini realtime).

    These providers ship a fixed published enum — no live gallery, no per-voice
    language/accent, no sample previews. `description` is the provider's own tone
    label ("Confident & Bold", "Upbeat"), which is the only thing that tells one
    voice from another when the roster is 37 interchangeable first names.
    """
    entries = voice_catalog_entries(kind, provider, model)
    voices = [
        VoiceItemResponse(
            id=voice.id,
            name=voice.name,
            provider=provider.lower(),
            gender=voice.gender,
            description=voice.description,
        )
        for entry in entries
        for voice in entry.voices
    ]
    genders = sorted({v.gender.lower() for v in voices if v.gender})
    search = search.strip().lower()
    gender = gender.strip().lower()

    out: list[VoiceItemResponse] = []
    for v in voices:
        if gender and genders and (v.gender or "").lower() != gender:
            continue
        if search and search not in f"{v.name or ''} {v.id} {v.description or ''}".lower():
            continue
        out.append(v)

    return VoicesResponse(
        voices=out,
        genders=genders,
        has_more=False,
        total_count=len(out),
        page=0,
    )
