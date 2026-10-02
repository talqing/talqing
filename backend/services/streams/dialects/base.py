"""What every WebSocket media-stream platform has in common, and the seam per platform.

Five platforms ship the same idea with different spelling, and the differences
are all trivia — field names, casing, whether audio is a binary frame or base64
inside JSON, whether the stream id is echoed on every message. A dialect absorbs
that trivia and hands the bridge one vocabulary.

Pure and synchronous, with no I/O and no LiveKit, so every rule that a live call
would otherwise teach us the hard way (endianness, frame sizing, an absent
metadata bag) can be checked without one.

Modelled on `services/telephony/providers/base.py`, one for one — the same
Protocol-plus-``get_*`` shape, for the same reason: the published API needs to
validate a dialect name at create time, and the gateway needs an implementation
at connect time, and neither should know about the other.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol

StreamDialectName = Literal["twilio", "sparktg", "plivo", "exotel", "vonage"]

# Ordered the way the product introduces them: the one partner already asking,
# then the dialect the largest number of prospects already speak, then the rest.
STREAM_DIALECTS: tuple[StreamDialectName, ...] = (
    "sparktg",
    "twilio",
    "plivo",
    "exotel",
    "vonage",
)

# One WebSocket message, either way. A text frame is control; a binary frame is
# audio on the dialects that carry it that way.
type Wire = str | bytes


class ProtocolError(Exception):
    """The peer sent something this dialect cannot read at all.

    Reserved for a message that is malformed rather than merely unrecognised —
    an unknown *event* is :class:`Ignored`, which is the forward-compatibility
    rule every one of these platforms asks for in writing. This is for a text
    frame that is not JSON, or a `start` with no format on a protocol that
    announces one.
    """


class UnsupportedCodecError(Exception):
    """The platform announced a format we cannot produce.

    A refusal, never a substitution: silently sending 16 kHz linear down an
    8 kHz μ-law socket is how a call becomes static with clean logs on both
    sides.
    """


# ── audio format ────────────────────────────────────────────────────────────

# Byte order is part of the encoding rather than a flag beside it, because it is
# a property of the *dialect and the announced codec together* — SparkTG's
# `slin8k` is big-endian while its `slin16k` is little-endian, in the same
# protocol. An `if codec == "slin"` will get that wrong for ever.
Encoding = Literal["pcm_s16le", "pcm_s16be", "pcmu"]

# What a codec string means once the spelling is stripped off it. The rate is
# separate because most platforms announce it in its own field.
CodecFamily = Literal["linear16", "pcmu", "opus"]

# Every spelling of a codec any of the five platforms may send, lower-cased.
# One table rather than one per dialect: `pcm_s16le_16000` and `slin16k` are the
# same format whoever says them, and a per-dialect copy is how the two drift.
_CODEC_FAMILIES: dict[str, CodecFamily] = {
    # 16-bit linear PCM
    "slin": "linear16",
    "slin8k": "linear16",
    "slin16k": "linear16",
    "slin_8000": "linear16",
    "slin_16000": "linear16",
    "pcm_s16le_8000": "linear16",
    "pcm_s16le_16000": "linear16",
    "l16": "linear16",
    "raw": "linear16",
    "audio/l16": "linear16",
    "audio/x-l16": "linear16",
    "audio/raw": "linear16",
    # G.711 μ-law
    "pcmu": "pcmu",
    "pcmu8k": "pcmu",
    "pcmu_8000": "pcmu",
    "ulaw": "pcmu",
    "mulaw": "pcmu",
    "audio/x-mulaw": "pcmu",
    "audio/mulaw": "pcmu",
    "audio/basic": "pcmu",
    # Opus — recognised so it can be REFUSED by name (§B12). A codec we cannot
    # name at all would close with "unknown codec", which sends the partner
    # looking for a typo rather than reading the one line that says we do not
    # do Opus yet.
    "opus": "opus",
    "opus48k": "opus",
    "opus_48000": "opus",
    "audio/opus": "opus",
}

# Rates a codec string may carry inside its own name, for the platforms that put
# it there instead of in a field of its own.
_CODEC_RATES: dict[str, int] = {
    "slin8k": 8000,
    "slin_8000": 8000,
    "pcm_s16le_8000": 8000,
    "pcmu8k": 8000,
    "pcmu_8000": 8000,
    "ulaw": 8000,
    "mulaw": 8000,
    "audio/x-mulaw": 8000,
    "audio/mulaw": 8000,
    "audio/basic": 8000,
    "slin16k": 16000,
    "slin_16000": 16000,
    "pcm_s16le_16000": 16000,
    "opus": 48000,
    "opus48k": 48000,
    "opus_48000": 48000,
}


@dataclass(frozen=True, slots=True)
class AudioFormat:
    """One call's wire format, as the platform announced it.

    Never configuration. Every protocol states its own format per call, so a
    stored copy could only ever disagree with the wire.
    """

    encoding: Encoding
    sample_rate: int

    def __post_init__(self) -> None:
        # A 20 ms frame has to be a whole number of samples, which is what makes
        # `frame_bytes` exact rather than rounded — and every rate any of these
        # platforms offers (8k/16k/24k/48k) satisfies it.
        if self.sample_rate % 50 or not 8000 <= self.sample_rate <= 48000:
            raise UnsupportedCodecError(
                f"sample rate {self.sample_rate} is not a supported streaming rate"
            )

    @property
    def bytes_per_sample(self) -> int:
        return 1 if self.encoding == "pcmu" else 2

    @property
    def frame_bytes(self) -> int:
        """Bytes in one 20 ms frame — 640 at slin16k, 320 at slin8k, 160 at μ-law."""
        return self.sample_rate // 50 * self.bytes_per_sample

    @property
    def silence_byte(self) -> int:
        """What a padded tail is filled with: 0x00 for linear, 0xFF for μ-law."""
        return 0xFF if self.encoding == "pcmu" else 0x00

    def __str__(self) -> str:
        return f"{self.encoding}@{self.sample_rate}"


def read_codec(value: str) -> tuple[CodecFamily, int | None]:
    """Normalise one codec string into a family and, if it carries one, a rate.

    Handles the three shapes these platforms use interchangeably: a bare name
    (`slin16k`), a MIME type (`audio/x-mulaw`), and a MIME type with parameters
    (`audio/l16;rate=16000`). Whitespace and case are the platform's business,
    not ours.
    """
    text = (value or "").strip().lower()
    if not text:
        raise UnsupportedCodecError("the platform announced no codec")
    rate: int | None = None
    head, _, params = text.partition(";")
    head = head.strip()
    for param in params.split(";"):
        key, _, param_value = param.partition("=")
        if key.strip() == "rate":
            try:
                rate = int(param_value.strip())
            except ValueError as exc:
                raise UnsupportedCodecError(f"unreadable sample rate in codec {value!r}") from exc
    family = _CODEC_FAMILIES.get(head)
    if family is None:
        raise UnsupportedCodecError(f"unknown audio codec {value!r}")
    return family, rate or _CODEC_RATES.get(head)


def resolve_format(
    codec: str,
    *,
    announced_rate: int | None = None,
    big_endian_linear_below: int = 0,
) -> AudioFormat:
    """The format to run this call in, from what the platform said about it.

    ``announced_rate`` is the rate the platform stated in a field of its own and
    wins over anything the codec name implies — every one of these protocols
    says to take the rate from the event rather than infer it, and only one of
    them has a codec name that carries a rate at all.

    ``big_endian_linear_below`` is SparkTG's rule and nobody else's: their
    `slin8k` is big-endian while their `slin16k` is little-endian. Expressed as
    a threshold rather than a boolean so the dialect states the rule once
    (`big_endian_linear_below=16000`) instead of branching on the rate itself.
    """
    family, name_rate = read_codec(codec)
    rate = announced_rate or name_rate
    if not rate:
        raise UnsupportedCodecError(
            f"codec {codec!r} carries no sample rate and none was announced"
        )
    if family == "opus":
        raise UnsupportedCodecError(
            "Opus is not supported on media streams yet - configure this connection for "
            "linear PCM (slin16k) or G.711 mu-law"
        )
    if family == "pcmu":
        return AudioFormat("pcmu", rate)
    return AudioFormat("pcm_s16be" if rate < big_endian_linear_below else "pcm_s16le", rate)


# ── events ──────────────────────────────────────────────────────────────────
#
# One vocabulary for five protocols. The bridge branches on these and never on
# a platform's own event names.


@dataclass(frozen=True, slots=True)
class Connected:
    """The socket is open. Carries the format on the platforms that announce it here."""

    fmt: AudioFormat | None = None
    call_id: str | None = None


@dataclass(frozen=True, slots=True)
class Started:
    """The call is live. Everything the gateway needs to dispatch an agent.

    ``fmt`` is optional on both this and :class:`Connected` because the platforms
    disagree about which one carries it — SparkTG announces it on `connected`,
    Twilio/Plivo/Exotel on `start`, Vonage on `websocket:connected`. The bridge
    waits until it holds a format *and* a call id, so either order works and a
    dialect that has produced neither by its `Started` is a protocol error.
    """

    call_id: str | None = None
    from_number: str | None = None
    to_number: str | None = None
    # `inbound` / `outbound` where the protocol states one, None everywhere else.
    # Never guessed: a stream partner owns the dialling, so an invented direction
    # would read as a fact on exactly the calls they care most about. A partner's
    # own `direction` metadata key is read later, by `streams.parties`.
    direction: str | None = None
    # The per-call metadata bag, verbatim. Passed through to `{{vars.*}}` with no
    # mapping — see the module docstring on `services/streams/service.py`.
    params: Mapping[str, str] = field(default_factory=dict)
    fmt: AudioFormat | None = None
    # What outbound messages must echo, on the dialects that require it.
    stream_id: str | None = None


@dataclass(frozen=True, slots=True)
class Audio:
    """One slice of caller audio, in the announced wire format.

    Already base64-decoded where the dialect wrapped it; still encoded in the
    call's codec, because turning μ-law into PCM is `api/stream/media.py`'s job
    and not a spelling question.
    """

    payload: bytes


@dataclass(frozen=True, slots=True)
class Dtmf:
    """The caller pressed a key. Out-of-band, never detected in the audio."""

    digit: str


@dataclass(frozen=True, slots=True)
class Marked:
    """The echo of a marker we sent — their playback reached that point."""

    name: str


@dataclass(frozen=True, slots=True)
class Cleared:
    """They acknowledged a `clear`: whatever was still queued is gone."""


@dataclass(frozen=True, slots=True)
class Ended:
    """The call is over, with the platform's own reason where it gave one."""

    reason: str | None = None
    duration_ms: int | None = None


