"""A `RecorderIO` that can stop recording without ending the recording.

An agent's `recording.enabled` follows whichever agent is speaking, so a handoff
into an agent with recording off has to silence the recorder for as long as that
agent holds the call — and then hand it back. `RecorderIO` has no pause API, but
it is one property away from one:

**`aclose()` is terminal.** It flushes the timeline, pushes `None` onto the queue
and leaves `with container:`, which finalises the Ogg. A later `start()` would
`av.open(..., mode="w")` over the same path and truncate everything already
written. So stop/start is not the route, and a handoff back would otherwise
produce a file that ends early with nothing saying why.

**But every accumulation site is gated on one public property.**
`RecorderIO.recording` is read by `RecorderAudioInput.__anext__` before it
forwards a frame, and by `RecorderAudioOutput`'s `capture_frame`,
`on_playback_progressed` and `on_playback_finished` before they place one.
Overriding it turns all four off at once while the container stays open and
frames keep flowing untouched to STT, VAD and the caller — nothing about the
call changes except what is written.

**The timeline takes care of itself.** Since livekit-agents 1.7, the encoder
writes an absolute timeline anchored at `start()`: each channel places its audio
at the wall time it happened and `_Track.take` returns silence wherever nothing
was placed. The writer flushes every `WRITE_INTERVAL` whether audio arrived or
not, so a paused stretch is written as real silence as it elapses, and a call
that ends while paused keeps its full length. An item's offset into the
recording stays `created_at - recording_started_at` throughout, which is what
transcript-synced seeking needs.

**Known limit: an utterance straddling the pause is dropped.** A segment still
playing when recording resumes reports its progress as an offset into the whole
utterance, while only the part after the resume was captured — so
`RecorderAudioOutput` finds nothing at that offset and writes nothing for it.
The stretch reads as silence, correctly placed. Handoffs land between turns, so
this needs the previous agent to still be speaking as the next one enters.
"""

from __future__ import annotations

import logging

from livekit.agents.voice.recorder_io import RecorderIO

logger = logging.getLogger("talqing.workers.voice.recorder")


class TalqingRecorderIO(RecorderIO):
    """`RecorderIO`, plus pause/resume. Substituted for it in `workers.voice.main`."""

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._paused = False

    @property
    def recording(self) -> bool:
        return super().recording and not self._paused

    @property
    def paused(self) -> bool:
        return self._paused

    def pause_recording(self) -> None:
        """Stop writing audio. The container stays open and the call is untouched.

        Named `pause_recording` rather than `pause` deliberately:
        `RecorderAudioOutput` inherits `pause()`/`resume()`, and those mean
        *playback* pause — a different thing on an adjacent object.
        """
        if self._paused or not super().recording:
            return
        self._paused = True
        logger.info("recording paused")

    def resume_recording(self) -> None:
        """Start writing again. The gap is already silence in the file."""
        if not self._paused:
            return
        self._paused = False
        logger.info("recording resumed")
