"""Conversation domain structs and service request/response shapes.

Row mappers and SQL live in ``service``; Redis SSE in ``events``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator

from api.core.schemas import JsonObject
from services.analysis import AnalysisResponse
from services.attachments import ConversationAttachment
from services.billing import SessionUsage
from services.messaging.models import TurnPayload as TurnEvent
from services.messaging.models import TurnStatus
from services.sessions.models import AgentMetadata, CostResponse, SessionEventResponse
from services.userdata import RESERVED_KEY_ERROR, is_reserved_key
from utils.latency import ReplyLatency

ConversationItemDirection = Literal["inbound", "outbound", "internal"]
# Mirrors livekit.agents.llm.chat_context.ChatItem discriminators.
ConversationItemType = Literal[
    "message",
    "function_call",
    "function_call_output",
    "agent_handoff",
    "agent_config_update",
]
# livekit.agents.llm.ChatRole, plus the "tool" we stamp on a function call or
# its output — LiveKit gives those no role of their own.
ConversationItemRole = Literal["developer", "system", "user", "assistant", "tool"]
ConversationItemVisibility = Literal["customer_visible", "internal"]
DeliveryStatus = Literal["not_applicable", "pending", "sending", "sent", "failed", "skipped"]
ConversationActivityState = Literal["running", "idle"]


class DeliveryError(BaseModel):
    """Outbound delivery failure detail (provider HTTP / shadow-mode skips).

    Writers always store `{"message": ...}` (see services.messaging.delivery).
    """

    message: str


class SessionMetrics(BaseModel):
    """Session-level metrics written at seal (voice finalize / text finalize).

    Latency averages are milliseconds over every message that reported the
    metric (same rule as call detail / workspace observability). Turn-level
    samples live on conversation_items.metrics for workspace daily aggregates.
    On a chat, `e2e_latency` is a message arriving → the first token back.
    """

    usage_reported: bool = False
    llm_node_ttft: float | None = None
    tts_node_ttfb: float | None = None
    transcription_delay: float | None = None
    e2e_latency: float | None = None
    end_of_turn_delay: float | None = None
    turns: int = 0


class SessionError(BaseModel):
    """Structured session failure payload when status is failed.

    The text seal writes `message` + the exception `type`.
    """

    message: str | None = None
    type: str | None = None


class ConversationRefSummary(BaseModel):
    """The contact this conversation belongs to: key, binding, and what we know."""

    contact_key: str | None = None
    kind: str | None = None
    bind_id: UUID | None = None
    # Convenience: bind_id when kind=integration (channel account).
    integration_id: UUID | None = None
    metadata: JsonObject = Field(default_factory=dict)
    # Convenience for UI: profile_name / username / wa_id when present.
    display_name: str | None = None


class ConversationResponse(BaseModel):
    """One conversation, and the contact it belongs to.

    ``id`` identifies THIS conversation. ``contact_key`` identifies the caller or
    endpoint it is with, and is shared by every conversation with them - use it
    to list a contact's whole history (``GET /v1/conversations?contact_key=…``).
    """

    # Response-only: every field below is always sent, so a default must not
    # publish it as optional. See HealthSnapshotResponse for the full reason.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    id: UUID
    contact_key: str | None = None
    ref: ConversationRefSummary | None = None
    # The agent that answered this conversation's newest session, on any channel.
    # Null when nothing has run on it yet, or the agent was defined inline.
    agent_id: UUID | None = None
    agent_name: str | None = None
    surface: str | None = None  # telegram | web | sip | …
    # `inactive`: we reached out and the contact has not replied or answered yet.
    status: Literal["active", "inactive"]
    last_message_text: str | None = None
    last_message_at: datetime | None = None
    summary: str | None = None
    # The CONTACT's bag, not this conversation's: what the agent has learned
    # about this person across every conversation with them, so every thread
    # with the same caller shows the same value. For what one call ended up
    # knowing, read ``ConversationSessionResponse.userdata``.
    userdata: JsonObject = Field(default_factory=dict)
    metadata: JsonObject = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class ModelMetadata(BaseModel):
    """Which model a metrics sample came from."""

    model_config = ConfigDict(extra="allow")

    model_provider: str | None = None
    model_name: str | None = None


class TurnMetrics(BaseModel):
    """LiveKit MetricsReport samples for one transcript row, in seconds.

    Every field is optional and unknown keys are kept: LiveKit owns this bag,
    so a version of it that reports something new must not fail a transcript
    write. What is named here is what the platform reads — the rest rides along
    verbatim.

    Which fields are present depends on the row: `transcription_delay` and
    `end_of_turn_delay` are measured on the caller's message, the rest on the
    reply that closed the turn.
    """

    model_config = ConfigDict(extra="allow")

    # User messages: end of speech -> final transcript, and -> the decision
    # that the turn is over.
    transcription_delay: float | None = None
    end_of_turn_delay: float | None = None
    on_user_turn_completed_delay: float | None = None
    # Assistant messages: turn closed -> first response token, and -> first
    # audio frame out.
    llm_node_ttft: float | None = None
    tts_node_ttfb: float | None = None
    llm_tokens_per_second: float | None = None
    # Caller stopped speaking -> agent started speaking. The one a listener
    # would call "how long the silence was".
    e2e_latency: float | None = None
    started_speaking_at: float | None = None
    stopped_speaking_at: float | None = None
    # Which model produced this turn's half of the pipeline.
    llm_metadata: ModelMetadata | None = None
    tts_metadata: ModelMetadata | None = None
    stt_metadata: ModelMetadata | None = None


class ConversationItemResponse(BaseModel):
    # Response-only: every field below is always sent, so a default must not
    # publish it as optional. See HealthSnapshotResponse for the full reason.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)
    id: UUID
    conversation_id: UUID
    session_id: UUID | None = None
    trigger_item_id: UUID | None = None
    direction: ConversationItemDirection
    type: ConversationItemType
    role: ConversationItemRole | None = None
    agent_id: UUID | None = None
    # "draft" when the session ran this agent's unpublished draft.
    agent_version: int | Literal["draft"] | None = None
    # NULL, never '', for a message that carries only images: the inbox
    # preview tests for non-blank text and the chat-context rebuilder tests
    # truthiness, so an empty string would be a third state read two ways.
    text: str | None = None
    # Signed at read time (`services.attachments.signer`); the object key behind
    # each one is stored and never returned.
    attachments: list[ConversationAttachment] = Field(default_factory=list)
    provider_message_id: str | None = None
    client_message_id: str | None = None
    delivery_status: DeliveryStatus
    delivery_error: DeliveryError | None = None
    # On the message that opened a turn: where that turn stands, and why it
    # stopped. Null on everything a turn produced.
    turn_status: TurnStatus | None = None
    turn_error: str | None = None
    source: str | None = None
    visibility: ConversationItemVisibility
    # LiveKit MetricsReport samples (seconds) for message items.
    metrics: TurnMetrics | None = None
    # How a user message arrived. Null on everything else.
    origin: Literal["speech", "typed", "keypad"] | None = None
    # On the reply a wait ended on: how long it took, and on what. Null on live
    # frames and to chat-token callers.
    latency: ReplyLatency | None = None
    metadata: JsonObject = Field(default_factory=dict)
    raw_payload: JsonObject | None = None
    created_at: datetime
    updated_at: datetime

    @field_validator("delivery_error", mode="before")
    @classmethod
    def _delivery_error(cls, value: object) -> object:
        if value is None or value == {}:
            return None
        return value

    @field_validator("metadata", "raw_payload", mode="before")
    @classmethod
    def _json_object(cls, value: object) -> object:
        if value is None:
            return None
        if not isinstance(value, dict):
            raise TypeError("expected a JSON object")
        return value


class ConversationSessionResponse(BaseModel):
    """One agent execution within a conversation: a call, or a chat."""

    # Response-only: every field below is always sent, so a default must not
    # publish it as optional. See HealthSnapshotResponse for the full reason.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    # Also its `get_call` or `get_chat` id.
    id: UUID
    conversation_id: UUID
    trigger_id: UUID | None = None
    integration_id: UUID | None = None
    conversation_ref_id: UUID | None = None
    agent: AgentMetadata
    agent_version_id: UUID | None = None
    # "draft" when it ran the agent's unpublished draft.
    agent_version: int | Literal["draft"] | None = None
    type: str
    channel: str
    status: str
    close_reason: str | None = None
    # The same ending in a sentence.
    close_reason_label: str | None = None
    # The `{{vars.*}}` values this session was started with.
    vars: dict[str, str] = Field(default_factory=dict)
    # Post-session analysis, in the shape `get_call` and the webhook carry.
    analysis: AnalysisResponse
    usage: SessionUsage
    cost: CostResponse | None = None
    duration_s: int | None = None
    metrics: SessionMetrics = Field(default_factory=SessionMetrics)
    error: SessionError | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class ConversationTraceEventResponse(SessionEventResponse):
    """One trace row of a conversation, and the session it belongs to."""

    session_id: UUID


class ConversationTraceResponse(BaseModel):
    """A conversation's trace over one window, oldest first."""

    events: list[ConversationTraceEventResponse]


