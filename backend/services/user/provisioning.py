"""Provisioning what a new organization and a new member each need to exist.

**Control plane only.** Identity is control's domain: these write ``tenants``,
``memberships`` and ``personal_access_tokens``, and a region has no connection
that could reach any of them.

An organization is not provisioned *into* a region. Every organization exists in
every region — there is no enablement step, no per-tenant table and nothing to
switch on — and its data-plane rows are created lazily the first time it does
something there. ``home_region`` records where it was created, which is the
default the dashboard opens in and nothing more.

`create_organization` and `add_member` take a connection rather than reaching
for the pool themselves, because their callers (first login, POST /v1/orgs,
accepting an invite) need them inside a transaction with the surrounding writes
— a half-provisioned signup would leave a user with no membership, which the
login policy treats as impossible.
"""

from __future__ import annotations

import logging
from uuid import UUID

import asyncpg

import db

from .context import ROLE_ADMIN
from .models import Tenant

logger = logging.getLogger("talqing.services.user.provisioning")

# Shown in the workspace token list, so it should read as an explanation of why
# a token nobody created is there.
MCP_TOKEN_NAME = "Dashboard MCP token"

# What a tenant row hands out, listed rather than `SELECT *` so a column added to
# the table later has to be added here on purpose.
TENANT_COLUMNS = "id, name, retention_days, created_at"


async def create_organization(
    conn: asyncpg.Connection,
    *,
    name: str,
    owner_id: UUID,
    home_region: str,
) -> Tenant:
    """Create an organization with `owner_id` as its ADMIN.

    An organization with no admin cannot be administered and no endpoint can
    repair it, so the founding membership is written here rather than left to
    the caller.

    `home_region` is a slug from control's own region list, resolved by the
    caller — from the visitor's country at signup, or from the creator's current
    region. It decides which region the dashboard opens in and nothing else.
    """
    row = await conn.fetchrow(
        f"""
        INSERT INTO tenants (name, home_region)
        VALUES ($1, $2)
        RETURNING {TENANT_COLUMNS}
        """,
        name,
        home_region,
    )
    tenant = Tenant.model_validate(dict(row))
    await add_member(conn, user_id=owner_id, tenant_id=tenant.id, role=ROLE_ADMIN)
    logger.info(
        "created organization %s (%s) in %s with admin %s", tenant.id, name, home_region, owner_id
    )
    return tenant


async def add_member(
    conn: asyncpg.Connection,
    *,
    user_id: UUID,
    tenant_id: UUID,
    role: str,
    invited_by: UUID | None = None,
) -> None:
    """Give a user a role in an organization. Re-adding an existing member is a
    no-op rather than an error — `POST /v1/org/invites` reports that case itself,
    with the member's current role."""
    await conn.execute(
        """
        INSERT INTO memberships (user_id, tenant_id, role, invited_by)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (user_id, tenant_id) DO NOTHING
        """,
        user_id,
        tenant_id,
        role,
        invited_by,
    )


async def provision_mcp_token(user_id: UUID, tenant_id: UUID) -> UUID:
    """The id of this user's MCP token for one organization, creating it if they
    have none there.

    Every member gets one so the dashboard can hand them a working MCP
    configuration on sight, rather than making them understand access tokens
    before they can connect a coding agent. It is per organization because a PAT
    names one: a member who switches organizations must be shown a token that
    reaches the organization they are looking at.

    Idempotent, and safe to call on a member who joined earlier: one 'mcp' token
    per user per organization is a partial unique index, so a losing insert reads
    the winner's row instead of failing.
    """
    pool = await db.control_pool()
    row = await pool.fetchrow(
        """
        INSERT INTO personal_access_tokens (user_id, tenant_id, name, kind)
        VALUES ($1, $2, $3, 'mcp')
        ON CONFLICT DO NOTHING
        RETURNING id
        """,
        user_id,
        tenant_id,
        MCP_TOKEN_NAME,
    )
    if row:
        logger.info("issued MCP token for user %s", user_id)
        return row["id"]
    # The insert conflicted, so the existing row is committed and visible.
    token_id = await pool.fetchval(
        "SELECT id FROM personal_access_tokens "
        "WHERE user_id = $1 AND tenant_id = $2 AND kind = 'mcp'",
        user_id,
        tenant_id,
    )
    if token_id is None:
        raise RuntimeError(f"MCP token for user {user_id} neither inserted nor found")
    return token_id


# `deprovision_tenant` used to live here: it deleted a tenant's data-plane rows
# and then its control row. Nothing called it, and with the planes split it could
# not work anyway — control has no data-plane connection, and one region deleting
# a tenant's rows would leave every other region's untouched. Deleting an
# organization for real is a fan-out to every region, and it wants designing when
# there is an endpoint that needs it.
