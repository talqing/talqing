"""WebSocket media streams — a partner's contact centre, answered by our agent.

Import what you need from here::

    from services import streams
    connection = await streams.resolve_connection(pool, tenant_id=…, agent_id=…)

Submodules are package-internal; external callers should not import them, with
one exception: `services.streams.dialects` is its own public surface, because the
gateway needs an implementation and the API needs the name list.
"""

from __future__ import annotations

from .models import (  # noqa: F401
    STREAM_CONNECTION_COLUMNS,
    ConnectionName,
    CreateStreamConnectionRequest,
    PatchStreamConnectionRequest,
    StreamConnection,
    StreamConnectionResponse,
    StreamConnectionStatus,
    StreamReadiness,
)
from .parties import CallParties, resolve_parties  # noqa: F401
from .service import (  # noqa: F401
    connect_url,
    create_stream_connection,
    delete_stream_connection,
    get_stream_connection,
    list_stream_connections,
    patch_stream_connection,
    resolve_connection,
)

__all__ = [
    "STREAM_CONNECTION_COLUMNS",
    "CallParties",
    "ConnectionName",
    "CreateStreamConnectionRequest",
    "PatchStreamConnectionRequest",
    "StreamConnection",
    "StreamConnectionResponse",
    "StreamConnectionStatus",
    "StreamReadiness",
    "connect_url",
    "create_stream_connection",
    "delete_stream_connection",
    "get_stream_connection",
    "list_stream_connections",
    "patch_stream_connection",
    "resolve_connection",
    "resolve_parties",
]
