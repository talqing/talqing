"""Provider webhook HTTP endpoints.

Adapter-dispatched — each channel provider implements ``receive_webhook``::

    POST /v1/integrations/webhooks/{provider}/{tenant_id}/{integration_id}

Answered with an empty 204. Twilio reads a messaging webhook's body as TwiML and
reports anything else as an error on the tenant's account, and Gupshup asks for
an empty 2xx; Telegram accepts either.

The one webhook that answers with a body is a WhatsApp call's Voice URL, whose
answer is the TwiML that routes the call::

    POST /v1/integrations/voice/whatsapp/{tenant_id}/{integration_id}
"""

from __future__ import annotations

import logging
import time
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, Response

from services.integrations.channel import (
    get_channel_adapter,
    resolve_webhook_integration,
)
from services.integrations.providers.whatsapp import calls as whatsapp_calls

router = APIRouter()
logger = logging.getLogger("talqing.latency_debug")


@router.post(
    "/webhooks/{provider}/{tenant_id}/{integration_id}",
    status_code=204,
    response_class=Response,
    include_in_schema=False,
)
async def receive_provider_webhook(
    provider: str,
    tenant_id: UUID,
    integration_id: UUID,
    request: Request,
) -> Response:
    adapter = get_channel_adapter(provider)
    if adapter is None:
        raise HTTPException(status_code=404, detail="integration not found")
    resolved = await resolve_webhook_integration(tenant_id, integration_id, provider)
    if resolved is None:
        raise HTTPException(status_code=404, detail="integration not found")
    tenant, integration = resolved
    raw_body = await request.body()
    # Lower-case header keys so adapters can look up provider names stably.
    headers = {k.lower(): v for k, v in request.headers.items()}
    await adapter.receive_webhook(
        tenant=tenant,
        integration=integration,
        headers=headers,
        query=dict(request.query_params),
        raw_body=raw_body,
    )
    return Response(status_code=204)


@router.post(
    "/voice/whatsapp/{tenant_id}/{integration_id}",
    response_class=Response,
    include_in_schema=False,
)
async def answer_whatsapp_call(
    tenant_id: UUID,
    integration_id: UUID,
    request: Request,
) -> Response:
    """Twilio's Voice URL for a WhatsApp sender: answered with TwiML."""
    t0 = time.monotonic()
    resolved = await resolve_webhook_integration(tenant_id, integration_id, "whatsapp")
    logger.info("latency-debug webhook resolve_integration %d ms", (time.monotonic() - t0) * 1000)
    if resolved is None:
        raise HTTPException(status_code=404, detail="integration not found")
    tenant, integration = resolved
    twiml = await whatsapp_calls.answer_call(
        tenant=tenant,
        integration=integration,
        headers={k.lower(): v for k, v in request.headers.items()},
        raw_body=await request.body(),
    )
    logger.info("latency-debug webhook total %d ms", (time.monotonic() - t0) * 1000)
    return Response(content=twiml, media_type="text/xml")
