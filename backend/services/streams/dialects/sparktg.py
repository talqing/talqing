"""SparkTG voice streaming, protocol v1.

Read from *Real-Time Voice Streaming WebSocket Integration Guide*, protocol v1,
supplied by the partner. Section numbers below are that document's.

The strictest of the five, and the one the outbound path is written to: §6.2
enforces the exact frame size and **hangs the call up** on a single violation
(`error: invalid_audio_frame`, then `end: integrator_invalid_codec`). Everyone
else buffers whatever they are sent, so relaxing per dialect is safe and the
other direction is not.

Audio is raw binary in both directions — no JSON wrapper, no base64, no
per-frame metadata. Commands are JSON text frames carrying a `command`
discriminator and nothing else; §5 is explicit that a command frame they cannot
parse is *silently discarded*, with no error on either side, which is why every
one of them is written here and nowhere else.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from .base import (
    Audio,
    AudioFormat,
    Connected,
    Dtmf,
    Ended,
    Error,
    Ignored,
    Marked,
    ProtocolError,
    Started,
    StreamEvent,
    Wire,
    read_int,
    read_params,
    read_phone,
    read_text,
    resolve_format,
)

# §6.1: `slin16k` is little-endian and `slin8k` is big-endian, in the same
# protocol. Their own troubleshooting table calls this "the single most common
# source of 'I hear static or silence'".
_BIG_ENDIAN_LINEAR_BELOW = 16000


class SparkTGDialect:
    name = "sparktg"
    binary_audio = True
    # §5: `{"command":"hangup"}` — the only platform of the five that lets the
    # agent actually end the call rather than just stop streaming.
    can_hangup = True
    can_send_dtmf = True
    exact_frames = True
    min_message_bytes = 0
    # §4.2 sends `+919876543210`.
    bare_numbers_are_international = False

    def parse(self, message: Wire) -> StreamEvent:
        if isinstance(message, (bytes, bytearray)):
            return Audio(bytes(message))
        try:
            evt: Any = json.loads(message)
        except json.JSONDecodeError as exc:
            raise ProtocolError(f"sparktg sent a text frame that is not JSON: {exc}") from exc
        if not isinstance(evt, dict):
            raise ProtocolError("sparktg sent a JSON text frame that is not an object")

        event = evt.get("event")
        if event == "connected":
            # §4.1: the codec is fixed for the life of the call and announced
            # here. Their own warning: do not string-compare it naively — the
            # shipped default announces `slin16k`, not `pcm_s16le_16000`, and
            # the rate comes from the event rather than from the name.
            return Connected(
                fmt=resolve_format(
                    str(evt.get("codec") or ""),
                    announced_rate=read_int(evt.get("sample_rate_hz")),
                    big_endian_linear_below=_BIG_ENDIAN_LINEAR_BELOW,
                ),
                call_id=read_text(evt.get("call_id")),
            )
        if event == "start":
            # §4.2: `attributes` is populated from SIP headers and is omitted
            # from the JSON entirely when none survived. Never indexed.
            return Started(
                call_id=read_text(evt.get("call_id")),
                from_number=read_phone(evt.get("from")),
                to_number=read_phone(evt.get("to")),
                direction=_read_direction(evt.get("direction")),
                params=read_params(evt.get("attributes")),
            )
        if event == "media_dtmf":
            digit = str(evt.get("digit") or "").strip()
            return Dtmf(digit) if digit else Ignored("media_dtmf")
        if event == "mark":
            return Marked(str(evt.get("name") or ""))
        if event == "end":
            # §7.2. Reasons are enumerated but additive changes may add more, so
            # the string travels verbatim rather than through a map.
            return Ended(
                reason=str(evt.get("reason") or "") or None,
                duration_ms=read_int(evt.get("duration_ms")),
            )
        if event == "error":
            return Error(
                code=str(evt.get("code") or "") or None,
                message=str(evt.get("message") or "") or None,
            )
        return Ignored(str(event) if event is not None else None)

    def audio_out(self, chunk: bytes, *, fmt: AudioFormat, stream_id: str | None) -> Wire:
        # Raw binary, nothing else. §6: "a frame is raw encoded audio and
        # nothing else". The size check is the bridge's, before this is called.
        return chunk

    def clear(self, stream_id: str | None) -> Wire:
        return _command("clear")

    def hangup(self, stream_id: str | None) -> Wire:
        return _command("hangup")

    def send_dtmf(self, stream_id: str | None, digits: str) -> Sequence[Wire]:
        # §5: `digit` is singular — one command per key. The pacing between them
        # belongs to the caller (`compiler/operations.py::_send_dtmf`), which
        # already owns the inter-digit delay for the SIP path.
        return [_command("send_dtmf", digit=digit) for digit in digits]


def _command(command: str, **payload: str) -> str:
    """The one place a SparkTG command is written.

    §5: a command frame they cannot parse — a wrong field name, a wrong type, a
    typo in the command name — is silently discarded, with no `error` event and
    nothing logged on their side. So if `hangup` "appears to have no effect",
    the JSON is the first suspect, and there is exactly one JSON to check.
    """
    return json.dumps({"command": command, **payload}, separators=(",", ":"))


def _read_direction(value: object) -> str | None:
    """§4.2 says exactly `inbound` or `outbound`. Anything else is not a direction."""
    text = str(value or "").strip().lower()
    return text if text in ("inbound", "outbound") else None
