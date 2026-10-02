"""Request and response shapes for batch outbound calling.

The row models at the bottom (``CallBatch``, ``CallBatchRecipient``) are what the
service and the dispatcher read; everything above them is the API surface.

Note what is *not* validated here: the recipient list. Row-level problems — an
unparseable number, a duplicate, a reserved ``userdata`` key — are collected with
their row numbers in ``service.py`` and returned as one 400 listing every bad
row. A pydantic validator would raise on the first one, and telling an operator
about row 12 when rows 12, 47 and 88 are wrong means three round trips through a
10 000-row upload.
"""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from services.agents.plan import AgentPlanRequest, StoredAgentPlan
from services.scheduling import DailyCap, FixedDailyCap, RampDailyCap, TimeWindow, cap_from_columns
from services.system_vars import validate_iana_timezone

# Both competitors cap a list at 10 000, and nothing in front of the API limits
# the body size. Above this a batch is really several batches, and the browser
# has to render every parsed row before submit.
MAX_RECIPIENTS_PER_REQUEST = 10_000

BatchStatus = Literal["scheduled", "running", "paused", "completed", "canceled", "failed"]
# The status a reader sees, which is not always the one stored: cancelling a
# batch writes one row, and its untouched recipients are reported `canceled` on
# read rather than rewritten (see `service.effective_status`).
RecipientStatus = Literal["pending", "dialing", "completed", "failed", "canceled"]
# Why a batch is waiting rather than dialling: its start time, a shut calling
# window, or today's limit spent.
NextDialReason = Literal["start", "window", "daily_cap"]


class BatchRecipientInput(BaseModel):
    """One person to call, and what the agent should know about them."""

    to: str
    # Read by `{{userdata.field}}` in the prompt and greeting. Values are
    # strings: substitution is textual, and a CSV cell reading "007" is a
    # reference number, not the integer 7.
    userdata: dict[str, str] = Field(default_factory=dict)


class CreateCallBatchRequest(AgentPlanRequest):
    """A campaign: who to call, what runs, and when it may dial.

    `vars` comes from `AgentPlanRequest` and is BATCH-level — one bag copied
    onto every call this campaign places. Per-person data is what a recipient's
    `userdata` is for.
    """

    name: str = Field(min_length=1, max_length=200)
    from_phone_number_id: UUID
    recipients: list[BatchRecipientInput] = Field(
        min_length=1, max_length=MAX_RECIPIENTS_PER_REQUEST
    )
    # Absolute instant. Null starts the batch on the dispatcher's next pass.
    start_at: datetime | None = None
    # IANA zone. The batch's clock: it interprets `calling_window` and renders
    # every time on the batch UI, so a list is dialled on its recipients' hours
    # rather than on whoever happens to be reading the page.
    timezone: str
    calling_window: TimeWindow | None = None
    # 1..10, and the only concurrency limit in the system. Defaults to one call
    # at a time — the safe reading of an unstated intention for something that
    # spends money.
    max_concurrency: int = Field(default=1, ge=1, le=10)
    max_attempts: int = Field(default=1, ge=1, le=5)
    retry_after_minutes: int = Field(default=30, ge=5, le=1440)
    dial_gap_seconds: int = Field(
        default=0,
        ge=0,
        le=3600,
        description="Minimum seconds between STARTING two calls; 0 dials as slots free. "
        "A call may start up to 30 seconds after its gap ends.",
    )
    dial_daily_cap: DailyCap | None = Field(
        default=None,
        description="Calls placed per local day, retries included: a fixed limit, or a ramp "
        "that rises on days it dialled. Null is uncapped.",
    )

    @field_validator("timezone")
    @classmethod
    def _known_zone(cls, value: str) -> str:
        return validate_iana_timezone(value)


