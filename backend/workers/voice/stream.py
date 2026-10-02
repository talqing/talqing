"""LiveKit room path for a partner's WebSocket media stream.

`run_sip_call`'s inbound half with the telephony taken out. The gateway
(`api/stream/`) holds the socket and joins this call's room as an ordinary
participant, so by the time this runs the room contains exactly what a web room
contains — a participant publishing audio — and `VoiceRun` runs unchanged.

Four things are deliberately different from SIP, and each is a decision rather
than an omission:

* **No transfer.** The partner owns the PSTN, so bridging a human in over our own
  trunk would have us paying for the call leg on a call whose entire premise is
  that they do not. `RUNTIME_KEY_TRANSFER` is withheld and the `transfer`
  operation refuses itself with the message it already has. *Stated plainly: a
  stream agent cannot escalate to a human until transfer-by-signal ships, and the
  docs say so rather than letting a partner find out in UAT.*
* **No refuse-on-unknown-caller.** SIP turns away a caller whose number it cannot
  read, because a SIP call always HAS one and an unreadable one would collapse
  strangers onto a single conversation. A Twilio `<Connect><Stream>`
  legitimately carries no caller number at all, and refusing it would refuse the
  most common integration in this market. An unidentified stream caller — no
  number, or a value that is not one (`services/streams/parties.py`) — gets a
  conversation of its own with no cross-call history, which is exactly what it
  is.
* **No background audio.** `BackgroundAudioPlayer` works by publishing a SECOND
  audio track into the room, and a phone call only hears the bed because
  livekit-sip mixes every track the agent publishes. The gateway does not mix —
  it forwards the microphone track and nothing else — so starting the player here
  would publish a bed nobody hears while giving the gateway a second track to
  mistake for the agent. Left off at the source rather than filtered at the
  gateway, so the two ends agree about what exists. The dashboard says so on the
  connection, because a tenant who switched an ambient bed on is owed the
  sentence. Mixing in the gateway (`rtc.AudioMixer`) is the upgrade path.
* **The close reason usually comes from the gateway**, over the room's data
  channel, because the partner's own `end` reason reaches nothing else.
"""

from __future__ import annotations

import json
import logging
from uuid import UUID, uuid4

from livekit import rtc
from livekit.agents import AutoSubscribe, CloseEvent, JobContext
from livekit.agents.voice.io import AudioOutput, AudioOutputCapabilities

import db
from compiler.operations import (
    RUNTIME_KEY_CALL_FIELDS,
    RUNTIME_KEY_SEND_DTMF,
)
from services import close_reasons, session_events
from services.agents import ConversationSpec
from services.conversations import ensure_stream_ref_on_conn, stream_conversation_key
from services.streams import signals
from services.system_vars import build_call_fields
from utils.bg import spawn
from workers.session import persistence
from workers.voice.keypad import KeypadCollector
from workers.voice.recording import recording_options
from workers.voice.room_options import room_options
from workers.voice.runtime import VoiceRun, VoiceSessionSpec

logger = logging.getLogger("talqing.workers.voice")

# RoomIO auto-closes AgentSession only for these; anything else would leave the
# session running until the stale sweeper. Same set and same reason as
# `workers/voice/sip.py::_ROOMIO_AUTO_CLOSE_DISCONNECT`.
_ROOMIO_AUTO_CLOSE_DISCONNECT = frozenset(
    {
        int(rtc.DisconnectReason.CLIENT_INITIATED),
        int(rtc.DisconnectReason.ROOM_DELETED),
        int(rtc.DisconnectReason.USER_REJECTED),
    }
)


