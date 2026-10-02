"""Catalog domain structs and service request/response shapes.

Domain: shapes of ``catalog.yaml`` (pricing, entries, ``Catalog``).
API: HTTP/CoPilot request/response models for public catalog, voices, avatars.

Free helpers live next to their use:
- Load/cache: ``services.catalog.loader``
- Live voice galleries: ``services.catalog.voices``
- Public ops: ``services.catalog.service``
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated, Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

# ───────────────────────────── provider catalog ─────────────────────────────

# Each offering is typed by KIND: an entry — and its pricing block — carries
# ONLY the fields that kind actually uses, so a price key resolves on the kind
# that bills it.


class Pricing(BaseModel):
    """Base pricing block. Subclassed per kind; each per-kind PriceLine narrows
    `rates` to its matching subclass, so a priced line dumps its real,
    kind-specific rates into the pricing_snapshot."""


class LLMPricing(Pricing):
    input_per_1m: float | None = None
    cached_input_per_1m: float | None = None
    # What it costs to PUT a prefix in the cache, when the provider charges for
    # that separately. OpenAI bills it at 1.25x input from the GPT-5.6 family
    # onward and Anthropic bills every cache write at 1.25x, so a model whose
    # entry omits this while reporting cache writes is under-billed — which is
    # why `price_llm_usage` quarantines that combination outright rather than
    # pricing the writes at zero. (Not through `_RowRates.require_billable`,
    # which only fires when a row matched NO rate at all; input and output
    # always match, so a missing cache-write rate needs its own check.)
    #
    # Transcribed per model from the provider's own pricing page, never derived
    # from a multiplier: the multiplier differs per vendor and per family,
    # exactly as `PriorityTier`'s premium does.
    cache_write_per_1m: float | None = None
    output_per_1m: float | None = None


class STTPricing(Pricing):
    per_audio_second: float | None = None
    input_per_1m: float | None = None
    output_per_1m: float | None = None


class TTSPricing(Pricing):
    per_character: float | None = None
    per_audio_second: float | None = None  # some TTS vendors bill audio, not chars
    input_per_1m: float | None = None  # token-based TTS, e.g. OpenAI realtime/audio
    output_per_1m: float | None = None


class RealtimePricing(Pricing):
    """Speech-to-speech rates. Two billing shapes exist and an entry uses one.

    OpenAI and Gemini price every modality separately per 1M tokens — audio
    input costs ~10x text input, so the modality split is the whole point and
    a single `input_per_1m` would be off by an order of magnitude.

    xAI prices the audio stream by the minute and does not bill tokens at all;
    its token rates are a real 0.00, not a missing rate.
    """

    text_input_per_1m: float | None = None
    cached_text_input_per_1m: float | None = None
    audio_input_per_1m: float | None = None
    cached_audio_input_per_1m: float | None = None
    text_output_per_1m: float | None = None
    audio_output_per_1m: float | None = None
    # Priced against the realtime socket's connection duration. LiveKit opens it
    # at session start and streams caller audio until the call ends, so
    # connection minutes and xAI's "minutes of audio sent or received" agree.
    per_session_minute: float | None = None


class AvatarPricing(Pricing):
    per_minute: float | None = None  # wall-clock minutes, billed per second


class CatalogEntry(BaseModel):
    """Base provider/model offering. Subclassed per kind so each entry exposes
    only the descriptive fields its kind uses and a pricing block of the matching
    shape."""

    provider: str
    channel: list[Literal["text", "voice", "video"]] = Field(default_factory=list)
    model: str
    # Alternate model ids that resolve to this entry (e.g. LiveKit xAI STT/TTS
    # report model="unknown"). Prefer explicit aliases over guessing when a
    # provider has only one entry of a kind.
    aliases: list[str] = Field(default_factory=list)
    label: str | None = None
    # A caveat the agent's author has to act on, shown under the model in the
    # editor. For what the author must *do* differently on this model, typically
    # in the prompt — not for describing what it is, which is the label's job.
    # No entry sets one today; the field exists because the first one that needs
    # it should not also have to build the plumbing.
    note: str | None = None
    # The model maker's logo where it differs from the provider's; null means use
    # the provider's. Only the searched registry fills it, at read time.
    logo_url: str | None = None
    pricing: Pricing = Field(default_factory=Pricing)
    # arbitrary per-provider plugin kwargs — legitimately a dict (spread into the
    # plugin constructor); kept untyped on purpose.
    extra: dict[str, Any] = Field(default_factory=dict)

    @field_validator("provider")
    @classmethod
    def normalize_provider(cls, value: str) -> str:
        provider = str(value or "").strip().lower()
        if not provider:
            raise ValueError("catalog entry is missing provider")
        return provider

    @field_validator("aliases")
    @classmethod
    def clean_aliases(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        seen: set[str] = set()
        for raw in value or []:
            alias = str(raw or "").strip()
            if not alias or alias in seen:
                continue
            seen.add(alias)
            cleaned.append(alias)
        return cleaned


class SearchedLLMs(BaseModel):
    """How to CALL one of a searched provider's language models.

    Everything the runtime reads off an ``LLMEntry`` to build a client is
    identical across every model such a provider serves; the one per-model read,
    the reasoning effort, is frozen onto the config at publish.
    """

    model_config = ConfigDict(extra="forbid")

    api: Literal["chat_completions", "responses"] = "chat_completions"
    base_url: str
    supports_prompt_cache_key: bool = True
    extra: dict[str, Any] = Field(default_factory=dict)


class SearchedSTTs(BaseModel):
    """How to CALL one of a searched provider's speech-to-text models.

    ``STTEntry`` carries no ``base_url``, so the builder reads it from here.
    ``streaming`` is the one fact the runtime needs before any client exists —
    it decides turn detection — and a voice job holds no registry to read it
    from a per-model entry.
    """

    model_config = ConfigDict(extra="forbid")

    base_url: str
    streaming: bool
    extra: dict[str, Any] = Field(default_factory=dict)


class SearchedModels(BaseModel):
    """How to CALL a model from a provider whose models are not in this file,
    one block per kind that provider has a registry for.

    A ``browse: "search"`` provider offers hundreds of models and the list moves
    weekly, so they live in a live registry (``services.catalog.openrouter``)
    rather than in ``catalog.yaml``. A voice job process cannot hold that
    registry — it is pre-spawned into an idle pool, handles one call, and on
    Linux is forked from a clean forkserver, so it inherits nothing and filling
    it would put an HTTP round trip on the call-start path. Declaring what the
    runtime needs once per kind is therefore what lets a call run with no
    registry at all.
    """

    model_config = ConfigDict(extra="forbid")

    llm: SearchedLLMs | None = None
    stt: SearchedSTTs | None = None

    @model_validator(mode="after")
    def check_kinds(self) -> Self:
        if not self.kinds():
            raise ValueError("a `models:` block must declare at least one kind (`llm:`, `stt:`)")
        return self

    def kinds(self) -> list[Literal["llm", "stt"]]:
        """The kinds this provider's registry serves, in a stable order."""
        return [kind for kind in ("llm", "stt") if getattr(self, kind) is not None]


