"""Organizations: the ones you belong to, and the people in the one you are in.

Naming rule — plural ``/orgs`` is *across my memberships*; singular ``/org`` is
*the organization this credential is scoped to*. So ``GET /v1/orgs`` drives the
switcher, while ``GET /v1/org/members`` lists the current organization's roster.

Two invariants are enforced here and nowhere else, because they are the only
things standing between the product and an unreachable organization or a
locked-out user:

1. An organization always keeps at least one ADMIN. Demoting, removing or
   leaving as the last one is refused.
2. You cannot leave your last organization — the next login would just create a
   personal one and you would be back where you started.

None of this is on the published API at all: it lives on the control plane, so
it is invisible to the CoPilots, to the MCP server and to both SDKs. User
management is where a human stays in the loop, and now that is structural rather
than a list.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

import asyncpg
from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import BaseModel, EmailStr, Field, StringConstraints

import db
from api.control.deps import AdminCtxDep, Context, CtxDep
from api.control.routes.auth import set_session_cookie
from api.core import security
from api.core.schemas import OkResponse, Page, page_slice
from services.user import ROLE_ADMIN, OrgRole, add_member, create_organization

router = APIRouter(prefix="/org", tags=["org"])
# Everything that spans a user's memberships rather than acting inside one.
orgs_router = APIRouter(prefix="/orgs", tags=["org"])

OrgName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]


class RoleRequest(BaseModel):
    # A role outside the three is a 422 from the schema, so no route checks it.
    role: OrgRole


class OrgNameRequest(BaseModel):
    name: OrgName


# `None` means unlimited, so "not supplied" needs a value of its own. A negative
# int cannot be confused with a policy: the column refuses one.
UNSET_RETENTION = -1


class PatchOrgRequest(BaseModel):
    """Merge-style, like every other PATCH here: an omitted field is unchanged.

    ``retention_days`` needs the sentinel because ``null`` is a real value —
    "keep everything forever" — and has to be distinguishable from "leave the
    policy alone".
    """

    name: OrgName | None = None
    retention_days: int | None = Field(
        default=UNSET_RETENTION,
        ge=1,
        le=3650,
        description=(
            "How many days to keep call and conversation content — recordings, "
            "transcripts, summaries and the phone numbers on a call. `null` keeps "
            "everything forever, which is the default. Applies to calls from now on."
        ),
    )


class InviteRequest(BaseModel):
    email: EmailStr
    role: OrgRole


class OrgResponse(BaseModel):
    id: UUID
    name: str
    role: OrgRole  # the caller's role in this organization
    # How long this organization keeps call content; null = forever. Readable by
    # any member — a VIEWER who cannot see why calls disappear will open a
    # support ticket about it — but only an admin can change it.
    retention_days: int | None = None
    # Where this organization was created. The dashboard opens it here when the
    # member has no stored preference for it — every region serves every
    # organization, so this grants nothing and blocks nothing.
    home_region: str
    joined_at: datetime


class MemberResponse(BaseModel):
    id: UUID
    email: str
    role: OrgRole
    name: str | None = None
    picture_url: str | None = None
    joined_at: datetime


class InviteResponse(BaseModel):
    id: UUID
    email: str
    role: OrgRole
    created_at: datetime
    # Set when the invited address already had an account, in which case they
    # were added to the organization on the spot and never saw a pending invite.
    accepted_at: datetime | None = None


async def _admin_count(conn: asyncpg.Connection, tenant_id: UUID) -> int:
    return await conn.fetchval(
        "SELECT count(*) FROM memberships WHERE tenant_id = $1 AND role = $2",
        tenant_id,
        ROLE_ADMIN,
    )


async def _drop_membership(conn: asyncpg.Connection, *, user_id: UUID, tenant_id: UUID) -> None:
    """Remove someone from an organization, and with them everything that only
    made sense while they were in it.

    Their personal access tokens for this organization die with the membership —
    the token's JWT still verifies, but every request re-reads ``memberships``,
    so the row would only linger in the organization's token list as a
    credential that can never work again.
    """
    await conn.execute(
        "DELETE FROM memberships WHERE user_id = $1 AND tenant_id = $2",
        user_id,
        tenant_id,
    )
    await conn.execute(
        "DELETE FROM personal_access_tokens WHERE user_id = $1 AND tenant_id = $2",
        user_id,
        tenant_id,
    )
    await conn.execute(
        "UPDATE users SET last_tenant_id = NULL WHERE id = $1 AND last_tenant_id = $2",
        user_id,
        tenant_id,
    )


# ── across my organizations ────────────────────────────────────────────────


@orgs_router.get("", response_model=Page[OrgResponse])
async def list_orgs(
    ctx: Context = CtxDep,
    limit: int = Query(100, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> Page[OrgResponse]:
    """Every organization the signed-in user belongs to, with their role in each.

    Newest membership first, deliberately: an admin can add you to their
    organization without you being asked or emailed, so the one you did not know
    about is the one that needs to be visible.
    """
    pool = await db.control_pool()
    rows = await pool.fetch(
        """
        SELECT t.id, t.name, t.retention_days, t.home_region, m.role, m.joined_at
        FROM memberships m
        JOIN tenants t ON t.id = m.tenant_id
        WHERE m.user_id = $1
        ORDER BY m.joined_at DESC
        LIMIT $2 OFFSET $3
        """,
        ctx.user.id,
        limit + 1,
        offset,
    )
    return page_slice(
        [OrgResponse.model_validate(dict(r)) for r in rows], limit=limit, offset=offset
    )


@orgs_router.post("", status_code=201, response_model=OrgResponse)
async def create_org(body: OrgNameRequest, ctx: Context = CtxDep) -> OrgResponse:
    """Create a new, empty organization with the caller as its ADMIN.

    Any member can do this — it grants nothing in the organization they are
    currently in. Switch into it to start working there.

    No signup grant: `billing.signup_grant_usd` is for an organization created at
    a first sign-in, and that is the whole of "one grant per person, not one per
    organization they create". An organization made here starts at $0 in every
    region.

    Its home region is inherited from the one the caller is already in rather
    than taken from the request or from the configured default. No request body
    or query parameter in this API carries a region slug — the dashboard picks a
    region by base URL, not by field — and defaulting an Indian workspace's
    second organization into `us` would open it empty in a region the person was
    not looking at, which is exactly the "looks like data loss" failure a region
    switcher has to avoid.
    """
    pool = await db.control_pool()
    async with pool.acquire() as conn, conn.transaction():
        home_region = await conn.fetchval(
            "SELECT home_region FROM tenants WHERE id = $1", ctx.tenant.id
        )
        org = await create_organization(
            conn, name=body.name, owner_id=ctx.user.id, home_region=home_region
        )
        joined_at = await conn.fetchval(
            "SELECT joined_at FROM memberships WHERE user_id = $1 AND tenant_id = $2",
            ctx.user.id,
            org.id,
        )
    return OrgResponse(
        id=org.id,
        name=org.name,
        role=ROLE_ADMIN,
        retention_days=org.retention_days,
        home_region=home_region,
        joined_at=joined_at,
    )


@orgs_router.post("/{org_id}/switch", response_model=OkResponse)
async def switch_org(org_id: UUID, response: Response, ctx: Context = CtxDep) -> OkResponse:
    """Point the dashboard session at another of the caller's organizations.

    The session cookie carries the organization, so switching re-issues it — the
    caller's role, resources and billing all move with it. Reload after this.
    """
    pool = await db.control_pool()
    membership = await pool.fetchrow(
        "SELECT role FROM memberships WHERE user_id = $1 AND tenant_id = $2",
        ctx.user.id,
        org_id,
    )
    if not membership:
        raise HTTPException(status_code=404, detail="you are not a member of that organization")
    await pool.execute("UPDATE users SET last_tenant_id = $2 WHERE id = $1", ctx.user.id, org_id)
    set_session_cookie(response, security.create_session(str(ctx.user.id), str(org_id)))
    return OkResponse()


# ── the organization I am in ───────────────────────────────────────────────


@router.patch("", response_model=OrgResponse)
async def patch_org(body: PatchOrgRequest, ctx: Context = AdminCtxDep) -> OrgResponse:
    """Change this organization's name or its data retention policy.

    The name is display only — nothing keys on it.

    `retention_days` is how long call and conversation *content* is kept:
    recordings, transcripts, summaries, extracted fields and the phone numbers
    on a call. When it passes, all of that is deleted and the call itself
    remains, with its duration, outcome and cost, so past invoices still
    resolve. `null` keeps everything forever, which is the default.

    It applies to calls from now on: a call already in the system keeps the
    deadline it was given when it ended, so shortening the policy never
    retroactively destroys anything. Erasing a contact's last call also resets
    what your agents remember about that person.
    """
    pool = await db.control_pool()
    row = await pool.fetchrow(
        """
        UPDATE tenants
        SET name = COALESCE($2, name),
            retention_days = CASE WHEN $4 THEN $3 ELSE retention_days END
        WHERE id = $1
        RETURNING id, name, retention_days, home_region
        """,
        ctx.tenant.id,
        body.name,
        None if body.retention_days == UNSET_RETENTION else body.retention_days,
        body.retention_days != UNSET_RETENTION,
    )
    joined_at = await pool.fetchval(
        "SELECT joined_at FROM memberships WHERE user_id = $1 AND tenant_id = $2",
        ctx.user.id,
        ctx.tenant.id,
    )
    return OrgResponse(
        id=row["id"],
        name=row["name"],
        role=ctx.user.role,
        retention_days=row["retention_days"],
        home_region=row["home_region"],
        joined_at=joined_at,
    )


@router.get("/members", response_model=Page[MemberResponse])
async def list_members(
    ctx: Context = CtxDep,
    limit: int = Query(200, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> Page[MemberResponse]:
    """Every member sees the roster; only an ADMIN can change it."""
    pool = await db.control_pool()
    rows = await pool.fetch(
        """
        SELECT u.id, u.email, u.name, u.picture_url, m.role, m.joined_at
        FROM memberships m
        JOIN users u ON u.id = m.user_id
        WHERE m.tenant_id = $1
        ORDER BY m.joined_at
        LIMIT $2 OFFSET $3
        """,
        ctx.tenant.id,
        limit + 1,
        offset,
    )
    return page_slice(
        [MemberResponse.model_validate(dict(r)) for r in rows], limit=limit, offset=offset
    )


@router.patch("/members/{user_id}", response_model=MemberResponse)
async def set_member_role(user_id: UUID, body: RoleRequest, ctx: Context = AdminCtxDep):
    """Change a member's role. Takes effect on their very next request."""
    pool = await db.control_pool()
    async with pool.acquire() as conn, conn.transaction():
        target = await conn.fetchrow(
            "SELECT role FROM memberships WHERE user_id = $1 AND tenant_id = $2",
            user_id,
            ctx.tenant.id,
        )
        if not target:
            raise HTTPException(status_code=404, detail="member not found in your organization")
        if target["role"] == ROLE_ADMIN and body.role != ROLE_ADMIN:
            if await _admin_count(conn, ctx.tenant.id) <= 1:
                raise HTTPException(
                    status_code=400, detail="an organization needs at least one admin"
                )
        row = await conn.fetchrow(
            """
            UPDATE memberships m SET role = $3
            FROM users u
            WHERE m.user_id = $1 AND m.tenant_id = $2 AND u.id = m.user_id
            RETURNING u.id, u.email, u.name, u.picture_url, m.role, m.joined_at
            """,
            user_id,
            ctx.tenant.id,
            body.role,
        )
    return MemberResponse.model_validate(dict(row))


