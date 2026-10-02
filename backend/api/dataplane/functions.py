"""The agent-facing function surface: one entry per exposed API endpoint.

This module is the single definition of what an AI builder — one of our own
CoPilots, or a user's Claude Code / Codex through the MCP server — is allowed to
do on the platform. Both surfaces are generated from it, so they cannot drift
apart:

- A CoPilot binds functions as LiveKit tools (``services.copilot.functions``) and
  dispatches in-process against the signed-in user's ``Context``.
- The MCP server serves ``mcp/tools.json``, exported from this same registry by
  ``openapi/export.py``, and executes each call over HTTP.

Tool name, description and argument schema all come from the route itself, so
documenting an endpoint documents the tool. Arguments mirror the HTTP call: path
and query parameters at the top level, request body under ``body``.

Adding a route does NOT expose it — ``EXPOSED`` is the security boundary and is
listed by hand.
"""

from __future__ import annotations

import asyncio
import inspect
import typing
from dataclasses import dataclass
from functools import cache
from typing import Any, Literal

import fastapi.params
from fastapi import BackgroundTasks, HTTPException
from fastapi.routing import APIRoute
from pydantic import BaseModel, TypeAdapter, ValidationError

from api.core.deps import Role, required_role
from api.core.schemas import field_errors, validation_error
from api.dataplane.deps import ChatAccess
from api.dataplane.sdk_surface import SURFACE
from services.user import ROLE_ADMIN, ROLE_EDITOR, Context
from utils import bg as bg_utils

