"""Request dependencies: read the credential, and turn it into a ``Context``.

A credential — a session cookie or a personal access token — names one user and
one organization. What it does NOT carry is authority: membership of that
organization is re-read on every request, which is what makes removal take effect
immediately. The JWT is only a signature check, so deleting the ``memberships``
row 401s the very next call with no session revocation machinery. **That is a
promise, and nothing here may start caching without also breaking it.**

*Where* membership is re-read is the one thing that differs between the two
apps, and it is the clearest expression in the codebase of what this deployment
is: control reads ``users ⋈ memberships ⋈ tenants`` from its own database, and a
region calls ``POST /internal/v1/auth-context`` because it has no such database
and must not. Two implementations of "who is this", each with the only access its
host has — see ``api.control.deps`` and ``api.dataplane.deps``, which both build
their dependencies through :func:`context_deps` here.

Reading the credential itself is genuinely shared, and stays shared: verifying an
Ed25519 signature is a local, offline operation on both nodes (``api.core.security``).
"""

from __future__ import annotations

import inspect
import typing
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, NamedTuple

import fastapi.params
from fastapi import Depends, HTTPException, Request

from api.core.security import read_pat, read_session
from services.user import ROLE_ADMIN, ROLE_EDITOR, Context
from settings import get_settings

# Context and roles live in services.user; imported here for HTTP deps.


@dataclass(frozen=True)
class Credential:
    """What a verified session cookie or personal access token asserts.

    ``pat_jti`` is the control-plane row id behind a token, and its presence is
    what says a revocation lookup is still owed: a PAT's signature never expires,
    so the row IS the revocation check. A session carries ``None`` — it expires on
    its own and there is nothing to look up.
    """

    user_id: str
    tenant_id: str
    pat_jti: str | None


def read_credential(request: Request) -> Credential:
    """The credential on this request, verified. Raises 401 if there is not one.

    Signature checking only. Whether this user still belongs to this organization
    — and whether a token has been revoked — is the resolver's job, because only
    one of the two hosts can answer it.
    """
    authz = request.headers.get("authorization", "")
    if authz.lower().startswith("bearer "):
        pat = read_pat(authz[7:].strip())
        if not pat:
            raise HTTPException(status_code=401, detail="invalid token")
        return Credential(user_id=pat["user_id"], tenant_id=pat["tenant_id"], pat_jti=pat["jti"])

    token = request.cookies.get(get_settings().security.session_cookie_name)
    if not token:
        raise HTTPException(status_code=401, detail="not authenticated")
    session = read_session(token)
    if not session:
        raise HTTPException(status_code=401, detail="invalid session")
    return Credential(user_id=session["user_id"], tenant_id=session["tenant_id"], pat_jti=None)


Role = Literal["public", "read", "write", "admin"]

# Every Context dependency either app builds, mapped to the role it demands.
# Populated by `context_deps`, read by `required_role` — so a dependency that
# reached a route without going through the factory is an error rather than a
# silent "read-only", which is how a privileged endpoint would end up exposed.
_ROLE_BY_DEPENDENCY: dict[Any, Role] = {}


class ContextDeps(NamedTuple):
    """The three ``Depends`` values a route picks from, for one app."""

    read: Any
    write: Any
    admin: Any


def context_deps(resolve: Callable[[Request], Awaitable[Context]]) -> ContextDeps:
    """Build one app's ``CtxDep`` / ``WriteCtxDep`` / ``AdminCtxDep``.

    A factory rather than three module-level functions because the write and
    admin guards wrap the *base* resolver, and the two apps do not share one —
    control reads its own database, a region asks control. Everything above the
    resolver is identical, and stays written once.
    """

    async def current_context(request: Request) -> Context:
        return await resolve(request)

    async def current_write_context(ctx: Context = Depends(current_context)) -> Context:
        """Mutating endpoints: org membership alone isn't enough — EDITOR or ADMIN."""
        if ctx.user.role not in (ROLE_ADMIN, ROLE_EDITOR):
            raise HTTPException(
                status_code=403,
                detail="your role is view-only — ask an organization admin for editor access",
            )
        return ctx

    async def current_admin_context(ctx: Context = Depends(current_context)) -> Context:
        if ctx.user.role != ROLE_ADMIN:
            raise HTTPException(status_code=403, detail="only an organization admin can do this")
        return ctx

    _ROLE_BY_DEPENDENCY[current_context] = "read"
    _ROLE_BY_DEPENDENCY[current_write_context] = "write"
    _ROLE_BY_DEPENDENCY[current_admin_context] = "admin"
    return ContextDeps(
        read=Depends(current_context),
        write=Depends(current_write_context),
        admin=Depends(current_admin_context),
    )


def register_role(dependency: Callable[..., Any], role: Role) -> None:
    """Name the role a dependency demands, for one that is not a plain ``Context``.

    The chat routes resolve a ``ChatAccess`` — a workspace member OR a browser
    holding a chat token — so their dependency is built outside ``context_deps``
    and says here which member role it requires.
    """
    _ROLE_BY_DEPENDENCY[dependency] = role


def required_role(endpoint: typing.Callable[..., Any], hints: dict[str, Any] | None = None) -> Role:
    """The organization role a route demands, read back off its auth dependency.

    Routes declare it as ``ctx: Context = CtxDep | WriteCtxDep | AdminCtxDep``,
    and two places read it from here so they cannot disagree with each other or
    with the route: ``api.core.app`` stamps each operation's documented ``403``
    from it, and ``api.dataplane.functions`` enforces the same rule on the
    CoPilot/MCP dispatch path — an operation can never be reached through an AI
    builder with weaker permissions than over HTTP. A route with no ``Context`` at
    all is public, as it is over HTTP.

    ``hints`` is the endpoint's resolved type hints, for callers that already
    have them; an unrecognized dependency is an error rather than a guess,
    because silently treating it as read-only is how a privileged endpoint ends
    up exposed.
    """
    resolved = typing.get_type_hints(endpoint) if hints is None else hints
    for parameter in inspect.signature(endpoint).parameters.values():
        default = parameter.default
        if isinstance(default, fastapi.params.Depends):
            role = _ROLE_BY_DEPENDENCY.get(default.dependency)
            if role is not None:
                return role
        if resolved.get(parameter.name) is not Context:
            continue
        if not isinstance(default, fastapi.params.Depends):
            raise RuntimeError(
                f"route '{endpoint.__name__}' takes a Context that is not a dependency"
            )
        raise RuntimeError(
            f"route '{endpoint.__name__}' resolves its Context through an unknown "
            "dependency — build it with api.core.deps.context_deps before exposing it"
        )
    return "public"
