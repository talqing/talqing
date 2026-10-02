"""Handoff media-graph checks over AgentConfig.

The video channel means one thing: the agent's own audio, whatever produces it,
gets an Anam face. The avatar joins the room as a second participant and renders
from the audio the agent publishes — so a video agent runs either pipeline, and
the TTS (or realtime model) is an ordinary model choice rather than a media path.
"""

from __future__ import annotations

from .models import AgentConfig


def media_signature(config: AgentConfig) -> tuple[object, ...]:
    """The media graph shape that must remain stable across a handoff."""
    # A realtime node owns the microphone and the speaker itself, so swapping it
    # for a cascade node mid-call would mean tearing down the speech-to-speech
    # socket and standing up an STT, a TTS and a VAD-driven turn detector in its
    # place. The pipeline is part of the media shape on every channel, video
    # included — there the avatar sink stays, but what feeds it is replaced.
    if config.channel != "video":
        return (config.channel, config.realtime is not None)

    av = config.avatar
    return (
        config.channel,
        config.realtime is not None,
        av.provider if av else None,
        av.model if av else None,
        av.avatar_id if av else None,
        av.name if av else None,
    )


def handoff_media_error(source: AgentConfig, target: AgentConfig) -> str | None:
    """Return a publish/runtime error when a handoff would need media rewiring."""
    if source.channel != target.channel:
        return (
            f"handoff target channel '{target.channel}' does not match source "
            f"channel '{source.channel}'"
        )
    if (source.realtime is not None) != (target.realtime is not None):
        realtime_side = "source" if source.realtime is not None else "target"
        return (
            "a handoff target must use the same pipeline as its source — the "
            f"{realtime_side} uses a realtime speech-to-speech model and the other "
            "uses an STT-LLM-TTS cascade"
        )
    if source.channel == "video" and media_signature(source) != media_signature(target):
        return (
            "video handoffs require the same avatar provider, catalog model, avatar and "
            "display name - the Anam session is opened once, for the call, and cannot be "
            "swapped for another face part-way through"
        )
    if err := _expressive_handoff_error(source, target):
        return err
    return None


def recording_handoff_warning(source: AgentConfig, target: AgentConfig) -> str | None:
    """Recording switches off mid-call cleanly, and back on only sometimes.

    Off always works: the recorder pauses while the target holds the call and
    writes the gap as silence, so a flow can send a caller to a payment step
    without their card number landing on the file
    (`compiler.compile.CompiledAgent._follow_recording_policy`).

    On works only if something is already recording. `AgentSession.start(record=…)`
    is read once, from the config of the agent that **answered**; with recording
    off there, no `RecorderIO` is wired into the media graph at all and there is
    nothing to resume.

    **A warning, deliberately not an error.** Whether this edge is a problem
    depends on which agent answers, which no per-agent publish check can know:
    "Support (on) → Payments (off) → Support (on)" is the shape this whole
    feature exists for, and its second edge looks exactly like the broken one.
    So publish says what to check, and the runtime records a
    `recording.unavailable` event on any call where it actually bites.
    """
    if source.recording.enabled or not target.recording.enabled:
        return None
    return (
        f"'{target.name}' has recording on but '{source.name}' has it off — recording cannot "
        f"start part-way through a call, so nothing will be recorded if '{source.name}' is the "
        f"agent that answers. Turn recording on for '{source.name}' too; it pauses by itself "
        "while an agent with recording off is speaking."
    )


def _expressive_handoff_error(source: AgentConfig, target: AgentConfig) -> str | None:
    """A handoff out of an expressive agent must stay on the same voice.

    The target inherits the source's transcript, which is full of delivery tags,
    and gets few-shot into writing its own. A target whose voice does not speak
    them then reads them out to the caller as words — measured on
    `eleven_flash_v2_5`, which says "Laughs". With no runtime text processing
    anywhere, this rule is the only thing standing between that history and the
    caller, so it demands the whole voice: same provider, same model, same
    toggle. Once provider and model must match, matching the toggle too costs the
    author nothing and keeps the entire handoff graph on one dialect.

    Conditioned on the *source*, which makes it transitive for free: a legal
    target is itself expressive, so its own outgoing edges get the same check.
    Handing off *into* an expressive agent stays legal — the incoming history is
    plain text, which every voice speaks.
    """
    if source.tts is None or not source.tts.expressive:
        return None
    # The checks above returned already if the two disagreed on channel or on
    # pipeline, so a source with a TTS guarantees the target has one too.
    assert target.tts is not None
    if (target.tts.provider, target.tts.model, target.tts.expressive) == (
        source.tts.provider,
        source.tts.model,
        True,
    ):
        return None
    state = "on" if target.tts.expressive else "off"
    return (
        "a handoff out of an agent with expressive delivery must keep the same voice, or the "
        "target inherits a transcript full of delivery tags and reads them out to the caller - "
        f"source uses {source.tts.provider}/{source.tts.model} with expressive delivery on, "
        f"target uses {target.tts.provider}/{target.tts.model} with expressive delivery {state}"
    )