# Functions an AI builder may call, by FastAPI operation id. Everything else on
# the API is invisible to it. The rule is that anything a user can do through
# the dashboard, an AI builder can do too — so this list grows with the API.
# Deliberately excluded:
#   - SSE/streaming endpoints (not tool-shaped): conversation event streams.
#     Their polling equivalent — `list_conversation_items` — is exposed, so
#     nothing is unreachable.
#   - The CoPilots' own chat endpoints — an agent must not drive itself or
#     another builder agent.
#   - Inbound provider and carrier webhooks: callbacks, not functions.
#   - EVERYTHING ON THE CONTROL PLANE, which is now a matter of which app this
#     module belongs to rather than a list. Sign-in, organizations, members,
#     roles, `patch_org` (a workspace-wide, irreversible retention setting) and
#     personal access tokens are all routes this app does not mount, so an AI
#     builder can no longer mint or revoke a credential and user management stays
#     where a human is deliberately in the loop. `me` went with them: keeping it
#     is what would have made the published client span two planes.
#   - Call recordings. `get_call_recording` is a 302 to an audio blob — not
#     tool-shaped, and `get_call` already reports `recording.state`, which is the
#     part an agent can reason about. `delete_call_recording` destroys a call
#     artifact that cannot be rebuilt, unlike the config a `delete_*` op removes.
#   - `delete_call` and `delete_chat`, for the same reason and more so: each
#     erases a session's whole content, and can take a contact's cross-call
#     memory with it.
#   - `delete_call_batch`, `delete_email_batch` and `delete_whatsapp_batch`,
#     likewise: each erases the record of who was contacted, which nothing
#     rebuilds. A person presses those.
#   - Everything under `/billing`. `create_credit_checkout` starts a payment, and
#     a CoPilot must not spend the workspace's money. Reading the balance or the
#     ledger is harmless but earns nothing: a CoPilot asked why a call failed
#     reads the call and finds `close_reason = insufficient_credits`, which is the
#     whole answer, and every schema added here is weight on an MCP surface
#     AGENTS.md already calls too large.
#   - `create_chat_token`: a browser's credential, which an AI builder has no
#     browser to hand to.
EXPOSED: tuple[str, ...] = (
    # agents
    "list_agents",
    "create_agent",
    "get_agent",
    "update_agent",
    "delete_agent",
    "validate_agent",
    "publish_agent",
    "get_agent_version",
    "rollback_agent_version",
    # tools
    "list_tools",
    "create_tool",
    "get_tool",
    "patch_tool",
    "delete_tool",
    "validate_tool",
    "run_tool",
    "publish_tool",
    "get_tool_version",
    "rollback_tool_version",
    # agent tasks. `run_task` is the one that spends money on the spot — it
    # executes for real, so its description says so.
    "list_tasks",
    "create_task",
    "get_task",
    "update_task",
    "delete_task",
    "validate_task",
    "publish_task",
    "get_task_version",
    "rollback_task_version",
    "run_task",
    "list_task_runs",
    "get_task_run",
    # integrations
    "list_integrations",
    "integration_catalog",
    "create_integration",
    "get_integration",
    "patch_integration",
    "delete_integration",
    "list_integration_mcp_tools",
    "list_integration_triggers",
    "create_integration_trigger",
    "patch_integration_trigger",
    "delete_integration_trigger",
    # faqs
    "list_faqs",
    "create_faq",
    "get_faq",
    "update_faq",
    "delete_faq",
    "create_faq_entries",
    "update_faq_entry",
    "delete_faq_entry",
    # telephony: carrier accounts
    "list_telephony_providers",
    "list_telephony_accounts",
    "create_telephony_account",
    "get_telephony_account",
    "patch_telephony_account",
    "delete_telephony_account",
    "provision_telephony_account",
    "list_remote_numbers",
    # telephony: phone numbers
    "list_phone_numbers",
    "import_phone_numbers",
    "get_phone_number",
    "patch_phone_number",
    "provision_phone_number",
    "assign_phone_number",
    "unassign_phone_number",
    # telephony: partner media streams
    "list_stream_connections",
    "create_stream_connection",
    "get_stream_connection",
    "patch_stream_connection",
    "delete_stream_connection",
    # running agents
    "create_outbound_call",
    # Batch outbound calling. `create_call_batch` is the most expensive
    # function on this list — one call can place thousands of billable dials —
    # so its description says so, and says the two things a tenant would
    # otherwise learn from an invoice: voicemail is billed unless the agent
    # detects it, and every call runs the agent's CURRENT published version.
    "create_call_batch",
    "list_call_batches",
    "get_call_batch",
    "patch_call_batch",
    "list_call_batch_recipients",
    "add_call_batch_recipients",
    "pause_call_batch",
    "resume_call_batch",
    "cancel_call_batch",
    # Email outbound. `create_email_batch` carries the two sentences a tenant
    # would otherwise learn from a suspension email — their provider's terms
    # forbid cold outreach, and every row runs their task's published version.
    # `create_email_send` is the one that mails real people, and its summary
    # says so; the five verbs under it exist so that a send an AI builder
    # created can be stopped by a person without one.
    "list_email_senders",
    "create_email_batch",
    "list_email_batches",
    "get_email_batch",
    "patch_email_batch",
    "pause_email_batch",
    "resume_email_batch",
    "cancel_email_batch",
    "list_email_batch_recipients",
    "add_email_batch_recipients",
    "patch_email_batch_recipient",
    "skip_email_batch_recipients",
    "restore_email_batch_recipients",
    "redraft_email_batch_recipients",
    "create_email_send",
    "list_email_sends",
    "get_email_send",
    "patch_email_send",
    "pause_email_send",
    "resume_email_send",
    "cancel_email_send",
    # WhatsApp outbound. `send_whatsapp_batch` and `resume_whatsapp_batch` are
    # left for a human to press. `add_whatsapp_batch_recipients` is exposed even
    # though appending to a sending or completed batch messages the new rows with
    # no further press — a founder decision, 2026-10-02.
    "list_whatsapp_templates",
    "create_whatsapp_batch",
    "list_whatsapp_batches",
    "get_whatsapp_batch",
    "patch_whatsapp_batch",
    "pause_whatsapp_batch",
    "cancel_whatsapp_batch",
    "list_whatsapp_batch_recipients",
    "add_whatsapp_batch_recipients",
    "patch_whatsapp_batch_recipient",
    "skip_whatsapp_batch_recipients",
    "restore_whatsapp_batch_recipients",
    "calls_token",
    # chats
    "create_chat",
    "list_chats",
    "get_chat",
    "get_chat_detail",
    "rerun_chat_analysis",
    "preview_chat_analysis",
    "list_chat_items",
    "create_chat_message",
    "end_chat",
    # calls and conversations
    "list_calls",
    "call_stats",
    "get_call",
    "preview_call_analysis",
    "backfill_call_analysis",
    "list_conversations",
    "get_conversation",
    "list_conversation_items",
    "list_conversation_sessions",
    "get_conversation_snapshot",
    "patch_conversation",
    # observability
    "get_observability",
    # secrets
    "list_secrets",
    "create_secret",
    "delete_secret",
    # webhooks
    "event_types",
    "list_webhooks",
    "create_webhook",
    "patch_webhook",
    "delete_webhook",
    "rotate_webhook_secret",
    "test_webhook",
    "webhook_deliveries",
    # catalog
    "get_catalog",
    "search_models",
    "list_model_hosts",
    "catalog_avatars",
    "catalog_voices",
    "add_elevenlabs_voice",
    "elevenlabs_voice_settings",
    # byok
    "list_provider_keys",
    "set_provider_key",
    "delete_provider_key",
    # schemas: the shapes the big request types defer to (see DEFERRED_SCHEMAS)
    "describe_schema",
    # …and the argument shapes every other function defers (see ALWAYS_INLINE)
    "describe_function",
)