def _no_reserved_keys(bag: JsonObject) -> JsonObject:
    if any(is_reserved_key(k) for k in bag):
        raise ValueError(RESERVED_KEY_ERROR)
    return bag


# A contact's userdata as a request writes it — every session with them starts from it.
ContactUserdata = Annotated[JsonObject, AfterValidator(_no_reserved_keys)]


class PatchConversationRequest(BaseModel):
    summary: str | None = None
    # Writes the CONTACT's bag, so it changes what every conversation with this
    # caller shows - and what the next call starts from when the agent is set to
    # initialize from it.
    userdata: ContactUserdata | None = None
    metadata: JsonObject | None = None


class ConversationActivity(BaseModel):
    """Whether a turn is being answered right now (SSE snapshot activity)."""

    # Response-only, and the tag is what a client narrows on, so it must be
    # `required` in the schema rather than optional-with-a-default. See
    # HealthSnapshotResponse for the general form of this.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    state: ConversationActivityState


class ConversationEventSnapshot(BaseModel):
    """Canonical conversation + items for the SSE reconnect frame."""

    # Response-only, and the tag is what a client narrows on, so it must be
    # `required` in the schema rather than optional-with-a-default. See
    # HealthSnapshotResponse for the general form of this.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    conversation: ConversationResponse
    items: list[ConversationItemResponse]
    activity: ConversationActivity


