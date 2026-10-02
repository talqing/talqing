"""provider → real plugin instance.

Always returns concrete plugin objects pointed at the provider's own API —
never model strings (which would route through the LiveKit Cloud gateway).

Talqing is strict BYOK: every builder here takes the `provider_keys` map the
caller loaded for whoever is paying. Agent runs pass the tenant's own keys
(`services.byok.load_provider_keys`); platform-owned features that Talqing pays
for — the CoPilots — pass `settings.provider_secrets`. There is no
fallback between the two: a missing key raises rather than quietly billing the
wrong party.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
from decimal import Decimal
from typing import Any, Literal

from google.genai import types as google_types
from livekit import rtc
from livekit.agents import llm, stt, tts
from livekit.agents import vad as lk_vad
from livekit.agents.metrics import LLMMetrics
from livekit.agents.types import (
    DEFAULT_API_CONNECT_OPTIONS,
    NOT_GIVEN,
    APIConnectOptions,
    NotGivenOr,
)
from livekit.agents.utils import AudioBuffer, is_given
from livekit.plugins import (
    ai_coustics,
    deepgram,
    elevenlabs,
    google,
    openai,
    sarvam,
    silero,
    soniox,
    xai,
)

# The Responses plugin narrows `chat()` to its own stream type but does not
# re-export it, and an override has to keep the narrower return type.
from livekit.plugins.openai.responses.llm import LLMStream as ResponsesLLMStream

# The `openai` name above is LiveKit's plugin, not the SDK — this is the SDK's
# own error class, which our OpenRouter client catches and rewords.
from openai import APIStatusError

# The realtime session config types the OpenAI plugin re-serializes verbatim —
# the xAI plugin subclasses that plugin, so it speaks the same dialect.
from openai.types.realtime.audio_transcription import AudioTranscription
from openai.types.realtime.realtime_audio_input_turn_detection import ServerVad

from compiler.provider_tools import provider_tool_type

# Both halves of Raya are ours: they ship no STT plugin, and their TTS one turns
# an ordinary pause in the conversation into a spurious synthesis failure. The
# header of plugins/raya/tts.py has the mechanism. Their package is still a
# dependency — our TTS imports its sentence tokenizer — so it registers itself
# with the SDK on import and stays in the forkserver preload list either way.
from plugins import raya
from services.agents import (
    LLMModelSpec,
    LLMSpec,
    NoiseCancellationSpec,
    RealtimeSpec,
    STTModelSpec,
    STTSpec,
    TTSModelSpec,
    TTSSpec,
    TurnHandlingSpec,
)
from services.billing import ReportedCost
from services.catalog import (
    TURN_DETECTOR_LANGUAGES,
    LLMEntry,
    RealtimeEntry,
    SearchedLLMs,
    SearchedSTTs,
    STTEntry,
    TTSEntry,
    get_catalog,
    openrouter,
    resolve_entry_language,
)
from services.metering import billable_output_tokens
from settings import get_settings

logger = logging.getLogger("talqing.compiler.factories")


class _NamedXaiSTT(xai.STT):
    """An xAI STT that reports which catalog entry built it.

    The plugin leaves `provider`/`model` at the base class's "unknown", which was
    survivable while xAI had a single STT entry (the catalog aliased "unknown" to
    it). It stops being survivable now that the streaming and REST modes are
    separate entries billed at different rates: both are this same class, so
    without a name they would meter as one indistinguishable row.
    """

    def __init__(self, *, catalog_model: str, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._catalog_model = catalog_model

    @property
    def provider(self) -> str:
        return "xai"

    @property
    def model(self) -> str:
        return self._catalog_model


@dataclasses.dataclass
class _ElevenVoiceSettings(elevenlabs.VoiceSettings):
    """`VoiceSettings` with the two voice-shaping knobs optional.

    The plugin types both as required floats, then runs the object through its
    own `_strip_nones` before sending (chunked and websocket paths alike), so
    `None` here reaches the wire as an ABSENT key rather than as a number. That
    is the whole point: any number we invented would override what the voice's
    author tuned, and ElevenLabs offers no "use the voice's value" sentinel.
    """

    stability: float | None = None
    similarity_boost: float | None = None


# OpenAI transcription models served only over the realtime socket, which is
# also the set that refuses a server-side turn detector ("Turn detection is not
# supported for this transcription model", measured 2026-09-22). They therefore
# need a local VAD to commit the audio buffer, and the EOS bridge below.
# Mirrors `_REALTIME_ONLY_MODELS` in livekit.plugins.openai.stt.
_OPENAI_CLIENT_COMMIT_MODELS = ("gpt-live-transcribe", "gpt-realtime-whisper")


class _EndOfSpeechAfterFinalSTT(stt.STT):
    """Inject STT EOS for providers that finalize text but lack an EOS event.

    OpenAI's realtime-only transcription models require client-side audio-buffer
    commits. The LiveKit plugin emits FINAL_TRANSCRIPT when that committed buffer
    is transcribed, but does not emit END_OF_SPEECH. Talqing uses STT
    endpointing, and LiveKit's STT mode commits turns from END_OF_SPEECH, so
    bridge that provider gap locally.
    """

    def __init__(self, inner: stt.STT) -> None:
        super().__init__(capabilities=inner.capabilities)
        self._inner = inner

    @property
    def provider(self) -> str:
        return self._inner.provider

    @property
    def model(self) -> str:
        return self._inner.model

    async def _recognize_impl(
        self,
        buffer: AudioBuffer,
        *,
        language: NotGivenOr[str] = NOT_GIVEN,
        conn_options: APIConnectOptions,
    ) -> stt.SpeechEvent:
        return await self._inner.recognize(
            buffer,
            language=language,
            conn_options=conn_options,
        )

    def stream(
        self,
        *,
        language: NotGivenOr[str] = NOT_GIVEN,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> stt.SpeechStream:
        return _EndOfSpeechAfterFinalStream(
            stt=self,
            inner=self._inner.stream(language=language, conn_options=conn_options),
            conn_options=conn_options,
        )

    async def aclose(self) -> None:
        await self._inner.aclose()


class _EndOfSpeechAfterFinalStream(stt.SpeechStream):
    def __init__(
        self,
        *,
        stt: _EndOfSpeechAfterFinalSTT,
        inner: stt.SpeechStream,
        conn_options: APIConnectOptions,
    ) -> None:
        super().__init__(stt=stt, conn_options=conn_options)
        self._inner = inner
        self._eos_after_final_request_ids: set[str] = set()

    async def _run(self) -> None:
        async def forward_input() -> None:
            async for data in self._input_ch:
                if isinstance(data, rtc.AudioFrame):
                    self._inner.push_frame(data)
                elif isinstance(data, self._FlushSentinel):
                    self._inner.flush()
            self._inner.end_input()

        forward_task = asyncio.create_task(forward_input())
        try:
            async for ev in self._inner:
                self._event_ch.send_nowait(ev)
                if ev.type == stt.SpeechEventType.FINAL_TRANSCRIPT and ev.alternatives:
                    already_emitted = (
                        bool(ev.request_id) and ev.request_id in self._eos_after_final_request_ids
                    )
                    if ev.alternatives[0].text and not already_emitted:
                        if ev.request_id:
                            self._eos_after_final_request_ids.add(ev.request_id)
                        self._event_ch.send_nowait(
                            stt.SpeechEvent(type=stt.SpeechEventType.END_OF_SPEECH)
                        )
        finally:
            forward_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await forward_task
            await self._inner.aclose()


class _CountsThinkingTokens:
    """Mixin: bill the thinking a provider does but does not report.

    LiveKit builds `LLMMetrics` from whatever the provider called "completion",
    and everything downstream — the session's usage collector, `llm_usage`,
    pricing — inherits that number. On both Gemini paths it is wrong: thinking
    tokens are billed as output but appear only in the gap between
    `total_tokens` and prompt + completion (see `services.metering`).

    This is the last point where all three counts still exist together — the
    collector keeps two of them and drops the total — so the repair happens here,
    on the way out of the LLM object, rather than at each of the metering taps
    that would by then have nothing left to repair it with.
    """

    def emit(self, event: str, *args: Any) -> None:
        if event == "metrics_collected" and args and isinstance(args[0], LLMMetrics):
            metrics: LLMMetrics = args[0]
            billable = billable_output_tokens(
                prompt_tokens=metrics.prompt_tokens,
                completion_tokens=metrics.completion_tokens,
                total_tokens=metrics.total_tokens,
            )
            if billable != metrics.completion_tokens:
                logger.debug(
                    "%s reported %d completion tokens but bills %d — counting the difference "
                    "as unreported thinking",
                    getattr(self, "model", "llm"),
                    metrics.completion_tokens,
                    billable,
                )
                metrics = metrics.model_copy(update={"completion_tokens": billable})
                args = (metrics, *args[1:])
        super().emit(event, *args)  # type: ignore[misc]


class _ChatCompletionsLLM(_CountsThinkingTokens, openai.LLM):
    """The Chat Completions client, plain apart from the token count above."""


# How many requests' cache-write counts an `_OpenRouterLLM` keeps while it waits
# for the matching metrics. One is the normal depth — the metrics for a request
# are emitted seconds after its last chunk — and the cap only matters for a
# request that reported usage and then failed before its metrics were built,
# which leaves an entry nobody comes back for.
_PENDING_CACHE_WRITES = 32


class _RecordsCost:
    """Passes an OpenAI stream through untouched, keeping two numbers off it.

    OpenRouter reports both on the final streamed chunk, beside the token counts
    LiveKit already reads, and LiveKit drops both:

    - `usage.cost`, the exact charge for the request. `llm.CompletionUsage` has
      no such field, so by the time it would reach billing it no longer exists.
      It goes straight into the session's collector — no correlation needed,
      because the total is all anyone asks for.
    - `usage.prompt_tokens_details.cache_write_tokens`. `CompletionUsage` DOES
      have somewhere to put this (`cache_creation_tokens`, which `LLMMetrics`
      and `LLMModelUsage` both carry) and `inference/llm.py` simply never fills
      it. That one has to reach the metrics for this request and not another, so
      it is left here under the chunk's own id for `_OpenRouterLLM.emit` to
      claim — the same id `_metrics_monitor_task` reads back as `request_id`.

    It is a monkey-patch on one client object we construct ourselves (see
    `_OpenRouterLLM`), and that is the smaller of two evils: the alternative is
    overriding `LLMStream._run`, which means copying ~80 lines of LiveKit's
    request assembly and keeping them in step across releases. Nothing global is
    touched and the stream's own behaviour is unchanged.
    """

    def __init__(
        self,
        inner: Any,
        collector: ReportedCost | None,
        provider: str,
        model: str,
        cache_writes: dict[str, int],
    ) -> None:
        self._inner = inner
        # None for the platform's own features, which are metered nowhere. The
        # wrapper still goes on, because the error translation below is about
        # what a person reads and not about billing.
        self._collector = collector
        self._provider = provider
        self._model = model
        self._cache_writes = cache_writes

    def _record(self, chunk: Any) -> None:
        usage = getattr(chunk, "usage", None)
        # `cost` is an extra field on the SDK's typed usage object, so it is
        # reachable but never guaranteed — hence the getattr rather than a read.
        cost = getattr(usage, "cost", None) if usage is not None else None
        if cost is not None and self._collector is not None:
            self._collector.add(self._provider, self._model, Decimal(str(cost)))
        details = getattr(usage, "prompt_tokens_details", None) if usage is not None else None
        written = int(getattr(details, "cache_write_tokens", 0) or 0) if details else 0
        request_id = str(getattr(chunk, "id", "") or "")
        if written and request_id:
            if len(self._cache_writes) >= _PENDING_CACHE_WRITES:
                # Oldest first: a dict preserves insertion order, and anything
                # still here after 32 later requests belongs to one whose
                # metrics were never built.
                del self._cache_writes[next(iter(self._cache_writes))]
            self._cache_writes[request_id] = written
        # A mid-stream failure arrives inside an HTTP 200: OpenRouter commits the
        # status as soon as an upstream accepts, so a later disconnect, timeout or
        # capacity error comes back as a chunk carrying a top-level `error`. The
        # SDK raises on it a moment later, but as a plain APIError, which LiveKit
        # re-reports as a connection error — so the upstream's own reason is only
        # legible from here.
        error = getattr(chunk, "error", None)
        if error:
            logger.warning("openrouter reported a mid-stream error on %s: %s", self._model, error)

    def __aiter__(self) -> _RecordsCost:
        self._iterator = self._inner.__aiter__()
        return self

    async def __anext__(self) -> Any:
        chunk = await self._iterator.__anext__()
        self._record(chunk)
        return chunk

    async def __aenter__(self) -> _RecordsCost:
        await self._inner.__aenter__()
        return self

    async def __aexit__(self, *exc: Any) -> Any:
        return await self._inner.__aexit__(*exc)


def _legible_openrouter_error(
    exc: APIStatusError,
    *,
    transcription: bool,
    model: str | None = None,
    hosts: list[str] | None = None,
) -> APIStatusError:
    """The same failure, said in terms the workspace can act on.

    Two of OpenRouter's refusals reach a tenant as advice they cannot follow.

    **Any 404 while the author chose hosts** means none of them could serve —
    OpenRouter already failed over between them inside the request. It says "No
    allowed providers…", or "…data policy…" for a set of only training hosts,
    and either way the fix is the author's host set, not the model. The
    account-settings clause is there because we cannot see those settings: an
    authenticated `/endpoints` call is not filtered by them (measured).

    A **404 on the data policy** means opposite things on the two surfaces. On a
    language model it points at their OpenRouter privacy settings, but the
    policy that refused the request is ours: every chat request Talqing sends
    carries `data_collection: "deny"`, and about 2% of the long tail has no host
    that meets it — changing the model helps, their settings do not. On
    transcription we send no policy at all (the endpoint ignores the block), so
    the only policy that can have refused it IS their account's own.

    A **402** is about the balance but rarely says "out of credit". OpenRouter
    prices a *reservation* — the largest reply the model could produce — against
    the credit behind the key and refuses before running anything, so a workspace
    with a little credit left cannot use a model with a large output ceiling at
    all. Measured: a key holding about $0.03 was refused on
    `anthropic/claude-haiku-4.5` with "You requested up to 64000 tokens, but can
    only afford 5160", having asked for no cap at all. The same code covers the
    other shape, a per-request prompt cap that an attached image can trip. Both
    read as the model being broken; the provider's own sentence is the one that
    explains it, so it leads.

    Anything else is returned untouched: the provider's own words are better than
    ours for a failure we have nothing to add to.
    """
    detail = str(exc.body.get("message") or "") if isinstance(exc.body, dict) else ""
    if exc.status_code == 404 and hosts:
        message = (
            f"None of the hosts chosen for {model} ({', '.join(hosts)}) can serve it right "
            "now. They may be down, or excluded by your OpenRouter account's provider or "
            "privacy settings. Allow more hosts or Automatic, or add a fallback model."
        )
    elif exc.status_code == 404 and "data policy" in detail.lower() and transcription:
        message = (
            "no host serving this model is allowed by your OpenRouter account's privacy "
            "settings. Loosen them at openrouter.ai/settings/privacy, or pick a different model."
        )
    elif exc.status_code == 404 and "data policy" in detail.lower():
        message = (
            "no host serving this model meets Talqing's data policy — every request refuses "
            "hosts that may store or train on your prompts, and this model has none that "
            "qualify. Pick a different model."
        )
    elif exc.status_code == 402 and transcription:
        message = (
            f"OpenRouter refused this request for want of credit: {detail} Top up to continue."
        )
    elif exc.status_code == 402:
        message = (
            f"OpenRouter refused this request for want of credit: {detail} It reserves the "
            "largest reply a model could produce before it runs anything, so a low balance "
            "fails outright on a model with a big output ceiling — top up, or pick a smaller "
            "model, before assuming this one is at fault."
        )
    else:
        return exc
    # The same class and the same status, so nothing downstream branches
    # differently — only the sentence a human reads changes.
    return type(exc)(message, response=exc.response, body=exc.body)


class _OpenRouterLLM(_CountsThinkingTokens, openai.LLM):
    """One OpenRouter model, called through the Chat Completions client.

    OpenRouter is a gateway, not a vendor: the request goes to one of ~106
    upstream hosts chosen per call. Three things follow from that, all of them
    decided here rather than left to the agent's author.

    **Routing is the platform's decision** among the hosts the author allowed
    (`LLMModelSpec.hosts`, all of them when unset) — see
    `openrouter.ROUTING_PREFERENCES` for the two settings and the measurements
    behind them. `openrouter.routing` builds the block for every request from
    here and from `services.llm_client`, the other caller that reaches OpenRouter.

    **Anthropic caches nothing unless asked.** Without a breakpoint a Claude agent
    re-prefills the system prompt and the whole tool array on every turn at full
    input price, and `warm_prompt_cache` buys nothing at all.
    The top-level `cache_control` is OpenRouter's automatic mode — it advances the
    breakpoint as the conversation grows — and applies on the Anthropic, Vertex,
    Azure and Bedrock endpoints. Models with a short prompt get nothing, the
    minimum cacheable prefix being 1,024-4,096 tokens depending on the model.

    **`strict` is silently stripped on Anthropic** without
    `x-anthropic-beta: structured-outputs-2025-11-13`. Verified with OpenRouter's
    own `debug.echo_upstream_body` on 2026-09-08: the same request reaches
    Anthropic with `"strict": true` on each tool with the header, and with the
    field simply gone without it. The plugin sends `strict` on every tool
    (`_strict_tool_schema` defaults True), so without this our schemas would be
    quietly downgraded on exactly the models people come here for.

    `openai.LLM.with_openrouter()` exists and is not used: it builds a plain
    `LLM` (no thinking-token repair, no cost hook), defaults `model="auto"` —
    which is a slug we refuse to list, because the model that runs is then not
    the model configured — and its `fallback_models` argument drives OpenRouter's
    own `models:` array, a second invisible failover competing with our
    `llm.FallbackAdapter`.
    """

    def __init__(
        self,
        *,
        model: str,
        hosts: list[str] | None,
        reported_cost: ReportedCost | None,
        **kwargs: Any,
    ) -> None:
        anthropic = model.startswith("anthropic/")
        extra_body: dict[str, Any] = {"provider": openrouter.routing(hosts)}
        extra_headers = dict(openrouter.ATTRIBUTION_HEADERS)
        if anthropic:
            extra_body["cache_control"] = {"type": "ephemeral"}
            extra_headers["x-anthropic-beta"] = "structured-outputs-2025-11-13"
        super().__init__(model=model, extra_body=extra_body, extra_headers=extra_headers, **kwargs)
        # Per request, keyed by the id the metrics come back under. See
        # `_RecordsCost` for why this one number needs correlating and the cost
        # does not.
        self._cache_writes: dict[str, int] = {}
        # Wrapped after construction so the plugin still builds its own client,
        # with its own streaming timeouts and connection limits. The attribute is
        # set on the resource object this LLM owns; no other client is affected.
        inner = self._client.chat.completions.create
        collector, provider, writes = reported_cost, self.provider, self._cache_writes

        async def create(*args: Any, **kwargs: Any) -> Any:
            try:
                stream = await inner(*args, **kwargs)
            except APIStatusError as exc:
                raise _legible_openrouter_error(
                    exc, transcription=False, model=model, hosts=hosts
                ) from None
            return _RecordsCost(stream, collector, provider, model, writes)

        self._client.chat.completions.create = create  # type: ignore[method-assign]

    def emit(self, event: str, *args: Any) -> None:
        """Put this request's cache-write count back on its metrics.

        The same repair-on-the-way-out `_CountsThinkingTokens` makes, and for the
        same reason: this is the last point where the number and the metrics
        object exist together. `LLMMetrics.request_id` is the chunk id
        `_RecordsCost` filed it under, so a session running several models — or
        one model answering two turns at once — cannot cross them.

        Ordering with the mixin is deliberate: this sets the field, then
        `super().emit` receives the updated metrics and makes its own repair on
        top. Both copy rather than mutate, so neither can lose the other's.
        """
        if event == "metrics_collected" and args and isinstance(args[0], LLMMetrics):
            metrics: LLMMetrics = args[0]
            written = self._cache_writes.pop(metrics.request_id, 0)
            if written:
                args = (metrics.model_copy(update={"cache_creation_tokens": written}), *args[1:])
        super().emit(event, *args)

    @property
    def provider(self) -> str:
        # The base class reports the transport host — "openrouter.ai" — and
        # metering canonicalizes against the catalog's own provider names.
        return "openrouter"


class _OpenRouterSTT(openai.STT):
    """One OpenRouter transcription model, on the stock OpenAI plugin's REST path.

    `/audio/transcriptions` is OpenAI-compatible and the plugin speaks it
    unmodified. Three things are added, all copied from `_OpenRouterLLM`:

    - **`provider` is "openrouter"**, not the hostname the base class reports.
      Without it `Catalog.canonicalize_usage` cannot tell this row from an
      `openai` STT fallback — the same class, reporting `api.openai.com`.
    - **The charge is kept.** Every model returns `usage.cost` on the response,
      which the SDK keeps as an extra field and LiveKit's `STTMetrics` has no
      room for. It is the line on the bill (see `price_session`), because the
      rate card is the cheapest host's: identical whisper-large-v3 requests were
      measured routed to two hosts and charged 3.3x apart.
    - **Attribution headers**, so OpenRouter's own dashboard names the traffic.

    Deliberately NOT sent: `openrouter.ROUTING_PREFERENCES`. This endpoint
    ignores the `provider` block wholesale (measured — see that constant), so
    sending it would only suggest a latency sort and a data policy that are not
    applied. Leave it absent; its absence is not an oversight.
    """

    def __init__(self, *, model: str, reported_cost: ReportedCost | None, **kwargs: Any) -> None:
        # `detect_language` always: an OpenRouter entry publishes no languages,
        # so no code is ever sent, and the plugin's own default is "en".
        super().__init__(model=model, use_realtime=False, detect_language=True, **kwargs)
        inner = self._client.audio.transcriptions.create
        collector = reported_cost

        async def create(*args: Any, **kwargs: Any) -> Any:
            kwargs["extra_headers"] = {
                **openrouter.ATTRIBUTION_HEADERS,
                **(kwargs.get("extra_headers") or {}),
            }
            try:
                response = await inner(*args, **kwargs)
            except APIStatusError as exc:
                raise _legible_openrouter_error(exc, transcription=True) from None
            # An extra field on the SDK's typed usage object: read, never assumed.
            cost = getattr(getattr(response, "usage", None), "cost", None)
            if cost is not None and collector is not None:
                collector.add("openrouter", model, Decimal(str(cost)))
            return response

        self._client.audio.transcriptions.create = create  # type: ignore[method-assign]

    @property
    def provider(self) -> str:
        return "openrouter"


class _GeminiLLM(_CountsThinkingTokens, google.LLM):
    """Gemini on its own plugin — `api: native` in the catalog.

    Carries the same mixin for a different reason than it was written for.
    Gemini's own endpoint *does* report thinking honestly, as
    `usageMetadata.thoughtsTokenCount`, but the plugin never forwards it into
    `CompletionUsage` (`google/llm.py`, the usage chunk) — it copies
    `candidates_token_count`, which excludes thinking, and `total_token_count`,
    which includes it. So the gap the mixin closes is exact on an ordinary turn:
    re-measured 2026-09-22 on livekit-agents 1.8.2, gemini-3.6-flash at effort
    `low` — prompt 27 + candidates 1 + thoughts 107 = total 135, and the mixin
    bills 108 where the plugin alone would have billed 1.

    **It is NOT exact when a built-in tool runs**, and that is the one case worth
    knowing about. Gemini reports the tool's own input as
    `toolUsePromptTokenCount`, which the plugin also drops and which also lands
    inside `total_token_count` — so the gap sweeps it up and the tenant sees
    Google's *input* tokens at the *output* rate (measured: 48 such tokens on a
    code-execution turn). Only `google_search`, `url_context` and
    `code_execution` turns are affected.

    Left alone deliberately, 2026-09-22. The exact fix is to read
    `thoughtsTokenCount` itself, and 1.8.0 even added the field to put it in
    (`CompletionUsage.reasoning_tokens`) — but unlike the Responses path, which
    exposes `_handle_response_completed` for `_ReportsCacheWrites` to override,
    this plugin builds its usage chunk inline inside a 145-line `_run` from a
    local, and tags it with a `shortuuid` it generates itself. There is no seam:
    reaching the raw `usage_metadata` means wrapping the genai client, and
    nothing then correlates that back to the metrics. The real fix belongs
    upstream, in the plugin.

    No `provider` override, unlike its Responses sibling: `google.LLM` already
    reports "Gemini" (it reports "Vertex AI" only on the Vertex path, which we do
    not take), and catalog canonicalization is case-insensitive on the provider.
    """


class _ReportsCacheWrites(ResponsesLLMStream):
    """A Responses stream that keeps the cache-WRITE count the plugin drops.

    LiveKit's chain for this number is already wired and only the first link is
    missing: `CompletionUsage.cache_creation_tokens` exists, `LLMMetrics` copies
    it and `LLMModelUsage.input_cache_creation_tokens` accumulates it — but the
    plugin reads only `cached_tokens` when it builds the usage chunk, so nothing
    ever fills it. That matters because OpenAI charges cache WRITES at 1.25x
    input from the GPT-5.6 family onward, and `warm_prompt_cache` makes one
    deliberately on every voice call. Measured on gpt-5.6-luna with a
    1694-token prompt: the cold call reports `cache_write_tokens: 1691` and the
    warm one two seconds later reports `cached_tokens: 1691`. gpt-5.4-nano and
    gpt-4.1-mini report zero on both calls — a provider reports cache writes
    only where it charges for them, which is what lets `price_llm_usage`
    quarantine a metered write with no rate rather than billing it at zero.
    """

    def _handle_response_completed(self, event: Any) -> llm.ChatChunk | None:
        chunk = super()._handle_response_completed(event)
        usage = getattr(event.response, "usage", None)
        details = getattr(usage, "input_tokens_details", None) if usage else None
        if chunk is not None and chunk.usage is not None and details is not None:
            # An extra field on the SDK's typed details object: present where the
            # provider charges for writes, absent everywhere else.
            chunk.usage.cache_creation_tokens = getattr(details, "cache_write_tokens", 0) or 0
        return chunk


class _PromptCachedResponsesLLM(_CountsThinkingTokens, openai.responses.LLM):
    """The Responses client with Talqing's cache, retention and tool policy fixed.

    Three things the plugin leaves to the caller, all of which we want the same
    way on every request:

    - **The prompt cache key.** There is no constructor parameter for it, only a
      per-call one, and Talqing compiles one LLM per call/session — so the key
      lives on the instance and rides every request from here.
    - **`store`.** The Responses API retains responses by default. The Chat
      Completions path stores nothing, so storing would be a change in what
      leaves the tenant's control rather than an optimisation; `store=False`
      keeps the posture and, deliberately, also turns off the plugin's
      `previous_response_id` incremental-context path.
    - **`_provider_tool_type`.** This is what makes `to_responses_fnc_ctx`
      serialize our provider tools instead of skipping them — and, because it is
      set per instance to this provider's subclass, what keeps the other model's
      tools out of this one's request when a fallback is configured. The two
      share one tool list; only the matching ones go on the wire.

    `use_websocket=False` because the plugin's websocket transport only speaks to
    OpenAI's own host; one transport for every provider keeps this one class.
    """

    def __init__(self, *, cache_key: str | None, provider: str, **kwargs: object) -> None:
        super().__init__(use_websocket=False, store=False, **kwargs)  # type: ignore[arg-type]
        self._cache_key = cache_key
        self._catalog_provider = provider
        # Instance attribute, shadowing the plugin's class attribute: two LLMs in
        # one FallbackAdapter need two different answers.
        self._provider_tool_type = provider_tool_type(provider)

    @property
    def provider(self) -> str:
        # The base class reports the transport host (api.x.ai, api.openai.com).
        # Usage metering canonicalizes against the catalog provider, so give it
        # the name the catalog uses.
        return self._catalog_provider

    def chat(
        self,
        *,
        chat_ctx: llm.ChatContext,
        tools: list[llm.Tool] | None = None,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
        parallel_tool_calls: NotGivenOr[bool] = NOT_GIVEN,
        tool_choice: NotGivenOr[llm.ToolChoice] = NOT_GIVEN,
        extra_kwargs: NotGivenOr[dict[str, object]] = NOT_GIVEN,
    ) -> ResponsesLLMStream:
        extra = dict(extra_kwargs) if is_given(extra_kwargs) else {}
        if self._cache_key:
            extra["prompt_cache_key"] = self._cache_key
        stream = super().chat(
            chat_ctx=chat_ctx,
            tools=tools,
            conn_options=conn_options,
            parallel_tool_calls=parallel_tool_calls,
            tool_choice=tool_choice,
            extra_kwargs=extra,
        )
        # Re-class rather than reconstruct. `_ReportsCacheWrites` overrides one
        # method and adds no state, while the `chat()` above is ~90 lines of
        # request assembly — copying it to pass a different class would be the
        # thing that breaks on the next plugin release.
        stream.__class__ = _ReportsCacheWrites
        return stream


def _provider_key(provider: str) -> str:
    """Catalog + settings keys are lowercase; never branch on raw casing."""
    key = (provider or "").strip().lower()
    if not key:
        raise ValueError("provider is required")
    return key


def _key(provider: str, provider_keys: dict[str, str]) -> str:
    provider = _provider_key(provider)
    key = provider_keys.get(provider, "")
    if not key:
        raise ValueError(f"missing API key for provider {provider!r}")
    return key


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


# Whichever entry renders the speech — a TTS model, or a realtime model that
# speaks for itself. Both carry the same voice and speed policy.
SpeakingEntry = TTSEntry | RealtimeEntry


def _entry_speed(entry: SpeakingEntry, speed: float | None) -> float:
    """Clamp (or force 1.0) using the catalog entry's speed policy only."""
    value = 1.0 if speed is None else float(speed)
    if not entry.supports_speed:
        return 1.0
    return _clamp(value, entry.speed_min, entry.speed_max)


