"""Chat request/response shapes. A chat is the text counterpart of a call."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from api.core.schemas import JsonObject
from services.agents.plan import AgentPlanRequest
from services.analysis import AnalysisResponse
from services.attachments import MAX_IMAGES_PER_MESSAGE, InboundImage
from services.conversations.models import (
    AssistantCompletedEvent,
    AssistantDeltaEvent,
    AssistantStartedEvent,
    ContactUserdata,
    ConversationItemResponse,
    ItemCreatedEvent,
)
from services.messaging.models import TurnStatus
from services.session_snapshot import HealthSnapshotResponse
from services.sessions.models import (
    AgentMetadata,
    CostDetailResponse,
    CostResponse,
    SessionEventResponse,
)

ChatStatus = Literal["open", "ended"]


class CreateChatRequest(AgentPlanRequest):
    """Who the chat is with, what it starts knowing, and what runs.

    `vars` comes from `AgentPlanRequest` and is fixed for the life of the chat.
    """

    # Your own stable id for this person. Omit it for a single-use one.
    contact_key: str | None = Field(default=None, min_length=1, max_length=256)
    userdata: ContactUserdata | None = None


class ChatResponse(BaseModel):
    """One chat: a text session from its first message to its end."""

    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    id: UUID
    # Null once the chat's content has been erased.
    conversation_id: UUID | None = None
    contact_key: str | None = None
    agent: AgentMetadata
    status: ChatStatus
    close_reason: str | None = None
    created_at: datetime
    # When the first message opened it; null until then.
    started_at: datetime | None = None
    ended_at: datetime | None = None
    # Priced as it goes, so an open chat reports what it has cost so far.
    cost: CostResponse | None = None
    # Written when the chat ends; null while it is open.
    analysis: AnalysisResponse | None = None
    userdata: JsonObject = Field(default_factory=dict)


class ChatDetailResponse(BaseModel):
    """One chat with what explains it: health, priced lines, vars and trace."""

    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    chat: ChatResponse
    snapshot: HealthSnapshotResponse
    cost: CostDetailResponse | None = None
    # The `{{vars.*}}` values the chat was started with.
    vars: dict[str, str] = Field(default_factory=dict)
    events: list[SessionEventResponse]


class ChatMessageRequest(BaseModel):
    """One message: what was typed, what was attached, or both."""

    message: str = Field(default="", max_length=100_000)
    images: list[InboundImage] = Field(default_factory=list, max_length=MAX_IMAGES_PER_MESSAGE)
    # A UUID you generate; re-sending with the same one returns the same turn.
    client_message_id: UUID
    # true answers as a `text/event-stream` instead of one JSON body.
    stream: bool = False


class ChatTurnResponse(BaseModel):
    """What one message led to: the agent's reply, and where the chat stands."""

    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    input: ConversationItemResponse
    # `canceled`: a newer message arrived first. `running`: still working when
    # the wait ran out — read the chat's items later.
    status: TurnStatus
    error: str | None = None
    superseded_by_item_id: UUID | None = None
    # Everything this message produced, oldest first.
    items: list[ConversationItemResponse]
    # `ended` when this turn's agent ended the chat.
    chat_status: ChatStatus


class ChatTurnResultEvent(ChatTurnResponse):
    """The last frame of a streamed turn, carrying what the JSON response would."""

    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    event: Literal["turn.result"] = "turn.result"


# What a streamed turn sends, in the order it can: the reply as it is generated,
# each item the turn produces, and one closing `turn.result`.
CHAT_STREAM_EVENTS: tuple[type[BaseModel], ...] = (
    AssistantStartedEvent,
    AssistantDeltaEvent,
    AssistantCompletedEvent,
    ItemCreatedEvent,
    ChatTurnResultEvent,
)


class ChatTokenResponse(BaseModel):
    """A browser's credential for one chat."""

    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    chat_id: UUID
    token: str
    expires_at: datetime
    # Where the browser sends its requests: this region's API.
    api_url: str
