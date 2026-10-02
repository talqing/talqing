"""WhatsApp template campaigns: the API shapes and the two stored rows.

A batch is one approved template sent once to every row of a CSV. There is no
drafting: Meta approved the wording and only the variables change per row, so a
row is ready the moment its cells can fill every variable.
"""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from services.integrations.providers.whatsapp.bsp import (
    Template,
    TemplateButton,
    TemplateHeader,
    render_template,
)
from services.scheduling import DailyCap, StoredSendCap, TimeWindow
from services.system_vars import validate_iana_timezone

# Rows per create or append request. A batch itself has no ceiling.
MAX_RECIPIENTS_PER_REQUEST = 10_000

WhatsAppBatchStatus = Literal[
    "draft", "scheduled", "sending", "paused", "completed", "canceled", "failed"
]
WhatsAppRecipientStatus = Literal[
    "ready", "skipped", "sending", "queued", "sent", "delivered", "read", "undelivered", "failed"
]
# What a recipient list can be filtered by: a stored status, or `replied`.
WhatsAppRecipientFilter = Literal[WhatsAppRecipientStatus, "replied"]
WhatsAppSkipReason = Literal["operator", "unfillable", "duplicate_recipient"]
NextSendReason = Literal["start", "window", "daily_cap", "retry", "gap"]
# The batch statuses a send loop may still act on.
LIVE_STATUSES = ("scheduled", "sending", "paused")


class WhatsAppTemplateHeader(BaseModel):
    type: Literal["text", "image", "video", "document"]
    text: str | None
    # The media file, for every type but `text`.
    url: str | None


class WhatsAppTemplateButton(BaseModel):
    type: Literal["url", "phone_number", "quick_reply", "copy_code", "voice_call"]
    text: str
    url: str | None
    phone_number: str | None
    code: str | None


class WhatsAppTemplate(BaseModel):
    """The whole message: header, body, footer and buttons."""

    id: str
    name: str
    language: str
    category: str
    header: WhatsAppTemplateHeader | None
    body: str
    footer: str | None
    buttons: list[WhatsAppTemplateButton]
    # Every placeholder in the header, body and buttons, in order: `1`, `2`… or
    # Twilio's named ones.
    variables: list[str]
    # Why a batch cannot send this template; null when it can.
    unsupported_reason: str | None


class WhatsAppTemplatesResponse(BaseModel):
    integration_id: UUID
    templates: list[WhatsAppTemplate]


class TwilioSendersRequest(BaseModel):
    account_sid: str = Field(min_length=1)
    # Plaintext, or an existing `{{secrets.NAME}}` reference. Never stored.
    auth_token: str = Field(min_length=1)

    model_config = ConfigDict(extra="forbid")


class TwilioSender(BaseModel):
    e164: str
    sid: str
    status: str


class TwilioSendersResponse(BaseModel):
    senders: list[TwilioSender]


class WhatsAppRecipientInput(BaseModel):
    """One CSV row, keyed by header, values kept as strings."""

    input: dict[str, str] = Field(default_factory=dict)

    model_config = ConfigDict(extra="forbid")


class _SendTerms(BaseModel):
    """How a batch sends, on create, send and edit alike. Omitted keeps the current value."""

    timezone: str | None = None
    window: TimeWindow | None = None
    send_daily_cap: DailyCap | None = Field(
        default=None,
        description="Messages per local day: a fixed limit, or a ramp that rises on days "
        "it sent. Null is uncapped.",
    )
    send_gap_seconds: int | None = Field(default=None, ge=1, le=3600)
    delivery_attempts: int | None = Field(
        default=None,
        ge=1,
        le=5,
        description="Sends per row, the first included, while Meta's per-person limit holds it back.",
    )
    delivery_retry_after_hours: int | None = Field(default=None, ge=24, le=168)

    model_config = ConfigDict(extra="forbid")

    @field_validator("timezone")
    @classmethod
    def _known_zone(cls, value: str | None) -> str | None:
        return None if value is None else validate_iana_timezone(value)


class CreateWhatsAppBatchRequest(_SendTerms):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    integration_id: UUID = Field(description="A connected WhatsApp sender.")
    template_id: str = Field(min_length=1, description="An approved template on that sender.")
    to_column: str = Field(min_length=1, description="The CSV column holding phone numbers.")
    variable_map: dict[str, str] = Field(
        description="Template variable -> CSV column, for every variable the template has."
    )
    recipients: list[WhatsAppRecipientInput] = Field(
        min_length=1, max_length=MAX_RECIPIENTS_PER_REQUEST
    )


