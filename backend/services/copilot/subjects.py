"""The CoPilots: one descriptor per thing a CoPilot can edit.

A CoPilot is a code-defined text agent that edits one resource in the editor the
user has open: AgentCoPilot an agent, ToolCoPilot a tool, TaskCoPilot an agent
task. Everything that differs between them is a field here; everything else —
storage, transport, the turn loop, the step cap — is shared.

None of them is a row in ``agents``: the runtime speaker is ``sentinel_id`` on
``TextTurnJob.requested_agent_id``, and what is being edited is
``conversation_refs.bind_id`` under this CoPilot's ``ref_kind``.

Storage: shared conversations / conversation_refs / conversation_items.
Never opens sessions; never billed.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from fastapi import HTTPException
from livekit.agents import llm

from api.dataplane.functions import TASK_COPILOT_FUNCTIONS, TOOL_COPILOT_FUNCTIONS
from compiler.integrations import exa_toolset
from services.conversations import ConversationRefKind
from services.user import Context
from settings import get_settings

from . import prompt
from .functions import build_copilot_functions

CopilotKind = Literal["agent", "tool", "task"]

Toolset = list[llm.Tool | llm.Toolset]


@dataclass(frozen=True)
class CopilotSubject:
    """One CoPilot: what it edits, where its chat lives, how it is prompted."""

    # Namespaces the SSE channel and names the route segment (/copilot/{kind}/…).
    kind: CopilotKind
    # User-facing name, used in errors and logs.
    label: str
    # Fixed sentinel used as TextTurnJob.requested_agent_id.
    sentinel_id: UUID
    # conversation_refs.kind; doubles as the conversation_key prefix.
    ref_kind: ConversationRefKind
    # conversation_items.source for the user's message, and for agent items.
    api_source: str
    item_source: str
    # Loads the subject and returns its display name, or raises 404. One query
    # serves both the HTTP guard and the worker's prompt setup.
    require: Callable[[UUID, Context], Awaitable[str]]
    # The slice of the exposed API surface this CoPilot may call; None is all
    # of it.
    functions: frozenset[str] | None
    # The CoPilot's whole system prompt. Deliberately takes no arguments: it is
    # the head of every request this CoPilot makes, and a provider prompt cache
    # only reuses a byte-identical prefix, so one interpolated name would give
    # every conversation a prefix of its own. What is being edited is named at
    # the top of `state_block`, which is appended past the cached prefix.
    instructions: Callable[[], str]
    # Live state re-read before every model call within a turn.
    state_block: Callable[[Context, UUID], Awaitable[str]]

    def build_tools(self, ctx: Context) -> Toolset:
        """Its API slice, plus web search and page reading — paid for like the
        CoPilot's model: on Talqing's own key, never a tenant's."""
        web = exa_toolset(get_settings().providers.exa.api_key, toolset_id="copilot_web")
        return [*build_copilot_functions(ctx, self.functions), web]


# ── AgentCoPilot ────────────────────────────────────────────────────────────


async def _require_agent(subject_id: UUID, ctx: Context) -> str:
    pool = await ctx.tenant_pool()
    name = await pool.fetchval(
        "SELECT name FROM agents WHERE id = $1 AND tenant_id = $2",
        subject_id,
        ctx.tenant.id,
    )
    if name is None:
        raise HTTPException(status_code=404, detail="agent not found")
    return str(name)


AGENT_COPILOT = CopilotSubject(
    kind="agent",
    label="AgentCoPilot",
    sentinel_id=UUID("00000000-0000-4000-8000-0000000000c0"),
    ref_kind="agent_copilot",
    api_source="agent_copilot_api",
    item_source="agent_copilot",
    require=_require_agent,
    # Every exposed operation: an agent reaches the whole workspace.
    functions=None,
    instructions=lambda: prompt.agent_copilot.system_prompt() + "\n\n" + prompt.provider_prompt(),
    state_block=lambda ctx, subject_id: prompt.agent_copilot.state_block(ctx, str(subject_id)),
)


# ── ToolCoPilot ─────────────────────────────────────────────────────────────


async def _require_tool(subject_id: UUID, ctx: Context) -> str:
    pool = await ctx.tenant_pool()
    name = await pool.fetchval(
        "SELECT name FROM tools WHERE id = $1 AND tenant_id = $2",
        subject_id,
        ctx.tenant.id,
    )
    if name is None:
        raise HTTPException(status_code=404, detail="tool not found")
    return str(name)


TOOL_COPILOT = CopilotSubject(
    kind="tool",
    label="ToolCoPilot",
    sentinel_id=UUID("00000000-0000-4000-8000-0000000000c1"),
    ref_kind="tool_copilot",
    api_source="tool_copilot_api",
    item_source="tool_copilot",
    require=_require_tool,
    functions=TOOL_COPILOT_FUNCTIONS,
    # No provider catalog: ToolCoPilot picks no models.
    instructions=prompt.tool_copilot.system_prompt,
    state_block=lambda ctx, subject_id: prompt.tool_copilot.state_block(ctx, str(subject_id)),
)


# ── TaskCoPilot ─────────────────────────────────────────────────────────────


async def _require_task(subject_id: UUID, ctx: Context) -> str:
    pool = await ctx.tenant_pool()
    name = await pool.fetchval(
        "SELECT name FROM agent_tasks WHERE id = $1 AND tenant_id = $2",
        subject_id,
        ctx.tenant.id,
    )
    if name is None:
        raise HTTPException(status_code=404, detail="task not found")
    return str(name)


TASK_COPILOT = CopilotSubject(
    kind="task",
    label="TaskCoPilot",
    sentinel_id=UUID("00000000-0000-4000-8000-0000000000c2"),
    ref_kind="task_copilot",
    api_source="task_copilot_api",
    item_source="task_copilot",
    require=_require_task,
    functions=TASK_COPILOT_FUNCTIONS,
    # The provider catalog comes with it: a task picks a model, and a
    # `reasoning_effort` the entry does not offer is a save error.
    instructions=prompt.task_copilot.system_prompt,
    state_block=lambda ctx, subject_id: prompt.task_copilot.state_block(ctx, str(subject_id)),
)


SUBJECTS: dict[CopilotKind, CopilotSubject] = {
    subject.kind: subject for subject in (AGENT_COPILOT, TOOL_COPILOT, TASK_COPILOT)
}

SUBJECTS_BY_SENTINEL: dict[UUID, CopilotSubject] = {
    subject.sentinel_id: subject for subject in SUBJECTS.values()
}
