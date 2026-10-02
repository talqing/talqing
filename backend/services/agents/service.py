"""Agent draft CRUD + publish. Reads/writes the tenant data DB.

Request/response shapes: ``services.agents.models``.
Draft validation: ``services.agents.validate``.

The agent definition is the typed `AgentConfig` model (validated on the way in,
serialized to JSONB on the way out). `name` and `channel` are also first-class
columns (kept in sync with config) for list/filter uniqueness.

Custom tools are pinned by publish: each `ToolSelection` in the frozen config
gains the tool version that was live at that moment (``services.agents.pin``).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any
from uuid import UUID

import asyncpg
from fastapi import BackgroundTasks, HTTPException
from pydantic import ValidationError

from api.core.schemas import (
    ErrorBody,
    OkResponse,
    Page,
    ValidateResponse,
    field_errors,
    validation_error,
)
from services import webhooks
from services.control import client as control
from services.integrations import agent_channel_for_trigger
from services.user import Context
from services.webhooks import events

from .materialize import compile_inline_code, has_inline_definitions, materialize_inline
from .models import (
    AgentConfig,
    AgentResponse,
    AgentVersionDetailResponse,
    AgentVersionResponse,
    CreateAgentRequest,
    PublishAgentResponse,
    missing_required_vars,
)
from .override import UpdateAgentRequest, deep_merge, drop_superseded_efforts, override_dump
from .pin import (
    pin_default_voices,
    pin_reasoning_efforts,
    pin_task_selections,
    pin_tool_selections,
    unpin_task_selections,
    unpin_tool_selections,
)
from .validate import validate_agent_draft

# Columns needed for AgentResponse + draft validation. The publish time of the
# live version comes along so a caller can tell an agent whose draft has moved on
# from one whose draft is what calls are running, without fetching every agent's
# history one at a time.
_AGENT_ROW = (
    "id, name, channel, config, published_version, created_at, updated_at, created_by, "
    "tenant_id, "
    "(SELECT v.published_at FROM agent_versions v "
    " WHERE v.agent_id = agents.id AND v.tenant_id = agents.tenant_id "
    "   AND v.version = agents.published_version) AS published_at"
)


def _config_of(row: Mapping[str, Any]) -> AgentConfig:
    """Parse the JSONB ``config`` column from an agents / agent_versions row."""
    return AgentConfig.model_validate(row["config"])


def _agent_out(
    row: Mapping[str, Any],
    versions: list[AgentVersionResponse] | None = None,
) -> AgentResponse:
    return AgentResponse(
        id=row["id"],
        config=_config_of(row),
        published_version=row["published_version"],
        published_at=row["published_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        created_by=row["created_by"],
        versions=versions,
    )


async def _require_agent(
    ctx: Context,
    agent_id: UUID,
    *,
    conn: asyncpg.Connection | None = None,
    for_update: bool = False,
) -> asyncpg.Record:
    """Load an agent row for this tenant, or raise 404.

    Pass ``conn`` (and optionally ``for_update=True``) when the caller already
    holds a transaction that must lock the row.
    """
    sql = f"SELECT {_AGENT_ROW} FROM agents WHERE id = $1 AND tenant_id = $2"
    if for_update:
        sql += " FOR UPDATE"
    executor = conn if conn is not None else await ctx.tenant_pool()
    row = await executor.fetchrow(sql, agent_id, ctx.tenant.id)
    if not row:
        raise HTTPException(status_code=404, detail="agent not found")
    return row


async def list_agents(
    ctx: Context,
    limit: int = 200,
    offset: int = 0,
) -> Page[AgentResponse]:
    pool = await ctx.tenant_pool()
    rows = await pool.fetch(
        f"SELECT {_AGENT_ROW} "
        "FROM agents WHERE tenant_id = $3 ORDER BY updated_at DESC LIMIT $1 OFFSET $2",
        limit + 1,
        offset,
        ctx.tenant.id,
    )
    has_more = len(rows) > limit
    items = [_agent_out(r) for r in rows[:limit]]
    return Page[AgentResponse](items=items, has_more=has_more, limit=limit, offset=offset)


async def create_agent(
    body: CreateAgentRequest,
    bg: BackgroundTasks,
    ctx: Context,
) -> AgentResponse:
    config = pin_default_voices(body.config)
    pool = await ctx.tenant_pool()
    result = await validate_agent_draft(ctx, config)
    if result.errors:
        raise validation_error(result.errors, "this agent config has problems")
    inline = has_inline_definitions(config)
    if inline:
        await compile_inline_code(config)
    async with pool.acquire() as conn:
        async with conn.transaction():
            if inline:
                # In the write's own transaction: a config that then fails to
                # store must leave no orphan tool behind.
                config = await materialize_inline(conn, ctx, config)
            try:
                row = await conn.fetchrow(
                    # Not RETURNING *: _agent_out reads the live version's
                    # publish time, which is a subquery rather than a column on
                    # this table.
                    f"""
                    INSERT INTO agents (tenant_id, name, channel, config, created_by)
                    VALUES ($1, $2, $3, $4::jsonb, $5)
                    RETURNING {_AGENT_ROW}
                    """,
                    ctx.tenant.id,
                    config.name,
                    config.channel,
                    config.model_dump_json(),
                    ctx.user.id,
                )
            except asyncpg.UniqueViolationError:
                raise HTTPException(
                    status_code=409, detail="an agent with that name already exists"
                )
    agent_id = str(row["id"])
    bg.add_task(
        webhooks.dispatch,
        ctx.tenant,
        events.AGENT_CREATED,
        agent_id,
        {"agent_id": agent_id, "name": config.name},
    )
    return _agent_out(row)


async def get_agent(agent_id: UUID, ctx: Context) -> AgentResponse:
    row = await _require_agent(ctx, agent_id)
    return _agent_out(row, await _version_list(agent_id, ctx))


async def update_agent(
    agent_id: UUID,
    body: UpdateAgentRequest,
    bg: BackgroundTasks,
    ctx: Context,
) -> AgentResponse:
    row = await _require_agent(ctx, agent_id)
    patch = override_dump(body.config)
    merged = deep_merge(_config_of(row).model_dump(mode="json"), patch)
    drop_superseded_efforts(merged, patch)
    try:
        config = pin_default_voices(AgentConfig.model_validate(merged))
    except ValidationError as exc:
        # Located under `config.`, as the request validator would have put them.
        errors = [{**error, "loc": ("config", *error["loc"])} for error in exc.errors()]
        raise validation_error(field_errors(errors), "this agent config has problems") from exc
    result = await validate_agent_draft(ctx, config)
    if result.errors:
        raise validation_error(result.errors, "this agent config has problems")
    pool = await ctx.tenant_pool()
    inline = has_inline_definitions(config)
    if inline:
        await compile_inline_code(config)
    async with pool.acquire() as conn:
        async with conn.transaction():
            if inline:
                config = await materialize_inline(conn, ctx, config)
            try:
                updated = await conn.fetchrow(
                    f"""
                    UPDATE agents
                    SET name = $2, channel = $3, config = $4::jsonb, updated_at = now()
                    WHERE id = $1 AND tenant_id = $5
                    RETURNING {_AGENT_ROW}
                    """,
                    agent_id,
                    config.name,
                    config.channel,
                    config.model_dump_json(),
                    ctx.tenant.id,
                )
            except asyncpg.UniqueViolationError:
                raise HTTPException(
                    status_code=409, detail="an agent with that name already exists"
                )
            if updated is None:
                # deleted while this update was being validated
                raise HTTPException(status_code=404, detail="agent not found")
    bg.add_task(
        webhooks.dispatch,
        ctx.tenant,
        events.AGENT_UPDATED,
        str(agent_id),
        {"agent_id": str(agent_id), "name": config.name},
    )
    return _agent_out(updated)


async def delete_agent(
    agent_id: UUID,
    bg: BackgroundTasks,
    ctx: Context,
) -> OkResponse:
    pool = await ctx.tenant_pool()
    # Refuse while anything would go on dispatching to this agent, and name it.
    #
    # A phone number: its LiveKit dispatch rule carries this agent's id, and
    # deleting the agent does not touch the rule, so every inbound call would be
    # answered by nothing. A disabled number has no rule, so it is not a blocker:
    # its assignment is cleared below and re-enabling it asks for an agent.
    #
    # A stream connection: its URL is a partner's live integration, and every
    # call on it would be refused. A disabled one is deleted with the agent, as
    # its URL embeds the agent id and could never answer again.
    #
    # An enabled integration trigger: every inbound message would arrive and go
    # unanswered while the trigger still reads `active`. A disabled one loses its
    # agent to the FK, and enabling it again asks for one.
    #
    # A call batch that is not over: it would fail itself on its next pass, which
    # is a campaign killed by a delete that could have said "cancel it first".
    # The entry agent is `agent_id`; any other member of its team is in the plan.
    #
    # Another agent handing off here, in its draft or its live version, the way
    # `delete_tool` checks its owners: the handoff would fail mid-call. A tool's
    # `handoff` operation is the same reference one level down, so it is checked
    # in the tool's draft and live trees, and in any older version a live agent
    # still pins — that is the tree its calls actually run.
    #
    # The number and stream FKs are RESTRICT, so no other delete path can skip
    # those two — and so the disabled rows, which still reference the agent, are
    # cleared before the delete.
    numbers = await pool.fetch(
        "SELECT e164 FROM phone_numbers "
        "WHERE inbound_agent_id = $1 AND tenant_id = $2 AND status <> 'disabled' ORDER BY e164",
        agent_id,
        ctx.tenant.id,
    )
    streams = await pool.fetch(
        "SELECT name FROM stream_connections "
        "WHERE agent_id = $1 AND tenant_id = $2 AND status <> 'disabled' ORDER BY name",
        agent_id,
        ctx.tenant.id,
    )
    handoff_sources = await pool.fetch(
        """
        SELECT DISTINCT a.name
        FROM agents a
        LEFT JOIN agent_versions av
            ON av.agent_id = a.id AND av.tenant_id = a.tenant_id
            AND av.version = a.published_version
        WHERE a.tenant_id = $1 AND a.id <> $2
            AND (a.config @> $3::jsonb OR av.config @> $3::jsonb)
        ORDER BY a.name
        """,
        ctx.tenant.id,
        agent_id,
        json.dumps({"handoffs": [{"agent_id": str(agent_id)}]}),
    )
    triggers = await pool.fetch(
        """
        SELECT DISTINCT i.display_name
        FROM integration_triggers t
        JOIN integrations i ON i.id = t.integration_id AND i.tenant_id = t.tenant_id
        WHERE t.agent_id = $1 AND t.tenant_id = $2 AND t.enabled
        ORDER BY i.display_name
        """,
        agent_id,
        ctx.tenant.id,
    )
    batches = await pool.fetch(
        """
        SELECT name FROM call_batches
        WHERE tenant_id = $1 AND status IN ('scheduled', 'running', 'paused')
            AND (agent_id = $2 OR agent_plan -> 'members' @> $3::jsonb)
        ORDER BY name
        """,
        ctx.tenant.id,
        agent_id,
        json.dumps([{"agent_id": str(agent_id)}]),
    )
    tool_handoffs = await pool.fetch(
        """
        WITH trees AS (
            SELECT t.name AS tool, NULL::text AS pinned_by, tree.operations
            FROM tools t
            LEFT JOIN tool_versions tv
                ON tv.tool_id = t.id AND tv.tenant_id = t.tenant_id
                AND tv.version = t.published_version
            CROSS JOIN LATERAL
                (VALUES (t.operations), (tv.definition -> 'operations')) AS tree(operations)
            WHERE t.tenant_id = $1
            UNION ALL
            -- every tool pin in a live agent version, tools and hooks alike
            SELECT t.name, a.name, tv.definition -> 'operations'
            FROM agents a
            JOIN agent_versions av
                ON av.agent_id = a.id AND av.tenant_id = a.tenant_id
                AND av.version = a.published_version
            CROSS JOIN LATERAL jsonb_path_query(
                av.config, 'strict $.** ? (exists(@.tool_id) && exists(@.tool_version))'
            ) AS pin
            JOIN tools t ON t.id = (pin ->> 'tool_id')::uuid AND t.tenant_id = a.tenant_id
            JOIN tool_versions tv
                ON tv.tool_id = t.id AND tv.tenant_id = t.tenant_id
                AND tv.version = (pin ->> 'tool_version')::int
                AND tv.version <> t.published_version
            WHERE a.tenant_id = $1 AND a.id <> $2
        )
        SELECT DISTINCT tool, pinned_by FROM trees
        WHERE jsonb_path_exists(
            operations,
            'strict $.** ? (@.kind == "handoff" && @.config.target_agent_id == $agent_id)',
            jsonb_build_object('agent_id', $2::text)
        )
        ORDER BY tool, pinned_by NULLS FIRST
        """,
        ctx.tenant.id,
        agent_id,
    )
    in_use = (
        [f"phone number {r['e164']}" for r in numbers]
        + [f"stream connection '{r['name']}'" for r in streams]
        + [f"trigger on integration '{r['display_name']}'" for r in triggers]
        + [f"call batch '{r['name']}'" for r in batches]
        + [f"handoff from agent '{r['name']}'" for r in handoff_sources]
        + [
            f"handoff from tool '{r['tool']}'"
            if r["pinned_by"] is None
            else f"handoff from an older version of tool '{r['tool']}' that agent "
            f"'{r['pinned_by']}' runs live; republish '{r['pinned_by']}'"
            for r in tool_handoffs
        ]
    )
    if in_use:
        raise HTTPException(
            status_code=409,
            detail=ErrorBody(
                message="this agent is still in use; repoint, disable or cancel these first",
                errors=in_use,
            ).model_dump(),
        )
    try:
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute(
                """
                UPDATE phone_numbers SET inbound_agent_id = NULL, updated_at = now()
                WHERE inbound_agent_id = $1 AND tenant_id = $2 AND status = 'disabled'
                """,
                agent_id,
                ctx.tenant.id,
            )
            await conn.execute(
                """
                DELETE FROM stream_connections
                WHERE agent_id = $1 AND tenant_id = $2 AND status = 'disabled'
                """,
                agent_id,
                ctx.tenant.id,
            )
            row = await conn.fetchrow(
                "DELETE FROM agents WHERE id = $1 AND tenant_id = $2 RETURNING id, name",
                agent_id,
                ctx.tenant.id,
            )
    except asyncpg.ForeignKeyViolationError:
        # a number or stream connection was pointed here between check and delete
        raise HTTPException(
            status_code=409, detail="this agent was just assigned somewhere; try again"
        )
    if not row:
        raise HTTPException(status_code=404, detail="agent not found")
    bg.add_task(
        webhooks.dispatch,
        ctx.tenant,
        events.AGENT_DELETED,
        None,
        {"agent_id": str(agent_id), "name": row["name"]},
    )
    return OkResponse()


async def _answering_errors(
    executor: asyncpg.Connection | asyncpg.Pool,
    *,
    tenant_id: UUID,
    agent_id: UUID,
    cfg: AgentConfig,
    rolling_back_to: int | None = None,
) -> list[str]:
    """Why ``cfg`` cannot become what runs behind this agent's live wiring.

    A phone number, a stream connection, an enabled integration trigger and a
    call batch that follows the published version all run whatever this agent
    publishes, and each checked the agent only when it was pointed here: a
    number and a stream need a `voice` agent, a trigger the channel its event
    arrives on, and an inbound call — carrying no request of ours — a config
    whose `required` variables all have defaults, as a batch needs them covered
    by the bag it stored at create. Three doors reach each of those states:
    pointing the thing here, publishing a config under it, and rolling back to a
    version that breaks it. All three refuse; miss any one and the state is
    reachable anyway, discovered as a refused call on a customer's line.

    Matches numbers and stream connections whatever their status, NOT only the
    ones routing today: a disabled number keeps its `inbound_agent_id` and a
    disabled connection its `agent_id`, and re-enabling either does not
    re-validate the agent, so narrowing this would leave disable → publish →
    re-enable as an open door. Do not tighten it. A disabled trigger does
    re-validate on enable, and a batch pinned to a version by its `agent_plan`
    never runs what is published, so neither is matched.

    ``rolling_back_to`` names the version being restored, which is a frozen
    config nobody can edit — so the way out it offers is a different version
    rather than a different setting.
    """
    rows = await executor.fetch(
        """
        SELECT 'number' AS kind, e164 AS label, NULL::jsonb AS vars, NULL AS trigger_type
        FROM phone_numbers WHERE inbound_agent_id = $1 AND tenant_id = $2
        UNION ALL
        SELECT 'stream', name, NULL, NULL
        FROM stream_connections WHERE agent_id = $1 AND tenant_id = $2
        UNION ALL
        SELECT 'batch', name, vars, NULL
        FROM call_batches
        WHERE agent_id = $1 AND tenant_id = $2 AND agent_plan IS NULL
            AND status IN ('scheduled', 'running', 'paused')
        UNION ALL
        SELECT 'trigger', i.display_name, NULL, t.trigger_type
        FROM integration_triggers t
        JOIN integrations i ON i.id = t.integration_id AND i.tenant_id = t.tenant_id
        WHERE t.agent_id = $1 AND t.tenant_id = $2 AND t.enabled
        ORDER BY kind, label
        """,
        agent_id,
        tenant_id,
    )
    subject = "this agent" if rolling_back_to is None else f"version {rolling_back_to}"
    errors: list[str] = []

    def refuse_channel(needs: str, who_needs: str, undo: str) -> None:
        if cfg.channel == needs:
            return
        redo = (
            f"switch the channel back to {needs}"
            if rolling_back_to is None
            else f"roll back to a {needs} version"
        )
        errors.append(
            f"{who_needs} a {needs} agent and {subject} is {cfg.channel} - {redo}, or {undo}"
        )

    numbers = [r["label"] for r in rows if r["kind"] == "number"]
    if numbers:
        where = ", ".join(numbers)
        refuse_channel("voice", f"inbound calls on {where} need", "unassign the number")
        for name in missing_required_vars(cfg.vars, {}):
            errors.append(
                f"'{name}' is required and has no default, and this agent answers inbound "
                f"calls on {where} - which carry no request to supply it. Give the variable a "
                f"default, make it optional, or unassign the number"
                if rolling_back_to is None
                else f"version {rolling_back_to} declares '{name}' as required with no "
                f"default, and this agent answers inbound calls on {where} - which carry no "
                f"request to supply it. Roll back to a version that gives it a default, or "
                f"unassign the number"
            )
    for row in rows:
        label = row["label"]
        if row["kind"] == "stream":
            refuse_channel(
                "voice",
                f"stream connection '{label}' needs",
                "point the connection at another agent",
            )
        elif row["kind"] == "batch":
            refuse_channel("voice", f"call batch '{label}' needs", "cancel the batch")
            for name in missing_required_vars(cfg.vars, row["vars"] or {}):
                errors.append(
                    f"{subject} requires '{name}' with no default, and call batch '{label}' "
                    f"supplies no value for it - "
                    + (
                        "give the variable a default, make it optional"
                        if rolling_back_to is None
                        else "roll back to another version"
                    )
                    + ", or cancel the batch"
                )
        elif row["kind"] == "trigger":
            needs = agent_channel_for_trigger(row["trigger_type"])
            # an unimplemented trigger type cannot be enabled in the first place
            assert needs is not None, row["trigger_type"]
            refuse_channel(
                needs,
                f"the trigger on integration '{label}' needs",
                "point the trigger at another agent",
            )
            if row["trigger_type"] != "whatsapp.call.inbound":
                continue
            # The two rules the trigger checked on enable
            # (`integrations.triggers._validate_trigger`), for the same reasons.
            fix = "change it back" if rolling_back_to is None else "roll back to another version"
            if cfg.conversation.context != "transcript":
                errors.append(
                    f"{subject} answers WhatsApp calls on '{label}', which must keep past "
                    f"conversations as the full transcript so the call and the chat stay one "
                    f"thread - {fix}, or point the trigger at another agent"
                )
            for name in missing_required_vars(cfg.vars, {}):
                errors.append(
                    f"'{name}' is required and has no default, and {subject} answers WhatsApp "
                    f"calls on '{label}', which carry no request to supply it - "
                    + (
                        "give the variable a default, make it optional"
                        if rolling_back_to is None
                        else "roll back to another version"
                    )
                    + ", or point the trigger at another agent"
                )
    return errors


async def validate_agent(agent_id: UUID, ctx: Context) -> ValidateResponse:
    row = await _require_agent(ctx, agent_id)
    cfg = _config_of(row)
    result = await validate_agent_draft(ctx, cfg, for_publish=True)
    # Here as well as in `publish_agent`, and not as belt-and-braces: the editor
    # publishes as save -> validate -> show the errors modal -> publish, so a
    # guard that lived only in `publish_agent` would give a clean pre-flight
    # followed by a bare "Could not publish the agent". The guard belongs where
    # the sentence is rendered.
    errors = list(result.errors)
    errors.extend(
        await _answering_errors(
            await ctx.tenant_pool(), tenant_id=ctx.tenant.id, agent_id=agent_id, cfg=cfg
        )
    )
    return ValidateResponse(errors=errors, warnings=result.warnings)


async def publish_agent(
    agent_id: UUID,
    bg: BackgroundTasks,
    ctx: Context,
) -> PublishAgentResponse:
    pool = await ctx.tenant_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            agent = await _require_agent(ctx, agent_id, conn=conn, for_update=True)
            next_version = await conn.fetchval(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM agent_versions WHERE agent_id = $1 AND tenant_id = $2",
                agent_id,
                ctx.tenant.id,
            )
            # freeze the validated config as the immutable snapshot
            cfg = _config_of(agent)
            result = await validate_agent_draft(ctx, cfg, for_publish=True)
            errors = list(result.errors)
            errors.extend(
                await _answering_errors(conn, tenant_id=ctx.tenant.id, agent_id=agent_id, cfg=cfg)
            )
            if errors:
                raise validation_error(errors, "this agent has validation errors")
            frozen = pin_reasoning_efforts(
                await pin_task_selections(
                    conn, ctx.tenant.id, await pin_tool_selections(conn, ctx.tenant.id, cfg)
                )
            )
            ver = await conn.fetchrow(
                """
                INSERT INTO agent_versions (
                    tenant_id, agent_id, version, config, published_by
                )
                VALUES ($4, $1, $2, $3::jsonb, $5)
                RETURNING version, published_at
                """,
                agent_id,
                next_version,
                frozen.model_dump_json(),
                ctx.tenant.id,
                ctx.user.id,
            )
            await conn.execute(
                "UPDATE agents SET published_version = $2 WHERE id = $1 AND tenant_id = $3",
                agent_id,
                next_version,
                ctx.tenant.id,
            )
    bg.add_task(
        webhooks.dispatch,
        ctx.tenant,
        events.AGENT_PUBLISHED,
        str(agent_id),
        {"agent_id": str(agent_id), "version": ver["version"]},
    )
    return PublishAgentResponse(
        agent_id=agent_id,
        version=ver["version"],
        published_at=ver["published_at"],
        warnings=result.warnings,
    )


# ─────────────────────────────────── versions ───────────────────────────────


async def _version_list(agent_id: UUID, ctx: Context) -> list[AgentVersionResponse]:
    """The publish history, newest first, with each publisher named."""
    pool = await ctx.tenant_pool()
    rows = await pool.fetch(
        "SELECT version, published_at, published_by FROM agent_versions "
        "WHERE agent_id = $1 AND tenant_id = $2 ORDER BY version DESC",
        agent_id,
        ctx.tenant.id,
    )
    if not rows:
        return []
    # Users live in the control plane, agent versions in this region's own DB, so
    # the name is a second lookup rather than a join — now an internal API call
    # rather than a second query. The ids come from this tenant's own version
    # rows, which is what scopes the lookup — deliberately not a membership join,
    # so who published a version survives them leaving.
    emails = await control.user_emails(list({r["published_by"] for r in rows}))
    return [
        AgentVersionResponse(
            version=r["version"],
            published_at=r["published_at"],
            published_by=emails.get(r["published_by"]),
        )
        for r in rows
    ]


async def get_agent_version(
    agent_id: UUID, version: int, ctx: Context
) -> AgentVersionDetailResponse:
    pool = await ctx.tenant_pool()
    row = await pool.fetchrow(
        "SELECT version, published_at, published_by, config FROM agent_versions "
        "WHERE agent_id = $1 AND version = $2 AND tenant_id = $3",
        agent_id,
        version,
        ctx.tenant.id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="agent version not found")
    # Scoped by the version row we just read from this tenant's DB, not by a
    # membership — see list_agent_versions.
    emails = await control.user_emails([row["published_by"]] if row["published_by"] else [])
    email = emails.get(row["published_by"])
    return AgentVersionDetailResponse(
        version=row["version"],
        published_at=row["published_at"],
        published_by=email,
        config=_config_of(row),
    )


async def rollback_agent_version(
    agent_id: UUID,
    version: int,
    bg: BackgroundTasks,
    ctx: Context,
) -> AgentResponse:
    pool = await ctx.tenant_pool()
    row = await pool.fetchrow(
        "SELECT version, published_at, config FROM agent_versions "
        "WHERE agent_id = $1 AND version = $2 AND tenant_id = $3",
        agent_id,
        version,
        ctx.tenant.id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="agent version not found")
    frozen = _config_of(row)
    # The third door into what this agent's numbers, streams, triggers and
    # batches run, and the easiest to forget: a version from before a channel
    # switch or a variable's default strands them exactly as a publish would.
    if errors := await _answering_errors(
        pool,
        tenant_id=ctx.tenant.id,
        agent_id=agent_id,
        cfg=frozen,
        rolling_back_to=row["version"],
    ):
        raise validation_error(errors, "rolling back would break what this agent is assigned to")
    draft = unpin_task_selections(unpin_tool_selections(frozen))
    try:
        await pool.execute(
            "UPDATE agents SET name = $2, channel = $3, config = $4::jsonb, "
            "published_version = $5, "
            # Not now(): the draft's content *is* what was saved at that moment,
            # and `updated_at` read against the live version's `published_at` is
            # how a caller tells a drafted agent from a clean one. Stamping now()
            # would leave every rolled-back agent permanently claiming an edit.
            "updated_at = $6 WHERE id = $1 AND tenant_id = $7",
            agent_id,
            draft.name,
            draft.channel,
            draft.model_dump_json(),
            row["version"],
            row["published_at"],
            ctx.tenant.id,
        )
    except asyncpg.UniqueViolationError:
        raise HTTPException(
            status_code=409,
            detail=(
                f"another agent is named '{draft.name}' — rename it before rolling this one back"
            ),
        )
    # Two things moved and a consumer may be mirroring either one: the draft is
    # now that version's config, and what live calls run has changed.
    for event in (events.AGENT_UPDATED, events.AGENT_PUBLISHED):
        bg.add_task(
            webhooks.dispatch,
            ctx.tenant,
            event,
            str(agent_id),
            {"agent_id": str(agent_id), "version": row["version"]},
        )
    return await get_agent(agent_id, ctx)
