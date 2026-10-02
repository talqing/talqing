"""Who is calling — as a region answers it, which is by asking control.

A region holds no identity database. It verifies the credential's signature
locally and offline (Ed25519, no round trip and never a network call), then makes
exactly one request to the control plane for the user, their role and the
organization together.

That request is on every authenticated call and is not cached, which is the
deliberate trade: roughly 150 ms per request, in exchange for
``api.core.deps``' promise staying literally true — deleting the memberships row
401s the very next call, with no revocation machinery and no invalidation
fan-out. A cache here must ship with a control-side invalidation push or it
quietly breaks that.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from fastapi import Depends, HTTPException, Request

from api.core.deps import context_deps, read_credential, register_role
from api.core.security import read_chat_token
from services.control import client as control
from services.user import ROLE_ADMIN, ROLE_EDITOR, Context, Tenant, load_tenant

# Re-exported so route modules can go on writing `ctx: Context = CtxDep`.
__all__ = [
    "AdminCtxDep",
    "ChatAccess",
    "ChatReadDep",
    "ChatWriteDep",
    "Context",
    "CtxDep",
    "WriteCtxDep",
]


async def _resolve(request: Request) -> Context:
    credential = read_credential(request)
    try:
        resolved = await control.auth_context(
            user_id=credential.user_id,
            tenant_id=credential.tenant_id,
            pat_jti=credential.pat_jti,
        )
    except control.ControlRefused as exc:
        # Control's verdict, in control's words — "token revoked", "you are not a
        # member of this organization". Passing the message through keeps the
        # caller-facing wording identical to the single-plane version.
        raise HTTPException(status_code=401, detail=exc.message) from exc
    except control.ControlPlaneError as exc:
        # NOT a 401. The credential may be perfectly good; we could not ask. A
        # 503 says "try again", which is the accepted behaviour when the control
        # plane is down: no new requests, rather than everyone signed out.
        raise HTTPException(
            status_code=503,
            detail="the control plane is unreachable, so this request cannot be authorized",
        ) from exc
    return Context(user=resolved.user, tenant=resolved.tenant)


CtxDep, WriteCtxDep, AdminCtxDep = context_deps(_resolve)


@dataclass(frozen=True)
class ChatAccess:
    """Who is acting on one chat: a workspace member, or a browser holding its token."""

    tenant: Tenant
    # True for a browser's chat token. That caller is the end customer, so it is
    # shown only what they could already see — never tool calls or internal notes.
    via_token: bool


def _chat_access(*, write: bool):
    """Accept a chat token for the chat in the path, else the workspace credential.

    These four routes are the ONLY ones a chat token opens; everywhere else
    ``read_pat`` rejects it, being a different algorithm and different claims.
    """

    async def resolve(chat_id: UUID, request: Request) -> ChatAccess:
        authz = request.headers.get("authorization", "")
        claims = read_chat_token(authz[7:].strip()) if authz.lower().startswith("bearer ") else None
        if claims is None:
            ctx = await _resolve(request)
            if write and ctx.user.role not in (ROLE_ADMIN, ROLE_EDITOR):
                raise HTTPException(
                    status_code=403,
                    detail="your role is view-only — ask an organization admin for editor access",
                )
            return ChatAccess(tenant=ctx.tenant, via_token=False)
        if claims["chat_id"] != str(chat_id):
            raise HTTPException(status_code=403, detail="this token is for a different chat")
        # Cached, unlike a member's context: a chat token names no user whose
        # membership could have been revoked.
        tenant = await load_tenant(claims["tenant_id"])
        if tenant is None:
            raise HTTPException(status_code=401, detail="invalid token")
        return ChatAccess(tenant=tenant, via_token=True)

    # The role it demands of a MEMBER, which is what the published `403` and the
    # CoPilot/MCP dispatcher read. A token caller has no role to check.
    register_role(resolve, "write" if write else "read")
    return Depends(resolve)


ChatReadDep = _chat_access(write=False)
ChatWriteDep = _chat_access(write=True)