class ProviderEntry(BaseModel):
    label: str
    logo_url: str
    enabled: bool = True
    # How the editor offers this provider's models. "list" means every model it
    # has is written out under `llm:`/`stt:`/… below and the editor renders a
    # dropdown. "search" means the models come from a live registry instead and
    # the editor renders a search box — they are real entries that `entry()` and
    # `require_entry()` resolve, they are simply too many and too fast-moving to
    # ship in a static file or in `GET /catalog`. Provider-wide: WHICH kinds are
    # searched is what `models:` declares.
    browse: Literal["list", "search"] = "list"
    # Required on a "search" provider, meaningless on a "list" one.
    models: SearchedModels | None = None

    @model_validator(mode="after")
    def check_browse(self) -> Self:
        if (self.browse == "search") != (self.models is not None):
            raise ValueError(
                "a provider block declares `models:` if and only if it is `browse: search` — "
                f"got browse={self.browse!r} with models={'set' if self.models else 'unset'}"
            )
        return self

    def searched(self, kind: str) -> SearchedLLMs | SearchedSTTs | None:
        """This provider's block for `kind`, or None when that kind is not
        searched here — including every kind of a `list` provider."""
        if self.models is None or kind not in ("llm", "stt"):
            return None
        return getattr(self.models, kind)


