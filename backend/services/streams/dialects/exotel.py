"""Exotel Voicebot applet (bidirectional AgentStream).

Read from https://developer.exotel.com/docs/agentstream/stream-voicebot-applet.

Snake-cased Twilio, with one rule that is genuinely its own and shapes the
outbound path: **a chunk must be at least 3.2 KB and a multiple of 320 bytes**.
Their own warning is that smaller chunks "risk audio distortion" and
non-compliant sizes "create gaps in audio playback", so the bridge coalesces
whole 20 ms frames up to `min_message_bytes` before sending. That is 200 ms of
added barge-in latency at 8 kHz, and it is this protocol's price rather than a
choice of ours — their `clear` only drops audio that has not started playing.

Always 16-bit linear PCM, little-endian, mono; the rate is whatever the applet's
`?sample-rate=` query parameter set (8000 default, 16000, 24000) and is announced
in `start.media_format.sample_rate` as a *string*.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Sequence
from typing import Any

from .base import (
    Audio,
    AudioFormat,
    Connected,
    Dtmf,
    Ended,
    Ignored,
    Marked,
    ProtocolError,
    Started,
    StreamEvent,
    Wire,
    read_b64,
    read_int,
    read_params,
    read_phone,
    read_text,
    resolve_format,
)

# Their stated minimum, in bytes. A multiple of 320 falls out for free: every
# 20 ms linear frame is 320 bytes at 8 kHz and 640 at 16 kHz, and 3200 is a whole
# number of both.
_MIN_CHUNK_BYTES = 3200


class ExotelDialect:
    name = "exotel"
    binary_audio = False
    # "The stream is automatically closed before executing the next Applet. The
    # call flow proceeds to the next applet configured" — so closing the socket
    # hands the caller back to their flow rather than hanging up.
    can_hangup = False
    can_send_dtmf = False
    exact_frames = False
    min_message_bytes = _MIN_CHUNK_BYTES
    # Read the way a SIP caller's number is: bare digits are national to the
    # business number's country.
    bare_numbers_are_international = False

    def parse(self, message: Wire) -> StreamEvent:
        if isinstance(message, (bytes, bytearray)):
            raise ProtocolError("exotel sent a binary frame; this protocol is JSON only")
        try:
            evt: Any = json.loads(message)
        except json.JSONDecodeError as exc:
            raise ProtocolError(f"exotel sent a text frame that is not JSON: {exc}") from exc
        if not isinstance(evt, dict):
            raise ProtocolError("exotel sent a JSON text frame that is not an object")

        event = evt.get("event")
        if event == "connected":
            # `{"event":"connected"}` and nothing else — the format is on `start`.
            return Connected()
        if event == "start":
            start = evt.get("start") if isinstance(evt.get("start"), dict) else {}
            media_format = start.get("media_format")
            if not isinstance(media_format, dict):
                raise ProtocolError("exotel `start` carried no media_format")
            return Started(
                call_id=read_text(start.get("call_sid")),
                from_number=read_phone(start.get("from")),
                to_number=read_phone(start.get("to")),
                params=read_params(start.get("custom_parameters")),
                fmt=resolve_format(
                    str(media_format.get("encoding") or ""),
                    announced_rate=read_int(media_format.get("sample_rate")),
                ),
                stream_id=read_text(evt.get("stream_sid")) or read_text(start.get("stream_sid")),
            )
        if event == "media":
            media = evt.get("media") if isinstance(evt.get("media"), dict) else {}
            return Audio(read_b64(media.get("payload")))
        if event == "dtmf":
            dtmf = evt.get("dtmf") if isinstance(evt.get("dtmf"), dict) else {}
            digit = str(dtmf.get("digit") or "").strip()
            return Dtmf(digit) if digit else Ignored("dtmf")
        if event == "mark":
            mark = evt.get("mark") if isinstance(evt.get("mark"), dict) else {}
            return Marked(str(mark.get("name") or ""))
        if event == "stop":
            stop = evt.get("stop") if isinstance(evt.get("stop"), dict) else {}
            # `stopped` = the applet ended; `callended` = the caller hung up.
            return Ended(reason=read_text(stop.get("reason")))
        return Ignored(str(event) if event is not None else None)

    def audio_out(self, chunk: bytes, *, fmt: AudioFormat, stream_id: str | None) -> Wire:
        return json.dumps(
            {
                "event": "media",
                "stream_sid": stream_id,
                "media": {"payload": base64.b64encode(chunk).decode("ascii")},
            },
            separators=(",", ":"),
        )

    def clear(self, stream_id: str | None) -> Wire:
        return json.dumps({"event": "clear", "stream_sid": stream_id}, separators=(",", ":"))

    def hangup(self, stream_id: str | None) -> Wire | None:
        return None

    def send_dtmf(self, stream_id: str | None, digits: str) -> Sequence[Wire]:
        return ()