# ToolCoPilot's slice. It sits in one tool's editor, so it gets every tool
# function, plus the reads a tree needs to name things it cannot invent: secret
# names for {{secrets.NAME}}, published agents for a `handoff` target, and
# integrations for an `http` op. This narrows EXPOSED and can never widen it —
# `registry()` fails at import if an entry here is not exposed at all.
TOOL_COPILOT_FUNCTIONS: frozenset[str] = frozenset(
    {
        "list_tools",
        "create_tool",
        "get_tool",
        "patch_tool",
        "delete_tool",
        "validate_tool",
        "run_tool",
        "publish_tool",
        "get_tool_version",
        "rollback_tool_version",
        "list_secrets",
        "list_agents",
        "list_integrations",
        "list_integration_mcp_tools",
        # Its operation trees defer, so it must be able to fetch the shape —
        # and every function on this slice defers its own arguments.
        "describe_schema",
        "describe_function",
    }
)


# TaskCoPilot's slice. It sits in one task's editor, so it gets every task
# function, plus the reads a config needs to name things it cannot invent:
# published tools to attach, integrations for `mcps`, FAQs for `faqs`, and secret
# names for {{secrets.NAME}}. Deliberately no tool WRITES — a task's tools are built in
# the tool editor, where the ToolCoPilot lives.
TASK_COPILOT_FUNCTIONS: frozenset[str] = frozenset(
    {
        "list_tasks",
        "create_task",
        "get_task",
        "update_task",
        "delete_task",
        "validate_task",
        "publish_task",
        "get_task_version",
        "rollback_task_version",
        "run_task",
        "list_task_runs",
        "get_task_run",
        "get_catalog",
        "list_provider_keys",
        "list_secrets",
        "list_tools",
        "get_tool",
        "list_integrations",
        "list_integration_mcp_tools",
        "list_faqs",
        # Its configs defer, so it must be able to fetch the shape — and every
        # function on this slice defers its own arguments.
        "describe_schema",
        "describe_function",
    }
)


# The type documents an AI builder fetches by name instead of reading inline.
#
# MCP gives no way to share `$defs` between tools — every `inputSchema` must
# stand alone — so a request type that appears on four endpoints is carried four
# times. Five of ours are whole product surfaces (an agent's config, its
# all-optional override twin, a team of them, a task's config, a tool's
# operation tree), and inlining them everywhere was 57% of a 230k-token tool
# surface that did not fit in a 200k-context client at all.
#
# So they are deferred, not duplicated: wherever one appears as a property the
# tool schema carries a stub pointing at `describe_schema`, which serves the
# real thing on demand.
#
# This changes the LLM-facing view ONLY. `openapi/openapi.json`, the generated
# clients and request validation all keep the full types — which is why this
# does not contradict `services.agents.override`'s argument against free-form
# objects: the OpenAPI document is untouched.
# Request fields an AI builder is not offered, as (schema, field) per function.
# `stream` answers a chat message as server-sent events, which a tool call
# cannot hold open; the same call without it returns the whole turn.
HIDDEN_BODY_FIELDS: dict[str, tuple[tuple[str, str], ...]] = {
    "create_chat_message": (("ChatMessageRequest", "stream"),),
}

SchemaName = Literal["AgentConfig", "AgentOverride", "AgentTeam", "TaskConfig", "ToolOperations"]

# Keyed on (containing schema, property) rather than on the target type, so a
# renamed model breaks the build instead of silently re-inlining 30k characters
# — `registry()` checks every entry against the OpenAPI document.
DEFERRED_SCHEMAS: dict[tuple[str, str], SchemaName] = {
    # Agent config, wherever a call accepts one.
    ("CreateAgentRequest", "config"): "AgentConfig",
    ("OutboundCallRequest", "agent"): "AgentConfig",
    ("TokenRequest", "agent"): "AgentConfig",
    ("CreateChatRequest", "agent"): "AgentConfig",
    ("CreateCallBatchRequest", "agent"): "AgentConfig",
    # Never fires in a tool schema — deferring `agent_team` takes
    # `AgentTeamMember` out of every tool's closure with it. It fires inside
    # `describe_schema('AgentTeam')`, which is what makes that document 2.3k
    # characters rather than 77k.
    ("AgentTeamMember", "agent"): "AgentConfig",
    # Its generated all-optional twin: one call's changes, or an update's.
    ("UpdateAgentRequest", "config"): "AgentOverride",
    ("OutboundCallRequest", "agent_override"): "AgentOverride",
    ("TokenRequest", "agent_override"): "AgentOverride",
    ("CreateChatRequest", "agent_override"): "AgentOverride",
    ("CreateCallBatchRequest", "agent_override"): "AgentOverride",
    ("AgentTeamMember", "agent_override"): "AgentOverride",
    # The team envelope, which composes both of the above.
    ("OutboundCallRequest", "agent_team"): "AgentTeam",
    ("TokenRequest", "agent_team"): "AgentTeam",
    ("CreateChatRequest", "agent_team"): "AgentTeam",
    ("CreateCallBatchRequest", "agent_team"): "AgentTeam",
    # Task config.
    ("CreateTaskRequest", "config"): "TaskConfig",
    ("PatchTaskRequest", "config"): "TaskConfig",
    # The operation tree — 11 discriminated variants plus their configs. The
    # `InlineTool` entry is what keeps `describe_schema('AgentConfig')` at 34k
    # rather than 48k: an agent may carry an inline tool with a whole tree in
    # it. `ToolSelection.tool` is deliberately not here — `InlineTool` is small
    # once its `operations` defer, and deferring the envelope too would lose the
    # shape for no gain.
    ("CreateToolRequest", "operations"): "ToolOperations",
    ("PatchToolRequest", "operations"): "ToolOperations",
    ("InlineTool", "operations"): "ToolOperations",
}