class CatalogLanguageOption(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    name: str

    @field_validator("code", "name")
    @classmethod
    def non_empty(cls, value: str) -> str:
        value = (value or "").strip()
        if not value:
            raise ValueError("catalog language code/name must be non-empty")
        return value


class BuiltinToolOption(BaseModel):
    """One configurable field of a provider built-in tool.

    `key` is what the agent config stores; `path` is where the value belongs
    inside the tool object on the wire. The two differ because providers nest
    the same idea differently — domain filters sit under `filters` for both xAI
    and OpenAI, while xAI's image switches are top-level."""

    key: str
    path: list[str] = Field(min_length=1)
    label: str
    type: Literal["boolean", "string", "string_list", "integer", "enum"]
    description: str | None = None
    choices: list[str] = Field(default_factory=list)  # enum only
    max_items: int | None = None  # string_list only
    required: bool = False
    # Options the provider refuses in the same request — an allow-list and a
    # block-list of the same thing. Declared both ways round on the pair.
    conflicts_with: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_type_fields(self) -> Self:
        if self.type == "enum" and not self.choices:
            raise ValueError(f"builtin tool option '{self.key}' is an enum with no choices")
        if self.type != "enum" and self.choices:
            raise ValueError(f"builtin tool option '{self.key}' is not an enum but lists choices")
        if self.max_items is not None and self.type != "string_list":
            raise ValueError(f"builtin tool option '{self.key}' sets max_items but is not a list")
        return self


# Tool types the provider hands BACK to the caller to execute. Talqing runs no
# such loop, so declaring one would hang the turn waiting for a result nobody
# produces — rejected at load rather than at 3am on a live call.
CLIENT_EXECUTED_TOOL_TYPES = frozenset(
    {
        "computer",
        "computer_use_preview",
        "shell",
        "local_shell",
        "apply_patch",
        "programmatic_tool_calling",
        "tool_search",
        "function",
        "custom",
        "namespace",
    }
)


class BuiltinTool(BaseModel):
    """A tool the model provider runs on its own servers during the agent's turn.

    We never see the call: the provider searches, executes or retrieves, and
    folds the result into the assistant's reply. That is also why there is no
    Python class per tool — the wire form is `{"type": ..., **options}`, so this
    declaration plus the agent's chosen values is the whole definition."""

    type: str
    label: str
    description: str
    # Shown beside the toggle. Display only: we do not meter invocations, so this
    # is what tells an author the call costs money outside Talqing's estimate.
    price_note: str
    # Parts of the tool object that are ours to decide, not the author's — e.g.
    # OpenAI requires a `container` on code_interpreter, and the only one we can
    # offer is an auto-provisioned one. Merged under `options`, which may write
    # inside it (`container.memory_limit`).
    fixed: dict[str, Any] = Field(default_factory=dict)
    options: list[BuiltinToolOption] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_tool(self) -> Self:
        if self.type in CLIENT_EXECUTED_TOOL_TYPES:
            raise ValueError(
                f"builtin tool '{self.type}' is executed by the caller, not the provider — "
                "Talqing has no execution loop for it and the turn would stall"
            )
        keys = [option.key for option in self.options]
        duplicates = {key for key in keys if keys.count(key) > 1}
        if duplicates:
            raise ValueError(
                f"builtin tool '{self.type}' repeats option key(s) {sorted(duplicates)}"
            )
        for option in self.options:
            for other in option.conflicts_with:
                if other not in keys:
                    raise ValueError(
                        f"builtin tool '{self.type}' option '{option.key}' conflicts with "
                        f"'{other}', which it does not declare"
                    )
        return self

    def option(self, key: str) -> BuiltinToolOption | None:
        return next((option for option in self.options if option.key == key), None)


class PriorityTier(BaseModel):
    """The low-latency lane a model sells, and what that lane costs.

    Every provider we carry schedules priority traffic ahead of standard traffic
    for a per-token premium, asked for with the same `service_tier` body
    parameter. Presence of this block is what declares support — there is no
    separate boolean, for the same reason `reasoning_efforts: []` needs none.

    The premium is NOT a multiplier: it is 2x across OpenAI's 5.6 family, 2.5x on
    gpt-5.5, 1.75x on gpt-4.1 and a flat 1.8x at Gemini — so the rates are
    transcribed per model rather than derived.
    """

    pricing: LLMPricing
    # What goes on the wire as `service_tier`. Every provider today spells it
    # "priority"; OpenAI also accepts "fast" (renamed 2026-07-30, same lane) but
    # reports "priority" back and neither our openai SDK nor the LiveKit plugin
    # types "fast", so "priority" is the value that is true everywhere. A future
    # provider that spells it differently is a catalog edit, not a code change.
    value: str = "priority"
    # Whether the response says which tier ACTUALLY served the request. OpenAI
    # and xAI both echo `service_tier` back; Gemini's compatibility layer
    # validates the parameter (a bad value 400s) but returns nothing, so its
    # documented "graceful degradation" to standard is invisible to us. Billing
    # charges the tier the agent asked for, which on Gemini is therefore taken on
    # trust — recorded here so that stays a fact in the catalog, not folklore.
    confirms: bool = True


ReasoningEffort = Literal["none", "minimal", "low", "medium", "high"]

# Fastest first. This is the order `reasoning_efforts` must be declared in, which
# is what lets the first value be the default without a second field to say so.
REASONING_EFFORT_ORDER: tuple[ReasoningEffort, ...] = (
    "none",
    "minimal",
    "low",
    "medium",
    "high",
)


class LLMEntry(CatalogEntry):
    # Which LiveKit client class the runtime builds. The first two are the
    # OpenAI plugin against the provider's own `base_url`: Chat Completions is
    # the compatibility path every OpenAI-compatible endpoint speaks, Responses
    # is the richer one OpenAI and xAI also serve. `native` is the provider's own
    # LiveKit plugin, taken when the compatibility endpoint cannot express
    # something the model is worth having for — Gemini's server-side tools live
    # nowhere else, since Google serves no /responses at all.
    api: Literal["chat_completions", "responses", "native"] = "chat_completions"
    # Every thinking effort this exact model was measured to accept, fastest
    # first; empty for a model with no such knob (nothing is sent, and the editor
    # shows no control). The first value is what an agent that chooses nothing
    # gets — always the fastest the model allows, because a voice caller hears
    # thinking as silence. Replaces the old `supports_reasoning` flag, which
    # could only say "accepts none" and so had no way to offer anything above it.
    reasoning_efforts: list[ReasoningEffort] = Field(default_factory=list)
    # The provider's OpenAI-compatible endpoint. Required by the two OpenAI-plugin
    # APIs; on a `native` entry it is only what `services.llm_client` (post-call
    # analysis) still talks to, since that client is a plain
    # AsyncOpenAI rather than a LiveKit plugin.
    base_url: str | None = None
    # False for an endpoint that rejects the `prompt_cache_key` body parameter.
    # Google's compatibility layer validates the request against its own proto
    # and 400s on any field it does not know, so sending one there fails the
    # turn outright rather than being ignored (measured 2026-08-07). Gemini
    # caches implicitly and needs no key, so there is nothing lost by omitting it.
    supports_prompt_cache_key: bool = True
    # Server-side tools this model can be given. Out of reach on Chat Completions
    # — that request has nowhere to put them.
    builtin_tools: list[BuiltinTool] = Field(default_factory=list)
    pricing: LLMPricing = Field(default_factory=LLMPricing)
    # The provider's low-latency lane, when this exact model sells one. Per model
    # and not per provider: OpenAI offers it on eight of our nine entries and not
    # on gpt-5.4-nano, which is absent from its fast pricing table entirely.
    priority: PriorityTier | None = None

    # Whether this exact model reads an image handed to it in the user turn.
    # PROBED for every entry in `catalog.yaml`, never read off a provider's docs
    # — the same house rule `reasoning_efforts` follows, and there it earns its
    # keep: xAI's realtime models accept a `conversation.item.create` carrying an
    # image without any error, echo the item back with the image part silently
    # removed, and then answer confidently about a photo they never saw.
    #
    # A `browse: "search"` provider is the one exception, deliberately: ~260
    # models cannot be probed, still less on every refresh, so its registry
    # derives this from the provider's own `input_modalities`. What makes that
    # safe is a property the static entries do not have — OpenRouter answers an
    # image sent to a text-only model with `404 "No endpoints found that support
    # image input"`, so a wrong `true` fails loudly rather than lying. Either way
    # this means CAPABILITY and not accuracy: a model marked true can still
    # misread the picture (grok-4.3 did, on our own probe).
    vision: bool = False
    # The model's context window in tokens, where the provider publishes one per
    # model. Only the searched registry does; the editor shows it beside the
    # price so a slug can be judged before it is picked.
    context_length: int | None = None

    @model_validator(mode="after")
    def check_api(self) -> Self:
        if self.builtin_tools and self.api == "chat_completions":
            raise ValueError(
                f"llm entry {self.provider}/{self.model} declares builtin_tools but "
                "api is 'chat_completions' — that request has nowhere to put a "
                "server-side tool; use 'responses' or 'native'"
            )
        if self.api == "native" and self.provider == "gemini" and "none" in self.reasoning_efforts:
            # `api: native` says which client class, never which vendor — this
            # rule is Gemini's alone. Its thinking knob is `thinking_level`, an
            # enum with no "none" member, and google-genai lets an unknown string
            # through with only a UserWarning, so without this the mistake
            # surfaces as a 400 on a live call instead of at load.
            raise ValueError(
                f"llm entry {self.provider}/{self.model} offers reasoning effort 'none' on the "
                "native client — Gemini's thinking_level has no such value; 'minimal' is the floor"
            )
        # `extra` goes on the wire underneath the runtime's own kwargs, so a
        # reasoning knob hidden in there would be silently overridden on one path
        # and authoritative on another. There is a field for it now.
        if {"reasoning", "reasoning_effort"} & set(self.extra):
            raise ValueError(
                f"llm entry {self.provider}/{self.model} sets a reasoning knob in `extra` — "
                "declare `reasoning_efforts: [...]` instead"
            )
        # Same trap, worse consequence: `extra` reaches the wire but not billing,
        # so a tier hidden there would buy the premium lane and charge standard
        # rates for it. The `priority:` block is the only way in, because it is
        # the only one that carries the price.
        if "service_tier" in self.extra:
            raise ValueError(
                f"llm entry {self.provider}/{self.model} sets `service_tier` in `extra` — "
                "declare a `priority: {...}` block instead, so the tier is priced"
            )
        if len(set(self.reasoning_efforts)) != len(self.reasoning_efforts):
            raise ValueError(f"llm entry {self.provider}/{self.model} repeats a reasoning effort")
        ranked = sorted(self.reasoning_efforts, key=REASONING_EFFORT_ORDER.index)
        if ranked != self.reasoning_efforts:
            # The first value is the default, so an unsorted list would quietly
            # make some middling effort the one every agent on this model runs at.
            raise ValueError(
                f"llm entry {self.provider}/{self.model} lists reasoning_efforts out of "
                f"order — declare them fastest first: {ranked}"
            )
        types = [tool.type for tool in self.builtin_tools]
        duplicates = {t for t in types if types.count(t) > 1}
        if duplicates:
            raise ValueError(
                f"llm entry {self.provider}/{self.model} repeats builtin tool(s) {sorted(duplicates)}"
            )
        return self

    def builtin_tool(self, tool_type: str) -> BuiltinTool | None:
        return next((tool for tool in self.builtin_tools if tool.type == tool_type), None)

    def effort_for(self, chosen: str | None) -> str | None:
        """The reasoning effort to send, or None to send nothing at all.

        `chosen` is what the caller configured; unset takes the fastest value the
        model offers. A value that is not on offer is validated where it is
        written (agent validation, catalog validation), so seeing one here is a
        bug upstream — hence the raise rather than quietly correcting it.
        """
        if not self.reasoning_efforts:
            if chosen:
                raise ValueError(
                    f"llm {self.provider}/{self.model} takes no reasoning effort, got {chosen!r}"
                )
            return None
        if chosen is None:
            return self.reasoning_efforts[0]
        if chosen not in self.reasoning_efforts:
            raise ValueError(
                f"llm {self.provider}/{self.model} does not accept reasoning effort "
                f"{chosen!r} — it takes {', '.join(self.reasoning_efforts)}"
            )
        return chosen

    def require_priority(self) -> PriorityTier:
        """This model's priority lane, or a loud failure.

        Agent validation rejects the combination first, so reaching this on a
        model that sells no lane is a bug upstream — raise rather than quietly
        running the turn on standard processing (same contract as `effort_for`).
        """
        if self.priority is None:
            raise ValueError(f"llm {self.provider}/{self.model} has no priority tier to run in")
        return self.priority

    def rates_for(self, priority: bool) -> LLMPricing:
        """The rate block one usage row bills at — the priority lane's, or standard."""
        return self.require_priority().pricing if priority else self.pricing


class STTEntry(CatalogEntry):
    default_language: str | None = None
    languages: list[CatalogLanguageOption] = Field(default_factory=list)
    # True for models with no auto-detect mode at all, which therefore cannot run
    # on an agent whose language is Auto. An empty `default_language` alone does
    # not say this — for ElevenLabs Scribe it means "detects by itself".
    language_required: bool = False
    # False for batch (request/response) models. The compiler wraps those in
    # LiveKit's stt.StreamAdapter, which makes the local VAD the sole end-of-turn
    # detector. That is why this flag decides turn detection and the endpointing
    # windows both — see `resolve_turn_detection` and `endpointing_windows` in
    # compiler/factories.py.
    streaming: bool = True
    pricing: STTPricing = Field(default_factory=STTPricing)


class VoiceOption(BaseModel):
    """One selectable TTS voice. Mirrors the shape the editor's VoicePicker reads
    (id/name/sample_url/…) so static (config-declared) voices and live provider
    galleries render identically. `sample_url` is optional — providers without a
    preview endpoint (xAI, Sarvam) simply show no play button."""

    id: str
    name: str | None = None
    gender: str | None = None
    description: str | None = None
    sample_url: str | None = None
    tags: list[str] = Field(default_factory=list)


class ExpressiveDialect(BaseModel):
    """How one text-to-speech model is taught its own delivery markup.

    Presence of this block is what declares support, the same way `priority:`
    declares a low-latency lane — there is no second boolean able to disagree
    with it. Granularity is per entry (provider + model) because ElevenLabs
    forces it: `eleven_v3` honours `[laughs]`, while `eleven_flash_v2_5` reads
    the word "Laughs" out to the caller.

    One field, deliberately: the tag vocabulary is *inside* the prompt, because
    that is the only place it is used. A parallel list in the catalog would be a
    second copy of the same facts, free to drift from the prose that actually
    reaches the model.
    """

    model_config = ConfigDict(extra="forbid")

    # Appended verbatim to the system prompt of an agent that turns the toggle
    # on. Provider-native syntax, and written per dialect rather than templated:
    # an xAI wrapping tag spans exactly what it encloses, while an ElevenLabs tag
    # decays after four or five words, so the two need different advice and not
    # just different labels.
    prompt: str

    @field_validator("prompt")
    @classmethod
    def check_prompt(cls, value: str) -> str:
        prompt = value.strip()
        if not prompt:
            raise ValueError("an expressive dialect needs a prompt to teach its tags")
        return prompt


class TTSEntry(CatalogEntry):
    default_language: str | None = None
    languages: list[CatalogLanguageOption] = Field(default_factory=list)
    # See STTEntry.language_required.
    language_required: bool = False
    default_voice: str | None = None
    # Whether this model reads the voice-shaping knobs an agent's `tts` carries
    # (`stability` / `similarity_boost`). True only on ElevenLabs, which is the
    # one provider here that sells them; validation refuses a value anywhere
    # else rather than letting the compiler drop it silently.
    supports_voice_settings: bool = False
    supports_speed: bool = False
    speed_min: float = 0.5
    speed_max: float = 2.0
    # Static voice list for providers without a live gallery (OpenAI, Sarvam).
    # Empty for providers fetched live (ElevenLabs/xAI/Deepgram → /catalog/voices).
    voices: list[VoiceOption] = Field(default_factory=list)
    pricing: TTSPricing = Field(default_factory=TTSPricing)
    # Absent on every model whose synthesis is plain text. Two entries carry one.
    expressive: ExpressiveDialect | None = None


class RealtimeEntry(CatalogEntry):
    """One speech-to-speech model — the whole voice pipeline in a single model.

    Carries the descriptive fields of all three cascade kinds because it plays
    all three roles: a language (the STT hint), a voice and speed (the TTS
    settings). There is no `streaming` flag — every realtime model is a
    bidirectional stream by construction.
    """

    # Which LiveKit client class the runtime builds, the same way `LLMEntry.api`
    # picks one. `realtime` is the speech-to-speech socket all three original
    # providers speak (OpenAI /v1/realtime, Gemini Live, xAI). `live` is OpenAI's
    # separate full-duplex service on /v1/live/sessions, which is a different
    # endpoint, a different protocol and a different plugin class — not a model
    # id the realtime client would accept.
    api: Literal["realtime", "live"] = "realtime"
    # Whether this model accepts new instructions once its session is open. A
    # handoff is exactly that — `AgentSession.update_agent` hands the target
    # agent's prompt to the live session — so a model that refuses cannot be
    # handed off from, and agent validation says so at save time rather than
    # letting the call raise mid-conversation.
    supports_instruction_update: bool = True
    default_language: str | None = None
    languages: list[CatalogLanguageOption] = Field(default_factory=list)
    # See STTEntry.language_required. False for every realtime model we carry —
    # they all treat the language as an optional recognition hint — but the field
    # has to exist, because language validation walks every language-aware kind.
    language_required: bool = False
    default_voice: str | None = None
    supports_speed: bool = False
    speed_min: float = 0.5
    speed_max: float = 2.0
    # Static voice list. Empty for providers with a live gallery (xAI, whose
    # speech-to-speech roster is the same one GET /catalog/voices already serves).
    voices: list[VoiceOption] = Field(default_factory=list)
    pricing: RealtimePricing = Field(default_factory=RealtimePricing)

    # Whether this exact model reads an image handed to it in the user turn.
    # PROBED, never read off a provider's docs — the same house rule
    # `reasoning_efforts` follows, and here it earns its keep: xAI's realtime
    # models accept a `conversation.item.create` carrying an image without any
    # error, echo the item back with the image part silently removed, and then
    # answer confidently about a photo they never saw.
    vision: bool = False


class AvatarEntry(CatalogEntry):
    pricing: AvatarPricing = Field(default_factory=AvatarPricing)


class NoiseCancellationEntry(CatalogEntry):
    """One speech-enhancement model applied to the caller's inbound audio.

    Carries no pricing block: the tenant's own key bills them directly at the
    provider, so nothing here reaches metering or the $/min estimate.
    """

    # Which caller this model suits, in the editor's own words. Required rather
    # than optional because the three models differ only in that, and a dropdown
    # of three names no one can choose between is worse than no choice at all.
    description: str


class CopilotSpec(BaseModel):
    """The CoPilots' model — the `copilot` block in
    catalog.yaml. `provider`/`model` must match an exact llm catalog entry.
    `base_url` is optional only for OpenAI or when the catalog entry provides it."""

    provider: str
    model: str
    base_url: str | None = None
    # How hard the CoPilot agents think. Unset means Talqing's default for every
    # other agent — effort "none", which exists for voice-turn latency a CoPilot
    # does not have. Read by the CoPilot runtime (`build_llm`).
    reasoning_effort: Literal["none", "minimal", "low", "medium", "high"] | None = None


class PlatformFeePerMinute(BaseModel):
    """Our margin per minute of a call, per channel. Every channel is required: a
    missing key must fail the catalog load, never quietly make a channel free."""

    model_config = ConfigDict(extra="forbid")

    voice: float
    video: float

    def rate_for(self, channel: str) -> float:
        if channel not in type(self).model_fields:
            raise ValueError(f"no per-minute platform fee for channel {channel!r}")
        return float(getattr(self, channel))


class PlatformFeePerMessage(BaseModel):
    """Our margin per answered message of a chat."""

    model_config = ConfigDict(extra="forbid")

    text: float


class CostEstimateUsagePerMinute(BaseModel):
    """Usage assumptions for the dashboard's client-side $/min estimator.

    Provider prices still come from catalog entry pricing blocks; these quantities
    turn token/character-priced services into an approximate per-minute display.
    """

    model_config = ConfigDict(extra="forbid")

    llm_input_tokens: float
    llm_cached_input_tokens: float
    llm_output_tokens: float
    tts_characters: float
    tts_input_tokens: float
    tts_output_tokens: float
    # Streaming STT bills the open socket (all 60s); batch STT bills only the
    # utterances submitted, so the two are not comparable per wall-clock minute.
    stt_streaming_audio_seconds: float
    stt_batch_audio_seconds: float
    # Speech-to-speech: one model bills audio in both directions, plus a little
    # text for the prompt and tool traffic.
    realtime_audio_input_tokens: float
    realtime_cached_audio_input_tokens: float
    realtime_text_input_tokens: float
    realtime_audio_output_tokens: float
    realtime_text_output_tokens: float
    # Screen share: the newest frame is added to every user turn, so this is
    # extra LLM input tokens per minute — and never cached, because the frame
    # sits in the request tail and never in stored history.
    screenshare_input_tokens: float


class CostEstimateUsagePerTextMessage(BaseModel):
    """Usage assumptions for the dashboard's per-message estimate of a text agent."""

    model_config = ConfigDict(extra="forbid")

    llm_input_tokens: float
    llm_cached_input_tokens: float
    llm_output_tokens: float


class Catalog(BaseModel):
    """Root shape of ``catalog.yaml`` plus lookup helpers.

    Load with ``yaml.safe_load`` + ``Catalog.model_validate``. After validation,
    offerings whose provider is disabled are dropped (runtime view).
    """

    model_config = ConfigDict(extra="forbid")

    providers: dict[str, ProviderEntry] = Field(default_factory=dict)
    llm: list[LLMEntry] = Field(default_factory=list)
    stt: list[STTEntry] = Field(default_factory=list)
    tts: list[TTSEntry] = Field(default_factory=list)
    realtime: list[RealtimeEntry] = Field(default_factory=list)
    avatar: list[AvatarEntry] = Field(default_factory=list)
    noise_cancellation: list[NoiseCancellationEntry] = Field(default_factory=list)
    copilot: CopilotSpec
    platform_fee_per_minute: PlatformFeePerMinute
    platform_fee_per_message: PlatformFeePerMessage
    # Deliberately not in `public()` and not on `CatalogResponse`: nothing
    # post-login quotes rupees, and the landing page is unauthenticated and never
    # calls /catalog. It is declared here because `extra="forbid"` requires it,
    # and it is in the file so the next person to move either number sees the
    # other. See the comment beside it in catalog.yaml.
    inr_per_usd: float
    cost_estimate_usage_per_minute: CostEstimateUsagePerMinute
    cost_estimate_usage_per_text_message: CostEstimateUsagePerTextMessage

    @model_validator(mode="after")
    def finalize_runtime_view(self) -> Self:
        for entries in (
            self.llm,
            self.stt,
            self.tts,
            self.realtime,
            self.avatar,
            self.noise_cancellation,
        ):
            for entry in entries:
                if entry.provider not in self.providers:
                    raise ValueError(
                        f"catalog entry {entry.provider}/{entry.model} is missing a "
                        f"providers.{entry.provider} block"
                    )

        self.llm = [e for e in self.llm if self.provider_enabled(e.provider)]
        self.stt = [e for e in self.stt if self.provider_enabled(e.provider)]
        self.tts = [e for e in self.tts if self.provider_enabled(e.provider)]
        self.realtime = [e for e in self.realtime if self.provider_enabled(e.provider)]
        self.avatar = [e for e in self.avatar if self.provider_enabled(e.provider)]
        self.noise_cancellation = [
            e for e in self.noise_cancellation if self.provider_enabled(e.provider)
        ]

        self._validate_model_ids("llm", self.llm)
        self._validate_model_ids("stt", self.stt)
        self._validate_model_ids("tts", self.tts)
        self._validate_model_ids("realtime", self.realtime)
        self._validate_model_ids("avatar", self.avatar)
        self._validate_model_ids("noise_cancellation", self.noise_cancellation)

        # `copilot` names a model the platform reaches for on its own, with no
        # agent config in between to be validated. Checked after the
        # provider-enabled filter above, so pointing it at a disabled provider
        # fails here rather than on the first call that needs it.
        #
        # Post-call analysis needs no equivalent block: it runs on the agent's
        # own LLM by default, which is already validated as part of the agent.
        if self.entry("llm", self.copilot.provider, self.copilot.model) is None:
            raise ValueError(
                f"catalog copilot names llm {self.copilot.provider}/{self.copilot.model}, "
                "which is not an enabled llm entry"
            )
        return self

    def provider_enabled(self, provider: str) -> bool:
        meta = self.providers.get(provider.lower())
        return bool(meta and meta.enabled)

    @staticmethod
    def _validate_model_ids(kind: str, entries: list[CatalogEntry]) -> None:
        """Fail loud on colliding model ids / aliases within a kind."""
        claimed: dict[tuple[str, str], str] = {}
        for e in entries:
            provider = e.provider.lower()
            names = [e.model, *e.aliases]
            if e.model in e.aliases:
                raise ValueError(
                    f"catalog {kind} entry {e.provider}/{e.model} lists its own model id as an alias"
                )
            for name in names:
                key = (provider, name)
                owner = claimed.get(key)
                if owner is not None:
                    raise ValueError(
                        f"catalog {kind} model id {e.provider}/{name!r} collides with "
                        f"entry {e.provider}/{owner}"
                    )
                claimed[key] = e.model

    def provider_label(self, provider: str) -> str:
        meta = self.providers.get(provider.lower())
        if meta is None:
            raise KeyError(f"unknown provider {provider!r}")
        return meta.label

    def _matches_model(self, entry: CatalogEntry, model: str) -> bool:
        return entry.model == model or model in entry.aliases

    def _find(self, kind: str, provider: str, model: str) -> CatalogEntry | None:
        # plugins report provider casing like "xAI"/"Sarvam"; the catalog uses
        # lowercase keys — match case-insensitively. Model must match the entry's
        # canonical model id or an explicit alias (e.g. xAI STT/TTS "unknown").
        # No single-entry guessing: adding a second model for a provider must not
        # silently change which entry an unmatched id resolves to.
        entries: list[CatalogEntry] = getattr(self, kind, [])
        p = provider.lower()
        for e in entries:
            if e.provider.lower() == p and self._matches_model(e, model):
                return e
        return self._searched(kind, p, model)

    def _searched(self, kind: str, provider: str, model: str) -> CatalogEntry | None:
        """One model from a ``browse: "search"`` provider's live registry.

        These entries are as real as the ones written out in ``catalog.yaml`` —
        validation, compilation and pricing all reach them through ``entry`` /
        ``require_entry`` and see no difference. The registry is asked last, so a
        hand-written entry always wins.

        A process with no refresher (a voice job) finds nothing here, and nothing
        on the call path asks: see the ``openrouter`` module docstring.

        Imported inside the function because that module builds catalog entries
        and so imports this one.
        """
        meta = self.providers.get(provider)
        if meta is None or not meta.enabled or meta.searched(kind) is None:
            return None
        from . import openrouter

        if provider != openrouter.PROVIDER:
            raise ValueError(
                f"provider {provider!r} is `browse: search` but has no registry — "
                f"only {openrouter.PROVIDER!r} does"
            )
        return openrouter.entry(kind, model)

    def _is_searched(self, kind: str, provider: str) -> bool:
        """Whether this provider's models of `kind` come from a live registry
        rather than from `catalog.yaml` — which is also whether a lookup for one
        of them can miss in a process that runs no refresher."""
        meta = self.providers.get(provider.strip().lower())
        return meta is not None and meta.searched(kind) is not None

    def entry(self, kind: str, provider: str, model: str) -> CatalogEntry | None:
        return self._find(kind, provider, model)

    def require_entry(self, kind: str, provider: str, model: str) -> CatalogEntry:
        """Lookup that fails loudly — use at compile/runtime after validation."""
        e = self._find(kind, provider, model)
        if e is None:
            raise ValueError(f"no catalog entry for {kind} provider={provider!r} model={model!r}")
        return e

    def canonicalize(self, kind: str, provider: str, model: str) -> tuple[str, str] | None:
        """The catalog's canonical (provider, model) for a plugin's reported
        names, or None when nothing matches. Case-insensitive on the provider;
        model matches the catalog id or an explicit `aliases` entry. Does NOT
        guess across provider names — an unrecognized provider (e.g. xAI plugin
        hostnames like `api.x.ai`) returns None so the caller can fall back to
        the agent's configured provider (ground truth) or fail loudly."""
        e = self._find(kind, provider, model)
        return (e.provider, e.model) if e else None

    def canonicalize_usage(
        self,
        kind: str,
        provider: str,
        model: str,
        configured: Sequence[tuple[str, str]],
    ) -> tuple[str, str]:
        """Canonical (provider, model) for one metered usage row.

        What the plugin reported wins when the catalog recognizes it. Several
        plugins report something it cannot: OpenAI reports the API hostname, xAI
        reports "unknown", and Deepgram TTS reports the voice-encoded model
        string rather than our billing id. For those, `configured` — the agent's
        own selection for this kind, primary first — is the ground truth.

        A configured fallback makes that ground truth ambiguous, so whichever
        half the plugin did report — the provider name, or the model id — has to
        single one of them out. Anything still unresolved is returned verbatim,
        so pricing raises UnpriceableUsageError and the session quarantines
        rather than billing a provider that never ran.

        ``configured`` is the agent that ANSWERED, so it does not name a handoff
        target's model. That is why the rescue below only ever narrows among
        entries it resolved: reaching for it when nothing resolved would relabel
        a second model's spend as the first model's, which is worse than leaving
        the row alone.
        """
        canon = self.canonicalize(kind, provider, model)
        if canon is not None:
            return canon
        if self._is_searched(kind, provider):
            # A `browse: "search"` provider reports our own canonical names —
            # the compiler's client overrides `provider` and the plugin echoes
            # the exact configured slug — so the pair is already right and the
            # lookup missed only because THIS process holds no registry (a voice
            # job never does). `configured` has nothing to add and, on a call
            # that handed off, would actively substitute the wrong model.
            return provider.lower(), model
        entries = [e for p, m in configured if (e := self._find(kind, p, m)) is not None]
        if len(entries) > 1:
            # Deepgram TTS reports a recognizable provider with a voice-encoded
            # model; OpenAI and xAI report the opposite. Try each half in turn,
            # then settle for the provider alone — two Deepgram voices give us
            # neither a matching model nor a unique provider, and attributing
            # that to the primary beats leaving a real, correctly-provider'd
            # charge unpriceable.
            by_provider = [e for e in entries if e.provider == provider.lower()]
            by_model = [e for e in entries if self._matches_model(e, model)]
            entries = (
                by_provider
                if len(by_provider) == 1
                else by_model
                if len(by_model) == 1
                else by_provider[:1]
            )
        if len(entries) == 1:
            return entries[0].provider, entries[0].model
        return provider, model

    def public(self) -> dict[str, Any]:
        """Shape returned by GET /catalog (drops internal `extra` / `aliases`).

        A ``browse: "search"`` provider contributes no entries at all, and that
        is the whole reason the concept exists: this payload is already ~107KB
        and the CoPilot loads it on every model call, so ~260 more models would
        add ~20k tokens to a request that has a ~52k-token floor before it starts
        (`memory/copilot-context-budget.md`). Those models are reached through
        `GET /catalog/models` instead, a few at a time. They are real entries —
        `entry()` and `require_entry()` resolve them — they are simply not
        something anyone can afford to be handed all at once.
        """

        def slim(entries: list[CatalogEntry]) -> list[dict[str, Any]]:
            return [e.model_dump(exclude={"extra", "aliases"}) for e in entries]

        return {
            "providers": {
                provider: {
                    **meta.model_dump(exclude={"models"}),
                    "searched_kinds": meta.models.kinds() if meta.models else [],
                }
                for provider, meta in self.providers.items()
            },
            "llm": slim(self.llm),
            "stt": slim(self.stt),
            "tts": slim(self.tts),
            "realtime": slim(self.realtime),
            "avatar": slim(self.avatar),
            "noise_cancellation": slim(self.noise_cancellation),
            "platform_fee_per_minute": self.platform_fee_per_minute.model_dump(),
            "platform_fee_per_message": self.platform_fee_per_message.model_dump(),
            "cost_estimate_usage_per_minute": self.cost_estimate_usage_per_minute.model_dump(),
            "cost_estimate_usage_per_text_message": (
                self.cost_estimate_usage_per_text_message.model_dump()
            ),
        }


# ───────────────────── HTTP / CoPilot request-response ──────────────────────


class AvatarItemResponse(BaseModel):
    # Response-only: every field below is always sent, so a default must not
    # publish it as optional. See HealthSnapshotResponse for the full reason.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)
    id: str
    name: str | None = None
    variant: str | None = None
    image_url: str | None = None
    versions: list = Field(default_factory=list)
    active_version: str | None = None
    # Anam renderStyle: realistic | animated_3d | illustrated | unknown
    render_style: str | None = None


class AvatarsResponse(BaseModel):
    # Response-only: every field below is always sent, so a default must not
    # publish it as optional. See HealthSnapshotResponse for the full reason.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)
    avatars: list[AvatarItemResponse] = Field(default_factory=list)
    # Distinct Anam activeVersion values in the full (unfiltered) gallery —
    # drives the Cara version dropdown. Built before active_version /
    # render_style filters so options stay stable while filtering.
    active_versions: list[str] = Field(default_factory=list)
    # Distinct render styles — drives the style dropdown (same stability rule).
    render_styles: list[str] = Field(default_factory=list)
    # Pagination over the filtered list (dashboard infinite scroll).
    has_more: bool = False
    # True when this gallery came from the workspace's own Anam key rather than
    # Talqing's, so the picker can say whose account it is showing — and so a
    # roster that looks unfamiliar is diagnosable rather than mysterious.
    workspace_key: bool = False


class VoiceItemResponse(BaseModel):
    # Response-only: every field below is always sent, so a default must not
    # publish it as optional. See HealthSnapshotResponse for the full reason.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)
    # Attributes are populated per provider only where that provider exposes them
    # (gender/accent/language/tier/image/country: ElevenLabs;
    # language/gender: xAI; language/locale/accent/image/sample: Deepgram;
    # gender only on static lists: Sarvam/OpenAI). The picker shows only what's present.
    id: str
    name: str | None = None
    provider: str | None = None  # the catalog provider this voice came from
    gender: str | None = None
    language: str | None = None  # e.g. "en", "hi" (xAI/ElevenLabs)
    accent: str | None = None  # free-form, e.g. "marathi", "american" (ElevenLabs)
    country: str | None = None  # ISO 3166-1 alpha-2 (ElevenLabs locale region)
    locale: str | None = None  # e.g. "hi-IN" (ElevenLabs)
    model: str | None = None  # e.g. aura-2 / aura-1 for Deepgram voices
    description: str | None = None
    sample_url: str | None = None
    image_url: str | None = None  # voice avatar (ElevenLabs library; often empty)
    tier: str | None = None  # ElevenLabs category label: Professional / Studio quality / Famous
    owner_id: str | None = None  # ElevenLabs public_owner_id required to save shared voices
    tags: list = Field(default_factory=list)


class VoiceSettingsResponse(BaseModel):
    """One ElevenLabs voice's own settings, as its author tuned them.

    Read when a voice is picked and copied onto the agent, because ElevenLabs
    has no way to inherit them at synthesis time — see `TTSModelSpec.stability`.
    Only the two knobs the models we carry actually read; `style` and
    `use_speaker_boost` are measured no-ops and `speed` is Talqing's own control.
    """

    stability: float | None = None
    similarity_boost: float | None = None


class LanguageOption(BaseModel):
    code: str
    name: str


class VoiceOptionResponse(BaseModel):
    id: str
    name: str | None = None
    gender: str | None = None
    description: str | None = None
    sample_url: str | None = None
    tags: list[str] = Field(default_factory=list)


class ProviderEntryResponse(BaseModel):
    # Response-only: every field below is always sent, so a default must not
    # publish it as optional. See HealthSnapshotResponse for the full reason.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)
    label: str
    logo_url: str
    enabled: bool = True
    # "list": this provider's models are in the `llm`/`stt`/… arrays of this same
    # response, and the editor renders a dropdown. "search": they are not, and
    # the editor searches `GET /catalog/models` instead. This is the field to
    # branch on — never the emptiness of the arrays, which is also what a
    # provider with nothing for this channel looks like.
    browse: Literal["list", "search"] = "list"
    # Which kinds a "search" provider searches — `GET /catalog/models?kind=`.
    # Empty on a "list" provider.
    searched_kinds: list[Literal["llm", "stt"]] = Field(default_factory=list)


class BaseCatalogEntryResponse(BaseModel):
    # extra: a catalog entry carries per-stage fields this base does not name.
    # json_schema_serialization_defaults_required: response-only, and every
    # field below is always sent, so a default must not publish it as optional.
    # See HealthSnapshotResponse for the full reason.
    model_config = ConfigDict(extra="allow", json_schema_serialization_defaults_required=True)

    provider: str
    channel: list[str] = Field(default_factory=list)
    model: str
    label: str | None = None
    # A caveat the agent's author has to act on, shown under the model in the
    # editor — the same field `CatalogEntry` carries. It reaches the editor only
    # because it is projected here.
    note: str | None = None
    # The model maker's logo where it differs from the provider's; null means use
    # the provider's.
    logo_url: str | None = None
    pricing: dict[str, Any] = Field(default_factory=dict)


class LLMCatalogEntryResponse(BaseCatalogEntryResponse):
    # Response-only: every field below is always sent, so a default must not
    # publish it as optional. See HealthSnapshotResponse for the full reason.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)
    base_url: str | None = None
    # The editor renders one toggle per entry, and its options from each option's
    # declared type — which is what makes a new provider a catalog edit rather
    # than a frontend change.
    builtin_tools: list[BuiltinTool] = Field(default_factory=list)
    # The thinking settings this model offers, fastest first — the editor renders
    # them in this order and treats the first as the default. Empty means the
    # model has no such setting and the editor shows no control for it.
    reasoning_efforts: list[ReasoningEffort] = Field(default_factory=list)
    # The low-latency lane this model sells, or null when it sells none (the
    # editor then shows no control). Carries its own rates so the cost estimate
    # can price the toggle before the author commits to it.
    priority: PriorityTier | None = None
    # Whether an agent on this model can be sent an image. The attach control is
    # hidden when it is false, and the editor notes it under the model so the
    # author learns it there rather than from a caller.
    vision: bool = False
    # Context window in tokens, where the provider publishes one. Null on the
    # entries in `catalog.yaml`, which do not carry it; the searched registry
    # does, and the model picker shows it beside the price.
    context_length: int | None = None


class STTCatalogEntryResponse(BaseCatalogEntryResponse):
    # Response-only: every field below is always sent, so a default must not
    # publish it as optional. See HealthSnapshotResponse for the full reason.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)
    default_language: str | None = None
    languages: list[LanguageOption] = Field(default_factory=list)
    # The editor greys out Auto when a selected model carries this.
    language_required: bool = False
    # The editor flags batch models: they add the transcription round-trip to
    # response latency and are estimated against speech seconds, not wall clock.
    streaming: bool = True


class TTSCatalogEntryResponse(BaseCatalogEntryResponse):
    # Response-only: every field below is always sent, so a default must not
    # publish it as optional. See HealthSnapshotResponse for the full reason.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)
    default_language: str | None = None
    languages: list[LanguageOption] = Field(default_factory=list)
    # The editor greys out Auto when a selected model carries this.
    language_required: bool = False
    default_voice: str | None = None
    # Whether picking a voice should carry that voice's own settings onto the
    # agent, and whether the ones already there survive a model switch. False
    # everywhere but ElevenLabs.
    supports_voice_settings: bool = False
    supports_speed: bool = False
    speed_min: float = 0.5
    speed_max: float = 2.0
    voices: list[VoiceOptionResponse] = Field(default_factory=list)
    # Null when this model speaks no delivery tags, which is what the editor
    # reads to decide whether the toggle is available at all.
    expressive: ExpressiveDialect | None = None


class RealtimeCatalogEntryResponse(BaseCatalogEntryResponse):
    # Response-only: every field below is always sent, so a default must not
    # publish it as optional. See HealthSnapshotResponse for the full reason.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)
    default_language: str | None = None
    languages: list[LanguageOption] = Field(default_factory=list)
    # The editor greys out Auto when a selected model carries this.
    language_required: bool = False
    default_voice: str | None = None
    supports_speed: bool = False
    speed_min: float = 0.5
    speed_max: float = 2.0
    voices: list[VoiceOptionResponse] = Field(default_factory=list)
    # See LLMCatalogEntryResponse.vision. False on both grok-voice entries, which
    # is the reason this is a served field rather than an assumption.
    vision: bool = False
    # False on a model that cannot be handed off from (gpt-live-1). Served so the
    # editor can say so where the author picks the model, instead of only at save.
    supports_instruction_update: bool = True


class AvatarCatalogEntryResponse(BaseCatalogEntryResponse):
    # Response-only: every field below is always sent, so a default must not
    # publish it as optional. See HealthSnapshotResponse for the full reason.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)
    pass


class NoiseCancellationCatalogEntryResponse(BaseCatalogEntryResponse):
    # Response-only: every field below is always sent, so a default must not
    # publish it as optional. See HealthSnapshotResponse for the full reason.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)
    # The editor shows this next to the model name; the three models differ in
    # which caller they suit, not in a number anyone can compare.
    description: str


class SystemVarResponse(BaseModel):
    """One `{{system_vars.*}}` variable the platform fills in."""

    key: str
    description: str
    example: str
    # The three phone-call fields. A `text` or `video` agent never takes a call,
    # so the editor lists them only where they can resolve; the clock fields
    # resolve on every channel.
    voice_only: bool


class CatalogResponse(BaseModel):
    # Response-only: every field below is always sent, so a default must not
    # publish it as optional. See HealthSnapshotResponse for the full reason.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)
    providers: dict[str, ProviderEntryResponse] = Field(default_factory=dict)
    llm: list[LLMCatalogEntryResponse]
    stt: list[STTCatalogEntryResponse]
    tts: list[TTSCatalogEntryResponse]
    realtime: list[RealtimeCatalogEntryResponse] = Field(default_factory=list)
    avatar: list[AvatarCatalogEntryResponse] = Field(default_factory=list)
    noise_cancellation: list[NoiseCancellationCatalogEntryResponse] = Field(default_factory=list)
    # Every language an agent may be set to — the union across the entries above,
    # deduped and named. Served rather than derived in the browser so the editor
    # and the validator cannot disagree about what a language is.
    languages: list[LanguageOption] = Field(default_factory=list)
    # The subset of the above that LiveKit's turn detector can judge — the
    # editor greys out that mode for anything else.
    turn_detector_languages: list[LanguageOption] = Field(default_factory=list)
    platform_fee_per_minute: PlatformFeePerMinute
    platform_fee_per_message: PlatformFeePerMessage
    cost_estimate_usage_per_minute: CostEstimateUsagePerMinute
    cost_estimate_usage_per_text_message: CostEstimateUsagePerTextMessage
    # The closed `{{system_vars.*}}` list the platform fills in. Served for the
    # same reason again: the editor's variable list, the publish-time validator
    # and the CoPilot must agree on what a valid key is. There is no entry here
    # for `{{vars.*}}` — those are the tenant's own, and come off the agent.
    system_vars: list[SystemVarResponse] = Field(default_factory=list)


class LLMModelSearchResponse(BaseModel):
    """One page of a searched provider's language models — never the whole
    registry.

    Ordered by real-world popularity when `q` is empty (OpenRouter ranks its own
    list by tokens processed, so the first screen is the models people actually
    run) and by match quality when it is not, popularity breaking the ties.
    """

    # Response-only: every field below is always sent, so a default must not
    # publish it as optional. See HealthSnapshotResponse for the full reason.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)
    kind: Literal["llm"] = "llm"
    models: list[LLMCatalogEntryResponse] = Field(default_factory=list)
    # Pass back as `cursor` for the next page; null when this is the last one.
    next_cursor: str | None = None


class STTModelSearchResponse(BaseModel):
    """One page of a searched provider's speech-to-text models, ordered as the
    provider lists them."""

    # Response-only: every field below is always sent, so a default must not
    # publish it as optional. See HealthSnapshotResponse for the full reason.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)
    kind: Literal["stt"] = "stt"
    models: list[STTCatalogEntryResponse] = Field(default_factory=list)
    # Pass back as `cursor` for the next page; null when this is the last one.
    next_cursor: str | None = None


# One shape per kind, told apart by `kind` — the same split `CatalogResponse`
# makes between its `llm` and `stt` arrays.
ModelSearchResponse = Annotated[
    LLMModelSearchResponse | STTModelSearchResponse, Field(discriminator="kind")
]


class ModelHostResponse(BaseModel):
    """One endpoint that can serve an OpenRouter language model."""

    # Response-only: every field below is always sent, so a default must not
    # publish it as optional. See HealthSnapshotResponse for the full reason.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)
    host: str  # the endpoint tag, e.g. `deepinfra/turbo`: a value for `llm.hosts`
    name: str  # e.g. "DeepInfra"
    logo_url: str | None
    quantization: str | None  # null when OpenRouter does not say
    pricing: LLMPricing
    context_length: int | None
    latency_p50_ms: int | None  # null without a workspace OpenRouter key
    throughput_p50: int | None  # tokens/s; null without a workspace OpenRouter key
    uptime_30m: float | None  # percent
    # A bare host slug that also matches this host's other endpoints (`azure`
    # beside `azure/us` and `azure/eu`).
    all_endpoints: bool


class ModelHostsResponse(BaseModel):
    # Response-only: every field below is always sent, so a default must not
    # publish it as optional. See HealthSnapshotResponse for the full reason.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)
    model: str
    hosts: list[ModelHostResponse]


class AddVoiceRequest(BaseModel):
    voice_id: str
    owner_id: str
    name: str | None = None


class AddVoiceResponse(BaseModel):
    voice_id: str


class VoicesResponse(BaseModel):
    # Response-only: every field below is always sent, so a default must not
    # publish it as optional. See HealthSnapshotResponse for the full reason.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)
    voices: list[VoiceItemResponse] = Field(default_factory=list)
    # filter options for the picker, provider-dependent (empty ⇒ no dropdown):
    languages: list[LanguageOption] = Field(default_factory=list)  # language dropdown
    accents: list[str] = Field(default_factory=list)  # accent dropdown (optional)
    genders: list[str] = Field(default_factory=list)  # gender dropdown (optional)
    # pagination (server-side for the ElevenLabs library; single-page otherwise):
    has_more: bool = False
    total_count: int | None = None
    page: int = 0
    # True when this listing came from the workspace's own provider key rather
    # than Talqing's — which is also when voices private to that account (an
    # ElevenLabs or Soniox clone) are in it. Always false for the static-enum
    # providers, which call no provider API at all.
    workspace_key: bool = False
