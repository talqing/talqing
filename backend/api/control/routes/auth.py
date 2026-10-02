"""Google OAuth login for the dashboard.

Signup and login are the same flow: the first successful Google sign-in for an
email creates the user, and every visit re-issues a session cookie naming one
organization.

There is no domain-based auto-join. `email_verified: true` from Google proves
only that someone could read mail at that address on the day the account was
created — Google will issue a consumer account on any address you can receive
mail at — so keying membership on the email's domain would let a shared alias or
a former employee walk into a workspace. Membership comes from an invite or from
creating an organization, and nothing else.
"""

from __future__ import annotations

import logging
from urllib.parse import urlencode
from uuid import UUID

import asyncpg
import httpx
from fastapi import APIRouter, HTTPException, Query, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

import db
from api.control.deps import Context, CtxDep
from api.core import security
from api.core.schemas import OkResponse
from services import billing
from services.user import (
    OrgRole,
    Tenant,
    User,
    add_member,
    create_organization,
    provision_mcp_token,
)
from settings import get_settings

logger = logging.getLogger("talqing.api.auth")

router = APIRouter(prefix="/auth", tags=["auth"])

_GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
_GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"


class PublicUserResponse(BaseModel):
    id: UUID
    email: str
    role: OrgRole
    name: str | None = None
    picture_url: str | None = None


class PublicTenantResponse(BaseModel):
    id: UUID
    name: str
    # Where this organization was created, and the only thing that decides:
    # which region the dashboard opens in when the member has no stored
    # preference for it. Every region serves every organization, so it grants
    # nothing and blocks nothing.
    home_region: str


class AuthContextResponse(BaseModel):
    user: PublicUserResponse
    tenant: PublicTenantResponse


def set_session_cookie(resp: Response, token: str) -> None:
    """Attach the dashboard session cookie. Shared with the org switcher, which
    re-issues the same cookie for a different organization.

    ``domain`` is what makes the cookie reach a region at all. It is issued by
    `api.control.talqing.com` and has to be sent to `api.in.talqing.com` and
    `api.us.talqing.com`, which a host-only cookie never would. `SameSite=Lax`
    keeps working because every one of those hosts shares the domain.

    Two costs, stated so nobody rediscovers them. The cookie goes to every other
    host under that domain, marketing site included. And a cookie is keyed by
    (name, domain, path) and nothing else, so **two environments that name the
    same domain share one cookie** — which is why dev scopes itself to
    `dev.talqing.com` and only prod claims `talqing.com`. Null (local
    development) is host-only, which is right when there is one host.
    """
    s = get_settings()
    resp.set_cookie(
        key=s.security.session_cookie_name,
        value=token,
        httponly=True,
        samesite="lax",
        secure=s.env != "local",
        max_age=security.SESSION_TTL_SECONDS,
        path="/",
        domain=s.security.session_cookie_domain,
    )


def _public_ctx(user: User, tenant: Tenant, home_region: str) -> AuthContextResponse:
    return AuthContextResponse(
        user=PublicUserResponse(
            id=user.id,
            email=user.email,
            role=user.role,
            name=user.name,
            picture_url=user.picture_url,
        ),
        tenant=PublicTenantResponse(id=tenant.id, name=tenant.name, home_region=home_region),
    )


def _login_error_redirect(code: str) -> RedirectResponse:
    s = get_settings()
    url = f"{s.app.dashboard_public_url.rstrip('/')}/login?{urlencode({'error': code})}"
    return RedirectResponse(url=url, status_code=302)


