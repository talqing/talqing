"""Plivo AudioStreams (`<Stream bidirectional="true">`).

Read from https://plivo.com/docs/voice-agents/audio-streaming/concepts/audio-streaming-reference
and the `<Stream>` XML reference.

A JSON-wrapped base64 protocol like Twilio's, with three differences that matter:
their outbound `playAudio` has to **echo the content type and sample rate**, they
*do* have a send-DTMF command, and the socket carries no stop event at all — the
close is the end, and the caller's number arrives only on the HTTP status
callback, never here.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Sequence
from typing import Any

from .base import (
    Audio,
    AudioFormat,
    Cleared,
    Dtmf,
    Ignored,
    Marked,
    ProtocolError,
    Started,
    StreamEvent,
    Wire,
    read_b64,
    read_int,
    read_kv_string,
    read_phone,
    read_text,
    resolve_format,
)


class PlivoDialect:
    name = "plivo"
    binary_audio = False
    # `keepCallAlive` decides what happens after the socket closes: the call
    # continues into the next XML element. There is no hangup command.
    can_hangup = False
    can_send_dtmf = True
    exact_frames = False
    min_message_bytes = 0
    # `extraHeaders` allows letters and digits only, so a number survives it
    # only as international digits without the `+`.
    bare_numbers_are_international = True

    def __init__(self) -> None:
        # What `playAudio` must echo. Captured from `start.mediaFormat` rather
        # than derived from the resolved format, because their requirement is
        # that the two strings MATCH — `audio/x-mulaw` is what they said and
        # `pcmu` is what we understood, and only one of those is their contract.
        self._content_type: str | None = None
        self._sample_rate: int | None = None

    def parse(self, message: Wire) -> StreamEvent:
        if isinstance(message, (bytes, bytearray)):
            raise ProtocolError("plivo sent a binary frame; this protocol is JSON only")
        try:
            evt: Any = json.loads(message)
        except json.JSONDecodeError as exc:
            raise ProtocolError(f"plivo sent a text frame that is not JSON: {exc}") from exc
        if not isinstance(evt, dict):
            raise ProtocolError("plivo sent a JSON text frame that is not an object")

        event = evt.get("event")
        if event == "start":
            # No `connected` on this protocol — `start` is the first message.
            start = evt.get("start") if isinstance(evt.get("start"), dict) else {}
            media_format = start.get("mediaFormat")
            if not isinstance(media_format, dict):
                raise ProtocolError("plivo `start` carried no mediaFormat")
            self._content_type = read_text(media_format.get("encoding"))
            self._sample_rate = read_int(media_format.get("sampleRate"))
            params = read_kv_string(evt.get("extra_headers"))
            return Started(
                call_id=read_text(start.get("callId")),
                # Their status callback carries From/To; the socket does not. A
                # partner who wants them on the call puts them in `extraHeaders`,
                # under our names — the same arrangement Twilio's `<Parameter>`
                # gets.
                from_number=read_phone(params.get("from")),
                to_number=read_phone(params.get("to")),
                params=params,
                fmt=resolve_format(
                    str(media_format.get("encoding") or ""),
                    announced_rate=self._sample_rate,
                ),
                stream_id=read_text(start.get("streamId")) or read_text(evt.get("streamId")),
            )
        if event == "media":
            media = evt.get("media") if isinstance(evt.get("media"), dict) else {}
            return Audio(read_b64(media.get("payload")))
        if event == "dtmf":
            dtmf = evt.get("dtmf") if isinstance(evt.get("dtmf"), dict) else {}
            digit = str(dtmf.get("digit") or "").strip()
            return Dtmf(digit) if digit else Ignored("dtmf")
        if event == "playedStream":
            # Their `mark`: the echo of a `checkpoint` we sent.
            return Marked(str(evt.get("name") or ""))
        if event == "clearedAudio":
            return Cleared()
        return Ignored(str(event) if event is not None else None)

    def audio_out(self, chunk: bytes, *, fmt: AudioFormat, stream_id: str | None) -> Wire:
        return json.dumps(
            {
                "event": "playAudio",
                "media": {
                    # Echoed, not re-derived: "the content type and sample rate
                    # must match what was specified in your Stream XML".
                    "contentType": self._content_type,
                    "sampleRate": self._sample_rate,
                    "payload": base64.b64encode(chunk).decode("ascii"),
                },
            },
            separators=(",", ":"),
        )

    def clear(self, stream_id: str | None) -> Wire:
        return json.dumps({"event": "clearAudio", "streamId": stream_id}, separators=(",", ":"))

    def hangup(self, stream_id: str | None) -> Wire | None:
        return None

    def send_dtmf(self, stream_id: str | None, digits: str) -> Sequence[Wire]:
        # One command for the whole string, unlike SparkTG's one-per-digit.
        return [json.dumps({"event": "sendDTMF", "dtmf": digits}, separators=(",", ":"))]
