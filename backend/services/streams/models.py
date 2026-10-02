"""Stream-connection domain model and API request/response shapes."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from services.streams.dialects import STREAM_DIALECTS, StreamDialectName

ConnectionName = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)
]

StreamConnectionStatus = Literal["active", "disabled"]

# What this connection can actually do right now, as one value — the same idea as
# `NumberReadiness` and computed here for the same reason: the dashboard, the
# CoPilot and both SDKs must read one answer rather than each re-deriving it.
StreamReadiness = Literal[
    "live",  # active, and its agent is published
    "needs_agent",  # the agent it points at has never been published
    "disabled",
]

# What every SELECT on this table reads. Shared so a hand-written query cannot
# miss a column and fail inside `StreamConnection` with a bare KeyError.
STREAM_CONNECTION_COLUMNS = """
id, tenant_id, name, dialect, agent_id, status, created_by, created_at, updated_at
"""


class StreamConnection:
    """One partner integration: a dialect, an agent, and the URL that answers.

    A plain class rather than a pydantic model because this is the gateway's
    view of the row and the response model below is the caller's; keeping them
    apart is what stops a column added here from reaching an API response by
    accident.
    """

    __slots__ = (
        "id",
        "tenant_id",
        "name",
        "dialect",
        "agent_id",
        "status",
        "created_by",
        "created_at",
        "updated_at",
    )

    def __init__(self, row: Mapping[str, Any]) -> None:
        self.id: UUID = row["id"]
        self.tenant_id: UUID = row["tenant_id"]
        self.name: str = row["name"]
        self.dialect: StreamDialectName = row["dialect"]
        self.agent_id: UUID = row["agent_id"]
        self.status: StreamConnectionStatus = row["status"]
        self.created_by: UUID | None = row["created_by"]
        self.created_at: datetime = row["created_at"]
        self.updated_at: datetime = row["updated_at"]


class CreateStreamConnectionRequest(BaseModel):
    name: ConnectionName = Field(
        description="What this partner integration is called in the dashboard."
    )
    dialect: StreamDialectName = Field(
        description=f"Which platform's WebSocket protocol this partner speaks. One of {', '.join(STREAM_DIALECTS)}."
    )
    agent_id: UUID = Field(description="The published voice agent that answers on this connection.")


class PatchStreamConnectionRequest(BaseModel):
    name: ConnectionName | None = None
    agent_id: UUID | None = None
    status: StreamConnectionStatus | None = None


class StreamConnectionResponse(BaseModel):
    """One connection, as a caller sees it.

    `url` is the whole configuration a partner needs, in full, on every read: it
    carries no credential, so there is nothing here to mask and nothing to show
    once and never again.
    """

    # Response-only: every field below is always sent, so a default must not
    # publish it as optional.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    id: UUID
    name: str
    dialect: StreamDialectName
    agent_id: UUID
    # Null when the agent has been deleted out from under it — which cannot
    # happen while the FK restricts, and is carried as optional so the response
    # does not depend on that staying true.
    agent_name: str | None = None
    status: StreamConnectionStatus
    readiness: StreamReadiness
    # The WebSocket URL to hand the partner. Derived from the three path
    # segments, so it is stable for the life of the connection and identical
    # for any connection pointed at the same agent on the same dialect — it is
    # an address, not a credential. Disabling the connection is what stops it
    # answering.
    url: str
    created_at: datetime
    updated_at: datetime