@router.delete("/members/{user_id}", response_model=OkResponse)
async def remove_member(user_id: UUID, ctx: Context = AdminCtxDep) -> OkResponse:
    """Remove someone from this organization.

    They lose access on their next request, and their access tokens for this
    organization stop working with them. Nothing they created is deleted.

    Allowed even when this is the only organization they belong to — whether
    they have somewhere else to go is not this organization's problem, and their
    next sign-in gives them one of their own.
    """
    if user_id == ctx.user.id:
        # Removing yourself is leaving, and leaving has an extra rule (you cannot
        # leave your last organization) that must not be sidestepped by coming
        # through this door instead.
        raise HTTPException(
            status_code=400, detail="to remove yourself, leave the organization instead"
        )
    pool = await db.control_pool()
    async with pool.acquire() as conn, conn.transaction():
        target = await conn.fetchrow(
            "SELECT role FROM memberships WHERE user_id = $1 AND tenant_id = $2",
            user_id,
            ctx.tenant.id,
        )
        if not target:
            raise HTTPException(status_code=404, detail="member not found in your organization")
        if target["role"] == ROLE_ADMIN and await _admin_count(conn, ctx.tenant.id) <= 1:
            raise HTTPException(status_code=400, detail="an organization needs at least one admin")
        await _drop_membership(conn, user_id=user_id, tenant_id=ctx.tenant.id)
    return OkResponse()


