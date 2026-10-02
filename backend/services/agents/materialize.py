"""Turn the inline tools, MCP servers, FAQs and tasks on an agent write into real rows.

A call carries an inline definition and throws it away afterwards. An **agent**
does not: `POST` / `PATCH /v1/agents` creates the tool, publishes v1 and stores
`{tool_id}` instead — because the first-class tool already has an editor, a test
panel, a ToolCoPilot, `run_tool`, version history and cross-agent reuse, and an
inline tool sitting in `agents.config` would have none of them, for ever, in the
one place the good version already exists.

An inline **task** is materialized the same way, and for the same reason. It
recurses: a task brought inline may itself bring its tools inline, and those
become rows before the task that uses them does.

It is also what makes "create the agent and its tools in one request" work,
which is the single biggest ergonomic win in a migration from another platform.

Everything happens inside the agent write's transaction, so a config that then
fails to store leaves no orphan tool behind.
"""

from __future__ import annotations

import json
from typing import Any, TypeVar
from uuid import UUID

import asyncpg
from fastapi import HTTPException

from services.faqs import InlineFaq, insert_faq
from services.tools import (
    Operation,
    inline_code_configs,
    operation_from_request,
    source_only,
    transpile_code_configs,
    tree_is_silent,
)
from services.tools.service import CODE_EXEC_UNREACHABLE
from services.user import Context

from .models import (
    AgentBase,
    AgentConfig,
    FaqSelection,
    InlineTool,
    McpSelection,
    TaskConfig,
    TaskSelection,
    ToolSelection,
)
from .pin import pin_tool_selections

_TOOL_HOOKS = ("on_enter", "on_exit", "on_user_turn_completed")

C = TypeVar("C", bound=AgentBase)


def _inline_tasks(config: AgentBase) -> list[TaskConfig]:
    """The tasks ``config`` brings inline — none unless it is an agent, and one
    level deep, which is all ``TaskConfig`` allows."""
    if not isinstance(config, AgentConfig):
        return []
    return [sel.task for sel in config.tasks if sel.task]


def _configs(config: AgentBase) -> list[AgentBase]:
    """``config`` and every task it brings inline: everything one write creates."""
    return [config, *_inline_tasks(config)]


def has_inline_definitions(config: AgentBase) -> bool:
    """Whether this write has anything to turn into a row.

    An inline **task** counts on its own, not only when it happens to bring
    inline tools with it. It is a definition that has to become a row for the
    same reason an inline tool does, and the commonest shape by far — a task with
    a prompt, an output and tools named by id — brings nothing else inline. The
    tool, MCP and FAQ walks look *through* it as well, which is what `_configs`
    is for.
    """
    return (
        bool(_inline_tasks(config))
        or any(sel.tool for cfg in _configs(config) for sel in cfg.tool_selections())
        or any(sel.mcp for cfg in _configs(config) for sel in cfg.mcps)
        or any(sel.faq for cfg in _configs(config) for sel in cfg.faqs)
    )


async def compile_inline_code(config: AgentBase, *, subject: str = "agent") -> None:
    """Compile every inline `code` op, in place, before the write begins.

    Outside the transaction on purpose: this is a network round trip to the
    code-execution service, and holding a database transaction across one is how
    a slow dependency turns into lock contention. The compiled JS lands on each
    authored `CodeConfig`, so it is already there when the tool below is
    converted and frozen into its version.

    ``subject`` names what is being saved in the error a failed transpile
    returns — a task write reaches this too, and "this agent config has
    problems" would be a sentence about something the caller never wrote.
    """
    configs = [
        code_config
        for cfg in _configs(config)
        for sel in cfg.tool_selections()
        if sel.tool
        for code_config in inline_code_configs(sel.tool.operations)
    ]
    if not configs:
        return
    errors = await transpile_code_configs(configs)
    if CODE_EXEC_UNREACHABLE in errors:
        raise HTTPException(status_code=502, detail="the code-execution service is unreachable")
    if errors:
        from api.core.schemas import validation_error

        raise validation_error(errors, f"this {subject} config has problems")


