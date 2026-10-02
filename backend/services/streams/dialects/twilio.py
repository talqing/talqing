"""Twilio Media Streams (`<Connect><Stream>`), bidirectional.

Read from https://www.twilio.com/docs/voice/media-streams/websocket-messages and
the `<Stream>` TwiML reference.

The dialect the largest number of prospects already speak, and the one with the
tightest constraints on how a partner reaches us: `<Stream url>` **does not
support a query string**, so every credential and routing hint has to live in the
URL path (`services/streams/service.py` explains what that decided), and the
`start` message carries **no caller number** — a Twilio partner must add
`<Parameter name="from" value="{{From}}"/>` or every call is anonymous.

μ-law 8 kHz, always: the docs say `encoding` is always `audio/x-mulaw`,
`sampleRate` always 8000 and `channels` always 1. The format is still read off
the wire rather than assumed, because a platform that documents a constant today
is a platform that can announce a second one tomorrow.
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


class TwilioDialect:
    name = "twilio"
    binary_audio = False
    # A `<Connect><Stream>` ends when the CALL ends; closing the socket only
    # hands the caller back to the TwiML after it. Their docs: "Twilio executes
    # the remaining TwiML instructions only after your server closes the
    # WebSocket connection", and with no further verb "Twilio will end the phone
    # call" — so the verb after the stream is where a partner routes the caller.
    can_hangup = False
    # There is no send-DTMF command in this protocol. `send_dtmf` therefore
    # refuses itself on a Twilio stream, by name and with a reason.
    can_send_dtmf = False
    exact_frames = False
    min_message_bytes = 0
    # Twilio's own `From` and `To` are E.164 with the `+`.
    bare_numbers_are_international = False

    def parse(self, message: Wire) -> StreamEvent:
        if isinstance(message, (bytes, bytearray)):
            # Twilio never sends binary. A binary frame here means the socket is
            # carrying something else entirely, which is worth saying rather
            # than feeding to the decoder as if it were audio.
            raise ProtocolError("twilio sent a binary frame; this protocol is JSON only")
        try:
            evt: Any = json.loads(message)
        except json.JSONDecodeError as exc:
            raise ProtocolError(f"twilio sent a text frame that is not JSON: {exc}") from exc
        if not isinstance(evt, dict):
            raise ProtocolError("twilio sent a JSON text frame that is not an object")

        event = evt.get("event")
        if event == "connected":
            # `{"event":"connected","protocol":"Call","version":"1.0.0"}` — no
            # format here; it arrives on `start`.
            return Connected()
        if event == "start":
            start = evt.get("start") if isinstance(evt.get("start"), dict) else {}
            media_format = start.get("mediaFormat")
            if not isinstance(media_format, dict):
                raise ProtocolError("twilio `start` carried no mediaFormat")
            params = read_params(start.get("customParameters"))
            return Started(
                call_id=read_text(start.get("callSid")),
                # Not sent by the protocol. A partner who wants them adds
                # `<Parameter name="from" .../>`, using OUR names — they write
                # the TwiML, so they can.
                from_number=read_phone(params.get("from")),
                to_number=read_phone(params.get("to")),
                params=params,
                fmt=resolve_format(
                    str(media_format.get("encoding") or ""),
                    announced_rate=read_int(media_format.get("sampleRate")),
                ),
                stream_id=read_text(evt.get("streamSid")) or read_text(start.get("streamSid")),
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
            # No reason field: on a bidirectional stream the only way to stop is
            # for the call to end, so this IS the caller hanging up.
            return Ended(reason="stop")
        return Ignored(str(event) if event is not None else None)

    def audio_out(self, chunk: bytes, *, fmt: AudioFormat, stream_id: str | None) -> Wire:
        # Their warning: the payload must carry no audio file header bytes.
        # `media.py` produces raw codec bytes and never a container.
        return json.dumps(
            {
                "event": "media",
                "streamSid": stream_id,
                "media": {"payload": base64.b64encode(chunk).decode("ascii")},
            },
            separators=(",", ":"),
        )

    def clear(self, stream_id: str | None) -> Wire:
        # Twilio's buffer genuinely IS purgeable, unlike SparkTG's — which is
        # most of why the barge-in signal in `workers/voice/stream.py` exists.
        return json.dumps({"event": "clear", "streamSid": stream_id}, separators=(",", ":"))

    def hangup(self, stream_id: str | None) -> Wire | None:
        return None

    def send_dtmf(self, stream_id: str | None, digits: str) -> Sequence[Wire]:
        return ()