class PatchCallBatchRequest(BaseModel):
    """Policy edits, allowed while a batch is `scheduled`, `running`, `paused` or `completed`.

    ``calling_window``, ``start_at`` and ``dial_daily_cap`` accept an explicit
    ``null`` to *clear* them — "dial at any hour", "start on the next pass", "no
    daily limit" — which is why the
    service reads ``model_fields_set`` rather than treating absent and null the
    same way. Every other field keeps the usual PATCH rule: absent means
    unchanged.
    """

    name: str | None = Field(default=None, min_length=1, max_length=200)
    agent_id: UUID | None = None
    from_phone_number_id: UUID | None = None
    start_at: datetime | None = None
    timezone: str | None = None
    calling_window: TimeWindow | None = None
    max_concurrency: int | None = Field(default=None, ge=1, le=10)
    max_attempts: int | None = Field(default=None, ge=1, le=5)
    retry_after_minutes: int | None = Field(default=None, ge=5, le=1440)
    dial_gap_seconds: int | None = Field(default=None, ge=0, le=3600)
    dial_daily_cap: DailyCap | None = None

    @field_validator("timezone")
    @classmethod
    def _known_zone(cls, value: str | None) -> str | None:
        return None if value is None else validate_iana_timezone(value)


class AddRecipientsRequest(BaseModel):
    recipients: list[BatchRecipientInput] = Field(
        min_length=1, max_length=MAX_RECIPIENTS_PER_REQUEST
    )


class SkippedRecipient(BaseModel):
    # The row's 1-based position in the request that sent it.
    row_number: int
    to: str
    reason: str


class AddRecipientsResponse(BaseModel):
    """What appending to a batch did.

    Numbers already in the batch, or repeated in the request, are skipped and
    reported rather than rejected: re-uploading a list the operator re-exported
    is the normal case, and the never-call-twice guarantee doing its job is not a
    user error.
    """

    added: int
    skipped: list[SkippedRecipient]
    total_recipients: int


class CallBatchCounts(BaseModel):
    total: int
    pending: int
    dialing: int
    completed: int
    failed: int
    canceled: int
    # Not a status, so not part of the sum above: a retry may still be pending.
    voicemail: int = Field(
        description="Recipients whose latest call reached voicemail; also counted in their status."
    )


class CallBatchResponse(BaseModel):
    id: UUID
    name: str
    agent_id: UUID | None
    # Resolved by join on every read, never stored: a renamed agent shows its
    # current name, and both go null once the agent or number is deleted —
    # which is also what the dispatcher sees when it fails the batch.
    #
    # An inline entry agent has no row to join, so the name comes out of the
    # plan instead. No `agent_name` column beside `agent_plan`: the plan is the
    # only copy, and a denormalized cache on an editable policy row is exactly
    # what that table's comment warns against.
    agent_name: str | None
    # What this batch was asked to run, when it differs from "the entry agent's
    # published version". Null on an ordinary batch.
    agent_plan: StoredAgentPlan | None = None
    # The `{{vars.*}}` values every call this batch places starts with.
    vars: dict[str, str] | None = None
    from_phone_number_id: UUID | None
    from_e164: str | None
    status: BatchStatus
    failure_reason: str | None
    start_at: datetime | None
    timezone: str
    calling_window: TimeWindow | None
    max_concurrency: int
    max_attempts: int
    retry_after_minutes: int
    dial_gap_seconds: int
    dial_daily_cap: DailyCap | None
    # The limit in force today: the fixed one, or where the ramp has got to.
    dial_daily_cap_today: int | None
    # Calls this batch placed in its current local day, retries included.
    dialed_today: int
    counts: CallBatchCounts
    # Why it is not dialling right now: the clock, not a bug. Null while dialling.
    next_dial_at: datetime | None
    # Which gate is holding it. Null whenever `next_dial_at` is.
    next_dial_reason: NextDialReason | None
    created_at: datetime
    started_at: datetime | None
    # "The dispatcher is done with this batch", not "the user stopped it". A
    # cancelled batch with a call still ringing has a terminal status and no
    # `ended_at` until that call lands.
    ended_at: datetime | None


