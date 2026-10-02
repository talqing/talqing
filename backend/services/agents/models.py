"""Agent domain structs and service request/response shapes.

Pure helpers live next to their call sites:
- Avatar / handoff media: ``services.agents.media``
- Row mappers / CRUD: ``services.agents.service``
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from services.catalog.models import ReasoningEffort
from services.conversation_context import ConversationContext
from services.faqs.models import InlineFaq
from services.system_vars import validate_iana_timezone, validate_var_name
from services.tools import OperationRequest, ToolDefinition, ToolName, ToolsNamespace

# ───────────────────────── agent definition ─────────────────────────
# The provider/model defaults below must name a real catalog entry (catalog.yaml);
# they're the stack a brand-new default agent gets. A catalog rename would orphan
# them — agent validation rejects such a config at write time.


# Each media slot is one `*ModelSpec` — a single provider/model selection with
# its settings — and the `*Spec` the agent actually holds adds the optional
# `fallback` the compiler fails over to when the primary provider errors at
# runtime (LiveKit's stt/llm/tts.FallbackAdapter).
#
# Fallback is off by default: with none set the compiler builds the bare plugin
# exactly as it always has. Depth is capped at one *structurally* — a fallback is
# a `*ModelSpec`, which has no `fallback` field to fill in. A chain would be more
# configuration than resilience, and every hop is another key to keep alive.


class STTModelSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str = "sarvam"
    model: str = "saaras:v4"


class STTSpec(STTModelSpec):
    model_config = ConfigDict(extra="forbid")

    fallback: STTModelSpec | None = None


# Not an artifact the tenant authors and publishes, unlike a Talqing tool — it
# is a capability of the chosen model, so it is versioned with the agent rather
# than pinned like a `ToolSelection`. The provider executes it and folds the
# result into its own reply; we never see the call. Agent validation rejects a
# `config` key the model's catalog entry does not declare, rather than dropping
# it on the way to the wire.
class BuiltinToolSpec(BaseModel):
    """A capability of the model itself, switched on for that model.

    `config` takes only the options that model's catalog entry declares.
    """

    model_config = ConfigDict(extra="forbid")

    type: str
    config: dict[str, Any] = Field(default_factory=dict)


class LLMModelSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str = "openai"
    # The cheapest OpenAI entry that holds a voice turn at effort "none". Only
    # what is CREATED from here on follows this — a stored agent names its own
    # model.
    model: str = "gpt-5.6-luna"
    # Per model, not per agent, and that is the point: the same tool name is a
    # different object at each vendor — xAI's code interpreter takes no fields
    # and OpenAI's requires a container — so a fallback carries its own rather
    # than inheriting a payload its provider would reject.
    builtin_tools: list[BuiltinToolSpec] = Field(default_factory=list)
    # Same reasoning: the accepted values differ per model (this one's catalog
    # entry lists them), so a fallback names its own or takes its own default.
    reasoning_effort: ReasoningEffort | None = Field(
        default=None,
        description=(
            "How long this model may think before it answers. Must be one of the values the "
            "model's catalog entry offers; unset means its fastest. On a voice call the "
            "caller hears every second of thinking as silence."
        ),
    )
    # Also per model, and for a third reason on top of the two above: support is
    # not even uniform within a vendor (OpenAI sells no lane on gpt-5.4-nano), so
    # a failover copying the primary's choice could ask for one that does not exist.
    priority: bool = Field(
        default=False,
        description=(
            "Run this model in its provider's priority (low-latency) lane, where its catalog "
            "entry declares one. Steadies the worst case rather than the average, and costs "
            "1.75x-2.5x per token."
        ),
    )
    # Per model for the reason `priority` is: a host means something only for
    # its own model, so a fallback names its own set or none.
    hosts: list[str] | None = Field(
        default=None,
        description=(
            "OpenRouter only: the hosts allowed to serve this model, from list_model_hosts; "
            "the fastest of them answers. Unset lets OpenRouter use any host."
        ),
    )


class LLMSpec(LLMModelSpec):
    model_config = ConfigDict(extra="forbid")

    fallback: LLMModelSpec | None = None


class TTSModelSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str = "sarvam"
    model: str = "bulbul:v3"
    # Unset takes the model's catalog `default_voice`, written in on save
    # (`pin_default_voices`) — a literal here would be one provider's voice id on
    # every other provider's model.
    voice: str | None = None
    voice_name: str | None = None  # cosmetic label for the editor (ignored at compile)
    speed: float = Field(default=1.0, ge=0.5, le=2.0)
    # The picked ElevenLabs voice's OWN settings, copied onto the agent when the
    # voice is chosen. Only for models whose catalog entry sets
    # `supports_voice_settings`; validation refuses them anywhere else.
    #
    # They are stored rather than looked up at synthesis time because ElevenLabs
    # gives no way to inherit them selectively: MEASURED 2026-08-19, sending a
    # `voice_settings` object at all resets every key it omits to ElevenLabs'
    # GLOBAL default (stability 0.5 / similarity_boost 0.75), and there is no
    # per-field fallback to the voice. Since a speed always goes on the wire,
    # leaving these unset does not get the voice's tuning — it silently flattens
    # it, and library voices are tuned hard (stability runs 0.0 to 1.0 across the
    # gallery). So the numbers travel with the agent, where they are versioned
    # and show up in the publish diff.
    #
    # Unset is still legal and means exactly ElevenLabs' global default: that is
    # what an agent created through the API without them gets, and what every
    # agent got before this existed.
    stability: float | None = Field(default=None, ge=0.0, le=1.0)
    similarity_boost: float | None = Field(default=None, ge=0.0, le=1.0)
    # On the model spec rather than on TTSSpec, for the reason `reasoning_effort`
    # and `priority` are: support differs per model, so a fallback has to say for
    # itself. Only the primary's flag reaches the prompt, which teaches one
    # dialect once at compile time; the fallback's is read by validation, which
    # requires the pair to agree so a failover cannot land tagged text on a voice
    # that would read the tags aloud.
    expressive: bool = Field(
        default=False,
        description=(
            "Let the model write delivery tags - a laugh, a whisper, a pause before the key "
            "detail - into what this voice speaks. Only for models whose catalog entry "
            "declares an `expressive` dialect; GET /catalog reports it per model."
        ),
    )


class TTSSpec(TTSModelSpec):
    model_config = ConfigDict(extra="forbid")

    fallback: TTSModelSpec | None = None


# No `fallback` twin, unlike the three specs above: LiveKit's
# ``llm.FallbackAdapter`` wraps ``llm.LLM``, and a realtime model is an
# ``llm.RealtimeModel`` — there is no adapter to fail one over to another.
class RealtimeSpec(BaseModel):
    """A speech-to-speech model, which replaces stt + llm + tts together."""

    model_config = ConfigDict(extra="forbid")

    provider: str = "openai"
    model: str = "gpt-realtime-2.1"
    # Unset takes the catalog default, as on `TTSModelSpec`.
    voice: str | None = None
    voice_name: str | None = None  # cosmetic label for the editor (ignored at compile)
    speed: float = Field(default=1.0, ge=0.5, le=2.0)


# One number, which ``compiler.factories.endpointing_windows`` turns into the
# windows the STT provider, the local voice detector and LiveKit each get.
# Nothing is shared between them: LiveKit's is a deadline measured from the
# moment speech stopped, so it is always the whole number, and what changes per
# mode is only who else is allowed to hold the turn open past it.
class EndpointingSpec(BaseModel):
    """How long a caller's silence must last before the agent takes the turn."""

    model_config = ConfigDict(extra="forbid")

    min_silence_duration: float = Field(
        default=0.5,
        # 0.25 is not arbitrary: LiveKit triggers the audio turn detector off
        # accumulated VAD silence and refuses to start a session whose VAD could
        # never reach MIN_SILENCE_DURATION_MS + 50ms. On the one pipeline that
        # runs that detector the VAD carries this number, so the floor here is
        # what keeps the session startable.
        ge=0.25,
        description=(
            "How long the caller must stay silent, in seconds, before the agent takes the "
            "turn. A streaming speech-to-text model may wait longer when it hears the caller "
            "is not finished."
        ),
    )
    max_silence_duration: float = Field(
        default=2.5,
        ge=0,
        description=(
            "How long to wait instead when the turn detector judges the caller is mid-thought. "
            "Only a batch speech-to-text model in one of the turn detector's 14 languages gets "
            "that judgement; every other pipeline ends turns on silence and ignores this."
        ),
    )


