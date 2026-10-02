"""Agent task CRUD, publish, versions, and the endpoint that runs one.

The same lifecycle an agent has, function for function: a draft you save freely,
a publish that freezes it as an immutable version with its tools pinned, and
runs and email batches that read the published one. ``services.agents.service``
is the source this mirrors — when a rule changes there it should change here.

Two validation grades carry the split. A draft write runs
``validate_task_draft`` at draft grade, so a half-written task saves and its
author can keep working; ``validate_task`` and ``publish_task`` run it at
publish grade, where a missing BYOK key or an empty `output` becomes a blocker
on a config that is meant to be finished.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any
from uuid import UUID, uuid4

import asyncpg
from fastapi import HTTPException
from pydantic import ValidationError

from api.core.schemas import (
    OkResponse,
    Page,
    ValidateResponse,
    field_errors,
    page_slice,
    validation_error,
)
from services.agents import missing_required_vars
from services.agents.materialize import (
    compile_inline_code,
    has_inline_definitions,
    materialize_inline,
)
from services.agents.override import deep_merge, drop_superseded_efforts, override_dump
from services.agents.pin import (
    pin_reasoning_efforts,
    pin_tool_selections,
    unpin_tool_selections,
)
from services.control import client as control
from services.user import Context

from .models import (
    CreateTaskRequest,
    PatchTaskRequest,
    PublishTaskResponse,
    RunTaskRequest,
    TaskConfig,
    TaskResponse,
    TaskRunResponse,
    TaskRunSummary,
    TaskVersionDetailResponse,
    TaskVersionResponse,
)
from .run import run_task, undeclared_vars
from .validate import validate_task_draft

# The task, plus the two facts a list page cannot render without a second query
# per row: how its last run went, and when the live version was frozen. The last
# run is a lateral join so one row of `task_runs` answers all four of its
# columns at once; the publish time is a correlated subquery, and it is here for
# the same reason `_AGENT_ROW` carries it — a caller must be able to tell a task
# whose draft has moved on from one whose draft is what runs, without fetching
# every task's history one at a time.
_TASK_SELECT = """
    t.id, t.config, t.published_version, t.created_at, t.updated_at, t.created_by,
    (SELECT v.published_at FROM agent_task_versions v
      WHERE v.task_id = t.id AND v.tenant_id = t.tenant_id
        AND v.version = t.published_version) AS published_at,
    r.id AS run_id, r.status AS run_status, r.error AS run_error,
    r.started_at AS run_started_at, r.duration_ms AS run_duration_ms,
    r.provider_cost AS run_provider_cost
    FROM agent_tasks t
    LEFT JOIN LATERAL (
        SELECT id, status, error, started_at, duration_ms, provider_cost
        FROM task_runs
        WHERE task_id = t.id AND tenant_id = t.tenant_id
        ORDER BY started_at DESC
        LIMIT 1
    ) r ON TRUE
