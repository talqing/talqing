"""OpenRouter's ~260 tool-capable language models and ~20 speech-to-text
models, kept live in memory.

One provider key reaches every model OpenRouter serves, so this is the only
catalog provider whose models are not written out in ``catalog.yaml``: the list
moves — roughly six tool-capable models a week, and four new ASR models in two
months — and a static copy would be wrong within days. What lives here is the
filtering, the mapping onto ``LLMEntry`` / ``STTEntry`` and one snapshot per
kind refreshed in the background; ``Catalog._find`` consults it last, so a
hand-written entry always wins.

**Reads never block and never fail.** A lookup is a dict lookup against the last
snapshot. Two hazards shape how that snapshot moves:

- *Expiry means refresh, not discard.* A failed refresh keeps the snapshot it
  could not replace. ``REFRESH_INTERVAL_SECONDS`` is how often we TRY;
  staleness on failure is unbounded, and that is correct — clearing it would
  make every OpenRouter model vanish from the editor and from publish
  validation at once.
- *A refresh must never shrink the registry.* A truncated or degraded response
  returning 40 models instead of 260 would make hundreds of slugs unpublishable
  and mark live agents invalid, silently. Anything below ``MIN_REFRESH_RATIO``
  of the current count is rejected and logged loudly — per kind, so 21 speech
  models shrinking to 8 is refused whatever the language models did, and one
  kind failing never discards the other's snapshot.

**Who needs this and who does not.** The API and the text worker run a
refresher; a voice job process runs none and needs none. It is pre-spawned into
an idle pool, handles one call, and on Linux is forked from a clean forkserver
— so it inherits nothing, and filling a registry there would put an OpenRouter
round trip on the call-start path. Nothing on that path asks: the config it runs
had its model and its thinking effort frozen at publish, ``SearchedModels``
carries the rest of what building the client takes — including that every
speech model here is batch — metering resolves the row from the configured
pair, and pricing uses the cost OpenRouter reported. So a
published OpenRouter agent runs with an empty registry; only editing, validating
and publishing one need a full snapshot.

There is deliberately **no seed file**. A generated floor checked into the repo
would be a second source of truth, months stale by the time anyone noticed, and
its failure mode is the bad one — offering a model that no longer exists, so the
call fails instead of the editor. Instead the refresher retries every
``COLD_RETRY_SECONDS`` until it has ever succeeded: an OpenRouter blip during a
deploy costs seconds of "unknown model" in the editor, which is loud, correct
and self-healing, rather than a quietly wrong list.

**This module never reaches the catalog**, because ``Catalog`` calls in here
from inside its own validator and a call back out would recurse. The provider's
own ``models:`` block is passed in by whoever starts the refresher.

Reasoning efforts (``_efforts``) are the one mapping worth stating outright,
because getting it wrong breaks a live turn rather than a dropdown. Measured
against the API on 2026-09-08:

    anthropic/claude-sonnet-5  (mandatory: false, default_enabled: true)
      nothing sent .................. 121 reasoning tokens
      reasoning_effort: "none" ......   0
      reasoning_effort: "minimal" ... 101   (silently aliased to "low")
    openai/gpt-oss-20b, z-ai/glm-5.3-flash  (mandatory: true)
      reasoning_effort: "none" ...... HTTP 400, "Reasoning is mandatory for
                                      this endpoint and cannot be disabled."

So "none" is what stops a Claude voice agent thinking through every turn while
the caller hears silence — and sending it to one of the ~63 mandatory models
fails that agent's first turn. "minimal" is excluded by the intersection rule
rather than by name: Anthropic omits it from ``supported_efforts``, and offering
it would put a control in the editor that does nothing.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal

import httpx

from .models import (
    LLMEntry,
    LLMPricing,
    ModelHostResponse,
    ReasoningEffort,
    SearchedLLMs,
    SearchedModels,
    STTEntry,
    STTPricing,
)

logger = logging.getLogger("talqing.catalog.openrouter")

PROVIDER = "openrouter"

Kind = Literal["llm", "stt"]

# Unauthenticated (measured): neither the refresher nor the search endpoint needs
# a key. `sort=most-popular` is server-side, so one call returns the list already
# ranked by tokens actually processed — better ordering than we would curate, and
# it updates itself. The default list is text-output only and holds no speech
# model at all, so transcription is a second request rather than a filter.
MODELS_URLS: dict[Kind, str] = {
    "llm": "https://openrouter.ai/api/v1/models?limit=1000&sort=most-popular",
    "stt": (
        "https://openrouter.ai/api/v1/models"
        "?output_modalities=transcription&limit=1000&sort=most-popular"
    ),
}
# UNDOCUMENTED — the endpoint OpenRouter's own docs site calls. The documented
# `/api/v1/providers` carries neither an icon nor a data policy. We read `slug`,
# `displayName`, `icon.url` and `dataPolicy.training`. If it goes away, host and
# model-maker logos degrade to an empty slot and `list_hosts` stops filtering out
# training hosts (one of those in a host set is then ignored at request time
# like any other unusable host, and a set of only those 404s legibly).
ALL_PROVIDERS_URL = "https://openrouter.ai/api/frontend/v1/all-providers"
ENDPOINTS_URL = "https://openrouter.ai/api/v1/models/{model}/endpoints"
# Tag suffixes naming a service tier rather than a region or quantization.
SERVICE_TIERS = ("flex", "priority", "fast")
_SITE = "https://openrouter.ai"
REFRESH_INTERVAL_SECONDS = 30 * 60
# How fast to retry while the registry has never loaded. Nothing works for
# OpenRouter until it does, so this is the one state worth being impatient about.
COLD_RETRY_SECONDS = 15
FETCH_TIMEOUT_SECONDS = 30.0
# A refresh returning less than this share of what we already hold is rejected.
MIN_REFRESH_RATIO = 0.8

# Fastest first, matching `REASONING_EFFORT_ORDER` — spelled out here so this
# module can order efforts without reaching for a private constant.
_EFFORT_ORDER: tuple[ReasoningEffort, ...] = ("none", "minimal", "low", "medium", "high")

# Slug shapes we refuse to list. Exclusions of shape, never of quality:
#   :free       request-capped per day against the account's lifetime spend, so a
#               published agent would stop working when the workspace crossed a
#               line nobody set. Unstable besides — some 404 outright.
#   :batch      asynchronous. There is no live turn.
#   ~*-latest   a moving target. Publishing freezes a config so that a version
#               means something; a slug whose model changes underneath makes the
#               version diff a lie.
#   openrouter/*  router slugs. Asked for `openrouter/auto`, we measured a reply
#               from `deepseek/deepseek-v4-flash-0731`: the model that ran is not
#               the model configured, which breaks metering, the version record
#               and the estimate. Our own `llm.FallbackAdapter` already does
#               model fallback, with a catalog entry on each side.
_EXCLUDED_SUFFIXES = (":free", ":batch")
_EXCLUDED_PREFIXES = ("~", f"{PROVIDER}/")

# The `provider` block on every request Talqing sends to OpenRouter, on every
# channel and from every caller (built per request by `routing`). Two settings,
# derived rather than configured — the same call
# `compiler.factories.resolve_turn_detection` makes. The one choice the author
# gets is WHICH hosts may serve (`LLMModelSpec.hosts`, sent as `only`); the
# platform then chooses among the hosts they allowed, or among all of them when
# they allowed none in particular. Exposing OpenRouter's ten routing knobs per
# agent would be the most configurable and least defensible thing we could do.
#
# `sort: "latency"` because the default strategy load-balances on price and was
# measured taking **88 seconds** to answer "what is the capital of France?" on
# `z-ai/glm-5.3-flash` (11.6s / 88.2s / 37.1s over three runs) against 7.0 / 3.4
# / 3.3s sorted by latency. That is not a slow turn, it is a dead one, and it is
# as dead in a chat window as on a phone line — which is why this is not
# conditioned on the channel. The objection to it was cost, and the measurement
# answers that too: `sort` ignores price, and latency-sorting picked Relace at
# $0.071/M — the cheapest of 25 endpoints — while default routing twice picked
# one at $0.150/M *and* took 88s and 37s. Worst penalty seen anywhere: 1.5x.
#
# LLM requests only. `/audio/transcriptions` ignores this block wholesale —
# measured: values `/chat/completions` 400s on are accepted there, and an
# `ignore: ["deepinfra"]` was served BY DeepInfra — so the STT client sends none
# and the privacy page says what that means for caller audio.
#
# `data_collection: "deny"` because OpenRouter picks one of ~106 upstream hosts
# per request and some of them log prompts and train on them. Cost of denying,
# measured: across ten frontier models it blocked nothing (it changed the chosen
# upstream on four, so the filter is real), and on a random sample of 45
# tool-capable models exactly one had no compliant endpoint — ~2% of the long
# tail, 0% of anything anyone would build a voice agent on. This is also the one
# place Talqing sets something on a tenant's behalf, and it only ever tightens;
# `documentation/platform/data-retention-and-privacy.mdx` says so out loud.
#
# Deliberately NOT sent: `require_parameters`. It would guarantee the serving
# endpoint supports `tools`, but it turns a routable request into a 404 with no
# fallback — and OpenRouter already applies `tools` as a soft preference by
# default, routing only to supporting endpoints when any exist. Same benefit, no
# failure mode.
ROUTING_PREFERENCES: dict[str, str] = {"sort": "latency", "data_collection": "deny"}


def routing(hosts: list[str] | None) -> dict[str, Any]:
    """The `provider` block for one request."""
    return {**ROUTING_PREFERENCES, "only": hosts} if hosts else dict(ROUTING_PREFERENCES)


# Sent on every request so OpenRouter's own dashboards attribute the traffic.
ATTRIBUTION_HEADERS: dict[str, str] = {
    "HTTP-Referer": "https://talqing.com",
    "X-Title": "Talqing",
}


@dataclass(frozen=True)
class RegistryHealth:
    """What one kind's snapshot holds and how old it is — reported on `/health`.

    A silently stale registry is the failure mode this design trades for
    freshness, so it has to be visible without reading logs.
    """

    models: int
    age_seconds: float | None  # None before the first refresh has ever landed
    last_error: str | None


@dataclass
class _Snapshot:
    entries: dict[str, LLMEntry | STTEntry] = field(default_factory=dict)
    fetched_at: float | None = None  # time.monotonic() of the last accepted load
    last_error: str | None = None


@dataclass(frozen=True)
class HostMeta:
    """One upstream host, as all-providers describes it."""

    name: str
    logo_url: str | None
    trains: bool  # `dataPolicy.training`: may train on prompts, so never offered


_snapshots: dict[Kind, _Snapshot] = {"llm": _Snapshot(), "stt": _Snapshot()}
# Host slug → metadata. Cosmetic plus a safety filter with a loud fallback, so
# unlike the model kinds it is never on `/health` and never keeps the refresher
# impatient.
_hosts: dict[str, HostMeta] = {}
# The kinds this process runs a refresher for — none at all in a voice job. What
# separates "the registry is empty because OpenRouter is unreachable" from "this
# process correctly has no registry"; `/health` must not report the second as a
# fault.
_refreshing: list[Kind] = []


# ───────────────────────────── the OpenRouter list ──────────────────────────


def _slug_excluded(slug: str) -> bool:
    return not slug or slug.startswith(_EXCLUDED_PREFIXES) or slug.endswith(_EXCLUDED_SUFFIXES)


def keep_llm(model: dict[str, Any]) -> bool:
    """Whether one `/models` record becomes a language-model entry.

    Three filters, and the first two are modality filters wearing a slug's
    clothes. Text output drops the music, image and translation models; `tools`
    drops what is left that no agent could use — a classifier, a bare completion
    model — and, usefully, most of the roleplay tier, so no taste judgment is
    made here and none has to be defended. The third is `_EXCLUDED_*` above.

    Nothing else is filtered. The tail is obsolete rather than embarrassing
    (`gpt-4`, `o1`, `reka-edge`) and it stays listed, at the bottom: a
    popularity floor is a magic number that silently drops a model somebody
    runs, and age is a bad proxy — 84 of these are over a year old, including
    ones that are simply stable.
    """
    if _slug_excluded(str(model.get("id") or "")):
        return False
    architecture = model.get("architecture") or {}
    if architecture.get("output_modalities") != ["text"]:
        return False
    return "tools" in (model.get("supported_parameters") or [])


def keep_stt(model: dict[str, Any]) -> bool:
    """Whether one transcription record becomes a speech-to-text entry.

    The slug exclusions only — no speech slug matches one today, and they are
    kept so the first that does is handled by the provider's one policy rather
    than a second. Nothing is excluded for being slow or obscure, for the reason
    `keep_llm` gives about popularity floors; the one slow model says so in its
    `note` instead (`_STT_NOTES`).
    """
    if _slug_excluded(str(model.get("id") or "")):
        return False
    return (model.get("architecture") or {}).get("output_modalities") == ["transcription"]


def _efforts(model: dict[str, Any]) -> list[ReasoningEffort]:
    """The thinking settings we offer for one model, fastest first.

    `supported_efforts` intersected with our own vocabulary — OpenRouter adds
    `xhigh` and `max` above `high`, which we do not carry: two more Literal
    members touching validation, the editor and the ordering rule on every
    provider, to buy latency nobody running a voice agent wants.

    Then `none` on top wherever thinking can be turned off at all, because the
    first value is the default every agent on the model gets and a voice caller
    hears thinking as silence. Three shapes come out of this:

    - a full ladder — `[none, low, medium, high]` on Claude Sonnet 5;
    - `[none]` alone, for the ~100 models that can only switch thinking on or
      off. The editor hides a one-value control and `none` is what we send,
      which is the right default and the only one we can express;
    - `[]`, for a model with no knob at all and for a mandatory-reasoning model
      that publishes no levels — nothing goes on the wire and it thinks as it
      likes.
    """
    reasoning = model.get("reasoning")
    if not isinstance(reasoning, dict):
        return []
    supported = set(reasoning.get("supported_efforts") or ())
    if not reasoning.get("mandatory"):
        supported.add("none")
    return [effort for effort in _EFFORT_ORDER if effort in supported]


def _per_million(pricing: dict[str, Any], key: str) -> float | None:
    """One OpenRouter rate as $/1M tokens, or None when the model does not
    charge that way.

    Its own numbers are per token, as decimal strings. Multiplied through
    ``Decimal`` rather than as floats because the binary expansion is what a
    tenant would read: `0.0000002` scaled in float lands on 0.19999999999999998,
    and that is the number the picker and the cost estimate would show.
    """
    raw = pricing.get(key)
    if raw is None:
        return None
    return float(Decimal(str(raw)) * 1_000_000)


def _note(model: dict[str, Any]) -> str | None:
    """A caveat this exact model's author has to act on, or nothing.

    Only the long-context tier qualifies. Everything else true of these models
    is true of all ~260 of them — that a request lands on one of ~106 upstream
    hosts, that the listed price is the cheapest endpoint's — and belongs beside
    the picker, said once, rather than repeated on every row.
    """
    overrides = (model.get("pricing") or {}).get("overrides")
    floors = [
        o["min_prompt_tokens"]
        for o in overrides or ()
        if isinstance(o, dict) and o.get("min_prompt_tokens")
    ]
    if not floors:
        return None
    return (
        f"Priced in tiers: the rates shown apply up to {min(floors):,} prompt tokens and "
        "rise above that. Your invoice uses what OpenRouter actually charged, so it is "
        "right either way — the estimate is what understates a long conversation."
    )


def to_llm_entry(model: dict[str, Any], models: SearchedLLMs) -> LLMEntry:
    """One `/models` record as a catalog entry.

    `vision` comes from `input_modalities` rather than from a probe — see
    `LLMEntry.vision` for why that is safe here and nowhere else. The pricing is
    the cheapest endpoint's, which is what the editor's estimate and the picker
    show; nothing bills from it (`price_llm_usage` uses the cost OpenRouter
    reports per request instead), and the gap is not small — two consecutive
    requests for `llama-3.3-70b-instruct` measured 2.4x apart.
    """
    pricing = model.get("pricing") or {}
    architecture = model.get("architecture") or {}
    return LLMEntry(
        provider=PROVIDER,
        model=str(model["id"]),
        label=str(model.get("name") or model["id"]),
        channel=["text", "voice", "video"],
        note=_note(model),
        vision="image" in (architecture.get("input_modalities") or []),
        reasoning_efforts=_efforts(model),
        context_length=model.get("context_length"),
        pricing=LLMPricing(
            input_per_1m=_per_million(pricing, "prompt"),
            cached_input_per_1m=_per_million(pricing, "input_cache_read"),
            cache_write_per_1m=_per_million(pricing, "input_cache_write"),
            output_per_1m=_per_million(pricing, "completion"),
        ),
        api=models.api,
        base_url=models.base_url,
        supports_prompt_cache_key=models.supports_prompt_cache_key,
        extra=dict(models.extra),
    )


# Latency measured 2026-09-22 on a 4.56-second utterance: the time a caller
# waits after they stop speaking, on top of end-of-speech detection. Every other
# model came back in 0.6-3.4 s; this one is not a slow turn, it is a dead one,
# and its brand is exactly what would make a voice author pick it unseen.
_STT_NOTES: dict[str, str] = {
    "openai/whisper-large-v3": (
        "Measured at about 11.6 seconds to transcribe a 4.6-second utterance, so the "
        "caller waits that long after they stop speaking. Too slow for a live "
        "conversation — Whisper Large V3 Turbo is the same family at about 2 seconds."
    ),
}


def to_stt_entry(model: dict[str, Any]) -> STTEntry:
    """One transcription record as a catalog entry.

    Always batch: OpenRouter has no streaming transcription surface at all, so
    every model is wrapped in LiveKit's `StreamAdapter` and the local VAD ends
    the caller's turn.

    `languages` is left empty on purpose. OpenRouter publishes no per-model
    language list, and a code a model does not really know does not fail — it
    RELABELS (English audio sent to qwen3-asr-flash as `hi` came back in
    Devanagari). An entry with no languages is skipped by agent language
    validation and sent no code, so every model auto-detects.

    `pricing.prompt` carries a different unit per model with no field saying
    which. The discriminator is `completion`: non-zero on exactly the two
    token-priced models (gpt-4o-transcribe, gpt-4o-mini-transcribe), zero on
    every per-audio-second one — including voxtral-mini-transcribe, which REPORTS
    tokens and is not priced by them. Display only, like the LLM rates: billing
    uses the cost OpenRouter returns per request, and the card is the cheapest
    host's (identical whisper-large-v3 requests measured 3.3x apart).
    """
    pricing = model.get("pricing") or {}
    per_token = Decimal(str(pricing.get("completion") or "0")) != 0
    return STTEntry(
        provider=PROVIDER,
        model=str(model["id"]),
        label=str(model.get("name") or model["id"]),
        channel=["voice", "video"],
        note=_STT_NOTES.get(str(model["id"])),
        streaming=False,
        pricing=STTPricing(
            input_per_1m=_per_million(pricing, "prompt"),
            output_per_1m=_per_million(pricing, "completion"),
        )
        if per_token
        else STTPricing(
            per_audio_second=None
            if pricing.get("prompt") is None
            else float(Decimal(str(pricing["prompt"])))
        ),
    )


def build_entries(
    kind: Kind, payload: dict[str, Any], models: SearchedModels
) -> dict[str, LLMEntry | STTEntry]:
    """One kind's `/models` response, filtered and mapped, in OpenRouter's order.

    Insertion order IS the popularity rank — the request asks for it server-side
    — so no entry carries a rank field of its own.
    """
    data = payload.get("data") or []
    if kind == "stt":
        return {str(m["id"]): to_stt_entry(m) for m in data if keep_stt(m)}
    assert models.llm is not None
    return {str(m["id"]): to_llm_entry(m, models.llm) for m in data if keep_llm(m)}


# ──────────────────────────────── the snapshot ──────────────────────────────


def entry(kind: str, model: str) -> LLMEntry | STTEntry | None:
    """One model by its exact slug, or None. Never blocks, never raises."""
    snapshot = _snapshots.get(kind)  # type: ignore[call-overload]
    return None if snapshot is None else snapshot.entries.get(model)


def search(
    kind: Kind, query: str, *, limit: int, offset: int
) -> tuple[list[LLMEntry | STTEntry], int | None]:
    """One page of the registry, and the offset of the next (None when done).

    With no query the order is popularity, so the first screen is the models
    people actually run rather than whatever sorts first alphabetically. With
    one, an exact slug match leads and substring matches on the slug and the
    label follow — popularity breaking the ties, so "claude" puts the Claude
    everyone uses above a year-old one.
    """
    ranked = list(_snapshots[kind].entries.values())
    q = query.strip().lower()
    if q:
        # Stable sort over an already-ranked list, so rank survives as the
        # tiebreak without being carried on the entry.
        ranked = [e for e in ranked if q in e.model.lower() or q in (e.label or "").lower()]
        ranked.sort(key=lambda e: e.model.lower() != q)
    page = [
        e.model_copy(update={"logo_url": maker_logo(e.model)})
        for e in ranked[offset : offset + limit]
    ]
    return page, offset + limit if offset + limit < len(ranked) else None


# Model makers whose OpenRouter slug prefix is not their host slug. The rest
# (`openai`, `anthropic`, `z-ai`, …) resolve directly; an unmapped long-tail
# maker gets no logo. Not `bytedance-seed` → `seed`: that host's icon is a
# favicon of its GitHub avatar URL, so it renders GitHub's mark (checked
# 2026-09-23).
_MAKER_HOST = {
    "x-ai": "xai",
    "mistralai": "mistral",
    "meta-llama": "meta",
    "google": "google-ai-studio",
    "qwen": "alibaba",
    "rekaai": "reka",
    "amazon": "amazon-bedrock",
}


def maker_logo(model: str) -> str | None:
    """The logo of whoever made `model`, from its slug prefix, or None.

    Read at search time rather than baked into the model snapshot, so a host
    snapshot that was still cold when the models loaded costs no logo for half
    an hour.
    """
    maker = model.split("/")[0]
    meta = _hosts.get(maker) or _hosts.get(_MAKER_HOST.get(maker, ""))
    return None if meta is None else meta.logo_url


def health() -> dict[Kind, RegistryHealth] | None:
    """Each refreshed kind's size and age, or None in a process that runs no
    refresher."""
    if not _refreshing:
        return None
    return {
        kind: RegistryHealth(
            models=len(snapshot.entries),
            age_seconds=None
            if snapshot.fetched_at is None
            else round(time.monotonic() - snapshot.fetched_at, 1),
            last_error=snapshot.last_error,
        )
        for kind in _refreshing
        for snapshot in [_snapshots[kind]]
    }


# ──────────────────────────────── refreshing ────────────────────────────────


async def refresh(client: httpx.AsyncClient, kind: Kind, models: SearchedModels) -> bool:
    """Fetch one kind's list once and swap it in. True when the snapshot moved.

    Every failure — transport, a bad payload, a suspiciously short list — leaves
    the previous snapshot exactly as it was. There is no state in which this
    function makes the registry worse.
    """
    snapshot = _snapshots[kind]
    try:
        response = await client.get(MODELS_URLS[kind])
        response.raise_for_status()
        fetched = build_entries(kind, response.json(), models)
    except Exception as exc:  # noqa: BLE001 - any failure keeps last-known-good
        snapshot.last_error = f"{type(exc).__name__}: {exc}"
        logger.warning(
            "openrouter %s refresh failed, keeping %d models: %s",
            kind,
            len(snapshot.entries),
            exc,
        )
        return False

    floor = int(len(snapshot.entries) * MIN_REFRESH_RATIO)
    if len(fetched) < floor:
        # The single most important line in this module. A partial response would
        # otherwise make hundreds of published slugs unresolvable at once —
        # agents invalid, publishes refused — with nothing saying why.
        snapshot.last_error = f"refresh returned {len(fetched)} models, below a floor of {floor}"
        logger.error(
            "openrouter %s refresh returned %d models against %d held (floor %d) — rejected",
            kind,
            len(fetched),
            len(snapshot.entries),
            floor,
        )
        return False

    snapshot.entries = fetched
    snapshot.fetched_at = time.monotonic()
    snapshot.last_error = None
    logger.info("openrouter %s registry refreshed: %d models", kind, len(fetched))
    return True


async def refresh_hosts(client: httpx.AsyncClient) -> None:
    """Fetch the host metadata once and swap it in, by `refresh`'s rules: any
    failure, or a list below `MIN_REFRESH_RATIO` of what we hold, keeps the old
    snapshot."""
    try:
        response = await client.get(ALL_PROVIDERS_URL)
        response.raise_for_status()
        fetched: dict[str, HostMeta] = {}
        for host in response.json()["data"]:
            icon = (host.get("icon") or {}).get("url")
            fetched[str(host["slug"])] = HostMeta(
                name=str(host.get("displayName") or host["slug"]),
                # 22 of 89 are site-relative (`/images/icons/Anthropic.svg`).
                logo_url=f"{_SITE}{icon}" if icon and icon.startswith("/") else icon,
                trains=bool((host.get("dataPolicy") or {}).get("training")),
            )
    except Exception as exc:  # noqa: BLE001 - any failure keeps last-known-good
        logger.warning("openrouter hosts refresh failed, keeping %d: %s", len(_hosts), exc)
        return
    floor = int(len(_hosts) * MIN_REFRESH_RATIO)
    if len(fetched) < floor:
        logger.error(
            "openrouter hosts refresh returned %d against %d held (floor %d) — rejected",
            len(fetched),
            len(_hosts),
            floor,
        )
        return
    _hosts.clear()
    _hosts.update(fetched)


async def run_refresher(models: SearchedModels) -> None:
    """Keep the snapshot current, forever. Started by the API and the text
    worker; a voice job process starts none — see the module docstring.

    Impatient while any kind is cold and unhurried once all are warm: until a
    refresh has ever succeeded there is no OpenRouter model of that kind in the
    editor at all, so that state is worth retrying out of, while a snapshot half
    an hour old is fine.
    """
    _refreshing[:] = models.kinds()
    while True:
        async with httpx.AsyncClient(timeout=FETCH_TIMEOUT_SECONDS) as client:
            await refresh_hosts(client)
            for kind in _refreshing:
                await refresh(client, kind, models)
        cold = any(not _snapshots[kind].entries for kind in _refreshing)
        await asyncio.sleep(COLD_RETRY_SECONDS if cold else REFRESH_INTERVAL_SECONDS)


# ─────────────────────────────── one model's hosts ──────────────────────────


async def list_hosts(model: str, api_key: str | None) -> list[ModelHostResponse]:
    """The endpoints that may serve one language model, fastest first.

    Live, not cached: the picker opens rarely and a cache would serve stale
    latency. With a key OpenRouter fills in latency and throughput (without one
    they are null); the list itself is the same either way, and is NOT filtered
    by the account's privacy settings (measured). Raises `httpx.HTTPError` when
    OpenRouter cannot answer.

    Dropped, because each one fails a request we would otherwise send:
    - an endpoint without `tools` — siliconflow 400s a request carrying them;
    - a `/flex` tier — the queued, cheaper tier trades away the latency a live
      turn needs;
    - a host whose policy is training — refused by our `data_collection: deny`
      and by the account setting alike, with a 404.
    """
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    async with httpx.AsyncClient(timeout=FETCH_TIMEOUT_SECONDS) as client:
        response = await client.get(ENDPOINTS_URL.format(model=model), headers=headers)
        response.raise_for_status()
    kept: dict[str, dict[str, Any]] = {}
    for endpoint in response.json()["data"]["endpoints"]:
        tag = str(endpoint["tag"])
        host = _hosts.get(tag.split("/")[0])
        if (
            tag in kept  # duplicates occur (`baseten/fp4` twice on gpt-oss-120b)
            or tag.endswith("/flex")
            or "tools" not in (endpoint.get("supported_parameters") or [])
            or (host is not None and host.trains)
        ):
            continue
        kept[tag] = endpoint

    # What a bare base slug also matches: its host's other endpoints, except the
    # service tiers, which OpenRouter only routes to when named in full.
    bases = [tag.split("/")[0] for tag in kept if tag.rsplit("/", 1)[-1] not in SERVICE_TIERS]
    hosts: list[ModelHostResponse] = []
    for tag, endpoint in kept.items():
        pricing = endpoint.get("pricing") or {}
        latency = endpoint.get("latency_last_30m") or {}
        throughput = endpoint.get("throughput_last_30m") or {}
        quantization = endpoint.get("quantization")
        host = _hosts.get(tag.split("/")[0])
        hosts.append(
            ModelHostResponse(
                host=tag,
                name=str(endpoint["provider_name"]),
                logo_url=None if host is None else host.logo_url,
                quantization=None if quantization in (None, "unknown") else str(quantization),
                pricing=LLMPricing(
                    input_per_1m=_per_million(pricing, "prompt"),
                    cached_input_per_1m=_per_million(pricing, "input_cache_read"),
                    cache_write_per_1m=_per_million(pricing, "input_cache_write"),
                    output_per_1m=_per_million(pricing, "completion"),
                ),
                context_length=endpoint.get("context_length"),
                latency_p50_ms=None if latency.get("p50") is None else round(latency["p50"]),
                throughput_p50=None if throughput.get("p50") is None else round(throughput["p50"]),
                uptime_30m=endpoint.get("uptime_last_30m"),
                # OpenRouter matches a bare base slug against every endpoint of
                # that host, so where it sits beside its own variants it is not
                # "the plain one" but all of them.
                all_endpoints="/" not in tag and bases.count(tag) > 1,
            )
        )
    # Stable, so OpenRouter's own order (price) survives among the unmeasured.
    hosts.sort(key=lambda h: (h.latency_p50_ms is None, h.latency_p50_ms or 0))
    return hosts
