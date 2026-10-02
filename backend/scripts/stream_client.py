"""Speak a partner's half of the media-stream protocol, from a laptop.

This is what makes a stream integration testable before a partner exists — and
what makes the three traps that have no error message regression-testable at all:
a short final frame, an absent metadata bag, and two sockets carrying one call
id.

    # Loopback first, always: an echoed frame is inherently correctly sized, so a
    # clean echo proves transport, codec, endianness and framing at once and
    # leaves any later distortion unambiguously ours.
    python -m scripts.stream_client --url wss://…/connect/twilio/<tenant>/<agent> \\
        --wav hello.wav --out heard.wav

    # Keypad input, without needing a phone: one event per key, paced like a
    # person typing.
    python -m scripts.stream_client --url … --dtmf "4821#"

    # Then the ways a real platform misbehaves.
    python -m scripts.stream_client --url … --misbehave short-frame
    python -m scripts.stream_client --url … --misbehave no-attributes
    python -m scripts.stream_client --url … --misbehave unknown-event
    python -m scripts.stream_client --url … --misbehave duplicate-call

The dialect is read out of the URL, so the same command exercises whichever one
the connection was created for.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import json
import sys
import time
import wave
from typing import Any
from urllib.parse import urlparse

import numpy as np
import websockets

from api.stream import media
from services.streams.dialects import AudioFormat, get_dialect, resolve_format

# One 20 ms tick. Real platforms pace at exactly this, and bursting is what makes
# a bounded inbound channel drop audio with no error at all.
FRAME_MS = 20


def _format_for(dialect: str) -> tuple[str, AudioFormat]:
    """What this platform would announce, and what that resolves to.

    Each vendor's default, not a choice of ours: μ-law 8 k is the only thing
    Twilio does, `slin16k` is SparkTG's verified default, Exotel's applet
    defaults to 8 kHz linear, and Vonage's examples use 16 kHz.
    """
    announced = {
        "twilio": "audio/x-mulaw",
        "sparktg": "slin16k",
        "plivo": "audio/x-mulaw",
        "exotel": "raw",
        "vonage": "audio/l16;rate=16000",
    }[dialect]
    rate = {"twilio": 8000, "sparktg": 16000, "plivo": 8000, "exotel": 8000, "vonage": 16000}[
        dialect
    ]
    big_endian_below = 16000 if dialect == "sparktg" else 0
    return announced, resolve_format(
        announced, announced_rate=rate, big_endian_linear_below=big_endian_below
    )


def _handshake(dialect: str, announced: str, fmt: AudioFormat, call_id: str, *, attributes: bool):
    """The `connected` / `start` pair this platform sends, in its own spelling."""
    if dialect == "sparktg":
        yield json.dumps(
            {
                "event": "connected",
                "call_id": call_id,
                "codec": announced,
                "sample_rate_hz": fmt.sample_rate,
                "protocol_version": "v1",
            }
        )
        start: dict[str, Any] = {
            "event": "start",
            "call_id": call_id,
            "from": "+919876543210",
            "to": "+919812345678",
            "direction": "inbound",
        }
        # The one shape that crashes a naive client on a real call: the key is
        # omitted entirely rather than sent empty.
        if attributes:
            start["attributes"] = {"svcId": "1599", "flowName": "sales_demo"}
        yield json.dumps(start)
        return
    if dialect == "twilio":
        yield json.dumps({"event": "connected", "protocol": "Call", "version": "1.0.0"})
        start = {
            "event": "start",
            "sequenceNumber": "1",
            "streamSid": f"MZ{call_id}",
            "start": {
                "accountSid": "ACemulator",
                "streamSid": f"MZ{call_id}",
                "callSid": call_id,
                "tracks": ["inbound"],
                "mediaFormat": {
                    "encoding": announced,
                    "sampleRate": fmt.sample_rate,
                    "channels": 1,
                },
            },
        }
        if attributes:
            start["start"]["customParameters"] = {"from": "+14155550101", "tier": "gold"}
        yield json.dumps(start)
        return
    if dialect == "plivo":
        start = {
            "event": "start",
            "sequenceNumber": 1,
            "start": {
                "callId": call_id,
                "streamId": f"stream-{call_id}",
                "accountId": "MAemulator",
                "tracks": ["inbound"],
                "mediaFormat": {"encoding": announced, "sampleRate": fmt.sample_rate},
            },
        }
        if attributes:
            start["extra_headers"] = "from=+14155550101;tier=gold"
        yield json.dumps(start)
        return
    if dialect == "exotel":
        yield json.dumps({"event": "connected"})
        start = {
            "event": "start",
            "sequence_number": 1,
            "stream_sid": f"stream-{call_id}",
            "start": {
                "stream_sid": f"stream-{call_id}",
                "call_sid": call_id,
                "account_sid": "emulator",
                "from": "+919876543210",
                "to": "+911234567890",
                "media_format": {
                    "encoding": announced,
                    "sample_rate": str(fmt.sample_rate),
                    "bit_rate": "128",
                },
            },
        }
        if attributes:
            start["start"]["custom_parameters"] = {"tier": "gold"}
        yield json.dumps(start)
        return
    connected = {"event": "websocket:connected", "content-type": announced}
    if attributes:
        connected |= {"from": "+442079460000", "call_id": call_id, "tier": "gold"}
    yield json.dumps(connected)


def _end(dialect: str, call_id: str, stream_id: str, duration_ms: int) -> str | None:
    """The last message a real platform sends before it closes the socket.

    Two of the five have none: Plivo's socket close IS the end, and Vonage's
    reason reaches the partner's own `eventUrl` webhook rather than the socket.
    Sending nothing on those is what a real call looks like, and the gateway has
    to file it correctly anyway.
    """
    if dialect == "sparktg":
        return json.dumps(
            {
                "event": "end",
                "call_id": call_id,
                "reason": "caller_hangup",
                "duration_ms": duration_ms,
            }
        )
    if dialect == "twilio":
        return json.dumps({"event": "stop", "streamSid": stream_id, "stop": {"callSid": call_id}})
    if dialect == "exotel":
        return json.dumps(
            {
                "event": "stop",
                "stream_sid": stream_id,
                "stop": {"call_sid": call_id, "reason": "callended"},
            }
        )
    return None


def _dtmf(dialect: str, call_id: str, stream_id: str, digit: str) -> str:
    """One keypress, in this platform's spelling. Out-of-band, never a tone."""
    if dialect == "sparktg":
        return json.dumps(
            {
                "event": "media_dtmf",
                "call_id": call_id,
                "digit": digit,
                "ts": int(time.time() * 1000),
            }
        )
    if dialect == "twilio":
        return json.dumps(
            {
                "event": "dtmf",
                "streamSid": stream_id,
                "dtmf": {"track": "inbound_track", "digit": digit},
            }
        )
    if dialect == "plivo":
        return json.dumps(
            {
                "event": "dtmf",
                "streamId": stream_id,
                "dtmf": {
                    "track": "inbound",
                    "digit": digit,
                    "timestamp": str(int(time.time() * 1000)),
                },
            }
        )
    if dialect == "exotel":
        return json.dumps(
            {"event": "dtmf", "stream_sid": stream_id, "dtmf": {"duration": "200", "digit": digit}}
        )
    return json.dumps({"event": "websocket:dtmf", "digit": digit, "duration": 260})