"""


# Everything `TaskRunResponse` reads, shared by the one-run read and the list
# so the two cannot report a run differently.
_RUN_COLUMNS = """
id, task_id, task_name, task_version, status, error, vars, output, trace, usage,
steps_used, max_steps, attempts, provider_cost, billing_status,
started_at, ended_at, duration_ms
"""


def _config_of(row: Mapping[str, Any]) -> TaskConfig:
    """Parse the JSONB ``config`` column from an agent_tasks / version row."""
    return TaskConfig.model_validate(row["config"])


def _task_out(
    row: Mapping[str, Any], versions: list[TaskVersionResponse] | None = None
) -> TaskResponse:
    last_run = (
        TaskRunSummary(
            id=row["run_id"],
            status=row["run_status"],
            error=row["run_error"],
            started_at=row["run_started_at"],
            duration_ms=row["run_duration_ms"],
            provider_cost=row["run_provider_cost"],
        )
        if row["run_id"]
        else None
    )
    return TaskResponse(
        id=row["id"],
        config=_config_of(row),
        published_version=row["published_version"],
        published_at=row["published_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        created_by=row["created_by"],
        versions=versions,
        last_run=last_run,
    )


async def _require_task(
    task_id: UUID,
    ctx: Context,
    *,
    conn: asyncpg.Connection | None = None,
    for_update: bool = False,
) -> asyncpg.Record:
    """Load a task row for this tenant, or raise 404.

    Pass ``conn`` (and optionally ``for_update=True``) when the caller already
    holds a transaction that must lock the row.
    """
    sql = f"SELECT {_TASK_SELECT} WHERE t.id = $1 AND t.tenant_id = $2"
    if for_update:
        # The lateral join makes a bare FOR UPDATE ambiguous; the task row is
        # the only one publish contends for.
        sql += " FOR UPDATE OF t"
    executor = conn if conn is not None else await ctx.tenant_pool()
    row = await executor.fetchrow(sql, task_id, ctx.tenant.id)
    if not row:
        raise HTTPException(status_code=404, detail="task not found")
    return row


async def list_tasks(ctx: Context, limit: int = 200, offset: int = 0) -> Page[TaskResponse]:
    pool = await ctx.tenant_pool()
    rows = await pool.fetch(
        f"SELECT {_TASK_SELECT} WHERE t.tenant_id = $3 ORDER BY t.updated_at DESC LIMIT $1 OFFSET $2",
        limit + 1,
        offset,
        ctx.tenant.id,
    )
    return page_slice([_task_out(r) for r in rows], limit=limit, offset=offset)


async def _validated_draft(ctx: Context, config: TaskConfig) -> None:
    """Refuse a draft that cannot be saved. Draft grade: the publish-only
    checks — a workspace key for the model, an output to produce — wait for the
    publish screen that renders them."""
    result = await validate_task_draft(ctx, config)
    if result.errors:
        raise validation_error(result.errors, "this task config has problems")


async def create_task(body: CreateTaskRequest, ctx: Context) -> TaskResponse:
    config = body.config
    await _validated_draft(ctx, config)
    inline = await _compile_inline(config)
    pool = await ctx.tenant_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            config = await _materialize(conn, ctx, config, inline)
            row = await _insert(conn, ctx, config)
    return _task_out(row)


async def get_task(task_id: UUID, ctx: Context) -> TaskResponse:
    row = await _require_task(task_id, ctx)
    return _task_out(row, await _version_list(task_id, ctx))


async def update_task(task_id: UUID, body: PatchTaskRequest, ctx: Context) -> TaskResponse:
    row = await _require_task(task_id, ctx)
    patch = override_dump(body.config)
    merged = deep_merge(_config_of(row).model_dump(mode="json"), patch, TaskConfig)
    drop_superseded_efforts(merged, patch)
    try:
        config = TaskConfig.model_validate(merged)
    except ValidationError as exc:
        # Located under `config.`, as the request validator would have put them.
        errors = [{**error, "loc": ("config", *error["loc"])} for error in exc.errors()]
        raise validation_error(field_errors(errors), "this task config has problems") from exc
    await _validated_draft(ctx, config)
    inline = await _compile_inline(config)
    pool = await ctx.tenant_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            config = await _materialize(conn, ctx, config, inline)
            try:
                await conn.execute(
                    "UPDATE agent_tasks SET name = $2, config = $3::jsonb, updated_at = now() "
                    "WHERE id = $1 AND tenant_id = $4",
                    task_id,
                    config.name,
                    config.model_dump_json(),
                    ctx.tenant.id,
                )
            except asyncpg.UniqueViolationError:
                raise HTTPException(
                    status_code=409, detail="a task with that name already exists"
                ) from None
            # Re-read rather than RETURNING, exactly as `_insert` does: the row
            # a caller gets back carries the last run and the live version's
            # publish time, and neither is a column on this table.
            #
            # Not `get_task`: `versions` is `get_task`'s alone, and a history
            # query plus a control-plane email lookup on every save would put an
            # internal round trip in the path of the editor's most frequent
            # action. A save cannot change the version list. `update_agent`
            # returns the same shape.
            row = await conn.fetchrow(
                f"SELECT {_TASK_SELECT} WHERE t.id = $1 AND t.tenant_id = $2",
                task_id,
                ctx.tenant.id,
            )
            assert row is not None
    return _task_out(row)


async def delete_task(task_id: UUID, ctx: Context) -> OkResponse:
    """Delete the task, unless an agent still attaches it.

    Its runs survive, with `task_id` set to null.

    A run from last month must never be the reason a delete starts failing, and
    the run's `task_name` snapshot is what keeps it readable afterwards. The same
    goes for a finished email batch — its `task_id` goes null and the batch still
    renders.

    A batch that is **still drafting** is different, and is refused: deleting the
    task under it would fail every remaining row with `configuration` and tell
    the person doing the deleting nothing at all.
    """
    # Local import: `services.email` reaches back into `services.tasks` for the
    # runner its drafting pass calls, so importing it at module scope is a cycle.
    from services.email import batches_blocking_task_delete

    pool = await ctx.tenant_pool()
    # The same guard `delete_tool` has, for the same failure: an agent that
    # attaches this task pins a version of it, and `resolve_pinned_tasks` raises
    # at session start when that version is gone — so the agent would simply not
    # answer. Older agent VERSIONS are allowed to dangle, exactly as they are for
    # a deleted tool: blocking on those would make a task undeletable for ever
    # after one publish.
    attached = await pool.fetch(
        """
        SELECT DISTINCT a.name
        FROM agents a
        LEFT JOIN agent_versions av
            ON av.agent_id = a.id AND av.tenant_id = a.tenant_id
            AND av.version = a.published_version
        -- the draft and the live version, checked by the same predicate
        CROSS JOIN LATERAL (VALUES (a.config), (av.config)) AS c(config)
        WHERE a.tenant_id = $2 AND c.config IS NOT NULL AND c.config @> $1::jsonb
        ORDER BY a.name
        """,
        json.dumps({"tasks": [{"task_id": str(task_id)}]}),
        ctx.tenant.id,
    )
    if attached:
        names = ", ".join(f"'{r['name']}'" for r in attached)
        raise HTTPException(
            status_code=409,
            detail=f"this task is attached to {names} — detach it there before deleting it",
        )
    blocking = await batches_blocking_task_delete(pool, ctx.tenant.id, task_id)
    if blocking:
        raise HTTPException(
            status_code=409,
            detail=(
                "these email batches are still drafting with this task: "
                f"{', '.join(blocking)} - cancel them first"
            ),
        )
    deleted = await pool.fetchval(
        "DELETE FROM agent_tasks WHERE id = $1 AND tenant_id = $2 RETURNING id",
        task_id,
        ctx.tenant.id,
    )
    if not deleted:
        raise HTTPException(status_code=404, detail="task not found")
    return OkResponse()


# ────────────────────────────── publish + versions ──────────────────────────


async def _live_batch_errors(
    executor: asyncpg.Connection | asyncpg.Pool,
    *,
    tenant_id: UUID,
    task_id: UUID,
    cfg: TaskConfig,
) -> list[str]:
    """Why this config cannot go live while a batch is still drafting with it.

    The task's answer to ``_inbound_number_errors``: a batch's CSV columns and
    its `field_map` are fixed once the rows are uploaded, so a publish that makes
    a variable required the list has no column for, or removes an output field
    the mapping points at, would fail every remaining row with `configuration`.
    """
    # Local import: `services.email` reaches back into `services.tasks` for the
    # runner its drafting pass calls, so importing it at module scope is a cycle.
    from services.email import batches_broken_by_task_config

    return await batches_broken_by_task_config(executor, tenant_id, task_id, cfg)


async def validate_task(task_id: UUID, ctx: Context) -> ValidateResponse:
    row = await _require_task(task_id, ctx)
    cfg = _config_of(row)
    result = await validate_task_draft(ctx, cfg, for_publish=True)
    # Here as well as in `publish_task`, and not as belt-and-braces: the editor
    # publishes as save -> validate -> show the errors modal -> publish, so a
    # guard that lived only in `publish_task` would give a clean pre-flight
    # followed by a bare "Could not publish the task". The guard belongs where
    # the sentence is rendered.
    errors = list(result.errors)
    errors.extend(
        await _live_batch_errors(
            await ctx.tenant_pool(), tenant_id=ctx.tenant.id, task_id=task_id, cfg=cfg
        )
    )
    return ValidateResponse(errors=errors, warnings=result.warnings)


async def publish_task(task_id: UUID, ctx: Context) -> PublishTaskResponse:
    pool = await ctx.tenant_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            task = await _require_task(task_id, ctx, conn=conn, for_update=True)
            next_version = await conn.fetchval(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM agent_task_versions "
                "WHERE task_id = $1 AND tenant_id = $2",
                task_id,
                ctx.tenant.id,
            )
            cfg = _config_of(task)
            result = await validate_task_draft(ctx, cfg, for_publish=True)
            errors = list(result.errors)
            errors.extend(
                await _live_batch_errors(conn, tenant_id=ctx.tenant.id, task_id=task_id, cfg=cfg)
            )
            if errors:
                raise validation_error(errors, "this task has validation errors")
            # `pin_tool_selections` is generic over `AgentBase`, so a task and
            # an agent share one implementation of what "pin" means and a task
            # comes back a task — hooks and all.
            frozen = pin_reasoning_efforts(await pin_tool_selections(conn, ctx.tenant.id, cfg))
            ver = await conn.fetchrow(
                """
                INSERT INTO agent_task_versions (
                    tenant_id, task_id, version, config, published_by
                )
                VALUES ($4, $1, $2, $3::jsonb, $5)
                RETURNING version, published_at
                """,
                task_id,
                next_version,
                frozen.model_dump_json(),
                ctx.tenant.id,
                ctx.user.id,
            )
            await conn.execute(
                "UPDATE agent_tasks SET published_version = $2 WHERE id = $1 AND tenant_id = $3",
                task_id,
                next_version,
                ctx.tenant.id,
            )
    return PublishTaskResponse(
        task_id=task_id,
        version=ver["version"],
        published_at=ver["published_at"],
        warnings=result.warnings,
    )


async def _version_list(task_id: UUID, ctx: Context) -> list[TaskVersionResponse]:
    """The publish history, newest first, with each publisher named."""
    pool = await ctx.tenant_pool()
    rows = await pool.fetch(
        "SELECT version, published_at, published_by FROM agent_task_versions "
        "WHERE task_id = $1 AND tenant_id = $2 ORDER BY version DESC",
        task_id,
        ctx.tenant.id,
    )
    if not rows:
        return []
    # Users live in the control plane, task versions in this region's own DB, so
    # the name is an internal API call rather than a join. The ids come from this
    # tenant's own version rows, which is what scopes the lookup — deliberately
    # not a membership join, so who published a version survives them leaving.
    emails = await control.user_emails(list({r["published_by"] for r in rows}))
    return [
        TaskVersionResponse(
            version=r["version"],
            published_at=r["published_at"],
            published_by=emails.get(r["published_by"]),
        )
        for r in rows
    ]


async def get_task_version(task_id: UUID, version: int, ctx: Context) -> TaskVersionDetailResponse:
    pool = await ctx.tenant_pool()
    row = await pool.fetchrow(
        "SELECT version, published_at, published_by, config FROM agent_task_versions "
        "WHERE task_id = $1 AND version = $2 AND tenant_id = $3",
        task_id,
        version,
        ctx.tenant.id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="task version not found")
    # Scoped by the version row we just read from this tenant's DB, not by a
    # membership — see _version_list.
    emails = await control.user_emails([row["published_by"]] if row["published_by"] else [])
    return TaskVersionDetailResponse(
        version=row["version"],
        published_at=row["published_at"],
        published_by=emails.get(row["published_by"]),
        config=_config_of(row),
    )


async def rollback_task_version(task_id: UUID, version: int, ctx: Context) -> TaskResponse:
    pool = await ctx.tenant_pool()
    row = await pool.fetchrow(
        "SELECT version, published_at, config FROM agent_task_versions "
        "WHERE task_id = $1 AND version = $2 AND tenant_id = $3",
        task_id,
        version,
        ctx.tenant.id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="task version not found")
    frozen = _config_of(row)
    # The second door into what a live batch drafts with, and the easier one to
    # forget: a version that predates a variable's default strands a drafting
    # batch exactly as a publish would.
    if errors := await _live_batch_errors(
        pool, tenant_id=ctx.tenant.id, task_id=task_id, cfg=frozen
    ):
        raise validation_error(errors, "this version cannot draft this task's live email batches")
    draft = _unpin(frozen)
    try:
        await pool.execute(
            "UPDATE agent_tasks SET name = $2, config = $3::jsonb, published_version = $4, "
            # Not now(): the draft's content *is* what was saved at that moment,
            # and `updated_at` read against the live version's `published_at` is
            # how a caller tells a drafted task from a clean one. Stamping now()
            # would leave every rolled-back task permanently claiming an edit.
            "updated_at = $5 WHERE id = $1 AND tenant_id = $6",
            task_id,
            draft.name,
            draft.model_dump_json(),
            row["version"],
            row["published_at"],
            ctx.tenant.id,
        )
    except asyncpg.UniqueViolationError:
        raise HTTPException(
            status_code=409,
            detail=f"another task is named '{draft.name}' — rename it before rolling this one back",
        ) from None
    return await get_task(task_id, ctx)


def _unpin(cfg: TaskConfig) -> TaskConfig:
    """The same config with every tool version dropped.

    A frozen version pins its tools; a draft never does — it tracks whatever is
    published now.
    """
    return unpin_tool_selections(cfg)


async def _config_to_run(
    task_id: UUID, row: asyncpg.Record, version: int | str | None, ctx: Context
) -> tuple[TaskConfig, int | None]:
    """Which definition this run uses, pinned and ready, and which version it is.

    Every path returns a config whose tools carry a version, because
    `resolve_pinned_tools` raises rather than falling back on one that does not.
    A frozen version is pinned by definition; the draft is pinned here, after
    being held to the publish bar — so a draft run that could not be published
    is refused with the publish errors instead of dying in the loaders with a
    `platform` error nobody can act on.
    """
    if version == "draft":
        cfg = _config_of(row)
        result = await validate_task_draft(ctx, cfg, for_publish=True)
        if result.errors:
            raise validation_error(result.errors, "this task's draft cannot run")
        pool = await ctx.tenant_pool()
        return await pin_tool_selections(pool, ctx.tenant.id, cfg), None

    wanted = row["published_version"] if version is None else version
    if wanted is None:
        raise HTTPException(
            status_code=400,
            detail=f"publish '{_config_of(row).name}' before running it",
        )
    pool = await ctx.tenant_pool()
    frozen = await pool.fetchval(
        "SELECT config FROM agent_task_versions WHERE task_id = $1 AND version = $2 "
        "AND tenant_id = $3",
        task_id,
        wanted,
        ctx.tenant.id,
    )
    if frozen is None:
        raise HTTPException(status_code=404, detail="task version not found")
    return TaskConfig.model_validate(frozen), wanted


async def run_task_once(task_id: UUID, body: RunTaskRequest, ctx: Context) -> TaskRunResponse:
    row = await _require_task(task_id, ctx)
    config, task_version = await _config_to_run(task_id, row, body.version, ctx)

    # Both checks happen here rather than inside the run: a request whose inputs
    # do not match the task's contract is a 400 the caller can fix, not a failed
    # run in their history. `run_task` re-checks the required half for the sake
    # of callers that settle a row instead of answering an HTTP request.
    if unknown := undeclared_vars(config, body.vars):
        raise validation_error(
            [f"this task does not declare a variable named '{name}'" for name in unknown],
            "these values do not match the task's variables",
        )
    if missing := missing_required_vars(config.vars, body.vars):
        raise validation_error(
            [f"'{name}' is required and has no default" for name in missing],
            "this task needs values it was not given",
        )

    return await run_task(
        tenant=ctx.tenant,
        cfg=config,
        vars=body.vars,
        run_id=uuid4(),
        task_id=task_id,
        task_version=task_version,
        created_by=ctx.user.id,
    )


async def get_task_run(task_id: UUID, run_id: UUID, ctx: Context) -> TaskRunResponse:
    """One run in full — its output, its trace and what it cost.

    `list_task_runs` returns the same shape and would answer this with a scan,
    but the caller that needs it holds one run id and nothing else: an email
    batch's review table, linking each drafted row to what the task actually did
    on it. That is the question "why did it write that", and it is the whole
    reason the trace is stored.
    """
    await _require_task(task_id, ctx)
    pool = await ctx.tenant_pool()
    row = await pool.fetchrow(
        f"""
        SELECT {_RUN_COLUMNS}
        FROM task_runs
        WHERE id = $1 AND task_id = $2 AND tenant_id = $3
        """,
        run_id,
        task_id,
        ctx.tenant.id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="task run not found")
    return TaskRunResponse.model_validate(dict(row))


async def list_task_runs(
    task_id: UUID, ctx: Context, limit: int = 50, offset: int = 0
) -> Page[TaskRunResponse]:
    await _require_task(task_id, ctx)
    pool = await ctx.tenant_pool()
    rows = await pool.fetch(
        f"""
        SELECT {_RUN_COLUMNS}
        FROM task_runs
        WHERE task_id = $1 AND tenant_id = $4
        ORDER BY started_at DESC
        LIMIT $2 OFFSET $3
        """,
        task_id,
        limit + 1,
        offset,
        ctx.tenant.id,
    )
    return page_slice(
        [TaskRunResponse.model_validate(dict(r)) for r in rows], limit=limit, offset=offset
    )


# ───────────────────────────── inline definitions ───────────────────────────


async def _compile_inline(config: TaskConfig) -> bool:
    """Transpile any inline `code` op in place; say whether anything is inline.

    Outside the write's transaction, exactly as the agent path does it: this is a
    network round trip to the code-execution service, and holding a transaction
    across one is how a slow dependency becomes lock contention. The compiled JS
    lands on the authored op, so it is already there when the tool is frozen.
    """
    if not has_inline_definitions(config):
        return False
    await compile_inline_code(config, subject="task")
    return True


async def _materialize(
    conn: asyncpg.Connection, ctx: Context, config: TaskConfig, inline: bool
) -> TaskConfig:
    """Turn any tool or MCP server defined inline into a real row.

    The same rule an agent write follows, through the same function, and for the
    same reason: a first-class tool has an editor, a test panel, a ToolCoPilot,
    `run_tool` and version history, and an inline copy sitting in
    `agent_tasks.config` would have none of them, for ever. It is also what makes
    "create the task and its tools in one request" work from the API and from an
    AI builder.
    """
    if not inline:
        return config
    return await materialize_inline(conn, ctx, config, subject="task")


async def _insert(conn: asyncpg.Connection, ctx: Context, config: TaskConfig) -> asyncpg.Record:
    try:
        task_id = await conn.fetchval(
            "INSERT INTO agent_tasks (tenant_id, name, config, created_by) "
            "VALUES ($1, $2, $3::jsonb, $4) RETURNING id",
            ctx.tenant.id,
            config.name,
            config.model_dump_json(),
            ctx.user.id,
        )
    except asyncpg.UniqueViolationError:
        raise HTTPException(
            status_code=409, detail="a task with that name already exists"
        ) from None
    row = await conn.fetchrow(
        f"SELECT {_TASK_SELECT} WHERE t.id = $1 AND t.tenant_id = $2", task_id, ctx.tenant.id
    )
    assert row is not None
    return row
