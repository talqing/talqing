"""Ending a chat: the one path both triggers take.

A chat ends when its agent calls `end_call`, or when the API says so
(`POST /v1/chats/{id}/end`, or a newer chat replacing it). Either way the row is
already marked ended by the time this runs, and what is left is everything a
call's finalize does, in the same order: the exit hook, usage, analysis, the
bill, retention, and one `session.completed`.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import db
from services import analysis, billing, retention, session_events, webhooks
from services.analysis import SKIP_TOO_SHORT, AnalysisOutcome
from services.messaging import TextTurnJob
from services.transcripts import load_session_transcript
from services.user import Tenant
from services.webhooks import events as webhook_events
from workers.session.events import SessionEventLog
from workers.text.planes import TENANT_PLANE
from workers.text.steps import StepRecorder, persist_userdata
from workers.text.tenant_executor import (
    initializes_userdata,
    load_chat,
    load_entry_agent,
    load_live_agent,
    open_window,
    refresh_contact_userdata,
)
from workers.text.turn import step_state_for
from workers.text.types import TextTurnInput, TextWindow
from workers.text.window import close_window, flush_window, mark_chat_ended

logger = logging.getLogger("talqing.workers.text.end")


async def end_chat(
    tenant: Tenant, job: TextTurnJob, window: TextWindow | None, *, close_reason: str
) -> None:
    """Finish the chat ``job.session_id`` names. Never raises.

    ``job`` is the turn whose agent called `end_call`, or the API's `end` job.
    ``window`` is the chat's warm window when it has one; with none, one is
    built only if there is an exit hook to run in it.
    """
    session_id = str(job.session_id)
    try:
        chat = await load_chat(tenant, session_id)
        if chat is None:
            logger.warning("end job for a chat that no longer exists: %s", session_id)
            return
        pool = await db.tenant_pool(tenant)
        # A chat ends once. Its agent's `end_call` and the API's end can both
        # arrive for the same chat, and the second must not run the exit hook,
        # the analysis or the webhook again.
        claimed = await pool.fetchval(
            "UPDATE sessions SET finalized_at = now() "
            "WHERE id = $1 AND tenant_id = $2 AND finalized_at IS NULL RETURNING id",
            job.session_id,
            tenant.id,
        )
        if claimed is None:
            if window is not None:
                await close_window(window)
            return
        entry_config = window.entry_config if window is not None else None
        # A chat that never took a message never entered, so it has nothing to
        # exit from and nothing to announce.
        started = chat.started_at is not None
        if window is None and started:
            entry = await load_entry_agent(tenant, chat)
            entry_config = entry.config
            live = await load_live_agent(tenant, chat, entry)
            if live.config.on_exit is not None:
                window = await open_window(tenant, job, chat, for_turn=False)
                await window.session.start(window.agent, record=False)
                if initializes_userdata(window.config):
                    await refresh_contact_userdata(tenant, job.session_id, window.session)

        if window is not None:
            # What the exit hook says is part of the chat: recorded and sent to
            # the contact like any other reply.
            step_input = TextTurnInput(
                job=job,
                tenant=tenant,
                plane=TENANT_PLANE,
                text="",
                images=(),
                created_at=job.created_at,
            )
            window.text_output = None
            recorder = StepRecorder(
                step_input, window.session, await step_state_for(step_input, window)
            )
            recorder.arm()
            try:
                await window.session.aclose()
            finally:
                await recorder.drain()
            await persist_userdata(step_input, window.session, session_id)
            await flush_window(window)

        await mark_chat_ended(tenant, session_id, close_reason)
        ended = await pool.fetchrow(
            "SELECT ended_at, close_reason FROM sessions WHERE id = $1 AND tenant_id = $2",
            job.session_id,
            tenant.id,
        )
        events = (
            window.events
            if window is not None and window.events is not None
            else SessionEventLog(
                tenant,
                session_id,
                after_seq=await pool.fetchval(
                    "SELECT COALESCE(max(seq), 0) FROM session_events "
                    "WHERE session_id = $1 AND tenant_id = $2",
                    job.session_id,
                    tenant.id,
                ),
            )
        )
        events.record(session_events.SESSION_ENDED, {"close_reason": ended["close_reason"]})
        await events.drain()

        # Analysis before the bill, as on a call: its LLM row has to be in
        # `llm_usage` when the final settlement reads it.
        outcome = AnalysisOutcome(status="skipped", skip_reason=SKIP_TOO_SHORT)
        if started and entry_config is not None:
            outcome = await analysis.run_for_session(
                tenant,
                session_id,
                entry_config,
                transcript=await load_session_transcript(tenant, session_id),
                close_reason=ended["close_reason"],
                # Claimed when the chat was started (`services.chats.open`).
                claim=False,
            )
        # That claim was `pending`, and it must not outlive the chat: a chat
        # nobody wrote to, an agent republished with analysis off, and a run
        # that failed all return without writing a status of their own.
        await pool.execute(
            """
            UPDATE sessions
            SET analysis_status = $3, analysis_skip_reason = $4, updated_at = now()
            WHERE id = $1 AND tenant_id = $2 AND analysis_status = 'pending'
            """,
            job.session_id,
            tenant.id,
            outcome.status,
            outcome.skip_reason,
        )
        await billing.settle_chat(tenant, session_id, job.session_id)
        await retention.schedule_session_purge(
            tenant, session_id, ended_at=ended["ended_at"] or datetime.now(UTC)
        )
        if not started:
            return
        payload = await webhooks.build_session_completed(tenant, session_id)
        if payload is not None:
            # Routed on the agent the payload NAMES — the chat's entry agent —
            # so a subscription scoped to one agent cannot receive an event
            # that says a different one.
            await webhooks.dispatch_bounded(
                tenant, webhook_events.SESSION_COMPLETED, payload["agent_id"], payload
            )
    except Exception:
        logger.exception("ending chat %s failed", session_id)
