"""Ambient + thinking background audio for LiveKit room sessions.

The player is built here and **owned by `VoiceRun`** (`start_background_audio` /
`stop_background_audio` there), not by whoever started it. Two reasons, both
audible:

- a warm transfer stops the ambient bed for the length of the hold and starts a
  fresh one if the caller comes back, and a `BackgroundAudioPlayer` cannot be
  reopened after `aclose()`;
- the bed has to stop when the *session* ends rather than when the job does.
  After a bridged transfer the job outlives the session by the whole
  human-to-human conversation, and a job-scoped close would play office ambience
  over both of them for all of it.
"""

from __future__ import annotations

from livekit.agents import (
    AgentSession,
    AudioConfig,
    BackgroundAudioPlayer,
    BuiltinAudioClip,
    JobContext,
)

from services.agents import AgentConfig
from services.tools import SessionUserData

_AMBIENT_CLIPS = {
    "office": BuiltinAudioClip.OFFICE_AMBIENCE,
    "city": BuiltinAudioClip.CITY_AMBIENCE,
    "forest": BuiltinAudioClip.FOREST_AMBIENCE,
    "crowd": BuiltinAudioClip.CROWDED_ROOM,
    "hold_music": BuiltinAudioClip.HOLD_MUSIC,
}
_THINKING_CLIPS = {
    "keyboard": BuiltinAudioClip.KEYBOARD_TYPING,
    "keyboard2": BuiltinAudioClip.KEYBOARD_TYPING2,
}
# Loud enough to read as "you are still connected" over a phone line. LiveKit's
# own default for the same clip.
_HOLD_MUSIC_VOLUME = 0.8


async def start_background_audio(
    ctx: JobContext, session: AgentSession[SessionUserData], cfg: AgentConfig
) -> BackgroundAudioPlayer | None:
    """Ambient + thinking sounds mixed into the agent's output track.

    Returns the player so its owner can stop it, or None when this agent has
    neither sound configured.
    """
    ba = cfg.background_audio
    ambient = _AMBIENT_CLIPS.get(ba.ambient or "")
    thinking = _THINKING_CLIPS.get(ba.thinking or "")
    if not ambient and not thinking:
        return None
    player = BackgroundAudioPlayer(
        ambient_sound=AudioConfig(ambient, volume=ba.ambient_volume) if ambient else None,
        thinking_sound=AudioConfig(thinking, volume=ba.thinking_volume) if thinking else None,
    )
    await player.start(room=ctx.room, agent_session=session)
    return player


def hold_audio_config(cfg: AgentConfig) -> AudioConfig:
    """What a caller hears while a warm transfer briefs the person answering.

    Always hold music — silence on a thirty-second hold reads as a dropped call,
    and none of the other ambient clips mean "wait". The one thing the builder
    gets a say in is how loud: an agent whose ambient bed *is* hold music has
    already had that conversation with its callers, so its volume carries.
    """
    ba = cfg.background_audio
    volume = ba.ambient_volume if ba.enabled and ba.ambient == "hold_music" else _HOLD_MUSIC_VOLUME
    return AudioConfig(BuiltinAudioClip.HOLD_MUSIC, volume=volume)
