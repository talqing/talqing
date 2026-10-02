"""Request and response shapes for email outbound, and the three stored rows.

The row models at the bottom (``EmailBatch``, ``EmailSendRun``,
``EmailRecipient``) are what the service, the drafting pass and the send pass
read; everything above them is the API surface.

**Which row a setting lives on is the whole design.** The batch carries the
list's identity, its drafting policy and the DEFAULTS a send inherits. A send run
carries the policy for one send — its schedule, its hours, its pacing, its sender
— copied by value when it is created, so editing the batch steers the next send
rather than the one already going out. A recipient carries the work.

Note what is *not* validated here: the recipient list and the field map.
Row-level and cross-referencing problems — a header that collides with an output
field, a mapped column that names neither — are collected with their row numbers
in ``service.py`` and returned as one 400 listing every bad row. A pydantic
validator raises on the first one, and telling an operator about row 12 when
rows 12, 47 and 88 are wrong means three round trips through a 5 000-row upload.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

from services.scheduling import DailyCap, FixedDailyCap, StoredSendCap, TimeWindow
from services.system_vars import validate_iana_timezone

# Rows per create or append request — batch calling's figure: the browser renders
# every parsed row before submit. A batch itself has no ceiling; appends grow it
# for as long as it runs.
MAX_RECIPIENTS_PER_REQUEST = 10_000

# The three fields a batch maps out of its merged column space. Everything else
# a task produces is kept on the row as context and read by nobody.
MAPPED_FIELDS = ("to", "subject", "body")

EmailBatchStatus = Literal["scheduled", "drafting", "paused", "drafted", "canceled", "failed"]
EmailSendStatus = Literal["scheduled", "sending", "paused", "sent", "canceled", "failed"]
# The statuses a send still covers its rows in. A paused send is one of them: it
# has stopped, not ended, and its rows are still spoken for.
LIVE_SEND_STATUSES = ("scheduled", "sending", "paused")
# `all` is a standing send — every row of the batch that reaches `draft`, drafted
# yet or not. `selected` is the fixed set of rows it was created with.
EmailSendScope = Literal["all", "selected"]
# What a row can be STORED as. `queued` is not here: it is a fact about the
# batch — a live send covers this draft — and is derived on read, like
# `canceled` (see `service.effective_status`).
EmailRecipientStatus = Literal[
    "pending",
    "drafting",
    "draft",
    "draft_failed",
    "skipped",
    "sending",
    "sent",
    "send_failed",
]
# `operator` — a person skipped it. `duplicate_recipient` — another row of this
# batch already has the address. `unfillable` — a mapped column the task never
# writes is blank, so no run could make it sendable and none was paid for.
SkipReason = Literal["operator", "duplicate_recipient", "unfillable"]
BodyFormat = Literal["text", "html"]

# Why a batch or a send is waiting rather than working. Rendered beside the time
# itself, because "Next: 00:00" and "Next: 00:00 — daily cap reached" are
# different amounts of help, and the dashboard used to guess the second one by
# comparing two other numbers.
NextDraftReason = Literal["start", "retry"]
NextSendReason = Literal["start", "window", "daily_cap", "retry", "gap", "drafting"]

# Deliberately permissive, and it is worth saying why: this decides whether a row
# is queueable, and an over-strict pattern turns a real address into a row an
# operator cannot send and cannot explain. Anything past "one @, a dot in the
# domain, no spaces" is the provider's judgement to make, not ours.
_EMAIL_RE = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$")


# A display name and a subject both end up in a MAIL HEADER, and a header ends
# at a newline. `formataddr` quotes and escapes, but a quoted string with a raw
# CR or LF in it is still two headers — so control characters are refused at the
# edge rather than escaped somewhere downstream and hoped about.
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def is_email_address(value: str) -> bool:
    return bool(_EMAIL_RE.match(value.strip()))


def _no_control_chars(value: str | None) -> str | None:
    if value is not None and _CONTROL_RE.search(value):
        raise ValueError("cannot contain line breaks or other control characters")
    return value


def email_domain(value: str) -> str:
    return value.strip().rsplit("@", 1)[-1].lower()


class FieldMap(BaseModel):
    """Which column in the merged space carries each of the three sent fields.

    A value may name a CSV header **or** an output field of the task — that is
    the whole point of the merged space, and it is what makes "the CSV has
    emails" and "feed it a phone number and let the task find the email" the
    same feature.
    """

    to: str = Field(min_length=1, description="The column holding the destination address.")
    subject: str = Field(min_length=1, description="The column holding the subject line.")
    body: str = Field(min_length=1, description="The column holding the message body.")

    model_config = ConfigDict(extra="forbid")

    def columns(self) -> dict[str, str]:
        return {"to": self.to, "subject": self.subject, "body": self.body}


class EmailRecipientInput(BaseModel):
    """One row of the uploaded list.

    ``input`` is one CSV row, keyed by header, values kept as strings —
    inferring types turns a "007" prefix into 7. It is handed to the task
    verbatim as its ``{{vars.*}}`` values.
    """

    input: dict[str, str] = Field(default_factory=dict)

    model_config = ConfigDict(extra="forbid")


class CreateEmailBatchRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    task_id: UUID = Field(description="The agent task that drafts each row.")
    integration_id: UUID = Field(description="A connected Resend account.")
    from_email: str = Field(
        description=(
            "The default sending address. Its domain must be verified on that Resend "
            "account; a send may override this for its own rows alone."
        )
    )
    from_name: str | None = Field(default=None, max_length=200)
    reply_to: str | None = None
    body_format: BodyFormat = "text"
    field_map: FieldMap
    recipients: list[EmailRecipientInput] = Field(
        min_length=1, max_length=MAX_RECIPIENTS_PER_REQUEST
    )
    # Absolute instant. Null starts drafting as soon as the job is picked up.
    # There is no drafting WINDOW to go with it: nobody receives a draft.
    start_at: datetime | None = None
    # IANA zone. The batch's clock: it interprets `window` and renders every time
    # on the batch UI, so a list is worked on its own hours rather than on
    # whoever happens to be reading the page.
    timezone: str
    window: TimeWindow | None = Field(
        default=None,
        description="When email may LEAVE. Inherited by every send this batch creates.",
    )
    draft_concurrency: int = Field(default=5, ge=1, le=10)
    draft_attempts: int = Field(default=1, ge=1, le=5)
    draft_retry_after_minutes: int = Field(default=30, ge=5, le=1440)
    draft_gap_seconds: int = Field(
        default=0,
        ge=0,
        le=3600,
        description="Minimum seconds between STARTING two rows. 0 starts each as a slot frees; "
        "raise it when the task calls something that rate-limits.",
    )
    send_attempts: int = Field(default=3, ge=1, le=5)
    send_retry_after_minutes: int = Field(default=30, ge=5, le=1440)
    send_gap_seconds: int = Field(
        default=1,
        ge=1,
        le=3600,
        description="Minimum seconds between STARTING two emails. 60 means one a minute.",
    )
    send_daily_cap: DailyCap | None = Field(
        default=FixedDailyCap(kind="fixed", limit=200),
        description="Emails this batch may send per local day, across all of its sends: a fixed "
        "limit, or a ramp that rises on days it sent. Null is uncapped.",
    )

    model_config = ConfigDict(extra="forbid")

    @field_validator("timezone")
    @classmethod
    def _known_zone(cls, value: str) -> str:
        return validate_iana_timezone(value)

    @field_validator("from_name")
    @classmethod
    def _header_safe(cls, value: str | None) -> str | None:
        return _no_control_chars(value)


class PatchEmailBatchRequest(BaseModel):
    """Policy edits.

    ``window``, ``start_at`` and ``send_daily_cap`` accept an explicit ``null``
    to *clear* them — "send at any hour", "start on the next pass", "no daily
    ceiling" — which is why the service reads ``model_fields_set`` rather than
    treating absent and null the same way. Every other field keeps the usual
    PATCH rule: absent means unchanged.

    Pacing stays editable at any time; identity does not once anything has been
    sent (see ``service.patch_batch``). The sending group here is the DEFAULT for
    the next send — a run already created carries its own copy.
    """

    name: str | None = Field(default=None, min_length=1, max_length=200)
    from_email: str | None = None
    from_name: str | None = Field(default=None, max_length=200)
    reply_to: str | None = None
    body_format: BodyFormat | None = None
    field_map: FieldMap | None = None
    start_at: datetime | None = None
    timezone: str | None = None
    window: TimeWindow | None = None
    draft_concurrency: int | None = Field(default=None, ge=1, le=10)
    draft_attempts: int | None = Field(default=None, ge=1, le=5)
    draft_retry_after_minutes: int | None = Field(default=None, ge=5, le=1440)
    draft_gap_seconds: int | None = Field(default=None, ge=0, le=3600)
    send_attempts: int | None = Field(default=None, ge=1, le=5)
    send_retry_after_minutes: int | None = Field(default=None, ge=5, le=1440)
    send_gap_seconds: int | None = Field(default=None, ge=1, le=3600)
    send_daily_cap: DailyCap | None = None

    model_config = ConfigDict(extra="forbid")

    @field_validator("timezone")
    @classmethod
    def _known_zone(cls, value: str | None) -> str | None:
        return None if value is None else validate_iana_timezone(value)

    @field_validator("from_name")
    @classmethod
    def _header_safe(cls, value: str | None) -> str | None:
        return _no_control_chars(value)


class PatchEmailRecipientRequest(BaseModel):
    """One human edit, merged over what the CSV and the model produced.

    Keys are column names in the merged space; an empty string clears an
    override and falls back to whatever was underneath it. Stored apart from
    ``output`` so "what the model wrote" and "what we actually sent" are both
    answerable afterwards.
    """

    overrides: dict[str, str]

    model_config = ConfigDict(extra="forbid")


class SelectRecipientsRequest(BaseModel):
    """What `skip` and `restore` accept — either shape.

    Both are reversible, which is why both take a bulk shorthand: skipping four
    thousand rows is undone by restoring them, and making curation tedious pushes
    an operator toward not curating.
    """

    recipient_ids: list[UUID] | None = None
    selection: Literal["all_eligible"] | None = Field(
        default=None,
        description="Every row this verb can act on: drafts for skip, rows a person skipped "
        "for restore.",
    )

    model_config = ConfigDict(extra="forbid")


class RedraftRequest(BaseModel):
    """Draft these rows again, from scratch.

    One verb for "write this row again", whatever put it in the state it is in —
    a failed attempt, a prompt that has since improved, a draft nobody liked.
    Four selections, and they differ in what they REFUSE as much as in what they
    move, because a redraft discards a draft the tenant has already paid for:

    - ``all`` — every row in the batch. Rows already sent or mid-send come back in
      ``rejected``, by name, so a bulk press cannot quietly do less than it said.
    - ``failed`` — rows whose drafting failed.
    - ``not_sent`` — every row that has not left, with nothing rejected.
    - ``stale_version`` — rows drafted by a task version older than the one
      published now. The selection that makes "watch the output, improve the
      task, publish, redraft the rest" a workflow rather than a wish.
    """

    selection: Literal["all", "failed", "not_sent", "stale_version"] | None = None
    recipient_ids: list[UUID] | None = None
    clear_overrides: bool = Field(
        default=False,
        description="Also discard the cells a person edited by hand. Off by default.",
    )

    model_config = ConfigDict(extra="forbid")


class CreateEmailSendRequest(BaseModel):
    """Send these rows. **This is the call that mails real people.**

    ``scope: "selected"`` sends exactly ``recipient_ids``. ``scope: "all"`` is a
    standing send: every row of the batch as it reaches `draft`, including rows
    not drafted yet — so it mails drafts nobody has read. Everything else
    defaults to the batch's own setting; ``window`` and ``send_daily_cap`` accept
    an explicit ``null`` to mean "no window" and "uncapped" rather than
    "inherit", so the service reads ``model_fields_set``.

    The sender applies to THIS send alone and never writes back to the batch.
    """

    scope: EmailSendScope = Field(
        description="`selected` sends `recipient_ids`; `all` sends every row as it is drafted."
    )
    recipient_ids: list[UUID] | None = Field(default=None, max_length=MAX_RECIPIENTS_PER_REQUEST)
    from_email: str | None = None
    from_name: str | None = Field(default=None, max_length=200)
    reply_to: str | None = None
    start_at: datetime | None = None
    timezone: str | None = None
    window: TimeWindow | None = None
    send_attempts: int | None = Field(default=None, ge=1, le=5)
    send_retry_after_minutes: int | None = Field(default=None, ge=5, le=1440)
    send_gap_seconds: int | None = Field(default=None, ge=1, le=3600)
    send_daily_cap: DailyCap | None = None

    model_config = ConfigDict(extra="forbid")

    @field_validator("timezone")
    @classmethod
    def _known_zone(cls, value: str | None) -> str | None:
        return None if value is None else validate_iana_timezone(value)

    @field_validator("from_name")
    @classmethod
    def _header_safe(cls, value: str | None) -> str | None:
        return _no_control_chars(value)

    @model_validator(mode="after")
    def _ids_match_scope(self) -> CreateEmailSendRequest:
        if self.scope == "selected" and not self.recipient_ids:
            raise ValueError("a `selected` send names its rows in `recipient_ids`")
        if self.scope == "all" and self.recipient_ids is not None:
            raise ValueError("an `all` send covers every row, so it takes no `recipient_ids`")
        return self


class PatchEmailSendRequest(BaseModel):
    """Re-steer one send: its pacing, its hours, its ceiling, its start time.

    Never its sender and never its scope — "re-steer the pacing, never the
    contents". Widening a send is another send; narrowing one has no defensible
    answer to "which rows does it keep?". To change what a send covers, cancel it
    — its unsent rows simply stay drafts — and create another.
    """

    start_at: datetime | None = None
    timezone: str | None = None
    window: TimeWindow | None = None
    send_attempts: int | None = Field(default=None, ge=1, le=5)
    send_retry_after_minutes: int | None = Field(default=None, ge=5, le=1440)
    send_gap_seconds: int | None = Field(default=None, ge=1, le=3600)
    send_daily_cap: DailyCap | None = None

    model_config = ConfigDict(extra="forbid")

    @field_validator("timezone")
    @classmethod
    def _known_zone(cls, value: str | None) -> str | None:
        return None if value is None else validate_iana_timezone(value)


class AddEmailRecipientsRequest(BaseModel):
    recipients: list[EmailRecipientInput] = Field(
        min_length=1, max_length=MAX_RECIPIENTS_PER_REQUEST
    )

    model_config = ConfigDict(extra="forbid")


class SkippedEmailRecipient(BaseModel):
    """A row of the request that will not be drafted. `row_number` is its 1-based
    position in the request; `recipient_id` is null when it was not stored."""

    row_number: int
    reason: str
    recipient_id: UUID | None


class AddEmailRecipientsResponse(BaseModel):
    """`standing_send_id` is the send of every row that will mail the new rows
    without review, or null when they wait for review."""

    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    added: int
    skipped: list[SkippedEmailRecipient]
    total_recipients: int
    standing_send_id: UUID | None


class RejectedRecipient(BaseModel):
    recipient_id: UUID
    row_number: int
    reason: str


class RecipientActionResponse(BaseModel):
    """Skip-and-report, never a partial silent accept."""

    # Response-only: every field below is always sent, so a default must not
    # publish it as optional. See HealthSnapshotResponse for the full reason.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    affected: int
    rejected: list[RejectedRecipient] = Field(default_factory=list)


class VerifiedSendersResponse(BaseModel):
    """The domains one connected account may send from, read live.

    Domains rather than addresses, because that is what an email provider
    actually verifies: any local part on a verified domain sends, and there is
    no list of addresses to enumerate. So this is what a sender field validates
    against and what it shows beside itself — the alternative is a 400 the
    operator only sees after typing.
    """

    integration_id: UUID
    domains: list[str]


class EmailBatchCounts(BaseModel):
    """Rows by the status a reader sees. `draft` is drafts no live send covers
    yet; `queued` is drafts one does."""

    total: int
    pending: int
    drafting: int
    draft: int
    draft_failed: int
    skipped: int
    queued: int
    sending: int
    sent: int
    send_failed: int
    canceled: int


class EmailBatchSkips(BaseModel):
    """`counts.skipped`, by who decided it."""

    model_config = ConfigDict(extra="forbid")

    operator: int
    duplicate_recipient: int
    unfillable: int


class DraftFailure(BaseModel):
    """One reason drafts failed, and how many rows it holds."""

    reason: str
    count: int


class DraftedVersion(BaseModel):
    """How many of this batch's rows were written by one version of its task.

    The batch page's whole answer to "did my edit change anything": a list that
    reads "1 204 rows on v2, 96 on v3" names both the improvement and exactly
    which rows are still on the old prompt.
    """

    version: int
    count: int


class EmailBatchResponse(BaseModel):
    # Response-only: every field below is always sent, so a default must not
    # publish it as optional. See HealthSnapshotResponse for the full reason.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)
    id: UUID
    name: str
    task_id: UUID | None
    # Resolved by join on every read, never stored: a renamed task shows its
    # current name, and null once the task is deleted — which is also what a
    # drafting pass sees when it fails the batch.
    task_name: str | None
    # The version the next pass will draft with. Null once the task is gone.
    published_task_version: int | None
    integration_id: UUID | None
    integration_name: str | None
    from_email: str
    from_name: str | None
    reply_to: str | None
    body_format: BodyFormat
    field_map: FieldMap
    # Every column the review table renders, in the operator's own order: the
    # CSV's headers first, then whatever the task declares it produces.
    input_columns: list[str]
    output_columns: list[str]
    status: EmailBatchStatus
    failure_reason: str | None
    start_at: datetime | None
    timezone: str
    window: TimeWindow | None
    draft_concurrency: int
    draft_attempts: int
    draft_retry_after_minutes: int
    draft_gap_seconds: int
    send_attempts: int
    send_retry_after_minutes: int
    send_gap_seconds: int
    send_daily_cap: DailyCap | None
    # The limit in force today: the fixed one, or where the ramp has got to.
    send_daily_cap_today: int | None
    counts: EmailBatchCounts
    # Why rows were skipped. `unfillable` rows can never be sent — a mapped column
    # the task does not write is blank — so drafting and sending both measure
    # progress without them, and none of them was paid for.
    skips: EmailBatchSkips
    # `draft_failed` rows by `last_error`, most common first, at most five: the
    # difference between "the task found no address" and "the provider was down"
    # is whether a redraft can help.
    draft_failures: list[DraftFailure]
    # How many of this batch's emails have gone out in the current local day, and
    # therefore how much of `send_daily_cap_today` is left. The cap is per BATCH, so
    # this is the number every one of its sends is measured against.
    sent_today: int
    # Which task version wrote what, newest first. Empty until something drafts.
    drafted_versions: list[DraftedVersion]
    # How many rows an older version wrote that a redraft would actually rewrite.
    # `drafted_versions` counts every row with a run, sent ones included, so a
    # banner built on it never goes away and its button can affect nothing.
    stale_redraftable: int
    # ...and how many of them have already gone, which is why they cannot be.
    stale_sent: int
    # Why it is not drafting right now: the clock, not a bug. Null while drafting.
    next_draft_at: datetime | None
    # `start` — a start time that has not arrived; `retry` — every remaining row
    # is waiting out its backoff. Null whenever `next_draft_at` is.
    next_draft_reason: NextDraftReason | None
    # What the drafting has cost so far, summed over every row's task run. Task
    # spend is not in the Observability charts — those are built on sessions —
    # so this is the only place it is reported.
    provider_cost: Decimal | None = None
    created_at: datetime
    started_at: datetime | None
    # When the last row finished drafting. Says nothing about sending, which is
    # a different job entirely.
    drafted_at: datetime | None

    @field_serializer("provider_cost")
    def _ser_cost(self, v: Decimal | None) -> float | None:
        return None if v is None else float(v)


class EmailSendCounts(BaseModel):
    """Where this send's rows are.

    ``total`` is the rows a `selected` send was created with, and null for an
    `all` send, which cannot know it while drafting runs. ``queued`` and
    ``drafting`` are what it still covers — drafts ready to go, and rows still
    being written — so both are 0 once it has ended.
    """

    total: int | None
    queued: int
    drafting: int
    sending: int
    sent: int
    send_failed: int
    skipped: int


class EmailSendResponse(BaseModel):
    # Response-only: every field below is always sent, so a default must not
    # publish it as optional. See HealthSnapshotResponse for the full reason.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    id: UUID
    batch_id: UUID
    scope: EmailSendScope
    status: EmailSendStatus
    failure_reason: str | None
    from_email: str
    from_name: str | None
    reply_to: str | None
    start_at: datetime | None
    timezone: str
    window: TimeWindow | None
    send_attempts: int
    send_retry_after_minutes: int
    send_gap_seconds: int
    send_daily_cap: DailyCap | None
    # The limit in force today, against the batch's `sent_today`.
    send_daily_cap_today: int | None
    counts: EmailSendCounts
    # When this send next does something, read off the run's own column. Null
    # while an email is actually going out, and null once it is paused or over.
    next_send_at: datetime | None
    # Which gate is holding it: its start time, a shut window, a spent daily cap,
    # rows waiting out a retry, the gap between two emails, or — with no instant
    # to name, so `next_send_at` is null — rows it covers still being drafted.
    # Otherwise null whenever `next_send_at` is.
    next_send_reason: NextSendReason | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class CreateEmailSendResponse(BaseModel):
    """The send that was created, and every row that did not go into it.

    Two halves rather than one, because they answer different questions and a
    caller needs both: what is now scheduled to go out, and which rows it
    refused and why. If NOTHING in the selection could be sent there is no send
    to describe, so that case is a 400 naming the reasons instead.
    """

    # Response-only: every field below is always sent, so a default must not
    # publish it as optional. See HealthSnapshotResponse for the full reason.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    send: EmailSendResponse
    rejected: list[RejectedRecipient] = Field(default_factory=list)


class EmailRecipientResponse(BaseModel):
    """One row of the review spreadsheet.

    ``columns`` is the merged space — `input`, then `output`, then `overrides` —
    which is what the three mapped fields resolve against and what the table
    renders. The three halves are returned beside it so an edited cell can be
    marked and "what the model wrote" stays readable under the edit.
    """

    # Response-only: every field below is always sent, so a default must not
    # publish it as optional. See HealthSnapshotResponse for the full reason.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    id: UUID
    row_number: int
    input: dict[str, str]
    output: dict[str, Any] | None
    overrides: dict[str, str]
    columns: dict[str, Any]
    # The status a reader sees, which is not always the one stored: a draft a
    # live send covers reads `queued`, and a cancelled batch's untouched rows
    # read `canceled` (see `service.effective_status`).
    status: Literal[EmailRecipientStatus, "queued", "canceled"]
    # Null when this row is sendable; otherwise the reason a send would refuse
    # it, which is what the review table shows in place of a checkbox.
    not_ready_reason: str | None = None
    skip_reason: SkipReason | None = None
    # The run that drafted this row, so the table can link to its trace…
    task_run_id: UUID | None = None
    # …and which published version of the task that run used, so a page can say
    # "these 1 204 were written by v2" and offer to redraft exactly them.
    task_version: int | None = None
    to_email: str | None = None
    sent_from: str | None = None
    sent_at: datetime | None = None
    # The send that took this row to go out — set once a send claims it, so
    # null on a row still waiting.
    send_run_id: UUID | None = None
    attempts: int
    send_attempts: int
    last_error: str | None = None
    updated_at: datetime


# ── stored rows ─────────────────────────────────────────────────────────────

BATCH_COLUMNS = """
id, tenant_id, name, task_id, integration_id, from_email, from_name, reply_to,
body_format, field_map, input_columns, status, failure_reason,
consecutive_draft_failures, start_at, timezone, window_start_local,
window_end_local, window_days, draft_concurrency, draft_attempts,
draft_retry_after_minutes, draft_gap_seconds, send_attempts,
send_retry_after_minutes, send_gap_seconds, send_daily_cap, send_ramp_start,
send_ramp_end, send_ramp_step, send_ramp_interval_days, send_ramp_base_days,
send_days, last_send_day, total_recipients, next_draft_at, next_draft_reason,
created_by_user_id, created_at, updated_at, started_at, drafted_at
"""

SEND_RUN_COLUMNS = """
id, batch_id, tenant_id, scope, from_email, from_name, reply_to, status,
failure_reason, consecutive_send_failures, start_at, timezone,
window_start_local, window_end_local, window_days, send_attempts,
send_retry_after_minutes, send_gap_seconds, send_daily_cap, send_ramp_start,
send_ramp_end, send_ramp_step, send_ramp_interval_days, send_ramp_base_days,
total_recipients, next_send_at, next_send_reason,
created_by_user_id, created_at, updated_at, started_at, finished_at
"""

RECIPIENT_COLUMNS = """
id, batch_id, tenant_id, row_number, input, output, overrides, status,
send_run_id, skip_reason, task_run_id, to_email, sent_from,
provider_message_id, sent_at, attempts, send_attempts, next_attempt_at,
last_error, created_at, updated_at
"""


# The same list, aliased, for the one read that joins: `list_recipients` reaches
# into `task_runs` for the version that drafted each row.
RECIPIENT_COLUMNS_R = ", ".join(
    f"r.{column.strip()}" for column in RECIPIENT_COLUMNS.split(",") if column.strip()
)


class _Windowed(BaseModel):
    """The three stored window columns, and the one shape they render as.

    ``email_batches`` and ``email_send_runs`` both store the window this way and
    both hand it to ``window_state``; a second copy of this property is a second
    place for "both or neither" to drift.
    """

    window_start_local: time | None
    window_end_local: time | None
    window_days: list[int]

    @property
    def window(self) -> TimeWindow | None:
        if self.window_start_local is None or self.window_end_local is None:
            return None
        return TimeWindow(
            start=self.window_start_local,
            end=self.window_end_local,
            days=self.window_days,
        )


class EmailBatch(_Windowed, StoredSendCap):
    """The policy row, exactly as stored.

    Re-read on every drafting pass and cached nowhere between them — which is
    what makes "pause it", "slow it down" and "cancel it" plain UPDATEs with
    nothing to coordinate. Consecutive passes may genuinely run in different
    containers, so this is enforced by the world as well as by the rule.
    """

    id: UUID
    tenant_id: UUID
    name: str
    task_id: UUID | None
    integration_id: UUID | None
    from_email: str
    from_name: str | None
    reply_to: str | None
    body_format: BodyFormat
    field_map: FieldMap
    input_columns: list[str]
    status: EmailBatchStatus
    failure_reason: str | None
    consecutive_draft_failures: int
    start_at: datetime | None
    timezone: str
    draft_concurrency: int
    draft_attempts: int
    draft_retry_after_minutes: int
    draft_gap_seconds: int
    send_attempts: int
    send_retry_after_minutes: int
    send_gap_seconds: int
    # Local days, in `timezone`, on which this batch sent at least one email, and
    # the latest of them. What a ramp advances on; see `services.scheduling.ramp`.
    send_days: int
    last_send_day: date | None
    total_recipients: int
    # When drafting next does something, and why. The loop writes these whenever
    # it waits and clears them on the way out; every API verb that changes when
    # drafting next acts writes them in the same transaction as the change. This
    # is the API's ONLY answer to "when": `scheduled_jobs` is machinery, and a
    # waiting loop holds its job `running` the whole time, so that table's
    # `scheduled_at` stopped meaning anything to a reader.
    next_draft_at: datetime | None
    next_draft_reason: str | None
    created_by_user_id: UUID
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    drafted_at: datetime | None


class EmailSendRun(_Windowed, StoredSendCap):
    """One send, exactly as stored. Re-read on every send pass, cached nowhere.

    Its sender and its pacing are its own copies, taken from the batch when it
    was created: an operator who fixes a batch setting mid-send is steering the
    NEXT send, and the one already going out keeps the terms it was created with.
    """

    id: UUID
    batch_id: UUID
    tenant_id: UUID
    scope: EmailSendScope
    from_email: str
    from_name: str | None
    reply_to: str | None
    status: EmailSendStatus
    failure_reason: str | None
    consecutive_send_failures: int
    start_at: datetime | None
    timezone: str
    send_attempts: int
    send_retry_after_minutes: int
    send_gap_seconds: int
    # How many rows a `selected` send was created with. Null for `all`.
    total_recipients: int | None
    # When this send next does something, and why — see `EmailBatch` above for
    # the rule. Sending has six reasons to wait where drafting has two.
    next_send_at: datetime | None
    next_send_reason: str | None
    created_by_user_id: UUID
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class EmailRecipient(BaseModel):
    """One work row, exactly as stored."""

    id: UUID
    batch_id: UUID
    tenant_id: UUID
    row_number: int
    input: dict[str, str]
    output: dict[str, Any] | None
    overrides: dict[str, str]
    status: EmailRecipientStatus
    send_run_id: UUID | None
    skip_reason: SkipReason | None
    task_run_id: UUID | None
    to_email: str | None
    sent_from: str | None
    provider_message_id: str | None
    sent_at: datetime | None
    attempts: int
    send_attempts: int
    next_attempt_at: datetime | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime

    def columns(self) -> dict[str, Any]:
        """The merged column space: uploaded, then generated, then edited.

        Order is the whole of the rule. ``overrides`` is merged LAST and is
        never written by a drafting pass or a send job, so a re-drafted row
        cannot silently discard a human's edit and an edit cannot be overwritten
        by a later draft.
        """
        return {**self.input, **(self.output or {}), **self.overrides}


def task_vars(batch: EmailBatch, recipient: EmailRecipient) -> dict[str, str]:
    """What the task actually receives for this row.

    The uploaded cells, with a person's edits to **those same columns** on top.
    An operator who corrects a typo'd domain in the review table and presses
    redraft is asking for a draft about the corrected company, and the table has
    been showing them the corrected value all along — passing `input` alone ran
    the task on the typo and billed them for a draft about the wrong business,
    with nothing on screen to say so.

    Overrides of the task's OWN output fields are deliberately not variables:
    those are edits to what it produced, and feeding a rewritten subject line
    back in as an input would be a different feature.

    Passed whole rather than filtered to what the task declares, so a task that
    later declares a new variable picks up the column that was always there —
    substitution reads only what the prompt names, so an unused column costs
    nothing.
    """
    return {
        **recipient.input,
        **{k: v for k, v in recipient.overrides.items() if k in batch.input_columns},
    }


def _field_problem(field: str, column: str, value: Any) -> str | None:
    """Why one mapped value could not go out, or ``None``."""
    text = value.strip() if isinstance(value, str) else ""
    if not text:
        return f"{field} is empty ({column})"
    if field == "to" and not is_email_address(text):
        return f"{text!r} is not an email address"
    if field == "subject" and _CONTROL_RE.search(text):
        # The subject is a header. A model that wrote a newline into one has
        # written two headers, so the row is refused at the review gate — where a
        # person can see it and fix the cell — rather than silently rewritten.
        return "the subject contains a line break"
    return None


def resolve_send_fields(
    batch: EmailBatch, recipient: EmailRecipient
) -> tuple[dict[str, str] | None, str | None]:
    """``({to, subject, body}, None)``, or ``(None, why not)``.

    The one implementation of "is this row sendable", and it is deliberately
    read in two places: the API refuses to send a row it rejects — the operator
    is standing right there and can fix it — and the send pass re-resolves
    anyway, because between the press and the send somebody may have edited the
    cell or the mapping.

    Resolution runs over the merged column space, so a value may have come from
    the CSV, from the task, or from a human's edit, and nothing downstream can
    tell which. That is the point.
    """
    columns = recipient.columns()
    resolved: dict[str, str] = {}
    for field, column in batch.field_map.columns().items():
        value = columns.get(column)
        if problem := _field_problem(field, column, value):
            return None, problem
        resolved[field] = value.strip()
    return resolved, None


def unfillable_reason(
    batch: EmailBatch, recipient: EmailRecipient, output_fields: set[str]
) -> str | None:
    """Why no run of the task could ever make this row sendable, or ``None``.

    Only the mapped columns the task does NOT write are judged, over what was
    uploaded plus what a person typed — never over ``output``. A column the task
    does write stays the task's to fill, which is what keeps "feed it a phone
    number and let the task find the email" working: that row is still work.
    """
    cells = {**recipient.input, **recipient.overrides}
    for field, column in batch.field_map.columns().items():
        if column in output_fields:
            continue
        if problem := _field_problem(field, column, cells.get(column)):
            return problem
    return None