async def run_stream_call(ctx: JobContext, meta: dict[str, object]) -> None:
    """One partner-streamed call, from dispatch to finalize."""
    tenant_id = meta.get("tenant")
    agent_id = meta.get("agent")
    connection_id = meta.get("stream_connection_id")
    gateway_identity = str(meta.get("gateway_identity") or "").strip()
    session_id = str(meta.get("session") or "").strip() or str(uuid4())
    room_name = ctx.job.room.name
    platform_call_id = str(meta.get("platform_call_id") or "").strip() or None
    # Normalized and oriented by the gateway: `human_e164` is the person on the
    # call whichever way it was placed, and `from`/`to` are the wire's own.
    from_e164 = str(meta.get("from_e164") or "").strip() or None
    to_e164 = str(meta.get("to_e164") or "").strip() or None
    human_e164 = str(meta.get("human_e164") or "").strip() or None
    agent_e164 = str(meta.get("agent_e164") or "").strip() or None
    direction = str(meta.get("direction") or "").strip() or None
    dialect = str(meta.get("dialect") or "").strip()
    raw_vars = meta.get("vars")
    session_vars: dict[str, str] = (
        {str(k): str(v) for k, v in raw_vars.items()} if isinstance(raw_vars, dict) else {}
    )

    ctx.log_context_fields = {
        "session_type": "STREAM",
        "tenant": tenant_id,
        "agent": agent_id,
        "session": session_id,
        "room": room_name,
        "dialect": dialect,
        "stream_connection": connection_id,
    }

    def _abort(reason: str, message: str, **fields: object) -> None:
        logger.error(
            "aborting stream dispatch: %s",
            message,
            extra={"dispatch_metadata": meta, "reason": reason, **fields},
        )
        ctx.shutdown(reason)

    if not tenant_id or not agent_id or not connection_id or not gateway_identity:
        _abort("missing_dispatch_metadata", "stream metadata is incomplete")
        return

    tenant = await persistence.load_tenant(str(tenant_id))
    if not tenant:
        _abort("unknown_tenant", "unknown tenant", tenant=tenant_id)
        return

    pool = await db.tenant_pool(tenant)
    # The gateway already gated on credit before it created the room, so there is
    # no second check here — refusing now would refuse a call the partner has
    # already connected, exactly as an outbound SIP dial is not re-gated.

    row = await pool.fetchrow(
        """
        SELECT a.published_version, av.config->'conversation' AS conversation
        FROM agents a
        JOIN agent_versions av
            ON av.agent_id = a.id AND av.version = a.published_version
           AND av.tenant_id = a.tenant_id
        WHERE a.id = $1::uuid AND a.tenant_id = $2
        """,
        str(agent_id),
        tenant.id,
    )
    if not row or row["published_version"] is None:
        # The gateway refuses an unpublished agent before it dispatches, and a
        # published version is never cleared, so this is a guard, not a path:
        # an abort here reaches no one — the gateway only notices an agent
        # that joined — and the caller would hear silence.
        _abort(close_reasons.STREAM_UNPUBLISHED_AGENT, "agent has no published version")
        return
    version = int(row["published_version"])
    if row["conversation"] is None:
        # A frozen config always carries this block — `AgentConfig` gives it a
        # default and dumps every field — so a missing one means the version was
        # written by something that is not `AgentConfig`, and answering the call
        # on invented defaults would hide that.
        _abort(
            close_reasons.STREAM_AGENT_UNRESOLVED,
            f"agent {agent_id} version {version} has no conversation settings",
        )
        return
    conversation = ConversationSpec.model_validate(row["conversation"])

    # Who this call is with. The person's number when we have a usable one, and
    # the platform's own call id when we do not — an anonymous Twilio stream then
    # gets a conversation of its own rather than being refused or, worse,
    # collapsed onto every other anonymous caller.
    peer = human_e164 or (f"call:{platform_call_id}" if platform_call_id else f"call:{session_id}")
    try:
        key = stream_conversation_key(connection_id=str(connection_id), peer=peer)
        async with pool.acquire() as conn:
            async with conn.transaction():
                thread = await ensure_stream_ref_on_conn(
                    conn,
                    tenant_id=tenant.id,
                    conversation_key=key,
                    stream_connection_id=UUID(str(connection_id)),
                    context=conversation.context,
                    metadata={
                        "phone_e164": human_e164,
                        "did_e164": agent_e164,
                        "direction": direction,
                        "dialect": dialect,
                        "platform_call_id": platform_call_id,
                    },
                )
    except Exception as exc:
        logger.exception("stream conversation bind failed")
        _abort("conversation_bind_failed", str(exc))
        return

    run: VoiceRun | None = None
    run_holder: list[VoiceRun] = []
    try:
        await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)

        try:
            run = await VoiceRun.prepare(
                VoiceSessionSpec(
                    tenant=tenant,
                    session_id=session_id,
                    agent_id=str(agent_id),
                    agent_version=version,
                    session_type="STREAM",
                    conversation_id=str(thread.conversation_id),
                    conversation_ref_id=str(thread.ref.id),
                    allowed_channels=("voice",),
                    livekit_room=room_name,
                    from_e164=from_e164,
                    to_e164=to_e164,
                    # The platform's own call id, which is what makes a reconnect
                    # or a duplicated dispatch collapse onto one row through
                    # `uq_sessions_idempotency`.
                    idempotency_key=platform_call_id,
                    # The partner's own metadata bag, already filtered to legal
                    # variable names by the gateway. It has to travel on the SPEC
                    # rather than be written to the row first, the way an outbound
                    # dial does it: nothing pre-mints a stream call's row, so
                    # `prepare` is what creates it and an UPDATE ahead of that
                    # would match nothing and drop every `{{vars.*}}` on the call.
                    session_vars=session_vars,
                ),
                ctx.proc.userdata["vad"],
                session_status="running",
                runtime_extra=_runtime_extra(ctx, meta, run_holder),
            )
        except RuntimeError as exc:
            logger.exception("stream prepare failed")
            _abort("prepare_failed", str(exc))
            return
        run_holder.append(run)

        await pool.execute(
            "UPDATE sessions SET stream_connection_id = $3::uuid "
            "WHERE id = $1::uuid AND tenant_id = $2",
            session_id,
            tenant.id,
            str(connection_id),
        )

        async def _finalize(reason: str) -> None:
            assert run is not None
            await run.finalize_with_timeout(reason or "job_shutdown")

        ctx.add_shutdown_callback(_finalize)
        run.wire_room_events(ctx.room)
        run.emit_session_started()
        _record_connected(run, meta)

        @ctx.room.on("disconnected")
        def _on_room_disconnected(*_: object) -> None:
            assert run is not None
            run.mark_ended()

        @run.session.on("close")
        def _on_close(ev: CloseEvent) -> None:
            reason = getattr(getattr(ev, "reason", None), "value", None) or str(
                getattr(ev, "reason", "")
            )
            ctx.shutdown(reason)

        _wire_gateway_signals(ctx, run, gateway_identity=gateway_identity)
        keypad = _build_keypad(run)
        if keypad is not None:
            keypad.attach(ctx.room)

        await run.session.start(
            run.agent,
            room=ctx.room,
            record=recording_options(run.cfg),
            room_options=room_options(
                gateway_identity,
                noise_cancellation=run.build_noise_cancellation(),
            ),
        )
        _wire_barge_in(ctx, run)
        # No `start_background_audio`: see the module docstring. Not an omission,
        # and not something to "fix" by calling it — the bed would be a second
        # published track that only the gateway would notice.
        await run.begin_entry()
    except Exception as exc:
        logger.exception("stream session failed; finalizing session %s", session_id)
        if run is not None:
            await run.finalize_with_timeout("agent_start_failed", error=exc)
        else:
            try:
                await pool.execute(
                    """
                    UPDATE sessions
                    SET status = 'failed',
                        close_reason = 'agent_start_failed',
                        error = $3::jsonb,
                        ended_at = COALESCE(ended_at, now()),
                        updated_at = now()
                    WHERE id = $1::uuid AND tenant_id = $2
                      AND status IN ('queued', 'running')
                    """,
                    session_id,
                    tenant.id,
                    json.dumps({"message": str(exc)[:2048], "type": type(exc).__name__}),
                )
            except Exception:
                logger.exception("failed to mark stream session failed after early error")
        ctx.shutdown("agent_start_failed")