def _entry_voice(entry: SpeakingEntry, voice: str | None) -> str:
    """Prefer the agent pick; otherwise the catalog default_voice."""
    picked = (voice or "").strip()
    if picked:
        return picked
    default = (entry.default_voice or "").strip()
    if default:
        return default
    raise ValueError(
        f"{entry.provider}/{entry.model} has no voice selected and no default_voice in the catalog"
    )


def stt_entry(spec: STTModelSpec) -> STTEntry:
    """The catalog entry backing an STT selection. Fails loudly."""
    entry = get_catalog().require_entry("stt", _provider_key(spec.provider), spec.model)
    if not isinstance(entry, STTEntry):
        raise TypeError(f"expected STTEntry for {spec.provider}/{spec.model}")
    return entry


def resolve_turn_detection(
    stt: STTModelSpec,
    language: str | None,
) -> Literal["stt", "vad", "livekit-turn-detector-v1-mini"]:
    """Which detector ends the caller's turn. Derived, never configured.

    This is not a preference — the pipeline decides it, so exposing a knob would
    only let a user pick something their models cannot do.

    A **streaming** model endpoints for itself, and that is the option to take:
    its end-of-speech signal comes from the same component that produces the
    transcript, in order, so the turn can never commit on a half-finished one.
    The local detectors run off the VAD instead, a different component, which
    leaves a race a slow final transcript can lose (see the copy in
    ``TurnHandlingSection``).

    A **batch** model has no endpointer at all — LiveKit's StreamAdapter
    synthesises end-of-speech from the local VAD — so the only question left is
    whether the audio end-of-turn model can help. It covers 14 languages, and
    ``supports_language`` cannot be relied on to say so: for v1-mini it never
    returns False, because ``ThresholdOptions.lookup`` falls back to the English
    threshold for anything it does not know. An unlisted language would run the
    model miscalibrated and log nothing, so the check has to happen here. The
    model keys off the primary subtag, so en-GB rides on en.

    A searched provider's streaming-ness comes from its `models.stt` block, not
    an entry: this runs inside the voice job, which holds no registry.
    """
    meta = get_catalog().providers.get(_provider_key(stt.provider))
    searched = meta.models.stt if meta is not None and meta.models is not None else None
    if searched.streaming if searched is not None else stt_entry(stt).streaming:
        return "stt"
    if (language or "").split("-", 1)[0].lower() in TURN_DETECTOR_LANGUAGES:
        return "livekit-turn-detector-v1-mini"
    return "vad"


