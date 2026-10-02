"""The WebSocket media-stream gateway: `uvicorn api.stream.main:app`.

A partner's contact-centre platform terminates the PSTN leg and connects one
socket per call here. We are the AI agent and nothing else — they keep the
number, the IVR, the queue, the recording, the CRM screen-pop and the wallboard.

**Its own container, not a router on `api.dataplane`.** A long-lived audio socket
has no business sharing an event loop with request/response traffic: one busy
partner would sit in the same process as every dashboard call in the region.

**Not part of the published API.** No `sdk_surface` entry, no OpenAPI, no MCP
tool. What a tenant can *configure* is `/v1/streams` on the data-plane API; this
is where the audio arrives, and its only caller is a partner's platform.

One flag on the command line is load-bearing rather than tidy:
`--ws-per-message-deflate false`. uvicorn's default is **on**, and the partner's
spec requires it off. Extensions are negotiated during the handshake, before our
connection lookup runs, so there is no per-dialect version of this question to
have. It is asserted at startup below, because a future default could reintroduce
it silently.

**Nothing in the request path is secret**, so the access log stays on and Caddy
logs this site like any other: the URL is `{dialect}/{tenant}/{agent}`, which is
three identifiers we publish elsewhere anyway, and one line per connect is the
first thing anyone debugging "it just doesn't connect" asks for. That was not
true of an earlier design whose URL carried a credential; if one ever comes back,
so does a redaction filter, because `--no-access-log` alone does NOT stop uvicorn
logging the path (its WebSocket server writes accept and reject lines through the
ERROR logger, which the flag does not touch).
"""

from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager
from uuid import UUID

from fastapi import FastAPI, WebSocket

import db
from api.stream.bridge import StreamBridge
from services import streams
from services.control import client as control
from services.streams.dialects import get_dialect
from services.telephony import livekit_sip
from settings import get_settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("talqing.api.stream")


_settings = get_settings()
if _settings.role != "region":
    # At import, the way `api.dataplane.main` does it: a stream endpoint on the
    # control droplet would be a stream endpoint with no tenant databases behind
    # it, and it should be a process that never starts rather than one that
    # accepts a partner's call and then cannot look it up.
    raise RuntimeError(
        f"api.stream is the REGIONAL media-stream gateway but ENV selected a "
        f"{_settings.role} config"
    )