def _wrap_audio(dialect: str, chunk: bytes, stream_id: str) -> str | bytes:
    """One frame of caller audio, as this platform would send it to us."""
    if dialect in ("sparktg", "vonage"):
        return chunk
    payload = base64.b64encode(chunk).decode("ascii")
    if dialect == "twilio":
        return json.dumps({"event": "media", "streamSid": stream_id, "media": {"payload": payload}})
    if dialect == "plivo":
        return json.dumps({"event": "media", "streamId": stream_id, "media": {"payload": payload}})
    return json.dumps({"event": "media", "stream_sid": stream_id, "media": {"payload": payload}})


def _read_wav(path: str, fmt: AudioFormat) -> bytes:
    """Load a WAV as PCM16 little-endian mono at the call's rate.

    Resampled here rather than in `media.py` on purpose: the gateway never
    resamples, so a test fixture that needed it to would be testing something the
    product does not do.
    """
    with wave.open(path, "rb") as handle:
        if handle.getsampwidth() != 2:
            raise SystemExit(f"{path} must be 16-bit PCM")
        frames = handle.readframes(handle.getnframes())
        samples = np.frombuffer(frames, dtype="<i2")
        if handle.getnchannels() > 1:
            samples = samples.reshape(-1, handle.getnchannels())[:, 0]
        rate = handle.getframerate()
    if rate != fmt.sample_rate:
        target = int(len(samples) * fmt.sample_rate / rate)
        samples = np.interp(
            np.linspace(0, len(samples), target, endpoint=False),
            np.arange(len(samples)),
            samples.astype(np.float64),
        ).astype("<i2")
    return samples.tobytes()


