"""A WhatsApp call: Twilio's leg dialled into a room our voice webhook made.

`run_sip_call`'s answered inbound call with the deciding already done. The
webhook (`services/integrations/providers/whatsapp/calls.py`) refused whatever
it had to, filed the call on the chat's thread, wrote the `queued` row and
dispatched this job, so everything this needs is in the dispatch metadata. What
reaches the room is an ordinary SIP leg, and from there the call runs on the SIP
path's own wiring (`wire_call_lifecycle`, `start_call`).

Two things differ from a phone call, and both are decisions:

* **The agent compiles while the leg is on its way.** Nothing here waits on the
  caller's SIP attributes, so `prepare` overlaps the INVITE. The row stays
  `queued` until the leg joins, as an outbound call's does while it rings, so
  ring time is not billed.
* **No transfer.** Meta allows no PSTN on any leg of a WhatsApp call and Twilio
  rejects one, so the `transfer` operation is handed a transfer that always
  fails, with the sentence the caller should hear. Failing rather than being
  absent is what lets the operation's `on_failure` run as the builder set it.
* **The greeting waits for the caller's audio.** The leg is answered before
  WhatsApp's audio path is up, so a greeting spoken on answer is partly lost.
"""

from __future__ import annotations

import asyncio
import logging
import time

from livekit import rtc
from livekit.agents import AutoSubscribe, JobContext

from compiler.operations import (
    RUNTIME_KEY_CALL_FIELDS,
    RUNTIME_KEY_SEND_DTMF,
    RUNTIME_KEY_TRANSFER,
)
from services.system_vars import build_call_fields
from settings import get_settings
from workers.session import persistence
from workers.voice.runtime import VoiceRun, VoiceSessionSpec
from workers.voice.sip import (
    fail_unprepared_call,
    make_dtmf_sender,
    start_call,
    wire_call_lifecycle,
    wire_sip_disconnect_safety,
)
from workers.voice.transfer import TransferOutcome

logger = logging.getLogger("talqing.workers.voice")

SESSION_TYPE = "WHATSAPP_INBOUND"
# MEASURED on five prod-US calls (Twilio): until WhatsApp's audio path is up, the
# caller's track carries digital silence — decoded peaks under 2 LSB — and it
# then steps to a real microphone floor of 7-10 LSB. Nothing on the leg says when
# that happens: it arrived 1.2-3.4 s after the recording started, and on the
# slow one the greeting had already played 2 s into the void. Their audio
# arriving is the only evidence ours is too.
_CALLER_AUDIO_PEAK = 4
# The cap on holding the greeting back. Past it the caller is waiting on us, not
# the network — a greeting half heard beats a silence that never ends.
_CALLER_AUDIO_TIMEOUT_S = 5.0
# The caller hung up before Twilio's INVITE reached us, or it never came.
_NEVER_CONNECTED = "sip_connection_timeout_failed"


async def _refuse_transfer(**_: object) -> TransferOutcome:
    return TransferOutcome(
        ok=False,
        transport="none",
        detail="calls on WhatsApp can't be transferred to a phone number",
    )


async def _wait_for_caller_audio(room: rtc.Room, caller: rtc.RemoteParticipant) -> None:
    """Return once the caller's audio carries sound, or after the cap."""
    started = time.monotonic()
    subscribed: asyncio.Future[rtc.Track] = asyncio.get_running_loop().create_future()

    def on_track_subscribed(
        track: rtc.Track, _pub: rtc.RemoteTrackPublication, participant: rtc.RemoteParticipant
    ) -> None:
        if participant.identity == caller.identity and track.kind == rtc.TrackKind.KIND_AUDIO:
            if not subscribed.done():
                subscribed.set_result(track)

    room.on("track_subscribed", on_track_subscribed)
    stream: rtc.AudioStream | None = None
    try:
        async with asyncio.timeout(_CALLER_AUDIO_TIMEOUT_S):
            for pub in caller.track_publications.values():
                if pub.kind == rtc.TrackKind.KIND_AUDIO and pub.track is not None:
                    on_track_subscribed(pub.track, pub, caller)
            stream = rtc.AudioStream.from_track(track=await subscribed)
            async for event in stream:
                if max(map(abs, event.frame.data), default=0) >= _CALLER_AUDIO_PEAK:
                    break
        logger.info("caller audio after %d ms", (time.monotonic() - started) * 1000)
    except TimeoutError:
        logger.warning("no caller audio after %.0f s; greeting anyway", _CALLER_AUDIO_TIMEOUT_S)
    finally:
        room.off("track_subscribed", on_track_subscribed)
        if stream is not None:
            await stream.aclose()