def _assert_deflate_disabled() -> None:
    """Refuse to serve with per-message-deflate on.

    uvicorn's default is `True`, the partner's spec requires it off, and the
    setting is process-wide because extensions are negotiated during the
    handshake — before our connection lookup runs. So the command line sets it
    and this checks that it did, because a future flag reshuffle would otherwise
    turn compression back on for every partner without a word.

    Reading `sys.argv` rather than a uvicorn object because there is no supported
    way for an ASGI app to see its server's config, and this process has exactly
    one launcher.

    Measured once (2026-09-09, real speech, zlib level 6) so the trade is not
    re-opened: deflate leaves raw binary frames at 89-94% of their size and
    base64 JSON at 59-76%, and both the CPU it costs and the bandwidth it saves
    are around 0.1% of the platform fee per call-minute. It is off because the
    partner requires it, not because it is expensive.
    """
    flag = "--ws-per-message-deflate"
    argv = sys.argv
    for index, arg in enumerate(argv):
        if arg == flag and index + 1 < len(argv):
            value = argv[index + 1]
        elif arg.startswith(f"{flag}="):
            value = arg.split("=", 1)[1]
        else:
            continue
        if value.strip().lower() in ("false", "0", "no"):
            return
        break
    raise RuntimeError(
        "the media-stream gateway must run with `--ws-per-message-deflate false` - "
        "compression is negotiated in the handshake, before any of our code runs, "
        "and the partner protocol requires it off"
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    _assert_deflate_disabled()
    # No migrations here, deliberately: this process reads `stream_connections`
    # and writes `sessions`, and both are the data-plane API's schema to own.
    # Two processes racing the same migration lock at deploy is a cost with
    # nothing to buy.
    yield
    await livekit_sip.aclose()
    await control.aclose()
    await db.close_all()


app = FastAPI(
    title="Talqing media-stream gateway",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


@app.get("/health", include_in_schema=False)
async def health() -> dict[str, str]:
    """Liveness. The partner's own health check target, and the compose one.

    Deliberately does not touch Postgres or the control plane: a gateway that
    reports itself unhealthy is a gateway taken out of rotation, and neither of
    those being slow means this process cannot serve the sockets it already
    holds.
    """
    return {"status": "ok", "region": _settings.region or ""}


@app.websocket("/v1/streams/connect/{dialect}/{tenant_id}/{agent_id}")
async def connect(websocket: WebSocket, dialect: str, tenant_id: str, agent_id: str) -> None:
    """One call, from a partner's platform.

    Everything before the accept is bounded work: a dict lookup to find the
    tenant's shard, then one indexed read. That is single-digit milliseconds
    behind pgbouncer, which matters because a partner's accept deadline can be as
    short as 5 seconds and missing it drops the call with no event on either side.

    A refusal is an **HTTP status**, not a close code. It reaches uvicorn's
    `websocket.http.response` ASGI extension, so a partner sees 403 in their own
    logs — and on this channel a closed socket is the only other signal they get.
    """
    try:
        tenant_uuid = UUID(tenant_id)
        agent_uuid = UUID(agent_id)
    except ValueError:
        await _deny(websocket, 404, "unknown stream connection")
        return

    try:
        adapter = get_dialect(dialect)
    except ValueError:
        await _deny(websocket, 404, f"unknown stream dialect: {dialect}")
        return

    # A dict lookup in this region's own `postgres.data.tenant_overrides` — no
    # control-plane round trip, which is the whole reason the tenant id is a
    # segment of the URL rather than something to be resolved from a shorter one.
    pool = await db.tenant_pool_for_id(tenant_uuid)
    connection = await streams.resolve_connection(
        pool, tenant_id=tenant_uuid, dialect=dialect, agent_id=agent_uuid
    )

    # One refusal for every way the URL can be wrong, so a caller cannot learn
    # which agent ids exist by watching the statuses. `logged` is what we say to
    # ourselves.
    if connection is None:
        await _deny(websocket, 403, "unknown stream connection", logged="no such connection")
        return
    if connection.status != "active":
        # Told apart from the one above, and only here: the URL was right, so
        # there is nothing an attacker learns from the difference and a partner
        # debugging a disabled integration should be told which it is.
        await _deny(websocket, 403, "this stream connection is disabled")
        return

    logger.info(
        "stream connect accepted",
        extra={
            "dialect": dialect,
            "tenant": tenant_id,
            "agent": agent_id,
            "connection_id": str(connection.id),
        },
    )
    await StreamBridge(websocket=websocket, dialect=adapter, connection=connection, pool=pool).run()


async def _deny(
    websocket: WebSocket, status: int, detail: str, *, logged: str | None = None
) -> None:
    """Refuse the handshake with a real HTTP response.

    The log line is the only record a refusal leaves, so it carries the path —
    `{dialect}/{tenant}/{agent}` names the connection that was aimed at.
    """
    logger.warning(
        "refusing stream handshake %s with %d: %s",
        websocket.url.path,
        status,
        logged or detail,
    )
    if "websocket.http.response" not in websocket.scope.get("extensions", {}):
        # The server did not advertise the Websocket Denial Response extension.
        # uvicorn does; anything else running this app leaves the partner with a
        # close code, which is worse but is still a refusal.
        await websocket.close(code=1008, reason=detail[:120])
        return
    body = detail.encode("utf-8")
    # Status and body only — deliberately NO headers. uvicorn's WebSocket server
    # builds the rejection with `ServerProtocol.reject`, which sets its own
    # `Content-Type` and `Content-Length` and then *appends* whatever the ASGI
    # message carried. A `PlainTextResponse` here therefore produces two of each,
    # and the client sees a malformed HTTP response instead of a 403 it can read
    # in its own logs — which on this channel is the only signal a partner gets.
    await websocket.send({"type": "websocket.http.response.start", "status": status, "headers": []})
    await websocket.send({"type": "websocket.http.response.body", "body": body})
