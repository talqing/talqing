"""Parking the caller while a transfer dials somebody else.

Both bridge transports leave the caller alone on the line for as long as a
stranger's phone takes to ring, so they park them the same way: the agent's
ambient bed stops, hold music takes over, and the caller's audio is switched
off in both directions until either the transfer lands or they come back to
the agent.

**Not the REFER path**, where the carrier owns the media the instant the
transfer is accepted and there is nothing of ours left to play. Today that is
theoretical: `supports_refer` is False on all four carriers
(`services/telephony/catalog.py`), so every transfer we place is a bridge and
every caller hears this. If a flag ever flips, that carrier's callers will hear
whatever their carrier plays instead, and that difference is not ours to remove.

The ambient bed has to *stop* rather than be played over. Two
`BackgroundAudioPlayer`s on one room publish two tracks under the same name,
and `aclose()` resolves the publication *by name*, so they can unpublish each
other. It is also the honest sound: hold is not the moment for the office
ambience that says "somebody is here".
"""

from __future__ import annotations

import logging
import time

from livekit.agents import BackgroundAudioPlayer, JobContext

from services import session_events
from workers.voice.background_audio import hold_audio_config
from workers.voice.runtime import VoiceRun

logger = logging.getLogger("talqing.workers.voice.hold")


class CallerHold:
    """The caller's leg while we dial. One use; `begin` then `end` exactly once.

    Both ends are traced. Hold is a stretch of the call where the caller hears
    music and nobody is listening to them, and it sits inside `duration_s`,
    inside the recording and inside the bill — so without `hold.started` /
    `hold.ended` every reader of a transferred call mistakes it for conversation.
    """

    def __init__(self, job: JobContext, run: VoiceRun) -> None:
        self._job = job
        self._run = run
        self._player: BackgroundAudioPlayer | None = None
        self._restore_io: tuple[bool, bool] | None = None
        self._started_at: float | None = None

    async def begin(self) -> None:
        await self._run.stop_background_audio()
        self._started_at = time.monotonic()
        self._run.events.record(session_events.HOLD_STARTED)

        session = self._run.session
        self._restore_io = (session.input.audio_enabled, session.output.audio_enabled)
        # Muting the microphone is not only about the agent staying quiet: an
        # unmuted leg bills speech-to-text for every second of a thirty-second
        # ring, on every bridged transfer, to hear a caller say "hello?" into a
        # turn nobody is listening for.
        session.input.set_audio_enabled(False)
        session.output.set_audio_enabled(False)

        player = BackgroundAudioPlayer(ambient_sound=hold_audio_config(self._run.cfg))
        await player.start(room=self._job.room)
        self._player = player

    async def end(self, *, restore: bool) -> None:
        """Stop the music, and give the caller back to the agent if `restore`.

        `restore=False` is the transfer having landed: the session is about to
        be shut down and the two humans own the room, so the one thing that
        still matters is that the music stops before they start talking.
        """
        player, self._player = self._player, None
        if player is not None:
            try:
                await player.aclose()
            except Exception:
                logger.exception("hold audio failed to stop")
        started_at, self._started_at = self._started_at, None
        self._run.events.record(
            session_events.HOLD_ENDED,
            {
                "duration_ms": (
                    None if started_at is None else round((time.monotonic() - started_at) * 1000, 1)
                ),
                # False is the transfer having landed: the caller went to a
                # person, not back to the agent, and the call ends here.
                "returned_to_agent": restore,
            },
        )
        if not restore:
            return
        if self._restore_io is not None:
            audio_in, audio_out = self._restore_io
            self._restore_io = None
            self._run.session.input.set_audio_enabled(audio_in)
            self._run.session.output.set_audio_enabled(audio_out)
        # A player cannot be reopened after `aclose()`, so the ambient bed comes
        # back as a new one.
        await self._run.start_background_audio(self._job)
