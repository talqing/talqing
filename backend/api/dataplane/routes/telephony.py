"""Telephony HTTP adapters: carrier accounts, phone numbers, outbound calls."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Query

from api.core.schemas import OkResponse, Page
from api.dataplane.deps import Context, CtxDep, WriteCtxDep
from services import telephony as svc
from services.telephony import batch as batch_svc
from services.telephony.batch.models import (
    AddRecipientsRequest,
    AddRecipientsResponse,
    CallBatchRecipientResponse,
    CallBatchResponse,
    CreateCallBatchRequest,
    PatchCallBatchRequest,
)
from services.telephony.models import (
    AssignPhoneNumberRequest,
    CreateTelephonyAccountRequest,
    ImportPhoneNumbersRequest,
    ImportPhoneNumbersResponse,
    OutboundCallRequest,
    OutboundCallResponse,
    PatchPhoneNumberRequest,
    PatchTelephonyAccountRequest,
    PhoneNumberResponse,
    RemoteNumber,
    TelephonyAccountResponse,
    TelephonyProviderSpec,
)

router = APIRouter(prefix="/telephony", tags=["telephony"])


@router.get("/providers", response_model=Page[TelephonyProviderSpec])
async def list_telephony_providers(
    _ctx: Context = CtxDep,
    limit: int = Query(200, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> Page[TelephonyProviderSpec]:
    """List the telephony carriers an account can be connected to.

    `account_info_keys`, `credential_keys` and `setup_fields` state exactly what
    connecting each carrier needs — read this before `create_telephony_account`
    rather than guessing field names. Entries with `implemented: false` have no
    adapter yet and cannot be used.
    """
    return await svc.catalog(limit, offset)


@router.get("/accounts", response_model=Page[TelephonyAccountResponse])
async def list_telephony_accounts(
    ctx: Context = CtxDep,
    limit: int = Query(200, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> Page[TelephonyAccountResponse]:
    """List the workspace's connected carrier accounts.

    A carrier account is the credential Talqing uses to reach a phone network;
    phone numbers hang off one. `setup_complete` and `setup_steps` say whether
    it is finished — including the steps that can only be done in the carrier's
    own console.
    """
    return await svc.list_accounts(ctx, limit, offset)


@router.post("/accounts", status_code=201, response_model=TelephonyAccountResponse)
async def create_telephony_account(
    body: CreateTelephonyAccountRequest, ctx: Context = WriteCtxDep
) -> TelephonyAccountResponse:
    """Connect a carrier account so the workspace can own phone numbers.

    `account_info` and `credentials` must carry exactly the keys that carrier's
    `list_telephony_providers` entry declares. A credential may be plaintext —
    stored as a workspace secret — or an existing reference.

    Creating only records the credential; call `provision_telephony_account`
    next to build the SIP trunk.
    """
    return await svc.create_account(body, ctx)


@router.get("/accounts/{account_id}", response_model=TelephonyAccountResponse)
async def get_telephony_account(
    account_id: UUID, ctx: Context = CtxDep
) -> TelephonyAccountResponse:
    """Read one carrier account, including what its setup still needs.

    `setup_steps` is the authoritative checklist — read it rather than trying
    to re-derive setup state from `provider_state`. A step marked
    `user_confirmable` cannot be checked over any API; it is done in the
    carrier's console and confirmed with `patch_telephony_account`.
    """
    return await svc.get_account(ctx, account_id)


@router.patch("/accounts/{account_id}", response_model=TelephonyAccountResponse)
async def patch_telephony_account(
    account_id: UUID,
    body: PatchTelephonyAccountRequest,
    ctx: Context = WriteCtxDep,
) -> TelephonyAccountResponse:
    """Update a carrier account: rename it, replace credentials, or disable it.

    Only the fields you send change. `status` accepts `disabled` to stop the
    account and `connected` to re-enable it. Set `console_setup_confirmed` when
    the user says they have finished the carrier-console steps — no API can
    verify that work, and inbound calling on this account's numbers stays
    blocked until they confirm.
    """
    return await svc.patch_account(account_id, body, ctx)


@router.delete("/accounts/{account_id}", response_model=OkResponse)
async def delete_telephony_account(account_id: UUID, ctx: Context = WriteCtxDep) -> OkResponse:
    """Disconnect a carrier account permanently.

    Every phone number on it must be disabled first, so this can never silently
    take live numbers off the air. Deleting also tears down the SIP trunk.
    """
    return await svc.delete_account(account_id, ctx)


@router.post("/accounts/{account_id}/provision", response_model=TelephonyAccountResponse)
async def provision_telephony_account(
    account_id: UUID, ctx: Context = WriteCtxDep
) -> TelephonyAccountResponse:
    """Build (or repair) the SIP trunk between Talqing and the carrier.

    Run this after connecting an account, and again after replacing its
    credentials. It is safe to repeat: it converges the carrier side to what
    the account should have rather than creating a second trunk. The response's
    `status` and `setup_steps` say what is working and what is left.
    """
    return await svc.provision_account(account_id, ctx)


@router.get("/accounts/{account_id}/remote-numbers", response_model=Page[RemoteNumber])
async def list_remote_numbers(
    account_id: UUID,
    ctx: Context = CtxDep,
    limit: int = Query(200, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> Page[RemoteNumber]:
    """List the numbers this account owns at the carrier, ready to import.

    Read live from the carrier, so it also proves the credentials work.
    `already_imported` marks the ones Talqing already has — pass the rest to
    `import_phone_numbers`. Buying a number is done at the carrier, not here.
    """
    return await svc.list_remote_numbers(account_id, ctx, limit=limit, offset=offset)


@router.get("/phone-numbers", response_model=Page[PhoneNumberResponse])
async def list_phone_numbers(
    ctx: Context = CtxDep,
    account_id: UUID | None = Query(None),
    limit: int = Query(200, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> Page[PhoneNumberResponse]:
    """List the workspace's phone numbers, optionally for one carrier account.

    `readiness` is the field to report to a user — it collapses status,
    capability and routing into the one answer they want: `live` (answering
    inbound), `needs_agent`, `needs_carrier_setup`, `setting_up`, `error` or
    `disabled`.
    """
    return await svc.list_numbers(ctx, account_id=account_id, limit=limit, offset=offset)


# 200, not 201: results are per-number, so a request can legitimately create
# nothing (every DID already imported) and 201 would be a lie.
@router.post("/phone-numbers/import", response_model=ImportPhoneNumbersResponse)
async def import_phone_numbers(
    body: ImportPhoneNumbersRequest, ctx: Context = WriteCtxDep
) -> ImportPhoneNumbersResponse:
    """Bring carrier-owned numbers into the workspace, provisioning each one.

    Per number, not all-or-nothing: `items` carries one result per requested
    E.164, so check every `ok` rather than assuming a 200 means all of them
    landed. `provision` defaults to true, which is what makes a number usable.
    """
    return await svc.import_numbers(body, ctx)


@router.get("/phone-numbers/{number_id}", response_model=PhoneNumberResponse)
async def get_phone_number(number_id: UUID, ctx: Context = CtxDep) -> PhoneNumberResponse:
    """Read one phone number: its carrier, capabilities, agent and readiness."""
    return await svc.get_number(ctx, number_id)


@router.patch("/phone-numbers/{number_id}", response_model=PhoneNumberResponse)
async def patch_phone_number(
    number_id: UUID,
    body: PatchPhoneNumberRequest,
    ctx: Context = WriteCtxDep,
) -> PhoneNumberResponse:
    """Relabel a number, change what it may do, or take it out of service.

    `can_inbound` and `can_outbound` gate the directions it will carry.
    `status` accepts `disabled`, which tears down its LiveKit routing, and
    `pending`, which re-opens a disabled number so it can be provisioned again.
    """
    return await svc.patch_number(number_id, body, ctx)


@router.post("/phone-numbers/{number_id}/provision", response_model=PhoneNumberResponse)
async def provision_phone_number(
    number_id: UUID, ctx: Context = WriteCtxDep
) -> PhoneNumberResponse:
    """Wire a number up at the carrier and in LiveKit so it can carry calls.

    `import_phone_numbers` already does this unless it was called with
    `provision: false`; use this to retry a number left in `error`, or to bring
    one back after re-enabling it.
    """
    return await svc.provision_number(number_id, ctx)


@router.post("/phone-numbers/{number_id}/assign", response_model=PhoneNumberResponse)
async def assign_phone_number(
    number_id: UUID,
    body: AssignPhoneNumberRequest,
    ctx: Context = WriteCtxDep,
) -> PhoneNumberResponse:
    """Put an agent on a number so it answers incoming calls.

    The agent must be **published** and on the `voice` or `video` channel, and
    the number active, inbound-capable and on a ready carrier account — each is
    refused with the reason. One agent per number; assigning again replaces the
    previous one. Republishing the agent later needs no reassignment.
    """
    return await svc.assign_number(number_id, body, ctx)


@router.post("/phone-numbers/{number_id}/unassign", response_model=PhoneNumberResponse)
async def unassign_phone_number(number_id: UUID, ctx: Context = WriteCtxDep) -> PhoneNumberResponse:
    """Take the agent off a number, so incoming calls are no longer answered.

    The number stays provisioned and can still place outbound calls; its
    readiness drops to `needs_agent`.
    """
    return await svc.unassign_number(number_id, ctx)


# Outbound dial lives under /v1/calls/outbound (mounted from calls router or here).
outbound_router = APIRouter(prefix="/calls", tags=["calls"])


@outbound_router.post("/outbound", status_code=201, response_model=OutboundCallResponse)
async def create_outbound_call(
    body: OutboundCallRequest, ctx: Context = WriteCtxDep
) -> OutboundCallResponse:
    """Dial a real phone number and have a published agent take the call.

    **This places a live, billable PSTN call** — only call it when the user has
    asked for this specific call to be made. The agent must be published and on
    the `voice` channel, and `from_phone_number_id` an active outbound-capable
    number on a ready carrier account.

    `userdata` seeds the session state read as `{{userdata.field}}`; keys
    beginning with `_talqing` are reserved. Prefer `agent_id`; reach for
    `agent_override`, `agent` or `agent_team` only when the definition is
    generated per request and would never be reused.

    It returns when the call is placed, not when it is answered.
    """
    return await svc.create_outbound_call(body, ctx)


# Batch outbound calling. Mounted BEFORE the calls router in `api/main.py`, or
# `GET /v1/calls/{session_id}` captures "batches" and refuses it as a UUID.
batches_router = APIRouter(prefix="/calls/batches", tags=["calls"])


@batches_router.post("", status_code=201, response_model=CallBatchResponse)
async def create_call_batch(
    body: CreateCallBatchRequest, ctx: Context = WriteCtxDep
) -> CallBatchResponse:
    """Call a list of people with a published agent, now or on a schedule.

    **This places real, billable PSTN calls** — one per recipient, up to 10 000
    of them. Only call it when the user has asked for this specific list to be
    dialled.

    `recipients[]` carries `to` (any format, normalized to E.164) and
    `userdata`, the session state behind `{{userdata.field}}` in the prompt and
    greeting. A number that appears twice is refused with both row numbers.

    `start_at` and `calling_window` schedule it in the timezone you give;
    `max_concurrency`, `max_attempts`, `dial_gap_seconds` and `dial_daily_cap`
    (a fixed limit or a ramp) pace it. A number that was reached is never
    called twice.

    Two things you would otherwise learn from an invoice:

    - **Voicemail is answered and billed** unless the agent has
      `voicemail_detection` on; then it hangs up (optionally after a message),
      and the recipient is retried like a missed call.
    - **Calls run your agent's current published version.** Republishing changes
      every call placed after that moment.
    """
    return await batch_svc.create_batch(body, ctx)


@batches_router.get("", response_model=Page[CallBatchResponse])
async def list_call_batches(
    ctx: Context = CtxDep,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    status: str | None = Query(
        default=None,
        description="scheduled, running, paused, completed, canceled or failed",
    ),
) -> Page[CallBatchResponse]:
    """List outbound calling batches, newest first, with live progress.

    `counts` breaks the recipient list down by outcome; `next_dial_at` and
    `next_dial_reason` say when a batch that is not dialling will start again and
    why — its start time, its window or today's limit, not a fault.
    A `failed` batch carries `failure_reason`: the circuit breaker stopped it
    after ten consecutive setup failures.
    """
    return await batch_svc.list_batches(ctx, limit=limit, offset=offset, status=status)


@batches_router.get("/{batch_id}", response_model=CallBatchResponse)
async def get_call_batch(batch_id: UUID, ctx: Context = CtxDep) -> CallBatchResponse:
    """One batch: its policy, its progress, and why it is or is not dialling.

    `agent_name` and `from_e164` are read live, so a renamed agent shows its
    current name and a deleted one shows null.
    """
    return await batch_svc.get_batch(ctx, batch_id)


@batches_router.patch("/{batch_id}", response_model=CallBatchResponse)
async def patch_call_batch(
    batch_id: UUID, body: PatchCallBatchRequest, ctx: Context = WriteCtxDep
) -> CallBatchResponse:
    """Change a batch's policy while it runs or after it completes. The next pass reads it.

    Everything here is policy, so nothing revisits a recipient already called,
    and calls in flight finish under the policy they were dispatched with. You
    do not need this to pick up a republished prompt — every call already runs
    the current published version. `start_at` moves only while the batch is
    still `scheduled`.
    """
    return await batch_svc.patch_batch(batch_id, body, ctx)


@batches_router.get("/{batch_id}/recipients", response_model=Page[CallBatchRecipientResponse])
async def list_call_batch_recipients(
    batch_id: UUID,
    ctx: Context = CtxDep,
    limit: int = Query(100, ge=1, le=200),
    offset: int = Query(0, ge=0),
    status: str | None = Query(
        default=None,
        description="pending, dialing, completed, failed or canceled",
    ),
) -> Page[CallBatchRecipientResponse]:
    """One batch's recipients and how each call went, in the uploaded order.

    `session_id` is that person's most recent call — pass it to `get_call` for
    the transcript, recording and cost, and `last_close_reason` says exactly how
    the attempt ended. A recipient of a cancelled batch reads as `canceled` even
    though their row was never touched.
    """
    return await batch_svc.list_recipients(ctx, batch_id, limit=limit, offset=offset, status=status)


@batches_router.post("/{batch_id}/recipients", response_model=AddRecipientsResponse)
async def add_call_batch_recipients(
    batch_id: UUID, body: AddRecipientsRequest, ctx: Context = WriteCtxDep
) -> AddRecipientsResponse:
    """Append people to a batch, including one that has finished — it resumes.

    Numbers the batch already holds or the upload repeats are **skipped and
    reported**, not rejected, so nobody is called twice. `row_number` in errors
    and skips is the position in this request.
    """
    return await batch_svc.add_recipients(batch_id, body, ctx)


@batches_router.post("/{batch_id}/pause", response_model=CallBatchResponse)
async def pause_call_batch(batch_id: UUID, ctx: Context = WriteCtxDep) -> CallBatchResponse:
    """Stop placing new calls; calls in progress run to their end.

    Pausing a completed batch holds rows added later until resume.
    """
    return await batch_svc.pause_batch(batch_id, ctx)


@batches_router.post("/{batch_id}/resume", response_model=CallBatchResponse)
async def resume_call_batch(batch_id: UUID, ctx: Context = WriteCtxDep) -> CallBatchResponse:
    """Undo a pause and carry on from where the batch stopped.

    A batch whose `start_at` is still in the future goes back to `scheduled`
    rather than starting early.
    """
    return await batch_svc.resume_batch(batch_id, ctx)


@batches_router.post("/{batch_id}/cancel", response_model=CallBatchResponse)
async def cancel_call_batch(batch_id: UUID, ctx: Context = WriteCtxDep) -> CallBatchResponse:
    """Stop a batch for good. There is no undo.

    Everyone not yet called immediately reads as `canceled`. Calls already
    ringing or in progress are **not** dropped — they run to their natural end,
    and the batch is only finished once they have.
    """
    return await batch_svc.cancel_batch(batch_id, ctx)


@batches_router.delete("/{batch_id}", response_model=OkResponse)
async def delete_call_batch(batch_id: UUID, ctx: Context = WriteCtxDep) -> OkResponse:
    """Delete a finished batch and its recipient list for good. Its calls stay.

    Refused while it is live or a call of it is still in progress; cancel it first.
    """
    await batch_svc.delete_batch(batch_id, ctx)
    return OkResponse()
