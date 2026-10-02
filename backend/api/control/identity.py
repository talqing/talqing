"""Resolving a credential against the control-plane database.

One query, and it is deliberately one: this is what a region pays a round trip
for, so returning the user, their role and the organization separately would pay
that round trip twice on every request in the deployment. The single-plane
version ran a ``users ⋈ memberships`` join *and* a tenant lookup; joining the
third table costs nothing here and saves ~150 ms there.

Two readers, so they cannot disagree about what "authorized" means:
``api.control.deps`` for control's own routes, and
``api.control.routes.internal`` for every region's.
"""

from __future__ import annotations

import asyncpg

import db
from services.user import TENANT_COLUMNS, Tenant, User


class Refused(Exception):
    """This credential does not authorize anything. Final, and caller-facing.

    The message is written for whoever holds the credential, because it is
    handed straight back as a 401 body — by control to its own callers, and by a
    region after it forwards this verdict.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


async def resolve(
    *, user_id: str, tenant_id: str, pat_jti: str | None = None
) -> tuple[User, Tenant]:
    """The user, their role in this organization, and the organization.

    **The membership join is the authorization check**: no row, no access. The
    role comes off the membership, so the same person can be an ADMIN here and a
    VIEWER in the organization they switch to next. Nothing is cached anywhere,
    which is what makes removal take effect on the member's very next request.
    """
    pool = await db.control_pool()
    if pat_jti:
        await _assert_token_live(pool, pat_jti=pat_jti, user_id=user_id, tenant_id=tenant_id)
    try:
        row = await pool.fetchrow(
            """
            SELECT u.id, u.email, u.name, u.picture_url, u.google_sub, u.created_at, m.role,
                   t.id AS tenant_id, t.name AS tenant_name, t.retention_days,
                   t.created_at AS tenant_created_at
            FROM users u
            JOIN memberships m ON m.user_id = u.id AND m.tenant_id = $2
            JOIN tenants t     ON t.id = m.tenant_id
            WHERE u.id = $1
            """,
            user_id,
            tenant_id,
        )
    except (ValueError, asyncpg.DataError):
        # A credential whose ids are not UUIDs at all: forged, or from a
        # deployment this is not.
        raise Refused("invalid credential") from None
    if not row:
        raise Refused("you are not a member of this organization")
    return (
        User.model_validate(dict(row)),
        Tenant(
            id=row["tenant_id"],
            name=row["tenant_name"],
            retention_days=row["retention_days"],
            created_at=row["tenant_created_at"],
        ),
    )


async def _assert_token_live(
    pool: asyncpg.Pool, *, pat_jti: str, user_id: str, tenant_id: str
) -> None:
    """A personal access token's JWT never expires, so the DB row IS the
    revocation check — no row, no access."""
    try:
        row = await pool.fetchrow(
            "SELECT tenant_id FROM personal_access_tokens WHERE id = $1 AND user_id = $2",
            pat_jti,
            user_id,
        )
    except (ValueError, asyncpg.DataError):
        raise Refused("invalid token") from None
    if not row:
        raise Refused("token revoked")
    if str(row["tenant_id"]) != str(tenant_id):
        raise Refused("invalid token")


async def load_tenant(tenant_id: str) -> Tenant | None:
    """One organization, for the region paths that have a tenant and no user.

    Session finalize, a queued job and an inbound webhook all name a tenant that
    may have been deleted since, so ``None`` is a real answer.
    """
    pool = await db.control_pool()
    try:
        row = await pool.fetchrow(f"SELECT {TENANT_COLUMNS} FROM tenants WHERE id = $1", tenant_id)
    except (ValueError, asyncpg.DataError):
        return None
    return Tenant.model_validate(dict(row)) if row else None