class SendWhatsAppBatchRequest(_SendTerms):
    """When and how fast. Every field has the batch's current value as its default."""

    start_at: datetime | None = None


class PatchWhatsAppBatchRequest(_SendTerms):
    """Edits. ``window``, ``start_at`` and ``send_daily_cap`` take ``null`` to clear."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    variable_map: dict[str, str] | None = None
    start_at: datetime | None = None


class AddWhatsAppRecipientsRequest(BaseModel):
    recipients: list[WhatsAppRecipientInput] = Field(
        min_length=1, max_length=MAX_RECIPIENTS_PER_REQUEST
    )

    model_config = ConfigDict(extra="forbid")


class SkippedWhatsAppRecipient(BaseModel):
    """A row of the request that will not be sent. `row_number` is its 1-based
    position in the request; `recipient_id` is null when it was not stored."""

    row_number: int
    reason: str
    recipient_id: UUID | None


class AddWhatsAppRecipientsResponse(BaseModel):
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    added: int
    skipped: list[SkippedWhatsAppRecipient]
    total_recipients: int


class PatchWhatsAppRecipientRequest(BaseModel):
    """Cell edits merged over the CSV row. An empty value clears that edit."""

    overrides: dict[str, str]

    model_config = ConfigDict(extra="forbid")


class WhatsAppBatchCounts(BaseModel):
    total: int
    ready: int
    skipped: int
    sending: int
    queued: int
    sent: int
    delivered: int
    read: int
    undelivered: int
    failed: int
    replied: int


class WhatsAppBatchResponse(BaseModel):
    # Response-only: every field below is always sent, so a default must not
    # publish it as optional. See HealthSnapshotResponse for the full reason.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    id: UUID
    name: str
    integration_id: UUID | None
    integration_name: str | None
    sender_e164: str | None
    # The agent that answers replies on this sender right now, if any.
    agent_id: UUID | None
    agent_name: str | None
    # The template as it was when the batch was created.
    template: WhatsAppTemplate
    to_column: str
    variable_map: dict[str, str]
    input_columns: list[str]
    status: WhatsAppBatchStatus
    failure_reason: str | None
    start_at: datetime | None
    timezone: str
    window: TimeWindow | None
    send_daily_cap: DailyCap | None
    # The limit in force today: the fixed one, or where the ramp has got to.
    send_daily_cap_today: int | None
    send_gap_seconds: int
    delivery_attempts: int
    delivery_retry_after_hours: int
    counts: WhatsAppBatchCounts
    # How many went out in the batch's current local day, against `send_daily_cap_today`.
    sent_today: int
    next_send_at: datetime | None
    next_send_reason: NextSendReason | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class WhatsAppRecipientResponse(BaseModel):
    # Response-only: every field below is always sent, so a default must not
    # publish it as optional. See HealthSnapshotResponse for the full reason.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    id: UUID
    row_number: int
    # Null while the row's phone cell is not a number it can be sent to.
    to_e164: str | None
    input: dict[str, str]
    overrides: dict[str, str]
    # The template's body as this row sends it.
    rendered: str
    status: WhatsAppRecipientStatus
    skip_reason: WhatsAppSkipReason | None
    last_error: str | None
    error_code: str | None
    send_attempts: int
    # Times the template was sent, against the batch's `delivery_attempts`.
    delivery_attempts: int
    # When a `ready` row that Meta held back is sent again.
    next_attempt_at: datetime | None
    sent_at: datetime | None
    delivered_at: datetime | None
    read_at: datetime | None
    replied_at: datetime | None
    conversation_id: UUID | None
    updated_at: datetime


# ── stored rows ─────────────────────────────────────────────────────────────

BATCH_COLUMNS = """
id, tenant_id, name, integration_id, template_id, template_name, template_language,
template_category, template_header, template_body, template_footer, template_buttons,
template_variables, to_column, variable_map, input_columns, status, failure_reason,
consecutive_send_failures, resumed_at, start_at, timezone,
window_start_local, window_end_local, window_days, send_daily_cap, send_ramp_start,
send_ramp_end, send_ramp_step, send_ramp_interval_days, send_ramp_base_days, send_days,
last_send_day, send_gap_seconds,
send_attempts, send_retry_after_minutes, delivery_attempts, delivery_retry_after_hours,
next_send_at, next_send_reason, total_recipients, created_by_user_id, created_at, updated_at, started_at, finished_at
"""

RECIPIENT_COLUMNS = """
id, tenant_id, batch_id, row_number, input, overrides, to_e164, status, skip_reason,
provider_message_id, error_code, last_error, send_attempts, delivery_attempts, next_attempt_at,
sending_started_at, sent_at, delivered_at, read_at, conversation_id, replied_at,
created_at, updated_at
"""


class WhatsAppBatch(StoredSendCap):
    """The policy row, exactly as stored. Re-read on every turn of the send loop."""

    id: UUID
    tenant_id: UUID
    name: str
    integration_id: UUID | None
    template_id: str
    template_name: str
    template_language: str
    template_category: str
    template_header: WhatsAppTemplateHeader | None
    template_body: str
    template_footer: str | None
    template_buttons: list[WhatsAppTemplateButton]
    template_variables: list[str]
    to_column: str
    variable_map: dict[str, str]
    input_columns: list[str]
    status: WhatsAppBatchStatus
    failure_reason: str | None
    consecutive_send_failures: int
    resumed_at: datetime | None
    start_at: datetime | None
    timezone: str
    window_start_local: time | None
    window_end_local: time | None
    window_days: list[int]
    # Local days, in `timezone`, on which this batch sent at least one message,
    # and the latest of them. What a ramp advances on.
    send_days: int
    last_send_day: date | None
    send_gap_seconds: int
    send_attempts: int
    send_retry_after_minutes: int
    delivery_attempts: int
    delivery_retry_after_hours: int
    next_send_at: datetime | None
    next_send_reason: NextSendReason | None
    total_recipients: int
    created_by_user_id: UUID
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    finished_at: datetime | None

    @property
    def window(self) -> TimeWindow | None:
        if self.window_start_local is None or self.window_end_local is None:
            return None
        return TimeWindow(
            start=self.window_start_local, end=self.window_end_local, days=self.window_days
        )

    @property
    def template(self) -> Template:
        return Template(
            id=self.template_id,
            name=self.template_name,
            language=self.template_language,
            category=self.template_category,
            header=(
                TemplateHeader(**self.template_header.model_dump())
                if self.template_header
                else None
            ),
            body=self.template_body,
            footer=self.template_footer,
            buttons=tuple(TemplateButton(**b.model_dump()) for b in self.template_buttons),
            variables=tuple(self.template_variables),
        )


class WhatsAppRecipient(BaseModel):
    """One row, exactly as stored."""

    id: UUID
    tenant_id: UUID
    batch_id: UUID
    row_number: int
    input: dict[str, str]
    overrides: dict[str, str]
    # Set by `service.reconcile` from the phone cell; null while that is not a
    # number this row can be sent to.
    to_e164: str | None
    status: WhatsAppRecipientStatus
    skip_reason: WhatsAppSkipReason | None
    provider_message_id: str | None
    error_code: str | None
    last_error: str | None
    send_attempts: int
    delivery_attempts: int
    next_attempt_at: datetime | None
    sending_started_at: datetime | None
    sent_at: datetime | None
    delivered_at: datetime | None
    read_at: datetime | None
    conversation_id: UUID | None
    replied_at: datetime | None
    created_at: datetime
    updated_at: datetime

    def columns(self) -> dict[str, str]:
        """The CSV row with a person's edits on top."""
        return {**self.input, **self.overrides}


