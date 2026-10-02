"""Shared LiveKit RoomOptions helpers for voice/video room sessions."""

from __future__ import annotations

from livekit import rtc
from livekit.agents import AutoSubscribe
from livekit.agents.voice.room_io import AudioInputOptions, RoomOptions


def room_options(
    participant_identity: str | None,
    *,
    noise_cancellation: rtc.FrameProcessor[rtc.AudioFrame] | None = None,
    **kwargs: object,
) -> RoomOptions:
    """Room I/O for one session.

    The enhancer belongs to the audio input rather than to the session: it runs
    ahead of `session.input`, so the speech-to-text model, the VAD and the
    recorder all read what it produced rather than what the caller's microphone
    sent.
    """
    if participant_identity:
        kwargs["participant_identity"] = participant_identity
    if noise_cancellation is not None:
        # `auto_gain_control` is left unset on purpose, which since
        # livekit-agents 1.8.0 means AGC is OFF here: RoomIO turns it off by
        # default whenever noise cancellation is a processor instance rather
        # than a selector. The enhancer already normalises level, so WebRTC's
        # AGC on top of it is a second gain stage that pumps and clips. Passing
        # True would restore the 1.7.1 behaviour — do that only with a recording
        # that shows a caller left too quiet.
        kwargs["audio_input"] = AudioInputOptions(noise_cancellation=noise_cancellation)
    return RoomOptions(**kwargs)


def auto_subscribe_for_channel(channel: str) -> AutoSubscribe:
    if channel not in {"voice", "video"}:
        raise ValueError(f"unsupported LiveKit room channel: {channel}")
    return AutoSubscribe.AUDIO_ONLY
