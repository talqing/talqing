"""Shared voice/video agent session lifecycle.

The web-room and SIP paths share one path for: load definition +
tools/hooks, compile, durable session row, live transcript,
finalize + bill + webhooks.

Media topology stays in the callers — this module is product session plumbing,
not RoomIO / Anam. Runtime path is derived from session type + channel.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import asyncpg
from livekit import rtc
from livekit.agents import (
    AgentSession,
    BackgroundAudioPlayer,
    CloseEvent,
    ConversationItemAddedEvent,
    FunctionToolsExecutedEvent,
    JobContext,
    get_job_context,
    llm,
)
from livekit.agents import vad as lk_vad

import db
from compiler.compile import CompiledAgent, compile_agent
from compiler.factories import build_noise_cancellation
from compiler.handoff import build_handoff_agent
from compiler.operations import (
    RUNTIME_KEY_AGENT_ROSTER,
    RUNTIME_KEY_BUILD_HANDOFF_AGENT,
    RUNTIME_KEY_CALL_FIELDS,
    RUNTIME_KEY_HANDOFF_TARGETS,
    RUNTIME_KEY_PROVIDER_KEYS,
    RUNTIME_KEY_RECORD_EVENT,
    RUNTIME_KEY_SCREENSHARE,
    RUNTIME_KEY_SESSION_VARS,
    RUNTIME_KEY_TOOL_SECRETS,
    RUNTIME_KEY_VOICEMAIL,
)
from services import analysis as analysis_service
from services import billing, recordings, retention, session_events, webhooks
from services.agents import AgentConfig, ConversationSpec
from services.agents.plan import load_plan_roster
from services.conversation_context import ConversationContext
from services.recordings import RecordingStatus
from services.telephony.batch import settle as batch_settle
from services.tools import AvatarState, HandoffTarget, RuntimeContext, UserData
from services.user import Tenant
from services.webhooks import events
from utils.bg import spawn
from workers.session import persistence
from workers.session.events import SessionEventLog
from workers.session.trace import wire_session_trace
from workers.voice import recording as recording_upload
from workers.voice import screenshare as screenshare_recording
from workers.voice.background_audio import start_background_audio
from workers.voice.call_bounds import CallBounds
from workers.voice.recording import RecordingOutcome, upload_session_recording
from workers.voice.screenshare import ScreenshareOutcome, ScreenshareRecorder, ScreenshareWatcher

logger = logging.getLogger("talqing.workers.voice.runtime")

# Everything finalize does apart from shipping the audio: the session row, the
# analysis LLM call, pricing and the `session.completed` webhook.
FINALIZE_WORK_TIMEOUT_SECONDS = 20.0
# Plus the worst case for the upload, which scales with the file and so with the
# call. Stated as a sum rather than as a number because the two move together: a
# recording that cannot finish inside this is killed when the job process exits,
# and the row lands on 'failed' for a recording that was seconds from stored.
# Both uploads, because they run one after the other and either can be the slow
# one: the audio scales with the call, and the screen video scales with how much
# of the screen changed. Each ceiling is its own module's arithmetic.
#
# This number only binds because the worker is configured to allow it: finalize
# runs as a job shutdown callback, and the supervisor SIGKILLs the process
# `shutdown_process_timeout` after shutdown begins. `workers/voice/main.py` sets
# that from this constant — raising the budget here without it is a budget the
# process never gets to spend.
FINALIZE_SHUTDOWN_TIMEOUT_SECONDS = (
    FINALIZE_WORK_TIMEOUT_SECONDS
    + recording_upload.MAX_UPLOAD_TIMEOUT_SECONDS
    + screenshare_recording.MAX_UPLOAD_TIMEOUT_SECONDS
)


@dataclass(frozen=True)
class VoiceSessionSpec:
    """Identity and routing for one voice/video agent run."""

    tenant: Tenant
    session_id: str
    # Both None when this call runs an agent defined in the request that started
    # it: there is no `agents` row and no frozen version. The cast then comes
    # from `sessions.agent_plan`, which `prepare` reads for every run.
    agent_id: str | None
    agent_version: int | None
    session_type: str = "WEB"
    conversation_id: str | None = None
    participant_identity: str | None = None
    # Dispatch/token overlay; wins over durable conversation userdata keys.
    initial_userdata: Mapping[str, object] = field(default_factory=dict)
    allowed_channels: Collection[str] = ("voice", "video")
    # SIP session columns (optional).
    phone_number_id: str | None = None
    telephony_account_id: str | None = None
    livekit_room: str | None = None
    from_e164: str | None = None
    to_e164: str | None = None
    conversation_ref_id: str | None = None
    idempotency_key: str | None = None
    # A WhatsApp call: the integration it arrived on and the trigger answering it.
    integration_id: str | None = None
    trigger_id: str | None = None
    # The `{{vars.*}}` this call was started with, for the paths where the WORKER
    # mints the session row. A media stream is the only one: the partner's
    # per-call metadata arrives in the dispatch metadata and there is no earlier
    # row to have carried it. Every other voice path pre-mints its row with the
    # values already on it (`calls_token`, `record_outbound_call`) and leaves
    # this None, so `_resolve_cast` reads them back off the row as it always has.
    session_vars: Mapping[str, str] | None = None
    # Set when the surface, not the agent, decides what history this call loads.
    # A WhatsApp call is the only one: it always carries the chat on.
    conversation_context: ConversationContext | None = None


class VoiceRun:
    """One compiled AgentSession with shared persistence and finalize lifecycle.

    Callers own media (room connect / avatar). After ``prepare``, wire media
    then ``session.start``; call ``begin_entry`` for the root greeting/on_enter.
    Finalize via ``ensure_finalize``.
    """

    def __init__(
        self,
        *,
        spec: VoiceSessionSpec,
        cfg: AgentConfig,
        agent_version_id: str,
        session: AgentSession[UserData],
        agent: CompiledAgent,
        provider_keys: dict[str, str],
        tool_secrets: dict[str, str],
        handoff_targets: dict[str, HandoffTarget],
        session_status: str,
        conversation_trace: dict[str, object],
        prior_call_messages: int,
        events: SessionEventLog,
    ) -> None:
        self.spec = spec
        self.cfg = cfg
        self.agent_version_id = agent_version_id
        self.session = session
        self.agent = agent
        # The tenant's BYOK keys this run compiled from — the media layer starts
        # the Anam avatar from the same map.
        self.provider_keys = provider_keys
        # The tenant's `{{secrets.NAME}}` values, kept for the same reason: the
        # outbound SIP dial resolves its trunk credentials out of this map
        # moments after the compile that loaded it.
        self.tool_secrets = tool_secrets
        # {LiveKit agent id: HandoffTarget}, filled in by `build_handoff_agent`
        # as the call hands off. The same dict object it holds on the runtime
        # context, so this reads what that wrote. Its one reader is the
        # `agent_handoff` transcript item, which belongs to the agent taking
        # over — and the stamp captured when the item was emitted names the one
        # handing over.
        self.handoff_targets = handoff_targets
        # Set after Anam joins the room; None when the agent has no avatar.
        self.avatar_state: AvatarState | None = None
        # The person's screen, on a web room where the agent watches one. The
        # watcher is built before this object exists (the compiled agent captures
        # its `peek` off the runtime context), so both are handed over by the
        # media layer the way `avatar_state` is. Absent on SIP and text, which is
        # exactly what tells the compiled agent it cannot see.
        self.screenshare_watcher: ScreenshareWatcher | None = None
        self.screenshare_recorder: ScreenshareRecorder | None = None
        # Set by `build_noise_cancellation` on the room paths; kept because the
        # enhancer is the one component that can fail after the call is up
        # without stopping it — see the check in `_finalize_once`.
        self.noise_cancellation: rtc.FrameProcessor[rtc.AudioFrame] | None = None

        # Where this call stands with respect to a transfer to a human. None on
        # every call that never attempts one. The values are defined in
        # `workers/voice/transfer.py` (in_flight / referred / bridged) and read
        # by the SIP disconnect and session-close handlers in `sip.py`, which is
        # why they live in one place rather than being invented at each site.
        self.transfer_state: str | None = None
        # The transfer record written to `sessions.transfer`, kept here so the
        # analysis prompt can say the call was escalated rather than trailing
        # off mid-sentence.
        self.transfer: dict[str, object] | None = None
        # The warm-transfer briefing legs (`workers/voice/warm_transfer.py`).
        # Each is a full speech-to-text + LLM + text-to-speech conversation on a
        # second AgentSession, and usage is read off one session at finalize —
        # so without this list the briefing is unbilled and uncosted. A list
        # because a declined transfer can be followed by another attempt.
        self.briefing_sessions: list[AgentSession[UserData]] = []
        # The ambient/thinking bed, owned here because a warm transfer stops it
        # for the hold and starts a fresh one if the caller comes back.
        self.background_audio: BackgroundAudioPlayer | None = None
        # Built here rather than at entry because the `voicemail_detected` tool
        # reaches it through the runtime context, which is compiled before entry.
        self.call_bounds = CallBounds(self)

        # Durable runtime trace (provider health, tool timing, connection
        # quality, lifecycle). Transport events are added by `wire_room_events`.
        # Built in `prepare` rather than here because the compiled tools capture
        # its `record` off the runtime context, and that happens before this
        # object exists.
        self.events = events

        # What this call started knowing (`_load_conversation_state`). Recorded
        # with session.started rather than stored on the session row: everything
        # else about the setup is already recoverable through
        # `sessions.agent_version_id` → `agent_versions.config`, and copying
        # agent fields into `sessions` to answer one debugging question is the
        # road that ends with all of them copied.
        self._conversation_trace = conversation_trace
        # How much of the seeded context belongs to EARLIER calls, so the warm
        # transfer briefing can label it as such instead of reciting it as if
        # the caller had just said it (`workers/voice/warm_transfer.py`).
        self.prior_call_messages = prior_call_messages

        self._session_status = session_status
        self._promoted = session_status == "running"
        # This tenant's active webhooks, read once for the life of the job. See
        # `_webhook_subscriptions`.
        self._webhooks: Sequence[asyncpg.Record] | None = None
        self._transcript_tasks: set[asyncio.Task[object]] = set()
        self._finalize_lock = asyncio.Lock()
        self._finalized = False
        self._finalize_task: asyncio.Task[None] | None = None
        self._close_reason: str | None = None
        self._error: BaseException | None = None
        self._ended_at: datetime | None = None
        self._framework_close_reason: str | None = None
        # When this call's content is scheduled to be deleted under the
        # organization's retention policy, set during finalize. None while the
        # policy is unlimited, which is the default.
        self._purge_at: datetime | None = None

        self._wire_events()

    # ── construction ────────────────────────────────────────────────────────

    @classmethod
    async def prepare(
        cls,
        spec: VoiceSessionSpec,
        vad: lk_vad.VAD,
        *,
        session_status: str = "running",
        runtime_extra: RuntimeContext | None = None,
        screenshare_watcher: ScreenshareWatcher | None = None,
    ) -> VoiceRun:
        """Load published definition, compile, create session row, wire events.

        ``runtime_extra`` adds keys to the runtime context the compiled tools
        capture — the SIP paths use it for the two things only a phone call has
        (the transfer callable and this call's phone numbers), and the web-room
        path for the one thing only a browser has: the getter for the newest
        frame of a shared screen. It has to be supplied here rather than set
        afterwards because the prompt is personalized and
        `compiler/tools.py::_make_dispatcher` copies the context into each tool
        during the `compile_agent` below; the absence of a key is what makes an
        operation refuse a run it does not belong on, and what keeps the same
        `voice` config from offering a phone caller a screen share.

        ``screenshare_watcher`` is that last one, and it is a parameter of its
        own rather than a `runtime_extra` key because the decision needs the
        config this method is the one to load: the key goes in only when this
        agent asked to watch a screen, and its presence is what tells the
        compiled agent it can see. The media layer supplies a watcher; whether
        the agent gets to use it is settled here, once.
        """
        if session_status not in ("queued", "running"):
            raise ValueError(f"session_status must be queued or running, got {session_status}")

        _t0 = time.monotonic()
        cfg, agent_version_id, frozen_tool_defs, roster, session_vars = await _resolve_cast(spec)
        logger.info("latency-debug prepare resolve_cast %d ms", (time.monotonic() - _t0) * 1000)
        # The two sources are mutually exclusive by construction — a row either
        # existed before this ran or it did not — and merging rather than picking
        # keeps that from being a rule anyone has to know. `create_session` below
        # writes the result, so the row a webhook or the call detail reads later
        # says the same thing the prompt was built from.
        if spec.session_vars:
            session_vars = {**session_vars, **dict(spec.session_vars)}
        if cfg.channel not in spec.allowed_channels:
            raise RuntimeError(
                f"agent channel {cfg.channel!r} is not allowed for session type "
                f"{spec.session_type!r} (allowed: {sorted(spec.allowed_channels)})"
            )

        # Everything between `_resolve_cast` and `create_session` is
        # independent: `cfg` decides what each one loads, and none of them reads
        # another's result. Sequential `await`s here were four round trips the
        # caller waited through in a row, for no ordering that exists. Behind
        # pgbouncer the four client connections this takes at once are cheap —
        # server connections are held per statement, not per call — which is
        # what turns "parallelise these" from a bad idea into the obvious one.
        #
        # `gather` propagates the first exception, which is what these already
        # did one at a time: an attached tool, integration or hook that will
        # not load must stop the session rather than silently change what the
        # agent can do.
        (
            (tool_defs, integrations, tool_secrets),
            faqs,
            provider_keys,
            hook_trees,
            tasks,
            state,
        ) = await asyncio.gather(
            _load_tools(spec.tenant, cfg, frozen_tool_defs),
            persistence.load_faqs(spec.tenant, cfg.faqs),
            persistence.load_provider_keys(spec.tenant),
            _load_hooks(spec.tenant, cfg, frozen_tool_defs),
            # Read here rather than at entry so a task tool costs no database
            # round trip mid-call — and so a pinned version that has gone missing
            # fails the session at start rather than on the turn that calls it.
            persistence.resolve_pinned_tasks(spec.tenant, cfg),
            _load_conversation_state(
                spec.tenant,
                spec=spec,
                conversation=(
                    cfg.conversation.model_copy(update={"context": spec.conversation_context})
                    if spec.conversation_context
                    else cfg.conversation
                ),
            ),
        )
        logger.info("latency-debug prepare gather %d ms", (time.monotonic() - _t0) * 1000)

        # Before `compile_agent`, because the tools capture the runtime context
        # by value as they are built — a trace handed over afterwards would
        # never reach them.
        events = SessionEventLog(spec.tenant, spec.session_id)
        handoff_targets: dict[str, HandoffTarget] = {}
        runtime_context = _runtime_context(
            spec,
            channel=cfg.channel,
            events=events,
            roster=roster,
            provider_keys=provider_keys,
            tool_secrets=tool_secrets,
            handoff_targets=handoff_targets,
        )
        # Set ONCE, here, and never rebound per agent: these belong to the
        # session, so `compiler.handoff.next_runtime_context` carries them into
        # every team member and every handoff target. Each agent's own declared
        # defaults are bound separately, by `compile_agent` below.
        runtime_context[RUNTIME_KEY_SESSION_VARS] = session_vars
        if runtime_extra:
            runtime_context.update(runtime_extra)
        # Filled the moment the run exists: the tools capture the context below,
        # before there is a run for `voicemail_detected` to end.
        run_holder: list[VoiceRun] = []
        call_fields = runtime_context.get(RUNTIME_KEY_CALL_FIELDS) or {}
        if call_fields.get("direction") == "outbound":

            async def end_on_voicemail(message: str | None) -> None:
                await run_holder[0].call_bounds.voicemail(message)

            runtime_context[RUNTIME_KEY_VOICEMAIL] = end_on_voicemail
        watching_screen = screenshare_watcher is not None and cfg.vision_input.screenshare.enabled
        if watching_screen:
            assert screenshare_watcher is not None
            runtime_context[RUNTIME_KEY_SCREENSHARE] = screenshare_watcher.peek
        # No `else` branch, deliberately: a `voice` agent that watches a screen on
        # the web is reached over SIP from the same stored config, and a phone
        # caller having no screen is the expected outcome rather than a fault.
        # The one case that IS a fault — a web room with no participant to link
        # to — is reported by `room_web`, which is where it is discovered.
        session, agent = compile_agent(
            cfg,
            vad,
            provider_keys,
            tasks=tasks,
            faqs=faqs,
            tool_defs=tool_defs,
            integrations=integrations,
            tool_secrets=tool_secrets,
            hook_trees=hook_trees,
            tenant=spec.tenant,
            agent_id=spec.agent_id,
            version=spec.agent_version,
            session_id=spec.session_id,
            userdata=state.userdata,
            participant_identity=spec.participant_identity,
            runtime_context=runtime_context,
            chat_ctx=state.chat_ctx,
        )
        logger.info("latency-debug prepare compile %d ms", (time.monotonic() - _t0) * 1000)

        await persistence.create_session(
            spec.tenant,
            spec.session_id,
            spec.agent_id,
            agent_version_id,
            agent_name=cfg.name,
            channel=cfg.channel,
            type_=spec.session_type,
            conversation_id=spec.conversation_id,
            conversation_ref_id=spec.conversation_ref_id,
            integration_id=spec.integration_id,
            trigger_id=spec.trigger_id,
            phone_number_id=spec.phone_number_id,
            telephony_account_id=spec.telephony_account_id,
            livekit_room=spec.livekit_room,
            from_e164=spec.from_e164,
            to_e164=spec.to_e164,
            idempotency_key=spec.idempotency_key,
            status=session_status,
            # So a live call reads as "analysis is coming", not "analysis is off
            # for this agent" — which is what the default said, for the whole
            # call, and for ever after on a call whose worker died. Same for
            # recording: 'pending' rather than "no recording", settling into
            # stored/failed/consent_withdrawn at finalize.
            analysis_enabled=cfg.analysis.enabled,
            recording_enabled=cfg.recording.enabled,
            # A no-op on every pre-minted row: the upsert COALESCEs onto what is
            # already there, and what is already there is where these came from.
            session_vars=dict(session_vars) or None,
        )
        logger.info("latency-debug prepare create_session %d ms", (time.monotonic() - _t0) * 1000)

        run = cls(
            spec=spec,
            cfg=cfg,
            agent_version_id=agent_version_id,
            session=session,
            agent=agent,
            provider_keys=provider_keys,
            tool_secrets=tool_secrets,
            handoff_targets=handoff_targets,
            session_status=session_status,
            conversation_trace=state.trace,
            prior_call_messages=state.prior_call_messages,
            events=events,
        )
        run_holder.append(run)
        if watching_screen:
            # Handed back so the media layer attaches the one the agent is
            # actually compiled against, rather than deciding a second time.
            run.screenshare_watcher = screenshare_watcher
        return run

    # ── promotion / entry / close signals ───────────────────────────────────

    async def promote_running(
        self,
        *,
        from_e164: str | None = None,
        to_e164: str | None = None,
        livekit_room: str | None = None,
        idempotency_key: str | None = None,
    ) -> None:
        """Promote a queued session row to running (post-answer).

        Used by SIP outbound after the callee answers so ring time is not part
        of billable wall-clock.

        The four keyword arguments are the columns the outbound dial learns only
        once LiveKit has placed the call. They ride the upsert this already is —
        writing them first and promoting second was two statements against one
        row, and a window in which the row said `queued` about a call that had
        answered. Absent means "whatever the spec carries", which is what every
        other caller wants.
        """
        if self._promoted:
            return
        await persistence.create_session(
            self.spec.tenant,
            self.spec.session_id,
            self.spec.agent_id,
            self.agent_version_id,
            agent_name=self.cfg.name,
            channel=self.cfg.channel,
            type_=self.spec.session_type,
            conversation_id=self.spec.conversation_id,
            integration_id=self.spec.integration_id,
            trigger_id=self.spec.trigger_id,
            phone_number_id=self.spec.phone_number_id,
            telephony_account_id=self.spec.telephony_account_id,
            livekit_room=livekit_room or self.spec.livekit_room,
            from_e164=from_e164 or self.spec.from_e164,
            to_e164=to_e164 or self.spec.to_e164,
            conversation_ref_id=self.spec.conversation_ref_id,
            idempotency_key=idempotency_key or self.spec.idempotency_key,
            status="running",
            analysis_enabled=self.cfg.analysis.enabled,
            recording_enabled=self.cfg.recording.enabled,
        )
        self._session_status = "running"
        self._promoted = True
        self.emit_session_started()

    def build_noise_cancellation(self) -> rtc.FrameProcessor[rtc.AudioFrame] | None:
        """The enhancer for this session's inbound audio, or None when it is off.

        Room paths only — pass the result to `room_options`. Kept on the run so
        `_finalize_once` can tell whether it survived the call.
        """
        self.noise_cancellation = build_noise_cancellation(
            self.cfg.noise_cancellation, self.provider_keys
        )
        return self.noise_cancellation

    async def start_background_audio(self, ctx: JobContext) -> None:
        """Start this agent's ambient/thinking bed, if it has one, and own it.

        Safe to call twice: a warm transfer stops the bed for the hold and calls
        this again when a failed transfer hands the caller back.
        """
        if not self.cfg.background_audio.enabled or self.background_audio is not None:
            return
        try:
            self.background_audio = await start_background_audio(ctx, self.session, self.cfg)
        except Exception:
            logger.exception("background audio failed to start")

    async def start_screenshare_recording(
        self, ctx: JobContext, watcher: ScreenshareWatcher
    ) -> None:
        """Begin writing the screen video, and say so on the row.

        The file goes in the job process's `session_directory`, which dies with
        the process — so `_finalize_screenshare_recording` has to ship it before
        teardown, exactly as the audio does.
        """
        recorder = ScreenshareRecorder(
            watcher, ctx.session_directory / f"screen.{recordings.SCREENSHARE_FILE_EXTENSION}"
        )
        recorder.start()
        self.screenshare_recorder = recorder
        # So a live call reads as "recording the screen", not "screen recording
        # is off". The row settles into stored/not_shared/failed at finalize.
        await persistence.set_screenshare_recording(
            self.spec.tenant, self.spec.session_id, status=RecordingStatus.PENDING
        )

    async def stop_background_audio(self) -> None:
        """Stop the bed and let go of the player. It cannot be reopened."""
        player, self.background_audio = self.background_audio, None
        if player is None:
            return
        try:
            await player.aclose()
        except Exception:
            logger.exception("background audio close failed")

    def emit_session_started(self) -> None:
        """Fire-and-forget session.started (+ session.in_progress for SIP)."""
        self.events.record(
            session_events.SESSION_STARTED,
            {
                "agent_id": self.spec.agent_id,
                "version": self.spec.agent_version,
                "channel": self.cfg.channel,
                "type": self.spec.session_type,
            },
        )
        self.events.record(
            session_events.CONVERSATION_CONTEXT_LOADED, dict(self._conversation_trace)
        )
        spawn(self._dispatch_session_started())

    async def begin_entry(self) -> None:
        """Root agent greeting / on_enter after media is live."""
        # The length limit counts from here — media up, the agent about to speak —
        # and the silence check-ins are armed with it.
        self.call_bounds.start()
        await self.agent.run_entry(initial=True)
        # After the greeting/on_enter: the agent can now hold a conversation.
        # The snapshot reads the absence of this as "never became ready".
        self.events.record(
            session_events.AGENT_READY,
            {
                "llm": self.cfg.llm.model if self.cfg.llm else None,
                "stt": self.cfg.stt.model if self.cfg.stt else None,
                "tts": self.cfg.tts.model if self.cfg.tts else None,
                "realtime": self.cfg.realtime.model if self.cfg.realtime else None,
            },
        )

    def mark_ended(self) -> None:
        if self._ended_at is None:
            self._ended_at = datetime.now(UTC)

    def set_close_reason(self, reason: str) -> None:
        """Name the close before tearing anything down.

        Finalize keeps the first reason it is given, and closing the session
        makes the framework offer its own generic one — so a caller that knows
        why the call is ending has to say so before it starts the teardown.
        """
        if self._close_reason is None:
            self._close_reason = reason

    @property
    def framework_close_reason(self) -> str | None:
        return self._framework_close_reason

    # ── finalize ────────────────────────────────────────────────────────────

    def ensure_finalize(
        self, reason: str | None, *, error: BaseException | None = None
    ) -> asyncio.Task[None]:
        """Start finalize at most once; safe from close/shutdown/error paths.

        ``error`` keeps the same first-one-wins discipline as the reason beside
        it: whoever named the failure first is the one who knew what it was.
        """
        self.mark_ended()
        reason = reason or "unknown"
        if self._close_reason is None:
            self._close_reason = reason
        if self._error is None:
            self._error = error
        if self._finalize_task is None:
            self._finalize_task = asyncio.create_task(
                self._finalize_once(self._close_reason),
                name="talqing_finalize_voice_session",
            )
        return self._finalize_task

    async def finalize_with_timeout(
        self,
        reason: str | None,
        *,
        error: BaseException | None = None,
        timeout: float = FINALIZE_SHUTDOWN_TIMEOUT_SECONDS,
    ) -> None:
        try:
            await asyncio.wait_for(
                asyncio.shield(self.ensure_finalize(reason, error=error)),
                timeout=timeout,
            )
        except TimeoutError:
            logger.error(
                "session finalize did not complete within %.1fs (session=%s)",
                timeout,
                self.spec.session_id,
            )

    async def _finalize_once(self, reason: str) -> None:
        async with self._finalize_lock:
            if self._finalized:
                return
            self._finalized = True

            if self._transcript_tasks:
                await asyncio.gather(*self._transcript_tasks, return_exceptions=True)

            # First, because it is the one thing here that is audible. After a
            # bridged transfer this job stays connected as the janitor for the
            # whole human-to-human conversation, and the bed would otherwise
            # play office ambience over both of them until they hung up.
            await self.stop_background_audio()

            # The ai-coustics core catches its own errors — a rejected license
            # key or an unsupported stream disables the enhancer and lets the
            # call run on the raw audio. Nothing else would ever say so, and the
            # agent editor would still be showing the toggle on.
            if self.noise_cancellation is not None and not self.noise_cancellation.enabled:
                self.events.record(
                    session_events.NOISE_CANCELLATION_FAILED,
                    {
                        "provider": self.cfg.noise_cancellation.provider,
                        "model": self.cfg.noise_cancellation.model,
                    },
                )
            self.events.record(session_events.SESSION_ENDED, {"close_reason": reason})
            # Last write wins the trace: nothing else emits after this point, so
            # draining here is what makes the timeline complete for the API.
            await self.events.drain()

            # Close Anam first so the billable avatar window ends here (not at
            # room-GC) and finalize_session reads a stamped stopped_at.
            await self._stop_avatar()

            transcript = await persistence.finalize_session(
                self.spec.tenant,
                self.spec.session_id,
                reason,
                self.session,
                self.cfg,
                avatar_state=self.avatar_state,
                ended_at=self._ended_at,
                extra_usage=self._briefing_usage(),
                error=self._error,
            )
            # If this call came from an outbound batch, its recipient leaves
            # `dialing` here — right after the two facts settlement reads
            # (`status`, `close_reason`) are written, and deliberately before the
            # recording upload, the analysis LLM call and the pricing pass below,
            # none of which it depends on. That is what frees the batch's
            # concurrency slot in a second rather than in half a minute. It
            # swallows its own errors: a batch table must never fail a call's
            # finalize, and the dispatcher's reconcile settles anything it drops.
            # Only `record_outbound_call` ever sets `sessions.batch_id`, so a web
            # or inbound call has no batch by construction and settlement would
            # open a transaction and read the row only to find `batch_id IS NULL`.
            if self.spec.session_type == "SIP_OUTBOUND":
                await batch_settle.settle_recipient(self.spec.tenant, self.spec.session_id)

            # The call's own deletion, booked the moment it ends. Nothing is
            # written for a workspace with no policy, so the common case costs
            # no rows. Before the recording ships because `recording.ready`
            # states the deadline, and the deadline is a property of the call
            # rather than of the upload.
            self._purge_at = await retention.schedule_session_purge(
                self.spec.tenant,
                self.spec.session_id,
                ended_at=self._ended_at or datetime.now(UTC),
            )

            # finalize_session already writes the contact's userdata from the
            # session bag — do not double-write here.
            #
            # Order below is load-bearing, in both directions:
            #  * the recording ships BEFORE analysis, so the dashboard's player
            #    lights up without waiting on an LLM (recording is not billed,
            #    so nothing about pricing depends on its position);
            #  * billing runs AFTER analysis, so the analysis llm_usage row is
            #    already in the table when `_load_usage` reads it. That is what
            #    lets analysis be metered for real in a single pricing pass,
            #    with no re-price and nothing to reconcile.
            await self._finalize_recording()
            await self._finalize_screenshare_recording()
            await analysis_service.run_for_session(
                self.spec.tenant,
                self.spec.session_id,
                self.cfg,
                transcript=transcript,
                close_reason=reason,
                transfer=self.transfer,
                # `create_session` claimed `analysis_status = 'pending'` when the
                # call started, precisely so a live call does not read as
                # "analysis is off". There is nothing left to claim.
                claim=False,
            )
            await billing.bill_session(self.spec.tenant, self.spec.session_id)

            # Built from the database, not from what this method happens to hold:
            # every fact above is already written, and reading it back is what
            # keeps this payload identical to the one the reconcile sweep sends
            # for a call whose worker died. See services/webhooks/payloads.py.
            # The transcript is the exception, and only because `finalize_session`
            # returned the same rows a moment ago.
            payload = await webhooks.build_session_completed(
                self.spec.tenant, self.spec.session_id, transcript=transcript
            )
            if payload is not None:
                await webhooks.dispatch_bounded(
                    self.spec.tenant,
                    events.SESSION_COMPLETED,
                    # The agent the payload NAMES, so a subscription scoped to
                    # one agent cannot receive an event that says a different
                    # one. Equal to `spec.agent_id` here — the session row is
                    # immutable and names the agent that answered — but reading
                    # it from the payload is what keeps all three producers of
                    # this event (here, the text worker, the reconcile sweep)
                    # routing the same way by construction rather than by three
                    # separate decisions that agree today.
                    payload["agent_id"],
                    payload,
                    hooks=await self._webhook_subscriptions(),
                )

    def _briefing_usage(self) -> list[Any]:
        """What the warm-transfer briefing legs cost, for this call's bill.

        Read here rather than when each briefing ended: `AgentSession.usage`
        survives `shutdown()` — the collector is only replaced in `start()` — so
        reading at finalize also catches metrics that landed during teardown.
        """
        usage: list[Any] = []
        for session in self.briefing_sessions:
            try:
                usage.extend(session.usage.model_usage)
            except Exception:
                logger.exception("could not read warm-transfer briefing usage")
        return usage

    async def _finalize_recording(self) -> None:
        """Ship (or deliberately drop) the session's audio.

        Runs inside finalize because the file lives in the job process's
        `TemporaryDirectory` and is gone once that process exits. Any failure
        here is contained: the row lands on 'failed' and the call still ends.

        Writes the outcome to the session row; `session.completed` reads it back
        from there rather than being handed it.
        """
        if not self.cfg.recording.enabled:
            return

        # Set by the `stop_recording` tool when the caller objected. Session
        # userdata is the channel because it is the one thing the tool and this
        # both hold; the `_talqing` prefix keeps it out of what gets persisted.
        userdata = self.session.userdata
        withdrawn_at = (
            userdata.get(recordings.WITHDRAWN_USERDATA_KEY) if isinstance(userdata, dict) else None
        )
        withdrawn = bool(withdrawn_at)
        if withdrawn:
            # Stamped with the moment the caller asked, not with now: the row is
            # written here, at finalize, and a compliance reader wants the point
            # in the call — see RECORDING_CONSENT_WITHDRAWN.
            asked_at = (
                datetime.fromisoformat(withdrawn_at) if isinstance(withdrawn_at, str) else None
            )
            self.events.record(
                session_events.RECORDING_CONSENT_WITHDRAWN,
                {"at": asked_at.isoformat() if asked_at else None},
                at=asked_at,
            )

        try:
            outcome = await upload_session_recording(
                self.spec.tenant,
                self.spec.session_id,
                session=self.session,
                job_ctx=get_job_context(required=False),
                withdrawn=withdrawn,
            )
        except Exception:
            logger.exception("recording finalize failed for session %s", self.spec.session_id)
            outcome = RecordingOutcome(status=RecordingStatus.FAILED)

        await persistence.set_recording(
            self.spec.tenant,
            self.spec.session_id,
            status=outcome.status,
            object_key=outcome.object_key,
            started_at=outcome.started_at,
            duration_s=outcome.duration_s,
            size_bytes=outcome.size_bytes,
        )

        if outcome.status is not RecordingStatus.STORED:
            return

        # A ready-to-use link, so a consumer that wants its own copy can take it
        # without a round trip back to the API. It is short-lived on purpose —
        # this payload lands in somebody's logs — and `get_call_recording` mints
        # a fresh one whenever they need another.
        assert outcome.object_key is not None  # STORED means an object was written
        try:
            url = await recordings.presigned_url(
                key=outcome.object_key,
                expires_in=int(recordings.PRESIGNED_TTL.total_seconds()),
            )
        except Exception:
            # Signing is local arithmetic, so this is a misconfigured endpoint
            # rather than a bucket problem — and the event still carries every
            # other fact about a recording that really was stored.
            logger.exception("could not sign a recording URL for session %s", self.spec.session_id)
            url = None

        await webhooks.dispatch_bounded(
            self.spec.tenant,
            events.RECORDING_READY,
            self.spec.agent_id,
            {
                "session_id": self.spec.session_id,
                "conversation_id": self.spec.conversation_id,
                "agent_id": self.spec.agent_id,
                "duration_s": outcome.duration_s,
                "bytes": outcome.size_bytes,
                "url": url,
                "url_expires_at": (datetime.now(UTC) + recordings.PRESIGNED_TTL).isoformat(),
                # When the platform will delete this call's content under the
                # organization's retention policy. Null while retention is
                # unlimited, which is the default.
                "expires_at": self._purge_at.isoformat() if self._purge_at else None,
            },
            hooks=await self._webhook_subscriptions(),
        )

    async def _finalize_screenshare_recording(self) -> None:
        """Ship (or deliberately drop) the screen video, and stop watching.

        A second artifact with its own outcome: this call's audio can be stored
        while the screen video is `not_shared`, because nobody shared one. Any
        failure is contained the same way — the row lands on 'failed' and the
        call still ends.
        """
        recorder, self.screenshare_recorder = self.screenshare_recorder, None
        watcher, self.screenshare_watcher = self.screenshare_watcher, None
        if recorder is not None:
            try:
                outcome = await recorder.finish(self.spec.tenant, self.spec.session_id)
            except Exception:
                logger.exception(
                    "screen recording finalize failed for session %s", self.spec.session_id
                )
                outcome = ScreenshareOutcome(status=RecordingStatus.FAILED)
            await persistence.set_screenshare_recording(
                self.spec.tenant,
                self.spec.session_id,
                status=outcome.status,
                object_key=outcome.object_key,
                started_at=outcome.started_at,
                duration_s=outcome.duration_s,
                size_bytes=outcome.size_bytes,
            )
        # After the recorder, so its last ticks still read the live buffer.
        if watcher is not None:
            try:
                await watcher.aclose()
            except Exception:
                logger.exception("screen share watcher close failed")

    async def _stop_avatar(self) -> None:
        if not self.avatar_state:
            return
        # Local import keeps SIP and text job processes off the Anam plugin.
        from workers.voice.avatar import stop_avatar

        await stop_avatar(self.avatar_state)

    async def _webhook_subscriptions(self) -> Sequence[asyncpg.Record]:
        """The tenant's active webhooks, read once for this whole call.

        A voice job process serves one tenant for one call and dispatches up to
        three events — `session.started` and `session.in_progress` on SIP, then
        `session.completed` — and each one used to re-read this tiny table.
        Nothing in a job process outlives the call, so the list cannot go stale
        across calls the way it would in `background-worker`, which is why that
        one keeps re-reading.

        A failed read is not cached: it returns "no subscriptions" for this event
        only, so `session.completed` still gets its own attempt.
        """
        if self._webhooks is None:
            try:
                self._webhooks = await webhooks.load_subscriptions(self.spec.tenant)
            except Exception:
                logger.exception(
                    "could not load webhook subscriptions for tenant %s", self.spec.tenant.id
                )
                return []
        return self._webhooks

    async def _dispatch_session_started(self) -> None:
        payload = {
            "session_id": self.spec.session_id,
            "conversation_id": self.spec.conversation_id,
            "agent_id": self.spec.agent_id,
            "version": self.spec.agent_version,
            "channel": self.cfg.channel,
            "type": self.spec.session_type,
            "phone_number_id": self.spec.phone_number_id,
            "integration_id": self.spec.integration_id,
            "from_e164": self.spec.from_e164,
            "to_e164": self.spec.to_e164,
        }
        try:
            await webhooks.dispatch(
                self.spec.tenant,
                events.SESSION_STARTED,
                self.spec.agent_id,
                payload,
                hooks=await self._webhook_subscriptions(),
            )
        except Exception:
            logger.exception("session.started webhook dispatch failed")
        # Telephony also exposes session.in_progress (same moment the call is live).
        if self.spec.session_type in ("SIP_INBOUND", "SIP_OUTBOUND", "WHATSAPP_INBOUND"):
            try:
                await webhooks.dispatch(
                    self.spec.tenant,
                    events.SESSION_IN_PROGRESS,
                    self.spec.agent_id,
                    payload,
                    hooks=await self._webhook_subscriptions(),
                )
            except Exception:
                logger.exception("session.in_progress webhook dispatch failed")

    # ── event wiring ────────────────────────────────────────────────────────

    def current_agent_stamp(self) -> tuple[str | None, int | None]:
        """The agent speaking right now — the entry agent, or a handoff target.

        `session.current_agent` is the single source; nothing is cached on the
        run beside it. It raises until the session is running, which is exactly
        the window in which the entry agent is the answer.
        """
        try:
            agent = self.session.current_agent
        except RuntimeError:
            agent = self.agent
        return (
            getattr(agent, "_talqing_agent_id", None) or self.spec.agent_id,
            getattr(agent, "_talqing_version", None),
        )

    def _wire_events(self) -> None:
        session = self.session
        spec = self.spec

        def schedule_transcript(items: Sequence[llm.ChatItem]) -> None:
            if not items:
                return
            # Who is speaking is captured HERE, when the items are emitted, not
            # inside the task: the tasks run concurrently, so two items either
            # side of a handoff could otherwise both read the post-handoff agent.
            agent_id, agent_version = self.current_agent_stamp()
            # LiveKit EventEmitter is sync-only; tasks are awaited before finalize.
            task = asyncio.create_task(
                persistence.persist_transcript_items(
                    spec.tenant,
                    spec.session_id,
                    items,
                    conversation_id=spec.conversation_id,
                    session_type=spec.session_type,
                    agent_id=agent_id,
                    agent_version=agent_version,
                    handoff_targets=self.handoff_targets,
                ),
                name="talqing_persist_transcript_items",
            )
            self._transcript_tasks.add(task)
            task.add_done_callback(self._transcript_tasks.discard)

        @session.on("conversation_item_added")
        def _on_conversation_item_added(ev: ConversationItemAddedEvent) -> None:
            item = getattr(ev, "item", None)
            if item is not None:
                schedule_transcript([item])

        @session.on("function_tools_executed")
        def _on_function_tools_executed(ev: FunctionToolsExecutedEvent) -> None:
            # One event, one write. A tool turn emits the call and its output
            # together — two to four items in the same tick — and each used to
            # take its own pooled connection and its own round trip.
            # Since livekit-agents 1.7 every call has an output, including one
            # whose tool raised `StopResponse` — that arrives as an empty output
            # that asks for no reply, rather than as a gap in the transcript.
            schedule_transcript([*ev.function_calls, *ev.function_call_outputs])

        @session.on("close")
        def _on_close(ev: CloseEvent) -> None:
            reason = getattr(getattr(ev, "reason", None), "value", None) or str(
                getattr(ev, "reason", "unknown")
            )
            self._framework_close_reason = reason
            self.ensure_finalize(reason)

        wire_session_trace(session, self.events, realtime=self.cfg.realtime is not None)

    def wire_room_events(self, room: rtc.Room) -> None:
        """Trace transport health for the call's LiveKit room."""

        # LiveKit emits (participant, quality) in that order, and the participant
        # is None when the room has already forgotten a leaver.
        @room.on("connection_quality_changed")
        def _on_quality(
            participant: rtc.Participant | None, quality: rtc.ConnectionQuality
        ) -> None:
            self.events.record(
                session_events.RTC_QUALITY,
                {
                    "identity": participant.identity if participant else None,
                    # Whose network this reading is about, decided HERE where the
                    # room knows — not by matching identity strings downstream.
                    # "the connection was poor" is a support ticket when it is
                    # our leg and a shrug when it is the caller's wifi, and a
                    # sentence that cannot tell them apart blames us for both.
                    "side": (
                        None
                        if participant is None
                        else "agent"
                        if participant.identity == room.local_participant.identity
                        else "caller"
                    ),
                    # QUALITY_EXCELLENT → "excellent", matching
                    # session_events.CONNECTION_QUALITY_RANK.
                    "quality": quality.name.removeprefix("QUALITY_").lower(),
                },
            )

        @room.on("participant_connected")
        def _on_joined(participant: rtc.RemoteParticipant) -> None:
            self.events.record(
                session_events.PARTICIPANT_JOINED, {"identity": participant.identity}
            )

        @room.on("participant_disconnected")
        def _on_left(participant: rtc.RemoteParticipant) -> None:
            self.events.record(session_events.PARTICIPANT_LEFT, {"identity": participant.identity})


# ── load helpers (definition side-effects kept in one place) ────────────────


async def _load_tools(
    tenant: Tenant,
    cfg: AgentConfig,
    frozen_tool_defs: list,
) -> tuple[list, list, dict[str, str]]:
    # Fail loudly: attached tools/integrations must load or the session should not run.
    tool_defs = persistence.select_tool_definitions(frozen_tool_defs, cfg.tools)
    # This agent's MCP servers and the tenant's secrets: neither read needs the
    # other's answer, and this is the longest chain in `prepare`'s gather.
    integrations, tool_secrets = await asyncio.gather(
        persistence.load_mcp_integrations(tenant, cfg.mcps),
        persistence.load_tool_secrets(tenant),
    )
    return tool_defs, integrations, tool_secrets


async def _load_hooks(tenant: Tenant, cfg: AgentConfig, frozen_tool_defs: list) -> dict:
    # Fail prepare loudly — missing hooks must not run as a silent no-op.
    return await persistence.load_hook_trees(tenant, cfg, frozen_tool_defs)


_SUMMARY_BLOCK_HEADER = (
    "Summaries of your earlier conversations with this person, oldest first. This is "
    "background only: none of it was said on the call that is starting now, and the person "
    "has not heard any of it in this call."
)


@dataclass(frozen=True)
class ConversationState:
    """What this run starts with, and a record of how much that turned out to be."""

    chat_ctx: llm.ChatContext
    userdata: UserData
    # How many user/assistant messages at the front of the context came from
    # EARLIER calls. The warm-transfer briefing renders the session's messages
    # under "# The call so far", and under `transcript` that heading would be a
    # lie — a fact from three weeks ago briefed as something the caller just
    # said. W5a's markers do not fix it on their own: they are `system`
    # messages, and the briefing only reads `user`/`assistant` ones.
    prior_call_messages: int
    # session_events payload: the one fact no config can reconstruct afterwards
    # is that `summary` found nothing, because re-running the query later returns
    # today's answer rather than the one from call time.
    trace: dict[str, object]


async def _load_conversation_state(
    tenant: Tenant,
    *,
    spec: VoiceSessionSpec,
    conversation: ConversationSpec,
) -> ConversationState:
    """Build the opening chat context and userdata for one run.

    Three modes, and only two of them read anything:

    * ``none`` — the run was pointed at a brand-new conversation, so there is
      nothing to load and nothing to say about it.
    * ``summary`` — one system message of what happened on earlier calls.
    * ``transcript`` — the stored timeline, with a marker at every call boundary
      (not only before the current call) so the model can tell last week's call
      from the one before it.
    """
    overlay = dict(spec.initial_userdata or {})
    trace: dict[str, object] = {
        "context": conversation.context,
        "summaries": 0,
        "history_items": 0,
        "userdata_initialized": False,
    }
    if conversation.context == "none" or not spec.conversation_id:
        return ConversationState(llm.ChatContext(), overlay, prior_call_messages=0, trace=trace)

    # What we already know about this person. Started here rather than read after
    # the history below, because the two answer different questions about the
    # same conversation and neither needs the other — this is the other chain in
    # `prepare`'s gather that would otherwise be two round trips deep.
    userdata_read = (
        asyncio.create_task(persistence.load_ref_userdata(tenant, spec.conversation_id))
        if conversation.initialize_userdata
        else None
    )
    try:
        chat_ctx, prior_call_messages = await _load_history(
            tenant, spec=spec, conversation=conversation, trace=trace
        )
    except BaseException:
        # Nothing will collect it now, and an abandoned task logs an unretrieved
        # exception long after the call it belonged to has gone.
        if userdata_read is not None:
            userdata_read.cancel()
        raise

    conversation_userdata: dict[str, object] = {}
    if userdata_read is not None:
        conversation_userdata = await userdata_read
        trace["userdata_initialized"] = True
    # What we know about this person is the base; dispatch/token overlay wins.
    return ConversationState(
        chat_ctx,
        {**conversation_userdata, **overlay},
        prior_call_messages=prior_call_messages,
        trace=trace,
    )


async def _load_history(
    tenant: Tenant,
    *,
    spec: VoiceSessionSpec,
    conversation: ConversationSpec,
    trace: dict[str, object],
) -> tuple[llm.ChatContext, int]:
    """The chat context this run opens with, and how much of it is older calls."""
    chat_ctx = llm.ChatContext()
    prior_call_messages = 0
    if conversation.context == "transcript":
        chat_ctx = await persistence.load_conversation_chat_context(tenant, spec.conversation_id)
        trace["history_items"] = len(chat_ctx.items)
        prior_call_messages = sum(
            1 for message in chat_ctx.messages() if message.role in ("user", "assistant")
        )
        if chat_ctx.items:
            chat_ctx.add_message(
                role="system",
                content=persistence.current_call_marker(datetime.now(UTC)),
            )
    elif spec.conversation_ref_id:
        # A run with no identity cannot have earlier calls to summarize. Today
        # only the direct (off-room) paths reach here without one.
        rows = await persistence.load_conversation_summaries(
            tenant,
            conversation_ref_id=spec.conversation_ref_id,
            exclude_session_id=spec.session_id,
            limit=conversation.summary_limit,
        )
        trace["summaries"] = len(rows)
        # No summaries is an ordinary empty result, not an error: a tenant can
        # switch analysis summaries off after publishing, and a call whose
        # earlier calls were all too short to analyse has nothing to say either.
        if rows:
            chat_ctx.add_message(
                role="system", content=persistence.summary_block(_SUMMARY_BLOCK_HEADER, rows)
            )
    return chat_ctx, prior_call_messages


async def _resolve_cast(
    spec: VoiceSessionSpec,
) -> tuple[AgentConfig, str | None, list, dict[str, AgentConfig], dict[str, str]]:
    """The entry agent, its pinned tools, the roster it can hand off inside, and
    the `{{vars.*}}` values this session was started with.

    The vars ride out of here rather than being fetched separately because they
    live on the same row as the plan — one read answers both.

    Two shapes, one return. Without a plan — every call that names an agent and
    stops there — this is exactly what it always was: one frozen version, its
    pinned tools, and a roster of one.

    With a plan, EVERY member is resolved here, in one query, rather than lazily
    at handoff: resolving a member needs its base version config, so a lazy
    resolve would put a database read in the middle of a handoff, and a member
    whose base has been deleted would only be discovered with the caller already
    asking to be transferred. What stays lazy is everything expensive — a
    member's tools, hooks and MCP servers load when the call actually
    hands off to it, exactly as `build_handoff_agent` does today. A five-member
    team costs one extra query at answer time and nothing at all for the four
    agents that may never speak.
    """
    plan, session_vars = await persistence.load_session_agent_plan(spec.tenant, spec.session_id)
    if not plan:
        if spec.agent_id is None or spec.agent_version is None:
            raise RuntimeError(
                f"session {spec.session_id} names no agent and carries no agent plan"
            )
        loaded = await persistence.load_definition(spec.tenant, spec.agent_id, spec.agent_version)
        if not loaded:
            raise RuntimeError(
                f"no published definition for agent {spec.agent_id} version {spec.agent_version}"
            )
        cfg, agent_version_id, frozen_tool_defs = loaded
        return cfg, agent_version_id, frozen_tool_defs, {cfg.name: cfg}, session_vars

    pool = await db.tenant_pool(spec.tenant)
    members = await load_plan_roster(pool, spec.tenant.id, plan)
    entry = members[0]
    agent_version_id = None
    if entry.agent_id and entry.version:
        version_id = await pool.fetchval(
            "SELECT id FROM agent_versions WHERE agent_id = $1::uuid AND version = $2 "
            "AND tenant_id = $3",
            entry.agent_id,
            entry.version,
            spec.tenant.id,
        )
        agent_version_id = str(version_id) if version_id else None
    # The API validated every resolved config against the same rules publishing
    # uses, and both inputs are immutable afterwards, so nothing is re-validated
    # here — a second source of truth could only ever agree.
    frozen_tool_defs = await persistence.resolve_pinned_tools(spec.tenant, entry.config)
    return (
        entry.config,
        agent_version_id,
        frozen_tool_defs,
        {m.name: m.config for m in members},
        session_vars,
    )


def _runtime_context(
    spec: VoiceSessionSpec,
    *,
    channel: str,
    events: SessionEventLog,
    roster: dict[str, AgentConfig],
    provider_keys: dict[str, str],
    tool_secrets: dict[str, str],
    handoff_targets: dict[str, HandoffTarget],
) -> RuntimeContext:
    ctx: RuntimeContext = {
        "channel": channel,
        "session_type": spec.session_type,
        "runtime_id": spec.session_id,
        "session_id": spec.session_id,
        "conversation_id": spec.conversation_id,
    }
    ctx[RUNTIME_KEY_BUILD_HANDOFF_AGENT] = build_handoff_agent
    # The cast this call runs, so a handoff destination with no `agent_id`
    # resolves. One entry on an ordinary call, which is what makes such a
    # destination a validation error there rather than a dead tool.
    ctx[RUNTIME_KEY_AGENT_ROSTER] = roster
    # Lets `compiler/handoff.py` and `compile.py`'s recording hooks put their own
    # rows on this call's trace, from code with no other route to it.
    ctx[RUNTIME_KEY_RECORD_EVENT] = events.record
    # Both tenant-scoped and both already loaded, so a handoff does not read
    # them again with the caller waiting — and the call runs to completion on
    # the credentials it answered with.
    ctx[RUNTIME_KEY_PROVIDER_KEYS] = provider_keys
    ctx[RUNTIME_KEY_TOOL_SECRETS] = tool_secrets
    # Written by `build_handoff_agent`, read by the run. Mutable and shared by
    # reference — `next_runtime_context` copies the mapping, not this dict.
    ctx[RUNTIME_KEY_HANDOFF_TARGETS] = handoff_targets
    return ctx