def provider_endpointing_seconds(
    turn_handling: TurnHandlingSpec,
    stt: STTModelSpec,
) -> float:
    """The window the STT provider's own end-of-speech detector gets.

    The whole setting, undivided. It used to be halved with LiveKit's own delay,
    which delivered half the number the user typed: both are measured from the
    instant silence began, so two waits from one anchor take the max, never the
    sum. See ``compile._turn_handling`` for the other half of that story.

    Inert on three kinds of model, all for their own reasons. A batch model takes
    no endpointing kwargs at all — the VAD in front of it is what endpoints, and
    `compile._configure_vad` gives that VAD the same number. Deepgram Flux
    (``STTv2``, probability thresholds only) and Sarvam Saaras (server
    ``vad_signals``) accept no time window, so on those two the setting is a floor
    rather than an exact wait.
    """
    seconds = turn_handling.endpointing.min_silence_duration
    if _provider_key(stt.provider) == "elevenlabs":
        # ElevenLabs rejects a vad_silence_threshold_secs outside [0.3, 3.0].
        return _clamp(seconds, 0.3, 3.0)
    if _provider_key(stt.provider) == "soniox":
        # Soniox's max_endpoint_delay_ms is documented as 500-3000 and the plugin
        # raises outside it, so an agent asking for 0.2s would fail to compile
        # rather than run fast. Clamped for the same reason as ElevenLabs above.
        return _clamp(seconds, 0.5, 3.0)
    return seconds


