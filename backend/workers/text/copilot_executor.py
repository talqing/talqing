"""CoPilot agent preparation (shared text turn loop does the rest).

One preparer for every CoPilot: which one is speaking arrives as the plane's
``CopilotSubject``.
"""

from __future__ import annotations

import logging
from uuid import uuid4

from livekit.agents import AgentSession

from compiler.factories import build_llm
from services.agents import LLMSpec
from services.catalog import get_catalog
from services.copilot import COPILOT_MAX_STEPS, CopilotAgent, CopilotSubject
from services.user import load_context
from settings import get_settings
from workers.session import persistence
from workers.text.types import TextTurnInput, TextWindow

logger = logging.getLogger("talqing.workers.text.copilot_executor")


async def prepare_copilot_agent(input: TextTurnInput, subject: CopilotSubject) -> TextWindow:
    if input.job.user_id is None:
        raise RuntimeError(f"{subject.label} turns require user_id on the Kafka job")
    if input.subject_id is None:
        raise RuntimeError(f"{subject.label} turn missing subject (ref.bind_id)")
    subject_id = input.subject_id
    # A queued turn from someone since removed must not still run as them — the
    # membership join inside `load_context` is that check, shared with the batch
    # dispatcher so there is one implementation of it rather than two that agree
    # today.
    ctx = await load_context(input.tenant, input.job.user_id)
    name = await subject.require(subject_id, ctx)

    # Ephemeral window id only — no sessions row for a CoPilot.
    session_id = str(uuid4())
    chat_context = await persistence.load_conversation_chat_context(
        input.tenant,
        input.job.conversation_id,
        exclude_item_ids=[input.job.input_item_id] if input.job.input_item_id else None,
        through_created_at=input.created_at,
        through_item_id=input.job.input_item_id,
    )
    cp = get_catalog().copilot
    llm_plugin = build_llm(
        # Talqing's own feature, on Talqing's own keys — never the tenant's BYOK
        # keys, which pay only for that tenant's agents.
        # A CoPilot writes into the tenant's configuration and answers in text,
        # so it is the one agent that should think before it acts — the effort
        # its model defaults to (none) would be the wrong setting here.
        LLMSpec(provider=cp.provider, model=cp.model, reasoning_effort=cp.reasoning_effort),
        get_settings().provider_secrets,
        # Per CoPilot, NOT per conversation — the opposite of what a tenant agent
        # wants. A tenant agent's system prompt is its own, so its cache bucket
        # may as well be its own session. A CoPilot's prompt and tool array are
        # identical for every conversation on the platform (which is why
        # `CopilotSubject.instructions` takes no arguments), so every one of them
        # should land on the same warm prefix instead of paying to prefill its
        # own copy. The key only influences routing; if one kind ever exceeds the
        # ~15 rpm a single cache machine holds, append a stable shard suffix.
        cache_key=f"copilot:{subject.kind}",
        has_tools=True,
    )
    agent = CopilotAgent(
        subject=subject,
        subject_id=subject_id,
        ctx=ctx,
        tools=subject.build_tools(ctx),
        chat_ctx=chat_context,
    )
    session = AgentSession(
        llm=llm_plugin,
        userdata={},
        # Never tighter than the CoPilot's own per-turn cap, which is the one
        # that answers with `_STEP_LIMIT_TEXT` instead of a tool-less guess.
        max_tool_steps=COPILOT_MAX_STEPS,
    )

    return TextWindow(
        session=session,
        agent=agent,
        session_id=session_id,
        tenant=input.tenant,
        plane=input.plane,
        agent_name=name,
    )
