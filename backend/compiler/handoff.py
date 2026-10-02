"""Build the agent a handoff hands to.

Multi-agent calls come from two places and land here: `AgentConfig.handoffs`,
which the compiler turns into one `handoff_to_*` tool per destination, and the
`handoff` operation inside a tool's tree. Both name either a STORED agent —
loaded at its latest published version, with its tools and hooks —
or a member of this call's team, resolved in the roster on the runtime context.

Either way this applies the edge's context-passing policy (transcript / summary
/ none — the same words an agent's own `conversation.context` uses) and compiles
a CompiledAgent with the target's own prompt/tools/voice/turn handling as
Agent-level overrides. AgentActivity then runs `session.update_agent(...)`; the
media session stays, while voice/VAD settings can be overridden on the target
Agent.
"""

from __future__ import annotations

import asyncio
import json
import logging

from livekit.agents import AgentSession, get_job_context, llm
from livekit.agents.llm import ToolError
from livekit.agents.types import NOT_GIVEN

from compiler.compile import CompiledAgent, compile_node_agent
from compiler.factories import build_llm
from compiler.operations import (
    RUNTIME_KEY_AGENT_ROSTER,
    RUNTIME_KEY_CONSENT_DISCLOSED,
    RUNTIME_KEY_HANDOFF_TARGETS,
    RUNTIME_KEY_PROVIDER_KEYS,
    RUNTIME_KEY_RECORD_EVENT,
    RUNTIME_KEY_TOOL_SECRETS,
)
from services import session_events
from services.agents import DEFAULT_SUMMARY_RECENT_TURNS, handoff_media_error
from services.billing import collector_in
from services.conversation_context import ConversationContext
from services.tools import HandoffTarget, RuntimeContext, UserData
from services.user import Tenant

logger = logging.getLogger("talqing.compiler.handoff")

# The provenance sentence in front of the summary the source agent wrote. The
# same shape the CALL-level `summary` mode uses (`workers/voice/runtime.py`'s
# `_SUMMARY_BLOCK_HEADER`): one system message, headed by where the text came
# from. One question, one answer, one shape.
#
# It can speak about the tail unconditionally because `recent_turns` is `ge=1`
# and `summary` resolves a null to two, so under this policy there is always one.
_HANDOFF_SUMMARY_HEADER = (
    "{source} has just handed you this conversation. This is their summary of what happened "
    "before the last few messages you can see below. The caller has already told {source} all "
    "of it, so do not ask them to repeat any of it and do not read it back to them."
)


def _recent_turns(history: llm.ChatContext, turns: int) -> list[llm.ChatItem]:
    """The last `turns` turns of the conversation, verbatim, messages only.

    A turn starts at a user message and runs until the next one, so a turn that
    took two replies and a tool call in between crosses whole. LiveKit's own
    summarizer counts messages instead (`keep_last_turns * 2`) and cuts such a
    turn in half.

    Tool calls do not cross. The source agent's tool results are *knowledge*, and
    knowledge belongs in the summary — a raw `get_balance` payload in the tail is
    the same fact twice, in a worse format. More to the point, slicing can cut a
    `function_call` away from its `function_call_output`, and a dangling half is
    a provider 400 rather than a degraded reply; excluding both halves removes
    the hazard instead of adding a repair pass.

    `exclude_empty_message` guards the turn that exclusion would otherwise
    strand — an assistant turn that was nothing but a tool call. It is belt and
    braces today: `AgentActivity` only writes an assistant message when there
    was text to write (`if forwarded_text and add_to_chat_ctx`, and the same
    guard on the realtime path), so such a turn leaves nothing behind to strand.
    Kept because the alternative is a tail that silently depends on that guard
    staying true in a library we pin but do not own.
    """
    if turns <= 0:
        return []
    items = history.copy(
        exclude_instructions=True,
        exclude_function_call=True,
        exclude_handoff=True,
        exclude_config_update=True,
        exclude_empty_message=True,
    ).items
    seen = 0
    for i in range(len(items) - 1, -1, -1):
        item = items[i]
        if item.type == "message" and item.role == "user":
            seen += 1
            if seen == turns:
                return list(items[i:])
    return list(items)  # fewer turns than asked for — all of it