# Every function's argument schema is served by `describe_function` rather than
# inlined in its tool definition — except the two that would deadlock if they
# were, below.
#
# `DEFERRED_SCHEMAS` above solves the other half of the same problem: a type so
# large it must not be carried by the several functions that accept it. What is
# left after it are functions whose OWN request model is big — `create_call_batch`
# alone renders 4.0k characters of its own fields — and no shared-type deferral
# can touch those.
#
# All of them and not the large ones only, deliberately. A size threshold reads
# like the tighter rule but is the worse one: it puts a cliff in the middle of
# the surface, so a model learns that arguments are usually present and is then
# wrong about the dozen that are not — and which dozen changes whenever somebody
# documents a field. One rule the whole surface obeys is a rule the model can
# hold, and `parallel_tool_calls` is on for every CoPilot, so the shapes for a
# step get fetched side by side rather than one round trip each.

# The two exceptions, and the only two there can be: a function whose own
# arguments were deferred could not be called without first calling itself.
# `describe_schema` is here for the same reason one step removed — it is how a
# deferred *type* is fetched, so making it cost a lookup would put two hops in
# front of every agent config on the platform. Both take one string.
ALWAYS_INLINE: frozenset[str] = frozenset({"describe_function", "describe_schema"})

# The same two, named for the other thing that is true of them: a call to either
# is a CoPilot paging in a shape this module chose not to inline — bookkeeping
# against a token budget, never work on the user's workspace. That is why the
# editor rails leave them out of the timeline they render; see
# `services.copilot.items.hidden_from_rail`.
#
# Tied to ALWAYS_INLINE rather than listed again, because the two sets cannot
# come apart: a function that serves the deferral is exactly a function whose
# own arguments cannot defer.
DEFERRAL_FUNCTIONS: frozenset[str] = ALWAYS_INLINE

# Each tool schema stands alone, so component refs are rewritten to point at the
# tool's own `$defs`; the documents are generated against the same template.
_DEFS_PREFIX = "#/$defs/"
_COMPONENTS_PREFIX = "#/components/schemas/"


class ImmediateBg:
    """``BackgroundTasks`` stand-in for route functions called outside a request.

    FastAPI runs background tasks after the response is sent; there is no
    response here, so each task starts immediately. That is what a live-updating
    dashboard wants anyway — the ``agent.updated`` webhook fires as the edit
    lands.
    """

    def add_task(self, fn, *args, **kwargs) -> None:
        # spawn() holds a strong reference until the task finishes and logs its
        # exceptions; a bare create_task can be garbage-collected mid-flight.
        if asyncio.iscoroutinefunction(fn):
            bg_utils.spawn(fn(*args, **kwargs))
        else:
            bg_utils.spawn(asyncio.to_thread(fn, *args, **kwargs))


@dataclass(frozen=True)
class Function:
    """One exposed endpoint, ready to describe to an LLM and to invoke."""

    name: str
    method: str
    path: str
    # The whole route docstring. Served by `describe_function`, alongside the
    # argument schema, at the moment a caller is deciding how to call this.
    description: str
    # Its first paragraph, on one line. This is what a tool definition carries,
    # so it is the only thing a model has to go on when deciding whether it
    # wants this function at all — see `_summary`.
    summary: str
    role: Role
    # What the function's tool definition carries. Identical to
    # `full_parameters` unless the shape was too large to inline, in which case
    # it is a stub naming `describe_function`.
    parameters: dict[str, Any]
    # The whole argument schema, always — what `describe_function` serves.
    full_parameters: dict[str, Any]
    endpoint: typing.Callable[..., typing.Awaitable[Any]]
    # Endpoint parameter name -> resolved annotation, for path/query coercion.
    signature: dict[str, Any]

    @property
    def read_only(self) -> bool:
        return self.method == "GET"

    @property
    def defers_arguments(self) -> bool:
        """True when the caller has to fetch the shape before it can be right."""
        return self.parameters is not self.full_parameters