def _runtime_extra(
    ctx: JobContext, meta: dict[str, object], run_holder: list[VoiceRun]
) -> dict[str, object]:
    """The keys a stream call's tools are compiled against.

    Note what is NOT here: `RUNTIME_KEY_TRANSFER`. Its absence is what makes the
    `transfer` operation refuse itself, with the message it already has.
    """
    extra: dict[str, object] = {
        # `direction` is None where the platform did not state one, and
        # `build_call_fields` omits it rather than guessing — an absent key and a
        # wrong one read identically in a prompt, and only one of them is honest.
        RUNTIME_KEY_CALL_FIELDS: build_call_fields(
            direction=_direction(meta.get("direction")),
            human_e164=str(meta.get("human_e164") or "") or None,
            agent_e164=str(meta.get("agent_e164") or "") or None,
        ),
    }
    if meta.get("can_send_dtmf"):
        # Per CALL, not per channel: on a stream, whether the agent can reach for
        # a keypad is a property of which platform is on the other end of the
        # socket. The gateway translates a room DTMF packet into that platform's
        # own command, so this is the same `publish_dtmf` the SIP path uses.
        async def send_dtmf(digit: str) -> None:
            from livekit.agents.beta.workflows.utils import DtmfEvent, dtmf_event_to_code

            await ctx.room.local_participant.publish_dtmf(
                code=dtmf_event_to_code(DtmfEvent(digit)), digit=digit
            )
            # A list holding the run rather than the run itself: the runtime
            # context has to exist before `prepare` compiles the tools that
            # capture it, and it is filled the moment prepare returns.
            if run_holder:
                run_holder[0].events.record(session_events.DTMF_SENT, {"digit": digit})

        extra[RUNTIME_KEY_SEND_DTMF] = send_dtmf
    return extra