@router.post("/leave", response_model=OkResponse)
async def leave_org(ctx: Context = CtxDep) -> OkResponse:
    """Leave this organization. Any member may, including an admin who has
    promoted a successor. Nothing they created is deleted."""
    pool = await db.control_pool()
    async with pool.acquire() as conn, conn.transaction():
        organizations = await conn.fetchval(
            "SELECT count(*) FROM memberships WHERE user_id = $1", ctx.user.id
        )
        if organizations <= 1:
            raise HTTPException(
                status_code=400,
                detail="you cannot leave your only organization — create or join another first",
            )
        if ctx.user.role == ROLE_ADMIN and await _admin_count(conn, ctx.tenant.id) <= 1:
            raise HTTPException(
                status_code=400,
                detail="an organization needs at least one admin — promote someone before leaving",
            )
        await _drop_membership(conn, user_id=ctx.user.id, tenant_id=ctx.tenant.id)
    return OkResponse()


# ── invites ────────────────────────────────────────────────────────────────


@router.get("/invites", response_model=Page[InviteResponse])
async def list_invites(
    ctx: Context = AdminCtxDep,
    limit: int = Query(200, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> Page[InviteResponse]:
    """Invites waiting on a first login. Accepted and revoked ones drop off the
    list — an accepted invite is a member, and belongs on the roster instead."""
    pool = await db.control_pool()
    rows = await pool.fetch(
        """
        SELECT id, email, role, created_at, accepted_at
        FROM org_invites
        WHERE tenant_id = $1 AND accepted_at IS NULL AND revoked_at IS NULL
        ORDER BY created_at DESC
        LIMIT $2 OFFSET $3
        """,
        ctx.tenant.id,
        limit + 1,
        offset,
    )
    return page_slice(
        [InviteResponse.model_validate(dict(r)) for r in rows], limit=limit, offset=offset
    )


@router.post("/invites", status_code=201, response_model=InviteResponse)
async def create_invite(body: InviteRequest, ctx: Context = AdminCtxDep) -> InviteResponse:
    """Invite an email address into this organization with a role.

    There is no invite link and no accept step: the invite is keyed to a Google
    identity, so signing in with that address *is* the check. Someone who
    already has an account joins immediately; anyone else joins the first time
    they sign in.

    No email is sent at MVP, so an existing user finds out by seeing the
    organization appear in their switcher — tell them yourself.
    """
    email = body.email.strip().lower()
    pool = await db.control_pool()
    async with pool.acquire() as conn, conn.transaction():
        user_id = await conn.fetchval("SELECT id FROM users WHERE lower(email) = $1", email)
        if user_id is not None:
            already = await conn.fetchval(
                "SELECT role FROM memberships WHERE user_id = $1 AND tenant_id = $2",
                user_id,
                ctx.tenant.id,
            )
            if already:
                raise HTTPException(
                    status_code=400,
                    detail=f"{email} is already a member of this organization ({already})",
                )
        pending = await conn.fetchval(
            """
            SELECT id FROM org_invites
            WHERE tenant_id = $1 AND lower(email) = $2
              AND accepted_at IS NULL AND revoked_at IS NULL
            """,
            ctx.tenant.id,
            email,
        )
        if pending:
            raise HTTPException(status_code=400, detail=f"{email} has already been invited")

        # An invite to an address that already has an account is applied on the
        # spot; the row is still written, as the record of who invited whom.
        row = await conn.fetchrow(
            """
            INSERT INTO org_invites (tenant_id, email, role, invited_by, accepted_at)
            VALUES ($1, $2, $3, $4, CASE WHEN $5::uuid IS NULL THEN NULL ELSE now() END)
            RETURNING id, email, role, created_at, accepted_at
            """,
            ctx.tenant.id,
            email,
            body.role,
            ctx.user.id,
            user_id,
        )
        if user_id is not None:
            await add_member(
                conn,
                user_id=user_id,
                tenant_id=ctx.tenant.id,
                role=body.role,
                invited_by=ctx.user.id,
            )
    return InviteResponse.model_validate(dict(row))


@router.delete("/invites/{invite_id}", response_model=OkResponse)
async def revoke_invite(invite_id: UUID, ctx: Context = AdminCtxDep) -> OkResponse:
    """Withdraw a pending invite, so signing in with that address no longer
    joins this organization."""
    pool = await db.control_pool()
    row = await pool.fetchrow(
        """
        UPDATE org_invites SET revoked_at = now()
        WHERE id = $1 AND tenant_id = $2 AND accepted_at IS NULL AND revoked_at IS NULL
        RETURNING id
        """,
        invite_id,
        ctx.tenant.id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="no pending invite with that id")
    return OkResponse()