# Talqing uses LiveKit's VAD interruption mode. The LiveKit mode key is omitted
# because it is not user-configurable here.
class InterruptionSpec(BaseModel):
    """Whether the caller can cut the agent off mid-sentence, and how easily."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(default=True, description="Whether interruptions are enabled.")
    discard_audio_if_uninterruptible: bool = Field(
        default=True,
        description="Drop buffered audio while the agent speaks and cannot be interrupted.",
    )
    min_speech_duration: float = Field(
        default=0.5,
        ge=0,
        description="Minimum speech length in seconds to register as an interruption.",
    )
    min_words: int = Field(
        default=0,
        ge=0,
        description="Minimum word count to consider an interruption. Applies to STT.",
    )
    resume_false_interruption: bool = Field(
        default=True,
        description="Resume the agent's speech after a false interruption.",
    )
    false_interruption_timeout: float | None = Field(
        default=2.0,
        ge=0,
        description="Seconds of silence after an interruption before it is classified as false. None disables false-interruption classification.",
    )


class PreemptiveGenerationSpec(BaseModel):
    """Start drafting a reply before the caller has certainly finished, to cut
    the pause before the agent speaks."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(default=False, description="Whether preemptive generation is enabled.")
    preemptive_tts: bool = Field(
        default=False,
        description="Whether to also run TTS preemptively before the turn is confirmed.",
    )
    max_speech_duration: float = Field(
        default=10.0,
        ge=0,
        description="Maximum user speech duration in seconds for which preemptive generation is attempted.",
    )
    max_retries: int = Field(
        default=3,
        ge=0,
        description="Maximum number of preemptive generation attempts per user turn.",
    )


# There is no `turn_detection` knob. Which detector runs is not a preference —
# it is dictated by the pipeline, so `compiler.factories.resolve_turn_detection`
# derives it from the speech-to-text model and the agent's language. Storing a
# choice as well would be a second source of truth able to disagree with the
# models it describes, the same reason `realtime` alone discriminates the
# pipeline (see `AgentConfig`).
class TurnHandlingSpec(BaseModel):
    """How the agent decides the caller has finished, and what it does about it."""

    model_config = ConfigDict(extra="forbid")

    endpointing: EndpointingSpec = Field(
        default_factory=EndpointingSpec,
        description="Endpointing configuration.",
    )
    interruption: InterruptionSpec = Field(
        default_factory=InterruptionSpec,
        description="Interruption handling configuration.",
    )
    preemptive_generation: PreemptiveGenerationSpec = Field(
        default_factory=PreemptiveGenerationSpec,
        description="Preemptive generation configuration.",
    )


# `model` is the catalog billing SKU (anam/anam). Cara generations
# (cara-3 / cara-4 / …) are gallery filters on GET /catalog/avatars (Anam
# activeVersion), not separate priced models. Runtime omits avatar_model on the
# Anam session so Anam uses the face's server-side default (activeVersion).
class AvatarSpec(BaseModel):
    """The Anam video layer, active when `channel == "video"`.

    `avatar_id` is a face from GET /catalog/avatars.
    """

    model_config = ConfigDict(extra="forbid")

    provider: str = "anam"
    model: str = "anam"
    avatar_id: str | None = "edf6fdcb-acab-44b8-b974-ded72665ee26"
    name: str | None = "Mia"  # persona display name (cosmetic)


class BackgroundAudioSpec(BaseModel):
    """Ambient + thinking sounds mixed into the agent's output (voice realism)."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    # closed sets we own (mapped to LiveKit BuiltinAudioClips in workers/voice/main.py)
    ambient: Literal["office", "city", "forest", "crowd", "hold_music"] | None = "office"
    ambient_volume: float = 0.4
    thinking: Literal["keyboard", "keyboard2"] | None = "keyboard"
    thinking_volume: float = 0.6


# Applied at the front of the input chain, so the speech-to-text model, the VAD
# and the recording all receive the enhanced audio. Off by default: enhancement
# is lossy, and the strength that rescues a call from a noisy street can hurt
# one from a quiet room, so it is never turned on for an existing agent by
# anything other than an explicit edit. The provider needs a BYOK key like any
# other, which `AgentConfig.required_providers` demands only while it is on.
class NoiseCancellationSpec(BaseModel):
    """Speech enhancement on the caller's inbound audio (voice and video).

    Off by default. `provider`/`model` name a `noise_cancellation` catalog entry.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(
        default=False,
        description="Clean up the caller's audio before the agent hears it. Voice and video only.",
    )
    provider: str = "aicoustics"
    # The cheapest model we offer, deliberately. The enhancer runs in the agent
    # process, so its CPU is ours while the licence is the tenant's — a default
    # that is wrong costs us pod density on every call that enables it. Voice
    # Focus L is four times this on CPU and neither ai-coustics
    # nor LiveKit publishes evidence that it is more accurate.
    model: str = "quail_vf_s"
    enhancement_level: float = Field(
        default=0.8,
        ge=0.0,
        le=1.0,
        description=(
            "How hard to suppress everything that is not the caller. 0.5 is conservative and "
            "always preserves the foreground speech, 0.8 gives the best word error rate on "
            "difficult audio, 1.0 suppresses interfering speech most aggressively."
        ),
    )


# No `retention_days` here on purpose: retention is one policy per workspace
# (`tenants.retention_days`), because a workspace has one data policy and it is
# the workspace that signs the DPA.
#
# On a call with handoffs the setting follows whichever agent is speaking:
# entering an agent with recording off pauses the recorder and writes silence
# for as long as it holds the call, so the file stays aligned with the
# transcript. It cannot be turned *on* part-way through, though — nothing is
# recording unless the agent that ANSWERED had it on — so publish warns about a
# handoff into a recording-on agent from one with it off
# (`services.agents.media.recording_handoff_warning`).
class RecordingSpec(BaseModel):
    """Whether calls with this agent are recorded, and what the caller is told.

    On by default: one stereo file per call, kept until the workspace's
    retention policy deletes it.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(
        default=True,
        description="Record the call audio. Voice and video agents only.",
    )
    consent: Literal["off", "disclosure"] = Field(
        default="off",
        description=(
            "Set to 'disclosure' to tell the caller the call is recorded. The greeting must "
            "then contain `{{consent.notice}}` to say where, and the agent gains a tool to "
            "stop recording if the caller objects."
        ),
    )
    # The compliance sentence itself. Its *position* is the greeting's business;
    # keeping the text here means one place to audit and to reword.
    consent_notice: str = Field(
        default="This call is recorded for quality and training purposes.",
        description="The sentence `{{consent.notice}}` resolves to in the greeting.",
    )


class VisionSourceSpec(BaseModel):
    """One live video source the agent watches while a call is running."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(
        default=False,
        description=(
            "Let the agent see this. On each turn it is shown the most recent frame, and only "
            "that one, so a long call does not get slower or more expensive over time. Each "
            "frame is charged as image input on your language model."
        ),
    )
    record: bool = Field(
        default=False,
        description=(
            "Record what was shared as a 1 fps video, so the call can be replayed with the "
            "screen in sync with the audio. Stored and purged exactly like the call recording, "
            "under this workspace's retention policy."
        ),
    )


# `vision` here and `vision` on a catalog entry are different questions about
# the same capability, and both names are right. A catalog entry's `vision` is
# what the *model* can do — read an image at all — a measured fact nobody
# configures. This is what the *agent* is pointed at, and it is a choice. The
# first is the precondition for the second, which is why publish refuses this on
# a model whose entry says `vision: false`.
#
# A container with one entry per source rather than a pair of flat fields, so
# the camera this deliberately does not ship is a new key in an existing object
# if a use case ever appears: additive, and no migration.
class VisionInputSpec(BaseModel):
    """What the agent watches live during a call, as opposed to images people send it.

    Web voice and video calls only, on a cascade pipeline, and only on a model
    whose catalog entry says `vision`. Enabling it on `text` is refused.
    """

    model_config = ConfigDict(extra="forbid")

    screenshare: VisionSourceSpec = Field(default_factory=VisionSourceSpec)