@dataclass(frozen=True, slots=True)
class Error:
    """Something failed on their side. `code` and `message` are theirs, verbatim."""

    code: str | None = None
    message: str | None = None


@dataclass(frozen=True, slots=True)
class Ignored:
    """An event this dialect does not recognise — a value, not an exception.

    Every one of these platforms asks for this in writing: *"Ignore unknown event
    names, unknown attribute keys, and unrecognised reason or error codes rather
    than treating them as failures. A client that rejects what it does not
    recognise will break on a routine platform update."*
    """

    event: str | None = None


type StreamEvent = Connected | Started | Audio | Dtmf | Marked | Cleared | Ended | Error | Ignored


# ── the dialect ─────────────────────────────────────────────────────────────


class StreamDialect(Protocol):
    """One platform's spelling of the stream protocol.

    Every method is pure. Nothing here opens a socket, reads a database or knows
    what a LiveKit room is.
    """

    name: StreamDialectName

    # True when a binary WS frame *is* audio and a text frame is control. False
    # when audio arrives base64-wrapped inside a JSON message.
    binary_audio: bool
    # Can we end the call, or only stop streaming? On four of the five, closing
    # the socket hands the caller back to the partner's own flow rather than
    # hanging up — see the per-dialect note on each `hangup`.
    can_hangup: bool
    # Whether this platform can emit DTMF into the telephony leg on our behalf.
    # Gates `RUNTIME_KEY_SEND_DTMF`, so on a stream whether the agent can dial a
    # keypad is a property of which platform is on the other end of the socket.
    can_send_dtmf: bool
    # Every outbound message must be EXACTLY one 20 ms frame, padded, never
    # short. SparkTG enforces this and ends the call on a violation.
    #
    # Nothing in `api/stream/bridge.py` branches on it, and that is the point:
    # the outbound path is written to this dialect and relaxed for the others, so
    # every message is already whole frames everywhere. It is here because
    # `scripts/stream_client.py` asserts it from the partner's side — which is
    # what makes "we never send a short frame" a regression test rather than a
    # promise — and because a sixth platform's author needs to know the rule
    # exists before they relax it.
    exact_frames: bool
    # Smallest outbound message this platform accepts. Exotel refuses anything
    # under 3200 bytes; everyone else takes whatever they are sent. The bridge
    # coalesces whole 20 ms frames up to this, which is latency this protocol
    # costs us rather than a choice we made.
    min_message_bytes: int
    # A caller number of bare digits is already international on this platform,
    # just without its `+`. Otherwise bare digits are read as national to the
    # business number's country — which silently files `447700900000` under +91.
    bare_numbers_are_international: bool

    def parse(self, message: Wire) -> StreamEvent:
        """One WebSocket message in, one event out. Never raises on an unknown event."""
        ...

    def audio_out(self, chunk: bytes, *, fmt: AudioFormat, stream_id: str | None) -> Wire:
        """Wrap already-encoded, already-sized audio for the wire."""
        ...

    def clear(self, stream_id: str | None) -> Wire | None:
        """Barge-in: discard whatever of ours is still queued. None where unsupported."""
        ...

    # There is deliberately no `mark`. Every one of these platforms can echo a
    # marker back when playback reaches it, which would be a TRUE playback
    # position — better than anything our SIP path has. It is left out because
    # half of it is worse than none: the worker's playback accounting comes from
    # `AudioSource`'s queue draining, one jitter buffer and one WS round trip
    # ahead of what the caller hears, and a mix of the two measures is worse than
    # either alone. `Marked` is still parsed, so a stray echo cannot break a call.

    def hangup(self, stream_id: str | None) -> Wire | None:
        """End the CALL, not the stream. None on every platform that cannot."""
        ...

    def send_dtmf(self, stream_id: str | None, digits: str) -> Sequence[Wire]:
        """Emit these digits onto the telephony leg.

        A sequence because the platforms disagree about arity: Plivo takes a
        whole string in one command, SparkTG one digit per command.
        """
        ...


