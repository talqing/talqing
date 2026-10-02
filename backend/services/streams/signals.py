"""The envelope the stream gateway and the voice worker speak over the room.

A stream call has two processes that can see things the other cannot: the gateway
holds the socket and is the only one that ever sees the partner's own events, and
the worker owns the session — its row, its trace, its close reason. They are in
the same LiveKit room, so the room's data channel is the channel, and this module
is the one place its messages are written.

**Why the worker records the trace and the gateway does not.** `session_events`
allocates `seq` from an in-process counter, and the table's own comment says why:
*"Exactly one worker process owns a session for its whole life."* A gateway
writing rows for the same session would race that counter and silently drop the
loser through `ON CONFLICT DO NOTHING`. So everything the gateway learns before
dispatch travels in the dispatch metadata, and everything it learns afterwards
travels over this channel — one writer, one timeline, and no new table.

Stdlib only, so both a uvicorn app and a job process can import it without
dragging the other's dependencies in.
"""

from __future__ import annotations

import json
from typing import Any

# LiveKit data-packet topic. Namespaced like `talqing.frontend_rpc`, and
# deliberately distinct from it: a browser on a web call must never be able to
# address the gateway's vocabulary, and topic is what separates them.
STREAM_TOPIC = "talqing.stream"

# ── gateway → worker ────────────────────────────────────────────────────────

# The partner said the call is over, with their own reason. Carries the close
# reason the session should end on, already mapped — the gateway is the only
# process that knows which dialect's vocabulary the raw string came from.
SIGNAL_ENDED = "ended"
# The partner reported a failure of their own (`error` on SparkTG). Their code
# and message, verbatim, for the call's trace.
SIGNAL_ERROR = "error"
# They acknowledged our `clear`: whatever we had queued at their end is gone.
SIGNAL_CLEARED = "cleared"
# Ours, and reported by no protocol: the outbound path fell further behind real
# time, so the caller heard silence in the middle of the agent speaking. Carries
# `behind_ms`, the total drift, and is sent only when that grows.
SIGNAL_UNDERRUN = "underrun"
# Ours too: caller audio arrived late — a burst after a stall — and was dropped
# rather than delivered behind real time. Carries `dropped_ms` for one run of
# drops, sent once audio flows again.
SIGNAL_INBOUND_DROPPED = "inbound_dropped"

# ── worker → gateway ────────────────────────────────────────────────────────

# Barge-in. The agent's speech was cut off, so stop sending and tell the partner
# to drop whatever of ours is still queued.
SIGNAL_CLEAR = "clear"


def encode(kind: str, **payload: Any) -> bytes:
    """One signal, as a data packet. Compact because it rides the media path."""
    return json.dumps({"t": kind, **payload}, separators=(",", ":")).encode("utf-8")


def decode(data: bytes) -> tuple[str, dict[str, Any]]:
    """Read a signal, or ``("", {})`` for anything that is not one.

    Never raises. This is fed by whatever arrives on the room's data channel, and
    a malformed packet from a participant we did not expect must not be able to
    take a live call down.
    """
    try:
        message = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "", {}
    if not isinstance(message, dict):
        return "", {}
    kind = message.get("t")
    if not isinstance(kind, str) or not kind:
        return "", {}
    return kind, {k: v for k, v in message.items() if k != "t"}
