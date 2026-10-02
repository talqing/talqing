"""What a region asks the control plane.

Every route here is authenticated by a per-region bearer token from control's own
config — a deployment credential, not a tenant's — and none of them is in any
document, SDK or MCP tool list. A region holds this token instead of a DSN into
the global identity store, which is the whole reason the control plane is an API
at all: a region droplet runs tenant-authored tool code and already holds the
tenant secrets key, and a connection string into every user, organization and
access token does not belong on that box.

**One secret per region, used in both directions.** A region sends it as its
``control.internal_token``; control matches it against that region's
``regions[<slug>].internal_token`` below, and sends the same value back when it
pushes credit. The two fields must hold the same string — see
``settings._internal_token``, which is where that rule is written down. Each
region has its own, so a compromised region still cannot speak for another.

Rotating one is a two-sided edit on two droplets deployed minutes apart, so the
receiving side accepts ``previous_internal_token`` as well for the length of the
rotation — otherwise the swap 401s every request in that region for the gap
between the deploys, and there is no third party to arbitrate it.
"""

from __future__ import annotations

import json
import logging
from decimal import Decimal
from hmac import compare_digest
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

import db
from api.control import identity
from api.core.schemas import OkResponse
from services import billing
from services.credits import CreditCheckoutResponse, CreditPackId
from services.user import Tenant, User
from settings import RegionConfig, get_settings

logger = logging.getLogger("talqing.api.control.internal")


def _calling_region(request: Request) -> RegionConfig:
    """Which region is calling, from its own token.

    Returning the region rather than a bare "authorized" is what makes a top-up's
    region come from a CREDENTIAL: nothing in a request body names a region, so
    nobody can start a checkout that credits the other one — where the money
    would then be stuck, since there is no transfer between balances.
    """
    authz = request.headers.get("authorization", "")
    token = authz[7:].strip() if authz.lower().startswith("bearer ") else ""
    if token:
        for region in get_settings().regions:
            # `compare_digest`, because the value being compared arrives from
            # whoever made the request and `==` on str returns at the first byte
            # that differs. The attack is impractical over the internet and the
            # fix is one call, so there is no argument to have about the most
            # privileged endpoint in the deployment.
            if any(compare_digest(token, known) for known in region.accepted_tokens):
                return region
    raise HTTPException(status_code=401, detail="not a known region credential")


RegionDep = Annotated[RegionConfig, Depends(_calling_region)]

router = APIRouter(prefix="/internal/v1", include_in_schema=False)


# ── identity ─────────────────────────────────────────────────────────────────


class AuthContextRequest(BaseModel):
    user_id: str
    tenant_id: str
    # Present when the credential was a personal access token, and it is what
    # adds the revocation lookup: a PAT's signature never expires, so its row is
    # the only thing that can withdraw it.
    pat_jti: str | None = None


class AuthContextResponse(BaseModel):
    user: User
    tenant: Tenant


@router.post("/auth-context", response_model=AuthContextResponse)
async def auth_context(body: AuthContextRequest, region: RegionDep) -> AuthContextResponse:
    """Who a credential is: the user, their role, and the organization. One query.

    This is the request every authenticated call in every region waits on, so it
    answers all three together — returning them separately would pay the ~150 ms
    round trip twice per request.

    Nothing is cached on either side. That is what keeps the promise that
    deleting a memberships row 401s the very next call, with no revocation
    machinery and no invalidation fan-out.
    """
    try:
        user, tenant = await identity.resolve(
            user_id=body.user_id, tenant_id=body.tenant_id, pat_jti=body.pat_jti
        )
    except identity.Refused as exc:
        # 401 with control's own wording, which the region hands straight back to
        # its caller. Distinct from any other status, which the region must read
        # as "could not ask" rather than "not authorized".
        raise HTTPException(status_code=401, detail=exc.message) from exc
    return AuthContextResponse(user=user, tenant=tenant)


class UserEmailsRequest(BaseModel):
    user_ids: list[UUID]


class UserEmailsResponse(BaseModel):
    emails: dict[UUID, str]


@router.post("/users/emails", response_model=UserEmailsResponse)
async def user_emails(body: UserEmailsRequest, region: RegionDep) -> UserEmailsResponse:
    """Names for user ids a region already holds.

    Agent versions record who published them, and users live here — so a region
    showing a version history has ids and no names. Deliberately NOT scoped by
    membership: the ids come from that tenant's own version rows, and who
    published a version should survive them leaving the organization.

    A cold path (one version panel), so it costs a round trip nobody is timing.
    """
    if not body.user_ids:
        return UserEmailsResponse(emails={})
    pool = await db.control_pool()
    rows = await pool.fetch(
        "SELECT id, email FROM users WHERE id = ANY($1::uuid[])", list(body.user_ids)
    )
    return UserEmailsResponse(emails={row["id"]: row["email"] for row in rows})


# ── OAuth Dynamic Client Registration ────────────────────────────────────────
#
# `oauth_dcr_clients` is a control-plane table and stays one, so a region reads
# and writes it through here. The cache key is (provider, redirect_uri) and every
# region has its own redirect URI, so each region registers once and they cannot
# collide — the table is shared, the rows are not.


class DcrLookupRequest(BaseModel):
    provider: str
    redirect_uri: str


class DcrLookupResponse(BaseModel):
    client_id: str | None = None