def get_dialect(name: str) -> StreamDialect:
    """The implementation for a dialect name. Raises ValueError on an unknown one.

    A fresh instance every call, and that is load-bearing rather than tidy: a
    dialect may hold what it has to echo back for the life of one socket (Plivo's
    `playAudio` must repeat the content type and rate it was given), and a shared
    instance would leak one call's format into the next. The partner's own
    troubleshooting table lists exactly this — *"works on the first call, fails on
    the second: per-call state leaking across sockets"*.
    """
    from .exotel import ExotelDialect
    from .plivo import PlivoDialect
    from .sparktg import SparkTGDialect
    from .twilio import TwilioDialect
    from .vonage import VonageDialect

    if name == "twilio":
        return TwilioDialect()
    if name == "sparktg":
        return SparkTGDialect()
    if name == "plivo":
        return PlivoDialect()
    if name == "exotel":
        return ExotelDialect()
    if name == "vonage":
        return VonageDialect()
    raise ValueError(f"unknown stream dialect: {name}")


# ── helpers every adapter needs ─────────────────────────────────────────────


def read_params(bag: object) -> dict[str, str]:
    """A platform's per-call metadata bag, read defensively into plain strings.

    Never indexes: the bag may be absent entirely rather than empty (SparkTG
    omits `attributes` when no header survived), and a `KeyError` here happens on
    a real call with a caller listening.

    Values are stringified because that is what `{{vars.*}}` substitutes, and a
    platform that sends a number would otherwise reach a prompt as one.
    """
    if not isinstance(bag, Mapping):
        return {}
    out: dict[str, str] = {}
    for key, value in bag.items():
        if not isinstance(key, str) or not key.strip():
            continue
        if value is None or isinstance(value, (list, dict)):
            continue
        out[key.strip()] = str(value)
    return out


