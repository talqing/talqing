"""Stream dialects — one platform's spelling of the WebSocket media protocol each.

Import what you need from here::

    from services.streams.dialects import get_dialect, STREAM_DIALECTS

Submodules are package-internal; external callers should not import them.
"""

from __future__ import annotations

from .base import (  # noqa: F401
    STREAM_DIALECTS,
    Audio,
    AudioFormat,
    Cleared,
    Connected,
    Dtmf,
    Encoding,
    Ended,
    Error,
    Ignored,
    Marked,
    ProtocolError,
    Started,
    StreamDialect,
    StreamDialectName,
    StreamEvent,
    UnsupportedCodecError,
    Wire,
    get_dialect,
    read_codec,
    resolve_format,
)

__all__ = [
    "STREAM_DIALECTS",
    "Audio",
    "AudioFormat",
    "Cleared",
    "Connected",
    "Dtmf",
    "Encoding",
    "Ended",
    "Error",
    "Ignored",
    "Marked",
    "ProtocolError",
    "Started",
    "StreamDialect",
    "StreamDialectName",
    "StreamEvent",
    "UnsupportedCodecError",
    "Wire",
    "get_dialect",
    "read_codec",
    "resolve_format",
]
