"""Provider catalog operations — shared by HTTP routes and CoPilot.

Auth is enforced at the HTTP/CoPilot boundary, which is also where a request's
``workspace_key`` is loaded (``services.byok.load_provider_key``) and handed
down: the gallery runs on the workspace's own provider key when it has one, and
on Talqing's otherwise. Nothing here reaches for a tenant's credentials itself —
same contract as ``compiler.factories``, where the caller loads the keys for
whoever is paying.

Naming note:
  get_catalog()         → in-process Catalog object (catalog.yaml)
  get_public_catalog()  → API/public CatalogResponse for dropdowns
"""

from __future__ import annotations

import asyncio

import httpx
from fastapi import HTTPException

from services.system_vars import SYSTEM_FIELDS
from utils.bg import spawn

from . import openrouter
from .languages import agent_language_options, turn_detector_language_options
from .loader import get_catalog
from .models import (
    AddVoiceRequest,
    AddVoiceResponse,
    AvatarsResponse,
    CatalogResponse,
    LLMCatalogEntryResponse,
    LLMModelSearchResponse,
    ModelHostsResponse,
    ModelSearchResponse,
    STTCatalogEntryResponse,
    STTModelSearchResponse,
    VoiceSettingsResponse,
    VoicesResponse,
)
from .voices import add_elevenlabs_shared_voice as save_elevenlabs_shared_voice
from .voices import anam
from .voices import elevenlabs_voice_settings as fetch_elevenlabs_voice_settings
from .voices import list_voices as fetch_provider_voices
from .voices.common import VoiceKind, resolve_credential


async def get_public_catalog() -> CatalogResponse:
    """Model/pricing dropdown payload from catalog.yaml (no provider secrets)."""
    catalog = get_catalog()
    return CatalogResponse.model_validate(
        {
            **catalog.public(),
            "languages": agent_language_options(catalog),
            "turn_detector_languages": turn_detector_language_options(catalog),
            "system_vars": [
                {
                    "key": f.key,
                    "description": f.description,
                    "example": f.example,
                    "voice_only": f.voice_only,
                }
                for f in SYSTEM_FIELDS
            ],
        }
    )


def start_model_registry() -> asyncio.Task | None:
    """Keep this process's searched-provider model list current. Returns the task
    so a caller with a shutdown path can cancel it, or None when nothing here is
    searched.

    The API and the text worker call this; a voice worker does not, and the
    ``services.catalog.openrouter`` docstring explains why nothing on the call
    path needs it. Everything before the first refresh lands behaves as if the
    provider had no models — publish validation refuses its slugs and the picker
    shows none — which is loud, temporary and self-correcting, and is the state
    a checked-in seed file would have replaced with a quietly wrong one.
    """
    meta = get_catalog().providers.get(openrouter.PROVIDER)
    if meta is None or not meta.enabled or meta.models is None:
        return None
    return spawn(openrouter.run_refresher(meta.models))


# The registry holds ~280 models and `GET /catalog` deliberately carries none of
# them, so this is the only way to see one. Capped low on purpose: it feeds a
# typeahead, and it is also an MCP tool whose every row costs a CoPilot tokens.
MODEL_SEARCH_LIMIT = 20
MODEL_SEARCH_MAX_LIMIT = 50


async def search_models(
    q: str = "",
    limit: int = MODEL_SEARCH_LIMIT,
    cursor: str = "",
    kind: openrouter.Kind = "llm",
) -> ModelSearchResponse:
    """One page of the models of `kind` a `browse: "search"` provider offers.

    Served from this process's own snapshot, so opening the model picker never
    waits on OpenRouter and never fails when OpenRouter does. An empty result
    while the registry is still cold is the honest answer — the alternative, a
    stale list checked into the repo, offers models that no longer exist and
    fails at call time instead of at pick time.

    Deliberately not a proxy for OpenRouter's own `/models/user`, which would
    apply the workspace's privacy settings. That is nicer and would make the
    editor's list depend on a live third-party call and on a key already being
    saved; worth revisiting if anyone asks for it by name.
    """
    catalog = get_catalog()
    page_cls = LLMModelSearchResponse if kind == "llm" else STTModelSearchResponse
    entry_cls = LLMCatalogEntryResponse if kind == "llm" else STTCatalogEntryResponse
    if not any(meta.enabled and meta.searched(kind) for meta in catalog.providers.values()):
        return page_cls()
    try:
        offset = int(cursor) if cursor else 0
    except ValueError:
        offset = -1
    if offset < 0:
        raise HTTPException(status_code=400, detail="cursor must come from a previous response")
    entries, next_offset = openrouter.search(
        kind, q, limit=min(max(limit, 1), MODEL_SEARCH_MAX_LIMIT), offset=offset
    )
    return page_cls(
        models=[
            entry_cls.model_validate(e.model_dump(exclude={"extra", "aliases"})) for e in entries
        ],
        next_cursor=None if next_offset is None else str(next_offset),
    )


async def list_model_hosts(model: str, *, workspace_key: str) -> ModelHostsResponse:
    """The hosts that may serve one OpenRouter language model, fastest first.

    Runs on the workspace's own OpenRouter key when it has one, because only an
    authenticated request is given latency and throughput, and anonymously
    otherwise — the list itself is the same either way.
    """
    if openrouter.entry("llm", model) is None:
        raise HTTPException(
            status_code=404,
            detail=f"unknown OpenRouter model '{model}' - search_models lists the valid slugs",
        )
    try:
        hosts = await openrouter.list_hosts(model, workspace_key or None)
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502, detail=f"OpenRouter did not answer ({exc}); try again"
        ) from None
    return ModelHostsResponse(model=model, hosts=hosts)


async def list_avatars(
    *,
    workspace_key: str,
    active_version: str | None = None,
    render_style: str | None = None,
    offset: int = 0,
    limit: int = 100,
) -> AvatarsResponse:
    """Anam avatar gallery (API key never reaches the browser).

    ``active_version`` and ``render_style`` are live gallery filters (Anam
    activeVersion / renderStyle), not catalog billing models. ``offset`` /
    ``limit`` page the filtered list (max limit 100).
    """
    if not get_catalog().provider_enabled("anam"):
        raise HTTPException(status_code=404, detail="provider 'anam' is not enabled")
    return await anam.list_avatars(
        resolve_credential("anam", workspace_key),
        active_version=active_version,
        render_style=render_style,
        offset=offset,
        limit=limit,
    )


async def list_voices(
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
    """Selectable voices for a TTS or speech-to-speech provider's voice picker."""
    return await fetch_provider_voices(
        provider=provider,
        workspace_key=workspace_key,
        model=model,
        kind=kind,
        search=search,
        language=language,
        accent=accent,
        gender=gender,
        page=page,
    )


async def add_elevenlabs_shared_voice(
    body: AddVoiceRequest, workspace_key: str
) -> AddVoiceResponse:
    """Save a shared-library voice onto the ElevenLabs account that will speak it."""
    return await save_elevenlabs_shared_voice(body, workspace_key)


async def elevenlabs_voice_settings(voice_id: str, *, workspace_key: str) -> VoiceSettingsResponse:
    """One ElevenLabs voice's own stability / similarity_boost."""
    return await fetch_elevenlabs_voice_settings(voice_id, workspace_key)
