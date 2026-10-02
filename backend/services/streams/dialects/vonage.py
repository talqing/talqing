"""Vonage Voice API WebSocket endpoint (NCCO `connect` → `type: websocket`).

Read from https://developer.vonage.com/en/voice/voice-api/concepts/websockets.

The other raw-binary dialect beside SparkTG: a binary frame is 16-bit
little-endian PCM at whatever the NCCO's `content-type` set (8/16/24 kHz), a text
frame is JSON. Their events are namespaced (`websocket:connected`,
`websocket:dtmf`, `websocket:cleared`, `websocket:notify`) and their commands use
`action` rather than `event`.

Two things this protocol does not have: any way to end the call, and any way to
emit DTMF. Two things it does have that no other does: the per-call metadata
arrives as **sibling keys of the `websocket:connected` event itself** rather than
in a bag of its own, and there is no end event at all — the socket close is the
end, and *why* it closed only reaches the partner's own `eventUrl` webhook.
"""

from __future__ import annotations

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
    read_params,
    read_phone,
    resolve_format,
)

# Set on the `websocket:connected` event by the protocol itself, so they are not
# the partner's metadata even though they arrive in the same object.
_RESERVED_CONNECT_KEYS = frozenset({"event", "content-type"})


class VonageDialect:
    name = "vonage"
    binary_audio = True
    can_hangup = False
    can_send_dtmf = False
    exact_frames = False
    min_message_bytes = 0
    # The answer webhook's `from` is `447700900000`: international, no `+`.
    bare_numbers_are_international = True

    def parse(self, message: Wire) -> StreamEvent:
        if isinstance(message, (bytes, bytearray)):
            return Audio(bytes(message))
        try:
            evt: Any = json.loads(message)
        except json.JSONDecodeError as exc:
            raise ProtocolError(f"vonage sent a text frame that is not JSON: {exc}") from exc
        if not isinstance(evt, dict):
            raise ProtocolError("vonage sent a JSON text frame that is not an object")

        event = evt.get("event")
        if event == "websocket:connected":
            # This one event is both `connected` and `start` on this protocol,
            # so it is emitted as `Started` — it carries the format, the
            # metadata and everything the gateway needs to dispatch. There is
            # nothing further to wait for.
            headers = {k: v for k, v in evt.items() if k not in _RESERVED_CONNECT_KEYS}
            params = read_params(headers)
            return Started(
                # No call id on the socket. The NCCO's own `headers` are where a
                # partner puts one, under our names, exactly as Twilio's
                # `<Parameter>` and Plivo's `extraHeaders` do.
                call_id=params.get("call_id") or params.get("uuid"),
                from_number=read_phone(params.get("from")),
                to_number=read_phone(params.get("to")),
                params=params,
                fmt=resolve_format(str(evt.get("content-type") or "")),
            )
        if event == "websocket:dtmf":
            digit = str(evt.get("digit") or "").strip()
            return Dtmf(digit) if digit else Ignored("websocket:dtmf")
        if event == "websocket:cleared":
            return Cleared()
        if event == "websocket:notify":
            # Their `mark`: the echo of a `notify` we sent, carrying back the
            # payload we chose. We put the marker name in it.
            payload = evt.get("payload") if isinstance(evt.get("payload"), dict) else {}
            return Marked(str(payload.get("name") or ""))
        return Ignored(str(event) if event is not None else None)

    def audio_out(self, chunk: bytes, *, fmt: AudioFormat, stream_id: str | None) -> Wire:
        return chunk

    def clear(self, stream_id: str | None) -> Wire:
        return json.dumps({"action": "clear"}, separators=(",", ":"))

    def hangup(self, stream_id: str | None) -> Wire | None:
        return None

    def send_dtmf(self, stream_id: str | None, digits: str) -> Sequence[Wire]:
        return ()
