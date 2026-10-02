"""Codec, framing and padding for a media stream. Pure functions over bytes.

No async, no LiveKit, no sockets — so every trap in the protocols can be checked
without a live call.

The split with `services/streams/dialects/` is: a dialect owns *spelling* (which
field the audio is in, what an event is called), this owns *bytes* (μ-law tables,
byte order, frame sizing, the padded tail). So μ-law appears in exactly one place
however many platforms speak it, and a platform's field names appear in exactly
one place however many codecs it offers.

numpy rather than `audioop`: `audioop` was removed from the standard library in
Python 3.13 and this image is on 3.12 (`backend/Dockerfile`), so reaching for it
now would buy a migration later. numpy is already a dependency.
"""

from __future__ import annotations

import numpy as np

from services.streams.dialects import AudioFormat

# ── G.711 μ-law ─────────────────────────────────────────────────────────────
#
# Two 256- and 65536-entry lookup tables, built once at import. A table is both
# faster than arithmetic and exactly reversible, which matters because the
# loopback echo test (the first thing to run against a real partner) is only a
# proof of transport if the codec is not quietly changing the samples.

_MULAW_BIAS = 0x84
# The clip point in the 14-bit domain G.711 encodes in, not in the 16-bit domain
# the samples arrive in. `8159` rather than `32635` is the difference between
# matching the reference codec and being one code out at the top of the range.
_MULAW_CLIP_14 = 8159
# G.711's segment boundaries, searched in order: the segment is the index of the
# first bound the biased magnitude fits under, or 8 (overflow) if none.
_MULAW_SEGMENTS = (0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF, 0x1FFF)


def _build_mulaw_decode() -> np.ndarray:
    """μ-law byte → int16 sample, ITU-T G.711 (`st_ulaw2linear16`)."""
    codes = np.arange(256, dtype=np.int32)
    inverted = ~codes & 0xFF
    magnitude = (((inverted & 0x0F) << 3) + _MULAW_BIAS) << ((inverted & 0x70) >> 4)
    return np.where(inverted & 0x80, _MULAW_BIAS - magnitude, magnitude - _MULAW_BIAS).astype(
        np.int16
    )


def _build_mulaw_encode() -> np.ndarray:
    """int16 sample → μ-law byte, indexed by (sample + 32768).

    Follows `st_14linear2ulaw` exactly, including the two details that are easy
    to get subtly wrong and that produce a codec which sounds fine and is not the
    one everyone else on the call is using:

    * the sample is arithmetically shifted down to 14 bits **before** the sign is
      taken, so negatives floor rather than truncate toward zero;
    * the magnitude is clipped in that 14-bit domain.

    Verified byte-for-byte against `audioop.lin2ulaw` over all 65 536 inputs when
    it was written, which is the point of writing it this way rather than from the
    shape of the format.
    """
    values = np.arange(-32768, 32768, dtype=np.int32) >> 2
    mask = np.where(values < 0, 0x7F, 0xFF)
    magnitude = np.minimum(np.abs(values), _MULAW_CLIP_14) + (_MULAW_BIAS >> 2)
    segment = np.full_like(magnitude, len(_MULAW_SEGMENTS))
    for index, bound in reversed(list(enumerate(_MULAW_SEGMENTS))):
        segment = np.where(magnitude <= bound, index, segment)
    # An overflowing magnitude encodes as 0x7F before inversion, which is what
    # the reference returns for a sample at the clip point.
    encoded = np.where(
        segment >= len(_MULAW_SEGMENTS),
        0x7F,
        (segment << 4) | ((magnitude >> (segment + 1)) & 0x0F),
    )
    return ((encoded ^ mask) & 0xFF).astype(np.uint8)


_MULAW_DECODE = _build_mulaw_decode()
_MULAW_ENCODE = _build_mulaw_encode()


# ── wire format ⇄ PCM16 little-endian ───────────────────────────────────────
#
# PCM16 little-endian is what `rtc.AudioFrame` takes and what `rtc.AudioStream`
# hands back, so it is the one internal representation and every conversion is
# to or from it.


