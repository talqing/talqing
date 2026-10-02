"""Personal access tokens: no-expiry JWTs for driving the API without the
dashboard (Authorization: Bearer <token>). A token authenticates as its
creator and acts with the user's LIVE role (ADMIN/EDITOR/VIEWER) — change the
role and every existing token follows. Deleting the row is the revocation (the
JWT itself never expires).

Two kinds, which differ only in how the plaintext is handed over:

- ``manual`` — created here by a user, shown ONCE at create and never again.
- ``mcp`` — issued automatically when you join an organization (one per user per
  organization) and readable at any time from ``GET /v1/mcp-token``. It is what
  the dashboard puts into the MCP setup it shows a user, so a fresh signup can
  connect Claude Code or Codex without first learning what an access token is.

Nothing is stored in plaintext either way: a PAT is a signed pointer to its
row, so ``mcp_token`` re-derives the same string rather than reading it back.

A token names one organization. You see your own; an organization admin sees
every token in it, and can revoke a teammate's.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, StringConstraints

import db
from api.control.deps import Context, CtxDep
from api.core import security
from api.core.schemas import OkResponse, Page, page_slice
from services.user import ROLE_ADMIN, provision_mcp_token

router = APIRouter(prefix="/tokens", tags=["tokens"])
# The MCP token is one per user rather than a collection, so it reads as its own
# resource instead of a lookup into /tokens.
mcp_router = APIRouter(tags=["tokens"])


class CreateTokenRequest(BaseModel):
    # strip surrounding space, then enforce 1..100 chars (empty/oversize → 422)
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]


class TokenResponse(BaseModel):
    id: UUID
    name: str
    kind: Literal["manual", "mcp"]
    user_id: UUID
    user_email: str
    created_at: datetime


class TokenCreatedResponse(TokenResponse):
    token: str  # plaintext, shown only at create


class McpTokenResponse(BaseModel):
    """The credential half of an MCP client configuration.

    No API URL: one token reaches every region, so the base URL is whichever
    region the caller wants to work in — a choice only the dashboard is holding.
    """

    id: UUID
    name: str
    token: str
    created_at: datetime


@router.get("", response_model=Page[TokenResponse])
async def list_tokens(
    ctx: Context = CtxDep,
    limit: int = Query(200, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> Page[TokenResponse]:
    """List personal access tokens: your own, or every one in the organization
    if you are an admin.

    Who holds which credential is organization-admin information, so an EDITOR
    or VIEWER sees only what they created. Plaintext token values are never
    returned. A token with `kind: mcp` was issued automatically for the
    dashboard's MCP setup; the rest were created by hand.
    """
    pool = await db.control_pool()
    rows = await pool.fetch(
        """
        SELECT p.id, p.name, p.kind, p.user_id, u.email AS user_email, p.created_at
        FROM personal_access_tokens p
        JOIN users u ON u.id = p.user_id
        WHERE p.tenant_id = $1 AND ($2::uuid IS NULL OR p.user_id = $2)
        ORDER BY p.created_at DESC
        LIMIT $3 OFFSET $4
        """,
        ctx.tenant.id,
        None if ctx.user.role == ROLE_ADMIN else ctx.user.id,
        limit + 1,
        offset,
    )
    return page_slice(
        [TokenResponse.model_validate(dict(r)) for r in rows],
        limit=limit,
        offset=offset,
    )


@router.post("", status_code=201, response_model=TokenCreatedResponse)
async def create_token(body: CreateTokenRequest, ctx: Context = CtxDep):
    """Create a personal access token for driving this API without the dashboard.

    The token authenticates as its creator and acts with that user's live role,
    so it can never do more than they can. It does not expire; deleting it is
    the revocation. The plaintext value is returned once here and never again —
    treat it like a password and do not echo it back into a conversation.
    """
    pool = await db.control_pool()
    row = await pool.fetchrow(
        """
        INSERT INTO personal_access_tokens (user_id, tenant_id, name, kind)
        VALUES ($1, $2, $3, 'manual')
        RETURNING id, name, kind, user_id, created_at
        """,
        ctx.user.id,
        ctx.tenant.id,
        body.name,
    )
    token = security.create_pat(str(ctx.user.id), str(ctx.tenant.id), str(row["id"]))
    # plaintext only here, never again
    return TokenCreatedResponse(
        id=row["id"],
        name=row["name"],
        kind=row["kind"],
        user_id=row["user_id"],
        user_email=ctx.user.email,
        created_at=row["created_at"],
        token=token,
    )


@mcp_router.get("/mcp-token", response_model=McpTokenResponse)
async def mcp_token(ctx: Context = CtxDep):
    """This user's MCP token, in plaintext.

    Unlike a token created by hand, this one can be read whenever it is needed:
    the dashboard renders it into ready-to-paste MCP configuration for Claude
    Code, Codex and Grok. The value is re-derived from the token's row, not
    stored, and is the same string every time — so a configuration a user
    already saved keeps working.

    It is always the caller's own token, never anyone else's, and is created on
    the spot for a user who somehow has none. Delete it from the tokens list to
    revoke it; the next call issues a new one, which rotates the credential.

    No API URL comes with it, and it is not an omission: one token reaches every
    region, and only the dashboard knows which region the person is looking at.
    Control could only answer with its own address, which is the one host an MCP
    client must never be pointed at.
    """
    token_id = await provision_mcp_token(ctx.user.id, ctx.tenant.id)
    pool = await db.control_pool()
    row = await pool.fetchrow(
        "SELECT id, name, created_at FROM personal_access_tokens WHERE id = $1 AND tenant_id = $2",
        token_id,
        ctx.tenant.id,
    )
    return McpTokenResponse(
        id=row["id"],
        name=row["name"],
        token=security.create_pat(str(ctx.user.id), str(ctx.tenant.id), str(row["id"])),
        created_at=row["created_at"],
    )


@router.delete("/{token_id}", response_model=OkResponse)
async def delete_token(token_id: UUID, ctx: Context = CtxDep):
    """Revoke a token immediately and permanently.

    Owners can delete their own; organization admins can delete any in the
    organization. Deleting an `mcp` token rotates it: the owner's next dashboard
    visit issues a replacement, and anything still configured with the old one
    stops working.
    """
    pool = await db.control_pool()
    row = await pool.fetchrow(
        """
        SELECT id, user_id
        FROM personal_access_tokens
        WHERE id = $1 AND tenant_id = $2
        """,
        token_id,
        ctx.tenant.id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="token not found")
    if row["user_id"] != ctx.user.id and ctx.user.role != ROLE_ADMIN:
        raise HTTPException(
            status_code=403,
            detail="only the token owner or an organization admin can delete this token",
        )
    await pool.execute(
        "DELETE FROM personal_access_tokens WHERE id = $1 AND tenant_id = $2",
        token_id,
        ctx.tenant.id,
    )
    return OkResponse()
