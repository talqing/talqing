"""Session usage metering and priced line shapes."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer

from services.catalog import (
    AvatarPricing,
    LLMPricing,
    Pricing,
    RealtimePricing,
    STTPricing,
    TTSPricing,
)

# ───────────────────── usage + billing (metering) ──────────────────────


class LLMUsage(BaseModel):
    """Billable LLM totals for one provider/model on a session.

    Only the quantities we price are stored. LiveKit may report modality
    breakdowns (audio/image/text splits, session_duration); those are not
    billed separately today and are not persisted.
    """

    # Response-only: every field below is always sent, so a default must not
    # publish it as optional. See HealthSnapshotResponse for the full reason.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    provider: str
    model: str
    input_tokens: int = 0
    input_cached_tokens: int = 0
    # Input tokens spent WRITING the prompt cache rather than reading it. Charged
    # at a premium where it is charged at all — 1.25x input on Anthropic and from
    # OpenAI's GPT-5.6 family onward — and a provider reports it only where it
    # bills for it, which is what makes a metered write with no rate a catalog
    # omission rather than a free model. `warm_prompt_cache` makes one on purpose
    # at the start of every voice call, so this is not a rare column.
    input_cache_write_tokens: int = 0
    output_tokens: int = 0
    # What the provider itself said this cost, for the ones that report it.
    # OpenRouter does, per request, and it is the only honest number there: it
    # routes each call to one of ~106 upstream hosts at that host's price, while
    # the rate card names the cheapest endpoint's — two consecutive requests for
    # one model measured 2.4x apart. Set means `price_llm_usage` bills this
    # figure and skips the rate block entirely; None on every other provider,
    # which prices from the catalog as it always has.
    reported_cost: Decimal | None = None
    # 'conversation' (the call itself) or 'analysis' (the one call that read the
    # finished transcript). Priced identically; kept apart so the breakdown can
    # say which is which.
    purpose: str = "conversation"
    # Whether this model ran in the provider's priority lane, which prices at a
    # different rate block on the same catalog entry. Taken from the agent's
    # published config, not from the response: the tier that actually served a
    # turn is dropped before anything we meter (LiveKit's LLMModelUsage keys on
    # provider/model alone), and two of three providers would report it while the
    # third never does. So this says what the agent ASKED for.
    priority: bool = False

    @field_serializer("reported_cost")
    def _ser_reported_cost(self, v: Decimal | None) -> float | None:
        # Same rule as every money field on a priced line: exact in Python and in
        # the NUMERIC column, a plain number on every JSON surface.
        return None if v is None else float(v)


class TTSUsage(BaseModel):
    # Response-only: every field below is always sent, so a default must not
    # publish it as optional. See HealthSnapshotResponse for the full reason.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)
    provider: str
    model: str
    characters_count: int = 0
    audio_duration: float = 0.0
    # LiveKit also reports token counts for token-billed TTS providers.
    input_tokens: int = 0
    output_tokens: int = 0


class STTUsage(BaseModel):
    # Response-only: every field below is always sent, so a default must not
    # publish it as optional. See HealthSnapshotResponse for the full reason.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)
    provider: str
    model: str
    audio_duration: float = 0.0
    # LiveKit also reports token counts for token-billed STT providers.
    input_tokens: int = 0
    output_tokens: int = 0
    # What the provider itself charged, for the ones that report it — see
    # `LLMUsage.reported_cost`. OpenRouter returns it on every transcription.
    reported_cost: Decimal | None = None


class RealtimeUsage(BaseModel):
    """Billable speech-to-speech totals for one provider/model on a session.

    The counterpart to LLMUsage, and the reason it is a separate shape: a
    realtime model prices each modality differently, so the splits LLMUsage
    discards are exactly what has to be stored here. `session_seconds` is the
    realtime socket's connection time, for the providers that bill the audio
    stream by the minute rather than by the token.
    """

    # Response-only: every field below is always sent, so a default must not
    # publish it as optional. See HealthSnapshotResponse for the full reason.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    provider: str
    model: str
    input_text_tokens: int = 0
    input_cached_text_tokens: int = 0
    input_audio_tokens: int = 0
    input_cached_audio_tokens: int = 0
    output_text_tokens: int = 0
    output_audio_tokens: int = 0
    session_seconds: float = 0.0


class AvatarUsage(BaseModel):
    """One avatar session's billable window: WALL-CLOCK seconds alive, idle
    included (Anam's billing rule). Metered from our own job
    timing — Anam has no usage webhooks; `avatar_session_id` (Anam's sessionId)
    is stored for reconciliation against any future Anam usage export."""

    # Response-only: every field below is always sent, so a default must not
    # publish it as optional. See HealthSnapshotResponse for the full reason.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    provider: str
    model: str
    seconds: float = 0.0
    avatar_id: str | None = None
    avatar_session_id: str | None = None


class SessionUsage(BaseModel):
    """Aggregate per-(provider, model) usage for one session."""

    # Response-only: every field below is always sent, so a default must not
    # publish it as optional. See HealthSnapshotResponse for the full reason.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    # order mirrors PriceBreakdown's per-kind fields
    llm: list[LLMUsage] = Field(default_factory=list)
    stt: list[STTUsage] = Field(default_factory=list)
    tts: list[TTSUsage] = Field(default_factory=list)
    realtime: list[RealtimeUsage] = Field(default_factory=list)
    avatar: list[AvatarUsage] = Field(default_factory=list)


# Money note: every Decimal money field below (cost on a line; the three totals
# on the breakdown) is exact — the arithmetic in pricing.py never touches float —
# and binds directly to the NUMERIC DB columns. The field_serializers emit plain
# floats so every JSON surface (pricing_snapshot, the session.completed webhook, the API
# responses) stays numeric, not stringified.
#
# Quantity fields reuse the *Usage vocabulary (input_cached_tokens /
# characters_count / audio_duration) so a line reads the same as the usage it
# came from. The one exception is uncached_input_tokens: pricing splits an LLM's
# input into the uncached remainder (full rate) and input_cached_tokens (cheaper
# rate), so it is NOT the same quantity as LLMUsage.input_tokens (the total) and
# is named distinctly to say so.
class PriceLine(BaseModel):
    """One priced provider/model line. Subclassed per kind so each line carries
    only its own quantity fields and a `rates` block of the matching shape.
    Grouped per kind on PriceBreakdown (llm/stt/tts/realtime/avatar, mirroring
    SessionUsage), so each list is concretely typed and dumps its kind-specific
    fields directly. The `kind` Literal stays as a self-describing tag on the
    serialized line."""

    # Response-only: every field below is always sent, so a default must not
    # publish it as optional. See HealthSnapshotResponse for the full reason.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    provider: str
    model: str
    cost: Decimal
    rates: Pricing | None = None

    @field_serializer("cost")
    def _ser_cost(self, v: Decimal) -> float:
        return float(v)


class LLMPriceLine(PriceLine):
    # Response-only: every field below is always sent, so a default must not
    # publish it as optional. See HealthSnapshotResponse for the full reason.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)
    kind: Literal["llm"] = "llm"
    uncached_input_tokens: int | None = None  # input minus cached and cache-write
    input_cached_tokens: int | None = None
    input_cache_write_tokens: int | None = None
    output_tokens: int | None = None
    # True when `cost` is what the provider charged for these requests rather
    # than this file's arithmetic over `rates` — which is also why `rates` is
    # null on such a line. A tenant reconciling a bill needs to know which of the
    # two they are reading, and only one of them can be checked against their own
    # provider dashboard.
    provider_reported: bool = False
    # What this LLM spend was for. Post-call analysis prices through the same
    # path as the conversation, so without this its cost would fold invisibly
    # into the call's own LLM line and a tenant could not see what enabling
    # analysis costs them.
    purpose: str = "conversation"
    # Whether `rates` below are the priority lane's rather than the standard
    # ones. The rates alone would show the higher numbers without saying why, and
    # a tenant looking at a doubled LLM line deserves the reason on the line.
    priority: bool = False
    rates: LLMPricing | None = None


class STTPriceLine(PriceLine):
    # Response-only: every field below is always sent, so a default must not
    # publish it as optional. See HealthSnapshotResponse for the full reason.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)
    kind: Literal["stt"] = "stt"
    audio_duration: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    # See `LLMPriceLine.provider_reported`.
    provider_reported: bool = False
    rates: STTPricing | None = None


class TTSPriceLine(PriceLine):
    # Response-only: every field below is always sent, so a default must not
    # publish it as optional. See HealthSnapshotResponse for the full reason.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)
    kind: Literal["tts"] = "tts"
    characters_count: int | None = None
    audio_duration: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    rates: TTSPricing | None = None


class RealtimePriceLine(PriceLine):
    # Response-only: every field below is always sent, so a default must not
    # publish it as optional. See HealthSnapshotResponse for the full reason.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)
    kind: Literal["realtime"] = "realtime"
    # Uncached remainders, same split as LLMPriceLine.uncached_input_tokens:
    # the cached halves bill at the cheaper cached rate.
    uncached_input_text_tokens: int | None = None
    input_cached_text_tokens: int | None = None
    uncached_input_audio_tokens: int | None = None
    input_cached_audio_tokens: int | None = None
    output_text_tokens: int | None = None
    output_audio_tokens: int | None = None
    session_seconds: float | None = None
    rates: RealtimePricing | None = None


class AvatarPriceLine(PriceLine):
    # Response-only: every field below is always sent, so a default must not
    # publish it as optional. See HealthSnapshotResponse for the full reason.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)
    kind: Literal["avatar"] = "avatar"
    seconds: float | None = None  # wall-clock seconds
    rates: AvatarPricing | None = None


class PriceBreakdown(BaseModel):
    # Response-only: every field below is always sent, so a default must not
    # publish it as optional. See HealthSnapshotResponse for the full reason.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)
    provider_cost: Decimal
    platform_fee: Decimal
    total_charge: Decimal
    duration_s: int | None
    # The rate this session's platform fee was charged at. A call carries the
    # per-minute one, a chat the per-message one and how many it answered.
    # Defaulted because snapshots written before chats were priced carry neither.
    platform_fee_per_minute: float | None
    platform_fee_per_message: float | None = None
    answered_messages: int | None = None
    # priced lines grouped by kind (mirrors SessionUsage); each list is
    # concretely typed so it dumps its kind-specific fields directly
    llm: list[LLMPriceLine] = Field(default_factory=list)
    stt: list[STTPriceLine] = Field(default_factory=list)
    tts: list[TTSPriceLine] = Field(default_factory=list)
    realtime: list[RealtimePriceLine] = Field(default_factory=list)
    avatar: list[AvatarPriceLine] = Field(default_factory=list)

    @field_serializer("provider_cost", "platform_fee", "total_charge")
    def _ser_money(self, v: Decimal) -> float:
        return float(v)