def _description(route: APIRoute, operation: dict[str, Any]) -> str:
    """The route docstring, out of its OpenAPI operation object."""
    text = (operation.get("description") or "").strip()
    if not text:
        raise RuntimeError(
            f"function '{route.name}' is exposed to AI builders but its route "
            "function has no docstring — the docstring is its tool description"
        )
    return text


# A summary is one sentence or two; anything longer is a docstring that has not
# decided which part is the headline. Generous enough that the limit only catches
# a genuine essay — the median today is 65 characters and the longest is 160.
MAX_SUMMARY_CHARS = 200


def _summary(name: str, description: str) -> str:
    """The one-line description a tool definition carries.

    The first PARAGRAPH, unwrapped — not the first line, which would cut most of
    these mid-sentence because docstrings wrap at 88 columns. Everything after it
    is still reachable through `describe_function`, so a docstring should lead
    with what the function is for and leave the caveats to later paragraphs.

    Across the surface this is most of what a tool list costs, and it is the
    whole budget a model has for deciding whether it needs a function, so the
    rules are enforced rather than hoped for.
    """
    summary = " ".join(description.strip().split("\n\n")[0].split())
    # Trailing emphasis is deliberate on a few of these — the warning on
    # `create_email_send` is the point of its summary, not decoration.
    if not summary.rstrip("*`").endswith((".", "?", "!")):
        raise RuntimeError(
            f"the first paragraph of '{name}' is not a sentence, so it reads as a "
            f"fragment wherever the function is listed: {summary!r}"
        )
    if len(summary) > MAX_SUMMARY_CHARS:
        raise RuntimeError(
            f"'{name}' opens with {len(summary)} characters, over "
            f"MAX_SUMMARY_CHARS ({MAX_SUMMARY_CHARS}). Lead with one sentence "
            "saying what it is for and move the rest into a second paragraph — "
            "`describe_function` still serves the whole docstring."
        )
    return summary


def _collect_refs(node: Any, into: set[str], prefix: str = _COMPONENTS_PREFIX) -> None:
    """Gather every schema name ``node`` points at under ``prefix``.

    Any string is a reference, not just a ``$ref`` value: a discriminated union
    also names its variants in ``discriminator.mapping``, and a closure that
    missed those would prune away a schema the tool still points at.
    """
    if isinstance(node, dict):
        for value in node.values():
            _collect_refs(value, into, prefix)
    elif isinstance(node, list):
        for value in node:
            _collect_refs(value, into, prefix)
    elif isinstance(node, str) and node.startswith(prefix):
        into.add(node[len(prefix) :])


def _rewrite_refs(node: Any) -> Any:
    """Point component refs at ``$defs`` so each tool schema stands alone.

    Every reference, ``discriminator.mapping`` included — one left pointing at
    ``#/components/schemas/`` dangles, because a tool schema has no components.
    """
    if isinstance(node, dict):
        return {key: _rewrite_refs(value) for key, value in node.items()}
    if isinstance(node, list):
        return [_rewrite_refs(value) for value in node]
    if isinstance(node, str) and node.startswith(_COMPONENTS_PREFIX):
        return _DEFS_PREFIX + node[len(_COMPONENTS_PREFIX) :]
    return node


def _defs_for(schemas: dict[str, Any], roots: list[Any]) -> dict[str, Any]:
    """Transitive closure of component schemas used by ``roots``."""
    pending: set[str] = set()
    for root in roots:
        _collect_refs(root, pending)
    resolved: dict[str, Any] = {}
    while pending:
        name = pending.pop()
        if name in resolved:
            continue
        schema = schemas[name]
        resolved[name] = _rewrite_refs(schema)
        found: set[str] = set()
        _collect_refs(schema, found)
        pending |= found - resolved.keys()
    return resolved


