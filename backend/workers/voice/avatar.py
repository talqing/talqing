"""The Anam avatar layer — what `channel: video` means.

Anam is the visual layer and nothing else: the agent stays the brain (the plugin
sends `llmId: CUSTOMER_CLIENT_V1`), and the Anam engine joins the LiveKit room as
a second participant that consumes the agent's audio over a DataStream and
publishes lip-synced audio+video on behalf of the agent. Whatever produced that
audio — a TTS in the cascade, or a realtime speech-to-speech model — the avatar
renders from it the same way. Start order matters: the avatar must start BEFORE
session.start() so it owns `session.output.audio`.

Every failure path (Anam 429/capacity, API errors, join timeout) returns None so
the worker can fail the video session. Video agents do not downgrade to voice.
The same holds after a successful join: if the avatar participant leaves, the
face is gone for good, so the worker ends the call instead of running on.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from livekit import rtc
from livekit.agents import (
    AgentSession,
    APIConnectionError,
    APIStatusError,
    AutoSubscribe,
    JobContext,
)

from services.agents import AgentConfig
from services.catalog import get_catalog
from settings import get_settings

logger = logging.getLogger("talqing.workers.voice.avatar")

if TYPE_CHECKING:
    from livekit.plugins.anam.avatar import AvatarSession

# the face must be live before the agent speaks; past this the video session
# fails instead of downgrading to voice-only.
AVATAR_JOIN_TIMEOUT = 30.0


async def start_avatar(
    ctx: JobContext,
    session: AgentSession[dict[str, object]],
    cfg: AgentConfig,
    provider_keys: dict[str, str],
    session_id: str,
    server_url: str,
    *,
    on_lost: Callable[[str], None],
) -> dict[str, object] | None:
    """Start the Anam avatar ahead of session.start(). Returns the metering
    state (provider/model/avatar_session_id/started_at) when the face is live,
    or None when the video session should fail.

    `on_lost` fires if the avatar leaves the room after joining — the worker
    ends the call there rather than letting it run on with no face."""
    s = get_settings()
    anam_api_key = provider_keys.get("anam", "")
    if not anam_api_key:
        logger.warning("video channel but the tenant has no Anam API key (BYOK)")
        return None
    if not cfg.avatar or not cfg.avatar.avatar_id:
        logger.warning("video channel but no avatar_id configured")
        return None

    # The room must be CONNECTED before avatar.start(): the base plugin only
    # arms its join-watcher once the room is up, and wait_for_join() is a no-op
    # when the watcher never started — without this, the join gate silently
    # passes after ~3s with no avatar in the room (found live 2026-06-12).
    # ctx.connect() is idempotent; session.start() later sees it connected.
    # Avatar rendering only needs caller audio on the worker side.
    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)

    from livekit.plugins import anam

    avatar = anam.AvatarSession(
        persona_config=anam.PersonaConfig(
            name=cfg.avatar.name or "avatar",
            avatarId=cfg.avatar.avatar_id,
            avatarModel=None,  # Always use Anam's recommended/server-side default.
        ),
        api_key=anam_api_key,
    )
    try:
        # The Anam engine joins from OUTSIDE the cluster, so it needs the
        # externally-reachable URL the API picked for this call (the in-cluster
        # ws://livekit:7880 is invisible to it). Local dev without a public
        # LiveKit causes a join timeout and the video session fails.
        await avatar.start(
            session,
            room=ctx.room,
            livekit_url=server_url,
            livekit_api_key=s.livekit.api_key,
            livekit_api_secret=s.livekit.api_secret,
        )
        # Anam's billing clock starts when the engine session is created (in
        # start()), so the metered window opens here — idle time included
        started_at = datetime.now(UTC)
        await avatar.wait_for_join(timeout=AVATAR_JOIN_TIMEOUT)
    except APIStatusError as e:
        if e.status_code == 429:
            logger.warning("Anam capacity reached (429)")
        else:
            logger.exception("Anam API error (%s)", e.status_code)
        await _abort(session, avatar)
        return None
    except (TimeoutError, APIConnectionError):
        logger.warning("avatar failed to join within %ss", AVATAR_JOIN_TIMEOUT)
        await _abort(session, avatar)
        return None
    except Exception:
        logger.exception("avatar start failed")
        await _abort(session, avatar)
        return None

    # Bill the catalog avatar SKU (anam/anam). Runtime does not send a Cara
    # override; Anam uses the avatar's server-side default (activeVersion).
    cat = get_catalog()
    provider = cfg.avatar.provider or "anam"
    model = (cfg.avatar.model or "").strip() or "anam"
    canon = cat.canonicalize("avatar", provider, model)
    if canon:
        provider, model = canon

    logger.info(
        "avatar live (anam session %s, avatar %s)",
        avatar.session_id,
        cfg.avatar.avatar_id,
    )
    state = {
        "provider": provider,
        "model": model,
        "avatar_id": cfg.avatar.avatar_id,
        "avatar_session_id": avatar.session_id,
        "started_at": started_at,
        "_avatar": avatar,  # held only for deterministic teardown; not persisted
    }

    @ctx.room.on("participant_disconnected")
    def _on_participant_disconnected(participant: rtc.RemoteParticipant) -> None:
        # The Anam engine joined as its own participant, so it leaving IS the
        # death signal. Our own teardown evicts it, and that stamps stopped_at
        # first — so a stamped window means this is the eviction, not a fault.
        if participant.identity != avatar.avatar_identity or state.get("stopped_at"):
            return
        state.setdefault("lost_at", datetime.now(UTC))
        logger.error("anam avatar participant %s left the room", participant.identity)
        on_lost(f"avatar participant {participant.identity} left the room")

    # Backstop: guarantee the Anam session is closed (and the billed window
    # stamped) even on an exit path that never reaches the entrypoint's _finalize
    # (e.g. a crash between here and its registration). Idempotent with the
    # explicit stop_avatar() call _finalize makes first.
    async def _close_backstop() -> None:
        await stop_avatar(state)

    ctx.add_shutdown_callback(_close_backstop)
    return state


async def stop_avatar(state: dict[str, object]) -> None:
    """Close the Anam session deterministically and stamp the billable window's
    end (`stopped_at`). Idempotent — safe to call from both the explicit finalize
    path and the shutdown backstop; the first caller wins (no await between the
    guard check and the stamp, so concurrent callers can't both proceed)."""
    if state.get("stopped_at"):
        return
    # An avatar that left mid-call stopped being billable then, not now: Anam's
    # own clock ended at the loss, so ours has to as well.
    state["stopped_at"] = state.get("lost_at") or datetime.now(UTC)
    avatar = state.pop("_avatar", None)
    if avatar is None:
        return
    try:
        # evicts the avatar participant → ends the Anam engine session. The handle
        # round-trips through dict[str, object] metering state, so it needs the narrow.
        await avatar.aclose()  # type: ignore[attr-defined]
    except Exception:
        logger.exception("avatar aclose failed at teardown")


async def _abort(session: AgentSession[dict[str, object]], avatar: AvatarSession) -> None:
    """Undo a half-started avatar: clear the session's audio output and tear the
    Anam side down so no orphaned engine session keeps billing."""
    try:
        discarded = session.output.audio
        session.output.audio = None
        # the discarded DataStreamAudioOutput has no public aclose, and its
        # internal start-task keeps waiting for the avatar participant —
        # cancel it or it logs a spurious ERROR when the room closes
        task = getattr(discarded, "_start_atask", None)
        if task is not None:
            task.cancel()
    except Exception:
        logger.exception("failed to reset session audio output")
    try:
        await avatar.aclose()
    except Exception:
        logger.exception("avatar aclose failed during abort")