def _required_language(language: str | None, entry: STTEntry | TTSEntry, label: str) -> str:
    """The language for a provider that will not run without one.

    Agent validation rejects a config that would land here empty, so reaching the
    raise means the catalog and the validator have drifted apart.
    """
    resolved = language or entry.default_language
    if not resolved:
        raise ValueError(
            f"{label} '{entry.provider}/{entry.model}' needs a language - set the agent's language"
        )
    return resolved


def build_llm(
    spec: LLMSpec,
    provider_keys: dict[str, str],
    *,
    cache_key: str | None = None,
    has_tools: bool = False,
    reported_cost: ReportedCost | None = None,
) -> llm.LLM:
    """The configured LLM, wrapped for failover when the spec names a fallback.

    Both models are instantiated up front (that is what makes a missing fallback
    key a hard error, see ``AgentConfig.required_providers``); the fallback only
    receives traffic once the primary errors.

    ``reported_cost`` is the SESSION's collector, not this model's: a call that
    hands off, fails over or enters a task runs several LLMs and every one of
    them adds into the same object, which is what finalize reads. Left unset for
    anything Talqing pays for itself — a CoPilot — which is metered nowhere.
    """
    primary = _build_llm_one(
        spec, provider_keys, cache_key=cache_key, has_tools=has_tools, reported_cost=reported_cost
    )
    if spec.fallback is None:
        return primary
    secondary = _build_llm_one(
        spec.fallback,
        provider_keys,
        cache_key=cache_key,
        has_tools=has_tools,
        reported_cost=reported_cost,
    )
    return llm.FallbackAdapter([primary, secondary])