def _direction(value: object) -> str | None:
    text = str(value or "").strip().lower()
    return text if text in ("inbound", "outbound") else None


def _build_keypad(run: VoiceRun) -> KeypadCollector | None:
    """The caller's keypad, when this agent asked for one."""
    if not run.cfg.keypad_input.enabled:
        return None

    def _trace(digit: str) -> None:
        run.events.record(session_events.DTMF_RECEIVED, {"digit": digit})

    return KeypadCollector(run.session, run.cfg.keypad_input, on_digit=_trace)


def _record_connected(run: VoiceRun, meta: dict[str, object]) -> None:
    """The shape of the socket this call arrived on, on the call's own trace.

    Written here rather than by the gateway because `session_events.seq` comes
    from an in-process counter and a session has exactly one writer — see
    `services/streams/signals.py`. Everything in the payload is a fact only the
    gateway saw, carried in the dispatch metadata.

    `unusable_param_keys` is the one nobody would think to ask for and the only
    answer to "why is `{{vars.flowName}}` empty?": a metadata key that cannot be
    a variable name is skipped rather than failing the call, and this is where it
    says so. `unusable_numbers` answers the same question about caller history:
    a `from` that was an unfilled placeholder is on the trace.
    """
    session_vars = meta.get("vars")
    run.events.record(
        session_events.STREAM_CONNECTED,
        {
            "dialect": meta.get("dialect"),
            "connection_id": meta.get("stream_connection_id"),
            "connection_name": meta.get("stream_connection_name"),
            "platform_call_id": meta.get("platform_call_id"),
            "codec": meta.get("codec"),
            "sample_rate": meta.get("sample_rate"),
            "direction": meta.get("direction"),
            "can_send_dtmf": meta.get("can_send_dtmf"),
            "vars": sorted(session_vars) if isinstance(session_vars, dict) else [],
            "unusable_param_keys": meta.get("unusable_param_keys") or [],
            "unusable_numbers": meta.get("unusable_numbers") or {},
        },
    )


def _wire_gateway_signals(ctx: JobContext, run: VoiceRun, *, gateway_identity: str) -> None:
    """Everything the gateway can see and this process cannot.

    Two channels, and they answer different questions. The data packets carry
    what the PARTNER said — their end reason, their errors — plus the two faults
    only the gateway can observe. The participant-disconnected wire is the
    backstop for a gateway that never got to say anything at all.
    """

    @ctx.room.on("data_received")
    def _on_data(packet: rtc.DataPacket) -> None:
        if packet.topic != signals.STREAM_TOPIC:
            return
        if packet.participant is not None and packet.participant.identity != gateway_identity:
            # Only the gateway speaks this vocabulary on this call. Anything else
            # publishing on the topic is not something to act on.
            return
        kind, payload = signals.decode(packet.data)
        if kind == signals.SIGNAL_ENDED:
            reason = str(payload.get("reason") or close_reasons.STREAM_PLATFORM_HANGUP)
            run.events.record(
                session_events.STREAM_ENDED,
                {"reason": payload.get("detail"), "duration_ms": payload.get("duration_ms")},
            )
            # Named BEFORE the teardown starts: finalize keeps the first reason
            # it is given, and closing the session makes the framework offer its
            # own generic one.
            run.set_close_reason(reason)
            run.mark_ended()
            try:
                run.session.shutdown()
            except Exception:
                logger.exception("failed to shutdown AgentSession after the stream ended")
            ctx.shutdown(reason)
        elif kind == signals.SIGNAL_ERROR:
            run.events.record(
                session_events.STREAM_ERROR,
                {"code": payload.get("code"), "message": payload.get("message")},
            )
        elif kind == signals.SIGNAL_CLEARED:
            run.events.record(session_events.STREAM_CLEARED, {})
        elif kind == signals.SIGNAL_UNDERRUN:
            run.events.record(session_events.STREAM_UNDERRUN, dict(payload))
        elif kind == signals.SIGNAL_INBOUND_DROPPED:
            run.events.record(session_events.STREAM_INBOUND_DROPPED, dict(payload))

    @ctx.room.on("participant_disconnected")
    def _on_gateway_left(participant: rtc.RemoteParticipant) -> None:
        if participant.identity != gateway_identity:
            return
        if run.framework_close_reason is not None:
            # Already ending. This is teardown following the close, not a fault.
            return
        reason = participant.disconnect_reason
        try:
            reason_i = int(reason) if reason is not None else None
        except (TypeError, ValueError):
            reason_i = None
        if reason_i is not None and reason_i in _ROOMIO_AUTO_CLOSE_DISCONNECT:
            # A clean leave. The gateway closes its room connection last, after
            # sending the end signal above, so this is the normal ending and
            # RoomIO closes the session for us.
            return
        logger.warning(
            "stream gateway disconnected without a reason; shutting down",
            extra={"identity": participant.identity, "disconnect_reason": reason},
        )
        run.set_close_reason(close_reasons.STREAM_GATEWAY_DISCONNECTED)
        run.mark_ended()
        try:
            run.session.shutdown()
        except Exception:
            logger.exception("failed to shutdown AgentSession after the gateway disconnected")
        ctx.shutdown(close_reasons.STREAM_GATEWAY_DISCONNECTED)