@cache
def _documents() -> dict[str, dict[str, Any]]:
    """Each deferred document's own JSON Schema, before deferral is applied.

    ``ToolOperations`` is the one name here that is not a model: the operation
    tree is a discriminated union, and what a request carries is a list of it.
    A ``TypeAdapter`` renders that as a self-contained document, so nothing has
    to be wrapped in a model invented for its sake — it only needs a name to be
    fetched by.
    """
    # Imported lazily, as registry() does: these modules import this one.
    from services.agents.models import AgentConfig
    from services.agents.override import AgentOverride
    from services.agents.plan import AgentTeam
    from services.tasks.models import TaskConfig
    from services.tools.defs import OperationRequest

    template = _DEFS_PREFIX + "{model}"
    documents: dict[str, dict[str, Any]] = {
        "AgentConfig": AgentConfig.model_json_schema(ref_template=template),
        "AgentOverride": AgentOverride.model_json_schema(ref_template=template),
        "AgentTeam": AgentTeam.model_json_schema(ref_template=template),
        "TaskConfig": TaskConfig.model_json_schema(ref_template=template),
        "ToolOperations": TypeAdapter(list[OperationRequest]).json_schema(ref_template=template),
    }
    if documents.keys() != set(typing.get_args(SchemaName)):
        raise RuntimeError("every SchemaName needs a document here, and vice versa")
    return documents


def schema_document(name: SchemaName) -> dict[str, Any]:
    """One deferred document, as ``describe_schema`` serves it.

    Rendered by the same two rules a tool schema is, so a property the model
    saw stubbed and the shape it fetches for it read as one dialect.
    """
    return _without_titles(_defer(_documents()[name]))


def _stub(schema: dict[str, Any], document: SchemaName) -> dict[str, Any]:
    """What a deferred property carries in place of its shape."""
    stub: dict[str, Any] = (
        {"type": "array", "items": {"type": "object"}}
        if _documents()[document]["type"] == "array"
        else {"type": "object"}
    )
    fetch = f"Full shape: describe_schema('{document}')."
    description = schema.get("description")
    stub["description"] = f"{description} {fetch}" if description else fetch
    return stub


def _defer(root: dict[str, Any]) -> dict[str, Any]:
    """Replace every deferred property under ``root`` with a stub, then prune.

    Applied to tool schemas and to the documents alike, which is what lets the
    documents compose instead of nesting: ``AgentTeam`` serves in 2.3k
    characters because the two documents it is built from defer out of it
    exactly as they do out of a tool.

    Only properties inside ``$defs`` are substituted. A tool's argument object
    is unnamed, so nothing could match it; and a document must never defer its
    own fields, or there would be nothing left to fetch.
    """
    defs = root.get("$defs")
    if not defs:
        return root

    substituted: dict[str, Any] = {}
    for name, schema in defs.items():
        properties = schema.get("properties") or {}
        stubs = {
            prop: _stub(properties[prop], document)
            for prop in properties
            if (document := DEFERRED_SCHEMAS.get((name, prop)))
        }
        substituted[name] = (schema | {"properties": properties | stubs}) if stubs else schema

    # Everything the stubs displaced is now unreferenced. That is the saving.
    body = {key: value for key, value in root.items() if key != "$defs"}
    pending: set[str] = set()
    _collect_refs(body, pending, _DEFS_PREFIX)
    reachable: dict[str, Any] = {}
    while pending:
        name = pending.pop()
        if name in reachable:
            continue
        reachable[name] = substituted[name]
        _collect_refs(substituted[name], pending, _DEFS_PREFIX)
    return body | {"$defs": reachable} if reachable else body


def _without_titles(node: Any) -> Any:
    """Drop every ``title`` keyword.

    Pydantic writes one for every property and every model, always the key
    title-cased (``max_steps`` -> "Max Steps"), which across the tool surface is ~7k
    characters saying nothing the key does not. ``properties`` and ``$defs`` map
    names to schemas rather than keywords to values, so a field actually called
    `title` survives. The OpenAPI document keeps them all; this is the
    LLM-facing view.
    """
    if isinstance(node, dict):
        kept: dict[str, Any] = {}
        for key, value in node.items():
            if key == "title":
                continue
            if key in ("properties", "$defs") and isinstance(value, dict):
                kept[key] = {name: _without_titles(schema) for name, schema in value.items()}
            elif key in ("default", "const", "enum", "examples"):
                kept[key] = value  # instance data, not a schema
            else:
                kept[key] = _without_titles(value)
        return kept
    if isinstance(node, list):
        return [_without_titles(value) for value in node]
    return node


