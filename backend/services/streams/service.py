"""Stream connections: CRUD for the published API, and resolve for the gateway.

**What a connection is.** The durable thing a partner is given — a dialect, an
agent, and the URL that answers. It is the analogue of a phone number: the thing
that answers. One socket against it becomes one `sessions` row of type `STREAM`,
and appears in Calls, Observability, Conversations and billing exactly like every
other call.

**Why the URL is shaped the way it is.** Twilio's `<Stream url>` does not support
a query string, which decides the whole scheme::

    wss://streams.<region>.talqing.com/v1/streams/connect/{dialect}/{tenant}/{agent}

Every routing hint has to be in the *path*. Taking the three segments in turn:

* **`dialect`** is half the connection's identity. One agent can answer on
  several platforms, one connection each, and this segment is what tells their
  URLs apart — and what picks the protocol parser before the first frame.
* **`tenant_id`** is not decoration — the lookup is impossible without it.
  `stream_connections` is a data-plane table, so it lives in the tenant's own
  database, and a region routes a tenant to a shard by id
  (`db.tenant_pool_for_id`). A lookup by any non-tenant key works only while
  every tenant shares one database and breaks silently — as a refused handshake —
  the day the first workspace is moved onto its own.
* **`agent_id`** is the other half. One URL is one agent, so (dialect, agent) is
  the connection's natural key: the URL is self-describing and there is no second
  public id to invent, store and explain.

**There is no fourth segment, and no credential anywhere.** Two UUIDv4s carry 244
bits between them and neither is guessable; what they are not is *revocable*, and
that is the cost this takes on knowingly. An agent id is an identifier we publish
by design — it is in the dashboard URL, in every `list_agents` response, in API
logs and in every CoPilot transcript — and the connect URL is derived from it, so
deleting a connection and creating another for the same agent and dialect hands
back the identical URL. A leaked URL is closed by `status = 'disabled'`, or by
pointing the connection at a different agent. There is no rotate, and the API
does not pretend there is one.

Two things follow that a reader should not have to work out:

* nothing here is secret, so the URL is returned in full on every read and the
  gateway's request path is safe to log; and
* the day a partner needs a revocable URL, the change is a `secret_digest`
  column on the row and a fourth path segment — deliberately the same shape this
  started as.
"""

from __future__ import annotations

from uuid import UUID

import asyncpg
from fastapi import HTTPException

from api.core.schemas import OkResponse, Page, coalesce, page_slice
from services.streams.dialects import STREAM_DIALECTS, get_dialect
from services.streams.models import (
    STREAM_CONNECTION_COLUMNS,
    CreateStreamConnectionRequest,
    PatchStreamConnectionRequest,
    StreamConnection,
    StreamConnectionResponse,
    StreamReadiness,
)
from services.user import Context
from settings import get_settings

# A host that cannot hold a certificate, and therefore cannot be reached over
# `wss://`. Only ever true on a laptop; every deployed region's `public_host` is
# a real name behind Caddy.
_LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "0.0.0.0", "host.docker.internal")


def connect_url(*, dialect: str, tenant_id: UUID, agent_id: UUID, public_host: str) -> str:
    """The URL a partner configures.

    Derived, never stored: the three segments are already columns, and a stored
    copy could only ever disagree with them after a rename of the host.

    `wss://` everywhere except loopback, which is a fact about certificates
    rather than a preference — `localhost` cannot hold one, so a laptop that
    printed `wss://localhost:8090` would print a URL nothing can dial.
    """
    scheme = "ws" if public_host.split(":")[0] in _LOOPBACK_HOSTS else "wss"
    return f"{scheme}://{public_host}/v1/streams/connect/{dialect}/{tenant_id}/{agent_id}"


def _public_host() -> str:
    host = get_settings().telephony.streams.public_host
    if not host:
        # Read and throw: a blank host would build a URL a partner cannot dial,
        # and they would discover it at their end rather than at ours.
        raise HTTPException(
            status_code=503,
            detail=(
                "media streams are not configured in this region "
                "(telephony.streams.public_host is unset)"
            ),
        )
    return host


