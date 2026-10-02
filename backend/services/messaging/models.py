"""Shared text-messaging wire shapes (SSE turn lifecycle, etc.)."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

# One vocabulary for a turn's lifecycle, on the wire and in the database
# (`conversation_items.turn_status`). The CHECK constraint in the data-plane
# schema lists the same four; keep them in step.
TurnStatus = Literal["running", "done", "error", "canceled"]


class TurnPayload(BaseModel):
    """Shared turn lifecycle for tenant conversation and CoPilot SSE.

    The `event` field is the discriminator every SSE payload on this API carries:
    it repeats the frame's `event:` line inside the JSON, so a generated client
    can narrow a stream with a plain `switch` instead of pairing each frame with
    its name by hand. `api.core.sse.sse` refuses to send a frame whose name and
    `event` field disagree.

    It is spelled `event` rather than `type` because a conversation item already
    uses `type` for its own kind (message, function_call, ...), and one field
    cannot mean two things on the same object.
    """

    # Response-only, and the tag is what a client narrows on, so it must be
    # `required` in the schema rather than optional-with-a-default. See
    # HealthSnapshotResponse for the general form of this.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    event: Literal["turn"] = "turn"
    turn_id: UUID
    status: TurnStatus
    session_id: str | None = None
    superseded_by_item_id: UUID | None = None
    error: str | None = None
    item_ids: list[str] | None = None
