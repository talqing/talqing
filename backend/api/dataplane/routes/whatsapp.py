"""WhatsApp outbound HTTP adapter over services.whatsapp."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Query

from api.core.schemas import OkResponse, Page
from api.dataplane.deps import Context, CtxDep, WriteCtxDep
from services import integrations
from services import whatsapp as svc
from services.email import RecipientActionResponse, SelectRecipientsRequest
from services.whatsapp.models import (
    AddWhatsAppRecipientsRequest,
    AddWhatsAppRecipientsResponse,
    CreateWhatsAppBatchRequest,
    PatchWhatsAppBatchRequest,
    PatchWhatsAppRecipientRequest,
    SendWhatsAppBatchRequest,
    TwilioSender,
    TwilioSendersRequest,
    TwilioSendersResponse,
    WhatsAppBatchResponse,
    WhatsAppBatchStatus,
    WhatsAppRecipientFilter,
    WhatsAppRecipientResponse,
    WhatsAppTemplatesResponse,
)

router = APIRouter(prefix="/whatsapp", tags=["whatsapp"])


@router.get("/templates", response_model=WhatsAppTemplatesResponse)
async def list_whatsapp_templates(
    integration_id: UUID = Query(..., description="A connected WhatsApp sender."),
    ctx: Context = CtxDep,
) -> WhatsAppTemplatesResponse:
    """List a WhatsApp sender's approved templates, read live from its BSP.

    One with `unsupported_reason` set cannot be sent from a batch.
    """
    return await svc.list_templates(ctx, integration_id)


@router.post("/twilio-senders", response_model=TwilioSendersResponse)
async def list_twilio_whatsapp_senders(
    body: TwilioSendersRequest, ctx: Context = WriteCtxDep
) -> TwilioSendersResponse:
    """List the WhatsApp senders on a Twilio account, before connecting one. Stores nothing."""
    senders = await integrations.list_twilio_whatsapp_senders(
        ctx, account_sid=body.account_sid, credentials_ref=body.auth_token
    )
    return TwilioSendersResponse(senders=[TwilioSender(**s) for s in senders])


@router.post("/batches", status_code=201, response_model=WhatsAppBatchResponse)
async def create_whatsapp_batch(
    body: CreateWhatsAppBatchRequest, ctx: Context = WriteCtxDep
) -> WhatsAppBatchResponse:
    """Create a WhatsApp batch: one approved template, sent once per CSV row. Sends nothing yet.

    Only message people who opted in to hear from you on WhatsApp. A row with an
    invalid or repeated phone, or a blank or line-broken mapped cell, is kept and
    skipped with the reason. Replies go to the sender's assigned agent, which
    sees the row's cells as `{{userdata.<column>}}`.
    """
    return await svc.create_batch(body, ctx)


@router.get("/batches", response_model=Page[WhatsAppBatchResponse])
async def list_whatsapp_batches(
    ctx: Context = CtxDep,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    status: WhatsAppBatchStatus | None = Query(None),
) -> Page[WhatsAppBatchResponse]:
    """List WhatsApp batches, newest first."""
    return await svc.list_batches(ctx, limit=limit, offset=offset, status=status)


@router.get("/batches/{batch_id}", response_model=WhatsAppBatchResponse)
async def get_whatsapp_batch(batch_id: UUID, ctx: Context = CtxDep) -> WhatsAppBatchResponse:
    """One batch: its template, schedule and per-status counts, `replied` included."""
    return await svc.get_batch(ctx, batch_id)


@router.patch("/batches/{batch_id}", response_model=WhatsAppBatchResponse)
async def patch_whatsapp_batch(
    batch_id: UUID, body: PatchWhatsAppBatchRequest, ctx: Context = WriteCtxDep
) -> WhatsAppBatchResponse:
    """Edit a batch. Pacing and the window reach a live send within minutes.

    `variable_map` changes only before sending, `start_at` only before it begins;
    a finished batch takes `name` alone.
    """
    return await svc.patch_batch(batch_id, body, ctx)


@router.post("/batches/{batch_id}/send", response_model=WhatsAppBatchResponse)
async def send_whatsapp_batch(
    batch_id: UUID, body: SendWhatsAppBatchRequest, ctx: Context = WriteCtxDep
) -> WhatsAppBatchResponse:
    """Send every ready row. **This messages real people and cannot be undone.**

    `start_at` schedules it, `window` holds it to business hours, and
    `send_daily_cap` (default a fixed 250, Meta's lowest tier; or a ramp) and
    `send_gap_seconds` pace it.
    """
    return await svc.send_batch(batch_id, body, ctx)


@router.post("/batches/{batch_id}/pause", response_model=WhatsAppBatchResponse)
async def pause_whatsapp_batch(batch_id: UUID, ctx: Context = WriteCtxDep) -> WhatsAppBatchResponse:
    """Stop sending before the next message. Nothing is lost."""
    return await svc.pause_batch(batch_id, ctx)


@router.post("/batches/{batch_id}/resume", response_model=WhatsAppBatchResponse)
async def resume_whatsapp_batch(
    batch_id: UUID, ctx: Context = WriteCtxDep
) -> WhatsAppBatchResponse:
    """Carry on sending from where the batch stopped."""
    return await svc.resume_batch(batch_id, ctx)


@router.post("/batches/{batch_id}/cancel", response_model=WhatsAppBatchResponse)
async def cancel_whatsapp_batch(
    batch_id: UUID, ctx: Context = WriteCtxDep
) -> WhatsAppBatchResponse:
    """Stop for good. Messages already sent stay sent, and replies are still answered."""
    return await svc.cancel_batch(batch_id, ctx)


@router.delete("/batches/{batch_id}", response_model=OkResponse)
async def delete_whatsapp_batch(batch_id: UUID, ctx: Context = WriteCtxDep) -> OkResponse:
    """Delete a batch and its rows for good. A live batch must be canceled first.

    The conversations it started stay; its delivery and reply counts do not.
    """
    await svc.delete_batch(batch_id, ctx)
    return OkResponse()


@router.get("/batches/{batch_id}/recipients", response_model=Page[WhatsAppRecipientResponse])
async def list_whatsapp_batch_recipients(
    batch_id: UUID,
    ctx: Context = CtxDep,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    status: list[WhatsAppRecipientFilter] | None = Query(None),
) -> Page[WhatsAppRecipientResponse]:
    """A batch's rows in upload order, each with its rendered message.

    `status` may repeat; a row matching any of them is returned.
    """
    return await svc.list_recipients(ctx, batch_id, limit=limit, offset=offset, status=status)


@router.post("/batches/{batch_id}/recipients", response_model=AddWhatsAppRecipientsResponse)
async def add_whatsapp_batch_recipients(
    batch_id: UUID, body: AddWhatsAppRecipientsRequest, ctx: Context = WriteCtxDep
) -> AddWhatsAppRecipientsResponse:
    """Append rows; repeated numbers are skipped. **Messages them if sending or completed.**

    A completed batch resumes. A draft or paused one holds the rows until sent or resumed.
    """
    return await svc.add_recipients(batch_id, body, ctx)


@router.patch(
    "/batches/{batch_id}/recipients/{recipient_id}", response_model=WhatsAppRecipientResponse
)
async def patch_whatsapp_batch_recipient(
    batch_id: UUID,
    recipient_id: UUID,
    body: PatchWhatsAppRecipientRequest,
    ctx: Context = WriteCtxDep,
) -> WhatsAppRecipientResponse:
    """Edit cells on a row not yet sent, its phone included. An empty value clears the edit."""
    return await svc.patch_recipient(batch_id, recipient_id, body, ctx)


@router.post("/batches/{batch_id}/recipients/skip", response_model=RecipientActionResponse)
async def skip_whatsapp_batch_recipients(
    batch_id: UUID, body: SelectRecipientsRequest, ctx: Context = WriteCtxDep
) -> RecipientActionResponse:
    """Take ready rows out of the send. Reversible with restore."""
    return await svc.act_on_recipients(batch_id, "skip", body, ctx)


@router.post("/batches/{batch_id}/recipients/restore", response_model=RecipientActionResponse)
async def restore_whatsapp_batch_recipients(
    batch_id: UUID, body: SelectRecipientsRequest, ctx: Context = WriteCtxDep
) -> RecipientActionResponse:
    """Put rows a person skipped back. A row skipped for its cells returns when they are fixed."""
    return await svc.act_on_recipients(batch_id, "restore", body, ctx)