def _readiness(row) -> StreamReadiness:
    if row["status"] == "disabled":
        return "disabled"
    if row["published_version"] is None:
        return "needs_agent"
    return "live"


def _out(row) -> StreamConnectionResponse:
    host = _public_host()
    return StreamConnectionResponse(
        id=row["id"],
        name=row["name"],
        dialect=row["dialect"],
        agent_id=row["agent_id"],
        agent_name=row["agent_name"],
        status=row["status"],
        readiness=_readiness(row),
        url=connect_url(
            dialect=row["dialect"],
            tenant_id=row["tenant_id"],
            agent_id=row["agent_id"],
            public_host=host,
        ),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


# The agent's name and whether it has ever been published, joined so `readiness`
# is one read rather than a query per row. INNER JOIN: the FK restricts, so a
# connection without its agent cannot exist.
_SELECT = """
SELECT sc.id, sc.tenant_id, sc.name, sc.dialect, sc.agent_id, sc.status,
       sc.created_by, sc.created_at, sc.updated_at,
       a.name AS agent_name, a.published_version
FROM stream_connections sc
JOIN agents a ON a.id = sc.agent_id AND a.tenant_id = sc.tenant_id
"""


async def _require_agent(agent_id: UUID, ctx: Context) -> None:
    """The agent this connection answers as must exist and be a voice agent.

    Checked at create/patch rather than at connect, because a partner discovering
    it has a caller on the line. Published is NOT required here — a workspace
    reasonably wires the integration up before the agent is finished, and
    `readiness` says `needs_agent` until it is. Publishing or rolling the agent
    back off `voice` is refused from the other side
    (`services.agents.service._answering_errors`).
    """
    pool = await ctx.tenant_pool()
    row = await pool.fetchrow(
        "SELECT config->>'channel' AS channel FROM agents WHERE id = $1 AND tenant_id = $2",
        agent_id,
        ctx.tenant.id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="agent not found")
    if row["channel"] != "voice":
        raise HTTPException(
            status_code=400,
            detail=(
                f"a {row['channel']} agent cannot answer a media stream - a stream carries "
                "audio, so it needs a voice agent"
            ),
        )


async def list_stream_connections(
    ctx: Context, limit: int = 200, offset: int = 0
) -> Page[StreamConnectionResponse]:
    pool = await ctx.tenant_pool()
    rows = await pool.fetch(
        f"{_SELECT} WHERE sc.tenant_id = $1 ORDER BY sc.created_at DESC LIMIT $2 OFFSET $3",
        ctx.tenant.id,
        limit + 1,
        offset,
    )
    return page_slice([_out(row) for row in rows], limit=limit, offset=offset)


async def get_stream_connection(connection_id: UUID, ctx: Context) -> StreamConnectionResponse:
    row = await _row(connection_id, ctx)
    return _out(row)


async def _row(connection_id: UUID, ctx: Context):
    pool = await ctx.tenant_pool()
    row = await pool.fetchrow(
        f"{_SELECT} WHERE sc.id = $1 AND sc.tenant_id = $2", connection_id, ctx.tenant.id
    )
    if not row:
        raise HTTPException(status_code=404, detail="stream connection not found")
    return row


async def create_stream_connection(
    body: CreateStreamConnectionRequest, ctx: Context
) -> StreamConnectionResponse:
    if body.dialect not in STREAM_DIALECTS:
        raise HTTPException(status_code=400, detail=f"unknown stream dialect: {body.dialect}")
    # Constructed rather than merely name-checked: an entry in the dialect list
    # with no implementation behind it would be a URL that accepts a socket and
    # then cannot read a frame.
    get_dialect(body.dialect)
    await _require_agent(body.agent_id, ctx)
    _public_host()

    pool = await ctx.tenant_pool()
    try:
        row = await pool.fetchrow(
            f"""
            WITH inserted AS (
                INSERT INTO stream_connections (
                    tenant_id, name, dialect, agent_id, created_by
                )
                VALUES ($1, $2, $3, $4, $5)
                RETURNING {STREAM_CONNECTION_COLUMNS}
            )
            SELECT inserted.*, a.name AS agent_name, a.published_version
            FROM inserted
            JOIN agents a ON a.id = inserted.agent_id AND a.tenant_id = inserted.tenant_id
            """,
            ctx.tenant.id,
            body.name,
            body.dialect,
            body.agent_id,
            ctx.user.id,
        )
    except asyncpg.UniqueViolationError as exc:
        # Two unique indexes, and the two mistakes behind them read very
        # differently to whoever hit one.
        if "uq_stream_connections_agent_dialect" in str(exc):
            raise HTTPException(
                status_code=409,
                detail=(
                    f"that agent already has a {body.dialect} stream connection - the URL is "
                    "derived from the dialect and the agent, so a second one would only ever "
                    "be the same URL under another name"
                ),
            ) from exc
        raise HTTPException(
            status_code=409, detail="a stream connection with that name already exists"
        ) from exc
    return _out(row)


async def patch_stream_connection(
    connection_id: UUID, body: PatchStreamConnectionRequest, ctx: Context
) -> StreamConnectionResponse:
    current = await _row(connection_id, ctx)
    if body.agent_id is not None and body.agent_id != current["agent_id"]:
        await _require_agent(body.agent_id, ctx)
    pool = await ctx.tenant_pool()
    try:
        row = await pool.fetchrow(
            f"""
            WITH updated AS (
                UPDATE stream_connections
                SET name = $3, agent_id = $4, status = $5, updated_at = now()
                WHERE id = $1 AND tenant_id = $2
                RETURNING {STREAM_CONNECTION_COLUMNS}
            )
            SELECT updated.*, a.name AS agent_name, a.published_version
            FROM updated
            JOIN agents a ON a.id = updated.agent_id AND a.tenant_id = updated.tenant_id
            """,
            connection_id,
            ctx.tenant.id,
            coalesce(body.name, current["name"]),
            coalesce(body.agent_id, current["agent_id"]),
            coalesce(body.status, current["status"]),
        )
    except asyncpg.UniqueViolationError as exc:
        raise HTTPException(
            status_code=409,
            detail=(
                f"that agent already has a {current['dialect']} stream connection"
                if "uq_stream_connections_agent_dialect" in str(exc)
                else "a stream connection with that name already exists"
            ),
        ) from exc
    return _out(row)


async def delete_stream_connection(connection_id: UUID, ctx: Context) -> OkResponse:
    pool = await ctx.tenant_pool()
    row = await pool.fetchrow(
        "DELETE FROM stream_connections WHERE id = $1 AND tenant_id = $2 RETURNING id",
        connection_id,
        ctx.tenant.id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="stream connection not found")
    return OkResponse()


# ── the gateway's side ──────────────────────────────────────────────────────


async def resolve_connection(
    pool, *, tenant_id: UUID, dialect: str, agent_id: UUID
) -> StreamConnection | None:
    """The connection a socket claims to be, by the three segments of its URL.

    One indexed read on `uq_stream_connections_agent_dialect`, and the whole
    reason the tenant id is in the path: with it, routing to the right shard is a
    dict lookup in this region's own config and there is no control-plane round
    trip at all. Returns the row whatever its status — whether a disabled
    connection is refused, and with which HTTP status, belongs to the caller.
    """
    row = await pool.fetchrow(
        f"SELECT {STREAM_CONNECTION_COLUMNS} FROM stream_connections "
        "WHERE tenant_id = $1 AND agent_id = $2 AND dialect = $3",
        tenant_id,
        agent_id,
        dialect,
    )
    return StreamConnection(row) if row else None
