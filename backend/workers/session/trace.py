"""What a LiveKit session reports about itself, written to the durable trace.

One wiring for a call and a chat: the same provider failure, tool run or
truncated turn must not be recorded differently depending on which worker was
holding the session. The vocabulary is `services/session_events.py`.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from livekit.agents import (
    AgentFalseInterruptionEvent,
    AgentSession,
    ErrorEvent,
    SpeechCreatedEvent,
    ToolExecutionUpdatedEvent,
    llm,
)
from livekit.agents import stt as lk_stt
from livekit.agents import tts as lk_tts
from livekit.agents.voice.speech_handle import SpeechHandle

from services import session_events
from workers.session.events import SessionEventLog


def wire_session_trace(session: AgentSession, events: SessionEventLog, *, realtime: bool) -> None:
    """Record this session's errors, tool runs, step limit and false interruptions.

    ``realtime`` is whether a speech-to-speech model runs the turns: LiveKit
    counts tool rounds there and never checks them.
    """

    @session.on("error")
    def _on_error(ev: ErrorEvent) -> None:
        # ErrorEvent serializes `source` to {model, provider} and `error` to
        # the provider's own error model — but every one of those five models
        # declares `error: Exception = Field(..., exclude=True)`, so the dump
        # carries the label and drops the only field that says what went
        # wrong. Without `cause` below a tenant reads "Raya/m1 raised an
        # error" and there is nowhere else to look: this event is the sole
        # record of a provider failure.
        dumped = ev.model_dump(mode="json")
        events.record(
            session_events.SESSION_ERROR,
            {
                "source": dumped.get("source"),
                "error": dumped.get("error"),
                "recoverable": getattr(ev.error, "recoverable", None),
                "cause": session_events.error_cause(getattr(ev.error, "error", None)),
            },
        )

    # LiveKit's own counter, read once the turn is over rather than shadowed:
    # `num_steps` starts at 1, gains one per tool round, and the round that
    # makes it `max_tool_steps + 1` is the last one allowed tools. So only a
    # truncated turn ends at `limit + 1`, `limit` being the author's
    # `max_steps` (LiveKit's knob plus one, see `compile_agent`). A realtime
    # turn never checks, so there the same threshold means "this turn ran
    # that deep", which is the only thing anyone can say about a pipeline
    # with no ceiling.
    limit = session.options.max_tool_steps + 1

    @session.on("speech_created")
    def _on_speech_created(ev: SpeechCreatedEvent) -> None:
        def _report(handle: SpeechHandle) -> None:
            if handle.num_steps >= limit + 1:
                events.record(
                    session_events.TOOL_STEP_LIMIT_REACHED,
                    {"steps": handle.num_steps - 1, "limit": None if realtime else limit},
                )

        ev.speech_handle.add_done_callback(_report)

    @session.on("agent_false_interruption")
    def _on_false_interruption(ev: AgentFalseInterruptionEvent) -> None:
        events.record(session_events.AGENT_FALSE_INTERRUPTION, {"resumed": ev.resumed})

    tool_started_at: dict[str, float] = {}

    @session.on("tool_execution_updated")
    def _on_tool_execution(ev: ToolExecutionUpdatedEvent) -> None:
        update = ev.update
        if update.type == "tool_call_started":
            call = update.function_call
            tool_started_at[call.call_id] = time.monotonic()
            events.record(
                session_events.TOOL_STARTED,
                {"call_id": call.call_id, "name": call.name},
            )
        elif update.type == "tool_call_ended":
            started = tool_started_at.pop(update.call_id, None)
            events.record(
                session_events.TOOL_ENDED,
                {
                    "call_id": update.call_id,
                    "status": update.status,
                    "message": update.message,
                    "duration_ms": (
                        None if started is None else round((time.monotonic() - started) * 1000, 1)
                    ),
                    # The tool released the turn early (`ctx.update()`), so
                    # `duration_ms` measured the WORK and not how long the
                    # caller waited — the one case where the two are
                    # unrelated. LiveKit says so by suffixing the terminal
                    # entry's id: `<call_id>_final` when the call deferred,
                    # the bare call_id when it ran inline
                    # (`voice/tool_executor.py::_on_done`). Read as a fact
                    # about THIS execution rather than as the tool's
                    # `long_running_task` flag, because the flag is a no-op
                    # on text and the flag can change after the call.
                    "background": update.id != update.call_id,
                },
            )
        elif update.type == "tool_reply_updated":
            # Keyed by the entry ids the reply covers (`<call_id>_final`),
            # which is what joins it back to the tool it speaks for.
            events.record(
                session_events.TOOL_REPLY,
                {
                    "update_ids": list(update.update_ids),
                    "status": update.status,
                    "speech_id": update.speech_id,
                },
            )

    _wire_provider_health(session, events)


def _wire_provider_health(session: AgentSession, events: SessionEventLog) -> None:
    """Trace failover on any component that compiled to a FallbackAdapter.

    LiveKit's FallbackAdapter reports availability transitions, not
    individual attempts — a "failed" here means the primary was taken out of
    rotation and traffic moved to the backup, not that one request 500'd.

    The test is the runtime object, not the config: only a FallbackAdapter
    emits these, and asking it directly cannot disagree with what the
    compiler actually built (a realtime agent, for instance, carries a
    default LLM spec it never uses).
    """

    def handler(component: str) -> Callable[[Any], None]:
        # `ev` is a per-module AvailabilityChangedEvent — three unrelated
        # dataclasses that share a shape but no base class. Each names its
        # model field after its own component (`ev.stt` / `ev.llm` /
        # `ev.tts`), which is why the component string is threaded through.
        def _on_availability(ev: Any) -> None:
            model = getattr(ev, component)
            events.record(
                session_events.PROVIDER_RECOVERED
                if ev.available
                else session_events.PROVIDER_FAILED,
                {"component": component, "provider": model.provider, "model": model.model},
            )

        return _on_availability

    if isinstance(session.stt, lk_stt.FallbackAdapter):
        session.stt.on("stt_availability_changed", handler("stt"))
    if isinstance(session.llm, llm.FallbackAdapter):
        session.llm.on("llm_availability_changed", handler("llm"))
    if isinstance(session.tts, lk_tts.FallbackAdapter):
        session.tts.on("tts_availability_changed", handler("tts"))
