"""One stream call: a partner's WebSocket ⇄ a LiveKit room.

The gateway joins that call's room as an ordinary participant — it publishes an
`AudioSource` fed by the partner's audio and subscribes to the agent's track
through an `AudioStream`. The room then contains exactly what `run_web_room`
already expects (a participant with an audio track), and `voice-worker` runs the
call on `VoiceRun` untouched.

**Why a bridge rather than an AgentSession on the socket.** Running the session
here would be lower latency and would give a *true* playback position from the
partner's own `mark` echoes. It would also lose per-call process isolation (every
concurrent call of a contact centre in one event loop, with one crash radius),
and it would mean rebuilding recording and DTMF, both of which `AgentSession`
wires only when there is a job context and a room. This way a stream call gets
recording, post-call analysis, billing, metrics, prompt caching and noise
cancellation for free, and DTMF on the same LiveKit primitive as a phone call.

The one thing it does **not** get is the ambient/thinking bed: that is a second
published track and this socket carries one stream of bytes, so
`workers/voice/stream.py` does not start it. See `_wire_room` below.

**The hop is real and it is the one we already pay.** Gateway → SFU → worker
costs an Opus encode and decode, but carrier → Kamailio → livekit-sip → SFU →
worker is the same shape with the same transcode — so a stream call has the same
latency profile as a Talqing phone call, which is a bar we can state.

**The partner paces the way in; the SFU paces the way out.** Every one of these
protocols already sends caller audio at 20 ms, so the inbound path must never push
back on it. The socket is read by one task from accept to close, and audio the
room cannot take promptly is late and dropped (`_to_room`). Queued instead, any
stall — a slow dispatch, a retransmit on the partner's link, a busy event loop —
becomes delay the caller carries for the rest of the call, because a paced stream
never slows down to let a backlog drain. On the way out, `AudioStream` delivers
20 ms frames at real time, so forwarding each frame as it arrives *is* the 20 ms
send loop the protocols ask for. Do not "optimise" it by draining `AudioStream`
into a queue and flushing: bursting is what makes a partner's bounded inbound
channel drop audio with no error, and the symptom is clipped speech with clean
logs on both sides.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import secrets as pysecrets
import time
from typing import Any
from uuid import uuid4

from livekit import api as lk_api
from livekit import rtc
from starlette.websockets import WebSocket, WebSocketDisconnect, WebSocketState

from api.stream import media
from services import close_reasons, credits, streams
from services.streams import signals
from services.streams.dialects import (
    Audio,
    AudioFormat,
    Cleared,
    Connected,
    Dtmf,
    Ended,
    Error,
    Ignored,
    Marked,
    ProtocolError,
    Started,
    StreamDialect,
    UnsupportedCodecError,
)
from services.system_vars import validate_session_vars
from services.telephony import livekit_sip
from services.user import Tenant, load_tenant
from settings import get_settings

logger = logging.getLogger("talqing.api.stream")

# How long we wait for the platform's `start` before giving up. Generous, because
# what it bounds is a socket that opened and then said nothing at all — a partner
# health check, a scanner, or a call their side abandoned between our accept and
# their answer. Every real protocol sends `start` within a ring.
_START_TIMEOUT_SECONDS = 30.0

# WebSocket close 1012, "service restart". uvicorn sends exactly this to every
# live socket when the process is asked to stop, and there is no drain to wait
# behind it — the gateway binds a port, so holding one would refuse new calls for
# as long as it held, and a refused connect on this channel is a dropped call the
# partner cannot see either. So a deploy ends live stream calls, and the only
# thing that matters is that the call says WE ended it: `stream_platform_hangup`
# would file it as a completion and charge the platform fee for a call we cut.
_SERVER_RESTART_CODE = 1012

# How far behind real time the outbound path may fall before it is worth a row on
# the call's trace. The caller hears exactly this as silence in the middle of the
# agent speaking, and nothing else on either side reports it: the partner's
# channel drops what it cannot take without an error, and our own logs stay clean.
_UNDERRUN_DRIFT_SECONDS = 0.1

# The most caller audio the room's source may already hold before a new frame is
# late and dropped. It is the trade between the two things a network stall costs:
# after a stall, delay settles at up to this much for the rest of the call, and a
# lower ceiling throws away more of the burst instead. Well above ordinary jitter,
# so a healthy call never loses a frame.
_MAX_INBOUND_QUEUE_SECONDS = 0.15


# What the gateway publishes into the room. Random per call so two sockets can
# never collide on an identity, and prefixed so a room's participant list reads
# as itself in the LiveKit dashboard.
def _gateway_identity() -> str:
    return f"stream-gw-{pysecrets.token_hex(6)}"


class StreamCallRefused(Exception):
    """The call cannot run, and we know why. Carries the close reason to record."""

    def __init__(self, close_reason: str, message: str) -> None:
        super().__init__(message)
        self.close_reason = close_reason
        self.message = message


class StreamBridge:
    """One socket, one room, one call."""

    def __init__(
        self,
        *,
        websocket: WebSocket,
        dialect: StreamDialect,
        connection: streams.StreamConnection,
        pool: Any,
    ) -> None:
        self._ws = websocket
        self._dialect = dialect
        self._connection = connection
        self._pool = pool

        self._session_id = str(uuid4())
        self._identity = _gateway_identity()
        self._fmt: AudioFormat | None = None
        self._started: Started | None = None
        self._parties: streams.CallParties | None = None
        self._stream_id: str | None = None
        # Set once the call has declared itself: a `Started` and an audio format.
        self._call_started = asyncio.Event()
        # The partner's own `end`. Its reason is theirs and goes on the trace.
        self._partner_end: Ended | None = None
        # Caller audio dropped as late since the last frame that made it in.
        self._dropped_seconds = 0.0
        # Keys pressed before the agent was in the room to hear them. An
        # impatient caller types over the first ring, and a data packet
        # published into a room nobody has joined goes nowhere — measured:
        # `4821#` arrived as `21`. Held until the agent's track is subscribed,
        # which is the first moment there is anything to publish to.
        self._pending_dtmf: list[str] = []

        self._room: rtc.Room | None = None
        self._source: rtc.AudioSource | None = None
        self._close_reason: str | None = None
        # Whole 20 ms frames waiting to reach the platform's minimum message
        # size. Empty on four of the five dialects.
        self._outbound: bytearray = bytearray()
        self._agent_identity: str | None = None
        self._ended = asyncio.Event()
        # Tasks spawned from LiveKit's synchronous event callbacks. Held so the
        # event loop cannot garbage-collect one mid-flight, and so teardown stops
        # them rather than leaving a pump writing into a closed socket.
        self._tasks: set[asyncio.Task[None]] = set()

    # ── lifecycle ───────────────────────────────────────────────────────────

    async def run(self) -> None:
        """Accept, dispatch, bridge, and close — the whole life of one call.

        Ordering rule, and it is not "accept as fast as possible": **bounded work
        before the accept, unbounded work after it.** Resolving the connection is
        one indexed read and happens in `main.py` before the handshake, so a URL
        that names nothing is an HTTP status a partner can see in their own logs
        rather than a socket that opens and closes. Everything from here on has a
        network dependency we do not control — `load_tenant` is an uncached call
        to the control plane in another region, with a 5 s timeout of its own,
        which is the partner's entire accept budget — so it all happens after.

        The socket itself is read from the accept until it closes, by
        `_read_socket` alone. None of the steps below ever stands between the
        partner and that loop: a paced stream left unread piles up, and a pile-up
        fed to the room at real time never drains.
        """
        await self._ws.accept(subprotocol=None)
        reader = asyncio.create_task(self._read_socket(), name="talqing_stream_reader")
        try:
            await self._until_started(reader)
            tenant = await self._admit()
            self._raise_if_partner_done(reader)
            await self._dispatch(tenant)
            await self._bridge(reader)
        except StreamCallRefused as refused:
            logger.warning(
                "refusing stream call: %s",
                refused.message,
                extra={"reason": refused.close_reason, "session": self._session_id},
            )
            self._close_reason = refused.close_reason
        except (ProtocolError, UnsupportedCodecError) as exc:
            reason = (
                close_reasons.STREAM_UNSUPPORTED_CODEC
                if isinstance(exc, UnsupportedCodecError)
                else close_reasons.STREAM_PROTOCOL_ERROR
            )
            logger.warning("stream protocol failure: %s", exc, extra={"reason": reason})
            self._close_reason = reason
            await self._publish(signals.SIGNAL_ENDED, reason=reason, detail=str(exc))
        except WebSocketDisconnect as disconnect:
            # The partner went away without an `end`. Their own spec says to
            # treat an unexpected close as end-of-call, and so do we — unless the
            # close came from OUR side, which is the one case where blaming them
            # would be both wrong and cheaper for us (see `_SERVER_RESTART_CODE`).
            ours = disconnect.code == _SERVER_RESTART_CODE
            logger.info(
                "stream socket closed by the %s",
                "gateway shutting down" if ours else "partner",
                extra={"session": self._session_id, "code": disconnect.code},
            )
            self._close_reason = self._close_reason or (
                close_reasons.STREAM_GATEWAY_RESTART
                if ours
                else close_reasons.STREAM_PLATFORM_HANGUP
            )
            await self._publish(signals.SIGNAL_ENDED, reason=self._close_reason)
        except Exception:
            logger.exception("stream call failed", extra={"session": self._session_id})
            self._close_reason = close_reasons.STREAM_PROTOCOL_ERROR
            await self._publish(signals.SIGNAL_ENDED, reason=self._close_reason)
        finally:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
            await self._teardown()

    def _spawn(self, coro, name: str) -> None:
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _teardown(self) -> None:
        """Leave the room and close the socket, in that order and never raising.

        The room first: the worker's disconnect handler is what ends the session,
        and it should see us leave rather than time out waiting. On the one
        dialect that can hang the call up we say so first — everywhere else
        closing the socket is the whole vocabulary the protocol has, and the
        caller goes back to the partner's own flow.
        """
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        # Only when WE are ending it. A `hangup` sent after the partner already
        # told us the call is over is a command into a closing socket — and on a
        # protocol where a command they cannot parse vanishes without a trace,
        # sending one that means nothing is not free to reason about later.
        if self._dialect.can_hangup and self._close_reason != close_reasons.STREAM_PLATFORM_HANGUP:
            await self._send(self._dialect.hangup(self._stream_id))
        room, self._room = self._room, None
        if room is not None:
            with contextlib.suppress(Exception):
                await room.disconnect()
        if self._ws.client_state is WebSocketState.CONNECTED:
            with contextlib.suppress(Exception):
                await self._ws.close()

    # ── step 1: read the socket, and wait for the call to declare itself ────

    async def _read_socket(self) -> None:
        """Every message the partner sends, for as long as the socket is open.

        Returns when the partner says the call is over; raises on a disconnect or
        on a message the dialect cannot read. It waits on nothing but the socket
        and the room's own audio source, which is what keeps the partner's
        stream drained while the rest of the call is still being set up.

        The call declares itself with a format and a `Started`, which arrive on
        different events depending on the platform — SparkTG announces the format
        on `connected`, Twilio/Plivo/Exotel on `start`, Vonage on the one event
        that is both — which is why `fmt` is optional on each and the call counts
        as started only once it holds the pair. A second declaration mid-call is
        ignored rather than allowed to change the format under a live room.
        """
        while True:
            event = self._dialect.parse(await self._receive())
            if isinstance(event, Audio):
                await self._to_room(event.payload)
            elif isinstance(event, Dtmf):
                await self._publish_dtmf(event.digit)
            elif isinstance(event, Connected) and not self._call_started.is_set():
                self._fmt = event.fmt or self._fmt
            elif isinstance(event, Started) and not self._call_started.is_set():
                self._fmt = event.fmt or self._fmt
                if self._fmt is None:
                    raise ProtocolError(
                        f"{self._dialect.name} started a call without announcing an audio format"
                    )
                self._started = event
                self._parties = streams.resolve_parties(event, self._dialect)
                self._stream_id = event.stream_id
                self._call_started.set()
            elif isinstance(event, Ended):
                self._partner_end = event
                return
            elif isinstance(event, Error):
                if not self._call_started.is_set():
                    raise ProtocolError(f"{event.code}: {event.message}")
                self._signal(signals.SIGNAL_ERROR, code=event.code, message=event.message)
            elif isinstance(event, Cleared):
                self._signal(signals.SIGNAL_CLEARED)
            elif isinstance(event, (Marked, Ignored)):
                # A `mark` echo is a true playback position and we deliberately
                # do not use one: the worker's accounting comes from
                # `AudioSource`'s queue draining, which is the same inaccuracy
                # our SIP path already has. A mix of the two would be worse than
                # either. `Ignored` is forward compatibility, by contract.
                pass

    async def _until_started(self, reader: asyncio.Task[None]) -> None:
        """Wait for the call to declare itself, unless the socket ends first."""
        accepted_at = time.monotonic()
        started = asyncio.create_task(self._call_started.wait(), name="talqing_stream_started")
        try:
            await asyncio.wait(
                (reader, started),
                timeout=_START_TIMEOUT_SECONDS,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            started.cancel()
            await asyncio.gather(started, return_exceptions=True)
        self._raise_if_partner_done(reader)
        if not self._call_started.is_set():
            raise ProtocolError(
                f"{self._dialect.name} sent no start within {_START_TIMEOUT_SECONDS:.0f}s"
            )
        elapsed_ms = (time.monotonic() - accepted_at) * 1000
        deadline_ms = get_settings().telephony.streams.accept_deadline_ms
        if elapsed_ms > deadline_ms:
            # We cannot enforce their deadline — it is theirs — so this is the
            # only warning we will ever get before a partner reports "some calls
            # just don't connect". Their failure leaves no event and no error on
            # our side at all.
            logger.warning(
                "stream start took %.0fms, past the partner's %dms accept deadline",
                elapsed_ms,
                deadline_ms,
            )

    def _raise_if_partner_done(self, reader: asyncio.Task[None]) -> None:
        """Stop before an agent is dispatched if the socket has already ended.

        Re-raises whatever the reader stopped on. A reader that returned saw the
        partner's own `end`, and a call they ended before any agent joined is
        refused rather than dispatched.
        """
        if not reader.done():
            return
        reader.result()
        raise StreamCallRefused(
            close_reasons.STREAM_PLATFORM_HANGUP,
            "the platform ended the call before an agent joined",
        )

    async def _receive(self) -> str | bytes:
        message = await self._ws.receive()
        if message["type"] == "websocket.disconnect":
            raise WebSocketDisconnect(message.get("code", 1000))
        text = message.get("text")
        return text if text is not None else message["bytes"]

    # ── step 2: admit the call — the workspace, its agent, its credit ───────

    async def _admit(self) -> Tenant:
        tenant = await load_tenant(self._connection.tenant_id)
        if tenant is None:
            raise StreamCallRefused(
                close_reasons.STREAM_AGENT_UNRESOLVED, "the workspace no longer exists"
            )
        # Here and not only in the worker, because the worker's refusal happens
        # before it joins the room, so this process never hears it: the caller
        # would sit in silence until they hung up, with no call on record. A
        # connection may legitimately point at an agent that is not published yet
        # (`readiness: needs_agent`), so this is a state a tenant really reaches.
        published_version = await self._pool.fetchval(
            "SELECT published_version FROM agents WHERE id = $1 AND tenant_id = $2",
            self._connection.agent_id,
            tenant.id,
        )
        if published_version is None:
            await self._record_refused(tenant, close_reasons.STREAM_UNPUBLISHED_AGENT)
            raise StreamCallRefused(
                close_reasons.STREAM_UNPUBLISHED_AGENT,
                "the connection's agent has never been published",
            )
        if not await credits.has_credit(tenant):
            # The same visible row an inbound SIP call leaves when it is refused
            # for the same reason: the tenant has to be able to see in the
            # product why their partner integration stopped answering.
            await self._record_refused(tenant, credits.INSUFFICIENT_CREDITS_CLOSE_REASON)
            raise StreamCallRefused(
                credits.INSUFFICIENT_CREDITS_CLOSE_REASON, "the workspace is out of credits"
            )
        return tenant

    # ── step 3: room + dispatch ─────────────────────────────────────────────

    async def _dispatch(self, tenant: Tenant) -> None:
        assert self._started is not None and self._fmt is not None
        started = self._started

        if started.call_id and await self._is_duplicate(tenant, started.call_id):
            raise StreamCallRefused(
                close_reasons.STREAM_DUPLICATE_CALL,
                f"platform call {started.call_id} already has a session",
            )

        room_name = self._session_id
        metadata = self._dispatch_metadata(tenant, room_name)
        settings = get_settings()
        try:
            async with livekit_sip.lk_client() as lk:
                await lk.room.create_room(
                    lk_api.CreateRoomRequest(name=room_name, empty_timeout=60, metadata="")
                )
                await lk.agent_dispatch.create_dispatch(
                    lk_api.CreateAgentDispatchRequest(
                        agent_name=settings.livekit.agent_name,
                        room=room_name,
                        metadata=metadata,
                    )
                )
        except Exception as exc:
            logger.exception("LiveKit room/dispatch failed for stream call %s", self._session_id)
            with contextlib.suppress(Exception):
                async with livekit_sip.lk_client() as lk:
                    await lk.room.delete_room(lk_api.DeleteRoomRequest(room=room_name))
            raise StreamCallRefused(
                close_reasons.STREAM_DISPATCH_FAILED, f"could not dispatch an agent: {exc}"
            ) from exc

        await self._join(room_name)

    def _dispatch_metadata(self, tenant: Tenant, room_name: str) -> str:
        """What the worker is told about this call before it can see the room.

        Everything the gateway learned during the handshake travels here rather
        than as `session_events` rows of its own: `seq` comes from an in-process
        counter and a session has exactly one writer, so the worker emits
        `stream.connected` from this and the timeline stays one timeline.
        """
        assert self._started is not None and self._parties is not None and self._fmt is not None
        started = self._started
        parties = self._parties
        # Open key space by design (`validate_session_vars`), so the partner's
        # bag passes through verbatim and a prompt reads `{{vars.flowName}}` with
        # nothing to configure and nothing to get wrong. A key that could not be
        # a variable name is dropped rather than failing the call — and which
        # keys survived is on the trace, because "why is {{vars.x}} empty" has no
        # other answer.
        session_vars: dict[str, str] = {}
        skipped: list[str] = []
        for key, value in started.params.items():
            try:
                session_vars.update(validate_session_vars({key: value}))
            except ValueError:
                skipped.append(key)
        return json.dumps(
            {
                "kind": "stream_call",
                "tenant": str(tenant.id),
                "agent": str(self._connection.agent_id),
                "channel": "voice",
                "session": self._session_id,
                "room": room_name,
                "stream_connection_id": str(self._connection.id),
                "stream_connection_name": self._connection.name,
                "dialect": self._dialect.name,
                "gateway_identity": self._identity,
                # None where neither the protocol nor the partner said, and
                # deliberately not defaulted: see `build_call_fields`.
                "direction": parties.direction,
                "platform_call_id": started.call_id,
                "from_e164": parties.from_e164,
                "to_e164": parties.to_e164,
                "human_e164": parties.human_e164,
                "agent_e164": parties.agent_e164,
                "unusable_numbers": dict(parties.unusable_numbers),
                "codec": self._fmt.encoding,
                "sample_rate": self._fmt.sample_rate,
                # Whether the agent may reach for the keypad on this call is a
                # property of which platform is on the other end of the socket.
                "can_send_dtmf": self._dialect.can_send_dtmf,
                "vars": session_vars,
                "unusable_param_keys": skipped,
            }
        )

    async def _is_duplicate(self, tenant: Tenant, call_id: str) -> bool:
        """Has this platform call id already got a session?

        Checked BEFORE the room is created, not after: a retry or a redundant leg
        can open two sockets carrying one call id, and `uq_sessions_idempotency`
        would collapse the rows but leave two rooms, two dispatches, two agents
        and one confused caller.
        """
        row = await self._pool.fetchrow(
            "SELECT id FROM sessions WHERE tenant_id = $1 AND idempotency_key = $2 LIMIT 1",
            tenant.id,
            call_id,
        )
        return row is not None

    async def _join(self, room_name: str) -> None:
        assert self._fmt is not None
        settings = get_settings()
        token = (
            lk_api.AccessToken(settings.livekit.api_key, settings.livekit.api_secret)
            .with_identity(self._identity)
            .with_grants(
                lk_api.VideoGrants(
                    room_join=True,
                    room=room_name,
                    can_publish=True,
                    can_subscribe=True,
                    can_publish_data=True,
                )
            )
            .to_jwt()
        )
        room = rtc.Room()
        self._wire_room(room)
        await room.connect(
            settings.livekit.url, token, options=rtc.RoomOptions(auto_subscribe=True)
        )
        self._room = room

        # Built at THEIR sample rate. Upsampling μ-law 8 kHz to 16 kHz invents
        # nothing and costs CPU on every frame of every call; LiveKit handles
        # everything downstream.
        source = rtc.AudioSource(self._fmt.sample_rate, 1, queue_size_ms=200)
        self._source = source
        track = rtc.LocalAudioTrack.create_audio_track("caller", source)
        await room.local_participant.publish_track(
            track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
        )

    # ── step 4: bridge both directions ──────────────────────────────────────

    async def _bridge(self, reader: asyncio.Task[None]) -> None:
        """Forward until the call ends, whichever end ends it.

        Three ways out, and the cap is not optional: nothing else in the stack
        bounds a stream call, so a partner whose socket never closes would bill
        speech-to-text, LLM and text-to-speech indefinitely and hold a worker
        slot the whole time.
        """
        cap = get_settings().telephony.streams.max_call_duration_seconds
        ended = asyncio.create_task(self._ended.wait(), name="talqing_stream_ended")
        try:
            done, _ = await asyncio.wait(
                (reader, ended), timeout=cap, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            ended.cancel()
            await asyncio.gather(ended, return_exceptions=True)
        if not done:
            logger.warning("stream call %s hit the %ss cap", self._session_id, cap)
            self._close_reason = close_reasons.STREAM_MAX_DURATION
            await self._publish(signals.SIGNAL_ENDED, reason=self._close_reason)
        elif reader in done:
            # Re-raise whatever the reader stopped on, so `run` maps it. A reader
            # that returned saw the partner's own `end`.
            reader.result()
            assert self._partner_end is not None
            self._close_reason = close_reasons.STREAM_PLATFORM_HANGUP
            await self._publish(
                signals.SIGNAL_ENDED,
                reason=self._close_reason,
                detail=self._partner_end.reason,
                duration_ms=self._partner_end.duration_ms,
            )

    async def _to_room(self, payload: bytes) -> None:
        """One slice of their audio into the room — or nowhere, if it is late.

        Before the room exists nobody can hear it, so it is discarded. After, a
        source already holding more than `_MAX_INBOUND_QUEUE_SECONDS` means this
        slice is late — a burst after a stall, or the partner's clock running
        ahead of ours — and it is dropped: delivered, it would become delay the
        caller carries for the rest of the call. Each run of drops leaves one row
        on the trace, sent when audio flows again.
        """
        source = self._source
        if source is None:
            return
        assert self._fmt is not None
        if source.queued_duration > _MAX_INBOUND_QUEUE_SECONDS:
            samples = len(payload) // self._fmt.bytes_per_sample
            self._dropped_seconds += samples / self._fmt.sample_rate
            return
        pcm = media.decode(payload, self._fmt)
        samples = len(pcm) // 2
        if not samples:
            return
        if self._dropped_seconds:
            self._signal(
                signals.SIGNAL_INBOUND_DROPPED, dropped_ms=round(self._dropped_seconds * 1000)
            )
            self._dropped_seconds = 0.0
        await source.capture_frame(
            rtc.AudioFrame(
                data=pcm,
                sample_rate=self._fmt.sample_rate,
                num_channels=1,
                samples_per_channel=samples,
            )
        )

    async def _pump_outbound(self, track: rtc.Track) -> None:
        """The agent's track → their socket, one 20 ms frame at a time.

        `AudioStream` resamples and re-packetises for us, so every frame that
        comes out is already exactly 20 ms at exactly the rate they announced —
        which is why there is no resampler here and no send timer either. It is
        also real-time paced end to end, so forwarding as frames arrive *is* the
        20 ms loop.

        `capacity` is set because the default is unbounded: a stalled socket must
        not be able to grow memory without a ceiling.
        """
        assert self._fmt is not None
        stream = rtc.AudioStream(
            track,
            sample_rate=self._fmt.sample_rate,
            num_channels=1,
            frame_size_ms=20,
            capacity=200,
        )
        # Audio time forwarded, against wall clock since the first frame went
        # out. Their difference is the only honest measure of an underrun here:
        # the gap between arrivals is not, because `AudioStream` buffers and a
        # loop that stalls for 300 ms then drains four frames back to back shows
        # no gap at all while the caller heard 300 ms of nothing. Reported only
        # when it has GROWN by the threshold, so falling behind once leaves one
        # row rather than one per frame for the rest of the call.
        started: float | None = None
        forwarded = 0.0
        reported = 0.0
        try:
            async for event in stream:
                if started is None:
                    started = time.monotonic()
                await self._to_socket(bytes(event.frame.data))
                forwarded += event.frame.samples_per_channel / event.frame.sample_rate
                drift = (time.monotonic() - started) - forwarded
                if drift - reported >= _UNDERRUN_DRIFT_SECONDS:
                    reported = drift
                    self._signal(signals.SIGNAL_UNDERRUN, behind_ms=round(drift * 1000))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("stream outbound pump failed", extra={"session": self._session_id})
        else:
            # `AudioStream` can end on a partial frame, and a short frame is what
            # ends the call on the strict dialect. Padded here rather than
            # inside the loop, which is the one place a residue can outlive the
            # track it came from.
            await self._flush_outbound()
        finally:
            with contextlib.suppress(Exception):
                await stream.aclose()

    async def _to_socket(self, pcm: bytes) -> None:
        """Encode, size, and put one message on the wire.

        Written to the strictest dialect and relaxed per dialect, never the other
        way round: SparkTG ends the call on a single wrong-sized frame, everyone
        else buffers whatever they are sent. So the tail is always padded to a
        whole frame, and only the *grouping* varies — one frame per message
        except on Exotel, which refuses anything under 3200 bytes.
        """
        assert self._fmt is not None
        self._outbound += media.encode(pcm, self._fmt)
        frame_bytes = self._fmt.frame_bytes
        minimum = max(self._dialect.min_message_bytes, frame_bytes)
        while len(self._outbound) >= minimum:
            # Truncated to whole frames, which is what makes every message this
            # sends legal on the strictest dialect: `exact_frames` needs a whole
            # number of 20 ms frames and Exotel needs a multiple of 320 bytes,
            # and any multiple of `frame_bytes` satisfies both at every rate
            # these platforms offer. Relax this and SparkTG ends the call.
            take = len(self._outbound) - (len(self._outbound) % frame_bytes)
            chunk = bytes(self._outbound[:take])
            del self._outbound[:take]
            await self._send(
                self._dialect.audio_out(chunk, fmt=self._fmt, stream_id=self._stream_id)
            )

    async def _flush_outbound(self) -> None:
        """Pad and send whatever is left of an utterance. Never a short frame."""
        assert self._fmt is not None
        if not self._outbound:
            return
        chunk = media.pad_to_frame(bytes(self._outbound), self._fmt)
        self._outbound.clear()
        await self._send(self._dialect.audio_out(chunk, fmt=self._fmt, stream_id=self._stream_id))

    async def _send(self, message: str | bytes | None) -> None:
        if message is None or self._ws.client_state is not WebSocketState.CONNECTED:
            return
        try:
            if isinstance(message, str):
                await self._ws.send_text(message)
            else:
                await self._ws.send_bytes(message)
        except (WebSocketDisconnect, RuntimeError):
            # The socket went away mid-write. The inbound pump will see the same
            # thing and end the call; this must not become the failure that is
            # reported, because it is a symptom of one.
            self._ended.set()

    # ── the room side ───────────────────────────────────────────────────────

    def _wire_room(self, room: rtc.Room) -> None:
        @room.on("track_subscribed")
        def _on_track(
            track: rtc.Track,
            publication: rtc.RemoteTrackPublication,
            participant: rtc.RemoteParticipant,
        ) -> None:
            # The agent's voice, and only the agent's voice. A participant may
            # publish several audio tracks — `BackgroundAudioPlayer` publishes a
            # second one, and an avatar would be a third — and this socket carries
            # exactly one stream of bytes, so a second pump would interleave two
            # sources into one buffer: garbled audio everywhere, and on the strict
            # dialect a multi-frame message that ends the call. livekit-sip can
            # take them all because it mixes; we forward, so we pick.
            #
            # Which is also why `workers/voice/stream.py` does not start the
            # background bed at all: filtering it out here and publishing it there
            # would be two halves of one decision, disagreeing.
            if track.kind != rtc.TrackKind.KIND_AUDIO:
                return
            if publication.source != rtc.TrackSource.SOURCE_MICROPHONE:
                logger.info(
                    "ignoring a non-microphone audio track on a stream call",
                    extra={"session": self._session_id, "track": publication.name},
                )
                return
            if self._agent_identity is not None:
                # Two microphone tracks from the agent is not something the
                # runtime does; if it ever starts, the caller must not hear both.
                logger.error(
                    "a second microphone track appeared on a stream call; ignoring it",
                    extra={"session": self._session_id, "track": publication.name},
                )
                return
            self._agent_identity = participant.identity
            self._spawn(self._pump_outbound(track), "talqing_stream_outbound")
            # The first moment there is anyone to publish to.
            self._spawn(self._flush_dtmf(), "talqing_stream_flush_dtmf")

        @room.on("participant_disconnected")
        def _on_left(participant: rtc.RemoteParticipant) -> None:
            # The agent left, which on this channel means the call is over: the
            # worker shut its session down (`end_call`, an error, the job
            # draining). Ending here is what turns that into a hangup on the one
            # dialect that has one and a socket close on the other four.
            if participant.identity == self._agent_identity:
                self._ended.set()

        @room.on("disconnected")
        def _on_disconnected(*_: object) -> None:
            self._ended.set()

        @room.on("sip_dtmf_received")
        def _on_dtmf(event: rtc.SipDTMF) -> None:
            # The agent dialling a keypad. We do not hear our own packets, so the
            # two directions cannot loop.
            if not self._dialect.can_send_dtmf or not event.digit:
                return
            self._spawn(self._send_dtmf(event.digit), "talqing_stream_send_dtmf")

        @room.on("data_received")
        def _on_data(packet: rtc.DataPacket) -> None:
            if packet.topic != signals.STREAM_TOPIC:
                return
            kind, _ = signals.decode(packet.data)
            if kind == signals.SIGNAL_CLEAR:
                self._spawn(self._on_barge_in(), "talqing_stream_clear")

    async def _on_barge_in(self) -> None:
        """The agent's speech was cut off: stop sending, and say so.

        The chain is real-time paced end to end, so the far end's buffer holds
        milliseconds and an interruption is already heard as one. This is for the
        case pacing cannot cover — a network hiccup that lets a burst reach a
        buffer a `clear` genuinely purges, which Twilio's is and SparkTG's is
        not. Their own words: *"stop generating and stop sending audio on your
        side — that is what actually shortens what the caller hears."*
        """
        self._outbound.clear()
        await self._send(self._dialect.clear(self._stream_id))

    async def _send_dtmf(self, digits: str) -> None:
        for message in self._dialect.send_dtmf(self._stream_id, digits):
            await self._send(message)

    async def _publish_dtmf(self, digit: str) -> None:
        """Their keypad event → a first-class LiveKit DTMF packet.

        The same primitive livekit-sip publishes on a phone call, so the worker's
        collector listens to one event on one channel, and a stream keypad and a
        phone keypad cannot behave differently.

        Held back until the agent is actually in the room. A data packet
        published to a room with nobody else in it is not queued anywhere — it is
        simply gone — and the window between our joining and the agent's track
        arriving is exactly when an impatient caller types over the first ring.
        """
        room = self._room
        if room is None or self._agent_identity is None:
            self._pending_dtmf.append(digit)
            return
        try:
            code = _dtmf_code(digit)
        except ValueError:
            logger.warning("ignoring unreadable DTMF digit %r", digit)
            return
        with contextlib.suppress(Exception):
            await room.local_participant.publish_dtmf(code=code, digit=digit)

    async def _flush_dtmf(self) -> None:
        """Replay what the caller typed before anyone could hear it, in order."""
        pending, self._pending_dtmf = self._pending_dtmf, []
        for digit in pending:
            await self._publish_dtmf(digit)

    # ── talking to the worker ───────────────────────────────────────────────

    async def _publish(self, kind: str, **payload: Any) -> None:
        """Tell the worker something only we can see, and wait for it to go.

        Reliable delivery, because these become rows on the call's trace — and
        the `ended` signal decides the call's close reason, so it is the one
        thing here that must not be lost. Awaited rather than spawned for
        exactly that: teardown cancels every outstanding task, and a spawned end
        signal loses that race every time, leaving the call filed as a plain
        participant disconnect with the partner's own reason nowhere.
        """
        room = self._room
        if room is None:
            return
        with contextlib.suppress(Exception):
            await room.local_participant.publish_data(
                signals.encode(kind, **payload), reliable=True, topic=signals.STREAM_TOPIC
            )

    def _signal(self, kind: str, **payload: Any) -> None:
        """The same, for a mid-call trace row that nothing waits on."""
        self._spawn(self._publish(kind, **payload), "talqing_stream_signal")

    async def _record_refused(self, tenant: Tenant, close_reason: str) -> None:
        """Leave a visible, explained row for a call we turned away.

        Refusing at the door means no worker ever runs, so without this the call
        would exist only in gateway logs — the tenant would see a partner
        integration that stopped answering, with nothing in the product to say
        why. `billing_status = 'computed'` with no money: nothing was consumed.
        """
        assert self._started is not None and self._parties is not None
        try:
            await self._pool.execute(
                """
                INSERT INTO sessions (
                    id, tenant_id, agent_id, agent_name, channel, type, status, close_reason,
                    stream_connection_id, from_e164, to_e164, idempotency_key,
                    started_at, ended_at, billing_status
                )
                VALUES (
                    $1::uuid, $2, $3::uuid,
                    (SELECT name FROM agents WHERE id = $3::uuid AND tenant_id = $2),
                    'voice', 'STREAM', 'failed', $4,
                    $5::uuid, $6, $7, $8, now(), now(), 'computed'
                )
                -- No conflict target: the id is freshly minted and cannot
                -- collide, but `uq_sessions_idempotency` can — a partner that
                -- retries a call we already refused sends the same platform call
                -- id — and naming `id` would have guarded the one that never
                -- fires while letting the one that does raise.
                ON CONFLICT DO NOTHING
                """,
                self._session_id,
                tenant.id,
                str(self._connection.agent_id),
                close_reason,
                str(self._connection.id),
                self._parties.from_e164,
                self._parties.to_e164,
                self._started.call_id,
            )
        except Exception:
            logger.exception("failed to record refused stream call %s", self._session_id)


def _dtmf_code(digit: str) -> int:
    """RFC 4733 code for one key: 0-9, `*` is 10, `#` is 11, A-D are 12-15."""
    from livekit.agents.beta.workflows.utils import DtmfEvent, dtmf_event_to_code

    return dtmf_event_to_code(DtmfEvent(digit.strip().upper()))