class DcrStoreRequest(DcrLookupRequest):
    client_id: str
    metadata: dict = {}


@router.post("/dcr-client/get", response_model=DcrLookupResponse)
async def get_dcr_client(body: DcrLookupRequest, region: RegionDep) -> DcrLookupResponse:
    pool = await db.control_pool()
    client_id = await pool.fetchval(
        "SELECT client_id FROM oauth_dcr_clients WHERE provider = $1 AND redirect_uri = $2",
        body.provider,
        body.redirect_uri,
    )
    return DcrLookupResponse(client_id=(str(client_id).strip() or None) if client_id else None)


@router.post("/dcr-client/put", response_model=OkResponse)
async def put_dcr_client(body: DcrStoreRequest, region: RegionDep) -> OkResponse:
    pool = await db.control_pool()
    await pool.execute(
        """
        INSERT INTO oauth_dcr_clients (provider, redirect_uri, client_id, metadata)
        VALUES ($1, $2, $3, $4::jsonb)
        ON CONFLICT (provider, redirect_uri) DO UPDATE
        SET client_id = EXCLUDED.client_id,
            metadata = EXCLUDED.metadata,
            updated_at = now()
        """,
        body.provider,
        body.redirect_uri,
        body.client_id,
        json.dumps(body.metadata),
    )
    return OkResponse()


class TenantRequest(BaseModel):
    tenant_id: str


class TenantResponse(BaseModel):
    tenant: Tenant | None


@router.post("/tenant", response_model=TenantResponse)
async def get_tenant(body: TenantRequest, region: RegionDep) -> TenantResponse:
    """One organization, for the region paths that have a tenant and no user.

    ``null`` rather than a 404: session finalize, a queued job and an inbound
    webhook all name a tenant that may have been deleted since, and "there is no
    such organization" is a real answer they each handle.
    """
    return TenantResponse(tenant=await identity.load_tenant(body.tenant_id))


# ── credits ──────────────────────────────────────────────────────────────────


class CheckoutRequest(BaseModel):
    tenant_id: UUID
    user_id: UUID
    user_email: str
    user_name: str | None = None
    pack: CreditPackId


@router.post("/topups/checkout", response_model=CreditCheckoutResponse)
async def start_checkout(body: CheckoutRequest, region: RegionDep) -> CreditCheckoutResponse:
    """Create the Dodo session and record the top-up, in that order.

    The whole operation lives here rather than being split with the region,
    because the session must exist before the ``credit_topups`` row — a row
    holding a placeholder session id is a row the webhook can never find — and
    both halves have to stay in one process for that to hold.

    The region comes from the calling token above and never from ``body``.
    """
    try:
        return await billing.create_credit_checkout(
            pack_id=body.pack,
            tenant_id=body.tenant_id,
            user_id=body.user_id,
            user_email=body.user_email,
            user_name=body.user_name,
            region=region.slug,
        )
    except billing.TopupError as exc:
        # The message is written for the BUYER, and the region passes it through
        # unchanged. 503 rather than 500: every cause is ours and transient from
        # where they are standing.
        raise HTTPException(status_code=503, detail=str(exc)) from exc


class PendingRequest(BaseModel):
    tenant_id: UUID
    # The top-up the dashboard is waiting on, when it is waiting on one. Its
    # control-side status is what splits the billing page's single vague "we have
    # not seen it" into two true sentences.
    topup_id: UUID | None = None


class PendingIssuanceResponse(BaseModel):
    issuance_id: UUID
    kind: str
    amount: Decimal
    topup_id: UUID | None = None
    note: str | None = None


class PendingResponse(BaseModel):
    issuances: list[PendingIssuanceResponse]
    topup_status: str | None = None


@router.post("/credits/pending", response_model=PendingResponse)
async def pending_credits(body: PendingRequest, region: RegionDep) -> PendingResponse:
    """Credit issued for this tenant IN THE CALLING REGION that is not applied yet.

    The backstop for a push that failed, asked for on demand by the region's
    billing page. There is no sweep and no timer anywhere: ``applied_at IS NULL``
    is an operator's list, not a queue — the same trade ``scheduled_jobs`` makes
    with no lease and no reclaim loop.
    """
    issuances = await billing.pending_for(tenant_id=body.tenant_id, region=region.slug)
    status = (
        await billing.topup_status(body.topup_id, tenant_id=body.tenant_id)
        if body.topup_id
        else None
    )
    return PendingResponse(
        issuances=[PendingIssuanceResponse(**i.model_dump()) for i in issuances],
        topup_status=status,
    )


class AppliedEntry(BaseModel):
    issuance_id: UUID
    # The kind decides which stamp this lands on: a top-up has two — one for the
    # credit and one for the reversal — because both directions are a push and
    # both can fail independently.
    kind: str


class AppliedRequest(BaseModel):
    applied: list[AppliedEntry]


@router.post("/credits/applied", response_model=OkResponse)
async def confirm_applied(body: AppliedRequest, region: RegionDep) -> OkResponse:
    """Stamp the issuances this region has now written to its ledger.

    Scoped to the calling region, exactly as ``credits/pending`` above is: a
    region stamps only what was issued to it.
    """
    await billing.mark_applied(
        [(entry.issuance_id, entry.kind) for entry in body.applied], region=region.slug
    )
    return OkResponse()