def read_kv_string(value: object) -> dict[str, str]:
    """A `k=v;k=v` metadata string, as Plivo's `extra_headers` carries it.

    Both `;` and `,` separate, because Plivo's own two reference pages disagree —
    the protocol reference shows `userId=12345;sessionId=abc-xyz` and the Stream
    XML page shows `userId=12345,sessionId=abc123`. One rule that accepts either
    is honest about a documented ambiguity; picking one would drop half a
    partner's metadata depending on which page they read.
    """
    if not isinstance(value, str) or not value.strip():
        return {}
    out: dict[str, str] = {}
    for pair in value.replace(",", ";").split(";"):
        key, sep, item = pair.partition("=")
        if not sep or not key.strip():
            continue
        out[key.strip()] = item.strip()
    return out


def read_phone(value: object) -> str | None:
    """A number off the wire, or None. Normalization belongs to `streams.parties`."""
    if not isinstance(value, str):
        return None
    return value.strip() or None


def read_text(value: object) -> str | None:
    """A non-empty string field, or None."""
    if not isinstance(value, str):
        return None
    return value.strip() or None


def read_int(value: object) -> int | None:
    """An integer field, whether the platform sent it as a number or as a string.

    Both spellings occur on the same field across these five: Exotel sends
    `sample_rate` as `"8000"` and Plivo sends `sampleRate` as `8000`.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def read_b64(value: object) -> bytes:
    """Decode one base64 audio payload, saying which field was wrong if it is."""
    if not isinstance(value, str):
        raise ProtocolError("media message carried no base64 payload")
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ProtocolError(f"media payload is not valid base64: {exc}") from exc