def _build_llm_one(
    spec: LLMModelSpec,
    provider_keys: dict[str, str],
    *,
    cache_key: str | None = None,
    has_tools: bool = False,
    reported_cost: ReportedCost | None = None,
) -> llm.LLM:
    """Build one configured LLM, on the client its catalog entry's `api` declares.

    Never a model string, which would route through LiveKit Cloud. Two of
    the three clients are the LiveKit openai plugin pointed at the provider's own
    base_url: Chat Completions is the compatibility path every OpenAI-compatible
    endpoint speaks, and Responses is the one that can carry the provider's own
    server-side tools. `native` is the provider's own plugin, and it says nothing
    about which one — the vendor still has to be matched, which is why the branch
    below switches on the provider and raises on a pairing nobody has built.
    Gemini takes it because Google serves no Responses endpoint at all, so its
    server-side tools are reachable on no other client.

    `cache_key` is the provider prompt-cache bucket (threaded from the worker):
    LiveKit session id for voice/video, persistent conversation id for text. The
    two OpenAI-plugin paths send it as `prompt_cache_key`; on Chat Completions
    xAI also needs the `x-grok-conv-id` header, which the Responses body
    parameter replaces. Gemini takes no key on either path — it caches
    implicitly, and its compatibility layer 400s the whole turn on the field.

    `has_tools`: when True, set parallel_tool_calls=True so the model may emit
    several tools in one step. When False, leave that provider knob unset.

    Reasoning effort is the agent author's (``spec.reasoning_effort``) or, unset,
    the catalog default for that model — always the fastest it offers, because a
    voice turn cannot afford thinking latency. It goes out in the shape the
    client takes it: `reasoning: {effort}` on Responses, `reasoning_effort` on
    Chat Completions, `thinking_config: {thinking_level}` on Gemini. Per model,
    not per agent: the fallback is a different model with its own accepted
    values, so it carries its own.

    ``spec.priority`` asks for the provider's low-latency lane. Also per model:
    the catalog decides which models sell one and at what price, and agent
    validation has already refused the combination on a model that sells none —
    so ``require_priority`` raising here means a bug upstream, not bad input.

    A ``browse: "search"`` provider takes the branch below instead, and looks up
    no entry at all. Its models live in a registry a voice job process does not
    hold, so everything needed to build the client comes from the provider's own
    ``models:`` block and from the config — which is exactly why publishing
    freezes the thinking effort onto the config (``services.agents.pin``). The
    invariant that rests on: **a config reaching a voice job process is always
    published, and therefore pinned; an unpinned one is only ever compiled by a
    process that holds the registry** (a task run on a draft, `validate_agent`).
    """
    provider = _provider_key(spec.provider)
    catalog = get_catalog()
    meta = catalog.providers.get(provider)
    if meta is None:
        raise ValueError(f"no catalog provider block for llm provider {provider!r}")
    if meta.models is not None and meta.models.llm is not None:
        return _build_searched_llm(
            spec,
            meta.models.llm,
            _key(provider, provider_keys),
            provider=provider,
            cache_key=cache_key,
            has_tools=has_tools,
            reported_cost=reported_cost,
        )
    entry = catalog.require_entry("llm", provider, spec.model)
    if not isinstance(entry, LLMEntry):
        raise TypeError(f"expected LLMEntry for {provider}/{spec.model}")
    extra = dict(entry.extra)
    key = _key(provider, provider_keys)
    effort = entry.effort_for(spec.reasoning_effort)
    kwargs: dict[str, object] = {}
    # One parameter, every provider, every client — which is why this sits above
    # the api split rather than in each branch. It has to be a kwarg and never
    # catalog `extra`: `extra` reaches the wire but not billing, so a tier set
    # there would buy the premium lane at standard rates.
    if spec.priority:
        kwargs["service_tier"] = entry.require_priority().value

    # entry.extra first in every branch; kwargs win so cache/reasoning policy
    # cannot be overridden from the catalog.
    if entry.api == "native":
        if provider != "gemini":
            raise ValueError(
                f"llm provider {provider} is set to api 'native' but has no native client here — "
                "add one, or point the entry at an OpenAI-compatible api"
            )
        # Deliberately absent, all three unsupported by google.LLM's constructor:
        # `base_url` (the plugin addresses Google itself), `prompt_cache_key`
        # (Gemini caches implicitly; explicit caching is a CachedContent resource
        # that would displace our tools and system prompt), and
        # `parallel_tool_calls` (Gemini emits parallel calls on its own — chat()
        # accepts the argument and ignores it, and __init__ rejects it outright).
        if effort:
            # Always sent. Left out, the plugin picks its own thinking level for
            # any gemini-3 model, which would silently outrank the catalog.
            kwargs["thinking_config"] = {"thinking_level": effort}
        return _GeminiLLM(model=spec.model, api_key=key, **{**extra, **kwargs})

    if not entry.base_url and provider != "openai":
        raise ValueError(
            f"llm provider {provider} has no base_url in catalog.yaml — "
            "required for non-OpenAI OpenAI-compatible providers"
        )
    if entry.base_url:
        kwargs["base_url"] = entry.base_url
    if has_tools:
        kwargs["parallel_tool_calls"] = True

    if entry.api == "responses":
        if effort:
            kwargs["reasoning"] = {"effort": effort}
        return _PromptCachedResponsesLLM(
            model=spec.model,
            api_key=key,
            cache_key=cache_key,
            provider=provider,
            **{**extra, **kwargs},
        )

    # Not every compatibility layer tolerates the parameter: Gemini's validates
    # the body against its own proto and 400s the whole turn on an unknown field.
    if cache_key and entry.supports_prompt_cache_key:
        kwargs["prompt_cache_key"] = cache_key
    if cache_key and provider == "xai":
        kwargs["extra_headers"] = {"x-grok-conv-id": cache_key}
    if effort:
        kwargs["reasoning_effort"] = effort
    return _ChatCompletionsLLM(model=spec.model, api_key=key, **{**extra, **kwargs})


def _build_searched_llm(
    spec: LLMModelSpec,
    models: SearchedLLMs,
    key: str,
    *,
    provider: str,
    cache_key: str | None,
    has_tools: bool,
    reported_cost: ReportedCost | None,
) -> llm.LLM:
    """One model from a provider whose models are not in ``catalog.yaml``.

    OpenRouter is the only one, and this raises on any other rather than
    inventing a client for it — the same shape the ``api: native`` branch above
    takes for the same reason: which client class to build is not something a
    catalog edit can decide on its own.

    The thinking effort takes two branches, for a permanent reason rather than a
    historical one. A **pinned** effort — which every published config has, and
    every call plan writes — is sent as configured, without `effort_for`
    validating it against a list this process may not hold. An **unpinned** one
    is resolved against the registry, and that branch is only ever reached by a
    process that has one: a draft task run, `validate_agent`. The invariant:
    *a config reaching a voice job process is always pinned; an unpinned one is
    always handled by a process that can resolve it.* Sending an effort a model
    does not take is a 400 on the first turn — loud, and impossible for a
    published agent, whose value was checked against the live registry at the
    moment it was frozen.

    ``spec.priority`` cannot arrive true: OpenRouter sells no priority lane, so
    no entry declares one and agent validation refuses the pairing. Asserted
    rather than assumed, because the cost of being wrong is charging a premium
    rate for standard processing.
    """
    if provider != "openrouter":
        raise ValueError(
            f"llm provider {provider} is `browse: search` but has no client here — "
            "add one, or point the provider at a listed catalog entry"
        )
    if spec.priority:
        raise ValueError(f"llm {provider}/{spec.model} sells no priority lane to run in")
    kwargs: dict[str, object] = {**models.extra, "base_url": models.base_url}
    if has_tools:
        kwargs["parallel_tool_calls"] = True
    if cache_key and models.supports_prompt_cache_key:
        # Doing double duty: OpenRouter uses this as its sticky-routing key when
        # a request names no `session_id`, so every turn of one call already pins
        # to the upstream the first turn landed on — which is what makes the
        # prompt cache hit at all.
        kwargs["prompt_cache_key"] = cache_key
    effort: str | None = spec.reasoning_effort
    if effort is None:
        entry = get_catalog().entry("llm", provider, spec.model)
        if isinstance(entry, LLMEntry):
            effort = entry.effort_for(None)
    if effort:
        kwargs["reasoning_effort"] = effort
    return _OpenRouterLLM(
        model=spec.model, hosts=spec.hosts, api_key=key, reported_cost=reported_cost, **kwargs
    )


# What the warm request pretends the caller said. Short and inert on purpose: it
# sits past the end of the cached prefix, so it buys nothing, and a question
# would risk a grounded model spending a (billable) provider tool call on it.
WARM_USER_MESSAGE = "Hello."


