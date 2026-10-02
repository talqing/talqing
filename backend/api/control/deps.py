"""Who is calling — as control answers it, which is from its own database.

The mirror of ``api.dataplane.deps``, and the pair is the clearest expression in
the codebase of what this deployment is: two implementations of "who is this",
each with the only access its host has.
"""

from __future__ import annotations

from fastapi import HTTPException, Request

from api.control import identity
from api.core.deps import context_deps, read_credential
from services.user import Context

__all__ = ["AdminCtxDep", "Context", "CtxDep", "WriteCtxDep"]


async def _resolve(request: Request) -> Context:
    credential = read_credential(request)
    try:
        user, tenant = await identity.resolve(
            user_id=credential.user_id,
            tenant_id=credential.tenant_id,
            pat_jti=credential.pat_jti,
        )
    except identity.Refused as exc:
        raise HTTPException(status_code=401, detail=exc.message) from exc
    return Context(user=user, tenant=tenant)


CtxDep, WriteCtxDep, AdminCtxDep = context_deps(_resolve)
