"""The payment processor's callback.

The only billing route on the control plane, and the only one that could be: the
balance is regional, and a Dodo webhook arrives with no region context. It is
also why there is exactly one endpoint for the whole deployment — Dodo has one
business, so a per-region webhook is not merely unnecessary, it is not
expressible.

Everything a tenant reads or does about credits — the balance, the ledger,
starting a purchase — is on their region's API.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request

from api.core.schemas import OkResponse
from services import billing

logger = logging.getLogger("talqing.api.control.billing")

router = APIRouter(prefix="/billing", tags=["billing"])


@router.post("/dodo/webhook", response_model=OkResponse, include_in_schema=False)
async def receive_dodo_webhook(request: Request) -> OkResponse:
    """Dodo Payments' callback. A processor's notification, not an operation.

    Unauthenticated by URL and authenticated by signature, so the raw body is read
    before anything parses it — the signature is over those exact bytes.
    """
    raw_body = await request.body()
    headers = {k.lower(): v for k, v in request.headers.items()}
    try:
        await billing.receive_dodo_webhook(headers, raw_body)
    except billing.WebhookSignatureError as exc:
        # 401, not 400: this is a credential failure, and Dodo's retries must not
        # be encouraged to redeliver something we will never accept.
        logger.warning("dodo webhook rejected: %s", exc)
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    return OkResponse()