async def warm_prompt_cache(
    model: llm.LLM,
    *,
    chat_ctx: llm.ChatContext,
    tools: list[llm.Tool],
) -> None:
    """Send one throwaway request so the first real turn hits a warm provider cache.

    A voice call's first LLM reply is measurably slower than every later one, and
    what is left of that gap (after livekit-agents' own `ipc/_preload.py`
    absorbed the SDK's lazy schema build) is the provider prompt cache: turn 2
    reads the system prompt and tool array out of the cache,
    and turn 1 pays to prefill all of it. The greeting is several seconds of
    speech during which the LLM has nothing to do — this spends that window
    prefilling the same prefix, so the human's first question meets a cache the
    way their second one does.

    **This is nearly free, which is the whole argument for doing it.** The warm
    request pays full price for a prefix the first turn would have paid full
    price for anyway; the first turn then pays the cached rate for it. The net
    extra is roughly a tenth of one prefix, plus the few dozen output tokens of a
    reply nobody reads.

    Measured against the live providers on a ~3.9k-token prefix: the first turn
    after a warm request read 3072 of its 3643 prompt tokens from cache on
    gpt-5.4-mini, and 3968 of 4102 on grok-4.5. Gemini caches nothing for us and
    is skipped — see below.

    `chat_ctx` and `tools` must be exactly what the first turn will send, in the
    same order — a prompt cache matches on a byte-identical prefix, and one extra
    tool or a re-rendered system message buys nothing at all. The caller appends
    the greeting and `WARM_USER_MESSAGE`, which is why the useful prefix ends at
    the greeting and everything past it is deliberately worthless.

    The cache *key* needs no handling here: it is fixed per instance at build
    time (`prompt_cache_key` on both OpenAI-plugin paths, `x-grok-conv-id` on
    xAI, nothing at all on Gemini, which caches implicitly), so this request and
    the call's real turns share a bucket by construction.

    Usage is billed to the tenant with no plumbing of its own: the session
    subscribes to `metrics_collected` on the LLM it was built with, and a
    `FallbackAdapter` re-emits its children's, so these tokens land in
    `session.usage` and finalize writes them into the call's `llm_usage` row.
    """
    if isinstance(model, llm.FallbackAdapter):
        # The primary, never the adapter: a warm request that fails *through* the
        # adapter marks the primary unavailable and moves the whole call onto the
        # fallback model before the human has said a word — and would file a
        # `provider.failed` event for a request nobody heard. Reaching for the
        # private list is the price of that; `_llm_instances` is stable in
        # livekit-agents 1.6.5 and there is no public accessor.
        model = model._llm_instances[0]

    if isinstance(model, _GeminiLLM):
        # Nothing to warm. Gemini is the one provider `_build_llm_one` sends no
        # cache key to, because it caches implicitly — and implicitly is exactly
        # what it declined to do when measured: four sequential requests sharing
        # a ~3.9k-token prefix reported `cached_content_token_count` 0 every
        # time, against the 3072 and 3968 the same experiment got from OpenAI and
        # xAI. So a warm request here buys a prefill nobody reuses. And even if
        # Google turns that on, doubling the request count is expensive against a
        # per-minute request quota.
        return

    async with model.chat(
        chat_ctx=chat_ctx,
        tools=tools,
        # No cap on the reply, deliberately, even though every token of it is
        # discarded. Capping is what breaks the metering: measured on grok-4.5, a
        # reply truncated by `max_output_tokens` comes back with **no usage block
        # at all** (prompt=0, completion=0), so a prefill the tenant's key paid
        # for reaches no meter and goes missing from their call. Reasoning models
        # spend their first tokens thinking, so where that line falls is a
        # property of the model rather than a number this could be set safely
        # below. It costs nothing to leave off: the reply to `WARM_USER_MESSAGE`
        # measured 13 output tokens on gpt-5.4-mini and 70 on grok-4.5, capped or
        # not.
        #
        # No retry either. A warm request that failed has already lost its race
        # with the greeting, and retrying only spends the tenant's tokens twice.
        conn_options=APIConnectOptions(max_retry=0),
    ) as stream:
        async for _ in stream:
            pass


def _build_batch_stt(
    spec: STTModelSpec, entry: STTEntry, key: str, language: str | None
) -> stt.STT:
    """A batch (request/response) STT plugin, before StreamAdapter wrapping.

    Deliberately takes no endpointing kwargs: the StreamAdapter's VAD decides
    where an utterance ends, and the plugin only transcribes the clip it is
    handed. Language is the one shared knob — everything else is catalog `extra`.
    """
    provider = _provider_key(spec.provider)
    kwargs = dict(entry.extra)
    if provider == "xai":
        # The plugin's REST path (`_recognize_impl`) always posts to /v1/stt; only
        # `stream()` opens the websocket. Reaching it needs nothing but never
        # calling stream() — which is exactly what StreamAdapter does.
        return _NamedXaiSTT(
            catalog_model=entry.model,
            api_key=key,
            language=_required_language(language, entry, "xai STT"),
            **kwargs,
        )
    if provider == "elevenlabs":
        # `server_vad` is realtime-only, which is why the batch catalog entry
        # must not carry it — the plugin warns and drops it.
        if language:
            kwargs["language_code"] = language
        return elevenlabs.STT(api_key=key, model=spec.model, **kwargs)
    if provider == "openai":
        if language:
            kwargs["language"] = language
            kwargs["detect_language"] = False
        else:
            kwargs["detect_language"] = True
        return openai.STT(api_key=key, model=spec.model, use_realtime=False, **kwargs)
    if provider == "raya":
        # Always sends a code, never an empty field. Omitting it is accepted and
        # behaves like `en` today, but that is Raya's undocumented default rather
        # than a documented one, and the catalog's `default_language` is where
        # that decision belongs. `catalog_model` is not decoration: Raya's STT
        # names no model in its response, so this is the only place the billing
        # id can come from.
        return raya.STT(
            api_key=key,
            catalog_model=entry.model,
            language=_required_language(language, entry, "STT model"),
            **kwargs,
        )
    raise ValueError(f"stt provider {provider} has no batch model in the catalog")


def _build_searched_stt(
    spec: STTModelSpec,
    models: SearchedSTTs,
    key: str,
    *,
    provider: str,
    vad: lk_vad.VAD | None,
    reported_cost: ReportedCost | None,
) -> stt.STT:
    """One speech-to-text model from a provider whose models are not in
    ``catalog.yaml``, already wrapped in ``stt.StreamAdapter``.

    OpenRouter is the only one, and it serves no streaming transcription at all,
    so a block declaring `streaming: true` has no client to build — raised
    rather than guessed, like ``_build_searched_llm``.
    """
    if provider != "openrouter" or models.streaming:
        raise ValueError(
            f"stt provider {provider} is `browse: search` with streaming={models.streaming} "
            "but has no such client here"
        )
    if vad is None:
        raise ValueError(
            f"STT {provider}/{spec.model} is a batch model and needs a VAD to segment "
            "speech — pass the session VAD to build_stt"
        )
    client = _OpenRouterSTT(
        model=spec.model,
        api_key=key,
        base_url=models.base_url,
        reported_cost=reported_cost,
        **models.extra,
    )
    return stt.StreamAdapter(stt=client, vad=vad)


def build_stt(
    spec: STTSpec,
    provider_keys: dict[str, str],
    *,
    agent_language: str | None = None,
    turn_handling: TurnHandlingSpec,
    vad: lk_vad.VAD | None = None,
    reported_cost: ReportedCost | None = None,
) -> stt.STT:
    """The configured STT, wrapped for failover when the spec names a fallback.

    ``FallbackAdapter`` is given only streaming instances — ``_build_stt_one``
    has already wrapped any batch model — so it never needs its own VAD.

    ``agent_language`` is the agent's single language setting; each model
    translates it into its own code, so a fallback from Deepgram to Sarvam keeps
    speaking Hindi even though one spells it ``hi`` and the other ``hi-IN``.

    ``reported_cost`` is the session's collector, as on ``build_llm``: a
    gateway-served model adds what each transcription was charged into it.
    """
    primary = _build_stt_one(
        spec,
        provider_keys,
        agent_language=agent_language,
        turn_handling=turn_handling,
        vad=vad,
        reported_cost=reported_cost,
    )
    if spec.fallback is None:
        return primary
    secondary = _build_stt_one(
        spec.fallback,
        provider_keys,
        agent_language=agent_language,
        turn_handling=turn_handling,
        vad=vad,
        reported_cost=reported_cost,
    )
    return stt.FallbackAdapter([primary, secondary])


