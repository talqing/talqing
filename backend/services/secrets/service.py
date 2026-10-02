"""Per-tenant tool credentials.

A secret is referenced from an operation's config via {{secrets.NAME}} and
substituted with the decrypted value inside the worker at runtime. Stored Fernet-
encrypted at rest in the tenant DB; the value is write-only over the API (reads
return a stored masked hint, never the plaintext).

This package owns all access to the ``secrets`` table and all encrypt/decrypt of
those values. Other packages must call these helpers — never query the table or
call utils.crypto for secret values themselves.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel, StringConstraints

import db
from api.core.schemas import OkResponse, Page, mask_tail, page_slice
from services.user import Context, Tenant
from utils.crypto import decrypt, encrypt

# letters/digits/underscore, not starting with a digit (used as {{secrets.NAME}})
SecretName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")]


class CreateSecretRequest(BaseModel):
    name: SecretName
    value: Annotated[str, StringConstraints(min_length=1, max_length=16384)]


class SecretResponse(BaseModel):
    id: UUID
    name: str
    value_hint: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class SecretRef:
    """id + name only — safe for workspace inventory / CoPilot context."""

    id: UUID
    name: str


def _out(row) -> SecretResponse:
    return SecretResponse(
        id=row["id"],
        name=row["name"],
        value_hint=row["value_hint"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


# ── Runtime / internal loaders (decrypted values stay in-process) ────────────


async def load_secrets(tenant: Tenant) -> dict[str, str]:
    """Decrypt the tenant's secrets into ``{name: value}`` for runtime use.

    Used for ``{{secrets.NAME}}`` substitution. In-process only — never log or
    return plaintext over the API.
    """
    pool = await db.tenant_pool(tenant)
    rows = await pool.fetch(
        "SELECT name, value_encrypted FROM secrets WHERE tenant_id = $1",
        tenant.id,
    )
    return {row["name"]: decrypt(row["value_encrypted"]) for row in rows}


async def list_secret_names(tenant: Tenant) -> set[str]:
    """Secret names that exist (for validation). Does not decrypt values."""
    pool = await db.tenant_pool(tenant)
    rows = await pool.fetch(
        "SELECT name FROM secrets WHERE tenant_id = $1",
        tenant.id,
    )
    return {row["name"] for row in rows}


async def list_secret_refs(tenant: Tenant) -> list[SecretRef]:
    """id + name inventory for CoPilot / workspace. No values or hints."""
    pool = await db.tenant_pool(tenant)
    rows = await pool.fetch(
        "SELECT id, name FROM secrets WHERE tenant_id = $1 ORDER BY name",
        tenant.id,
    )
    return [SecretRef(id=row["id"], name=row["name"]) for row in rows]


# ── HTTP / CoPilot CRUD (write-only values; reads return hints only) ─────────


async def list_secrets(
    ctx: Context,
    limit: int = 200,
    offset: int = 0,
) -> Page[SecretResponse]:
    pool = await ctx.tenant_pool()
    rows = await pool.fetch(
        "SELECT id, name, value_hint, created_at, updated_at, tenant_id "
        "FROM secrets WHERE tenant_id = $1 ORDER BY name "
        "LIMIT $2 OFFSET $3",
        ctx.tenant.id,
        limit + 1,
        offset,
    )
    return page_slice([_out(r) for r in rows], limit=limit, offset=offset)


async def create_secret(body: CreateSecretRequest, ctx: Context):
    """Create or replace a secret by name (upsert). Same name rotates the value."""
    pool = await ctx.tenant_pool()
    hint = mask_tail(body.value)
    row = await pool.fetchrow(
        """
        INSERT INTO secrets (tenant_id, name, value_encrypted, value_hint, created_by)
        VALUES ($3, $1, $2, $4, $5)
        -- created_by is deliberately not updated: rotating a value keeps the
        -- attribution of whoever put the secret there.
        ON CONFLICT (tenant_id, name) DO UPDATE SET
            value_encrypted = EXCLUDED.value_encrypted,
            value_hint = EXCLUDED.value_hint,
            updated_at = now()
        RETURNING id, name, value_hint, created_at, updated_at, tenant_id
        """,
        body.name,
        encrypt(body.value),
        ctx.tenant.id,
        hint,
        ctx.user.id,
    )
    return _out(row)


async def _secret_users(ctx: Context, name: str) -> list[str]:
    """Everything that would stop working without secret ``name``, as labels.

    The tools read is every tree a call can run now or after the next agent
    publish: each tool's draft and published version, plus any older version a
    live agent or task still pins (directly, or through a task version a live
    agent pins). Older versions nobody runs are allowed to dangle, exactly as
    `delete_tool` lets them — blocking on those would make a secret undeletable
    for ever after one publish.
    """
    # Local import: `services.tools` imports this package, so a module-scope
    # import here would be a cycle. `collect_secret_names` is the scan the
    # runtime refuses a tree by, so the guard and the failure cannot disagree.
    from services.tools.validate import collect_secret_names

    pool = await ctx.tenant_pool()
    trees = await pool.fetch(
        """
        WITH live AS (
            SELECT 'agent' AS owner, a.name, av.config
            FROM agents a
            JOIN agent_versions av
                ON av.agent_id = a.id AND av.tenant_id = a.tenant_id
                AND av.version = a.published_version
            WHERE a.tenant_id = $1
            UNION ALL
            SELECT 'task', t.name, tv.config
            FROM agent_tasks t
            JOIN agent_task_versions tv
                ON tv.task_id = t.id AND tv.tenant_id = t.tenant_id
                AND tv.version = t.published_version
            WHERE t.tenant_id = $1
            UNION ALL
            -- a task version a live agent pins, which need not be the task's own
            SELECT 'agent', a.name, tv.config
            FROM agents a
            JOIN agent_versions av
                ON av.agent_id = a.id AND av.tenant_id = a.tenant_id
                AND av.version = a.published_version
            CROSS JOIN LATERAL jsonb_array_elements(av.config -> 'tasks') AS sel
            JOIN agent_task_versions tv
                ON tv.task_id = (sel ->> 'task_id')::uuid AND tv.tenant_id = a.tenant_id
                AND tv.version = (sel ->> 'task_version')::int
            WHERE a.tenant_id = $1
        )
        SELECT t.name AS tool, NULL::text AS pinned_by, tree.operations
        FROM tools t
        LEFT JOIN tool_versions tv
            ON tv.tool_id = t.id AND tv.tenant_id = t.tenant_id
            AND tv.version = t.published_version
        CROSS JOIN LATERAL
            (VALUES (t.operations), (tv.definition -> 'operations')) AS tree(operations)
        WHERE t.tenant_id = $1
        UNION ALL
        SELECT t.name, live.owner || ' ''' || live.name || '''', tv.definition -> 'operations'
        FROM live
        CROSS JOIN LATERAL jsonb_path_query(
            live.config, 'strict $.** ? (exists(@.tool_id) && exists(@.tool_version))'
        ) AS pin
        JOIN tools t ON t.id = (pin ->> 'tool_id')::uuid AND t.tenant_id = $1
        JOIN tool_versions tv
            ON tv.tool_id = t.id AND tv.tenant_id = t.tenant_id
            AND tv.version = (pin ->> 'tool_version')::int
            AND tv.version <> t.published_version
        """,
        ctx.tenant.id,
    )
    users: list[str] = []
    for r in trees:
        if name not in collect_secret_names(r["operations"]):
            continue
        tool = f"tool '{r['tool']}'"
        if r["pinned_by"] is None:
            label = tool
        elif tool in users:
            continue
        else:
            label = (
                f"an older version of {tool} that {r['pinned_by']} runs live "
                f"(republish {r['pinned_by']})"
            )
        if label not in users:
            users.append(label)

    # Integration and carrier-account credentials are stored as refs only
    # (`materialize_secret_input`), so a text match on the token is exact. A name
    # is identifier-shaped, so it needs no escaping inside the pattern.
    pattern = r"\{\{\s*secrets\." + name + r"\s*\}\}"
    rows = await pool.fetch(
        """
        SELECT 'integration' AS kind, display_name AS name FROM integrations
        WHERE tenant_id = $1 AND (
            credentials_ref ~ $2 OR mcp_config::text ~ $2 OR webhook_config::text ~ $2
        )
        UNION ALL
        SELECT 'carrier account', display_name FROM telephony_accounts
        WHERE tenant_id = $1 AND credentials::text ~ $2
        ORDER BY 1, 2
        """,
        ctx.tenant.id,
        pattern,
    )
    users.extend(f"{r['kind']} '{r['name']}'" for r in rows)
    return users


async def delete_secret(secret_id: UUID, ctx: Context):
    pool = await ctx.tenant_pool()
    name = await pool.fetchval(
        "SELECT name FROM secrets WHERE id = $1 AND tenant_id = $2",
        secret_id,
        ctx.tenant.id,
    )
    if name is None:
        raise HTTPException(status_code=404, detail="secret not found")
    # A deleted secret fails every call that reaches for it, so refuse while
    # anything live still does, and name it — the same rule `delete_tool` keeps.
    # Rotating a value is an upsert on the name and never needs this.
    if users := await _secret_users(ctx, name):
        raise HTTPException(
            status_code=409,
            detail=f"secret '{name}' is used by {', '.join(users)} — "
            "remove it there before deleting it",
        )
    await pool.execute(
        "DELETE FROM secrets WHERE id = $1 AND tenant_id = $2",
        secret_id,
        ctx.tenant.id,
    )
    return OkResponse()