def _parameters(operation: dict[str, Any], schemas: dict[str, Any]) -> dict[str, Any]:
    """Flatten an OpenAPI operation into one function-argument schema.

    Path and query parameters become top-level properties; the request body
    keeps its own shape under ``body``. Keeping the body nested means a path
    parameter can never collide with a field of the same name, and the arguments
    read exactly like the HTTP call they stand for.

    Two things make this an LLM-facing view rather than the API's own: the five
    documents in ``DEFERRED_SCHEMAS`` are replaced by a stub naming what to
    fetch, and Pydantic's auto-generated titles are dropped. ``schema_document``
    applies the same two, so the whole agent-facing surface is one dialect. The
    OpenAPI document this is built from is not touched by either.
    """
    properties: dict[str, Any] = {}
    required: list[str] = []
    roots: list[Any] = []

    for parameter in operation.get("parameters") or []:
        schema = dict(parameter["schema"])
        if parameter.get("description"):
            schema["description"] = parameter["description"]
        properties[parameter["name"]] = schema
        roots.append(schema)
        if parameter.get("required"):
            required.append(parameter["name"])

    body = (operation.get("requestBody") or {}).get("content", {}).get("application/json")
    if body:
        properties["body"] = body["schema"]
        roots.append(body["schema"])
        if (operation.get("requestBody") or {}).get("required"):
            required.append("body")

    parameters: dict[str, Any] = {
        "type": "object",
        "properties": _rewrite_refs(properties),
        "required": required,
        "additionalProperties": False,
    }
    defs = _defs_for(schemas, roots)
    if defs:
        parameters["$defs"] = defs
    return _without_titles(_defer(parameters))


def _arguments_stub(name: str) -> dict[str, Any]:
    """What a deferred function carries in place of its argument schema.

    Deliberately permissive rather than empty: a caller that already fetched the
    shape — or that is replaying a call it made earlier in the conversation —
    must be able to send the real arguments without another lookup. The Responses
    API normalizes a schema into strict mode only when it can, and falls back to
    best-effort for one like this, which is the behaviour this needs.

    Getting it wrong is cheap and self-correcting: `invoke` validates against the
    route exactly as it always did, and says where to find the shape.
    """
    return {
        "type": "object",
        "properties": {},
        "additionalProperties": True,
        # Names itself rather than saying "call describe_function first", so the
        # model never has to work out what to pass. Carrying the name on every
        # deferred function is not free, but it sits in the cached prefix and it
        # is the one place a caller is certain to read before calling.
        "description": f"Arguments not inlined: call describe_function('{name}').",
    }


@cache
def registry() -> dict[str, Function]:
    """Every exposed function, keyed by name. Built once per process."""
    # Imported lazily: api.dataplane.main imports the routers, which import services, and
    # services.copilot imports this module.
    from api.core.app import iter_routes
    from api.dataplane.main import app

    spec = app.openapi()
    schemas = spec.get("components", {}).get("schemas", {})
    # The OpenAPI document is the authority on each function's path and method,
    # and the route only has to supply the callable. Its operationId is
    # the dotted SDK id rather than the route name (api.core.app sets
    # generate_unique_id_function), so it is mapped back through the app's own
    # sdk_surface
    # — tool names here stay route names, which is what keeps this rename
    # invisible to MCP clients.
    endpoints = {route.name: route for route in iter_routes(app) if route.name in EXPOSED}
    located = {
        SURFACE.route_name(op["operationId"]): (path, method, op)
        for path, methods in spec["paths"].items()
        for method, op in methods.items()
        if isinstance(op, dict) and SURFACE.route_name(op["operationId"]) in EXPOSED
    }
    missing = [name for name in EXPOSED if name not in endpoints or name not in located]
    if missing:
        raise RuntimeError(f"exposed functions have no route: {', '.join(missing)}")
    unexposed = sorted(TOOL_COPILOT_FUNCTIONS - set(EXPOSED))
    if unexposed:
        raise RuntimeError(
            "TOOL_COPILOT_FUNCTIONS narrows EXPOSED, so every entry must be in it — "
            f"these are not: {', '.join(unexposed)}"
        )
    # Checked against the OpenAPI document rather than against the rendered
    # tools: deferral legitimately prunes some of these schemas out of every
    # tool, so a rendered-output check would report entries that are working.
    stale = sorted(
        f"{schema}.{prop}"
        for schema, prop in DEFERRED_SCHEMAS
        if prop not in (schemas.get(schema) or {}).get("properties", {})
    )
    if stale:
        raise RuntimeError(
            "DEFERRED_SCHEMAS is keyed on (schema, property) so that a rename cannot quietly "
            f"re-inline a whole document — these no longer exist: {', '.join(stale)}"
        )

    functions: dict[str, Function] = {}
    for name in EXPOSED:
        route = endpoints[name]
        path, method, spec_operation = located[name]
        signature = typing.get_type_hints(route.endpoint)
        full_parameters = _parameters(spec_operation, schemas)
        for schema_name, field in HIDDEN_BODY_FIELDS.get(name, ()):
            del full_parameters["$defs"][schema_name]["properties"][field]
        description = _description(route, spec_operation)
        functions[name] = Function(
            name=name,
            method=method.upper(),
            path=path,
            description=description,
            summary=_summary(name, description),
            role=required_role(route.endpoint, signature),
            parameters=(full_parameters if name in ALWAYS_INLINE else _arguments_stub(name)),
            full_parameters=full_parameters,
            endpoint=route.endpoint,
            signature=signature,
        )

    # Every slice has to be able to reach the shapes its own functions defer.
    # Cheap to state and it fails at import, which is what keeps it true after
    # somebody trims one of these lists.
    blind = sorted(
        kind
        for kind, slice_ in (("tool", TOOL_COPILOT_FUNCTIONS), ("task", TASK_COPILOT_FUNCTIONS))
        if not ALWAYS_INLINE <= slice_
    )
    if blind:
        raise RuntimeError(
            "every CoPilot slice must carry ALWAYS_INLINE, or its functions defer their "
            f"arguments to something it cannot call: {', '.join(blind)}"
        )
    return functions