def decode(payload: bytes, fmt: AudioFormat) -> bytes:
    """Their audio → PCM16 little-endian mono at their own sample rate.

    Deliberately does not resample. Upsampling μ-law 8 kHz to 16 kHz invents
    nothing and costs CPU on every frame of every call; the LiveKit
    ``AudioSource`` is built at the announced rate instead and everything
    downstream of the SFU handles the difference.
    """
    if fmt.encoding == "pcm_s16le":
        # Already the internal representation — but still trimmed, because this
        # is the one path where the bytes reach `rtc.AudioFrame` untouched and
        # it raises on a length that is not a whole number of samples. A partner
        # that sends one short frame must not be able to end the call.
        return _even(payload)
    if fmt.encoding == "pcm_s16be":
        # `>i2` reads big-endian; `astype` with a native dtype writes back
        # little-endian. This is the conversion whose absence is "I hear static"
        # on every 8 kHz SparkTG call and on nothing else.
        return np.frombuffer(_even(payload), dtype=">i2").astype("<i2").tobytes()
    return _MULAW_DECODE[np.frombuffer(payload, dtype=np.uint8)].astype("<i2").tobytes()


def encode(pcm: bytes, fmt: AudioFormat) -> bytes:
    """PCM16 little-endian mono → their audio, at whatever rate they announced.

    ``pcm`` is expected to already be at ``fmt.sample_rate``: the LiveKit
    ``AudioStream`` the agent's track is read through is constructed with that
    rate and resamples for us, so there is no resampler here and there should
    not be one.
    """
    if fmt.encoding == "pcm_s16le":
        return pcm
    if fmt.encoding == "pcm_s16be":
        return np.frombuffer(_even(pcm), dtype="<i2").astype(">i2").tobytes()
    samples = np.frombuffer(_even(pcm), dtype="<i2").astype(np.int32)
    return _MULAW_ENCODE[samples + 32768].tobytes()


def _even(payload: bytes) -> bytes:
    """Drop a trailing odd byte before reading 16-bit samples.

    A half sample is not a sample. It should not happen — every producer on both
    sides of this is frame-aligned — but a partner's platform is not ours to
    trust, and the alternative is a `ValueError` from deep inside the SDK that
    ends a live call over one stray byte. Trim and carry on: this is exactly the
    forward-compatibility posture every one of these protocols asks for, applied
    to bytes instead of event names.
    """
    return payload[: len(payload) - (len(payload) % 2)] if len(payload) % 2 else payload


# ── framing ─────────────────────────────────────────────────────────────────


def pad_to_frame(chunk: bytes, fmt: AudioFormat) -> bytes:
    """Extend a short tail to a whole 20 ms frame with silence.

    SparkTG ends the call on a single wrong-sized frame — `error:
    invalid_audio_frame`, then `end: integrator_invalid_codec`, with no tolerance
    and no recovery. A synthesised utterance is almost never an exact multiple of
    640 bytes, so without this the failure lands on the first real TTS response
    of the first real call and reads as "the call drops a second after the agent
    speaks".

    Silence is 0x00 for linear and 0xFF for μ-law — 0x00 in μ-law is close to
    full-scale, so padding with the wrong byte is an audible click on every
    utterance rather than a quiet one.
    """
    remainder = len(chunk) % fmt.frame_bytes
    if not remainder:
        return chunk
    return chunk + bytes([fmt.silence_byte]) * (fmt.frame_bytes - remainder)


def split_frames(chunk: bytes, fmt: AudioFormat) -> list[bytes]:
    """Cut a padded buffer into exact 20 ms frames.

    Raises if the buffer is not a whole number of frames, rather than emitting a
    short one: on the strict dialect a short frame is a dropped call, and a
    caller who can produce one has a bug that a silent truncation would hide.
    """
    if len(chunk) % fmt.frame_bytes:
        raise ValueError(
            f"{len(chunk)} bytes is not a whole number of {fmt.frame_bytes}-byte frames "
            f"for {fmt} - pad_to_frame first"
        )
    return [chunk[i : i + fmt.frame_bytes] for i in range(0, len(chunk), fmt.frame_bytes)]


def silence(fmt: AudioFormat, *, frames: int = 1) -> bytes:
    """N whole frames of silence in the wire format — the shape of a padded tail."""
    return bytes([fmt.silence_byte]) * (fmt.frame_bytes * frames)