def _wire_barge_in(ctx: JobContext, run: VoiceRun) -> None:
    """Tell the gateway when the agent's speech was cut off.

    The chain is real-time paced end to end — `AudioSource.capture_frame` returns
    only when there is queue room — so the far end's buffer holds milliseconds
    and an interruption is already heard as one. This exists for the case pacing
    cannot cover: a network hiccup that lets a burst reach a buffer a `clear`
    genuinely purges, which Twilio's is and SparkTG's is not. Their own words:
    *"stop generating and stop sending audio on your side — that is what actually
    shortens what the caller hears."*

    A wrapper, not a fork: `AgentOutput.audio` has a setter and `AudioOutput`
    supports `next_in_chain`, so this adds one signal and changes nothing else
    about playback. Installed after `session.start`, because that is when the
    room output exists to wrap.
    """
    current = run.session.output.audio
    if current is None:
        # No audio output to wrap. Nothing is broken — there is simply nothing to
        # interrupt — but a stream call with no output would be silent, so say so.
        logger.error("stream session %s has no audio output to wrap", run.spec.session_id)
        return
    run.session.output.audio = _BargeInSignal(ctx, next_in_chain=current)


class _BargeInSignal(AudioOutput):
    """Pass-through audio output that publishes a `clear` when playback is cut.

    Everything except `clear_buffer` forwards, exactly as LiveKit's own
    `_AudioSinkProxy` does — this is a tee on one method, not a sink.
    """

    def __init__(self, ctx: JobContext, *, next_in_chain: AudioOutput) -> None:
        super().__init__(
            label="TalqingStreamBargeIn",
            capabilities=AudioOutputCapabilities(pause=next_in_chain.can_pause),
            next_in_chain=next_in_chain,
            sample_rate=next_in_chain.sample_rate,
        )
        self._ctx = ctx

    @property
    def _sink(self) -> AudioOutput:
        assert self.next_in_chain is not None
        return self.next_in_chain

    async def capture_frame(self, frame: rtc.AudioFrame) -> None:
        await super().capture_frame(frame)
        await self._sink.capture_frame(frame)

    def flush(self) -> None:
        super().flush()
        self._sink.flush()

    def clear_buffer(self) -> None:
        spawn(self._publish_clear())
        self._sink.clear_buffer()

    async def _publish_clear(self) -> None:
        try:
            await self._ctx.room.local_participant.publish_data(
                signals.encode(signals.SIGNAL_CLEAR),
                reliable=True,
                topic=signals.STREAM_TOPIC,
            )
        except Exception:
            # A barge-in signal that does not arrive costs milliseconds of extra
            # audio at the caller's end, not the call. Never worth raising into
            # a synchronous LiveKit callback.
            logger.debug("could not signal barge-in to the stream gateway", exc_info=True)
