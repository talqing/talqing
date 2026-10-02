"""The person's screen, on a web voice or video call.

Two consumers of one subscription, and they are deliberately independent.

**The agent** is handed the single newest frame on each user turn, on a copy of
the context that is thrown away after the turn (`compiler/compile.py`). So the
model sees exactly one image, forever: the context does not grow, there is
nothing to prune, and no API is needed to manage it. That is why the buffer here
is *peeked* and never consumed — a screen nobody is typing on produces no new
frames at all (the encoder has nothing to send), and it is still the answer.
`None` therefore means "not sharing" and never "nothing new happened", which is
what lets the compiler's absent-screen note be true.

**The recording** — only when the author asked for it — is a second file of the
call, written at 1 fps for the whole call whether or not anyone is sharing.
Never driven from the turn hook: its timeline is wall-clock and the hook's is
turn-shaped, so a call with no turns for two minutes would write no video for
two minutes and everything after it would drift.

LiveKit's own video input (`RoomOptions(video_input=True)`) is not used, and the
reason is worth knowing before changing any of this: it routes frames to
`AgentActivity.push_video`, which forwards to the realtime session and does
nothing otherwise. On the cascade pipeline we run it is a silent no-op — no
error, no log.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path

import aiofiles
import av
import numpy as np
from livekit import rtc

from services import recordings, session_events
from services.recordings import RecordingStatus
from services.user import Tenant
from workers.session.events import SessionEventLog
from workers.voice.recording import (
    UPLOAD_MIN_THROUGHPUT_MB_S,
    UPLOAD_TIMEOUT_BASE_SECONDS,
    upload_timeout_seconds,
)

logger = logging.getLogger("talqing.workers.voice.screenshare")

# The recording's frame size. 720p is what the agent itself is shown (the frame
# is resized to a 1280px box before it reaches the model), so the replay answers
# "what could it actually see?" rather than showing detail the model never had.
# Fixed rather than taken from the first frame because the writer starts with
# the call — before any share exists — and because a second share of a different
# window must not need a new container.
FRAME_WIDTH = 1280
FRAME_HEIGHT = 720
# One frame a second: enough to follow what happened, and cheap enough that the
# encode is ~1% of a core (measured at ~9ms per 1080p frame, worst case).
FRAME_INTERVAL_S = 1.0
# Seconds between keyframes, i.e. 30 frames. Short enough that seeking to a turn
# lands quickly, long enough not to dominate the file.
GOP_SECONDS = 30
# `crf` sets the quality; `maxrate` is the only thing that bounds the FILE, and
# it is what makes the finalize budget below arithmetic rather than a hope.
# Measured at 1 fps, 720p: a static editor is ~20 kbps and one scrolling
# continuously for the whole call is ~39 kbps, so 80 kbps barely binds on screen
# content and only really bites on a shared video, where softness is the right
# trade against an unbounded upload.
ENCODER_OPTIONS = {
    "crf": "28",
    "preset": "veryfast",
    "tune": "stillimage",
    "maxrate": "80k",
    "bufsize": "160k",
}
# The ceiling above over the longest call the platform allows (3h), which is what
# `VoiceRun.FINALIZE_SHUTDOWN_TIMEOUT_SECONDS` has to contain. Deliberately the
# same number the audio budgets for: two artifacts of one call should not be able
# to hold a job process open for wildly different lengths of time.
MAX_SCREEN_RECORDING_MB = 110.0
MAX_UPLOAD_TIMEOUT_SECONDS = (
    UPLOAD_TIMEOUT_BASE_SECONDS + MAX_SCREEN_RECORDING_MB / UPLOAD_MIN_THROUGHPUT_MB_S
)


# ── the live frame ──────────────────────────────────────────────────────────


class ScreenshareWatcher:
    """The newest frame of one person's screen share, and nothing else.

    Constructed before `VoiceRun.prepare` so its `peek` can be handed to the
    compiled agent through the runtime context, and attached to the room after
    `ctx.connect()`.
    """

    def __init__(self, participant_identity: str) -> None:
        self._participant_identity = participant_identity
        self._room: rtc.Room | None = None
        self._frame: rtc.VideoFrame | None = None
        self._stream: rtc.VideoStream | None = None
        self._publication: rtc.RemoteTrackPublication | None = None
        self._forward_task: asyncio.Task[None] | None = None
        self._events: SessionEventLog | None = None
        # Monotonic start of the current share, and how many frames it delivered
        # — the pair `screenshare.stopped` reports, which is where "this client
        # published at 15 fps" becomes visible in support.
        self._share_started_at: float | None = None
        self._frames_received = 0
        self._closing: set[asyncio.Task[None]] = set()

    def peek(self) -> rtc.VideoFrame | None:
        """The newest frame, left in place. Never a pop — see the module docstring."""
        return self._frame

    def attach(self, room: rtc.Room, *, events: SessionEventLog) -> None:
        """Subscribe to this person's screen share, now and whenever it appears.

        `ctx.connect()` asked for AUDIO_ONLY, which is not a server-side mode:
        LiveKit translates it to "subscribe to nothing", then opts each audio
        publication in as it sees it (`job.py::_apply_auto_subscribe_opts`). This
        adds a second opt-in beside theirs with one predicate changed, so there
        is no window in which we are subscribed to something we did not want.

        That predicate is the whole safety story. It cannot reach the caller's
        microphone (wrong source), it cannot reach the Anam avatar (which
        publishes as SOURCE_CAMERA), and it cannot reach a second participant —
        which is also why the identity is required at construction rather than
        defended against here: without one the predicate would match nothing and
        the call would run with an agent that says it can see and never does.
        """
        self._events = events
        self._room = room

        room.on("track_published", self._on_track_published)
        room.on("track_subscribed", self._on_track_subscribed)
        room.on("track_unpublished", self._on_track_unpublished)
        room.on("participant_disconnected", self._on_participant_disconnected)

        # Anything published before we got here. LiveKit sweeps the same way for
        # the same reason: `track_published` only covers the future.
        participant = room.remote_participants.get(self._participant_identity)
        if participant is not None:
            for publication in participant.track_publications.values():
                self._on_track_published(publication, participant)
                if publication.track is not None:
                    self._on_track_subscribed(publication.track, publication, participant)

    async def aclose(self) -> None:
        """Stop reading. Safe to call whether or not anything was ever shared.

        **No synthetic `screenshare.stopped`.** A share that was still up when
        the call ended reads as a `started` with no `stopped`, which is exactly
        what happened — and finalize has already drained the trace by the time
        this runs, so an event recorded here would be racing the process exit. A
        person hanging up mid-share still gets a real stop, from
        `participant_disconnected`.
        """
        room, self._room = self._room, None
        if room is not None:
            room.off("track_published", self._on_track_published)
            room.off("track_subscribed", self._on_track_subscribed)
            room.off("track_unpublished", self._on_track_unpublished)
            room.off("participant_disconnected", self._on_participant_disconnected)
        self._stop_share(record_event=False)
        if self._closing:
            await asyncio.gather(*self._closing, return_exceptions=True)

    # ── room events ─────────────────────────────────────────────────────────

    def _wanted(
        self, publication: rtc.RemoteTrackPublication, participant: rtc.RemoteParticipant
    ) -> bool:
        return (
            participant.identity == self._participant_identity
            and publication.source == rtc.TrackSource.SOURCE_SCREENSHARE
        )

    def _on_track_published(
        self, publication: rtc.RemoteTrackPublication, participant: rtc.RemoteParticipant
    ) -> None:
        if self._wanted(publication, participant):
            publication.set_subscribed(True)

    def _on_track_subscribed(
        self,
        track: rtc.RemoteTrack,
        publication: rtc.RemoteTrackPublication,
        participant: rtc.RemoteParticipant,
    ) -> None:
        if not self._wanted(publication, participant):
            return
        if self._publication is not None and self._publication.sid == publication.sid:
            return
        # A candidate who stops sharing and shares a different window arrives
        # here as a new publication. Close the old stream before opening the new
        # one; never hold two.
        self._stop_share(record_event=True)
        # `from_track`, not `from_participant(track_source=…)`, even though the
        # latter looks simpler: LiveKit's own participant input stream manages
        # republish by hand for the same reason, and copying the shape that is
        # proven here beats betting on FFI behaviour nobody has measured. The
        # ring queue of one drops stale frames at the consumer — it does not stop
        # the per-frame copy the SDK makes on delivery, which is what
        # `screenshare.stopped`'s frame count is there to expose.
        self._stream = rtc.VideoStream.from_track(track=track, capacity=1)
        self._publication = publication
        self._share_started_at = time.monotonic()
        self._frames_received = 0
        self._forward_task = asyncio.create_task(
            self._forward(self._stream), name="talqing_screenshare_forward"
        )
        self._record(
            session_events.SCREENSHARE_STARTED,
            {
                "track_sid": publication.sid,
                "width": publication.width,
                "height": publication.height,
                # vp9 and av1 make `livekit-client` force `contentHint: motion`,
                # which preserves smoothness at the cost of the spatial detail
                # this feature is entirely about. If a tenant reports the agent
                # misreading code, this is the first thing to look at.
                "codec": publication.mime_type,
            },
        )

    def _on_track_unpublished(
        self, publication: rtc.RemoteTrackPublication, participant: rtc.RemoteParticipant
    ) -> None:
        if self._publication is None or self._publication.sid != publication.sid:
            return
        if participant.identity != self._participant_identity:
            return
        self._stop_share(record_event=True)

    def _on_participant_disconnected(self, participant: rtc.RemoteParticipant) -> None:
        if participant.identity == self._participant_identity:
            # Closing the browser tab mid-share never unpublishes anything, so
            # without this the call would end with sharing apparently still on.
            self._stop_share(record_event=True)

    # ── internals ───────────────────────────────────────────────────────────

    async def _forward(self, stream: rtc.VideoStream) -> None:
        try:
            async for event in stream:
                self._frame = event.frame
                self._frames_received += 1
        except asyncio.CancelledError:
            return  # our own teardown
        except Exception:
            logger.exception("screen share stream stopped")

    def _stop_share(self, *, record_event: bool) -> None:
        """Forget the current share. Idempotent, and synchronous by necessity —
        every caller is a LiveKit event callback, which cannot await."""
        if self._stream is None:
            return
        stream, self._stream = self._stream, None
        publication, self._publication = self._publication, None
        if self._forward_task is not None:
            self._forward_task.cancel()
            self._forward_task = None
        task = asyncio.create_task(stream.aclose(), name="talqing_screenshare_close")
        self._closing.add(task)
        task.add_done_callback(self._closing.discard)
        # Cleared, so the agent is told it cannot see rather than answering
        # confidently about a screen that is gone. This is the whole reason the
        # buffer can be peeked instead of popped.
        self._frame = None
        if record_event:
            started_at = self._share_started_at
            self._record(
                session_events.SCREENSHARE_STOPPED,
                {
                    "track_sid": publication.sid if publication else None,
                    "duration_ms": (
                        round((time.monotonic() - started_at) * 1000)
                        if started_at is not None
                        else None
                    ),
                    "frames": self._frames_received,
                },
            )
        self._share_started_at = None
        self._frames_received = 0

    def _record(self, type_: str, payload: dict[str, object]) -> None:
        if self._events is not None:
            self._events.record(type_, payload)


# ── the recording ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ScreenshareOutcome:
    status: RecordingStatus
    object_key: str | None = None
    started_at: datetime | None = None
    duration_s: int | None = None
    size_bytes: int | None = None


class ScreenshareRecorder:
    """One 1 fps H.264 file of what was on screen, for the whole call.

    **Always write, every second, from the start of the call to finalize** —
    whether or not anyone is sharing, black when nobody is. There is no paused
    state, so there is no gap to fill and no resume path to get wrong. Sharing
    that starts nine minutes in is 540 black frames and then the screen; sharing
    that stops and restarts is a content change and nothing else; a call that
    ends while nobody is sharing produces a file exactly as long as the call.

    **`pts` is derived from elapsed wall time, never incremented.** A counter
    drifts exactly when the event loop is busy, which is when calls are hard. A
    derived `pts` cannot drift, and a late or dropped tick self-corrects on the
    next one — which is what keeps the video lined up with the transcript, whose
    offsets are measured from the same origin.

    An hour of nobody sharing costs kilobytes: H.264 encodes a repeated black
    frame as a P-frame with essentially no residual. "Always write" buys the
    removal of every edge case above for almost nothing.
    """

    def __init__(self, watcher: ScreenshareWatcher, path: Path) -> None:
        self._watcher = watcher
        self._path = path
        self._container: av.container.OutputContainer | None = None
        self._stream: av.video.stream.VideoStream | None = None
        self._black: av.VideoFrame | None = None
        self._task: asyncio.Task[None] | None = None
        self._started_at: datetime | None = None
        self._origin: float = 0.0
        # Whether a real screen ever reached the file. A call where nobody shared
        # is the normal case for an agent with this on, and storing an hour of
        # black would tell a reader nothing while costing a purge.
        self._saw_screen = False
        self._failed = False
        # The encode runs in a worker thread, and `stop()` cancels the tick task
        # without waiting for a thread already inside `_write` — cancelling an
        # `asyncio.to_thread` awaitable does not stop the thread. So the muxer is
        # guarded rather than assumed idle: whichever of the two gets here first
        # wins, and a late frame lands on a closed container as a no-op instead
        # of a data race.
        self._muxing = threading.Lock()
        self._closed = False
        # pts must be strictly increasing for the encoder. Derived time makes
        # that true a second at a time, but a tick that overran its slot could
        # land in the same millisecond as the next one.
        self._last_pts_ms = -1

    @property
    def started_at(self) -> datetime | None:
        return self._started_at

    def start(self) -> None:
        """Open the container and begin ticking. Never raises into the call."""
        try:
            container = av.open(str(self._path), mode="w")
            stream = container.add_stream("libx264", rate=round(1 / FRAME_INTERVAL_S))
            stream.width = FRAME_WIDTH
            stream.height = FRAME_HEIGHT
            stream.pix_fmt = "yuv420p"
            # Milliseconds, so a tick that lands slightly late keeps its real
            # position instead of being rounded onto the second it missed.
            stream.codec_context.time_base = Fraction(1, 1000)
            stream.gop_size = round(GOP_SECONDS / FRAME_INTERVAL_S)
            stream.options = dict(ENCODER_OPTIONS)
        except Exception:
            logger.exception("screen recording could not be opened; the call is unaffected")
            self._failed = True
            return
        self._container = container
        self._stream = stream
        # Built from a zeroed RGB canvas rather than `av.VideoFrame(w, h, fmt)`,
        # which allocates without initializing — all-zero yuv420p is bright
        # green, not black. Black, and never the last frame held: showing a
        # screen that is no longer being shared is a lie a player tells
        # confidently.
        self._black = _to_encoder_frame(np.zeros((FRAME_HEIGHT, FRAME_WIDTH, 3), dtype=np.uint8))
        self._started_at = datetime.now(UTC)
        # Wall clock for the origin the transcript is measured against; monotonic
        # for the arithmetic, so an NTP step mid-call cannot skew the file.
        self._origin = time.monotonic()
        self._task = asyncio.create_task(self._tick(), name="talqing_screenshare_record")

    async def stop(self) -> None:
        """Stop ticking and flush the encoder. Call before reading the file."""
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._container is not None:
            try:
                await asyncio.to_thread(self._close)
            except Exception:
                logger.exception("screen recording could not be closed")
                self._failed = True
            self._container = None
            self._stream = None

    # ── internals ───────────────────────────────────────────────────────────

    async def _tick(self) -> None:
        n = 0
        while True:
            elapsed = time.monotonic() - self._origin
            frame = self._watcher.peek()
            if frame is not None:
                self._saw_screen = True
            try:
                await asyncio.to_thread(self._write, frame, round(elapsed * 1000))
            except asyncio.CancelledError:
                raise
            except Exception:
                # One bad frame must never end the call, and must not end the
                # recording either — the next tick is a second away.
                logger.warning("screen recording dropped a frame", exc_info=True)
            n += 1
            await asyncio.sleep(max(0.0, self._origin + n * FRAME_INTERVAL_S - time.monotonic()))

    def _write(self, frame: rtc.VideoFrame | None, pts_ms: int) -> None:
        """Encode one tick. Runs off the event loop — the worker is CPU-bound at
        1 vCPU and a 1080p frame costs ~9ms through this path."""
        picture = self._black if frame is None else _fit(frame)
        with self._muxing:
            # Bound to locals inside the lock: `stop()` clears both attributes
            # from the event loop, and it does not hold this.
            container, stream = self._container, self._stream
            if self._closed or container is None or stream is None or picture is None:
                return
            picture.pts = max(pts_ms, self._last_pts_ms + 1)
            picture.time_base = Fraction(1, 1000)
            self._last_pts_ms = picture.pts
            for packet in stream.encode(picture):
                container.mux(packet)

    def _close(self) -> None:
        with self._muxing:
            container, stream = self._container, self._stream
            if self._closed or container is None or stream is None:
                return
            self._closed = True
            for packet in stream.encode(None):
                container.mux(packet)
            container.close()

    async def finish(self, tenant: Tenant, session_id: str) -> ScreenshareOutcome:
        """Stop, and put the file in the bucket if there is anything worth keeping.

        Runs inside finalize because the file lives in the job process's
        temporary directory and is gone once that process exits.
        """
        await self.stop()
        if self._failed:
            return ScreenshareOutcome(status=RecordingStatus.FAILED)
        if not self._saw_screen:
            # Not FAILED and not NONE: screen recording was on and worked, and
            # nobody shared. One of those three sends a reader to change a
            # setting and the others do not.
            return ScreenshareOutcome(status=RecordingStatus.NOT_SHARED)
        if self._started_at is None or not self._path.exists():
            logger.error("session %s: screen recording produced no file", session_id)
            return ScreenshareOutcome(status=RecordingStatus.FAILED)

        size_bytes = self._path.stat().st_size
        if size_bytes == 0:
            logger.error("session %s: screen recording file is empty", session_id)
            return ScreenshareOutcome(status=RecordingStatus.FAILED, started_at=self._started_at)

        key = recordings.screenshare_object_key(tenant_id=tenant.id, session_id=session_id)
        try:
            duration_s = await asyncio.to_thread(_probe_duration_s, self._path)
            # Streamed, never `read_bytes()`, for the reason the audio upload
            # states: the job process is memory-capped and several calls can be
            # finalizing at once.
            async with aiofiles.open(self._path, "rb") as fileobj:
                await asyncio.wait_for(
                    recordings.upload_object(
                        key=key,
                        fileobj=fileobj,
                        content_type=recordings.SCREENSHARE_CONTENT_TYPE,
                    ),
                    timeout=upload_timeout_seconds(size_bytes),
                )
        except Exception:
            # Timeout included. A slow bucket must never hold a session open: the
            # row says 'failed', which is queryable, and the call finishes.
            logger.exception("session %s: screen recording upload failed", session_id)
            return ScreenshareOutcome(status=RecordingStatus.FAILED, started_at=self._started_at)

        return ScreenshareOutcome(
            status=RecordingStatus.STORED,
            object_key=key,
            started_at=self._started_at,
            duration_s=duration_s,
            size_bytes=size_bytes,
        )


def _fit(frame: rtc.VideoFrame) -> av.VideoFrame:
    """One captured frame, scaled into the recording's box without distortion.

    Aspect-fit onto black rather than a stretch: a shared 4:3 window squeezed
    into 16:9 is a replay that misrepresents what was on screen, and letterboxing
    is two slices and a scale.
    """
    rgb = frame.convert(rtc.VideoBufferType.RGB24)
    source = np.frombuffer(rgb.data, dtype=np.uint8).reshape(rgb.height, rgb.width, 3)
    scale = min(FRAME_WIDTH / rgb.width, FRAME_HEIGHT / rgb.height)
    # Even dimensions: yuv420p subsamples chroma by two in both directions.
    width = max(2, int(rgb.width * scale)) & ~1
    height = max(2, int(rgb.height * scale)) & ~1
    scaled = av.VideoFrame.from_ndarray(source, format="rgb24").reformat(width=width, height=height)
    canvas = np.zeros((FRAME_HEIGHT, FRAME_WIDTH, 3), dtype=np.uint8)
    top = (FRAME_HEIGHT - height) // 2
    left = (FRAME_WIDTH - width) // 2
    canvas[top : top + height, left : left + width] = scaled.to_ndarray(format="rgb24")
    return _to_encoder_frame(canvas)


def _to_encoder_frame(canvas: np.ndarray) -> av.VideoFrame:
    """A full-size RGB canvas as the yuv420p frame libx264 takes."""
    return av.VideoFrame.from_ndarray(canvas, format="rgb24").reformat(format="yuv420p")


def _probe_duration_s(path: Path) -> int | None:
    """Real container duration, so the number we show matches the player's."""
    try:
        with av.open(str(path)) as container:
            if container.duration is None:
                return None
            return round(container.duration / av.time_base)
    except Exception:
        logger.warning("could not probe screen recording duration at %s", path, exc_info=True)
        return None