async def build_handoff_agent(
    tenant: Tenant,
    session: AgentSession[UserData],
    *,
    target_agent_id: str | None = None,
    target_name: str | None = None,
    context_policy: str = "transcript",
    summary: str | None = None,
    recent_turns: int | None = None,
    via: str = "handoffs",
    runtime_id: str | None = None,
    runtime_context: RuntimeContext | None = None,
) -> CompiledAgent:
    """Compile the agent this call is handing to.

    Two kinds of target, one code path from the media check down:

    * ``target_agent_id`` — a stored agent, entered at its latest PUBLISHED
      version. The media check runs here as well as at publish, because that
      version follows the agent and can have been republished since.
    * ``target_name`` — a member of this call's team, resolved in the roster the
      worker put on the runtime context. Its config was resolved and validated
      when the call was planned and is immutable, so the re-check can only agree
      — but it runs anyway, because one code path is worth more than the
      microseconds.

    ``summary`` is the text that crosses under ``context_policy="summary"``:
    written by the source agent as an argument on its `handoff_to_*` tool, or
    resolved from the `handoff` operation's own `summary` template. There is no
    second LLM call either way.

    ``recent_turns`` is what the AUTHOR configured, null included — the policy's
    own default is resolved here, in one place, so neither caller has to know it.
    ``via`` names which of the two callers this is, for the trace.
    """
    # lazy import: workers.session.persistence is the shared data-access layer; importing
    # at module top would cycle through workers.voice.main → compiler → here
    from workers.session import persistence

    label = target_agent_id or target_name
    if target_agent_id:
        loaded = await persistence.load_published_definition(tenant, target_agent_id)
        if not loaded:
            logger.error("handoff target %s has no published version", target_agent_id)
            raise ToolError("I'm sorry, that team isn't available right now.")
        cfg, version, frozen_tool_defs = loaded
    else:
        roster = (runtime_context or {}).get(RUNTIME_KEY_AGENT_ROSTER) or {}
        cfg = roster.get(target_name)
        if cfg is None:
            logger.error(
                "handoff target %r is not a member of this call's team (members: %s)",
                target_name,
                sorted(roster),
            )
            raise ToolError("I'm sorry, that team isn't available right now.")
        version = None
        frozen_tool_defs = await persistence.resolve_pinned_tools(tenant, cfg)
    try:
        source_agent = session.current_agent
    except RuntimeError:
        source_agent = None
    source_cfg = getattr(source_agent, "cfg", None)
    if source_cfg is not None:
        if err := handoff_media_error(source_cfg, cfg):
            logger.warning(
                "blocked unsafe handoff to %s: %s",
                label,
                err,
            )
            raise ToolError("I'm sorry, that team isn't available right now.")

    # Who this call is handing to, for the worker's transcript item. Recorded
    # here because this is the only place that knows it, and before the agent is
    # compiled because the item is emitted the moment AgentActivity switches.
    # Keyed by the LiveKit agent id that item will carry — `compile_node_agent`
    # sets it to the stored agent's id, or to the member's name when there is
    # none, which is also this entry's key.
    targets = (runtime_context or {}).get(RUNTIME_KEY_HANDOFF_TARGETS)
    if isinstance(targets, dict):
        # `cfg.name` in both roles, and that is not a coincidence: the roster is
        # built as `{cfg.name: cfg}` with a member's own `name` merged over the
        # stored agent's, and it is unique across the call — so it is both the
        # key the model called and the name a reader will recognise.
        targets[str(target_agent_id or cfg.name)] = HandoffTarget(
            name=cfg.name,
            agent_id=target_agent_id,
            version=version,
        )

    # Both are tenant-scoped and were loaded when the call started, so the call
    # runs to completion on the credentials it answered with — see
    # RUNTIME_KEY_PROVIDER_KEYS.
    provider_keys = (runtime_context or {}).get(RUNTIME_KEY_PROVIDER_KEYS)
    if not isinstance(provider_keys, dict):
        raise RuntimeError("handoff: the runtime context carries no provider keys")
    tool_secrets = (runtime_context or {}).get(RUNTIME_KEY_TOOL_SECRETS)
    if not isinstance(tool_secrets, dict):
        raise RuntimeError("handoff: the runtime context carries no tool secrets")

    # Fail the handoff rather than enter a target missing something it was built
    # with — the same rule the worker applies at session start. Raising here
    # leaves the caller with the source agent, which works.
    tool_defs = persistence.select_tool_definitions(frozen_tool_defs, cfg.tools)
    hook_trees = await persistence.load_hook_trees(tenant, cfg, frozen_tool_defs)
    integrations, tasks, faqs = await asyncio.gather(
        persistence.load_mcp_integrations(tenant, cfg.mcps),
        persistence.resolve_pinned_tasks(tenant, cfg),
        persistence.load_faqs(tenant, cfg.faqs),
    )

    # Handoffs stay in the same prompt-cache bucket as the source execution.
    # Realtime calls use the LiveKit session id from job metadata; text
    # passes the conversation id as runtime_id (no job context).
    if runtime_id is None:
        session_id = str(json.loads(get_job_context().job.metadata or "{}")["session"])
    else:
        session_id = runtime_id
    cache_key = session_id
    # A realtime target has no separate LLM to build here; compile_node_agent
    # constructs its speech-to-speech model itself.
    target_llm = (
        None
        if cfg.realtime is not None
        else build_llm(
            cfg.llm,
            provider_keys,
            cache_key=cache_key,
            has_tools=bool(tool_defs or integrations or faqs),
            # The call's own collector, not a new one: this is the same session's
            # spend, on its second (or fifth) model. Reading the total off
            # `session.llm` at the end instead would drop every turn before the
            # last handoff.
            reported_cost=collector_in(session.userdata),
        )
    )

    # context-passing policy: what the target agent starts from.
    # Always use session.history (the public session chat context) so voice and
    # text handoffs share one context source.
    source_chat_ctx = session.history
    source_name = source_cfg.name if source_cfg is not None else "The previous agent"

    # Null means "the policy's own answer", which is what keeps each policy's
    # name honest out of the box: `none` starts the target with nothing, and
    # `summary` starts it with a summary plus enough to know what was just asked.
    turns = 0
    if context_policy == "summary":
        turns = recent_turns or DEFAULT_SUMMARY_RECENT_TURNS
    elif context_policy == "none":
        turns = recent_turns or 0

    summary_text = (summary or "").strip()
    # `required` on the tool argument is a request, not a guarantee, and the
    # operation's `summary` is a template that can render empty. Either way the
    # call keeps working and the caller notices nothing — the target simply gets
    # what it would have got before this feature existed.
    summary_fallback = context_policy == "summary" and not summary_text
    if summary_fallback:
        logger.warning(
            "handoff to %s asked for a summary and got an empty one — passing the full transcript",
            label,
        )

    tail: list[llm.ChatItem] = []
    chat_ctx = NOT_GIVEN
    entry_context: ConversationContext = "transcript"
    if context_policy == "transcript" or summary_fallback:
        chat_ctx = source_chat_ctx.copy(exclude_instructions=True)
    elif context_policy == "summary":
        tail = _recent_turns(source_chat_ctx, turns)
        entry_context = "summary"
        # Built as a list rather than added-then-inserted: `ChatContext.insert`
        # places items by `created_at` and `add_message` stamps *now*, so the
        # summary would sort AFTER every tail item — silently inverting the order
        # and making the header's "the messages you can see below" a lie. Nothing
        # raises; the context is just wrong.
        chat_ctx = llm.ChatContext(
            [
                llm.ChatMessage(
                    role="system",
                    content=[
                        f"{_HANDOFF_SUMMARY_HEADER.format(source=source_name)}\n\n{summary_text}"
                    ],
                ),
                *tail,
            ]
        )
    else:  # "none" — nothing at all, or exactly the tail the author asked for
        tail = _recent_turns(source_chat_ctx, turns)
        entry_context = "none"
        if tail:
            chat_ctx = llm.ChatContext(list(tail))

    userdata = session.userdata if isinstance(session.userdata, dict) else {}
    participant_identity = userdata.get("_talqing_participant_identity")
    next_runtime_context = dict(runtime_context or {})
    next_runtime_context["runtime_id"] = session_id
    # Whether the caller has already been told the call is recorded. The target
    # cannot work this out on entry — by then it *is* `session.current_agent` —
    # and a target that discloses would otherwise either repeat a notice the
    # caller just heard or stay silent about a recording nobody announced.
    next_runtime_context[RUNTIME_KEY_CONSENT_DISCLOSED] = bool(
        source_cfg is not None
        and source_cfg.recording.enabled
        and source_cfg.recording.consent == "disclosure"
    )
    agent = compile_node_agent(
        cfg,
        tool_defs=tool_defs,
        integrations=integrations,
        tool_secrets=tool_secrets,
        tasks=tasks,
        faqs=faqs,
        hook_trees=hook_trees,
        tenant=tenant,
        agent_id=target_agent_id,
        version=version,
        runtime_id=session_id,
        userdata=userdata,
        provider_keys=provider_keys,
        chat_ctx=chat_ctx,
        llm_instance=target_llm,
        vad=session.vad,
        entry_context=entry_context,
        entry_has_tail=bool(tail),
        participant_identity=participant_identity
        if isinstance(participant_identity, str)
        else None,
        runtime_context=next_runtime_context,
    )

    # The only place that knows every field of this event — the resolved turn
    # count, how many items the tail actually came to, and whether a blank
    # summary degraded — and it is shared by both callers, so neither the config
    # edge nor the operation can end up untraced. On success only: a handoff that
    # fails to compile has already raised above and already logged.
    record = (runtime_context or {}).get(RUNTIME_KEY_RECORD_EVENT)
    if callable(record):
        record(
            session_events.AGENT_HANDOFF,
            {
                "from_agent_id": getattr(source_agent, "_talqing_agent_id", None),
                "from_agent_name": source_cfg.name if source_cfg is not None else None,
                "to_agent_id": target_agent_id,
                # The roster key for a team member — what the model called and
                # what resolved it — and the target agent's own name for a stored
                # one, which is the name the reader will find in the agents list.
                "to_name": target_name or cfg.name,
                "context": context_policy,
                "recent_turns": turns,
                "tail_items": len(tail),
                "summary": summary_text or None,
                "summary_fallback": summary_fallback,
                "via": via,
            },
        )
    return agent
