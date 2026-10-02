"""Email outbound HTTP adapter over services.email."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Query

from api.core.schemas import OkResponse, Page
from api.dataplane.deps import Context, CtxDep, WriteCtxDep
from services import email as svc
from services.email import (
    AddEmailRecipientsRequest,
    AddEmailRecipientsResponse,
    CreateEmailBatchRequest,
    CreateEmailSendRequest,
    CreateEmailSendResponse,
    EmailBatchResponse,
    EmailRecipientResponse,
    EmailSendResponse,
    PatchEmailBatchRequest,
    PatchEmailRecipientRequest,
    PatchEmailSendRequest,
    RecipientActionResponse,
    RedraftRequest,
    SelectRecipientsRequest,
    VerifiedSendersResponse,
)

router = APIRouter(prefix="/email", tags=["email"])


@router.get("/senders", response_model=VerifiedSendersResponse)
async def list_email_senders(
    integration_id: UUID = Query(..., description="A connected Resend account."),
    ctx: Context = CtxDep,
) -> VerifiedSendersResponse:
    """The domains a connected email account may send from, right now.

    Domains rather than addresses, because that is what Resend verifies: any
    local part on a verified domain sends. Read live off the account, and this
    is what a `from_email` is checked against — both the batch's default and a
    per-send override.
    """
    return await svc.verified_senders(ctx, integration_id)


@router.post("/batches", status_code=201, response_model=EmailBatchResponse)
async def create_email_batch(
    body: CreateEmailBatchRequest, ctx: Context = WriteCtxDep
) -> EmailBatchResponse:
    """Create an email batch: a list, an agent task that drafts each row, and a
    connected Resend account to send from.

    **Read your provider's terms first.** Resend's Acceptable Use Policy
    prohibits unsolicited mail of any kind — cold outreach, purchased lists,
    scraped contacts — and this runs on your account under your agreement.

    **Nothing is sent that a person has not selected.** Creating a batch only
    starts drafting; sending is a separate call that creates a send you can
    schedule, pace, pause and cancel.

    **Every row runs the task's CURRENT published version**, re-read every pass,
    so publishing a better prompt mid-batch changes every row drafted after it.

    `field_map` points `to`, `subject` and `body` at a column of your list or at
    a field the task produces. A row where a mapped column the task does not
    write is blank is skipped at once (`unfillable`) and never drafted. `window`
    is when email may LEAVE, inherited by every send; drafting is not restricted
    by hours. An invalid batch is a 400 listing every problem.

    There is no spend cap: every row runs the task and is billed, so a 5 000-row
    batch doing research costs 5 000 task runs whether or not anything is sent.
    """
    return await svc.create_batch(body, ctx)


@router.get("/batches", response_model=Page[EmailBatchResponse])
async def list_email_batches(
    ctx: Context = CtxDep,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    status: str | None = Query(None),
) -> Page[EmailBatchResponse]:
    """List email batches, newest first.

    `status` is about **drafting** and nothing else: `scheduled`, `drafting`,
    `paused`, `drafted`, `canceled`, `failed`. A `drafted` batch is one whose
    drafts are ready to review — it says nothing about how many have been sent,
    which is what `counts` is for.
    """
    return await svc.list_batches(ctx, limit=limit, offset=offset, status=status)


@router.get("/batches/{batch_id}", response_model=EmailBatchResponse)
async def get_email_batch(batch_id: UUID, ctx: Context = CtxDep) -> EmailBatchResponse:
    """One batch: its policy, its per-status counts and what drafting has cost.

    `drafted_versions` says which published version of the task wrote how many
    rows, and `sent_today` how much of `send_daily_cap_today` is left.
    """
    return await svc.get_batch(ctx, batch_id)


@router.patch("/batches/{batch_id}", response_model=EmailBatchResponse)
async def patch_email_batch(
    batch_id: UUID, body: PatchEmailBatchRequest, ctx: Context = WriteCtxDep
) -> EmailBatchResponse:
    """Edit a batch's policy. The next drafting pass reads the new row.

    Pacing, the window and the daily cap are editable at any time; the sending
    half is the DEFAULT for the next send, and a send already created carries its
    own copy. The sender, the body format and the field map freeze once the batch
    has sent anything — changing them then would make the rows already delivered
    no longer describe what they were sent as. `start_at` moves only while the
    batch is still scheduled.
    """
    return await svc.patch_batch(batch_id, body, ctx)


@router.post("/batches/{batch_id}/pause", response_model=EmailBatchResponse)
async def pause_email_batch(batch_id: UUID, ctx: Context = WriteCtxDep) -> EmailBatchResponse:
    """Stop drafting new rows. Runs already in flight finish.

    A paused batch has nothing scheduled at all until you resume it. Drafts
    already produced stay reviewable and sendable.
    """
    return await svc.pause_batch(batch_id, ctx)


@router.post("/batches/{batch_id}/resume", response_model=EmailBatchResponse)
async def resume_email_batch(batch_id: UUID, ctx: Context = WriteCtxDep) -> EmailBatchResponse:
    """Undo a pause and start drafting again from where it stopped."""
    return await svc.resume_batch(batch_id, ctx)


@router.post("/batches/{batch_id}/cancel", response_model=EmailBatchResponse)
async def cancel_email_batch(batch_id: UUID, ctx: Context = WriteCtxDep) -> EmailBatchResponse:
    """Stop drafting for good.

    Rows already drafted are **not** touched and neither is any send: cancelling
    stops drafting, and drafts already produced are still worth reviewing and
    sending. Rows never reached report as `canceled`.
    """
    return await svc.cancel_batch(batch_id, ctx)


@router.delete("/batches/{batch_id}", response_model=OkResponse)
async def delete_email_batch(batch_id: UUID, ctx: Context = WriteCtxDep) -> OkResponse:
    """Delete a batch, its rows and its sends for good. **The record of what was sent goes too.**

    Refused while it is still drafting or a send has not finished; cancel those first.
    """
    await svc.delete_batch(batch_id, ctx)
    return OkResponse()


@router.get("/batches/{batch_id}/recipients", response_model=Page[EmailRecipientResponse])
async def list_email_batch_recipients(
    batch_id: UUID,
    ctx: Context = CtxDep,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    status: str | None = Query(None),
) -> Page[EmailRecipientResponse]:
    """One batch's rows, in the order they were uploaded.

    Each row carries `columns` — the merged space of what you uploaded, what the
    task produced and what a person edited, in that precedence — plus the three
    halves separately, so an edited cell can be told from the model's own value.
    `not_ready_reason` is why this row cannot be sent yet, and is null when it
    can be.
    """
    return await svc.list_recipients(ctx, batch_id, limit=limit, offset=offset, status=status)


@router.post("/batches/{batch_id}/recipients", response_model=AddEmailRecipientsResponse)
async def add_email_batch_recipients(
    batch_id: UUID, body: AddEmailRecipientsRequest, ctx: Context = WriteCtxDep
) -> AddEmailRecipientsResponse:
    """Append rows to a batch; a drafted batch resumes. Duplicates are skipped and reported.

    If the latest send is a finished send of every row, it reopens and mails the new drafts.
    """
    return await svc.add_recipients(batch_id, body, ctx)


@router.patch(
    "/batches/{batch_id}/recipients/{recipient_id}", response_model=EmailRecipientResponse
)
async def patch_email_batch_recipient(
    batch_id: UUID,
    recipient_id: UUID,
    body: PatchEmailRecipientRequest,
    ctx: Context = WriteCtxDep,
) -> EmailRecipientResponse:
    """Edit cells on one row before it is sent.

    `overrides` is merged over the uploaded and generated values at send time and
    stored apart from both, so what the model wrote stays readable underneath.
    Sending an empty value clears that edit rather than sending an empty field —
    the way to send nothing is to skip the row. A `queued` row is still editable
    and goes out as edited; one a send has already taken cannot be.
    """
    return await svc.patch_recipient(batch_id, recipient_id, body, ctx)


@router.post("/batches/{batch_id}/sends", status_code=201, response_model=CreateEmailSendResponse)
async def create_email_send(
    batch_id: UUID, body: CreateEmailSendRequest, ctx: Context = WriteCtxDep
) -> CreateEmailSendResponse:
    """Send rows. **This mails real people and cannot be undone.**

    `scope: "selected"` sends `recipient_ids`; a row that cannot be sent comes
    back in `rejected` and stays a draft. `scope: "all"` sends every row as it
    finishes drafting — including drafts nobody has read — and waits while
    drafting runs. It cannot be live beside another send of the batch (409).

    `start_at` schedules it, `window` holds it to business hours, and
    `send_gap_seconds` and `send_daily_cap` (a fixed limit or a ramp) pace the
    whole batch. Everything
    omitted comes from the batch. Cancelling it leaves the unsent rows as drafts.
    """
    return await svc.create_send(batch_id, body, ctx)


@router.get("/batches/{batch_id}/sends", response_model=Page[EmailSendResponse])
async def list_email_sends(
    batch_id: UUID,
    ctx: Context = CtxDep,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> Page[EmailSendResponse]:
    """Every send on this batch, newest first, with its counts and its schedule."""
    return await svc.list_sends(ctx, batch_id, limit=limit, offset=offset)


@router.get("/batches/{batch_id}/sends/{send_id}", response_model=EmailSendResponse)
async def get_email_send(batch_id: UUID, send_id: UUID, ctx: Context = CtxDep) -> EmailSendResponse:
    """One send: where its rows are, and when it next does something.

    `next_send_reason` says which gate is holding it — `start`, `window`,
    `daily_cap`, `retry`, `gap`, or `drafting` (no `next_send_at`).
    """
    return await svc.get_send(ctx, batch_id, send_id)


@router.patch("/batches/{batch_id}/sends/{send_id}", response_model=EmailSendResponse)
async def patch_email_send(
    batch_id: UUID, send_id: UUID, body: PatchEmailSendRequest, ctx: Context = WriteCtxDep
) -> EmailSendResponse:
    """Re-steer one send: its pace, its hours, its daily cap, its start time.

    Never its sender or its scope — to change what it covers, cancel it and
    create another. `start_at` moves only before the send has begun.
    """
    return await svc.patch_send(batch_id, send_id, body, ctx)


@router.post("/batches/{batch_id}/sends/{send_id}/pause", response_model=EmailSendResponse)
async def pause_email_send(
    batch_id: UUID, send_id: UUID, ctx: Context = WriteCtxDep
) -> EmailSendResponse:
    """Stop sending. It takes hold on the next email, and nothing is lost —
    the rows stay queued, and editable, until you resume."""
    return await svc.pause_send(batch_id, send_id, ctx)


@router.post("/batches/{batch_id}/sends/{send_id}/resume", response_model=EmailSendResponse)
async def resume_email_send(
    batch_id: UUID, send_id: UUID, ctx: Context = WriteCtxDep
) -> EmailSendResponse:
    """Undo a pause and carry on from where it stopped."""
    return await svc.resume_send(batch_id, send_id, ctx)


@router.post("/batches/{batch_id}/sends/{send_id}/cancel", response_model=EmailSendResponse)
async def cancel_email_send(
    batch_id: UUID, send_id: UUID, ctx: Context = WriteCtxDep
) -> EmailSendResponse:
    """Stop this send for good. Every row it has not sent stays a draft,
    exactly as it was, ready for another send — not a destructive action."""
    return await svc.cancel_send(batch_id, send_id, ctx)


@router.post("/batches/{batch_id}/skip", response_model=RecipientActionResponse)
async def skip_email_batch_recipients(
    batch_id: UUID, body: SelectRecipientsRequest, ctx: Context = WriteCtxDep
) -> RecipientActionResponse:
    """Take rows out of consideration. Reversible with `restore`.

    Accepts drafted rows and rows whose drafting failed. Takes either
    `recipient_ids` or `selection: "all_eligible"`, which here means both.
    """
    return await svc.act_on_recipients(batch_id, "skip", body, ctx)


@router.post("/batches/{batch_id}/restore", response_model=RecipientActionResponse)
async def restore_email_batch_recipients(
    batch_id: UUID, body: SelectRecipientsRequest, ctx: Context = WriteCtxDep
) -> RecipientActionResponse:
    """Put skipped rows back. `selection: "all_eligible"` means every row a person
    skipped; an `unfillable` row is restored by filling in its cell instead."""
    return await svc.act_on_recipients(batch_id, "restore", body, ctx)


@router.post("/batches/{batch_id}/redraft", response_model=RecipientActionResponse)
async def redraft_email_batch_recipients(
    batch_id: UUID, body: RedraftRequest, ctx: Context = WriteCtxDep
) -> RecipientActionResponse:
    """Draft rows again from scratch. **Each one runs the task and is billed again.**

    `selection` is `failed` (drafting failed), `stale_version` (drafted by an
    older published version of the task), `not_sent`, or `all` — which names
    every row it refuses. Rows already sent are never redrafted; a `queued` one
    is, and its send takes the new draft.

    This revives a batch that had finished drafting, and a person's edited cells
    survive unless you pass `clear_overrides` — and are what the task is given.
    Refused on a canceled batch, whose rows can never draft again.
    """
    return await svc.redraft_recipients(batch_id, body, ctx)
