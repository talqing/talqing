"""A tenant chat's warm window: who answers, and how the window is (re)built.

The shared turn loop does the rest.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from livekit.agents import AgentSession, io, llm

import db
from compiler.compile import compile_agent
from compiler.handoff import build_handoff_agent
from compiler.operations import (
    RUNTIME_KEY_AGENT_ROSTER,
    RUNTIME_KEY_BUILD_HANDOFF_AGENT,
    RUNTIME_KEY_HANDOFF_TARGETS,
    RUNTIME_KEY_PROVIDER_KEYS,
    RUNTIME_KEY_RECORD_EVENT,
    RUNTIME_KEY_SESSION_VARS,
    RUNTIME_KEY_TOOL_SECRETS,
)
from services import conversations, credits
from services.agents import AgentConfig
from services.agents.plan import load_plan_roster
from services.conversations.models import AssistantDeltaEvent, AssistantStartedEvent
from services.messaging import TextTurnJob
from services.tools import HandoffTarget, ToolDefinition
from services.user import Tenant
from services.userdata import is_reserved_key
from workers.session import persistence
from workers.session.events import SessionEventLog
from workers.session.trace import wire_session_trace
from workers.text.planes import TENANT_PLANE
from workers.text.types import TextWindow
from workers.text.window import emit_session_started

# `summary` mode's one system message, worded for a chat. The call's version is
# `workers.voice.runtime._SUMMARY_BLOCK_HEADER`.
_SUMMARY_BLOCK_HEADER = (
    "Summaries of your earlier conversations with this person, oldest first. This is "
    "background only: none of it was said in the chat you are in now, and the person "
    "has not seen any of it in this chat."
)


class TurnRefused(Exception):
    """A turn that will not run, for a reason the sender should read."""


@dataclass(frozen=True, slots=True)
class Chat:
    """The `sessions` row a tenant job belongs to, as the worker reads it."""

    id: str
    conversation_id: UUID
    conversation_ref_id: UUID | None
    status: str
    started_at: datetime | None
    ended_at: datetime | None
    close_reason: str | None
    # The entry agent. None when it was defined in the request that started the chat.
    agent_id: str | None
    agent_plan: dict[str, Any] | None
    vars: dict[str, str]
    # What the chat knows so far: what it was started with, then whatever each
    # turn left behind. The next warm window starts from it.
    userdata: dict[str, Any]

    @property
    def ended(self) -> bool:
        return self.status not in ("queued", "running")


async def load_chat(tenant: Tenant, session_id: UUID | str) -> Chat | None:
    pool = await db.tenant_pool(tenant)
    row = await pool.fetchrow(
        """
        SELECT id, conversation_id, conversation_ref_id, status, started_at, ended_at,
               close_reason, agent_id, agent_plan, vars, userdata
        FROM sessions
        WHERE id = $1 AND tenant_id = $2 AND channel = 'text'
        """,
        UUID(str(session_id)),
        tenant.id,
    )
    if row is None or row["conversation_id"] is None:
        return None
    return Chat(
        id=str(row["id"]),
        conversation_id=row["conversation_id"],
        conversation_ref_id=row["conversation_ref_id"],
        status=row["status"],
        started_at=row["started_at"],
        ended_at=row["ended_at"],
        close_reason=row["close_reason"],
        agent_id=str(row["agent_id"]) if row["agent_id"] else None,
        agent_plan=row["agent_plan"],
        vars=row["vars"] or {},
        userdata=row["userdata"] or {},
    )


class ConversationTextOutput(io.TextOutput):
    """Forwards LiveKit text deltas to the tenant conversation SSE bus.

    Used for web text agents only (token streaming). CoPilots do not
    attach this output — they stream complete steps via item fan-out instead.
    """

    def __init__(self, job: TextTurnJob) -> None:
        super().__init__(label="talqing-conversation-sse", next_in_chain=None)
        self._job = job
        self._started = False
        self.first_text_at: float | None = None

    async def capture_text(self, text: str) -> None:
        if not text:
            return
        if not self._started:
            self._started = True
            import time

            self.first_text_at = time.time()
            await conversations.publish(
                self._job.tenant_id,
                self._job.conversation_id,
                AssistantStartedEvent(trigger_item_id=self._job.input_item_id),
            )
        await conversations.publish(
            self._job.tenant_id,
            self._job.conversation_id,
            AssistantDeltaEvent(trigger_item_id=self._job.input_item_id, text=text),
        )

    def flush(self) -> None:
        return


@dataclass(frozen=True, slots=True)
class TextEntryAgent:
    """One agent a chat can run, resolved: its config, its tools, its cast."""

    config: AgentConfig
    version: int | None
    agent_id: str | None
    agent_version_id: str | None
    tools: list[ToolDefinition]
    # Whether this is a stored agent at its published version — the one case a
    # republish can move under a warm window.
    follows_published: bool
    # The whole roster, when this chat runs a plan. Threaded onto the runtime
    # context so a `handoffs` edge with no `agent_id` resolves in it.
    roster: dict[str, AgentConfig] = field(default_factory=dict)
    # The roster members that are stored agents, by id. A LiveKit agent id is the
    # stored agent's id when there is one, and this is what turns one found in a
    # handoff item back into the member it names.
    roster_names: dict[str, str] = field(default_factory=dict)


async def load_published_agent(tenant: Tenant, agent_id: UUID) -> TextEntryAgent:
    pool = await db.tenant_pool(tenant)
    row = await pool.fetchrow(
        """
        SELECT av.id, av.version, av.config
        FROM agents a
        JOIN agent_versions av
            ON av.agent_id = a.id
            AND av.version = a.published_version
            AND av.tenant_id = a.tenant_id
        WHERE a.id = $1 AND a.tenant_id = $2
        """,
        agent_id,
        tenant.id,
    )
    if not row:
        raise TurnRefused("this chat's agent is no longer published")
    config = AgentConfig.model_validate(row["config"])
    if config.channel != "text":
        raise TurnRefused(f"'{config.name}' is no longer a text agent")
    return TextEntryAgent(
        config=config,
        version=row["version"],
        agent_id=str(agent_id),
        agent_version_id=str(row["id"]),
        tools=await persistence.resolve_pinned_tools(tenant, config),
        follows_published=True,
        roster={config.name: config},
        roster_names={str(agent_id): config.name},
    )


async def load_planned_agent(tenant: Tenant, plan: dict) -> TextEntryAgent:
    """The cast a chat was started with, resolved back into configs.

    One code path with `load_published_agent` from here on: the entry member's
    config is a config like any other, and the roster is what a `handoffs` edge
    with no `agent_id` resolves against.
    """
    pool = await db.tenant_pool(tenant)
    roster = await load_plan_roster(pool, tenant.id, plan)
    entry = roster[0]
    if entry.config.channel != "text":
        raise TurnRefused(f"'{entry.config.name}' is not a text agent")
    agent_version_id = None
    if entry.agent_id and entry.version:
        agent_version_id = await pool.fetchval(
            "SELECT id FROM agent_versions WHERE agent_id = $1 AND version = $2 AND tenant_id = $3",
            UUID(entry.agent_id),
            entry.version,
            tenant.id,
        )
    return TextEntryAgent(
        config=entry.config,
        version=entry.version,
        agent_id=entry.agent_id,
        agent_version_id=str(agent_version_id) if agent_version_id else None,
        tools=await persistence.resolve_pinned_tools(tenant, entry.config),
        follows_published=False,
        roster={m.name: m.config for m in roster},
        roster_names={m.agent_id: m.name for m in roster if m.agent_id},
    )


async def load_entry_agent(tenant: Tenant, chat: Chat) -> TextEntryAgent:
    """The agent a chat entered on. A plan is pinned for the life of the chat; a
    plain stored agent follows its latest published version."""
    if chat.agent_plan:
        return await load_planned_agent(tenant, chat.agent_plan)
    if chat.agent_id is None:
        raise RuntimeError(f"chat {chat.id} has neither an agent nor an agent plan")
    return await load_published_agent(tenant, UUID(chat.agent_id))


async def _agent_by_livekit_id(
    tenant: Tenant, entry: TextEntryAgent, livekit_id: str
) -> TextEntryAgent | None:
    """The agent a handoff item's `new_agent_id` / `old_agent_id` names.

    A LiveKit agent id is one of three things (`compiler.compile`): a member of
    the chat's roster — by its name, or by its stored agent's id — some other
    stored agent, entered at its published version exactly as a handoff enters
    it; or an agent TASK's id, which names nothing here and returns None.
    """
    name = entry.roster_names.get(livekit_id, livekit_id)
    if name == entry.config.name:
        return entry
    if name in entry.roster:
        member = entry.roster[name]
        return TextEntryAgent(
            config=member,
            version=None,
            # As a handoff by name compiles it: a member carries no agent id.
            agent_id=None,
            agent_version_id=None,
            tools=await persistence.resolve_pinned_tools(tenant, member),
            follows_published=False,
            roster=entry.roster,
            roster_names=entry.roster_names,
        )
    try:
        agent_id = UUID(livekit_id)
    except ValueError:
        return None
    try:
        stored = await load_published_agent(tenant, agent_id)
    except TurnRefused:
        return None
    return TextEntryAgent(
        config=stored.config,
        version=stored.version,
        agent_id=stored.agent_id,
        agent_version_id=stored.agent_version_id,
        tools=stored.tools,
        follows_published=True,
        roster=entry.roster,
        roster_names=entry.roster_names,
    )


async def load_live_agent(tenant: Tenant, chat: Chat, entry: TextEntryAgent) -> TextEntryAgent:
    """Who holds the chat now: where its newest handoff went, else the entry agent.

    A handoff lasts for the life of the chat, so a window rebuilt after an idle
    minute re-enters at the agent the last one left off with. A handoff INTO a
    task is the exception — a task cannot be resumed part-way — so the chat
    returns to the agent that entered it.
    """
    pool = await db.tenant_pool(tenant)
    handoff = await pool.fetchrow(
        """
        SELECT metadata->'data'->>'new_agent_id' AS new_agent_id,
               metadata->'data'->>'old_agent_id' AS old_agent_id
        FROM conversation_items
        WHERE session_id = $1 AND tenant_id = $2 AND type = 'agent_handoff'
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """,
        UUID(chat.id),
        tenant.id,
    )
    if handoff is None:
        return entry
    for livekit_id in (handoff["new_agent_id"], handoff["old_agent_id"]):
        if livekit_id and (live := await _agent_by_livekit_id(tenant, entry, livekit_id)):
            return live
    return entry


async def integration_ids(tenant: Tenant, config: AgentConfig) -> frozenset[UUID]:
    """The MCP servers the config names that resolve right now.

    The ids are frozen with the config, but their integrations' *status* is not:
    one disabled mid-conversation drops out of this set, which is what makes it
    a useful warm-window fingerprint.
    """
    integrations = await persistence.load_mcp_integrations(tenant, config.mcps)
    return frozenset(i.id for i in integrations)


async def warm_still_valid(tenant: Tenant, window: TextWindow) -> bool:
    """Whether the warm window's live agent is still what it was compiled from.

    Keyed on the agent holding the floor, not on the chat's entry agent — an
    in-window handoff must not force a cold restart on the next message.

    Only a stored agent running its published version can have moved: a plan
    member is pinned and an inline agent has no versions. Whether the agent's
    MCP servers still resolve can change for any of them, and is checked either
    way.
    """
    if window.follows_published:
        try:
            published = await load_published_agent(tenant, UUID(str(window.agent_id)))
        except TurnRefused:
            return False
        if published.agent_version_id != window.agent_version_id:
            return False
    return await integration_ids(tenant, window.config) == window.integration_ids


def attach_tenant_text_output(window: TextWindow, job: TextTurnJob) -> None:
    """Attach a fresh token-stream output for this tenant web turn."""
    window.text_output = ConversationTextOutput(job)
    window.already_started = True


async def refresh_contact_userdata(tenant: Tenant, chat_id: UUID, session: AgentSession) -> None:
    """Bring what we know about the contact into the session, before a turn.

    The contact's bag is the single source of truth and each turn writes the
    session's back onto it — so without this, an edit made from outside
    (`PATCH /v1/conversations/{id}`) would be overwritten by the very next turn.
    Reserved runtime keys are the session's own and are left alone.
    """
    pool = await db.tenant_pool(tenant)
    bag = await pool.fetchval(
        """
        SELECT r.userdata
        FROM sessions s
        JOIN conversation_refs r ON r.id = s.conversation_ref_id AND r.tenant_id = s.tenant_id
        WHERE s.id = $1 AND s.tenant_id = $2
        """,
        chat_id,
        tenant.id,
    )
    for key, value in (bag or {}).items():
        if not is_reserved_key(key):
            session.userdata[key] = value


def initializes_userdata(config: AgentConfig) -> bool:
    """Whether this agent starts from what is known about the contact. As on a
    call, a `none` agent never does."""
    return config.conversation.context != "none" and config.conversation.initialize_userdata


async def _chat_context(
    tenant: Tenant, job: TextTurnJob, chat: Chat, entry: AgentConfig
) -> llm.ChatContext:
    """What a rebuilt window starts from: the whole stored thread.

    The conversation IS the context — it holds this chat, and under `transcript`
    everything before it. Under `summary` the earlier sessions are not in the
    thread, so what they concluded goes in front as one system message.
    """
    history = await persistence.load_conversation_chat_context(
        tenant,
        job.conversation_id,
        exclude_item_ids=[job.input_item_id] if job.input_item_id else None,
        # A turn stops at its own message. The job that ends a chat has none.
        through_created_at=job.created_at if job.input_item_id else None,
        through_item_id=job.input_item_id,
    )
    if entry.conversation.context != "summary" or chat.conversation_ref_id is None:
        return history
    rows = await persistence.load_conversation_summaries(
        tenant,
        conversation_ref_id=chat.conversation_ref_id,
        exclude_session_id=chat.id,
        limit=entry.conversation.summary_limit,
    )
    if not rows:
        return history
    summaries = llm.ChatMessage(
        role="system", content=[persistence.summary_block(_SUMMARY_BLOCK_HEADER, rows)]
    )
    return llm.ChatContext([summaries, *history.items])


async def open_window(
    tenant: Tenant, job: TextTurnJob, chat: Chat, *, for_turn: bool = True
) -> TextWindow:
    """Build a chat's warm window: compile whoever holds it now, on its history.

    The first window of a chat starts it — the row goes `queued` → `running`,
    `session.started` fires and the agent's entry will run. Every later one
    re-enters quietly at the chat's live agent.

    ``for_turn`` is False when the window exists only to run the exit hook of a
    chat that is ending: no credit is asked for and nothing is streamed.
    """
    # Before anything is compiled, and only when a window has to be built: a
    # warm one never re-checks, so a balance can run a little negative inside it
    # — which under BYOK costs us the per-message fee and nothing else.
    if for_turn and not await credits.has_credit(tenant):
        raise TurnRefused(credits.INSUFFICIENT_CREDITS_MESSAGE)

    entry = await load_entry_agent(tenant, chat)
    starting = chat.started_at is None
    live = entry if starting else await load_live_agent(tenant, chat, entry)
    config, agent_id = live.config, live.agent_id

    pool = await db.tenant_pool(tenant)
    if for_turn:
        # `agent_version_id` is the version the chat LAST RAN, which is what its
        # analysis reads when it ends; a plain stored agent follows its latest
        # publish from window to window. The version it started on is in the
        # `session.started` event.
        promoted = await pool.fetchval(
            """
            UPDATE sessions
            SET status = 'running',
                started_at = COALESCE(started_at, now()),
                agent_version_id = COALESCE($3::uuid, agent_version_id),
                updated_at = now()
            WHERE id = $1 AND tenant_id = $2 AND status IN ('queued', 'running')
            RETURNING id
            """,
            UUID(chat.id),
            tenant.id,
            entry.agent_version_id,
        )
        if promoted is None:
            raise TurnRefused("this chat ended before the message was answered")

    events = SessionEventLog(
        tenant,
        chat.id,
        after_seq=await pool.fetchval(
            "SELECT COALESCE(max(seq), 0) FROM session_events "
            "WHERE session_id = $1 AND tenant_id = $2",
            UUID(chat.id),
            tenant.id,
        ),
    )
    if starting and for_turn:
        emit_session_started(
            tenant,
            session_id=chat.id,
            conversation_id=str(job.conversation_id),
            agent_id=entry.agent_id,
            version=entry.version,
            channel="text",
            session_type=TENANT_PLANE.session_type,
            events=events,
        )

    tool_defs = persistence.select_tool_definitions(live.tools, config.tools)
    tasks = await persistence.resolve_pinned_tasks(tenant, config)
    integrations = await persistence.load_mcp_integrations(tenant, config.mcps)
    faqs = await persistence.load_faqs(tenant, config.faqs)
    hook_trees = await persistence.load_hook_trees(tenant, config, live.tools)
    conversation_cache_key = str(job.conversation_id)
    provider_keys = await persistence.load_provider_keys(tenant)
    tool_secrets = await persistence.load_tool_secrets(tenant)
    handoff_targets: dict[str, HandoffTarget] = {}
    session, agent = compile_agent(
        config,
        None,
        provider_keys,
        tool_defs=tool_defs,
        integrations=integrations,
        tool_secrets=tool_secrets,
        tasks=tasks,
        faqs=faqs,
        hook_trees=hook_trees,
        tenant=tenant,
        agent_id=agent_id,
        version=live.version,
        runtime_id=conversation_cache_key,
        session_id=chat.id,
        # A copy: the session mutates its bag, and `chat` is a frozen record of
        # the row as it was read.
        userdata=json.loads(json.dumps(chat.userdata)),
        runtime_context={
            "channel": "text",
            "session_type": TENANT_PLANE.session_type,
            "runtime_id": conversation_cache_key,
            "trigger_item_id": str(job.input_item_id) if job.input_item_id else None,
            "conversation_id": str(job.conversation_id),
            "session_id": chat.id,
            # Injected so compiler.operations never imports handoff/compile.
            RUNTIME_KEY_BUILD_HANDOFF_AGENT: build_handoff_agent,
            # The same hook the voice runtime puts here. `agent.handoff` is
            # written from `build_handoff_agent`, which is shared, so without
            # this line a text handoff would silently leave no trace.
            RUNTIME_KEY_RECORD_EVENT: events.record,
            # The cast this chat runs, so a `handoffs` edge with no agent_id
            # — and a `handoff` operation naming an `agent_name` — resolves.
            RUNTIME_KEY_AGENT_ROSTER: entry.roster,
            # The chat's `{{vars.*}}` values, never rebound per agent: they
            # belong to the chat, so a handoff target reads the same ones over
            # its own declared defaults.
            RUNTIME_KEY_SESSION_VARS: chat.vars,
            # Both tenant-scoped, so a handoff target's copy could only be the
            # same rows — carried forward rather than read again with the person
            # waiting on a reply.
            RUNTIME_KEY_PROVIDER_KEYS: provider_keys,
            RUNTIME_KEY_TOOL_SECRETS: tool_secrets,
            # Written by the compiler as this window hands off or enters a task,
            # read when the `agent_handoff` item is persisted.
            RUNTIME_KEY_HANDOFF_TARGETS: handoff_targets,
        },
        chat_ctx=await _chat_context(tenant, job, chat, entry.config),
    )

    # Attached for the life of the warm window, not per turn: the session
    # outlives any one turn, and a provider error is a property of the window.
    wire_session_trace(session, events, realtime=False)

    return TextWindow(
        session=session,
        agent=agent,
        session_id=chat.id,
        tenant=tenant,
        plane=TENANT_PLANE,
        run_entry=starting,
        agent_id=agent_id,
        agent_version_id=live.agent_version_id,
        agent_name=config.name,
        follows_published=live.follows_published,
        config=config,
        entry_config=entry.config,
        integration_ids=frozenset(i.id for i in integrations),
        already_started=False,
        text_output=ConversationTextOutput(job) if for_turn else None,
        events=events,
        handoff_targets=handoff_targets,
    )