# Beside `vision_input` rather than inside `turn_handling`: both are "a second
# way input reaches the agent", and that is the sibling that already exists.
class KeypadInputSpec(BaseModel):
    """Digits the caller types, delivered as a turn the agent can answer.

    Phone and stream calls only, and only where the platform sends DTMF as an
    event — tones inside the audio are not detected. `voice` agents only:
    enabling it on `video` or `text` is refused.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(
        default=False,
        description=(
            "Let the caller answer with the keypad. Each entry arrives as one labelled user "
            "turn, and the first keypress interrupts whatever the agent is saying."
        ),
    )
    timeout: float = Field(
        default=2.0,
        ge=0,
        le=10,
        description=(
            "Seconds of quiet before an entry is taken as finished. 0 waits for the "
            "terminator however long it takes."
        ),
    )
    terminator: Literal["#", "*", ""] = Field(
        default="#",
        description=(
            "The key that ends an entry. It is not part of the entry, and pressing it on "
            "nothing is ignored. Empty means only the quiet timer ends one."
        ),
    )


# ElevenLabs' `voicemail_detection` system tool, as a config object rather than a
# tool entry: the model decides, by calling a generated `voicemail_detected` tool
# (`compiler/tools.py`), so a person who picks up hears the greeting with no
# detection delay. Offered only on calls the agent placed — nothing else can be
# answered by a machine — and `voice` agents only, like the keypad.
class VoicemailDetectionSpec(BaseModel):
    """Hang up on, or leave a message for, an outbound call that reaches voicemail."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(
        default=False,
        description=(
            "On calls the agent places, let it recognise a voicemail greeting and hang up. "
            "The call ends as `voicemail`."
        ),
    )
    message: str | None = Field(
        default=None,
        min_length=1,
        max_length=2000,
        description=(
            "Spoken word for word after the greeting, then the agent hangs up. Null hangs up "
            "without one. Supports the same substitutions as the greeting."
        ),
    )


# Session-wide, like `max_steps`: LiveKit's `user_away_timeout` is an
# `AgentSession` parameter, so the agent that answered sets it for the whole call
# and a handoff target's own is not read. The check-in is written by the agent's
# own model rather than being a fixed line, so it comes out in the language and
# the voice the conversation is already in.
class SilenceSpec(BaseModel):
    """What the agent does when the caller goes quiet (voice and video)."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(
        default=True,
        description=(
            "Check in on a caller who goes quiet, and hang up if they never answer. "
            "Voice and video only."
        ),
    )
    timeout: float = Field(
        default=10.0,
        ge=5,
        le=600,
        description="Seconds of silence after the agent stops talking before it checks in.",
    )
    max_check_ins: int = Field(
        default=2,
        ge=0,
        le=5,
        description=(
            "Check-ins before the agent says goodbye and hangs up. 0 hangs up at the first silence."
        ),
    )


# The built-in analysis outputs. They are columns on `sessions` and top-level
# keys in the webhook's `analysis` object, so a tenant field of the same name
# would be shadowed by one of them.
_RESERVED_ANALYSIS_NAMES = frozenset({"summary", "outcome", "outcome_rationale"})


class AnalysisField(BaseModel):
    """One value to pull out of the finished transcript."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        pattern=r"^[a-z][a-z0-9_]{0,39}$",
        description=(
            "Identifier for the extracted value, as it appears in the API and webhook. "
            "Lower-case letters, digits and underscores."
        ),
    )
    type: Literal["string", "boolean", "integer", "number"] = Field(
        default="string",
        description="The shape of the value. Anything that is not a number or a yes/no is a string.",
    )
    description: str = Field(
        min_length=1,
        description=(
            "How to find this value in the transcript. Be explicit about the format you want "
            "and about what to do when the call never mentions it."
        ),
    )


class OutcomeSpec(BaseModel):
    """What counts as this call having achieved its purpose."""

    model_config = ConfigDict(extra="forbid")

    # No default, deliberately. A success rate the author never defined is a
    # number they cannot act on, so there is no inferring one from the prompt.
    prompt: str = Field(
        min_length=1,
        description=(
            "What a successful call looks like for this agent. Each call is judged against "
            "this and comes back as success, failure, or unknown, with the reasoning."
        ),
    )