async def _accept_pending_invites(
    conn: asyncpg.Connection, *, user_id: UUID, email: str
) -> list[UUID]:
    """Turn every pending invite for this address into a membership.

    Runs on every login, not just the first: an invite written in the moment
    between a signup reading this table and committing its user row would
    otherwise stay pending forever, with nothing to resolve it.

    Returns the organizations joined, oldest invite first.
    """
    invites = await conn.fetch(
        """
        UPDATE org_invites
        SET accepted_at = now()
        WHERE lower(email) = $1 AND accepted_at IS NULL AND revoked_at IS NULL
        RETURNING tenant_id, role, invited_by, created_at
        """,
        email,
    )
    joined = [dict(row) for row in invites]
    joined.sort(key=lambda row: row["created_at"])
    for invite in joined:
        await add_member(
            conn,
            user_id=user_id,
            tenant_id=invite["tenant_id"],
            role=invite["role"],
            invited_by=invite["invited_by"],
        )
    return [invite["tenant_id"] for invite in joined]


async def _sign_in_google(
    *,
    email: str,
    google_sub: str,
    name: str | None,
    picture_url: str | None,
    country: str | None,
) -> tuple[User, Tenant]:
    """Resolve a verified Google identity to a user and the organization to open.

    A visitor who belongs nowhere — a first-time signup, or someone an admin has
    since removed from the last organization they were in — lands in whichever
    organizations invited them, or in a fresh one of their own. A visitor who
    belongs somewhere lands where they were last.

    The whole thing runs in one transaction, so a failure part-way cannot leave
    a user row behind with no organization to sign into.
    """
    pool = await db.control_pool()
    async with pool.acquire() as conn, conn.transaction():
        user_row = await conn.fetchrow(
            """
            SELECT id, email, google_sub, name, picture_url, last_tenant_id, created_at
            FROM users
            WHERE google_sub = $1 OR lower(email) = $2
            ORDER BY CASE WHEN google_sub = $1 THEN 0 ELSE 1 END
            LIMIT 1
            """,
            google_sub,
            email,
        )
        if user_row:
            # Google is the source of truth for all four on every login: an email
            # or a display name changed there should show up here next visit.
            user_row = await conn.fetchrow(
                """
                UPDATE users SET google_sub = $2, email = $3, name = $4, picture_url = $5
                WHERE id = $1
                RETURNING id, email, google_sub, name, picture_url, last_tenant_id, created_at
                """,
                user_row["id"],
                google_sub,
                email,
                name,
                picture_url,
            )
        else:
            user_row = await conn.fetchrow(
                """
                INSERT INTO users (email, google_sub, name, picture_url)
                VALUES ($1, $2, $3, $4)
                RETURNING id, email, google_sub, name, picture_url, last_tenant_id, created_at
                """,
                email,
                google_sub,
                name,
                picture_url,
            )

        user_id = user_row["id"]
        invited_to = await _accept_pending_invites(conn, user_id=user_id, email=email)

        memberships = await conn.fetch(
            """
            SELECT m.tenant_id, m.role, t.name, t.retention_days, t.created_at
            FROM memberships m
            JOIN tenants t ON t.id = m.tenant_id
            WHERE m.user_id = $1
            ORDER BY m.joined_at, t.created_at
            """,
            user_id,
        )
        # The one organization eligible for the signup grant, if this login made
        # one. `POST /v1/orgs` deliberately does not grant, which is the whole of
        # "one grant per person, not one per organization they create".
        granted_org: Tenant | None = None
        if not memberships:
            # Belonging nowhere is not only a brand-new signup: an admin may
            # remove someone from what happens to be their last organization,
            # and refusing that would make one person's account state a veto
            # over another organization's roster. So a member with nothing left
            # signs in exactly as a newcomer does, rather than being locked out
            # of an account they can no longer reach by any other route.
            #
            # Named after them rather than named *as* them, so the switcher reads
            # as a place rather than as a person. Renameable from /settings.
            org = await create_organization(
                conn,
                name=f"{name or email}'s Org",
                owner_id=user_id,
                # The visitor's country, mapped through control's region list.
                # The COUNTRY is what crossed the wire rather than a slug, so
                # adding a region and pointing the EEA at it is one config edit
                # here and not a dashboard deploy.
                home_region=get_settings().region_for_country(country).slug,
            )
            granted_org = org
            memberships = await conn.fetch(
                """
                SELECT m.tenant_id, m.role, t.name, t.retention_days, t.created_at
                FROM memberships m
                JOIN tenants t ON t.id = m.tenant_id
                WHERE m.user_id = $1 AND m.tenant_id = $2
                """,
                user_id,
                org.id,
            )

        by_id = {row["tenant_id"]: row for row in memberships}
        if user_row["last_tenant_id"] in by_id:
            chosen = by_id[user_row["last_tenant_id"]]
        elif invited_to:
            # First login off an invite: open the organization that invited them
            # first, not one they were added to later.
            chosen = by_id[invited_to[0]]
        else:
            chosen = memberships[0]

        await conn.execute(
            "UPDATE users SET last_tenant_id = $2 WHERE id = $1",
            user_id,
            chosen["tenant_id"],
        )

    user = User(
        id=user_id,
        email=user_row["email"],
        role=chosen["role"],
        name=user_row["name"],
        picture_url=user_row["picture_url"],
        google_sub=user_row["google_sub"],
        created_at=user_row["created_at"],
    )
    tenant = Tenant(
        id=chosen["tenant_id"],
        name=chosen["name"],
        retention_days=chosen["retention_days"],
        created_at=chosen["created_at"],
    )
    # Outside the transaction, and deliberately: this is what lets a brand-new
    # user's first dashboard visit already show a working MCP configuration, but
    # GET /v1/mcp-token re-provisions if it is ever missing, so failing here must
    # not cost them the signup.
    await provision_mcp_token(user.id, tenant.id)
    if granted_org is not None:
        # Same reasoning one step further, and now it fires ONCE PER REGION: the
        # organization exists in every region, so a grant in only one of them
        # would be a balance the customer cannot spend in the other. The cost to
        # hold consciously is that one signup costs `signup_grant_usd × regions`.
        #
        # Each grant is a control-plane row plus a push to that region. The row
        # is what makes a failed push recoverable at all — unlike the MCP token
        # there is no endpoint that re-provisions a missing grant, so this must
        # not be able to cost the user their login, and a push that fails is
        # picked up the next time that region's billing page reconciles.
        await billing.grant_signup_credit(granted_org.id)
    return user, tenant