async def run_whatsapp_call(ctx: JobContext, meta: dict[str, object]) -> None:
    """One WhatsApp call, from dispatch to finalize."""
    _t0 = time.monotonic()

    def _lap(stage: str) -> None:
        logger.info("latency-debug job %s %d ms", stage, (time.monotonic() - _t0) * 1000)

    tenant = await persistence.load_tenant(str(meta["tenant"]))
    if not tenant:
        logger.error("aborting WhatsApp call: unknown tenant", extra={"dispatch_metadata": meta})
        ctx.shutdown("unknown_tenant")
        return
    session_id = str(meta["session"])
    sender_e164 = str(meta["sender_e164"])
    # Null for a user who hides their number; the thread is keyed on their id.
    from_e164 = str(meta["from_e164"]) if meta.get("from_e164") else None
    ctx.log_context_fields = {
        "session_type": SESSION_TYPE,
        "tenant": str(tenant.id),
        "agent": str(meta["agent"]),
        "version": meta["version"],
        "session": session_id,
        "room": ctx.job.room.name,
        "conversation_id": str(meta["conversation_id"]),
    }

    run: VoiceRun | None = None
    run_holder: list[VoiceRun] = []
    try:
        _lap("load_tenant")
        await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)
        _lap("connect")
        leg = asyncio.create_task(
            asyncio.wait_for(
                ctx.wait_for_participant(kind=rtc.ParticipantKind.PARTICIPANT_KIND_SIP),
                timeout=get_settings().livekit.sip.ringing_timeout_seconds,
            )
        )
        try:
            run = await VoiceRun.prepare(
                VoiceSessionSpec(
                    tenant=tenant,
                    session_id=session_id,
                    agent_id=str(meta["agent"]),
                    agent_version=int(meta["version"]),  # type: ignore[arg-type]
                    session_type=SESSION_TYPE,
                    conversation_id=str(meta["conversation_id"]),
                    conversation_ref_id=str(meta["conversation_ref_id"]),
                    allowed_channels=("voice",),
                    integration_id=str(meta["integration_id"]),
                    trigger_id=str(meta["trigger_id"]),
                    livekit_room=ctx.job.room.name,
                    from_e164=from_e164,
                    to_e164=sender_e164,
                    idempotency_key=str(meta["call_sid"]),
                    conversation_context="transcript",
                ),
                ctx.proc.userdata["vad"],
                session_status="queued",
                runtime_extra={
                    RUNTIME_KEY_TRANSFER: _refuse_transfer,
                    RUNTIME_KEY_CALL_FIELDS: build_call_fields(
                        direction="inbound", human_e164=from_e164, agent_e164=sender_e164
                    ),
                    # livekit-sip carries RFC 4733 both ways on this leg too.
                    RUNTIME_KEY_SEND_DTMF: make_dtmf_sender(ctx, run_holder),
                },
            )
        except BaseException:
            leg.cancel()
            raise
        _lap("prepare")
        run_holder.append(run)
        wire_call_lifecycle(ctx, run)

        try:
            sip_participant = await leg
        except (TimeoutError, RuntimeError):
            # Either our wait ran out, or the room closed first: it was made with
            # the same 60 s `empty_timeout`, and the agent does not keep it open.
            # `wait_for_participant` raises RuntimeError for that and nothing else.
            logger.warning("WhatsApp call leg never reached the room")
            await run.finalize_with_timeout(_NEVER_CONNECTED)
            ctx.shutdown(_NEVER_CONNECTED)
            return
        _lap("leg_joined")
        await run.promote_running()
        wire_sip_disconnect_safety(ctx, run, sip_identity=sip_participant.identity)
        _spoke: list[bool] = []

        def _on_state(ev) -> None:
            if ev.new_state == "speaking" and not _spoke:
                _spoke.append(True)
                _lap("agent_speaking")

        run.session.on("agent_state_changed", _on_state)

        async def _before_entry() -> None:
            _lap("session_started")
            await _wait_for_caller_audio(ctx.room, sip_participant)
            _lap("caller_audio")

        await start_call(ctx, run, sip_participant, before_entry=_before_entry)
        _lap("entry_begun")
    except Exception as exc:
        logger.exception("WhatsApp call failed; finalizing session %s", session_id)
        if run is not None:
            await run.finalize_with_timeout("agent_start_failed", error=exc)
        else:
            await fail_unprepared_call(tenant, session_id, exc)
        ctx.shutdown("agent_start_failed")
