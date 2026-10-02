"""Server-Sent Events: the response type, and one stream over Redis pub/sub.

Four endpoints stream to a browser — the three CoPilot rails and a text
conversation — and all four are the same machine: subscribe to a Redis channel,
send a snapshot of where things stand, then relay what the workers publish until
the client goes away. Only the channel and the snapshot differ between them, so
only those are arguments.

Redis is here because the two ends are in different processes: turns run on the
text-worker, while the open connection is on an API replica. Nothing on the bus
is canonical — every snapshot is read from Postgres, so an event dropped while
no subscriber was attached self-heals on the client's next reconnect.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from typing import Any

import anyio
from fastapi.responses import StreamingResponse

from api.core.schemas import JsonObject
from iredis.client import get_redis

# Long enough to be quiet, short enough to beat the idle timeout of anything
# that might sit in front of us (a load balancer's is typically 60s).
KEEPALIVE_SECONDS = 15

# What a stream sends before any live event: the state the client should start
# from. `None` means there is nothing to send — the caller looked and the
# resource had no snapshot to give.
Snapshot = Callable[[], Awaitable[JsonObject | None]]

# An SSE `id:` for one frame, or None to send none.
EventId = Callable[[JsonObject], str | None]


def sse(data: JsonObject, *, event_id: str | None = None) -> str:
    """One Server-Sent Events frame, named by the payload itself.

    Every payload on this API carries an `event` field, so the frame's name is
    read off the body rather than passed beside it. That is deliberate: the two
    used to be separate arguments, and a client narrowing the stream on that
    field could be told one thing by the `event:` line and another by the JSON.
    Deriving one from the other removes the disagreement instead of checking
    for it.
    """
    event = data.get("event")
    if not isinstance(event, str):
        raise RuntimeError(f"SSE payload has no `event` field to name the frame: {data!r}")
    id_line = f"id: {event_id}\n" if event_id is not None else ""
    return f"{id_line}event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


class EventStreamResponse(StreamingResponse):
    """An SSE stream, with the headers that keep it flowing end to end.

    `text/event-stream` as a class attribute is also what FastAPI reads the
    published media type off: a `StreamingResponse` leaves it unset, and the
    OpenAPI document then tells every generated client to parse a stream that
    never ends as a single JSON body.
    """

    media_type = "text/event-stream"

    # `status_code` is spelled out rather than swept into **kwargs because
    # FastAPI reads a route's default status code off this signature, and an
    # endpoint that only says `response_class=EventStreamResponse` gives it
    # nothing else to read.
    def __init__(self, content: Any, status_code: int = 200, **kwargs: Any) -> None:
        super().__init__(
            content,
            status_code=status_code,
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                # nginx and Caddy buffer a response by default, which holds
                # every frame until the stream ends — i.e. forever.
                "X-Accel-Buffering": "no",
            },
            **kwargs,
        )


# The frame format is the same on every SSE route; the events differ, so each
# route passes its own union and the published document says exactly what that
# stream can send.
def event_stream_responses(events: Any) -> dict[int | str, dict[str, Any]]:
    """The 200 an SSE route declares, given the union of events it sends.

    Passing the union rather than `{"type": "string"}` is what lets a generated
    client hand back a typed, discriminated stream instead of raw text. FastAPI
    renders `model` under the route's own media type, which `EventStreamResponse`
    fixes at `text/event-stream`.
    """
    return {
        200: {
            "model": events,
            "description": (
                "A `text/event-stream` that stays open. Each frame is `event: <name>` "
                "followed by `data: <json>`, and the JSON repeats the name in its "
                "`event` field so one value identifies the frame. A line starting `:` "
                "is a keep-alive, sent every 15s so idle proxies do not close the "
                "connection. Read it with an SSE client (`EventSource`, `httpx-sse`) — "
                "the body is never a single JSON document."
            ),
        }
    }


def decode_event(message: Mapping[str, Any]) -> JsonObject | None:
    """One event payload off the bus, or None if the message is not one.

    Both publishers — `services.conversations.events` and
    `services.copilot.stream` — put a dumped event model
    on the wire and nothing else, so the payload is already self-describing.
    Anything that does not parse is dropped rather than raised: one malformed
    payload must not take a live stream down with it.
    """
    raw = message.get("data")
    if not isinstance(raw, str):
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("event"), str):
        return None
    return payload


async def redis_event_stream(
    channel: str,
    *,
    snapshot: Snapshot,
    event_id: EventId | None = None,
) -> AsyncIterator[str]:
    """Subscribe, send the snapshot, then relay until the client disconnects."""
    redis = get_redis()
    # `RedisCluster` builds its slot map lazily on the first command — but
    # `ClusterPubSub.execute_command` is the one path that skips that and reads
    # the map directly, so a SUBSCRIBE issued before any other Redis command
    # raises KeyError on a freshly started process. Ask for it explicitly;
    # `initialize` is lock-guarded and returns at once once it has run.
    await redis.initialize()
    pubsub = redis.pubsub()
    await pubsub.subscribe(channel)
    try:
        # Taken after subscribing, never before: an event published in the gap
        # would otherwise be missing from the snapshot *and* never delivered.
        first = await snapshot()
        if first is not None:
            yield sse(first)

        while True:
            message = await pubsub.get_message(
                ignore_subscribe_messages=True, timeout=KEEPALIVE_SECONDS
            )
            # Nothing published within the window. A comment frame keeps the
            # connection warm and fires no handler on the client.
            if message is None:
                yield ": keep-alive\n\n"
                continue
            data = decode_event(message)
            if data is None:
                continue
            yield sse(data, event_id=event_id(data) if event_id else None)
    finally:
        # Starlette cancels this generator when the client disconnects, so this
        # runs inside a cancelled scope. Without the shield the first `await`
        # re-raises CancelledError and `aclose` never happens — one Redis
        # connection and one live subscription leaked per closed tab, and SSE
        # clients reconnect on their own, so it compounds. `suppress` cannot
        # stand in for the shield: CancelledError is a BaseException.
        with anyio.CancelScope(shield=True), contextlib.suppress(Exception):
            await pubsub.unsubscribe(channel)
            await pubsub.aclose()