def require_function(name: str) -> Function:
    """One exposed function by name, as `describe_function` looks it up.

    Answers for every exposed function, not only the deferred ones: a caller
    that asks for a shape it was already given has misremembered rather than
    misbehaved, and a 404 would teach it nothing.
    """
    function = registry().get(name)
    if function is None:
        raise HTTPException(status_code=404, detail=f"no function named '{name}'")
    return function


def _authorize(function: Function, ctx: Context) -> None:
    if function.role == "write" and ctx.user.role not in (ROLE_ADMIN, ROLE_EDITOR):
        raise HTTPException(
            status_code=403,
            detail="your role is view-only — ask an organization admin for editor access",
        )
    if function.role == "admin" and ctx.user.role != ROLE_ADMIN:
        raise HTTPException(status_code=403, detail="only an organization admin can do this")


def _invalid(message: str, exc: ValidationError) -> HTTPException:
    """The same {message, errors} a 422 from this endpoint would carry."""
    return validation_error(field_errors(exc.errors()), message)


def _hint(function: Function) -> str:
    """Appended to an argument error when the caller was never shown the shape.

    Carried in the message rather than as a field of its own, because every error
    this API raises is one `{message, errors}` and a second shape would be one
    more thing a client has to know. It is what makes deferral self-correcting:
    a caller that guessed rather than looked is told where to look, and its next
    attempt is right.
    """
    if not function.defers_arguments:
        return ""
    return f" — call describe_function('{function.name}') for the full argument shape"


def _body(function: Function, model: type[BaseModel], args: dict[str, Any], hint: str) -> BaseModel:
    payload = args.get("body", {})
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail=f"body must be a JSON object{hint}")
    hidden = [field for _, field in HIDDEN_BODY_FIELDS.get(function.name, ()) if field in payload]
    if hidden:
        raise HTTPException(
            status_code=400, detail=f"{', '.join(hidden)} is not available to a function call"
        )
    try:
        return model.model_validate(payload)
    except ValidationError as e:
        raise _invalid(f"invalid request body{hint}", e)


def _argument(name: str, annotation: Any, default: Any, args: dict[str, Any], hint: str) -> Any:
    """Coerce one path/query argument, or fall back to the route's default."""
    if name not in args:
        if isinstance(default, fastapi.params.Param):
            return default.default
        if default is inspect.Parameter.empty:
            raise HTTPException(status_code=400, detail=f"{name} is required{hint}")
        return default
    try:
        return TypeAdapter(annotation).validate_python(args[name])
    except ValidationError as e:
        raise _invalid(f"invalid {name}{hint}", e)


async def invoke(function: Function, ctx: Context, args: dict[str, Any]) -> Any:
    """Run one function as ``ctx``'s user and return the route's response model.

    The route's own callable is invoked directly, so a function behaves exactly
    as the HTTP endpoint does — same validation, same tenant scoping, same
    errors.
    """
    _authorize(function, ctx)

    hint = _hint(function)
    kwargs: dict[str, Any] = {}
    accepted: set[str] = set()
    for name, param in inspect.signature(function.endpoint).parameters.items():
        annotation = function.signature.get(name, param.annotation)
        if annotation is Context:
            kwargs[name] = ctx
        elif annotation is ChatAccess:
            kwargs[name] = ChatAccess(tenant=ctx.tenant, via_token=False)
        elif annotation is BackgroundTasks:
            kwargs[name] = ImmediateBg()
        elif isinstance(annotation, type) and issubclass(annotation, BaseModel):
            kwargs[name] = _body(function, annotation, args, hint)
            accepted.add("body")
        else:
            kwargs[name] = _argument(name, annotation, param.default, args, hint)
            accepted.add(name)

    unknown = sorted(set(args) - accepted)
    if unknown:
        raise HTTPException(
            status_code=400, detail=f"unknown argument(s): {', '.join(unknown)}{hint}"
        )
    return await function.endpoint(**kwargs)