def _build_stt_one(
    spec: STTModelSpec,
    provider_keys: dict[str, str],
    *,
    agent_language: str | None = None,
    turn_handling: TurnHandlingSpec,
    vad: lk_vad.VAD | None = None,
    reported_cost: ReportedCost | None = None,
) -> stt.STT:
    """Build one configured STT, always as a *streaming* object.

    Batch (`streaming: false`) catalog models are wrapped here in LiveKit's
    stt.StreamAdapter rather than left for `Agent.default.stt_node` to wrap: the
    default path would reach for the session VAD untouched, whereas `vad` here is
    the same instance already tuned to this agent's endpointing budget
    (`compile._configure_vad`).

    A ``browse: "search"`` provider takes the branch below and looks up no entry
    at all — the voice job holds no registry, the same reason as
    ``_build_llm_one``. Nothing about its models needs pinning at publish: the
    client reads only the provider's ``models.stt`` block, and no language is
    ever sent.
    """
    provider = _provider_key(spec.provider)
    meta = get_catalog().providers.get(provider)
    searched = meta.models.stt if meta is not None and meta.models is not None else None
    if searched is not None:
        return _build_searched_stt(
            spec,
            searched,
            _key(provider, provider_keys),
            provider=provider,
            vad=vad,
            reported_cost=reported_cost,
        )
    entry = stt_entry(spec)
    extra = dict(entry.extra)
    language = resolve_entry_language(agent_language, entry)
    key = _key(provider, provider_keys)
    settings = get_settings()
    provider_endpointing_silence = provider_endpointing_seconds(turn_handling, spec)
    provider_endpointing_ms = max(0, round(provider_endpointing_silence * 1000))
    if not entry.streaming:
        if vad is None:
            raise ValueError(
                f"STT {provider}/{spec.model} is a batch model and needs a VAD to segment "
                "speech — pass the session VAD to build_stt"
            )
        return stt.StreamAdapter(stt=_build_batch_stt(spec, entry, key, language), vad=vad)
    if provider == "xai":
        # Language is a first-class catalog/agent field, never entry.extra.
        extra["endpointing"] = provider_endpointing_ms
        return _NamedXaiSTT(
            catalog_model=entry.model,
            api_key=key,
            language=_required_language(language, entry, "STT model"),
            **extra,
        )
    if provider == "sarvam":
        if spec.model == "saaras:v3-realtime":
            # A different endpoint (the speech-to-text-realtime websocket), so a
            # different class, and one that takes no `model` at all: it serves
            # this one model and nothing else. Its language set is its own too —
            # Odia is `or-IN` here and `od-IN` on `sarvam.STT`, and the plugin
            # raises on construction rather than at the socket, which is what
            # keeps that mismatch a compile error instead of a dropped call.
            return sarvam.STTRealtime(
                api_key=key,
                language=_required_language(language, entry, "STT model"),
                **extra,
            )
        return sarvam.STT(
            api_key=key,
            language=_required_language(language, entry, "STT model"),
            model=spec.model,
            **extra,
        )
    if provider == "soniox":
        # `language_hints` is where the agent's language goes, and `strict` is
        # what makes it mean anything. Measured 2026-08-12: an English sentence
        # in a strong Indian accent came back as Gujarati script — with
        # `language_hints: ["en"]` set. The same audio under
        # `language_hints_strict` transcribed as correct English. A non-strict
        # hint only nudges the language identifier, so on an agent that has
        # already declared its one language it buys nothing and lets the LLM be
        # handed a turn in the wrong script.
        #
        # On Auto neither is sent and all 60 languages are detected freely,
        # which is the honest reading of Auto and the setting to leave alone —
        # strict with no hints would have nothing to be strict about.
        hints = {"language_hints": [language], "language_hints_strict": True} if language else {}
        return soniox.STT(
            api_key=key,
            params=soniox.STTOptions(
                model=spec.model,
                max_endpoint_delay_ms=provider_endpointing_ms,
                **hints,
                **extra,
            ),
        )
    if provider == "elevenlabs":
        # model_id picks the Scribe variant (scribe_v2_realtime = streaming);
        # Scribe can auto-detect when no language_code is provided.
        kwargs = dict(extra)
        # ElevenLabs Scribe realtime otherwise uses commit_strategy=manual, but
        # the LiveKit plugin does not send manual commit frames on turn flush.
        # Supplying server_vad makes LiveKit use commit_strategy=vad.
        kwargs.setdefault(
            "server_vad",
            {
                "vad_silence_threshold_secs": provider_endpointing_silence,
                "vad_threshold": 0.5,
                "min_speech_duration_ms": 100,
                "min_silence_duration_ms": provider_endpointing_ms,
            },
        )
        if language:
            kwargs["language_code"] = language
        return elevenlabs.STT(api_key=key, model=spec.model, **kwargs)
    if provider == "openai":
        # Streaming OpenAI STT means the Realtime transcription API; the plugin
        # would otherwise default to batch/REST. Batch models never reach here —
        # they return above via _build_batch_stt + StreamAdapter.
        kwargs = dict(extra)
        kwargs["use_realtime"] = True
        needs_eos_bridge = spec.model.startswith(_OPENAI_CLIENT_COMMIT_MODELS)
        if needs_eos_bridge:
            # Its own VAD, not the session one: this VAD triggers
            # input_audio_buffer.commit, so it is this model's endpointer and
            # takes the provider window. Under the modes where the STT only
            # transcribes that window is short, so it commits more often — the
            # same trade every follower-mode provider makes here.
            kwargs["vad"] = silero.VAD.load(
                min_speech_duration=settings.vad.min_speech_duration,
                min_silence_duration=provider_endpointing_silence,
                activation_threshold=settings.vad.activation_threshold,
            )
        if language:
            kwargs["language"] = language
            kwargs["detect_language"] = False
        else:
            kwargs["detect_language"] = True
        openai_stt = openai.STT(api_key=key, model=spec.model, **kwargs)
        return _EndOfSpeechAfterFinalSTT(openai_stt) if needs_eos_bridge else openai_stt
    if provider == "deepgram":
        if spec.model in {"flux-general-en", "flux-general-multi"}:
            # Flux takes a hint, not a setting: STTv2 sends `language_hint` and
            # never sends `language` at all, so Flux Multilingual auto-detects
            # when the agent is on Auto. Known limitation: STTv2 also *labels*
            # every transcript with its own `_opts.language` ("en", settable via
            # neither the constructor nor update_options), so on this model the
            # LiveKit turn detector always reads English and picks the English
            # threshold. Harmless in the silence-based modes, slightly
            # miscalibrated when the end-of-turn model runs.
            # No endpointing window is sent because STTv2 has none to send: Flux
            # runs its own end-of-turn model on probability thresholds and emits
            # a final transcript only when it fires. The agent's silence budget
            # is therefore a floor on this model, never an exact wait.
            kwargs = dict(extra)
            if spec.model == "flux-general-multi" and language:
                kwargs["language_hint"] = [language]
            return deepgram.STTv2(
                api_key=key,
                model=spec.model,
                **kwargs,
            )
        # Language is first-class (agent config / default_language). Catalog extra
        # holds only plugin knobs like smart_format — never language.
        extra["endpointing_ms"] = provider_endpointing_ms
        return deepgram.STT(
            api_key=key,
            model=spec.model,
            language=_required_language(language, entry, "STT model"),
            **extra,
        )
    raise ValueError(f"unsupported stt provider: {provider}")


def build_tts(
    spec: TTSSpec,
    provider_keys: dict[str, str],
    *,
    agent_language: str | None = None,
) -> tts.TTS:
    """The configured TTS, wrapped for failover when the spec names a fallback.

    ``FallbackAdapter`` resamples to the highest sample rate among the voices but
    insists they share a channel count. Every catalog TTS is mono, so the pair is
    always compatible — including on video, where the avatar renders from
    whichever of the two is speaking.
    """
    primary = _build_tts_one(spec, provider_keys, agent_language=agent_language)
    if spec.fallback is None:
        return primary
    secondary = _build_tts_one(spec.fallback, provider_keys, agent_language=agent_language)
    return tts.FallbackAdapter([primary, secondary])


def _build_tts_one(
    spec: TTSModelSpec,
    provider_keys: dict[str, str],
    *,
    agent_language: str | None = None,
) -> tts.TTS:
    provider = _provider_key(spec.provider)
    entry = get_catalog().require_entry("tts", provider, spec.model)
    if not isinstance(entry, TTSEntry):
        raise TypeError(f"expected TTSEntry for {provider}/{spec.model}")
    extra = dict(entry.extra)
    language = resolve_entry_language(agent_language, entry)
    speed = _entry_speed(entry, spec.speed)
    voice = _entry_voice(entry, spec.voice)
    key = _key(provider, provider_keys)
    if provider == "xai":
        return xai.TTS(
            api_key=key,
            voice=voice,
            language=language or entry.default_language or "auto",
            speed=speed,
            **extra,
        )
    if provider == "sarvam":
        # Omitted on Auto rather than guessed. Bulbul v3 takes the spoken language
        # from the text it is given, not from this parameter — measured
        # 2026-08-11 across 7 scripts x 5 codes, synthesising and reading the
        # result back through Saaras: Tamil text tagged `hi-IN` still comes out
        # Tamil, and Hinglish stays Hinglish. The plugin will not accept an empty
        # code (it raises), so leave the argument off and take its own `en-IN`
        # default, which the API then ignores like any other.
        kwargs = dict(extra)
        if language:
            kwargs["target_language_code"] = language
        return sarvam.TTS(
            api_key=key,
            model=spec.model,
            speaker=voice,
            pace=speed,
            **kwargs,
        )
    if provider == "raya":
        # Language is required, never inferred: the API rejects a request without
        # one, and the plugin's own default is Hindi — so an agent left on Auto
        # would speak Hindi at whoever called. `language_required` on the catalog
        # entry makes agent validation refuse that at publish time, and
        # `_required_language` is the belt to its braces.
        #
        # `model` and `voice` travel together. Each Raya voice belongs to exactly
        # one of the two models and is rejected by the other, which is why the
        # catalog carries them as separate entries and the picker scopes itself
        # to the one the agent is on.
        return raya.TTS(
            api_key=key,
            model=spec.model,
            voice_id=voice,
            language=_required_language(language, entry, "TTS model"),
            speed=speed,
            **extra,
        )
    if provider == "soniox":
        # Language is required and consequential. The API 400s without one, and
        # the code chosen is the phonetic system the text is read through rather
        # than a label on it: English text sent as `es` comes out as English
        # words pronounced through Spanish phonics, which is not accented
        # English, it is noise. `language_required` on the entry keeps an agent
        # on Auto from reaching here at all; this is the belt to those braces.
        #
        # Script is a non-issue: Devanagari and romanized Hindi both synthesize
        # correctly as Hindi (measured 2026-08-12), so the prompt needs no script
        # discipline. Raya measured the same on romanized input.
        return soniox.TTS(
            api_key=key,
            model=spec.model,
            voice=voice,
            language=_required_language(language, entry, "TTS model"),
            speed=speed,
            **extra,
        )
    if provider == "elevenlabs":
        # `voice_id` is one of the ids from GET /catalog/voices?provider=elevenlabs.
        # Speed bounds come from the catalog entry (ElevenLabs Flash/Turbo: 0.8–1.2).
        #
        # stability/similarity_boost are the picked voice's own numbers, carried
        # on the agent (see TTSModelSpec). Unset sends no key, which ElevenLabs
        # reads as its global default — NOT as the voice's setting, which it has
        # no way to ask for once any voice_settings object is present at all.
        #
        # `speed` follows the entry's own flag rather than always going out at
        # 1.0: `eleven_v3` has no speed control at all (see the catalog entry),
        # it is synthesized over text-to-dialogue since livekit-agents 1.7, and
        # that API warns about every voice_settings key it does not read.
        kwargs = dict(extra)
        if language:
            kwargs["language"] = language
        return elevenlabs.TTS(
            api_key=key,
            model=spec.model,
            voice_id=voice,
            voice_settings=_ElevenVoiceSettings(
                stability=spec.stability if entry.supports_voice_settings else None,
                similarity_boost=spec.similarity_boost if entry.supports_voice_settings else None,
                speed=speed if entry.supports_speed else None,
            ),
            **kwargs,
        )
    if provider == "openai":
        # OpenAI TTS has no language param (the model is multilingual); `voice` is
        # one of the static voices in the catalog entry (alloy, echo, …).
        return openai.TTS(
            api_key=key,
            model=spec.model,
            voice=voice,
            speed=speed,
            **extra,
        )
    if provider == "deepgram":
        # Deepgram TTS encodes the voice IN the model id (aura-2-<voice>-en,
        # flux-<voice>-en) and takes no separate voice/language param. spec.model
        # is our catalog/billing id ("flux"/"aura-2"/"aura-1"); voice carries the
        # real Deepgram model string (catalog default_voice or the picked voice).
        if spec.model == "flux":
            # A different endpoint, so a different class: Flux TTS is served on
            # /v2/speak, which `deepgram.TTS` does not speak at all.
            return deepgram.TTSv2(api_key=key, model=voice, **extra)
        return deepgram.TTS(api_key=key, model=voice, **extra)
    raise ValueError(f"unsupported tts provider: {provider}")


