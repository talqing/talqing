"""LiveKit room path for web voice / video (token mint → RoomAgentDispatch)."""

from __future__ import annotations

import logging

from livekit.agents import CloseEvent, JobContext

from workers.session import persistence
from workers.voice.avatar import start_avatar
from workers.voice.client_rpc import register_client_handlers
from workers.voice.images import register_image_handler
from workers.voice.recording import recording_options
from workers.voice.room_options import auto_subscribe_for_channel, room_options
from workers.voice.runtime import VoiceRun, VoiceSessionSpec
from workers.voice.screenshare import ScreenshareWatcher

logger = logging.getLogger("talqing.workers.voice")

# Anam went away mid-call. Deliberately NOT in billing's FAILED_CLOSE_REASONS:
# the call ran and is billed like any other, platform fee included. It still
# reads as this reason on the session, so a lost avatar stays diagnosable.
AVATAR_DISCONNECTED = "avatar_disconnected"


async def run_web_room(ctx: JobContext, meta: dict[str, object]) -> None:
    """Web / video room session: prepare → connect → avatar? → start → entry."""
    tenant_id, agent_id, version = (
        meta.get("tenant"),
        meta.get("agent"),
        meta.get("version"),
    )
    participant_identity = meta.get("participant_identity")
    if not isinstance(participant_identity, str) or not participant_identity.strip():
        participant_identity = None
    initial_userdata = meta.get("userdata") if isinstance(meta.get("userdata"), dict) else {}
    conversation_id_meta = meta.get("conversation_id")
    conversation_ref_meta = meta.get("conversation_ref_id")
    # The session id minted by the API is also the provider prompt-cache key.
    session_id = str(meta["session"])
    room_name = ctx.job.room.name
    ctx.log_context_fields = {
        "tenant": tenant_id,
        "agent": agent_id,
        "version": version,
        "session": session_id,
        "room": room_name,
        "conversation_id": conversation_id_meta,
    }

    def _abort_dispatch(reason: str, message: str, **fields: object) -> None:
        logger.error(
            "aborting dispatch: %s",
            message,
            extra={"dispatch_metadata": meta, "reason": reason, **fields},
        )
        ctx.shutdown(reason)

    # A null `version` means the plan on the session row decides — an inline
    # agent (which also has a null `agent`) or an unpublished draft, neither of
    # which has a frozen version to name. `prepare` reads that plan for every
    # run and says so out loud when there is neither.
    try:
        version = int(version) if version is not None else None
    except (TypeError, ValueError):
        _abort_dispatch(
            "invalid_dispatch_metadata",
            "malformed dispatch metadata",
            tenant=tenant_id,
            agent=agent_id,
            version=version,
        )
        return
    if not tenant_id:
        _abort_dispatch("missing_dispatch_metadata", "dispatch metadata missing tenant")
        return

    tenant = await persistence.load_tenant(str(tenant_id))
    if not tenant:
        _abort_dispatch("unknown_tenant", "unknown tenant", tenant=tenant_id)
        return

    conversation_id = conversation_id_meta if isinstance(conversation_id_meta, str) else None
    conversation_ref_id = conversation_ref_meta if isinstance(conversation_ref_meta, str) else None

    # Built before `prepare`, because the compiled agent captures its `peek` off
    # the runtime context as it is built — and the presence of that key is what
    # tells the agent it can see at all. Attaching to the room comes later, after
    # connect; nothing is subscribed until then.
    #
    # No identity, no watcher: the subscribe predicate is "this participant's
    # screen share", so without one it would match nothing and the agent would
    # spend the call claiming a view it never gets. `calls_token` always puts one
    # in the room metadata, so this is a broken dispatch rather than a state —
    # `prepare` says so out loud if the agent asked to watch a screen.
    watcher = ScreenshareWatcher(participant_identity) if participant_identity else None

    try:
        # Create the session row FIRST (inside prepare) so started_at (the
        # platform-fee clock) opens before avatar join — an Anam join can take
        # up to AVATAR_JOIN_TIMEOUT (30s), and that time is part of the call.
        run = await VoiceRun.prepare(
            VoiceSessionSpec(
                tenant=tenant,
                session_id=session_id,
                agent_id=str(agent_id) if agent_id else None,
                agent_version=version,
                session_type="WEB",
                conversation_id=conversation_id,
                conversation_ref_id=conversation_ref_id,
                participant_identity=participant_identity,
                initial_userdata=initial_userdata,
                allowed_channels=("voice", "video"),
            ),
            ctx.proc.userdata["vad"],
            session_status="running",
            screenshare_watcher=watcher,
        )
    except RuntimeError as exc:
        msg = str(exc)
        if "not allowed for session type" in msg and "text" in msg:
            _abort_dispatch(
                "unsupported_text_room_dispatch",
                "text agents cannot be dispatched to LiveKit rooms",
                tenant=tenant_id,
                agent=agent_id,
                version=version,
            )
            return
        if "no published definition" in msg:
            _abort_dispatch(
                "missing_published_definition",
                "no published definition for agent/version",
                tenant=tenant_id,
                agent=agent_id,
                version=version,
            )
            return
        logger.exception("voice prepare failed")
        _abort_dispatch("prepare_failed", msg, tenant=tenant_id, agent=agent_id)
        return

    async def _finalize_on_shutdown(reason: str) -> None:
        await run.finalize_with_timeout(reason or "job_shutdown")

    ctx.add_shutdown_callback(_finalize_on_shutdown)

    try:
        run.emit_session_started()

        await ctx.connect(auto_subscribe=auto_subscribe_for_channel(run.cfg.channel))

        def _on_avatar_lost(detail: str) -> None:
            """Anam died mid-call. A video agent has no voice-only mode to fall
            back to, so the call is over — end it here instead of leaving the
            caller with a frozen face and a mute agent until they give up."""
            if run.framework_close_reason is not None:
                return  # already ending; this is teardown, not a fault
            logger.error("ending video session %s: anam lost (%s)", session_id, detail)
            run.set_close_reason(AVATAR_DISCONNECTED)
            run.mark_ended()
            ctx.shutdown(AVATAR_DISCONNECTED)

        if run.cfg.channel == "video":
            run.avatar_state = await start_avatar(
                ctx,
                run.session,
                run.cfg,
                run.provider_keys,
                session_id,
                str(meta["server_url"]),
                on_lost=_on_avatar_lost,
            )
            if run.avatar_state is None:
                logger.error("video session cannot start because Anam did not join")
                await run.finalize_with_timeout("avatar_start_failed")
                ctx.shutdown("avatar_start_failed")
                return

        run.wire_room_events(ctx.room)

        @ctx.room.on("disconnected")
        def _on_room_disconnected(*_: object) -> None:
            run.mark_ended()

        @run.session.on("close")
        def _on_close_shutdown_job(ev: CloseEvent) -> None:
            reason = getattr(getattr(ev, "reason", None), "value", None) or str(
                getattr(ev, "reason", "")
            )
            ctx.shutdown(reason)

        # Always built, even with no participant to link to: room options now
        # also carry the noise-cancellation processor, which must not depend on
        # whether an identity happens to be known.
        await run.session.start(
            run.agent,
            room=ctx.room,
            record=recording_options(run.cfg),
            room_options=room_options(
                participant_identity,
                noise_cancellation=run.build_noise_cancellation(),
            ),
        )

        try:
            register_client_handlers(ctx, run.session, participant_identity)
        except Exception:
            logger.exception("client RPC handler registration failed")
        try:
            register_image_handler(ctx.room, run)
        except Exception:
            logger.exception("image byte-stream handler registration failed")
        if run.screenshare_watcher is not None:
            # After connect, and beside LiveKit's own subscribe sweep: this both
            # opts the screen-share publication in and starts reading it. Nothing
            # else about the room changes — `auto_subscribe_for_channel` still
            # says AUDIO_ONLY.
            try:
                run.screenshare_watcher.attach(ctx.room, events=run.events)
                if run.cfg.vision_input.screenshare.record:
                    await run.start_screenshare_recording(ctx, run.screenshare_watcher)
            except Exception:
                logger.exception("screen share watcher failed to attach")
        elif run.cfg.vision_input.screenshare.enabled:
            # The agent asked to watch a screen and there is nobody to link to.
            # `calls_token` always puts a participant identity in the room
            # metadata, so this is a broken dispatch. The call is safe — `prepare`
            # withheld the runtime key, so the agent was never told it can see —
            # but a feature the tenant turned on did not run, and nothing else
            # would ever say so.
            logger.error(
                "session %s has no participant identity, so the agent cannot watch a screen",
                session_id,
            )
        await run.start_background_audio(ctx)

        await run.begin_entry()
    except Exception as exc:
        logger.exception(
            "voice session failed during connect/start/entry; finalizing session %s",
            session_id,
        )
        await run.finalize_with_timeout("agent_start_failed", error=exc)
        ctx.shutdown("agent_start_failed")
        return
