"""CoPilot request/response shapes and public timeline message.

Row projection: ``services.copilot.items``.
Message enqueue / snapshot: ``services.copilot.service``.
"""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from services.conversations.models import ConversationItemRole
from services.messaging.models import TurnPayload as TurnEvent


class CopilotTextItem(BaseModel):
    """A plain message on the rail: the person's, or the CoPilot's reply."""

    # Response-only, and the tag is what a client narrows on, so it must be
    # `required` in the schema rather than optional-with-a-default. See
    # HealthSnapshotResponse for the general form of this.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    type: Literal["message"] = "message"
    # The row's own role. Closed by `conversation_items.role`, which the database
    # constrains — so this says the set rather than "some string".
    role: ConversationItemRole
    content: str


class CopilotFunctionCallItem(BaseModel):
    """The CoPilot calling one of its tools. `arguments` is JSON, as a string,
    because that is what the model emitted and what a replay must re-send."""

    # Response-only, and the tag is what a client narrows on, so it must be
    # `required` in the schema rather than optional-with-a-default. See
    # HealthSnapshotResponse for the general form of this.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    type: Literal["function_call"] = "function_call"
    call_id: str
    name: str
    arguments: str


class CopilotFunctionCallOutputItem(BaseModel):
    """What that tool answered."""

    # Response-only, and the tag is what a client narrows on, so it must be
    # `required` in the schema rather than optional-with-a-default. See
    # HealthSnapshotResponse for the general form of this.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    type: Literal["function_call_output"] = "function_call_output"
    call_id: str
    output: str


# One LiveKit ChatItem, of the three kinds a rail can show. `type` is present on
# all three — including the plain message, which is why it is a discriminated
# union a client can `switch` on rather than one it has to sniff by key.
CopilotItem = Annotated[
    CopilotTextItem | CopilotFunctionCallItem | CopilotFunctionCallOutputItem,
    Field(discriminator="type"),
]


class PublicMessage(BaseModel):
    """Dashboard SSE/message payload for one CoPilot timeline row."""

    # Response-only, and the tag is what a client narrows on, so it must be
    # `required` in the schema rather than optional-with-a-default. See
    # HealthSnapshotResponse for the general form of this.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    id: str
    item: CopilotItem
    role: ConversationItemRole
    content: str | None = None
    tool_calls: list[dict[str, str]] | None = None
    tool_call_id: str | None = None
    name: str | None = None
    created_at: str


class SendMessageRequest(BaseModel):
    text: str


class SendMessageResponse(BaseModel):
    turn_id: UUID
    # The id of what this CoPilot edits — an agent or a tool. The API name is
    # kept for clients; it is not a conversations.id.
    conversation_id: UUID


class SnapshotPayload(BaseModel):
    # Response-only, and the tag is what a client narrows on, so it must be
    # `required` in the schema rather than optional-with-a-default. See
    # HealthSnapshotResponse for the general form of this.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)
    messages: list[PublicMessage]
    busy: bool
    # Why the newest turn stopped, when it stopped badly. Here rather than only
    # on the `turn` SSE frame because that frame is gone the moment it is sent:
    # a rail that reloads, or reconnects a second late, would otherwise show a
    # build that simply stopped halfway with nothing said about why.
    error: str | None = None


# ── The CoPilot event stream ────────────────────────────────────────────────
#
# All three CoPilot rails (agent, tool, task) stream exactly these.
# See services.messaging.models.TurnPayload for why the discriminator is
# `event` and not `type`.


class CopilotSnapshotEvent(SnapshotPayload):
    """The whole conversation. First frame, and again on every reconnect."""

    # Response-only, and the tag is what a client narrows on, so it must be
    # `required` in the schema rather than optional-with-a-default. See
    # HealthSnapshotResponse for the general form of this.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    event: Literal["snapshot"] = "snapshot"


class CopilotMessageEvent(PublicMessage):
    """One new message on the rail — the person's, or the CoPilot's."""

    # Response-only, and the tag is what a client narrows on, so it must be
    # `required` in the schema rather than optional-with-a-default. See
    # HealthSnapshotResponse for the general form of this.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    event: Literal["message"] = "message"


CopilotEvent = Annotated[
    CopilotSnapshotEvent | CopilotMessageEvent | TurnEvent,
    Field(discriminator="event"),
]
