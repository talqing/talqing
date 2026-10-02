"""Ending a call that has stopped being a conversation.

Three things end a call nobody hung up: the caller going quiet and staying quiet
(`AgentConfig.silence`), the call reaching its length limit
(`AgentConfig.max_duration_seconds`, or `MAX_CALL_DURATION_SECONDS` when that is
null), and an outbound call turning out to have reached voicemail
(`AgentConfig.voicemail_detection`). Without them a call to an off-hook handset,
hold music or an answering machine bills the tenant's models and our platform
fee until something outside us gives up — and on a web call, nothing outside us
ever does.

**Voicemail is the model's call**, made through the generated
`voicemail_detected` tool (`compiler/tools.py`), which lands in
:meth:`CallBounds.voicemail`. Deciding in the model rather than with LiveKit's
AMD classifier is what lets a person who picks up hear the greeting at once:
AMD holds every greeting until it has classified the other end.

**Silence** rides LiveKit's own signal. The session marks the caller `away`
after `user_away_timeout` (set from `silence.timeout` in `compile_agent`) of
quiet while both sides are listening, and only speech brings them back — so
`away` fires once per quiet spell, and the repeat check-ins pace themselves
here, one `timeout` after each finishes playing. This is the shape of LiveKit's
own `examples/drive_thru` nudge loop.

**Every line is the agent's model's**, asked for with an instruction rather than
spoken from a fixed string. A fixed "Are you still there?" would be English on a
Hindi agent and off-voice on every agent with a persona; the model already knows
the language and the register of the conversation it is in.

Both read the config of the agent that ANSWERED (`VoiceRun.cfg`): they bound the
call, like `max_steps`, and a handoff does not reset them. Neither acts while a
transfer holds the call — the caller is on hold music then, and once bridged the
two humans are not ours to hang up on.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from livekit.agents import CloseEvent, UserStateChangedEvent, get_job_context

from compiler.speech import SPEECH_WAIT_TIMEOUT, speak
from services import close_reasons, session_events
from services.agents import MAX_CALL_DURATION_SECONDS
from utils.bg import spawn

if TYPE_CHECKING:
    from workers.voice.runtime import VoiceRun

logger = logging.getLogger("talqing.workers.voice.call_bounds")

CHECK_IN_INSTRUCTIONS = (
    "The caller has gone quiet. In one short sentence, check whether they are still there. "
    "Do not repeat what you said last."
)
SILENCE_GOODBYE_INSTRUCTIONS = (
    "The caller has not answered for a while. Say a brief goodbye in one short sentence - "
    "the call is ending now."
)
DURATION_GOODBYE_INSTRUCTIONS = (
    "This call has reached its time limit. In one or two short sentences, tell the caller you "
    "have to end the call now, and say goodbye."
)

# How long a limit that lands mid-transfer waits before looking again. The
# transfer either fails back to the agent — and the limit applies after all — or
# lands, and the session closes, which cancels it.
_TRANSFER_RECHECK_SECONDS = 30.0


class CallBounds:
    """The silence check-ins, the length limit and the voicemail hang-up of one call.

    `start` once, when the agent can hold the conversation; `voicemail` is called
    by the model's tool, and only exists on a call the agent placed.
    """

    def __init__(self, run: VoiceRun) -> None:
        self._run = run
        self._deadline: asyncio.TimerHandle | None = None
        self._silence_task: asyncio.Task[None] | None = None
        self._ending = False

    def start(self) -> None:
        """Arm both, from the moment the agent can hold the conversation."""
        session = self._run.session
        limit = self._run.cfg.max_duration_seconds or MAX_CALL_DURATION_SECONDS
        self._arm_deadline(limit)
        if self._run.cfg.silence.enabled:
            session.on("user_state_changed", self._on_user_state_changed)
        session.on("close", self._on_close)

    # ── length ──────────────────────────────────────────────────────────────

    def _arm_deadline(self, delay: float) -> None:
        self._deadline = asyncio.get_running_loop().call_later(delay, self._on_deadline)

    def _on_deadline(self) -> None:
        self._deadline = None
        if self._ending:
            return
        if self._run.transfer_state is not None:
            self._arm_deadline(_TRANSFER_RECHECK_SECONDS)
            return
        logger.info("call %s reached its length limit", self._run.spec.session_id)
        spawn(
            self._hang_up(
                close_reasons.MAX_DURATION, goodbye_instructions=DURATION_GOODBYE_INSTRUCTIONS
            )
        )

    # ── silence ─────────────────────────────────────────────────────────────

    def _on_user_state_changed(self, ev: UserStateChangedEvent) -> None:
        if self._ending:
            return
        if ev.new_state != "away":
            # They spoke. Whatever check-in was pending has its answer.
            if self._silence_task is not None:
                self._silence_task.cancel()
                self._silence_task = None
            return
        if self._run.transfer_state is not None:
            return  # on hold music; the quiet is ours, not theirs
        if self._silence_task is None or self._silence_task.done():
            self._silence_task = asyncio.create_task(
                self._check_in_until_answered(), name="talqing_call_silence"
            )

    async def _check_in_until_answered(self) -> None:
        silence = self._run.cfg.silence
        session = self._run.session
        for attempt in range(1, silence.max_check_ins + 1):
            handle = session.generate_reply(
                instructions=CHECK_IN_INSTRUCTIONS, tool_choice="none", allow_interruptions=True
            )
            self._run.events.record(
                session_events.CALLER_CHECK_IN,
                {"attempt": attempt, "of": silence.max_check_ins},
            )
            await handle.wait_for_playout()
            await asyncio.sleep(silence.timeout)
        await self._hang_up(
            close_reasons.SILENCE_TIMEOUT, goodbye_instructions=SILENCE_GOODBYE_INSTRUCTIONS
        )

    # ── voicemail ───────────────────────────────────────────────────────────

    async def voicemail(self, message: str | None) -> None:
        """The model heard a voicemail greeting: leave `message`, if any, and hang up.

        `message` arrives already personalized — it is the author's line, resolved
        when the tool was built, exactly as the greeting is.
        """
        logger.info("call %s reached voicemail", self._run.spec.session_id)
        await self._hang_up(close_reasons.VOICEMAIL, message=message)

    # ── ending ──────────────────────────────────────────────────────────────

    async def _hang_up(
        self,
        reason: str,
        *,
        goodbye_instructions: str | None = None,
        message: str | None = None,
    ) -> None:
        """Say the last line, then end the call the way the `end_call` operation does.

        The last line is the model's own goodbye (`goodbye_instructions`), the
        author's fixed `message`, or nothing.
        """
        if self._ending:
            return
        self._ending = True
        # Named before anything closes: finalize keeps the first reason it is
        # given, and the framework's own close would otherwise offer a generic one.
        self._run.set_close_reason(reason)
        session = self._run.session
        # Pinned so nobody can talk the agent out of hanging up — nor a voicemail
        # system's beep cut its message off — except on a realtime model, whose
        # server-side turn detection LiveKit will not let a reply opt out of (it
        # warns and ignores the flag).
        allow_interruptions = self._run.cfg.realtime is not None
        try:
            handle = None
            if goodbye_instructions is not None:
                handle = session.generate_reply(
                    instructions=goodbye_instructions,
                    tool_choice="none",
                    allow_interruptions=allow_interruptions,
                )
            elif message is not None:
                handle = speak(session, message, allow_interruptions=allow_interruptions)
            if handle is not None:
                await asyncio.wait_for(handle.wait_for_playout(), timeout=SPEECH_WAIT_TIMEOUT)
        except Exception:
            logger.warning("last line did not finish; hanging up anyway", exc_info=True)

        job = get_job_context()

        async def _delete_room() -> None:
            # Every leg goes with it — the SIP caller, a stream gateway, a browser.
            try:
                await job.delete_room()
            except Exception:
                logger.exception("delete_room failed after %s", reason)

        job.add_shutdown_callback(_delete_room)
        # Each entry path's `close` handler shuts the job down from here.
        session.shutdown()

    def _on_close(self, _ev: CloseEvent) -> None:
        self._ending = True
        if self._deadline is not None:
            self._deadline.cancel()
            self._deadline = None
        if self._silence_task is not None:
            self._silence_task.cancel()
            self._silence_task = None