@router.get("/google/start", include_in_schema=False)
async def google_login_start(country: str | None = Query(default=None, max_length=2)):
    """Begin Google sign-in.

    ``country`` is the visitor's ISO 3166-1 alpha-2 code, read by the DASHBOARD
    off its own Cloudflare edge (`/cdn-cgi/trace`) before anyone is signed in. It
    decides one thing: which region a brand-new workspace is created in.

    It comes from the browser, so a visitor can send any country they like, and
    that is fine here and would not be anywhere else on this API — every region
    serves every organization, the switcher is one click away, and `home_region`
    grants no privilege and blocks nothing.

    Absent is the normal case, not a failure: a dashboard served from somewhere
    that is not behind Cloudflare has no `/cdn-cgi/trace`, and login must never
    wait on that fetch. No country means the default region.
    """
    s = get_settings()
    if not s.oauth.google.client_id or not s.oauth.google.client_secret:
        raise HTTPException(status_code=503, detail="Google login is not configured")
    if not s.oauth.google.login_redirect_uri:
        raise HTTPException(status_code=503, detail="Google login redirect URI is not configured")

    # In the SIGNED state, because that is what survives the round trip to
    # Google — a query parameter on our own redirect would not come back.
    state = security.create_google_login_state(country=country)
    params = {
        "client_id": s.oauth.google.client_id,
        "redirect_uri": s.oauth.google.login_redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "access_type": "online",
        "prompt": "select_account",
    }
    return RedirectResponse(url=f"{_GOOGLE_AUTH_URL}?{urlencode(params)}", status_code=302)


