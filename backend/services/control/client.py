"""Calling the control plane from a region.

Every authenticated request in every region makes one of these, synchronously,
before it does anything else — there is no cache, deliberately. That keeps
``api.core.deps``' documented promise literally true (deleting a membership 401s
the very next call) at the price of one round trip, measured at roughly 150 ms
from `blr1` to `fra1`. Two things make the difference between 150 ms and 450 ms,
and neither is optional:

1. **One round trip, not two.** ``auth_context`` returns the user, their role and
   the organization in a single response. The alternative — the two queries the
   single-plane version ran — would pay the RTT twice on every request.
2. **A module-level keep-alive HTTP/2 client**, never ``async with
   httpx.AsyncClient()`` per call. Without a pooled connection each call pays TCP
   + TLS + request, which is about 3x the round trip.

Failures split in two, and the split is the point: :class:`ControlRefused` is a
verdict — this credential is not valid, this membership is gone — and is final;
:class:`ControlPlaneError` is "control did not answer", which is transient and
must never be read as a refusal. Confusing them either locks out a legitimate
user during a blip or lets a removed member keep working.
"""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal
from typing import Any
from uuid import UUID

import httpx
from pydantic import BaseModel

from services.user.models import OrgRole, Tenant, User
from settings import get_settings

logger = logging.getLogger("talqing.control.client")


class ControlPlaneError(RuntimeError):
    """The control plane did not answer, or answered with a fault of its own.

    Transient by definition. A caller must retry or fail the request — never
    treat it as "not authorized", which is the opposite verdict.
    """


class ControlRefused(RuntimeError):
    """Control answered, and the answer is no.

    Final: the credential is invalid, revoked, or names an organization this user
    is not a member of. ``message`` is written for the caller of the region's API,
    so it is safe to hand straight back as a 401 body.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class AuthContext(BaseModel):
    """Who a credential is, and the organization it acts in. One round trip."""

    user: User
    tenant: Tenant


class PendingIssuance(BaseModel):
    """One credit issuance control believes this region has not applied yet.

    ``kind`` is what the region writes into its ledger. ``topup_id`` is the
    control-plane row that anchors idempotency at the region: the two partial
    unique indexes on ``credit_ledger`` are what make a reconcile racing a late
    push credit exactly once.
    """

    issuance_id: UUID
    kind: str  # "signup_grant" | "purchase" | "refund"
    amount: Decimal
    topup_id: UUID | None = None
    note: str | None = None


class PendingCredits(BaseModel):
    issuances: list[PendingIssuance] = []
    # Control-side status of the top-up the dashboard is waiting on, when it
    # named one. It is what splits the billing page's single vague "we have not
    # seen it" into two true sentences.
    topup_status: str | None = None


_client: httpx.AsyncClient | None = None
_lock = asyncio.Lock()


async def _http() -> httpx.AsyncClient:
    """The process-wide client. Pooled and HTTP/2 — see the module docstring."""
    global _client
    if _client is None:
        async with _lock:
            if _client is None:
                cfg = _config()
                _client = httpx.AsyncClient(
                    base_url=cfg.api_url,
                    timeout=cfg.timeout_seconds,
                    http2=True,
                    headers={"authorization": f"Bearer {cfg.internal_token}"},
                )
    return _client


def _config():
    cfg = get_settings().control
    if cfg is None:
        raise RuntimeError(
            "this node has no control plane configured — services.control.client is "
            "how a REGION reaches control, and control does not call itself"
        )
    return cfg


async def aclose() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def _post(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    client = await _http()
    try:
        response = await client.post(path, json=payload)
    except httpx.HTTPError as exc:
        raise ControlPlaneError(f"control plane unreachable ({exc.__class__.__name__})") from exc
    if response.status_code == 401:
        # Control's own refusal, phrased for the person holding the credential.
        # Distinguished from OUR token being wrong by the body's shape: a
        # rejected internal token answers with the standard error body too, so
        # the log line below is what tells the two apart in practice.
        raise ControlRefused(_message(response) or "not authorized")
    if response.status_code >= 400:
        logger.error(
            "control plane %s answered %s: %s", path, response.status_code, response.text[:500]
        )
        raise ControlPlaneError(f"control plane refused {path} with {response.status_code}")
    return response.json()


def _message(response: httpx.Response) -> str:
    try:
        detail = response.json().get("detail")
    except ValueError:
        return ""
    if isinstance(detail, dict):
        return str(detail.get("message") or "")
    return str(detail or "")


async def auth_context(*, user_id: str, tenant_id: str, pat_jti: str | None = None) -> AuthContext:
    """Who this credential is: the user, their role here, and the organization.

    The membership join on control's side is the authorization check, exactly as
    it was when a region held the database. ``pat_jti`` adds the revocation
    lookup — a personal access token's signature never expires, so its row is the
    only thing that can withdraw it.
    """
    payload: dict[str, Any] = {"user_id": user_id, "tenant_id": tenant_id}
    if pat_jti:
        payload["pat_jti"] = pat_jti
    return AuthContext.model_validate(await _post("/internal/v1/auth-context", payload))


async def load_tenant(tenant_id: UUID | str) -> Tenant | None:
    """The organization with this id, or ``None`` if there is no such row.

    For the paths that have a tenant and no user: session finalize, a queued job,
    an inbound webhook. Those name a tenant that may have been deleted since, so
    ``None`` is a real answer rather than an impossibility.
    """
    body = await _post("/internal/v1/tenant", {"tenant_id": str(tenant_id)})
    tenant = body.get("tenant")
    return Tenant.model_validate(tenant) if tenant else None


async def user_emails(user_ids: list[UUID]) -> dict[UUID, str]:
    """Names for user ids this region already holds — agent version publishers.

    Users live in the control plane and agent versions in this region's own
    database, so this is a second lookup rather than a join, exactly as it was
    when both were one query away.
    """
    if not user_ids:
        return {}
    body = await _post("/internal/v1/users/emails", {"user_ids": [str(i) for i in user_ids]})
    return {UUID(user_id): email for user_id, email in body["emails"].items()}


async def dcr_client_id(*, provider: str, redirect_uri: str) -> str | None:
    """This deployment's registered OAuth client id for one redirect URI."""
    body = await _post(
        "/internal/v1/dcr-client/get", {"provider": provider, "redirect_uri": redirect_uri}
    )
    return body.get("client_id")


