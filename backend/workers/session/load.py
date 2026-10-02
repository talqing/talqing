"""Load tenants, published definitions, tools, hooks."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

import db
from services import byok
from services.agents import (
    AgentBase,
    AgentConfig,
    FaqSelection,
    InlineMcpServer,
    McpSelection,
    PinnedTask,
    TaskConfig,
    ToolSelection,
)
from services.faqs import FaqForPrompt, load_stored_faqs
from services.integrations.models import INTEGRATION_COLUMNS, Integration
from services.secrets import load_secrets
from services.tools import (
    OperationTree,
    ToolDefinition,
    hook_trees_from_defs,
    operation_from_request,
)
from services.user import Tenant

logger = logging.getLogger("talqing.workers.session")

# A stable, collision-free id for an inline MCP server, derived from its name so
# the LiveKit toolset id stays put for the life of the call. Nothing resolves it.
_INLINE_MCP_NAMESPACE = uuid5(NAMESPACE_URL, "https://talqing.com/inline-mcp")

from workers.session._util import _frozen_tool_definitions


def select_tool_definitions(
    tool_defs: list[ToolDefinition], tools: list[ToolSelection]
) -> list[ToolDefinition]:
    """Return definitions in config order without exposing hook-only tools.

    A stored selection looks its frozen definition up; an inline one carries the
    definition already and passes straight through.
    """
    by_id: dict[str, ToolDefinition] = {d["id"]: d for d in tool_defs if d["id"]}
    missing = [sel.tool_id for sel in tools if sel.tool_id and sel.tool_id not in by_id]
    if missing:
        raise ValueError(f"agent references tool id(s) with no frozen definition: {missing}")
    return [by_id[sel.tool_id] if sel.tool_id else inline_tool_definition(sel) for sel in tools]


async def load_tool_secrets(tenant: Tenant) -> dict[str, str]:
    """Decrypt the tenant's tool credentials into {name: value} for runtime
    {{secrets.NAME}} substitution (in-process only, never logged)."""
    return await load_secrets(tenant)


async def load_provider_keys(tenant: Tenant) -> dict[str, str]:
    """Decrypt the tenant's BYOK AI-provider keys into {provider: api_key}.

    Every agent run compiles from these — Talqing has no platform key to fall
    back on, so a session whose provider is missing here fails to compile.
    """
    return await byok.load_provider_keys(tenant)


async def _load_tool_versions(tenant: Tenant, tool_ids: list[str]) -> list[ToolDefinition]:
    """Resolve each attached tool id to its current published_version definition.

    For callers holding an *unpinned* config — a draft. A frozen agent version
    pins its tools, and must go through `resolve_pinned_tools` instead, or the
    call would silently pick up a tool republished since the agent was published.

    Missing/unpublished ids raise — callers that only want present defs must
    filter the id list first. Order follows `tool_ids`.
    """
    if not tool_ids:
        return []
    pool = await db.tenant_pool(tenant)
    rows = await pool.fetch(
        """
        SELECT t.id AS tool_id, tv.definition
        FROM tools t
        JOIN tool_versions tv
            ON tv.tool_id = t.id
            AND tv.tenant_id = t.tenant_id
            AND tv.version = t.published_version
        WHERE t.id = ANY($1::uuid[]) AND t.tenant_id = $2
            AND t.published_version IS NOT NULL
        """,
        tool_ids,
        tenant.id,
    )
    by_id: dict[str, ToolDefinition] = {}
    for r in rows:
        defs = _frozen_tool_definitions([r["definition"]])
        by_id[str(r["tool_id"])] = defs[0]
    missing = [tid for tid in tool_ids if tid not in by_id]
    if missing:
        raise ValueError(f"tool id(s) have no published definition: {missing}")
    return [by_id[tid] for tid in tool_ids]


def inline_tool_definition(sel: ToolSelection) -> ToolDefinition:
    """An inline tool as the runtime shape a frozen version would have held.

    `id` is None: this definition has no `tools` row and inventing one would put
    a phantom id in the transcript, the call detail and every tool webhook. The
    `code` ops already carry their `compiled_js` — `resolve_call_plan` compiled
    them at create time, exactly as publishing does — so the worker never
    transpiles.
    """
    tool = sel.tool
    if tool is None:
        raise ValueError("inline_tool_definition called on a stored tool selection")
    return {
        "id": None,
        "name": tool.name,
        "description": tool.description,
        "json_schema": tool.json_schema,
        "long_running_task": tool.long_running_task,
        "silent": tool.silent,
        "disable_interruptions": tool.disable_interruptions,
        "operations": [operation_from_request(node) for node in tool.operations],
    }


async def resolve_pinned_tools(tenant: Tenant, cfg: AgentBase) -> list[ToolDefinition]:
    """The definitions this config's tools resolve to — attached tools and hooks.

    A stored selection must be pinned by the time it reaches here: publishing
    pins, and so does `resolve_call_plan`, so an unpinned one means the config
    took a path that skipped both. `unnest` pairs each id with its own version,
    so this matches the exact (tool_id, version) rows rather than every version
    of every id. An inline tool is already the definition.
    """
    selections = cfg.tool_selections()
    if not selections:
        return []
    inline = {id(sel): inline_tool_definition(sel) for sel in selections if sel.tool is not None}
    stored = [sel for sel in selections if sel.tool_id]
    unpinned = [sel.tool_id for sel in stored if sel.tool_version is None]
    if unpinned:
        raise ValueError(f"agent config has unpinned tool(s): {unpinned}")
    by_id: dict[str, ToolDefinition] = {}
    if stored:
        pool = await db.tenant_pool(tenant)
        rows = await pool.fetch(
            """
            SELECT tv.tool_id, tv.definition
            FROM unnest($2::uuid[], $3::int[]) AS p(tool_id, version)
            JOIN tool_versions tv
                ON tv.tool_id = p.tool_id AND tv.version = p.version AND tv.tenant_id = $1
            """,
            tenant.id,
            [sel.tool_id for sel in stored],
            [sel.tool_version for sel in stored],
        )
        by_id = {str(r["tool_id"]): _frozen_tool_definitions([r["definition"]])[0] for r in rows}
        missing = [
            f"{sel.tool_id} v{sel.tool_version}" for sel in stored if sel.tool_id not in by_id
        ]
        if missing:
            # The tool was deleted after this version was published, taking its
            # versions with it. Nothing sane to run — say which one is gone.
            raise ValueError(f"agent version pins tool version(s) that no longer exist: {missing}")
    return [by_id[sel.tool_id] if sel.tool_id else inline[id(sel)] for sel in selections]


async def resolve_pinned_tasks(tenant: Tenant, cfg: AgentConfig) -> list[PinnedTask]:
    """The tasks this agent may enter, resolved to what entering one needs.

    Modelled on `resolve_pinned_tools`, and read at the same moment — once, at
    session start — so entering a task mid-call costs no database round trip for
    its definition or its tools. A stored selection must be pinned by the time it
    reaches here: publishing pins, and so does `resolve_call_plan`, so an
    unpinned one means the config took a path that skipped both.

    An inline task carries its own definition and its own (already inline) tools.
    """
    if not cfg.tasks:
        return []
    stored = [sel for sel in cfg.tasks if sel.task_id]
    unpinned = [sel.task_id for sel in stored if sel.task_version is None]
    if unpinned:
        raise ValueError(f"agent config has unpinned task(s): {unpinned}")
    by_id: dict[str, TaskConfig] = {}
    if stored:
        pool = await db.tenant_pool(tenant)
        rows = await pool.fetch(
            """
            SELECT v.task_id, v.config
            FROM unnest($2::uuid[], $3::int[]) AS p(task_id, version)
            JOIN agent_task_versions v
                ON v.task_id = p.task_id AND v.version = p.version AND v.tenant_id = $1
            """,
            tenant.id,
            [sel.task_id for sel in stored],
            [sel.task_version for sel in stored],
        )
        by_id = {str(r["task_id"]): TaskConfig.model_validate(r["config"]) for r in rows}
        missing = [
            f"{sel.task_id} v{sel.task_version}" for sel in stored if sel.task_id not in by_id
        ]
        if missing:
            # The task was deleted after this version was published, taking its
            # versions with it. `delete_task` refuses exactly this, so reaching
            # here means the row went away another way — say which one is gone
            # rather than starting a call whose tool cannot run.
            raise ValueError(f"agent version pins task version(s) that no longer exist: {missing}")

    out: list[PinnedTask] = []
    for sel in cfg.tasks:
        config = by_id[sel.task_id] if sel.task_id else sel.task
        assert config is not None  # TaskSelection requires exactly one side
        out.append(
            PinnedTask(
                selection=sel,
                config=config,
                tools=await resolve_pinned_tools(tenant, config),
                task_id=sel.task_id,
                version=sel.task_version,
            )
        )
    return out


async def load_hook_trees(
    tenant: Tenant, cfg: AgentBase, tool_defs: list[ToolDefinition] | None = None
) -> dict[str, OperationTree]:
    """Resolve the agent's hook tool ids (on_enter/on_exit/on_user_turn_completed)
    to frozen published operation trees."""
    hooks = [h for h in (cfg.on_enter, cfg.on_exit, cfg.on_user_turn_completed) if h]
    if not hooks:
        return {}
    if tool_defs is None:
        tool_defs = await _load_tool_versions(tenant, [h.tool_id for h in hooks if h.tool_id])
        tool_defs = [*tool_defs, *(inline_tool_definition(h) for h in hooks if h.tool)]
    return hook_trees_from_defs(cfg, tool_defs)


async def load_mcp_integrations(
    tenant: Tenant, selections: Sequence[McpSelection]
) -> list[Integration]:
    """The MCP servers a config names or brings, in the order it lists them.

    For a named one, status is live rather than frozen: an integration disabled
    after the agent was published drops out here rather than failing the
    session, the same way it did when this read a join table. Validation only
    warns about a non-active one, so this is where it is actually left out.

    An inline server has no row and therefore no status to check — it is exactly
    as active as the request that carried it.
    """
    from services.integrations import TOOL_PROVIDER_SET

    if not selections:
        return []
    named = [sel.integration_id for sel in selections if sel.integration_id]
    by_id: dict[UUID, Integration] = {}
    if named:
        pool = await db.tenant_pool(tenant)
        rows = await pool.fetch(
            f"""
            SELECT {INTEGRATION_COLUMNS}
            FROM integrations
            WHERE tenant_id = $1
                AND id = ANY($2::uuid[])
                AND status = 'active'
                AND provider = ANY($3::text[])
            """,
            tenant.id,
            named,
            list(TOOL_PROVIDER_SET),
        )
        by_id = {row["id"]: Integration.from_row(row) for row in rows}
        missing = [iid for iid in named if iid not in by_id]
        if missing:
            # Deleted, disabled, or no longer MCP-capable. Not fatal — the
            # session runs with the servers that did resolve — but never silent:
            # before the attachment moved into the config this case vanished
            # with no log at all.
            logger.warning(
                "agent config names %d unresolvable MCP integration(s): %s", len(missing), missing
            )
    out: list[Integration] = []
    for sel in selections:
        if sel.mcp is not None:
            out.append(inline_integration(tenant, sel.mcp))
        elif sel.integration_id in by_id:
            out.append(by_id[sel.integration_id])
    return out


async def load_faqs(tenant: Tenant, selections: Sequence[FaqSelection]) -> list[FaqForPrompt]:
    """The FAQs a config names or brings, in the order it lists them.

    Questions AND answers, read once, here: the tool that returns an answer
    then never touches the database mid-call, and what it returns is the answer
    to the question the prompt showed. Content is live rather than frozen — an
    edit made since the agent was published is what this reads.

    An inline FAQ is used as written. An FAQ with no entries contributes
    nothing, so it is left out.
    """
    if not selections:
        return []
    named = [sel.faq_id for sel in selections if sel.faq_id]
    stored = await load_stored_faqs(tenant, named) if named else {}
    if missing := [faq_id for faq_id in named if faq_id not in stored]:
        # `delete_faq` refuses while a draft or the live version names it, so
        # this is an older version or a stored call plan. Not fatal — the
        # session runs with the FAQs that did resolve — but never silent.
        logger.warning("agent config names %d unresolvable FAQ(s): %s", len(missing), missing)
    out: list[FaqForPrompt] = []
    for sel in selections:
        if sel.faq is not None:
            out.append(
                FaqForPrompt(
                    name=sel.faq.name, entries=[(e.question, e.answer) for e in sel.faq.entries]
                )
            )
        elif sel.faq_id in stored:
            out.append(stored[sel.faq_id])
    return [faq for faq in out if faq.entries]


def inline_integration(tenant: Tenant, server: InlineMcpServer) -> Integration:
    """An inline MCP server as the `custom_mcp` integration it behaves like.

    The compiler only ever reads `provider`, `mcp_config` and `allowed_tools`
    off one of these, so building the domain model here is what lets
    ``compiler.integrations._build_custom_mcp`` — URL resolution, the SSRF
    guard, the `{{secrets.NAME}}` headers, the allow-list filter — run unchanged
    for a server that has no row.

    `id` is derived from the name so the LiveKit toolset id is stable for the
    life of the call; nothing looks it up, because there is nothing to look up.
    """
    now = datetime.now(UTC)
    return Integration(
        id=uuid5(_INLINE_MCP_NAMESPACE, server.name),
        tenant_id=tenant.id,
        display_name=server.name,
        provider="custom_mcp",
        auth_type="manual",
        mcp_config={"url": server.url, "headers": dict(server.headers)},
        allowed_tools=server.allowed_tools,
        tools_namespace=server.tools_namespace,
        status="active",
        created_at=now,
        updated_at=now,
    )


async def load_session_agent_plan(
    tenant: Tenant, session_id: UUID | str
) -> tuple[dict[str, Any] | None, dict[str, str]]:
    """The cast this call runs and the `{{vars.*}}` values it was started with.

    The plan is None when the call runs the entry agent's published version —
    which is every call that named an `agent_id` and stopped there.

    Both are written before dispatch by whoever started the call (`calls_token`,
    `record_outbound_call`), which is why the row is pre-minted: the LiveKit
    RoomConfiguration metadata is embedded in the JWT the browser receives, and
    neither a multi-KB plan nor a bag of tenant configuration belongs in
    something base64-encoded and handed to a browser. One read, two columns: an
    inbound PSTN call has no row yet and gets `(None, {})`, which is correct —
    its agents' declared defaults stand alone.
    """
    pool = await db.tenant_pool(tenant)
    row = await pool.fetchrow(
        "SELECT agent_plan, vars FROM sessions WHERE id = $1 AND tenant_id = $2",
        UUID(str(session_id)),
        tenant.id,
    )
    return (row["agent_plan"], row["vars"] or {}) if row else (None, {})


async def load_definition(
    tenant: Tenant, agent_id: str, version: int
) -> tuple[AgentConfig, str, list[ToolDefinition]] | None:
    """Returns (definition, agent_version_id, the tool definitions it pinned)."""
    pool = await db.tenant_pool(tenant)
    row = await pool.fetchrow(
        "SELECT id, config FROM agent_versions WHERE agent_id = $1 AND version = $2 "
        "AND tenant_id = $3",
        agent_id,
        version,
        tenant.id,
    )
    if not row:
        return None
    # config is JSONB → the connection codec already decoded it to a dict
    cfg = AgentConfig.model_validate(row["config"])
    return (
        cfg,
        str(row["id"]),
        await resolve_pinned_tools(tenant, cfg),
    )


async def load_published_definition(
    tenant: Tenant, agent_id: str
) -> tuple[AgentConfig, int, list[ToolDefinition]] | None:
    """Load the latest published agent version, including the tools it pinned.

    Handoff targets intentionally enter the current published version of the target
    agent; version-pinned call tokens use `load_definition` instead.
    """
    pool = await db.tenant_pool(tenant)
    row = await pool.fetchrow(
        """
        SELECT av.version, av.config
        FROM agents a
        JOIN agent_versions av ON av.agent_id = a.id AND av.version = a.published_version
            AND av.tenant_id = a.tenant_id
        WHERE a.id = $1 AND a.tenant_id = $2
        """,
        agent_id,
        tenant.id,
    )
    if not row:
        return None
    cfg = AgentConfig.model_validate(row["config"])  # JSONB → already a dict
    return (
        cfg,
        row["version"],
        await resolve_pinned_tools(tenant, cfg),
    )