async def materialize_inline(
    conn: asyncpg.Connection, ctx: Context, config: C, *, subject: str = "agent"
) -> C:
    """Create + publish every inline definition and return the rewritten config.

    Rewrites to `{tool_id}` with **`tool_version` null**, not `{tool_id,
    tool_version: 1}`: a draft never pins — it tracks each tool's latest
    published version — and a materialized tool obeys that rule like any other.
    Pinning it here would freeze the agent's draft against v1 of a tool the
    author is about to keep editing. An inline task materializes to
    `{task_id}` under the same rule.
    """
    # Depth first: a task's own inline tools become rows, then the task does, so
    # the version this publishes already names them.
    task_ids: dict[int, str] = {}
    for task in _inline_tasks(config):
        task_ids[id(task)] = await _create_task(
            conn, ctx, await _materialize_one(conn, ctx, task, "task")
        )
    config = await _materialize_one(conn, ctx, config, subject)
    if not task_ids or not isinstance(config, AgentConfig):
        return config
    return config.model_copy(
        update={
            "tasks": [
                TaskSelection(
                    name=sel.name,
                    task_id=task_ids[id(sel.task)],
                    description=sel.description,
                    message=sel.message,
                )
                if sel.task
                else sel
                for sel in config.tasks
            ]
        }
    )


async def _materialize_one(conn: asyncpg.Connection, ctx: Context, config: C, subject: str) -> C:
    """One config's own inline tools, MCP servers and FAQs, without recursing."""
    tool_ids: dict[int, str] = {}
    for sel in config.tool_selections():
        if sel.tool is None:
            continue
        tool_ids[id(sel.tool)] = await _create_tool(conn, ctx, sel.tool, subject)

    integration_ids: dict[int, str] = {}
    for mcp_sel in config.mcps:
        if mcp_sel.mcp is None:
            continue
        integration_ids[id(mcp_sel.mcp)] = await _create_mcp(conn, ctx, mcp_sel.mcp)

    faq_ids: dict[int, UUID] = {}
    for faq_sel in config.faqs:
        if faq_sel.faq is None:
            continue
        faq_ids[id(faq_sel.faq)] = await _create_faq(conn, ctx, faq_sel.faq)

    if not tool_ids and not integration_ids and not faq_ids:
        return config

    def stored(sel: ToolSelection | None) -> ToolSelection | None:
        if sel is None or sel.tool is None:
            return sel
        return ToolSelection(tool_id=tool_ids[id(sel.tool)])

    return config.model_copy(
        update={
            "tools": [stored(sel) for sel in config.tools],
            "mcps": [
                McpSelection(integration_id=integration_ids[id(sel.mcp)]) if sel.mcp else sel
                for sel in config.mcps
            ],
            "faqs": [
                FaqSelection(faq_id=faq_ids[id(sel.faq)]) if sel.faq else sel for sel in config.faqs
            ],
            **{hook: stored(getattr(config, hook)) for hook in _TOOL_HOOKS},
        }
    )


async def _create_tool(
    conn: asyncpg.Connection, ctx: Context, tool: InlineTool, subject: str
) -> str:
    tree: list[Operation] = [operation_from_request(node) for node in tool.operations]
    try:
        row = await conn.fetchrow(
            """
            INSERT INTO tools (
                tenant_id, name, description, json_schema, long_running_task,
                silent, disable_interruptions, operations, published_version, created_by
            )
            VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7, $8::jsonb, 1, $9)
            RETURNING id
            """,
            ctx.tenant.id,
            tool.name,
            tool.description,
            json.dumps(tool.json_schema),
            tool.long_running_task,
            tool.silent,
            tool.disable_interruptions,
            json.dumps(source_only(tree)),
            ctx.user.id,
        )
    except asyncpg.UniqueViolationError as exc:
        raise HTTPException(
            status_code=409,
            detail=(
                f"a tool named '{tool.name}' already exists — "
                'reference it as {"tool_id": "…"}, or rename this one'
            ),
        ) from exc
    tool_id = row["id"]
    # The same frozen shape `publish_tool` writes, including the derived
    # silence: a tool whose every operation is silent IS silent, whatever the
    # box says. Written here rather than through `publish_tool` because that
    # re-reads the row through its own pool connection, which cannot see a row
    # this transaction has not committed.
    await conn.execute(
        """
        INSERT INTO tool_versions
            (tenant_id, tool_id, version, definition, changelog, published_by)
        VALUES ($1, $2, 1, $3::jsonb, $4, $5)
        """,
        ctx.tenant.id,
        tool_id,
        json.dumps(
            {
                "id": str(tool_id),
                "name": tool.name,
                "description": tool.description,
                "json_schema": tool.json_schema,
                "long_running_task": tool.long_running_task,
                "silent": tool.silent or tree_is_silent(tree),
                "disable_interruptions": tool.disable_interruptions,
                "operations": tree,
            }
        ),
        f"created with the {subject} that uses it",
        ctx.user.id,
    )
    return str(tool_id)


