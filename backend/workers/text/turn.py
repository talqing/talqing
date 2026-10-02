"""Run one text turn on a (possibly warm) LiveKit session window."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from uuid import UUID

from livekit.agents import StopResponse, llm

import db
from services import conversations
from services.agents import AgentConfig
from services.conversations.models import AssistantCompletedEvent
from services.messaging import TextTurnResult, turn_error_text
from workers.session import persistence
from workers.text.interruption import TextTurnInterruption
from workers.text.steps import StepRecorder, StepState, seal_turn_success
from workers.text.tenant_executor import (
    initializes_userdata,
    integration_ids,
    refresh_contact_userdata,
)
from workers.text.types import TextTurnInput, TextTurnOutcome, TextWindow
from workers.text.window import mark_chat_ended

logger = logging.getLogger("talqing.workers.text.turn")

# Status callback: same vocabulary as SSE TurnPayload.status.
TurnStatusCallback = Callable[..., Awaitable[None]]


async def _sync_chat_context_delta(input: TextTurnInput, window: TextWindow) -> None:
    """Append DB items that arrived while this warm window was busy.

    Latest-wins drops intermediate Kafka jobs without session.run, so those
    inbound user rows exist in conversation_items but not LiveKit history.
    Pull model-facing rows with created_at in (ctx_synced_through, this turn)
    and append them before generating a reply for the current message only.
    """
    if not window.already_started:
        return
    agent = window.session.current_agent
    delta = await persistence.load_conversation_chat_items_since(
        input.tenant,
        input.job.conversation_id,
        after_created_at=window.ctx_synced_through,
        before_created_at=input.created_at,
        exclude_item_ids=[input.job.input_item_id] if input.job.input_item_id else None,
    )
    if not delta:
        return
    existing_ids = {
        str(getattr(item, "id", "") or "")
        for item in agent.chat_ctx.items
        if getattr(item, "id", None)
    }
    ctx = agent.chat_ctx.copy()
    appended = 0
    latest: datetime | None = window.ctx_synced_through
    for created_at, item in delta:
        item_id = str(getattr(item, "id", "") or "")
        if item_id and item_id in existing_ids:
            continue
        ctx.items.append(item)
        if item_id:
            existing_ids.add(item_id)
        appended += 1
        if latest is None or created_at > latest:
            latest = created_at
    if appended == 0:
        return
    await agent.update_chat_ctx(ctx)
    if latest is not None:
        window.ctx_synced_through = latest
    logger.info(
        "warm chat delta conversation=%s appended=%s through=%s",
        input.job.conversation_id,
        appended,
        window.ctx_synced_through,
    )


def _advance_ctx_sync(
    window: TextWindow, input: TextTurnInput, step_state: StepState | None
) -> None:
    """Mark DB items through this turn as present in the LiveKit session."""
    synced = input.created_at
    if step_state is not None:
        for row in step_state.persisted:
            created = row.get("created_at")
            if isinstance(created, datetime) and created > synced:
                synced = created
    if window.ctx_synced_through is None or synced > window.ctx_synced_through:
        window.ctx_synced_through = synced


async def step_state_for(input: TextTurnInput, window: TextWindow) -> StepState:
    """What the step recorder needs to know about the session as it stands now:
    which items are already history, and whose name new ones are written under."""
    session = window.session
    # Prefer current agent ctx (post-handoff + delta); fall back to session.history.
    try:
        ctx_items = list(session.current_agent.chat_ctx.items)
    except RuntimeError:
        ctx_items = list(session.history.items)
    agent_id: UUID | None = None
    agent_version: int | None = None
    if window.agent_id:
        agent_id = UUID(str(window.agent_id))
        pool = await db.tenant_pool(input.tenant)
        agent_version = await pool.fetchval(
            "SELECT published_version FROM agents WHERE id = $1 AND tenant_id = $2",
            agent_id,
            input.tenant.id,
        )
    return StepState(
        session_id=window.session_id,
        source=input.plane.item_source,
        outbound_visibility=input.plane.outbound_visibility,
        initial_item_ids={item.id for item in ctx_items},
        handoff_targets=window.handoff_targets,
        agent_id=agent_id,
        agent_version=agent_version,
        text_output=window.text_output,
    )


async def run_text_turn(
    input: TextTurnInput,
    window: TextWindow,
    interruption: TextTurnInterruption,
    *,
    on_status: TurnStatusCallback | None = None,
) -> TextTurnOutcome:
    """Run one turn on a (possibly warm) session. Does not bill or aclose."""
    session = window.session
    session_id = window.session_id
    plane = input.plane
    step_state: StepState | None = None
    recorder: StepRecorder | None = None
    ran_turn = False

    async def _status(
        status: str,
        *,
        superseded_by_item_id: UUID | None = None,
        error: str | None = None,
        item_ids: list[str] | None = None,
    ) -> None:
        if on_status is None:
            return
        await on_status(
            status,
            session_id=session_id,
            superseded_by_item_id=superseded_by_item_id,
            error=error,
            item_ids=item_ids,
        )

    try:
        if window.text_output is not None:
            session.output.transcription = window.text_output
        cold = not window.already_started
        if cold:
            # LiveKit start schedules on_enter with wait_on_enter=False and does
            # not await it. Root agents use defer_entry=True (no-op framework
            # on_enter); we run entry ourselves after step listeners are armed
            # so hook side-effects (e.g. add_message) are progressive-persisted.
            await session.start(window.agent, record=False)
        await interruption.bind(session)
        if plane.name == "tenant" and initializes_userdata(window.config):
            await refresh_contact_userdata(input.tenant, UUID(session_id), session)
        # Warm: pull DB items that never got session.run (superseded inbounds).
        await _sync_chat_context_delta(input, window)
        step_state = await step_state_for(input, window)
        recorder = StepRecorder(
            input,
            session,
            step_state,
            skip=lambda: interruption.superseded_by_item_id is not None,
        )
        recorder.arm()

        # Once per session: a chat's entry runs when its FIRST window opens, and
        # a window rebuilt after an idle minute re-enters without it.
        if cold and window.run_entry:
            run_entry = getattr(window.agent, "run_entry", None)
            if run_entry is not None:
                await run_entry(initial=True)

        # Live agent after handoff — not the cold-start root on window.agent.
        live_agent = session.current_agent
        if interruption.superseded_by_item_id is None:
            ran_turn = True
            await _status("running")
            # An image turn goes into the context BEFORE the hook runs, and
            # unconditionally: `generate_reply(user_input=…)` commits the message
            # only if its speech handle schedules, and the text actor's
            # latest-wins supersede is exactly a thing that stops it scheduling —
            # so the model could be asked about a photo it never received while
            # the row says the sender attached one. Inserting first also lets an
            # `on_user_turn_completed` hook see the image in `turn_ctx`.
            #
            # No duplicate row: the API already wrote this item with its
            # attachments, and an item added through `update_chat_ctx`
            # fires no `conversation_item_added` for `persist_step` to see.
            if input.images:
                images = await persistence.image_contents(input.images)
                ctx = live_agent.chat_ctx.copy()
                ctx.insert(
                    llm.ChatMessage(
                        role="user",
                        content=[*([input.text] if input.text else []), *images],
                    )
                )
                await live_agent.update_chat_ctx(ctx)
            # LiveKit only invokes on_user_turn_completed on the audio end-of-turn
            # path; session.run → generate_reply never does. Call it explicitly so
            # text agents share hook parity with voice/video.
            skip_reply = False
            on_user_turn = getattr(live_agent, "on_user_turn_completed", None)
            if on_user_turn is not None:
                # Text only, deliberately: `{{args.user_message}}` is a sentence
                # for a tool to read, and an image has no string form.
                user_message = llm.ChatMessage(role="user", content=[input.text])
                temp_ctx = live_agent.chat_ctx.copy()
                try:
                    await on_user_turn(temp_ctx, user_message)
                except StopResponse:
                    # end_call / handoff from CompiledAgent — skip this agent's reply
                    skip_reply = True
            # No turn deadline: RunResult awaits a shielded future, so a
            # wait_for timeout would abandon the run without ending it and
            # leave the warm session unable to start the next one ("nested runs
            # are not supported"). Individual LLM/tool calls are bounded by
            # their own APIConnectOptions; the step count by max_tool_steps.
            if not skip_reply:
                if input.images:
                    # The message is already in the context above, so this asks
                    # for a reply to it rather than carrying it.
                    session.generate_reply()
                    await session.wait_for_idle()
                else:
                    # Left on `session.run` on purpose: it also arms the
                    # nested-run guard the per-conversation actor relies on.
                    await session.run(user_input=input.text)
            else:
                # handoff may have scheduled the target's on_enter / generate_reply
                await session.wait_for_idle()

        # Everything the turn produced is written before the turn is sealed.
        await recorder.drain()

        superseded_by = await interruption.seal()
        if superseded_by is not None:
            if ran_turn:
                _advance_ctx_sync(window, input, step_state)
            await _status("canceled", superseded_by_item_id=superseded_by)
            items = list(step_state.persisted) if step_state else []
            return TextTurnOutcome(
                result=TextTurnResult(
                    input.job.input_item_id,
                    [i["id"] for i in items if isinstance(i.get("id"), UUID)],
                    None,
                ),
                window=window.park(),
            )

        assert step_state is not None
        items, assistant_text = await seal_turn_success(input, session, step_state)
        _advance_ctx_sync(window, input, step_state)

        userdata = session.userdata if isinstance(session.userdata, dict) else {}
        end_requested = bool(userdata.get("_talqing_end_call_requested")) and plane.name == "tenant"
        if end_requested:
            # Before the turn is reported done, so whoever is waiting on it reads
            # `chat_status: ended` and the next message is refused. The rest of
            # the ending — exit hook, analysis, bill, webhook — follows the turn.
            await mark_chat_ended(input.tenant, session_id, "end_call")

        # Handoff may have switched the live agent; keep fingerprint in sync so
        # the next warm turn does not cold-restart unnecessarily.
        if step_state.handed_off:
            window.agent_id = str(step_state.agent_id)
            # A stored agent, entered at its published version: a republish can
            # now move under this window, where it could not under a plan member.
            window.follows_published = True
            pool = await db.tenant_pool(input.tenant)
            av_row = await pool.fetchrow(
                """
                SELECT av.id, av.config
                FROM agents a
                JOIN agent_versions av
                    ON av.agent_id = a.id
                    AND av.version = a.published_version
                    AND av.tenant_id = a.tenant_id
                WHERE a.id = $1 AND a.tenant_id = $2
                """,
                step_state.agent_id,
                input.tenant.id,
            )
            window.agent_version_id = str(av_row["id"]) if av_row else None
            # The target's servers are named by the version just loaded, but
            # which of them resolve is live — so the fingerprint is recomputed
            # rather than read off the config.
            try:
                target = AgentConfig.model_validate(av_row["config"]) if av_row else None
                # `config` follows the live agent too. It is what the next
                # `warm_still_valid` computes the fingerprint from — leaving the
                # entry's here would make the two disagree on every message after
                # a handoff — and what the window's usage is canonicalized
                # against at finalize, which should be the model that ran.
                if target is not None:
                    window.config = target
                window.integration_ids = (
                    await integration_ids(input.tenant, target)
                    if target is not None
                    else frozenset()
                )
            except Exception:
                logger.exception("failed to refresh MCP fingerprint after handoff")

        # Before the turn is reported done: a caller waiting on this turn stops
        # listening the moment it is, and would miss the reply's closing frame.
        if plane.name == "tenant":
            await conversations.publish(
                input.tenant.id,
                input.job.conversation_id,
                AssistantCompletedEvent(
                    trigger_item_id=input.job.input_item_id,
                    session_id=session_id,
                    text=assistant_text,
                    item_ids=[item["id"] for item in items],
                ),
            )
        await _status("done", item_ids=[str(i["id"]) for i in items])
        return TextTurnOutcome(
            result=TextTurnResult(
                input_item_id=input.job.input_item_id,
                output_item_ids=[i["id"] for i in items],
                assistant_text=assistant_text,
            ),
            window=window.park(),
            end_chat=end_requested,
        )
    except BaseException as exc:
        if interruption.superseded_by_item_id is not None:
            if ran_turn:
                _advance_ctx_sync(window, input, step_state)
            await interruption.seal()
            await _status("canceled")
            return TextTurnOutcome(
                result=TextTurnResult(input.job.input_item_id, [], None),
                window=window.park(),
            )
        if isinstance(exc, asyncio.CancelledError):
            # A worker shutdown. The turn is not resumable — the text topic is
            # `latest` with auto-commit and there is no durable work row — so the
            # person is told plainly rather than left with a spinner that only
            # the NEXT message clears (`copilot.service._latest_turn` consults
            # only the newest turn).
            #
            # `error` and not `canceled`: `canceled` already means *superseded by
            # a newer message* everywhere in this feature, and overloading it
            # would make the two indistinguishable to the rail.
            #
            # Suppressed because the `raise` must happen: `record_turn_status`
            # never raises, but the Redis publish after it can, and an exception
            # here would REPLACE the `CancelledError` — which `aclose()`
            # suppresses so it can go on to finalize the parked window. The
            # database write is what stops the spinner on the next reload; the
            # SSE frame is best-effort by design.
            with contextlib.suppress(Exception):
                await _status(
                    "error",
                    error="the worker restarted before this reply finished — send your message again",
                )
            raise
        if ran_turn:
            _advance_ctx_sync(window, input, step_state)
        # No error item in the timeline: it would be read back into the model's
        # chat context next turn. The reason belongs on the turn itself, which
        # `_status` records alongside the SSE frame.
        logger.exception(
            "text turn failed conversation=%s input=%s",
            input.job.conversation_id,
            input.job.input_item_id,
        )
        # Not str(exc): what surfaces here is the wrapper LiveKit raised, whose
        # message is "Connection error." however specific the cause underneath.
        await _status("error", error=turn_error_text(exc))
        # Keep the window warm for the next message when possible; fatal
        # LiveKit death is handled by fingerprint / next-turn failure.
        raise
    finally:
        if recorder is not None:
            await recorder.drain()