# ── The conversation event stream ───────────────────────────────────────────
#
# GET /v1/conversations/{id}/events sends exactly these, and the union below is
# what the endpoint publishes as its response schema — so a generated client
# gets a discriminated union it can `switch` on rather than a `string`.
#
# Every variant carries `event`, repeating the frame's `event:` line inside the
# JSON. See services.messaging.models.TurnPayload for why it is not called
# `type`.


class ConversationSnapshotEvent(ConversationEventSnapshot):
    """Where the conversation stands. First frame, and again on every reconnect."""

    # Response-only, and the tag is what a client narrows on, so it must be
    # `required` in the schema rather than optional-with-a-default. See
    # HealthSnapshotResponse for the general form of this.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    event: Literal["conversation.snapshot"] = "conversation.snapshot"


class ItemCreatedEvent(ConversationItemResponse):
    """One new item, in the same shape `list_conversation_items` returns it."""

    # Response-only, and the tag is what a client narrows on, so it must be
    # `required` in the schema rather than optional-with-a-default. See
    # HealthSnapshotResponse for the general form of this.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    event: Literal["item.created"] = "item.created"


class ItemDeliveryUpdatedEvent(BaseModel):
    """An outbound item reached the provider, or failed to."""

    # Response-only, and the tag is what a client narrows on, so it must be
    # `required` in the schema rather than optional-with-a-default. See
    # HealthSnapshotResponse for the general form of this.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    event: Literal["item.delivery_updated"] = "item.delivery_updated"
    item_ids: list[UUID]
    delivery_status: DeliveryStatus
    provider_message_id: str | None = None
    error: str | None = None


class TurnFailedEvent(BaseModel):
    """The API could not queue the turn. Raised before any worker owned it, so
    it is not a `turn` frame — there was no turn to have a lifecycle."""

    # Response-only, and the tag is what a client narrows on, so it must be
    # `required` in the schema rather than optional-with-a-default. See
    # HealthSnapshotResponse for the general form of this.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    event: Literal["turn.failed"] = "turn.failed"
    trigger_item_id: UUID
    message: str


class AssistantStartedEvent(BaseModel):
    """The reply has begun; `assistant.delta` frames follow."""

    # Response-only, and the tag is what a client narrows on, so it must be
    # `required` in the schema rather than optional-with-a-default. See
    # HealthSnapshotResponse for the general form of this.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    event: Literal["assistant.started"] = "assistant.started"
    trigger_item_id: UUID


class AssistantDeltaEvent(BaseModel):
    """One chunk of the reply. Append; these are not cumulative."""

    # Response-only, and the tag is what a client narrows on, so it must be
    # `required` in the schema rather than optional-with-a-default. See
    # HealthSnapshotResponse for the general form of this.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    event: Literal["assistant.delta"] = "assistant.delta"
    trigger_item_id: UUID
    text: str


class AssistantCompletedEvent(BaseModel):
    """The reply is whole. `text` is every delta joined, so a client that missed
    some can replace what it accumulated rather than reconcile it."""

    # Response-only, and the tag is what a client narrows on, so it must be
    # `required` in the schema rather than optional-with-a-default. See
    # HealthSnapshotResponse for the general form of this.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    event: Literal["assistant.completed"] = "assistant.completed"
    trigger_item_id: UUID
    session_id: str | None = None
    text: str | None = None
    item_ids: list[UUID] = Field(default_factory=list)


ConversationEvent = Annotated[
    ConversationSnapshotEvent
    | ItemCreatedEvent
    | ItemDeliveryUpdatedEvent
    | TurnEvent
    | TurnFailedEvent
    | AssistantStartedEvent
    | AssistantDeltaEvent
    | AssistantCompletedEvent,
    Field(discriminator="event"),
]