class CallBatchRecipientResponse(BaseModel):
    id: UUID
    row_number: int
    to: str
    userdata: dict[str, str]
    status: RecipientStatus
    attempts: int
    last_close_reason: str | None
    # The same plain sentence the calls list and the observability page show for
    # an ending, served from `services/close_reasons.py` rather than mapped in
    # each client — that map is the one place that knows what a reason means, and
    # a copy in a dashboard goes stale the first time a worker learns a new
    # string. Null only when nothing has been attempted yet.
    last_close_reason_label: str | None
    # The most recent attempt's call — null until first claimed. The UI links it
    # to /calls?id=…, which is how an operator gets from a row in their
    # spreadsheet to that call's transcript, recording and cost.
    session_id: UUID | None
    updated_at: datetime


# ── stored rows ─────────────────────────────────────────────────────────────

BATCH_COLUMNS = """
id, tenant_id, name, agent_id, agent_plan, vars, from_phone_number_id, status,
failure_reason, consecutive_setup_failures, start_at, timezone,
window_start_local, window_end_local, window_days, max_concurrency,
max_attempts, retry_after_minutes, dial_gap_seconds, dial_daily_cap,
dial_ramp_start, dial_ramp_end, dial_ramp_step, dial_ramp_interval_days,
dial_ramp_base_days, dial_days, last_dial_day, last_dial_at, total_recipients,
created_by_user_id, created_at, updated_at, started_at, ended_at
"""


class CallBatch(BaseModel):
    """The policy row, exactly as stored.

    Re-read on every dispatcher pass and cached nowhere between them: that is
    what makes "pause it", "point it at a different agent" and "cancel it" plain
    UPDATEs with nothing to coordinate.
    """

    id: UUID
    tenant_id: UUID
    name: str
    agent_id: UUID | None
    # The cast every call this batch places runs, validated once at create. NULL
    # means "the entry agent's current published version", re-read every pass so
    # a republished prompt reaches the calls that follow.
    agent_plan: dict[str, Any] | None
    # The `{{vars.*}}` values copied onto every call this batch places, decided
    # once at create. NULL means the agents' own declared defaults stand.
    vars: dict[str, str] | None
    from_phone_number_id: UUID | None
    status: BatchStatus
    failure_reason: str | None
    consecutive_setup_failures: int
    start_at: datetime | None
    timezone: str
    window_start_local: time | None
    window_end_local: time | None
    window_days: list[int]
    max_concurrency: int
    max_attempts: int
    retry_after_minutes: int
    dial_gap_seconds: int
    dial_daily_cap: int | None
    dial_ramp_start: int | None
    dial_ramp_end: int | None
    dial_ramp_step: int | None
    dial_ramp_interval_days: int | None
    dial_ramp_base_days: int
    # Local days, in `timezone`, on which this batch placed at least one call,
    # and the latest of them. What a ramp advances on.
    dial_days: int
    last_dial_day: date | None
    # When the last call was claimed, which `dial_gap_seconds` is measured from.
    last_dial_at: datetime | None
    total_recipients: int
    created_by_user_id: UUID
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    ended_at: datetime | None

    @property
    def daily_cap(self) -> FixedDailyCap | RampDailyCap | None:
        return cap_from_columns(
            self.dial_daily_cap,
            self.dial_ramp_start,
            self.dial_ramp_end,
            self.dial_ramp_step,
            self.dial_ramp_interval_days,
        )

    @property
    def calling_window(self) -> TimeWindow | None:
        if self.window_start_local is None or self.window_end_local is None:
            return None
        return TimeWindow(
            start=self.window_start_local,
            end=self.window_end_local,
            days=self.window_days,
        )


class ClaimedRecipient(BaseModel):
    """One recipient the dispatcher has taken out of `pending` to dial."""

    id: UUID
    row_number: int
    to_e164: str
    userdata: dict[str, str]
    attempts: int