@router.get("/google/callback", include_in_schema=False)
async def google_login_callback(
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
    error_description: str | None = Query(default=None),
):
    if error:
        logger.warning("google login denied: %s (%s)", error, error_description)
        return _login_error_redirect("oauth_denied")
    if not code or not state:
        return _login_error_redirect("oauth_failed")
    login_state = security.read_google_login_state(state)
    if login_state is None:
        return _login_error_redirect("oauth_failed")

    s = get_settings()
    if (
        not s.oauth.google.client_id
        or not s.oauth.google.client_secret
        or not s.oauth.google.login_redirect_uri
    ):
        return _login_error_redirect("oauth_failed")

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            token_resp = await client.post(
                _GOOGLE_TOKEN_URL,
                data={
                    "code": code,
                    "client_id": s.oauth.google.client_id,
                    "client_secret": s.oauth.google.client_secret,
                    "redirect_uri": s.oauth.google.login_redirect_uri,
                    "grant_type": "authorization_code",
                },
            )
            if token_resp.status_code != 200:
                logger.warning(
                    "google token exchange failed: %s %s",
                    token_resp.status_code,
                    token_resp.text,
                )
                return _login_error_redirect("oauth_failed")
            token_body = token_resp.json()
            access_token = token_body.get("access_token")
            if not access_token:
                return _login_error_redirect("oauth_failed")

            info_resp = await client.get(
                _GOOGLE_USERINFO_URL,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if info_resp.status_code != 200:
                logger.warning(
                    "google userinfo failed: %s %s",
                    info_resp.status_code,
                    info_resp.text,
                )
                return _login_error_redirect("oauth_failed")
            info = info_resp.json()
    except httpx.HTTPError:
        logger.exception("google login http error")
        return _login_error_redirect("oauth_failed")

    google_sub = info.get("sub")
    email = info.get("email")
    email_verified = info.get("email_verified")
    if not google_sub or not email or email_verified is not True:
        return _login_error_redirect("oauth_failed")

    email = str(email).strip().lower()
    try:
        user, tenant = await _sign_in_google(
            email=email,
            google_sub=str(google_sub),
            # Both are optional in the userinfo response, so `None` here means
            # Google did not send one — not that we failed to read it.
            name=info.get("name"),
            picture_url=info.get("picture"),
            country=login_state["country"],
        )
    except Exception:
        logger.exception("google login provisioning failed for %s", email)
        return _login_error_redirect("oauth_failed")

    session = security.create_session(str(user.id), str(tenant.id))
    redirect = RedirectResponse(
        url=f"{s.app.dashboard_public_url.rstrip('/')}/agents",
        status_code=302,
    )
    set_session_cookie(redirect, session)
    return redirect


@router.post("/logout", response_model=OkResponse)
async def logout(response: Response):
    # Stateless JWT cookie — clearing it ends the session client-side. The domain
    # has to match the one it was set with, or the browser deletes nothing and
    # the user stays signed in.
    s = get_settings()
    response.delete_cookie(
        s.security.session_cookie_name, path="/", domain=s.security.session_cookie_domain
    )
    return OkResponse()


@router.get("/me", response_model=AuthContextResponse)
async def me(ctx: Context = CtxDep):
    """Who this credential is: the signed-in user, their role, and the organization.

    Everything else you can reach is scoped to that organization, and `role`
    (`ADMIN`, `EDITOR` or `VIEWER`) decides whether writes will be accepted. A
    user may belong to several organizations with a different role in each; this
    reports the one this credential is for.

    `tenant.home_region` is read here rather than carried on the tenant object: a
    region has no use for it — every region serves every organization — so it
    stays a control-plane fact the dashboard asks for, and not a field every
    worker in every region hauls around.
    """
    pool = await db.control_pool()
    home_region = await pool.fetchval(
        "SELECT home_region FROM tenants WHERE id = $1", ctx.tenant.id
    )
    return _public_ctx(ctx.user, ctx.tenant, home_region)