async def store_dcr_client(
    *, provider: str, redirect_uri: str, client_id: str, metadata: dict[str, Any]
) -> None:
    await _post(
        "/internal/v1/dcr-client/put",
        {
            "provider": provider,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "metadata": metadata,
        },
    )


async def start_checkout(
    *, tenant_id: UUID, user_id: UUID, user_email: str, user_name: str | None, pack: str
) -> dict[str, Any]:
    """Ask control to create a Dodo checkout session and record the top-up.

    Both halves stay in control's process on purpose: the session must exist
    before the ``credit_topups`` row, or the row holds a placeholder id the
    webhook can never find. The region forwards rather than doing either, which
    is also what makes the top-up's region come from a credential — this token —
    rather than from a request body someone could point at the other region,
    where the money would then be stuck.
    """
    return await _post(
        "/internal/v1/topups/checkout",
        {
            "tenant_id": str(tenant_id),
            "user_id": str(user_id),
            "user_email": user_email,
            "user_name": user_name,
            "pack": pack,
        },
    )


async def pending_credits(*, tenant_id: UUID, topup_id: UUID | None = None) -> PendingCredits:
    """Credit control has issued for this tenant HERE that we have not applied.

    The backstop for a push that failed, and it is asked for on demand rather
    than swept: Dodo's return URL sends a buyer straight to the billing page, and
    a tenant whose calls are being refused arrives there by the shortest path
    there is. A timer would be a forever-running cross-region call for an event
    that essentially never happens.
    """
    payload: dict[str, Any] = {"tenant_id": str(tenant_id)}
    if topup_id:
        payload["topup_id"] = str(topup_id)
    return PendingCredits.model_validate(await _post("/internal/v1/credits/pending", payload))


async def confirm_applied(applied: list[dict[str, str]]) -> None:
    """Tell control which issuances this region has now written to its ledger.

    Each entry is ``{"issuance_id": ..., "kind": ...}``. The kind travels back
    because it decides the stamp on control's side: a top-up has two —
    ``applied_at`` for the credit and ``reversed_at`` for the reversal — since
    both directions are a push and both can fail independently.
    """
    if not applied:
        return
    await _post("/internal/v1/credits/applied", {"applied": applied})


__all__ = [
    "AuthContext",
    "ControlPlaneError",
    "ControlRefused",
    "OrgRole",
    "PendingCredits",
    "PendingIssuance",
    "aclose",
    "auth_context",
    "confirm_applied",
    "dcr_client_id",
    "load_tenant",
    "pending_credits",
    "start_checkout",
    "store_dcr_client",
    "user_emails",
]