def variable_values(batch: WhatsAppBatch, recipient: WhatsAppRecipient) -> dict[str, str]:
    """Template variable -> this row's value, blanks included."""
    columns = recipient.columns()
    return {name: columns.get(column, "") for name, column in batch.variable_map.items()}


def rendered(batch: WhatsAppBatch, recipient: WhatsAppRecipient) -> str:
    return render_template(batch.template_body, variable_values(batch, recipient))


def message_text(batch: WhatsAppBatch, recipient: WhatsAppRecipient) -> str:
    """The whole message as text: what the agent "said", for when the person replies.

    A part the template does not have is left out, so a text template is its body.
    """
    values = variable_values(batch, recipient)
    parts: list[str] = []
    if header := batch.template_header:
        parts.append(
            render_template(header.text or "", values)
            if header.type == "text"
            else f"[{header.type.capitalize()}: {render_template(header.url or '', values)}]"
        )
    parts.append(render_template(batch.template_body, values))
    if batch.template_footer:
        parts.append(batch.template_footer)
    if batch.template_buttons:
        lines = []
        for button in batch.template_buttons:
            target = render_template(button.url or button.phone_number or button.code or "", values)
            lines.append(f"[Button: {button.text}{f' → {target}' if target else ''}]")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def userdata_seed(recipient: WhatsAppRecipient) -> dict[str, str]:
    """What the agent knows about this person when they reply: every non-blank cell.

    `{{userdata.<column>}}`, the same rule as a call batch. The reply itself adds
    `userdata.whatsapp`.
    """
    return {k: v for k, v in recipient.columns().items() if v.strip()}