def _realtime_silence_ms(turn_handling: TurnHandlingSpec) -> int:
    """The whole endpointing budget, in the provider's units.

    Simpler than the cascade: the model's own server VAD is the only
    end-of-turn detector in the session, and it commits the turn before any
    transcript exists, so LiveKit is given no endpointing block at all
    (``compile._realtime_turn_handling``) and there is only one window to set.
    """
    total = turn_handling.endpointing.min_silence_duration
    return max(0, round(total * 1000))


def build_realtime(
    spec: RealtimeSpec,
    provider_keys: dict[str, str],
    *,
    agent_language: str | None = None,
    turn_handling: TurnHandlingSpec,
) -> llm.RealtimeModel:
    """The configured speech-to-speech model — the whole pipeline in one object.

    No failover twin, unlike the three cascade builders. LiveKit *does* ship
    ``llm.RealtimeModelFallbackAdapter`` (since 1.6.5), so this is a product
    choice, not a missing adapter: it merges both models' capabilities
    conservatively for the whole call, even when the fallback never runs, so
    pairing anything with Gemini's Live API would cost the primary the ability to
    trim an interrupted reply and reconnect the model on every handoff.

    Two settings are threaded in rather than left to plugin defaults:

    *Transcription.* These models emit no transcript unless asked, and Talqing
    persists every conversation item — so an unset transcription config would
    empty the call's transcript with no error anywhere.

    *Turn detection.* Every entry uses the provider's server VAD rather than a
    semantic classifier, so the agent's endpointing setting keeps meaning: the
    silence budget maps onto the provider's own silence window instead of being
    quietly overridden by a model-side judgement about whether a sentence sounds
    finished.
    """
    provider = _provider_key(spec.provider)
    entry = get_catalog().require_entry("realtime", provider, spec.model)
    if not isinstance(entry, RealtimeEntry):
        raise TypeError(f"expected RealtimeEntry for {provider}/{spec.model}")
    extra = dict(entry.extra)
    key = _key(provider, provider_keys)
    voice = _entry_voice(entry, spec.voice)
    # A recognition hint, always optional: an entry that publishes no languages
    # (xAI) simply never receives one.
    language = resolve_entry_language(agent_language, entry)
    silence_ms = _realtime_silence_ms(turn_handling)
    interruptible = turn_handling.interruption.enabled if turn_handling else True

    if provider == "openai" and entry.api == "live":
        # OpenAI's GPT-Live: a different service from /v1/realtime, so a
        # different class. Almost none of the realtime kwargs exist here —
        # it transcribes both sides itself, detects turns itself (it is full
        # duplex, so `silence_ms` and `interruptible` have nothing to configure),
        # and exposes no speed control.
        #
        # `backend_model` is a catalog field, not a plugin kwarg, for the same
        # reason `transcription_model` is one below: GPT-Live delegates its
        # reasoning and every tool call to a second model billed at that model's
        # own per-token rates, so which model runs is a pricing decision that
        # belongs in catalog.yaml.
        backend_model = extra.pop("backend_model", None)
        if not backend_model:
            raise ValueError(
                f"realtime {provider}/{spec.model} has no backend_model in catalog.yaml — "
                "GPT-Live runs its reasoning and tools on a Responses model, and leaving "
                "it unset would take OpenAI's default without pricing it"
            )
        return openai.realtime.GPTLiveModel(
            model=spec.model,
            voice=voice,
            api_key=key,
            responses_options={"model": backend_model},
            **extra,
        )
    if entry.api != "realtime":
        raise ValueError(
            f"realtime {provider}/{spec.model} declares api '{entry.api}', which only "
            "OpenAI serves — there is no client here that speaks it"
        )
    if provider == "openai":
        # `transcription_model` is a catalog field, not a plugin kwarg: OpenAI
        # bills the transcription separately from the session's audio tokens, so
        # which model runs is a pricing decision that belongs in catalog.yaml.
        transcription_model = extra.pop("transcription_model", None)
        if not transcription_model:
            raise ValueError(
                f"realtime {provider}/{spec.model} has no transcription_model in catalog.yaml — "
                "without one OpenAI returns no user transcript at all"
            )
        # `language` is OMITTED, never sent as null. The plugin serializes the
        # session config with exclude_unset=True, so a field we set to None goes
        # out as an explicit `null` — and OpenAI rejects that with an invalid_type
        # error that fails the WHOLE session.update, taking the voice, the speed
        # and the turn detection down with the transcription config.
        transcription: dict[str, object] = {"model": transcription_model}
        if language:
            transcription["language"] = language
        return openai.realtime.RealtimeModel(
            model=spec.model,
            voice=voice,
            api_key=key,
            speed=_entry_speed(entry, spec.speed),
            input_audio_transcription=AudioTranscription(**transcription),  # type: ignore[arg-type]
            turn_detection=ServerVad(
                type="server_vad",
                silence_duration_ms=silence_ms,
                create_response=True,
                interrupt_response=interruptible,
            ),
            **extra,
        )
    if provider == "gemini":
        # Gemini transcribes both directions inside the Live session; the input
        # side feeds our transcript and the output side feeds the captions.
        return google.realtime.RealtimeModel(
            model=spec.model,
            voice=voice,
            api_key=key,
            language=language or NOT_GIVEN,
            input_audio_transcription=google_types.AudioTranscriptionConfig(),
            output_audio_transcription=google_types.AudioTranscriptionConfig(),
            realtime_input_config=google_types.RealtimeInputConfig(
                automatic_activity_detection=google_types.AutomaticActivityDetection(
                    silence_duration_ms=silence_ms,
                ),
            ),
            **extra,
        )
    if provider == "xai":
        # The xAI plugin sets its own transcription config and fixes the
        # modalities; voice, model and turn detection are the knobs it exposes.
        return xai.realtime.RealtimeModel(
            model=spec.model,
            voice=voice,
            api_key=key,
            turn_detection=ServerVad(
                type="server_vad",
                silence_duration_ms=silence_ms,
                create_response=True,
                interrupt_response=interruptible,
            ),
            **extra,
        )
    raise ValueError(f"unsupported realtime provider: {provider}")


# The plugin's enum also carries SPARROW_S (which it deprecates itself) and
# ROOK_S (undocumented for LiveKit). Mapped explicitly rather than by
# `EnhancerModel[model.upper()]` so the catalog can never reach a model we have
# not decided to offer.
_ENHANCER_MODELS: dict[str, ai_coustics.EnhancerModel] = {
    "quail_vf_l": ai_coustics.EnhancerModel.QUAIL_VF_L,
    "quail_vf_s": ai_coustics.EnhancerModel.QUAIL_VF_S,
    "quail_l": ai_coustics.EnhancerModel.QUAIL_L,
}


def build_noise_cancellation(
    spec: NoiseCancellationSpec,
    provider_keys: dict[str, str],
) -> rtc.FrameProcessor[rtc.AudioFrame] | None:
    """The enhancer for one session's inbound audio, or None when it is off.

    Stateful: it holds a native core keyed to the stream's sample rate, so this
    must be called per session and the result never shared between calls.

    `vad_settings` is left at the plugin's default (every field None, i.e. the
    bundled SDK's own defaults). The enhancer runs a VAD internally whatever we
    pass, and its output is only metadata here — Silero remains the session's
    VAD, so endpointing and interruption keep honouring `turn_handling`.
    """
    if not spec.enabled:
        return None
    provider = _provider_key(spec.provider)
    if provider != "aicoustics":
        raise ValueError(f"unsupported noise cancellation provider: {provider}")
    model = _ENHANCER_MODELS.get(spec.model)
    if model is None:
        raise ValueError(f"unsupported noise cancellation model: {spec.model!r}")
    return ai_coustics.audio_enhancement(
        model=model,
        model_parameters=ai_coustics.ModelParameters(enhancement_level=spec.enhancement_level),
        # Never Auth.livekit_cloud() (the plugin's default): that bills the
        # enhancement through LiveKit Cloud, which we do not use. The tenant's
        # own license key keeps it on our workers and on their ai-coustics plan.
        auth=ai_coustics.Auth.ai_coustics_api(license_key=_key(provider, provider_keys)),
    )