class AnalysisSpec(BaseModel):
    """What to work out about a call or a chat once it has ended.

    One LLM call per session reads the finished transcript.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(
        default=True,
        description=(
            "Analyse calls after they end. This runs one extra LLM call per call, "
            "billed like any other, and adds roughly 5% to the cost of a call."
        ),
    )
    model: LLMModelSpec | None = Field(
        default=None,
        description=(
            "The model that reads the transcript. Leave unset to use the platform default, "
            "which is picked for accuracy rather than the low latency a live call needs."
        ),
    )
    summary: bool = Field(
        default=True,
        description="Write a short summary of each call, shown on the calls list.",
    )
    outcome: OutcomeSpec | None = Field(
        default=None,
        description=(
            "Judge whether each call achieved its purpose. Unset means calls are not judged "
            "and the outcome is left empty."
        ),
    )
    fields: list[AnalysisField] = Field(
        default_factory=list,
        max_length=25,
        description=(
            "Values to extract from the transcript. These are what a reader inferred "
            "afterwards, and are kept apart from the data the agent itself collected "
            "during the call."
        ),
    )

    @model_validator(mode="after")
    def unique_field_names(self) -> AnalysisSpec:
        """Field names address a value in the API, the webhook and the UI table,
        so a duplicate or a collision with a built-in output is not a preference
        — one of the two values would be unreachable."""
        seen: set[str] = set()
        for field in self.fields:
            if field.name in _RESERVED_ANALYSIS_NAMES:
                raise ValueError(
                    f"analysis field '{field.name}' uses a reserved name - "
                    f"pick another (reserved: {', '.join(sorted(_RESERVED_ANALYSIS_NAMES))})"
                )
            if field.name in seen:
                raise ValueError(f"analysis field '{field.name}' is defined twice")
            seen.add(field.name)
        return self

    def produces_nothing(self) -> bool:
        """Enabled but with every output switched off — an LLM call with no result."""
        return not self.summary and self.outcome is None and not self.fields


class ConversationSpec(BaseModel):
    """What a new call knows about earlier ones with the same person."""

    model_config = ConfigDict(extra="forbid")

    context: ConversationContext = Field(
        default="none",
        description=(
            "How much of the past comes into a new call: 'none' starts clean, 'summary' adds "
            "background on recent conversations, 'transcript' carries the same conversation on. "
            "Set on the agent that answers. A WhatsApp call always carries the chat on."
        ),
    )
    summary_limit: int | None = Field(
        default=None,
        ge=1,
        description=(
            "How many recent calls to summarize. Leave empty for all of them. Used by 'summary' "
            "only."
        ),
    )
    initialize_userdata: bool = Field(
        default=True,
        description=(
            "Start the call already knowing what the agent learned about this person on earlier "
            "ones. Used by 'summary' and 'transcript' only; a 'none' call always starts empty."
        ),
    )


# ───────────────────────── name it, or bring it ─────────────────────────────
# One selection grammar, used four ways: an agent, a tool, an MCP server, an
# FAQ. Each
# is `{<thing>_id, <thing>}` with exactly one side filled in — reference
# something the workspace stores, or send the whole definition in the request.
# Learned once, read the same wherever it appears. `AgentSelection` lives in
# `services.agents.plan` because it also carries the per-call override.


# The materialization asymmetry is deliberate: a stored agent must not carry a
# second-class tool with no editor, no test panel, no CoPilot and no reuse.
class InlineTool(BaseModel):
    """A tool defined here rather than named — the shape `create_tool` takes, with
    no id and no versions.

    On the call endpoints it runs for that call only; on the agent endpoints it
    is created and published as a real tool.
    """

    name: ToolName
    description: str = ""
    json_schema: dict[str, Any] = Field(default_factory=dict)
    operations: list[OperationRequest] = Field(default_factory=list)
    long_running_task: bool = False
    silent: bool = False
    disable_interruptions: bool = False

    model_config = ConfigDict(extra="forbid")


# ``tool_version`` is null in a draft and set in a frozen agent version: the
# draft names the tool, publishing records the tool *version*. That asymmetry is
# the whole point. A draft always tracks each tool's latest published version,
# so republishing a tool reaches the agent's next publish and nothing sooner; a
# published agent pins what it was published with, so a tool republish (or a
# rollback) never moves under a call that is already running.
#
# The pin lives here rather than in a column beside the frozen config because it
# qualifies *this attachment*: it then diffs, freezes and rolls back through the
# same machinery as every other field of the config. An inline ``tool`` has
# nothing to pin — it *is* the definition.
class ToolSelection(BaseModel):
    """A tool the agent may run: name one with `tool_id`, or define one inline."""

    tool_id: str | None = None
    tool_version: int | None = None
    tool: InlineTool | None = None

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _exactly_one_side(self) -> ToolSelection:
        if (self.tool_id is None) == (self.tool is None):
            raise ValueError("set either tool_id or tool, not both")
        if self.tool_version is not None and self.tool_id is None:
            raise ValueError("tool_version needs a tool_id - an inline tool has no versions")
        return self


# Exactly what the `custom_mcp` provider already stores in `mcp_config`, so it
# compiles through ``compiler.integrations`` unchanged. API-key-shaped only is a
# stated limitation rather than an oversight: an OAuth provider needs somewhere
# to keep a refresh token, and an inline definition has none.
class InlineMcpServer(BaseModel):
    """A remote MCP server defined here rather than named.

    API-key-shaped only — an OAuth server has to be an integration.
    `tools_namespace` prefixes every tool the model is shown: `crm` -> `crm_search`.
    """

    name: str = Field(min_length=1)  # label; also the toolset id
    url: str  # supports {{secrets.NAME}}
    headers: dict[str, str] = Field(default_factory=dict)  # values support {{secrets.NAME}}
    # None = every tool the server lists, matching `integrations.allowed_tools`.
    allowed_tools: list[str] | None = None
    # The prefix this server's tools are presented under, e.g. `crm` turns the
    # server's `search` into `crm_search`. Blank presents the server's own names
    # unchanged, which is what an inline server does unless the author says
    # otherwise. A plain string rather than `| None`: there is nothing to derive,
    # so "unset" and "no prefix" are the same answer.
    tools_namespace: ToolsNamespace = ""

    model_config = ConfigDict(extra="forbid")


# An object rather than a bare id for the same reason `ToolSelection` is one:
# what an attachment *is* stays stable while what an attachment *carries* does
# not, and widening an object is additive where changing an array element's type
# is not.
# Brought inline, it is a URL and headers that live for this call only —
# materialized into a `custom_mcp` integration on the agent endpoints, for the
# same reason an inline tool is.
class McpSelection(BaseModel):
    """An MCP server this agent may call tools on: name an integration, or define
    one inline."""

    integration_id: UUID | None = None
    mcp: InlineMcpServer | None = None

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _exactly_one_side(self) -> McpSelection:
        if (self.integration_id is None) == (self.mcp is None):
            raise ValueError("set either integration_id or mcp, not both")
        return self


# An object rather than a bare id for the reason `McpSelection` is one. Unlike a
# tool there is no version to pin: FAQ content is live, so publishing an agent
# freezes WHICH FAQs are attached and never what they say.
# Brought inline, it is created as a real FAQ on the agent and task endpoints
# and lives for one call on the call endpoints, exactly as an inline tool does.
class FaqSelection(BaseModel):
    """An FAQ the agent answers from: name one with `faq_id`, or define one inline."""

    faq_id: UUID | None = None
    faq: InlineFaq | None = None

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _exactly_one_side(self) -> FaqSelection:
        if (self.faq_id is None) == (self.faq is None):
            raise ValueError("set either faq_id or faq, not both")
        return self


# What the source agent is asked to write when an edge is on `summary`, used
# verbatim when `summary_prompt` is null. It is the `description` of the tool
# argument, so it is literally the prompt — and it sits in the source agent's
# tool schema on every request of the call, which is why it is one line and not
# three. The target is told not to make the caller repeat themselves by the
# provenance header the compiler builds, so this does not have to say it too.
DEFAULT_SUMMARY_PROMPT = (
    "A short summary of this conversation for {name}: what the caller wants, "
    "what you have already done, and what is still open."
)
# The tail under `summary` when `recent_turns` is null. `none` defaults to no
# tail at all, which is what keeps each policy's name honest out of the box.
DEFAULT_SUMMARY_RECENT_TURNS = 2


# The generated tool takes no arguments — except under `context: "summary"`,
# where it takes exactly one: the summary the model writes as it hands over.
class HandoffTarget(BaseModel):
    """One destination this agent may hand the conversation to.

    Each becomes a `handoff_to_<slug>` tool, so `description` is what the model
    routes on.
    """

    # This destination's name in this agent's mouth. It is the label the model
    # sees (`handoff_to_<slug>`), the label the transcript records, and — when
    # `agent_id` is absent — the team member it resolves to. Stable across a
    # rename of the target agent, deliberately: renaming an agent must not
    # change the tool name a published prompt was written against.
    name: str = Field(min_length=1)
    # A stored agent, entered at its latest published version. Absent means
    # `name` is resolved in this call's team roster instead.
    agent_id: str | None = None
    # What the model routes on. Required and non-empty: an undescribed
    # destination is a coin flip.
    description: str = Field(min_length=1)
    # The same three words `AgentConfig.conversation.context` uses for what a new
    # CALL starts with — one question, one set of answers. Under `summary` the
    # source agent writes the summary itself, as an argument on the generated
    # tool, so there is no second LLM call and a realtime agent can use the mode.
    context: ConversationContext = "transcript"
    # How many recent turns cross verbatim, so the target knows what is being
    # asked right now. A turn starts at a user message and runs until the next
    # one. Null is the policy's own answer — two under `summary`, no tail at all
    # under `none` — and `ge=1` is what makes null unambiguous, because there is
    # no 0 competing to mean the same thing. Past ten turns the honest answer is
    # `transcript`.
    recent_turns: int | None = Field(default=None, ge=1, le=10)
    # What the source agent is asked to write. Null uses DEFAULT_SUMMARY_PROMPT.
    # Personalized and token-checked at publish exactly as `description` and
    # `message` are — it is baked into the schema the model is shown.
    summary_prompt: str | None = None
    message: str | None = None  # spoken while the target loads

    model_config = ConfigDict(extra="forbid")

    @field_validator("name", "description")
    @classmethod
    def _strip(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped

    @field_validator("summary_prompt")
    @classmethod
    def _strip_or_default(cls, value: str | None) -> str | None:
        # Whitespace is not an override. Blanking the field in the editor has to
        # mean "use the platform's line" — the same thing leaving it alone means
        # — rather than shipping a tool argument with an empty description.
        return value.strip() or None if value else None

    @model_validator(mode="after")
    def _fields_match_policy(self) -> HandoffTarget:
        """Refused rather than ignored: `transcript` already carries everything,
        so a turn count there is a misunderstanding worth a 422 rather than a
        setting that silently does nothing."""
        if self.context == "transcript" and self.recent_turns is not None:
            raise ValueError(
                "recent_turns does not apply to context 'transcript', which already carries "
                "every turn - use context 'summary' or 'none'"
            )
        if self.context != "summary" and self.summary_prompt is not None:
            raise ValueError(
                f"summary_prompt only applies to context 'summary', not '{self.context}'"
            )
        return self


# The declaration is not the value. Declaring is also what gives the editor a
# list to show, the CoPilot something it can legitimately write into a prompt,
# and the tenant's own backend a readable contract for what to send.
class VarDeclaration(BaseModel):
    """One `{{vars.*}}` variable this agent reads, and what it falls back to.

    The request that starts the session supplies values that override `default`;
    an inbound phone call or message has none, so `default` is what resolves.
    """

    # Read back as `{{vars.<name>}}`, so it has to be a name the resolver's token
    # pattern can match — checked by the same function the request side uses.
    name: str = Field(min_length=1)
    # For the humans and the CoPilot, never substituted anywhere.
    description: str = ""
    # What `{{vars.<name>}}` resolves to when the session supplied no value.
    # Null is "empty unless supplied" — an absent key and a blank one read
    # identically in a prompt, and only one of them is honest.
    default: str | None = None
    # Its own flag rather than "no default", because a var with no default
    # legitimately means "empty unless supplied", and overloading the absence of
    # a default would change what every stored agent's variables already mean.
    #
    # Enforced at the door of a session and never inside one: the four requests
    # that start a call, a dial, a campaign or a text thread, and a task run.
    # Nothing refuses mid-session — not a handoff, not a message — because
    # killing a connected session over the next agent's requirement is a bigger
    # harm than a blank substitution. An inbound phone call has no request of
    # ours at all, so the three doors into "this agent answers this number"
    # (assign, publish, roll back) refuse a voice agent that requires something
    # with no default, which is what keeps one sentence true: a required
    # variable is always satisfied when the session starts.
    required: bool = Field(
        default=False,
        description=(
            "Refuse the session rather than resolve this to nothing; a `default` satisfies "
            "it. An inbound phone call carries no request to supply values, so a voice agent "
            "requiring a variable with no default cannot be assigned to a phone number."
        ),
    )

    model_config = ConfigDict(extra="forbid")

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        return validate_var_name(value.strip())

    @field_validator("description")
    @classmethod
    def _strip_description(cls, value: str) -> str:
        return value.strip()


def missing_required_vars(
    declared: Sequence[VarDeclaration], values: Mapping[str, str]
) -> list[str]:
    """The `required` variables this session supplies no value and no default for.

    A door rule, and only a door rule. Every caller is a request being accepted
    or refused — the four that start a session (`services.agents.plan`, and
    `services.telephony.dial` for a campaign whose agent changed under it), the
    three that let a voice agent answer a phone number
    (`services.agents.service`, `services.telephony.service`) and the task run
    (`services.tasks`). No worker calls it: once a session is connected a missing
    variable resolves to nothing, exactly as a missing `userdata` key does.

    Takes the declarations rather than a config, which is what lets an agent and
    a task share it. `name not in values` and never truthiness: an empty string
    is a caller deliberately blanking a default, not a request for it.
    """
    return [v.name for v in declared if v.required and v.name not in values and v.default is None]


def handoff_tool_name(destination: str) -> str:
    """The function name the model sees for one handoff destination.

    Derived from the destination's `name` rather than from the target agent's,
    so renaming an agent in the dashboard cannot change the tool name a
    published prompt was written against. Returns ``handoff_to_`` alone when the
    name slugs to nothing, which validation refuses.
    """
    return "handoff_to_" + re.sub(r"[^a-z0-9]+", "_", destination.strip().lower()).strip("_")


# ────────────────────── what an agent and a task both are ───────────────────
# LiveKit divides configuration in one specific place, and this mirrors it.
# `AgentSession.__init__` takes the media stack as session-wide defaults, plus
# userdata and the step ceiling; `Agent.__init__` takes instructions, tools and
# MCP servers, plus the same media slots as optional overrides; and
# `AgentTask.__init__` takes a strict subset of `Agent`'s.
#
# So this class is everything an `Agent` is, and `AgentConfig` adds the session
# it owns and the media stack that session runs. A task declares no media at
# all: it runs inside the session of the agent that entered it and inherits the
# whole stack (`documentation/agents/agent-tasks.mdx`).


class AgentBase(BaseModel):
    """What an agent and a task both are: a prompt, a model, and what it can reach."""

    model_config = ConfigDict(extra="forbid")

    # Required non-empty display name (also stored on the row's `name` column).
    name: str = Field(min_length=1, description="Unique in the workspace.")
    # Personalized per session: `{{userdata.field}}` is substituted from the
    # session's state when the agent is built. `{{args.…}}`/`{{secrets.…}}` are
    # rejected by validation — see `compiler.compile.personalize`.
    prompt: str = Field(
        default="You are a helpful, concise voice assistant.",
        description=(
            "The system prompt. `{{userdata.field}}` is substituted from the session's userdata "
            "when the agent starts, so one definition can address the person it reached."
        ),
    )
    # Where the clock runs. Unlike the media stack it applies on every channel,
    # and a task with no channel at all still needs one to read the date.
    timezone: str | None = Field(
        default=None,
        description=(
            "The IANA timezone this clock runs on, e.g. 'Asia/Kolkata'. Set it and the prompt "
            "and tools can read {{system_vars.now}}, {{system_vars.date}} and "
            "{{system_vars.time}}. Required only if one of those is used."
        ),
    )
    # Uncapped, deliberately. A count is not what makes a variable list bad; the
    # one number anybody could defend is the size of the prompt they render
    # into, and nothing here measures that either — the model's context window
    # is the only limit, enforced by the provider on the call. `AgentConfig`
    # never had a cap; inheriting one from `TaskConfig` would have made an
    # existing agent with more than 25 unparseable.
    vars: list[VarDeclaration] = Field(
        default_factory=list,
        description=(
            "The variables read as {{vars.name}} in the prompt, the greeting and the tools. "
            "Values from the request that starts a session override the defaults. They are "
            "visible to the model - keep credentials in a workspace secret."
        ),
    )
    # `| None` because a realtime agent has no separate text model: it is the
    # discriminator `normalize_channel_media` clears the cascade slots against.
    # `TaskConfig` narrows it back, having no realtime pipeline to choose.
    llm: LLMSpec | None = Field(default_factory=LLMSpec)
    tools: list[ToolSelection] = Field(
        default_factory=list,
        description=(
            "Tools the model may call. Publishing pins each one to the tool version live at "
            "that moment, so republishing a tool does not change this until it is published "
            "again."
        ),
    )
    mcps: list[McpSelection] = Field(
        default_factory=list,
        description=(
            "MCP servers whose tools may be called — each named by the integration that holds "
            "its credential, or defined inline. Which of a named server's tools may be called "
            "is the integration's own `allowed_tools`, not this list."
        ),
    )
    faqs: list[FaqSelection] = Field(
        default_factory=list,
        max_length=10,
        description=(
            "FAQs the agent answers from: questions go in the prompt, answers are fetched on "
            "demand."
        ),
    )
    on_enter: ToolSelection | None = None
    on_exit: ToolSelection | None = None
    # Runs after each user turn, before the LLM replies. On voice/video that is
    # each spoken turn; on text, each inbound message. Exposes {{args.user_message}}.
    on_user_turn_completed: ToolSelection | None = None

    @field_validator("name", mode="before")
    @classmethod
    def _strip_name(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip()
        return value

    @field_validator("timezone")
    @classmethod
    def _check_timezone(cls, value: str | None) -> str | None:
        return None if value is None else validate_iana_timezone(value)

    def tool_selections(self) -> list[ToolSelection]:
        """Every tool this can run — the LLM-callable ones, then the hooks.

        Deduplicated, in that order: the same tool can be both attached and
        wired to a hook, and it is one tool either way. Stored tools dedupe by
        id; an inline tool is only ever the object it was written as, so it
        dedupes by identity — the same `InlineTool` reused on a hook collapses,
        two separately-written copies do not, and the name-uniqueness check is
        what refuses those.
        """
        hooks = (self.on_enter, self.on_exit, self.on_user_turn_completed)
        seen: dict[object, ToolSelection] = {}
        for sel in [*self.tools, *[h for h in hooks if h]]:
            seen.setdefault(sel.tool_id if sel.tool_id else id(sel.tool), sel)
        return list(seen.values())

    def required_providers(self) -> set[str]:
        """AI providers this needs a BYOK key for — here, the language model.

        Fallbacks count: the compiler builds them up front, alongside the
        primary, so a missing fallback key breaks the whole session rather than
        just the failover. ``AgentConfig`` adds the rest of its pipeline.
        """
        specs = [self.llm, self.llm.fallback if self.llm else None]
        return {spec.provider.strip().lower() for spec in specs if spec and spec.provider.strip()}


# ────────────────────────────── agent tasks ─────────────────────────────────
# A task is LiveKit's `AgentTask`: an agent with a typed return value, entered
# from a conversation and handing control back when it is done. It lives here
# rather than in `services.tasks` because `AgentConfig.tasks` embeds it — the
# same reason `InlineTool` lives beside `ToolSelection`.

# The name the compiler injects for the structured-output tool, and the name of
# the one that ends a sub-conversation without one. Both reserved
# workspace-wide in ``services.tools.RESERVED_TOOL_NAMES`` so a tenant tool can
# never collide and leave the model looking at two tools of one name.
SUBMIT_RESULT_TOOL = "submit_result"
FINISH_WITHOUT_RESULT_TOOL = "finish_without_result"

# What a NEW task's two descriptions start as. One plain line each: they exist so
# a required field is never blank on a task nobody has written yet, NOT so the
# platform gets to decide how anyone's task finishes. The abandon line does say
# *when* — a bare "give up" invites giving up at the first hurdle. Nothing is
# appended to a task's prompt, so whatever stands here is the whole of what the
# model is told about calling that tool — which is the author's job, and the
# editor, the docs and the CoPilot all say to do it.
#
# They are the defaults of two required fields, so `create_task` writes them into
# the stored config the first time it dumps it. From that moment they are the
# author's: in the editor, in the version diff, in what the compiler sends. The
# compiler has nowhere to fall back TO.
STARTER_SUBMIT_RESULT_DESCRIPTION = (
    "Submit results if you are sure that you have achieved the goal of this task"
)
STARTER_FINISH_WITHOUT_RESULT_DESCRIPTION = (
    "Finish without result only if you have failed to achieve the goal of this task and would "
    "like to give up rather than continue."
)


# The vocabulary is ``AnalysisField``'s, reused rather than reinvented. A task
# that wants to return five talking points returns one string containing them —
# a stated limitation, and what keeps a task's output mergeable into a flat
# column space downstream.
class TaskOutputField(BaseModel):
    """One value the model must produce.

    Flat scalars only — no arrays and no nested objects.
    """

    name: str = Field(
        pattern=r"^[a-z][a-z0-9_]{0,39}$",
        description=(
            "Identifier for this value, as it appears in the run's `output` and anywhere "
            "the run is consumed. Lower-case letters, digits and underscores."
        ),
    )
    type: Literal["string", "boolean", "integer", "number"] = Field(
        default="string",
        description="The shape of the value. Anything that is not a number or a yes/no is a string.",
    )
    description: str = Field(
        min_length=1,
        description=(
            "What the model should put here, and what to send when it cannot find out. "
            "This is the field's description in the tool schema, so it is literally the "
            "prompt for this value."
        ),
    )
    # Whether the model may answer `null`. The key is always required in
    # `submit_result` either way, so "omitted" can never be mistaken for "not
    # generated yet"; this only decides whether "I could not find it" is an
    # accepted answer or a validation error the model has to fix.
    required: bool = Field(
        default=True,
        description=(
            "Refuse a null for this field. Turn it off for a value the model may "
            "legitimately fail to find, and say in the description when to send null."
        ),
    )

    model_config = ConfigDict(extra="forbid")


class TaskConfig(AgentBase):
    """LiveKit's `AgentTask`: an agent with a typed result, on someone else's stack.

    It declares no channel, no media and no greeting. Entered by an agent it
    speaks with that call's voice and hears with its ears; run on its own from
    `POST /tasks/{id}/run` it is an LLM with tools and nobody to talk to. Which
    of the two happens is the caller's, never the task's.
    """

    prompt: str = Field(
        default="",
        description=(
            "The system prompt. `{{vars.name}}` is substituted from the values the run was "
            "started with — or, entered by an agent, from the call's own values and the "
            "arguments the model supplied."
        ),
    )
    # Narrowed from `AgentBase`: there is no realtime pipeline to leave it empty
    # for, and a standalone run has no session to inherit a model from.
    llm: LLMSpec = Field(default_factory=LLMSpec)
    # The task's inputs, and there is no second word for them. Entered by an
    # agent they are also the schema of the tool that enters it, minus whatever
    # the call already supplies by name — see `TaskSelection`.
    vars: list[VarDeclaration] = Field(
        default_factory=list,
        description=(
            "The inputs this task takes, read as {{vars.name}} in the prompt and in its "
            "tools. Declare only what the caller or the model must supply: anything the "
            "task can read for itself ({{userdata.*}}, {{system_vars.*}}) it should read "
            "in its prompt instead."
        ),
    )
    output: list[TaskOutputField] = Field(
        default_factory=list,
        max_length=25,
        description=(
            "The typed value this task produces. The model is given a `submit_result` tool "
            "built from these fields and told that calling it is how the run finishes. "
            "Publishing needs at least one; a draft may be empty."
        ),
    )
    # The two generated tools' descriptions. Required and non-empty, exactly as
    # `TaskOutputField.description` and `TaskSelection.description` are, and for
    # the same reason: this sentence is the only thing telling the model when to
    # call the tool, and an undescribed tool is a coin flip. The default is a
    # one-line starter the first dump writes into the row, not a fallback the
    # compiler reaches for — see `STARTER_SUBMIT_RESULT_DESCRIPTION`.
    submit_result_description: str = Field(
        default=STARTER_SUBMIT_RESULT_DESCRIPTION,
        min_length=1,
        description=(
            "Description of the generated `submit_result` tool - what the call is, where the "
            "output fields describe the values."
        ),
    )
    # Unused by a standalone run, where the tool is not injected at all. It is
    # still the task's rather than the attachment's: the same task entered by
    # two agents wants the same answer to "when may you give up", and the
    # attachment already carries the thing that does differ per agent — the
    # `description` the caller decides on.
    finish_without_result_description: str = Field(
        default=STARTER_FINISH_WITHOUT_RESULT_DESCRIPTION,
        min_length=1,
        description=(
            "Description of `finish_without_result`, the tool that abandons a task an agent "
            "entered. Narrow it if the task gives up too readily. Unused by a standalone run."
        ),
    )
    # A "step" is an LLM->tools->LLM ROUND, not a tool call: several tools in one
    # assistant message cost one step. 25 rounds that each fan out is ample, and
    # it is the same number text agents use. `timeout_seconds` is the real bound;
    # this is the runaway-loop guard.
    #
    # Standalone runs only. Entered by an agent the task runs on the calling
    # session's `max_steps`, because that is an `AgentSession` parameter in
    # LiveKit and a hand-written `AgentTask` inherits its session's budget too.
    max_steps: int = Field(
        default=25,
        ge=1,
        le=50,
        description=(
            "How many LLM -> tools -> LLM rounds a standalone run may take. A round, not a "
            "tool call: four tools in one reply cost one step. Entered by an agent, the "
            "calling call's own limit applies instead."
        ),
    )
    # Five minutes, not two, because the first thing a research task hits is this
    # one. A provider tool (`llm.builtin_tools`) runs INSIDE a step rather than
    # as one, so it spends tens of seconds and no steps at all -- the run dies on
    # the clock with `max_steps` untouched, and `max_steps` is the number the
    # author reaches for. Changing the default moves new tasks only: an existing
    # config is stored JSONB with its own number already written.
    timeout_seconds: int = Field(
        default=300,
        ge=10,
        le=600,
        description=(
            "How long this may take before it is abandoned. Entered by an agent it is the "
            "only bound on how long the caller is held, so keep it short on a voice agent."
        ),
    )

    @field_validator("submit_result_description", "finish_without_result_description")
    @classmethod
    def _described(cls, value: str) -> str:
        """`min_length` refuses "", this refuses "   " — a tool described by
        whitespace is described by nothing."""
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped

    @model_validator(mode="before")
    @classmethod
    def _no_handoffs(cls, data: Any) -> Any:
        """A task hands control back to whoever entered it, so it cannot hand the
        conversation on.

        `extra="forbid"` already refuses the key; this is here only so the answer
        says why, because "convert this agent into a task" is exactly the moment
        someone sends one.
        """
        if isinstance(data, dict) and "handoffs" in data:
            raise ValueError(
                "a task cannot hand the conversation to another agent - it returns to the "
                "agent that entered it, so a handoff would strand that return"
            )
        return data

    def column_space(self) -> Counter[str]:
        """Every name this task puts into one flat space: its inputs and its
        outputs together.

        They share a space because a consumer merges them — the values a run was
        given beside the values it produced — so a collision would make one of
        the two unreachable. Validation refuses one; this is the count both it
        and any caller reads.
        """
        return Counter([v.name for v in self.vars] + [f.name for f in self.output])


class TaskSelection(BaseModel):
    """One job this agent can enter and come back from: name a stored task, or
    define one inline."""

    # The function name the model sees. Authored, not derived from the task's
    # name — renaming a task must not change the tool name a published prompt
    # was written against. The same rule `HandoffTarget.name` follows.
    #
    # No `enter_`/`task_` prefix, unlike a handoff's `handoff_to_*`: a handoff
    # changes who is speaking, permanently, and a task call returns — so from
    # the model's side this is an ordinary tool. Typed in, typed out, control
    # comes back.
    name: ToolName
    task_id: str | None = None
    # Null in a draft, pinned at publish — sharper than a tool pin, because the
    # task's `vars` ARE this tool's schema and its `output` IS the return type.
    # An inline task has nothing to pin: it *is* the definition.
    task_version: int | None = None
    task: TaskConfig | None = None
    # What the model decides on. Required and non-empty, exactly as a handoff
    # destination's is: an undescribed job is a coin flip.
    description: str = Field(
        min_length=1,
        description=(
            'Start with "Handoff to an agent task responsible for ...", then say when to '
            "enter it and when to skip it."
        ),
    )
    message: str | None = None  # spoken while the task loads
    # There is no context policy. A task always starts with the conversation so
    # far minus tool mechanics, because a task that does not know what was said
    # asks the caller to repeat themselves — the worst thing a voice agent does.
    # The way back out is fixed by LiveKit for the same reason, so a knob here
    # would make the boundary asymmetric for no reason a user could name.

    model_config = ConfigDict(extra="forbid")

    @field_validator("description")
    @classmethod
    def _strip(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped

    @model_validator(mode="after")
    def _exactly_one_side(self) -> TaskSelection:
        if (self.task_id is None) == (self.task is None):
            raise ValueError("set either task_id or task, not both")
        if self.task_version is not None and self.task_id is None:
            raise ValueError("task_version needs a task_id - an inline task has no versions")
        return self


# Not an API shape: what the runtime resolves a `TaskSelection` into once the
# pinned version and its frozen tools have been read. It lives here because it is
# the definition side of the selection above, and because both halves have to
# stay in step — the attachment names the tool, the config IS its schema.
@dataclass(frozen=True, slots=True)
class PinnedTask:
    """One task an agent may enter, resolved to everything entering it needs.

    Read once at session start (``workers.session.load.resolve_pinned_tasks``),
    so entering a task mid-call costs no database round trip for the definition
    or its tools. ``task_id`` and ``version`` are None for an inline task, which
    has no row and nothing to pin.
    """

    selection: TaskSelection
    config: TaskConfig
    tools: list[ToolDefinition]
    task_id: str | None = None
    version: int | None = None


# What a null `AgentConfig.max_duration_seconds` runs as, and the most a set one
# may be. The same 3 hours `livekit.sip.max_call_duration_seconds` holds a phone
# leg to and the voice worker's `drain_timeout` waits out, applied by the voice
# runtime to every call — web, video and media streams have no carrier to do it.
MAX_CALL_DURATION_SECONDS = 10800

# What a null `AgentConfig.max_steps` means. Voice and video stay low because
# every extra round is dead air on a live call; a text turn can afford a long chain
# of tool or MCP calls.
DEFAULT_MAX_STEPS: dict[Literal["voice", "video", "text"], int] = {
    "voice": 4,
    "video": 4,
    "text": 25,
}


# The live draft (agents.config) and the frozen snapshot
# (agent_versions.config) are both this model. It is the deterministic compile
# input.
class AgentConfig(AgentBase):
    """The whole agent: what it says, which models run it, and what it can do.

    ``AgentBase`` plus the session this agent owns and the media stack that
    session runs — which is what a task, running inside someone else's session,
    has none of.

    A complete definition, not a patch — whatever it omits takes that field's
    default.
    """

    # video = the same voice pipeline + an Anam avatar face
    channel: Literal["voice", "video", "text"] = "voice"
    greeting: str | None = Field(
        default="Hello! How can I help you today?",
        description=(
            "The opening line, spoken on connect and on the way in from a handoff; empty or "
            "null makes the agent wait for the caller to speak first. Voice and video only. "
            "Supports the same substitutions as the prompt, plus `{{consent.notice}}`, the "
            "recording disclosure's place in the line."
        ),
    )
    # A greeting that speaks the recording notice is protected regardless: the
    # disclosure is the one line a caller must hear whole.
    greeting_interruptible: bool = Field(
        default=True,
        description=(
            "Whether the caller can cut the greeting off. A greeting speaking the recording "
            "notice never can. Cannot be false on a realtime model."
        ),
    )

    # Translated per model by ``services.catalog.resolve_entry_language``, so the
    # user never picks a language twice and STT and TTS cannot disagree.
    language: str | None = Field(
        default=None,
        description=(
            "The one language the caller and the agent speak, shared by every model in the "
            "pipeline. Use a code from the provider catalog's language lists; null means "
            "Auto. Voice and video only."
        ),
    )

    # A voice/video agent runs one of two pipelines, and `realtime` is the
    # discriminator. Set, it is the whole pipeline and the three cascade slots
    # are cleared; unset, the cascade runs as it always has. A stored mode enum
    # would be a second source of truth able to disagree with the spec it
    # describes.
    stt: STTSpec | None = Field(default_factory=STTSpec)
    tts: TTSSpec | None = Field(default_factory=TTSSpec)
    realtime: RealtimeSpec | None = None

    turn_handling: TurnHandlingSpec = Field(default_factory=TurnHandlingSpec)

    handoffs: list[HandoffTarget] = Field(
        default_factory=list,
        description=(
            "Where this agent may hand the conversation next. Each entry becomes one tool "
            "the model can call, so routing quality lives in its `description`. A target is a "
            "stored agent (`agent_id`) or a member of the team defined on the call."
        ),
    )
    # Immediately after `handoffs` because they are the two ways an agent reaches
    # another definition, and the difference is the whole point: a handoff gives
    # the caller away, a task borrows the floor and hands it back.
    tasks: list[TaskSelection] = Field(
        default_factory=list,
        description=(
            "Jobs this agent can enter and come back from. Each becomes one tool the model "
            "can call; the task runs inside this call, on its voice and its ears, and returns "
            "its typed output as the tool's result."
        ),
    )
    # The same unit as `TaskConfig.max_steps` — rounds, not LiveKit's own
    # `max_tool_steps`, which is one less than the rounds it allows (see
    # `compile_agent`). Null rather than a number because the right default
    # depends on the channel, and a stored 4 would outlive a switch to text.
    # It is an `AgentSession` parameter, so the agent that starts the session
    # sets it for the whole call: a handoff target's own value is not read.
    max_steps: int | None = Field(
        default=None,
        ge=1,
        le=50,
        description=(
            "How many LLM -> tools -> LLM rounds one turn may take before the agent must answer "
            "without tools. Null means 4 on voice and video, 25 on text. Not enforced on "
            "realtime models; a handoff keeps the starting agent's limit."
        ),
    )
    # Both bound the call rather than the agent, so like `max_steps` they are
    # read off the agent that answered. Null is not "unbounded": the voice
    # runtime still ends every call at `MAX_CALL_DURATION_SECONDS`, the same
    # ceiling the SIP trunk enforces, so a web call is held to it too.
    silence: SilenceSpec = Field(default_factory=SilenceSpec)
    max_duration_seconds: int | None = Field(
        default=1800,
        ge=30,
        le=MAX_CALL_DURATION_SECONDS,
        description=(
            "The longest a call may last; the agent then says goodbye and hangs up. Null means "
            "the platform's 3-hour cap. Voice and video only; a handoff keeps the starting "
            "agent's limit."
        ),
    )

    background_audio: BackgroundAudioSpec = Field(default_factory=BackgroundAudioSpec)
    noise_cancellation: NoiseCancellationSpec = Field(default_factory=NoiseCancellationSpec)
    vision_input: VisionInputSpec = Field(default_factory=VisionInputSpec)
    keypad_input: KeypadInputSpec = Field(default_factory=KeypadInputSpec)
    voicemail_detection: VoicemailDetectionSpec = Field(default_factory=VoicemailDetectionSpec)
    recording: RecordingSpec = Field(default_factory=RecordingSpec)
    analysis: AnalysisSpec = Field(default_factory=AnalysisSpec)
    conversation: ConversationSpec = Field(default_factory=ConversationSpec)
    avatar: AvatarSpec | None = None

    @model_validator(mode="after")
    def normalize_channel_media(self) -> AgentConfig:
        if self.channel != "voice":
            # The keypad is a phone-line feature, and only a `voice` agent can
            # hold a phone number or answer a media stream. A `video` agent is a
            # web call wearing an avatar and a `text` agent has no call at all.
            # Refused rather than cleared: `enabled: true` is the author asking
            # for it, and clearing it would leave their next read showing it off
            # with no reason given. The rest of the spec means nothing while it
            # is off, so it is reset like any other field that does not apply.
            if self.keypad_input.enabled:
                raise ValueError(
                    f"a {self.channel} agent cannot take keypad input - only a voice agent can "
                    "answer a phone call or a media stream. Turn keypad_input off first."
                )
            self.keypad_input = KeypadInputSpec()
            # Same reason, same treatment: only a voice agent places calls.
            if self.voicemail_detection.enabled:
                raise ValueError(
                    f"a {self.channel} agent cannot detect voicemail - only a voice agent places "
                    "phone calls. Turn voicemail_detection off first."
                )
            self.voicemail_detection = VoicemailDetectionSpec()
        if self.channel == "text":
            # Text agents are LLM + tools only: no media stack, no spoken
            # greeting, no voice turn knobs. Lifecycle hooks are allowed and
            # map to the chat: enter when it starts, exit when it ends. Analysis
            # and `conversation` mean on a chat what they mean on a call.
            self.stt = None
            self.tts = None
            self.realtime = None
            self.llm = self.llm or LLMSpec()
            self.greeting = None
            self.greeting_interruptible = True
            self.language = None  # nothing to configure a language on; the prompt sets the tone
            self.turn_handling = TurnHandlingSpec()
            self.background_audio = BackgroundAudioSpec(enabled=False)
            # No caller audio to clean up either — and left enabled it would go on
            # demanding an ai-coustics key to publish a chat agent.
            self.noise_cancellation = NoiseCancellationSpec(enabled=False)
            # A text conversation has no room and no tracks, so there is nothing
            # to watch. Refused when on, for the reason the keypad is above.
            if self.vision_input.screenshare.enabled:
                raise ValueError(
                    "a text agent cannot watch a screen share - a chat has no call to share a "
                    "screen into. Turn vision_input.screenshare off first."
                )
            self.vision_input = VisionInputSpec()
            # No audio to record, so no disclosure to make either. Left set, the
            # two would be a claim the UI has to explain away.
            self.recording = RecordingSpec(enabled=False, consent="off")
            self.avatar = None
        elif self.realtime is not None:
            # One model hears and speaks, so there is nothing to cascade. It also
            # runs its own turn detection server-side, which means LiveKit never
            # holds a reply back — preemptive generation has nothing to generate
            # ahead of.
            self.stt = None
            self.tts = None
            self.llm = None
            self.turn_handling.preemptive_generation.enabled = False
            self.turn_handling.preemptive_generation.preemptive_tts = False
            # The avatar renders from the audio the agent publishes, and a
            # realtime model publishes audio like any TTS — so video works on
            # this pipeline too, and needs the same face.
            if self.channel == "video":
                self.avatar = self.avatar or AvatarSpec()
            else:
                self.avatar = None
            if self.vision_input.screenshare.enabled:
                # Refused rather than cleared, for the reason `conversation.context`
                # is refused on text: they did ask, and the answer is no. A
                # speech-to-speech model detects turns inside the provider's own
                # socket, so `on_user_turn_completed` — the hook the newest frame
                # is injected into — never fires; the agent would run and see
                # nothing at all. The catalog `vision` flag does not cover this:
                # three of the five realtime entries can read an image, so the
                # publish check would wave through exactly the models where the
                # frame goes nowhere.
                raise ValueError(
                    "a realtime agent cannot watch a screen share - a speech-to-speech model "
                    "detects turns itself, so there is no moment to hand it the newest frame. "
                    "Switch to the speech-to-text -> LLM -> text-to-speech cascade."
                )
        else:
            self.stt = self.stt or STTSpec()
            self.tts = self.tts or TTSSpec()
            self.llm = self.llm or LLMSpec()
            if self.channel == "video":
                self.avatar = self.avatar or AvatarSpec()
            else:
                self.avatar = None
            if self.vision_input.screenshare.enabled:
                # A speculative reply is only usable if the context it was
                # generated against still matches the one the turn ends up
                # running on — LiveKit compares the two after
                # `on_user_turn_completed`. Injecting the newest frame there
                # mutates that context on EVERY turn (the "nobody is sharing"
                # note mutates it too), so every preemptive reply would be
                # generated, billed, thrown away and logged as a warning. Binds
                # `voice` and `video` alike — an agent that watches a screen
                # gets no preemptive generation on either.
                self.turn_handling.preemptive_generation.enabled = False
                self.turn_handling.preemptive_generation.preemptive_tts = False
        return self

    def required_providers(self) -> set[str]:
        """AI providers this agent needs a BYOK key for.

        Every model the compiler will instantiate — the LLM (``AgentBase``'s
        half, with its fallback); voice adds STT + TTS; video adds the avatar; a
        realtime agent needs exactly one, for the model that does all three. Run
        after ``normalize_channel_media``, so the specs present here are exactly
        the ones ``compiler.factories`` will build.

        Post-call analysis is NOT counted here even though it also needs a key.
        Its model is optional, and unset means a default that lives in
        catalog.yaml — which this module deliberately cannot read. Nor are
        attached tasks, whose models live on rows this module cannot read. The
        publish check in ``services.agents.validate`` unions both in.
        """
        specs: list[Any] = [self.stt, self.tts, self.realtime, self.avatar]
        specs += [spec.fallback for spec in (self.stt, self.tts) if spec]
        # Noise cancellation counts only while it is on. It is the one spec that
        # survives being turned off — the model and strength stay stored so the
        # toggle remembers them — so its mere presence must not demand a key.
        if self.noise_cancellation.enabled:
            specs.append(self.noise_cancellation)
        return super().required_providers() | {
            spec.provider.strip().lower() for spec in specs if spec and spec.provider.strip()
        }


# ───────────────────────── service request / response ───────────────────────
# Shared by HTTP routes and CoPilot.


class CreateAgentRequest(BaseModel):
    config: AgentConfig  # validated against the model on the way in


class AgentVersionResponse(BaseModel):
    version: int
    published_at: datetime
    # The email of whoever published it. Null once that account is gone — the
    # version outlives the person, and saying so is better than inventing a name.
    published_by: str | None = None


class AgentVersionDetailResponse(AgentVersionResponse):
    """One frozen version, unpacked: the definition that ran, beside its metadata.

    `config` comes back through `AgentConfig` rather than as raw JSONB, so a
    version and the draft are normalized by the same validator and a diff of the
    two cannot report a difference that `normalize_channel_media` invented.

    It is the whole definition: the tool versions this publish pinned live in
    `config.tools`, and their definitions in `tool_versions`.
    """

    config: AgentConfig


class AgentResponse(BaseModel):
    id: UUID
    config: AgentConfig
    published_version: int | None = None
    # When `published_version` was frozen. Null while the agent is a draft. Read
    # against `updated_at` it answers the question a draft/publish model always
    # raises: has the draft moved on from what calls are running?
    published_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    # Who created this, as a control-plane user id. Attribution only — resolve it
    # to a name against GET /v1/org/members, which is where people live.
    created_by: UUID | None = None
    # Only `get_agent` fills this; the list endpoint leaves it null rather than
    # running a history query per agent.
    versions: list[AgentVersionResponse] | None = None


class PublishAgentResponse(BaseModel):
    agent_id: UUID
    version: int
    published_at: datetime
    warnings: list[str] = []
