"""Catalog HTTP adapter over services.catalog.

The gallery routes load the workspace's own key for the provider being browsed
and pass it down. That is done here rather than inside ``services.catalog``
because ``services.byok`` imports the catalog (to validate provider names), so
the catalog cannot import it back.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Query

from api.dataplane.deps import Context, CtxDep
from services import byok, catalog
from services.catalog import (
    AddVoiceRequest,
    AddVoiceResponse,
    AvatarsResponse,
    CatalogResponse,
    ModelHostsResponse,
    ModelSearchResponse,
    VoiceKind,
    VoiceSettingsResponse,
    VoicesResponse,
)

router = APIRouter(prefix="/catalog", tags=["catalog"])


@router.get("", response_model=CatalogResponse)
async def get_catalog(_ctx: Context = CtxDep):
    """The provider catalog: every LLM, STT, TTS, realtime and avatar model that
    agents may use.

    The authoritative source for `provider`/`model` pairs and for what each
    entry supports — channels, language codes, speed range, default voice. A
    config naming anything not listed here is rejected.

    `realtime` lists the speech-to-speech models. One of those replaces `stt`,
    `llm` and `tts` together, so an agent sets either `realtime` or the three
    cascade slots, never both.

    One provider's models are NOT here: a `providers` entry whose `browse` is
    `search` reaches hundreds of them through one key, and they are searched
    through `GET /catalog/models` instead.
    """
    return await catalog.get_public_catalog()


@router.get("/models", response_model=ModelSearchResponse)
async def search_models(
    q: str = "",
    limit: int = Query(20, ge=1, le=50),
    cursor: str = "",
    kind: Literal["llm", "stt"] = "llm",
    _ctx: Context = CtxDep,
):
    """Search the models a provider offers too many of to list.

    `GET /catalog` carries every model except these. A provider marked
    `browse: "search"` there — OpenRouter — reaches hundreds of models through
    one key, so they are searched a page at a time instead; its `searched_kinds`
    says which `kind`s. With no `q` the order is real-world popularity. Everything
    returned is a valid `llm.model` or `stt.model`, per `kind`.
    """
    return await catalog.search_models(q, limit, cursor, kind)


@router.get("/models/hosts", response_model=ModelHostsResponse)
async def list_model_hosts(model: str, ctx: Context = CtxDep):
    """The hosts that can serve one OpenRouter language model; fastest first when the workspace
    has an OpenRouter key. Each `host` is a valid entry for `llm.hosts`."""
    return await catalog.list_model_hosts(
        model, workspace_key=await byok.load_provider_key(ctx.tenant, "openrouter")
    )


@router.get("/avatars", response_model=AvatarsResponse)
async def catalog_avatars(
    active_version: str | None = None,
    render_style: str | None = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=100),
    ctx: Context = CtxDep,
):
    """The Anam avatar gallery — the faces a video agent can wear.

    An avatar's `id` is what a video agent's `avatar.avatar_id` refers to.
    `active_version` and `render_style` are gallery facets, not billing models —
    an agent's `avatar.model` always comes from the provider catalog.

    Shows your own Anam account when you have a key for it and Talqing's
    otherwise; `workspace_key` says which.
    """
    return await catalog.list_avatars(
        workspace_key=await byok.load_provider_key(ctx.tenant, "anam"),
        active_version=active_version,
        render_style=render_style,
        offset=offset,
        limit=limit,
    )


@router.get("/voices", response_model=VoicesResponse)
async def catalog_voices(
    provider: str,
    model: str | None = None,
    kind: VoiceKind = "tts",
    search: str = "",
    language: str = "",
    accent: str = "",
    gender: str = "",
    page: int = 0,
    ctx: Context = CtxDep,
):
    """Browse and filter the voices available for one provider.

    A voice's `id` is what an agent's `tts.voice` refers to, or `realtime.voice`
    when `kind` is `realtime` — speech-to-speech models pick from their own
    roster. Use this before choosing a voice by style, accent or gender rather
    than assuming an id exists.

    With a workspace key saved for the provider the listing runs on that key and
    leads with your own account's voices; `workspace_key` says whether it did.
    """
    return await catalog.list_voices(
        provider,
        await byok.load_provider_key(ctx.tenant, provider),
        model,
        kind,
        search,
        language,
        accent,
        gender,
        page,
    )


@router.post("/voices/elevenlabs/add", response_model=AddVoiceResponse)
async def add_elevenlabs_voice(body: AddVoiceRequest, ctx: Context = CtxDep):
    """Save a shared ElevenLabs library voice so agents can use it.

    Takes the `voice_id` and `owner_id` of a shared voice and returns the id to
    set as an agent's `tts.voice`. Saved into your workspace's own ElevenLabs
    account when you have a key for it. Voices already in that account come back
    from `catalog_voices` with no `owner_id` and need no save at all.
    """
    return await catalog.add_elevenlabs_shared_voice(
        body, await byok.load_provider_key(ctx.tenant, "elevenlabs")
    )


@router.get("/voices/elevenlabs/{voice_id}/settings", response_model=VoiceSettingsResponse)
async def elevenlabs_voice_settings(voice_id: str, ctx: Context = CtxDep):
    """The `stability` and `similarity_boost` an ElevenLabs voice was tuned with.

    Copy these onto an agent's `tts` when you set its `voice`: ElevenLabs
    applies a voice's own settings only when a request carries no overrides at
    all, and Talqing always sends a speed — so an agent without these numbers
    speaks in the generic default rather than the voice its author shipped.

    A shared-library voice must be saved by `add_elevenlabs_voice` first; call
    this with the id THAT returned.
    """
    return await catalog.elevenlabs_voice_settings(
        voice_id, workspace_key=await byok.load_provider_key(ctx.tenant, "elevenlabs")
    )
