"""Tools CRUD and publish.

A Tool is a tree of first-class Operations. The draft operation tree lives as
JSON on `tools.operations`; publishing validates that draft, freezes it into
`tool_versions.definition`, and bumps `published_version`. Published agent
versions snapshot the tool definitions they were published with, so a tool
republish requires an agent republish before live calls use the new definition.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

import asyncpg
import httpx
from fastapi import HTTPException
from jsonschema import Draft202012Validator, SchemaError
from pydantic import BaseModel, Field

from api.core.schemas import (
    OkResponse,
    Page,
    ValidateResponse,
    coalesce,
    page_slice,
    validation_error,
)
from services.secrets import list_secret_names
from services.user import Context
from settings import get_settings

# The authored operation-tree shapes live in `defs.py` (layer 0) so the agent
# domain model can embed them; re-exported here because this is where every
# existing caller imports them from.
from .defs import (
    CodeConfig,
    CodeOperation,
    IfOperation,
    OnError,  # noqa: F401
    Operation,
    OperationRequest,
    PublishFieldRequest,  # noqa: F401
    PublishKey,  # noqa: F401
    PublishPath,  # noqa: F401
    PublishStore,  # noqa: F401
    ToolDefinition,
    ToolName,
)
from .tree import source_only, tree_is_silent, tree_kinds, walk_tree
from .validate import (
    normalized_tool_schema,
    shape_errors,
    validate_operation_tree,
)

# names the runtime injects itself (compiler/tools.py's `build_stop_recording`
# and `build_voicemail_detected`, compiler/tasks.py's `submit_result` and
# `finish_without_result`, compiler/faqs.py's `get_faq_answers`) — a tenant tool with one of these would make
# ToolContext raise on duplicate names at session start and kill the call.
#
# The two `lk_agents_*` names are LiveKit's own: it auto-exposes both whenever
# any tool on the agent carries `ToolFlag.CANCELLABLE`, which a voice or video
# `long_running_task` tool does (compiler/tools.py::build_tools).
#
# Reserved workspace-wide even though each is injected only in some situations:
# `stop_recording` appears only on a recording agent and `submit_result` only on
# a task, but a name that is safe here and fatal there is not a rule anyone can
# hold in their head.
RESERVED_TOOL_NAMES = {
    "stop_recording",
    "voicemail_detected",
    "submit_result",
    "finish_without_result",
    "get_faq_answers",
    "lk_agents_cancel_task",
    "lk_agents_get_running_tasks",
}
Row = asyncpg.Record | Mapping[str, Any]


# ───────────────────────────── request bodies ───────────────────────────────


class CreateToolRequest(BaseModel):
    name: ToolName
    description: str = ""
    json_schema: dict[str, Any] = Field(default_factory=dict)
    # Voice/video detach the work and keep talking. Text agents await it.
    long_running_task: bool = False
    silent: bool = False
    disable_interruptions: bool = False
    operations: list[OperationRequest] = Field(default_factory=list)


class PatchToolRequest(BaseModel):
    name: ToolName | None = None
    description: str | None = None
    json_schema: dict[str, Any] | None = None
    long_running_task: bool | None = None
    silent: bool | None = None
    disable_interruptions: bool | None = None
    operations: list[OperationRequest] | None = None


CreateToolRequest.model_rebuild()
PatchToolRequest.model_rebuild()


class PublishToolRequest(BaseModel):
    changelog: str | None = None


# ─────────────────────────────── response models ────────────────────────────


class ToolVersionResponse(BaseModel):
    version: int
    changelog: str | None = None
    published_at: datetime


class ToolVersionDetailResponse(ToolVersionResponse):
    """One frozen version, unpacked: the definition that ran, beside its metadata.

    The same fields a `ToolResponse` carries, so a caller can diff a version
    against the draft — or against another version — without reshaping either.
    """

    name: str
    description: str
    json_schema: dict[str, Any]
    operations: list[OperationRequest]
    long_running_task: bool
    silent: bool
    disable_interruptions: bool


class ToolResponse(BaseModel):
    id: UUID
    name: str
    description: str
    json_schema: dict[str, Any]
    operations: list[OperationRequest]
    long_running_task: bool
    silent: bool
    disable_interruptions: bool
    published_version: int | None = None
    # When `published_version` was frozen. Null while the tool is a draft.
    # Read against `updated_at` it answers the question a draft/publish model
    # always raises: has the draft moved on from what agents are calling?
    published_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    # Who created this, as a control-plane user id. Attribution only — resolve it
    # to a name against GET /v1/org/members, which is where people live.
    created_by: UUID | None = None
    versions: list[ToolVersionResponse] | None = None


class PublishResponse(BaseModel):
    tool_id: UUID
    version: int
    published_at: datetime
    warnings: list[str] = Field(default_factory=list)


# ─────────────────────────────── serializers ────────────────────────────────


def operation_from_request(node: OperationRequest) -> Operation:
    """One authored node as the runtime shape the compiler and validator read.

    A straight dump, because the variant already IS the runtime shape: each kind
    carries exactly the fields that kind has, so there is nothing left to
    normalize on the way through. `then` / `else` recurse with it.

    The config comes out as a fresh dict rather than the model's own, so a `code`
    op's `compiled_js` travels the other way: `transpile_code_configs` writes it
    onto the `CodeConfig` before an inline tool is converted, never into the dict
    afterwards.
    """
    return node.model_dump(by_alias=True)


def operation_tree(nodes: Sequence[OperationRequest]) -> list[Operation]:
    """An authored tree, converted and shape-checked. Raises on a bad shape."""
    tree = [operation_from_request(node) for node in nodes]
    if errs := shape_errors(tree):
        # All of them, in the `{message, errors}` shape every other validation
        # failure in this API uses — an author with three bad tokens would
        # otherwise fix them one save at a time.
        raise validation_error(errs, "this tool's operations have problems")
    return tree


def stored_tree(value: object) -> list[Operation]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise HTTPException(status_code=500, detail="tool operations is not an operation tree")
    return value


def _tool_out(
    row: Row,
    *,
    published_at: datetime | None,
    versions: list[ToolVersionResponse] | None = None,
) -> ToolResponse:
    return ToolResponse(
        id=row["id"],
        name=row["name"],
        description=row["description"],
        json_schema=row["json_schema"],
        operations=stored_tree(row["operations"]),
        long_running_task=row["long_running_task"],
        silent=row["silent"],
        disable_interruptions=row["disable_interruptions"],
        published_version=row["published_version"],
        published_at=published_at,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        created_by=row["created_by"],
        versions=versions,
    )


def json_schema_errors(schema: object) -> list[str]:
    if not isinstance(schema, dict):
        return ["tool json_schema must be a JSON object"]
    candidate = normalized_tool_schema(schema)
    try:
        Draft202012Validator.check_schema(candidate)
    except SchemaError as e:
        return [f"tool json_schema is invalid: {e.message}"]
    if candidate.get("type") != "object":
        return ["tool json_schema must describe an object of named parameters"]
    return []


# ─────────────────────────────────── tools ──────────────────────────────────


async def list_tools(
    ctx: Context,
    limit: int = 200,
    offset: int = 0,
) -> Page[ToolResponse]:
    pool = await ctx.tenant_pool()
    rows = await pool.fetch(
        "SELECT id, name, description, json_schema, long_running_task, silent, "
        "disable_interruptions, operations, published_version, "
        "created_at, updated_at, created_by, "
        # The publish time of the live version, so a caller can tell a tool whose
        # draft has moved on from one whose draft is what agents are calling,
        # without fetching every tool's version history one at a time.
        "(SELECT v.published_at FROM tool_versions v "
        " WHERE v.tool_id = tools.id AND v.tenant_id = tools.tenant_id "
        "   AND v.version = tools.published_version) AS published_at "
        "FROM tools WHERE tenant_id = $1 ORDER BY updated_at DESC "
        "LIMIT $2 OFFSET $3",
        ctx.tenant.id,
        limit + 1,
        offset,
    )
    return page_slice(
        [_tool_out(r, published_at=r["published_at"]) for r in rows],
        limit=limit,
        offset=offset,
    )


async def create_tool(body: CreateToolRequest, ctx: Context) -> ToolResponse:
    if body.name in RESERVED_TOOL_NAMES:
        raise HTTPException(status_code=400, detail=f"'{body.name}' is reserved by the platform")
    operations = operation_tree(body.operations)
    pool = await ctx.tenant_pool()
    try:
        row = await pool.fetchrow(
            """
            INSERT INTO tools (
                tenant_id, name, description, json_schema, long_running_task,
                silent, disable_interruptions, operations, created_by
            )
            VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7, $8::jsonb, $9) RETURNING *
            """,
            ctx.tenant.id,
            body.name,
            body.description,
            json.dumps(body.json_schema),
            body.long_running_task,
            body.silent,
            body.disable_interruptions,
            operations,
            ctx.user.id,
        )
    except asyncpg.UniqueViolationError:
        raise HTTPException(status_code=409, detail="a tool with that name already exists")
    return _tool_out(row, published_at=None, versions=[])


async def get_tool(tool_id: UUID, ctx: Context) -> ToolResponse:
    pool = await ctx.tenant_pool()
    row = await pool.fetchrow(
        "SELECT id, name, description, json_schema, long_running_task, silent, "
        "disable_interruptions, operations, "
        "published_version, created_at, updated_at, created_by, tenant_id "
        "FROM tools WHERE id = $1 AND tenant_id = $2",
        tool_id,
        ctx.tenant.id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="tool not found")
    versions = [
        ToolVersionResponse.model_validate(dict(v))
        for v in await pool.fetch(
            "SELECT version, changelog, published_at FROM tool_versions WHERE tool_id = $1 AND tenant_id = $2 ORDER BY version DESC",
            tool_id,
            ctx.tenant.id,
        )
    ]
    live = next((v for v in versions if v.version == row["published_version"]), None)
    return _tool_out(row, published_at=live.published_at if live else None, versions=versions)


async def patch_tool(tool_id: UUID, body: PatchToolRequest, ctx: Context) -> ToolResponse:
    pool = await ctx.tenant_pool()
    row = await pool.fetchrow(
        "SELECT id, name, description, json_schema, long_running_task, silent, "
        "disable_interruptions, operations, "
        "published_version, created_at, updated_at, created_by, tenant_id, "
        "(SELECT v.published_at FROM tool_versions v "
        " WHERE v.tool_id = tools.id AND v.tenant_id = tools.tenant_id "
        "   AND v.version = tools.published_version) AS published_at "
        "FROM tools WHERE id = $1 AND tenant_id = $2",
        tool_id,
        ctx.tenant.id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="tool not found")
    if body.name in RESERVED_TOOL_NAMES:
        raise HTTPException(status_code=400, detail=f"'{body.name}' is reserved by the platform")
    name = coalesce(body.name, row["name"])
    description = coalesce(body.description, row["description"])
    # JSONB params take a dict straight through (encoded by the pool codec)
    schema = coalesce(body.json_schema, row["json_schema"])
    long_running_task = coalesce(body.long_running_task, row["long_running_task"])
    silent = coalesce(body.silent, row["silent"])
    disable_interruptions = coalesce(body.disable_interruptions, row["disable_interruptions"])
    operations = (
        operation_tree(body.operations)
        if body.operations is not None
        else stored_tree(row["operations"])
    )
    try:
        updated = await pool.fetchrow(
            "UPDATE tools SET name=$2, description=$3, json_schema=$4::jsonb, long_running_task=$5, "
            "silent=$6, disable_interruptions=$7, operations=$8::jsonb, updated_at=now() "
            "WHERE id=$1 AND tenant_id=$9 RETURNING *",
            tool_id,
            name,
            description,
            schema,
            long_running_task,
            silent,
            disable_interruptions,
            operations,
            ctx.tenant.id,
        )
    except asyncpg.UniqueViolationError:
        raise HTTPException(status_code=409, detail="a tool with that name already exists")
    # A patch only ever touches the draft, so the live version — and when it was
    # published — is whatever it was before this call.
    return _tool_out(updated, published_at=row["published_at"])


async def delete_tool(tool_id: UUID, ctx: Context) -> OkResponse:
    pool = await ctx.tenant_pool()
    # Deleting a tool cascades its versions away, and an agent or a task that
    # references it — in its draft, or pinned by the version currently serving
    # calls — would be left pointing at nothing. Refuse, and name them to go fix.
    #
    # Older versions in the history are deliberately allowed to dangle:
    # blocking on those would make a tool undeletable forever after one publish.
    # The version-history UI shows such a tool as deleted, and rolling back onto
    # that version fails with a message saying which one is gone.
    in_use = await pool.fetch(
        """
        SELECT DISTINCT owner, name FROM (
            SELECT 'agent' AS owner, a.name, c.config
            FROM agents a
            LEFT JOIN agent_versions av
                ON av.agent_id = a.id AND av.tenant_id = a.tenant_id
                AND av.version = a.published_version
            -- the draft and the live version, checked by the same predicate
            CROSS JOIN LATERAL (VALUES (a.config), (av.config)) AS c(config)
            WHERE a.tenant_id = $3
            UNION ALL
            -- A task runs the same tools an agent does, and a task whose tool
            -- has been deleted fails at session start exactly as an agent's
            -- would. Tasks have no lifecycle-hook columns of their own here
            -- because the `@>` probe already covers `tools`, and a hook is a
            -- separate key checked below.
            SELECT 'task', t.name, c.config
            FROM agent_tasks t
            LEFT JOIN agent_task_versions tv
                ON tv.task_id = t.id AND tv.tenant_id = t.tenant_id
                AND tv.version = t.published_version
            CROSS JOIN LATERAL (VALUES (t.config), (tv.config)) AS c(config)
            WHERE t.tenant_id = $3
        ) owners
        WHERE config IS NOT NULL AND (
            config @> $1::jsonb
            OR config -> 'on_enter' ->> 'tool_id' = $2
            OR config -> 'on_exit' ->> 'tool_id' = $2
            OR config -> 'on_user_turn_completed' ->> 'tool_id' = $2
        )
        ORDER BY owner, name
        """,
        json.dumps({"tools": [{"tool_id": str(tool_id)}]}),
        str(tool_id),
        ctx.tenant.id,
    )
    if in_use:
        names = ", ".join(f"{r['owner']} '{r['name']}'" for r in in_use)
        raise HTTPException(
            status_code=409,
            detail=f"this tool is attached to {names} — detach it there before deleting it",
        )
    row = await pool.fetchrow(
        "DELETE FROM tools WHERE id = $1 AND tenant_id = $2 RETURNING id",
        tool_id,
        ctx.tenant.id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="tool not found")
    return OkResponse()


# ─────────────────────────────────── publish ────────────────────────────────


CODE_EXEC_UNREACHABLE = (
    "code: the code-execution service is unreachable — is the code-exec container running?"
)


async def _transpile(sources: Sequence[str]) -> tuple[list[str | None], list[str]]:
    """Compile each TypeScript source on the code-exec service, all at once.

    Returns the JS for each source (None where it failed) beside the
    deduplicated errors. One round trip per source, in parallel: a five-member
    team with a `code` op each should cost one round trip, not five.

    Compiling IS how a `code` op is validated — the errors come back to the
    caller as publish errors like any other. Because validating hands us the
    build artifact for free, a published definition carries `compiled_js` beside
    `source_ts` and the worker never transpiles mid-call.
    """
    url = get_settings().app.code_exec_url.rstrip("/") + "/transpile"
    async with httpx.AsyncClient(timeout=15) as client:

        async def one(source: str) -> tuple[str | None, str | None]:
            try:
                resp = await client.post(url, json={"source": source})
            except httpx.HTTPError:
                return None, CODE_EXEC_UNREACHABLE
            if resp.status_code != 200:
                try:
                    detail = resp.json().get("error") or resp.text
                except Exception:
                    detail = resp.text
                return None, f"code: the TypeScript does not compile — {detail}"
            return resp.json()["js"], None

        results = await asyncio.gather(*(one(source) for source in sources))
    # dict.fromkeys: an outage reports itself once, not once per operation.
    return [js for js, _ in results], list(dict.fromkeys(e for _, e in results if e))


async def transpile_code_ops(tree: list[Operation]) -> list[str]:
    """Compile every `code` op in a STORED tree, writing `compiled_js` in place.

    The publish path: the draft comes out of JSONB as dicts, so the artifact
    goes back into the dict that is about to be frozen.
    """
    code_ops = [op for op, _ in walk_tree(tree) if op["kind"] == "code"]
    if not code_ops:
        return []
    compiled, errors = await _transpile(
        [(op.get("config") or {}).get("source_ts") or "" for op in code_ops]
    )
    for op, js in zip(code_ops, compiled):
        if js is not None:
            op["config"]["compiled_js"] = js
    return errors


def inline_code_configs(nodes: Sequence[OperationRequest]) -> list[CodeConfig]:
    """Every `code` op's config in an AUTHORED tree, branches included.

    Collected across a whole roster before compiling, so an agent carrying five
    inline tools still costs one round trip.
    """
    out: list[CodeConfig] = []
    for node in nodes:
        if isinstance(node, CodeOperation):
            out.append(node.config)
        elif isinstance(node, IfOperation):
            out.extend(inline_code_configs(node.then or []))
            out.extend(inline_code_configs(node.else_ or []))
    return out


async def transpile_code_configs(configs: Sequence[CodeConfig]) -> list[str]:
    """Compile authored `code` configs, writing `compiled_js` onto each model.

    The inline path: an agent's own tools are still pydantic models here, and
    the artifact has to be on the model before `operation_from_request` dumps it
    into the tree that reaches the plan.
    """
    if not configs:
        return []
    compiled, errors = await _transpile([config.source_ts for config in configs])
    for config, js in zip(configs, compiled):
        if js is not None:
            config.compiled_js = js
    return errors


async def validate_tool_definition(
    ctx: Context,
    *,
    name: str,
    json_schema: Mapping[str, Any] | None,
    tree: list[Operation],
    long_running_task: bool,
    silent: bool,
    disable_interruptions: bool,
    secret_names: set[str],
    label: str | None = None,
    source_config: Any = None,
) -> tuple[list[str], list[str]]:
    """Everything a tool has to satisfy to be runnable, wherever it came from.

    One implementation for both: the draft a tenant publishes, and the inline
    tool a call carries. It deliberately does NOT transpile — compiling a `code`
    op is part of validating it, but a call plan compiles every op it carries in
    one round trip rather than one per tool, so the two callers own that step.

    ``label`` names the tool in errors; ``source_config`` is the agent whose
    media graph a `handoff` operation inside the tree is checked against.
    """
    schema = json_schema or {}
    if schema_errors := json_schema_errors(schema):
        return schema_errors, []
    result = await validate_operation_tree(
        tree,
        arg_names=set((schema.get("properties") or {}).keys()),
        secret_names=secret_names,
        tool_name=label or name,
        ctx=ctx,
        source_config=source_config,
    )
    whole_errors, whole_warnings = _whole_tool_problems(
        long_running_task=long_running_task,
        silent=silent,
        disable_interruptions=disable_interruptions,
        tree=tree,
    )
    errors = list(dict.fromkeys([*result.errors, *whole_errors]))
    warnings = list(dict.fromkeys([*result.warnings, *whole_warnings]))
    return errors, warnings


async def _validate_tool_for_publish(
    ctx: Context, tool: Row, tree: list[Operation]
) -> tuple[list[str], list[str]]:
    errors, warnings = await validate_tool_definition(
        ctx,
        name=tool["name"],
        json_schema=tool["json_schema"],
        tree=tree,
        long_running_task=tool["long_running_task"],
        # The author's own checkbox, never the value OR'd with `tree_is_silent`:
        # the `tools.silent` column holds the tick, and the OR happens only when
        # a version is frozen, below.
        silent=tool["silent"],
        disable_interruptions=tool["disable_interruptions"],
        secret_names=await list_secret_names(ctx.tenant),
    )
    # Compiling IS the validator for a `code` op, so its failures are publish
    # errors like any other — and they are reported alongside the rest rather
    # than after them, so an author with two problems fixes them in one save.
    code_errors = await transpile_code_ops(tree)
    return list(dict.fromkeys([*errors, *code_errors])), warnings


def _whole_tool_problems(
    *,
    long_running_task: bool,
    silent: bool,
    disable_interruptions: bool,
    tree: list[Operation],
) -> tuple[list[str], list[str]]:
    """Publish rules about the tool rather than about any one operation.

    All of them are about `long_running_task`, and every message names the
    control the author is actually looking at in the editor.
    """
    if not long_running_task:
        return [], []

    errors: list[str] = []
    kinds = tree_kinds(tree)
    if "transfer" in kinds:
        # A background tool releases the turn at dispatch, so the transfer's wait
        # for outstanding speech and the session shutdown that follows it would
        # run outside the turn that owns them — the caller would be handed over
        # mid-sentence, or not at all.
        errors.append(
            "a long-running task cannot contain a transfer — the handover has to happen "
            "inside the turn that asked for it. Untick 'Long-running task'."
        )
    if "handoff" in kinds:
        # Same reason, plus one of its own: the executor is activity-scoped, so a
        # background result is dropped when the agent it belongs to closes.
        errors.append(
            "a long-running task cannot hand off to another agent — the switch has to "
            "happen inside the turn that asked for it. Untick 'Long-running task'."
        )
    if silent:
        # One flag, one meaning. `long_running_task` exists to report a result
        # later; a silent tool never reports one. Pure fire-and-forget is a
        # different feature and we already have it, per operation.
        errors.append(
            "a silent tool cannot also be a long-running task — a background tool exists to "
            "report its result later, and a silent one never reports. Tick 'Background' on "
            "the operation instead, or untick 'Silent'."
        )

    warnings: list[str] = []
    if disable_interruptions:
        # A no-op, and worse than a no-op: `_ToolExecutor.cancel` refuses a call
        # whose speech handle disallows interruptions, so it also takes the
        # model-facing cancel away.
        warnings.append(
            "'the caller cannot interrupt' has no effect on a background tool: the turn ends "
            "as soon as the work is dispatched."
        )
    return errors, warnings


async def validate_tool(tool_id: UUID, ctx: Context) -> ValidateResponse:
    pool = await ctx.tenant_pool()
    tool = await pool.fetchrow(
        "SELECT id, name, description, json_schema, long_running_task, silent, "
        "disable_interruptions, operations, "
        "published_version, created_at, updated_at, tenant_id "
        "FROM tools WHERE id = $1 AND tenant_id = $2",
        tool_id,
        ctx.tenant.id,
    )
    if not tool:
        raise HTTPException(status_code=404, detail="tool not found")
    tree = stored_tree(tool["operations"])
    if not tree:
        return ValidateResponse(
            errors=["add at least one operation before publishing"], warnings=[]
        )
    errors, warnings = await _validate_tool_for_publish(ctx, tool, tree)
    return ValidateResponse(errors=errors, warnings=warnings)


async def publish_tool(tool_id: UUID, body: PublishToolRequest, ctx: Context) -> PublishResponse:
    pool = await ctx.tenant_pool()
    tool = await pool.fetchrow(
        "SELECT id, name, description, json_schema, long_running_task, silent, "
        "disable_interruptions, operations, "
        "published_version, created_at, updated_at, tenant_id "
        "FROM tools WHERE id = $1 AND tenant_id = $2",
        tool_id,
        ctx.tenant.id,
    )
    if not tool:
        raise HTTPException(status_code=404, detail="tool not found")
    tree = stored_tree(tool["operations"])
    if not tree:
        raise HTTPException(status_code=400, detail="add at least one operation before publishing")

    errors, warnings = await _validate_tool_for_publish(ctx, tool, tree)
    if errors:
        raise validation_error(errors, "this tool has validation errors")

    definition = {
        "id": str(tool["id"]),
        "name": tool["name"],
        "description": tool["description"],
        "json_schema": tool["json_schema"] or {},
        "long_running_task": tool["long_running_task"],
        # Derived, and one-way: a tool whose every operation is silent IS silent,
        # whatever the box says. Frozen into the version rather than written back
        # to the draft, so adding a non-silent `http` later restores the author's
        # own choice instead of leaving a tick they never made.
        "silent": tool["silent"] or tree_is_silent(tree),
        "disable_interruptions": tool["disable_interruptions"],
        "operations": tree,
    }
    async with pool.acquire() as conn:
        async with conn.transaction():
            next_version = await conn.fetchval(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM tool_versions WHERE tool_id = $1 AND tenant_id = $2",
                tool_id,
                ctx.tenant.id,
            )
            ver = await conn.fetchrow(
                "INSERT INTO tool_versions "
                "(tenant_id, tool_id, version, definition, changelog, published_by) "
                "VALUES ($1, $2, $3, $4::jsonb, $5, $6) RETURNING version, published_at",
                ctx.tenant.id,
                tool_id,
                next_version,
                json.dumps(definition),
                body.changelog,
                ctx.user.id,
            )
            await conn.execute(
                "UPDATE tools SET published_version = $2, updated_at = now() WHERE id = $1 AND tenant_id = $3",
                tool_id,
                next_version,
                ctx.tenant.id,
            )
    return PublishResponse(
        tool_id=tool_id,
        version=ver["version"],
        published_at=ver["published_at"],
        warnings=warnings,
    )


# ─────────────────────────────────── versions ───────────────────────────────


async def _version_row(tool_id: UUID, version: int, ctx: Context) -> Row:
    pool = await ctx.tenant_pool()
    row = await pool.fetchrow(
        "SELECT version, changelog, published_at, definition FROM tool_versions "
        "WHERE tool_id = $1 AND version = $2 AND tenant_id = $3",
        tool_id,
        version,
        ctx.tenant.id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="tool version not found")
    return row


async def get_tool_version(tool_id: UUID, version: int, ctx: Context) -> ToolVersionDetailResponse:
    row = await _version_row(tool_id, version, ctx)
    definition: ToolDefinition = row["definition"]
    return ToolVersionDetailResponse(
        version=row["version"],
        changelog=row["changelog"],
        published_at=row["published_at"],
        name=definition["name"],
        description=definition["description"],
        json_schema=definition["json_schema"],
        operations=definition["operations"],
        long_running_task=definition["long_running_task"],
        silent=definition["silent"],
        disable_interruptions=definition["disable_interruptions"],
    )


async def rollback_tool_version(tool_id: UUID, version: int, ctx: Context) -> ToolResponse:
    row = await _version_row(tool_id, version, ctx)
    definition: ToolDefinition = row["definition"]
    pool = await ctx.tenant_pool()
    try:
        await pool.execute(
            "UPDATE tools SET name=$2, description=$3, json_schema=$4::jsonb, long_running_task=$5, "
            "silent=$6, disable_interruptions=$7, operations=$8::jsonb, published_version=$9, "
            # Not now(): the draft's content *is* what was saved at that moment, and
            # `updated_at` read against the live version's `published_at` is how a
            # caller tells a drafted tool from a clean one. Stamping now() would
            # leave every rolled-back tool permanently claiming an unpublished edit.
            "updated_at=$10 WHERE id=$1 AND tenant_id=$11",
            tool_id,
            definition["name"],
            definition["description"],
            definition["json_schema"],
            definition["long_running_task"],
            definition["silent"],
            definition["disable_interruptions"],
            source_only(definition["operations"]),
            row["version"],
            row["published_at"],
            ctx.tenant.id,
        )
    except asyncpg.UniqueViolationError:
        raise HTTPException(
            status_code=409,
            detail=(
                f"another tool is named '{definition['name']}' — "
                "rename it before rolling this one back"
            ),
        )
    return await get_tool(tool_id, ctx)