async def _create_mcp(conn: asyncpg.Connection, ctx: Context, server: Any) -> str:
    """One inline MCP server as the `custom_mcp` integration it behaves like.

    It lands in the integrations list, which is where its `allowed_tools` can be
    curated — and curating them is exactly what a durable server wants.
    """
    try:
        row = await conn.fetchrow(
            """
            INSERT INTO integrations (
                tenant_id, display_name, provider, mcp_config, allowed_tools,
                tools_namespace, created_by
            )
            VALUES ($1, $2, 'custom_mcp', $3::jsonb, $4, $5, $6)
            RETURNING id
            """,
            ctx.tenant.id,
            server.name,
            json.dumps({"url": server.url, "headers": dict(server.headers)}),
            server.allowed_tools,
            server.tools_namespace,
            ctx.user.id,
        )
    except asyncpg.UniqueViolationError as exc:
        raise HTTPException(
            status_code=409,
            detail=(
                f"an integration named '{server.name}' already exists — "
                'reference it as {"integration_id": "…"}, or rename this one'
            ),
        ) from exc
    return str(row["id"])


async def _create_faq(conn: asyncpg.Connection, ctx: Context, faq: InlineFaq) -> UUID:
    """One inline FAQ as the stored FAQ it behaves like, entries and all."""
    try:
        row = await insert_faq(conn, ctx, faq)
    except asyncpg.UniqueViolationError as exc:
        raise HTTPException(
            status_code=409,
            detail=(
                f"an FAQ named '{faq.name}' already exists — "
                'reference it as {"faq_id": "…"}, or rename this one'
            ),
        ) from exc
    return row["id"]


async def _create_task(conn: asyncpg.Connection, ctx: Context, task: TaskConfig) -> str:
    """One inline task as the stored, published task it behaves like.

    **Two shapes, exactly as `publish_task` writes them.** The draft row is what
    the author goes on to edit, so its tools stay unpinned and track whatever is
    published now; the version row is what a call actually runs, so its tools are
    pinned to the versions live at this moment. `resolve_pinned_tools` refuses an
    unpinned selection rather than reaching for whatever is published later, so
    storing the draft shape in both would fail every session on the agent that
    attaches this — at session start, before the agent speaks, not on the turn
    that enters the task.

    Published at v1 here rather than through `publish_task`, for the reason
    `_create_tool` freezes its own version: that function re-reads the row
    through its own pool connection, which cannot see a row this transaction has
    not committed. Pinning through `conn` is what lets the tools materialized a
    moment ago, in this same transaction, be found.
    """
    frozen = await pin_tool_selections(conn, ctx.tenant.id, task)
    try:
        task_id = await conn.fetchval(
            "INSERT INTO agent_tasks (tenant_id, name, config, published_version, created_by) "
            "VALUES ($1, $2, $3::jsonb, 1, $4) RETURNING id",
            ctx.tenant.id,
            task.name,
            task.model_dump_json(),
            ctx.user.id,
        )
    except asyncpg.UniqueViolationError as exc:
        raise HTTPException(
            status_code=409,
            detail=(
                f"a task named '{task.name}' already exists — "
                'reference it as {"task_id": "…"}, or rename this one'
            ),
        ) from exc
    await conn.execute(
        "INSERT INTO agent_task_versions (tenant_id, task_id, version, config, published_by) "
        "VALUES ($1, $2, 1, $3::jsonb, $4)",
        ctx.tenant.id,
        task_id,
        frozen.model_dump_json(),
        ctx.user.id,
    )
    return str(task_id)