async def _send_audio(ws, dialect: str, pcm: bytes, fmt: AudioFormat, stream_id: str) -> None:
    """One frame per 20 ms of wall clock, exactly as a real platform paces."""
    frame_bytes = fmt.frame_bytes
    encoded = media.encode(pcm, fmt)
    started = time.monotonic()
    for index, offset in enumerate(range(0, len(encoded) - frame_bytes + 1, frame_bytes)):
        await ws.send(_wrap_audio(dialect, encoded[offset : offset + frame_bytes], stream_id))
        await asyncio.sleep(max(0.0, (index + 1) * FRAME_MS / 1000 - (time.monotonic() - started)))


async def _run(args: argparse.Namespace) -> int:
    # `/v1/streams/connect/{dialect}/{tenant}/{agent}` — read off the
    # marker rather than by index, so a version prefix cannot shift it silently.
    segments = urlparse(args.url).path.strip("/").split("/")
    if "connect" not in segments or segments.index("connect") + 1 >= len(segments):
        raise SystemExit(f"not a stream connect URL: {args.url}")
    dialect = segments[segments.index("connect") + 1]
    adapter = get_dialect(dialect)
    announced, fmt = _format_for(dialect)
    call_id = args.call_id
    stream_id = f"stream-{call_id}"
    heard = bytearray()

    opened = time.monotonic()
    async with websockets.connect(args.url, compression=None) as ws:
        for message in _handshake(
            dialect, announced, fmt, call_id, attributes=args.misbehave != "no-attributes"
        ):
            await ws.send(message)

        if args.misbehave == "unknown-event":
            # Forward compatibility: the gateway must log this and carry on.
            await ws.send(json.dumps({"event": "something_new_in_v2", "call_id": call_id}))

        async def _read() -> None:
            async for message in ws:
                event = adapter.parse(message)
                kind = type(event).__name__
                if kind == "Audio":
                    if adapter.exact_frames and len(event.payload) != fmt.frame_bytes:
                        # What the strict dialect would answer with `error:
                        # invalid_audio_frame` before ending the call.
                        print(
                            f"!! wrong-sized frame: {len(event.payload)} bytes, "
                            f"expected {fmt.frame_bytes}",
                            file=sys.stderr,
                        )
                    heard.extend(media.decode(event.payload, fmt))
                else:
                    print(f"<< {kind}: {event}")

        reader = asyncio.create_task(_read())
        try:
            if args.misbehave == "short-frame":
                # The trap that fires on the first real TTS response of the first
                # real call and reads as "the call drops a second after the agent
                # speaks". Sent from OUR side here to prove the gateway's own
                # check refuses it rather than putting it on the wire.
                await ws.send(_wrap_audio(dialect, b"\x00" * 7, stream_id))
            if args.wav:
                await _send_audio(ws, dialect, _read_wav(args.wav, fmt), fmt, stream_id)
            if args.dtmf:
                # Paced like a person typing. The first keypress should interrupt
                # whatever the agent is saying, and the whole entry should arrive
                # as ONE labelled user turn rather than one turn per key.
                for digit in args.dtmf:
                    await ws.send(_dtmf(dialect, call_id, stream_id, digit))
                    await asyncio.sleep(0.35)
            await asyncio.sleep(args.hold)
            # The caller hangs up. A real platform says so and then closes, and
            # that reason is the only thing that can tell a completed call from
            # a dropped one on our side.
            farewell = _end(dialect, call_id, stream_id, int((time.monotonic() - opened) * 1000))
            if farewell:
                await ws.send(farewell)
                await asyncio.sleep(0.3)
        finally:
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reader

    if args.out and heard:
        with wave.open(args.out, "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(fmt.sample_rate)
            handle.writeframes(bytes(heard))
        print(f"wrote {len(heard) // 2} samples to {args.out}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="the wss:// URL a partner was given")
    parser.add_argument("--wav", help="16-bit WAV to play in as the caller")
    parser.add_argument("--out", help="write what the agent said to this WAV")
    parser.add_argument("--call-id", default=f"emulator-{int(time.time())}")
    parser.add_argument(
        "--dtmf", help="keys the caller presses after the audio, e.g. 1234# — one event each"
    )
    parser.add_argument(
        "--hold", type=float, default=20.0, help="seconds to keep listening after the audio ends"
    )
    parser.add_argument(
        "--misbehave",
        choices=["short-frame", "no-attributes", "unknown-event", "duplicate-call"],
        help="reproduce one of the failure modes that has no error message",
    )
    args = parser.parse_args()

    if args.misbehave == "duplicate-call":
        # Two sockets, one platform call id. The second must be refused before a
        # second room is created — otherwise it is two agents and one confused
        # caller.
        async def _both() -> int:
            first = asyncio.create_task(_run(args))
            await asyncio.sleep(1.0)
            second = await _run(args)
            await first
            return second

        return asyncio.run(_both())
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
