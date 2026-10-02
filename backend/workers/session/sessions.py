"""Create and finalize agent sessions."""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from livekit.agents import AgentSession

import db
from services.agents import AgentConfig
from services.billing import collector_in, session_status_for_close_reason
from services.catalog import get_catalog
from services.recordings import RecordingStatus
from services.tools import (
    AvatarState,
    TranscriptRow,
)
from services.transcripts import load_session_transcript
from services.user import Tenant
from services.userdata import is_reserved_key
from utils.latency import compute_latency

logger = logging.getLogger("talqing.workers.session")


async def record_refused_call(
    tenant: Tenant,
    *,
    session_id: str,
    type_: str,
    agent_id: str | None,
    close_reason: str,
    livekit_room: str | None,
    from_e164: str | None = None,
    to_e164: str | None = None,
    idempotency_key: str | None = None,
    phone_number_id: str | None = None,
    telephony_account_id: str | None = None,
    integration_id: str | None = None,
    trigger_id: str | None = None,
) -> None:
    """Leave a visible, explained row for an inbound call we turned away.

    Refusing at the door means `VoiceRun.prepare` never runs, so without this the
    call would exist only in logs — the tenant would see a caller who reached
    them and vanished, with nothing in the product to explain it. One INSERT, no
    conversation, no agent compile.

    `billing_status = 'computed'` with no money: nothing was consumed, so the
    row is finished with, and leaving it `pending` would report a refused call as
    one still waiting to be priced (`observability.pending_sessions`).

    Shared by every door that turns calls away — the SIP worker (an unreadable
    caller, no credits) and the WhatsApp voice webhook — so they cannot drift.
    A replay of a call already recorded (same id or idempotency key) writes
    nothing.
    """
    pool = await db.tenant_pool(tenant)
    try:
        await pool.execute(
            """
            INSERT INTO sessions (
                id, tenant_id, agent_id, agent_name, channel, type, status, close_reason,
                phone_number_id, telephony_account_id, integration_id, trigger_id,
                livekit_room, from_e164, to_e164, idempotency_key,
                started_at, ended_at, billing_status
            )
            VALUES (
                $1::uuid, $2, $3::uuid,
                (SELECT name FROM agents WHERE id = $3::uuid AND tenant_id = $2),
                'voice', $4, 'failed', $5,
                $6::uuid, $7::uuid, $8::uuid, $9::uuid,
                $10, $11, $12, $13,
                now(), now(), 'computed'
            )
            ON CONFLICT DO NOTHING
            """,
            session_id,
            tenant.id,
            agent_id,
            type_,
            close_reason,
            phone_number_id,
            telephony_account_id,
            integration_id,
            trigger_id,
            livekit_room,
            from_e164,
            to_e164,
            idempotency_key,
        )
    except Exception:
        logger.exception("failed to record refused call %s", session_id)


async def set_recording(
    tenant: Tenant,
    session_id: str,
    *,
    status: RecordingStatus,
    object_key: str | None = None,
    started_at: datetime | None = None,
    duration_s: int | None = None,
    size_bytes: int | None = None,
) -> None:
    """Write the recording outcome for one session.

    Every field moves together: a row that says ``stored`` without a key, or
    keeps a key after ``deleted``, describes an object nobody can find. Callers
    pass the whole picture and this overwrites it, rather than patching fields
    one at a time into an inconsistent middle state.
    """
    pool = await db.tenant_pool(tenant)
    await pool.execute(
        """
        UPDATE sessions
        SET recording_status = $3,
            recording_object_key = $4,
            recording_started_at = $5,
            recording_duration_s = $6,
            recording_bytes = $7,
            updated_at = now()
        WHERE id = $1 AND tenant_id = $2
        """,
        session_id,
        tenant.id,
        status.value,
        object_key,
        started_at,
        duration_s,
        size_bytes,
    )


async def set_screenshare_recording(
    tenant: Tenant,
    session_id: str,
    *,
    status: RecordingStatus,
    object_key: str | None = None,
    started_at: datetime | None = None,
    duration_s: int | None = None,
    size_bytes: int | None = None,
) -> None:
    """Write the screen-recording outcome for one session.

    Deliberately a second function rather than a flag on ``set_recording``: the
    two artifacts settle at different moments and on different reasons — the
    audio can be 'consent_withdrawn' while the screen video is 'not_shared' —
    and one function writing both would have to be given every field of each on
    every call. Same rule inside, though: every column moves together, so a row
    never says ``stored`` without a key.
    """
    pool = await db.tenant_pool(tenant)
    await pool.execute(
        """
        UPDATE sessions
        SET screenshare_recording_status = $3,
            screenshare_recording_object_key = $4,
            screenshare_recording_started_at = $5,
            screenshare_recording_duration_s = $6,
            screenshare_recording_bytes = $7,
            updated_at = now()
        WHERE id = $1 AND tenant_id = $2
        """,
        session_id,
        tenant.id,
        status.value,
        object_key,
        started_at,
        duration_s,
        size_bytes,
    )


async def set_transfer(tenant: Tenant, session_id: str, transfer: dict[str, object]) -> None:
    """Record that this call was handed to a human, and how it went.

    Written the moment the transfer resolves rather than at finalize, for two
    readers that cannot wait: `session.completed` is built by re-reading the
    session row, so a fact held only in the worker's memory would never reach
    the webhook; and on the bridge path this runs just before the session is
    shut down, which is what drives finalize in the first place.

    A failed transfer is recorded too. "We tried to reach a person and could
    not" is the fact a tenant most wants when a caller complains, and a row that
    only ever records successes cannot answer it.
    """
    pool = await db.tenant_pool(tenant)
    await pool.execute(
        """
        UPDATE sessions
        SET transfer = $3::jsonb,
            updated_at = now()
        WHERE id = $1 AND tenant_id = $2
        """,
        session_id,
        tenant.id,
        json.dumps(transfer, default=str),
    )


async def create_session(
    tenant: Tenant,
    session_id: str,
    agent_id: str | None,
    agent_version_id: str | None,
    agent_name: str | None = None,
    channel: str = "voice",
    type_: str = "WEB",
    conversation_id: str | None = None,
    *,
    trigger_id: str | None = None,
    integration_id: str | None = None,
    conversation_ref_id: str | None = None,
    phone_number_id: str | None = None,
    telephony_account_id: str | None = None,
    livekit_room: str | None = None,
    from_e164: str | None = None,
    to_e164: str | None = None,
    idempotency_key: str | None = None,
    status: str = "running",
    billing_status: str = "pending",
    analysis_enabled: bool = False,
    recording_enabled: bool = False,
    session_vars: dict[str, str] | None = None,
) -> None:
    """Insert or promote a call's session row. (A chat's is written by the API.)

    Voice paths pass a pre-minted id (LiveKit room/job id). SIP outbound may
    insert status=queued first; this promotes queued→running.
    SIP outbound pre-mints queued rows before the worker promotes them.

    ON CONFLICT (id) is a full column upsert for non-terminal rows: linkage
    fields (conversation_ref, trigger, integration, telephony, e164,
    idempotency_key) COALESCE onto existing values; status only promotes
    queued→running (or updates while still non-terminal).

    ``session_vars`` is the `{{vars.*}}` values the call was started with, for
    the paths where the WORKER mints the row. A pre-minted row already carries
    them, so the upsert COALESCEs rather than overwrites — a promote must never
    drop what the call is already running on.

    ``analysis_enabled`` claims `analysis_status = 'pending'` up front. The
    column defaults to 'none', which every reader renders as "analysis is
    switched off for this agent" — a flat lie for the whole duration of a live
    call, and for ever afterwards on a call whose worker died before finalize
    could claim the row (`services/analysis/run.py::run_for_session`). Claiming
    it here makes the column honest by construction: 'none' means off, and
    'pending' means it is coming or the run died owing it.

    ``recording_enabled`` claims `recording_status = 'pending'` the same way and
    for the same reason, in the INSERT rather than in an UPDATE immediately
    after it. `set_recording` writes every recording column together and is what
    settles the row at finalize; this only ever claims it.
    """
    if status not in ("queued", "running"):
        raise ValueError(f"create_session status must be queued or running, got {status}")
    if billing_status not in ("pending", "computed", "unpriceable"):
        raise ValueError(f"invalid billing_status {billing_status}")
    executor = await db.tenant_pool(tenant)
    row = await executor.fetchrow(
        """
        INSERT INTO sessions (
            id, tenant_id, conversation_id, agent_id, agent_version_id,
            agent_name, channel, type, status, trigger_id, integration_id,
            conversation_ref_id, phone_number_id, telephony_account_id,
            livekit_room, from_e164, to_e164, idempotency_key, started_at, billing_status,
            analysis_status, recording_status, vars
        )
        VALUES (
            $1, $2, $3, $4::uuid, $5::uuid,
            $6, $7, $8, $9, $10::uuid, $11::uuid,
            $12::uuid, $13::uuid, $14::uuid,
            $15, $16, $17, $18,
            CASE WHEN $9 = 'running' THEN now() ELSE NULL END,
            $19,
            CASE WHEN $20 THEN 'pending' ELSE 'none' END,
            CASE WHEN $21 THEN 'pending' ELSE 'none' END,
            $22::jsonb
        )
        ON CONFLICT (id) DO UPDATE SET
            conversation_id = COALESCE(EXCLUDED.conversation_id, sessions.conversation_id),
            agent_id = COALESCE(EXCLUDED.agent_id, sessions.agent_id),
            agent_version_id = COALESCE(
                EXCLUDED.agent_version_id, sessions.agent_version_id
            ),
            agent_name = COALESCE(EXCLUDED.agent_name, sessions.agent_name),
            channel = EXCLUDED.channel,
            type = EXCLUDED.type,
            trigger_id = COALESCE(EXCLUDED.trigger_id, sessions.trigger_id),
            integration_id = COALESCE(EXCLUDED.integration_id, sessions.integration_id),
            conversation_ref_id = COALESCE(
                EXCLUDED.conversation_ref_id, sessions.conversation_ref_id
            ),
            phone_number_id = COALESCE(EXCLUDED.phone_number_id, sessions.phone_number_id),
            telephony_account_id = COALESCE(
                EXCLUDED.telephony_account_id, sessions.telephony_account_id
            ),
            livekit_room = COALESCE(EXCLUDED.livekit_room, sessions.livekit_room),
            from_e164 = COALESCE(EXCLUDED.from_e164, sessions.from_e164),
            to_e164 = COALESCE(EXCLUDED.to_e164, sessions.to_e164),
            idempotency_key = COALESCE(EXCLUDED.idempotency_key, sessions.idempotency_key),
            vars = COALESCE(sessions.vars, EXCLUDED.vars),
            -- Only ever claims an unclaimed row. A promote (queued→running)
            -- must not walk back a verdict a re-analysis already wrote.
            analysis_status = CASE
                WHEN sessions.analysis_status = 'none' THEN EXCLUDED.analysis_status
                ELSE sessions.analysis_status
            END,
            -- Same rule, same reason: a promote must not walk back an outcome
            -- `set_recording` already wrote.
            recording_status = CASE
                WHEN sessions.recording_status = 'none' THEN EXCLUDED.recording_status
                ELSE sessions.recording_status
            END,
            status = CASE
                WHEN sessions.status = 'queued' AND EXCLUDED.status = 'running'
                    THEN 'running'
                WHEN sessions.status IN ('queued', 'running') THEN EXCLUDED.status
                ELSE sessions.status
            END,
            started_at = CASE
                WHEN sessions.status IN ('queued', 'running')
                    AND EXCLUDED.status = 'running'
                    THEN COALESCE(sessions.started_at, now())
                ELSE sessions.started_at
            END,
            updated_at = now()
        RETURNING status
        """,
        session_id,
        tenant.id,
        conversation_id,
        agent_id,
        agent_version_id,
        agent_name,
        channel,
        type_,
        status,
        trigger_id,
        integration_id,
        conversation_ref_id,
        phone_number_id,
        telephony_account_id,
        livekit_room,
        from_e164,
        to_e164,
        idempotency_key,
        billing_status,
        analysis_enabled,
        recording_enabled,
        json.dumps(session_vars) if session_vars else None,
    )
    if row is None:
        raise RuntimeError("create_session returned no row")
    if status == "running" and row["status"] != "running":
        raise RuntimeError(f"execution is already terminal: {row['status']}")
    if status == "running" and conversation_id:
        # The contact is on the line: an outbound call's conversation was opened
        # `inactive` and this is the moment it was answered.
        await executor.execute(
            """
            UPDATE conversations SET status = 'active', updated_at = now()
            WHERE id = $1 AND tenant_id = $2 AND status = 'inactive'
            """,
            conversation_id,
            tenant.id,
        )


async def finalize_session(
    tenant: Tenant,
    session_id: str,
    close_reason: str | None,
    session: AgentSession[dict[str, object]],
    cfg: AgentConfig,
    avatar_state: AvatarState | None = None,
    ended_at: datetime | None = None,
    extra_usage: Sequence[Any] = (),
    error: BaseException | None = None,
) -> list[TranscriptRow]:
    """Settle one session's row: status, transcript, usage, metrics.

    ``extra_usage`` is model usage from sessions other than the caller's own —
    today only the warm-transfer briefing leg, which is a second `AgentSession`
    and would otherwise be free. It is priced exactly like the caller's usage:
    the models are the same objects, so canonicalization and the failover lanes
    below already cover it.

    ``error`` is what killed the run, and it is the only reason the tenant can
    read: the exception itself is otherwise a line in a container log nobody
    outside this platform can open. It lands in ``sessions.error`` and the call
    detail page renders it under the ending.
    """
    pool = await db.tenant_pool(tenant)

    status = session_status_for_close_reason(close_reason)
    terminal_status = "completed" if status == "completed" else "failed"
    if not isinstance(session.userdata, dict):
        raise TypeError(f"session.userdata must be a dict, got {type(session.userdata).__name__}")
    userdata = {k: v for k, v in session.userdata.items() if not is_reserved_key(k)}

    # Items are written live via session events; do not re-ingest history here.
    # Caller must await in-flight persist tasks before finalize.
    transcript: list[TranscriptRow] = await load_session_transcript(tenant, session_id)

    try:
        usage = list(session.usage.model_usage)
    except Exception:
        usage = []
    usage.extend(extra_usage)
    cat = get_catalog()
    # Per kind, the models this agent could have run: the primary and, when one
    # is configured, its failover target. A session that failed over meters both.
    configured: dict[str, list[tuple[str, str]]] = {
        kind: [
            (s.provider, s.model) for s in (spec, getattr(spec, "fallback", None)) if s is not None
        ]
        for kind, spec in (
            ("llm", cfg.llm),
            ("stt", cfg.stt),
            ("tts", cfg.tts),
            ("realtime", cfg.realtime),
        )
        if spec is not None
    }
    # The second model a GPT-Live agent runs. It is not in `cfg` at all — the
    # agent names one speech-to-speech model and the catalog names the Responses
    # model that model delegates to — so it has to be read off the entry here, or
    # its tokens arrive as an LLM row this session has no configured LLM to
    # canonicalize against. None for every other realtime model and every cascade
    # agent, which is what leaves their behaviour untouched.
    backend_llm: tuple[str, str] | None = None
    if cfg.realtime is not None:
        realtime_entry = cat.entry("realtime", cfg.realtime.provider, cfg.realtime.model)
        backend_model = getattr(realtime_entry, "extra", {}).get("backend_model")
        if backend_model:
            backend_llm = cat.canonicalize("llm", cfg.realtime.provider, str(backend_model))
            if backend_llm is not None:
                configured["llm"] = [backend_llm]
    # Which of this agent's LLMs ran in the provider's priority lane, keyed by the
    # canonical names a metered row is resolved to. Per model rather than one
    # flag for the session: a failover is a different model and picks its own
    # lane, so a session that failed over must not bill the fallback at the
    # primary's rates.
    llm_priority: dict[tuple[str, str], bool] = {
        (entry.provider, entry.model): s.priority
        for s in (cfg.llm, cfg.llm.fallback if cfg.llm else None)
        if s is not None
        for entry in [cat.entry("llm", s.provider, s.model)]
        if entry is not None
    }

    # LiveKit has no realtime usage type: a speech-to-speech model reports
    # LLMModelUsage, already broken out per modality. So `llm_usage` rows are
    # sorted per ROW rather than per session, because one speech-to-speech model
    # produces both kinds. GPT-Live runs its reasoning and its tools on a second,
    # ordinary Responses model, and reports that model's tokens under its own
    # name alongside the session's per-minute row — bill both as realtime and the
    # backend spend would land on an entry whose token rates are 0.00 and vanish.
    #
    # Everything else still resolves exactly as before: on a cascade agent there
    # is no realtime model, and on the other speech-to-speech models there is no
    # second model, so in both cases every row takes the one branch that exists.
    def llm_row_kind(model: str) -> str:
        if cfg.realtime is None:
            return "llm"
        # Asked against the CONFIGURED provider, never the one the row reports:
        # the OpenAI plugin reports the API hostname there, which the catalog
        # recognizes for nothing. The model id is the half that is trustworthy,
        # and OpenAI echoes it unchanged ("gpt-5.6-luna", not a dated snapshot).
        if backend_llm and cat.canonicalize("llm", cfg.realtime.provider, model) == backend_llm:
            return "llm"
        return "realtime"

    # What this call's models said their own turns and transcriptions cost, for
    # the providers that report it. Read off the session rather than off any
    # model object: a call that hands off, fails over or enters a task runs
    # several, and `session.llm` is only the last of them. This worker writes `llm_usage` rows itself rather
    # than through `llm_usage_lines`, so it does its own lookup.
    reported_cost = collector_in(session.userdata)
    # These DELETEs make a re-finalize of the same session idempotent, so they
    # only need to cover the tables this run is ABOUT to write — a normal voice
    # call has rows in two of the four. `llm_kind` is what decides which table
    # `llm_usage` metrics land in, so it decides which one is cleared too.
    # Nothing is cleared for a run that reports no usage at all, which is the
    # right way round: an empty second pass must not wipe a bill the first one
    # got right.
    kinds: set[str] = set()
    for u in usage:
        reported = str(getattr(u, "type", ""))
        kinds.add(
            llm_row_kind(getattr(u, "model", "") or "")
            if reported == "llm_usage"
            else reported.removesuffix("_usage")
        )
    # Iterating the literal tuple rather than `kinds`: the table name is
    # interpolated into the statement, so it must come from this file.
    for kind in ("llm", "tts", "stt", "realtime"):
        if kind not in kinds:
            continue
        try:
            await pool.execute(
                f"DELETE FROM {kind}_usage WHERE session_id = $1 AND tenant_id = $2",
                session_id,
                tenant.id,
            )
        except Exception:
            logger.warning("failed to clear prior %s_usage for %s", kind, session_id, exc_info=True)
    for u in usage:
        kind = getattr(u, "type", "")
        provider = getattr(u, "provider", "") or ""
        model = getattr(u, "model", "") or ""
        ck = llm_row_kind(model) if kind == "llm_usage" else kind.removesuffix("_usage")
        if ck in configured:
            provider, model = cat.canonicalize_usage(ck, provider, model, configured[ck])
        try:
            if ck == "realtime":
                await pool.execute(
                    "INSERT INTO realtime_usage (session_id, provider, model, "
                    "input_text_tokens, input_cached_text_tokens, input_audio_tokens, "
                    "input_cached_audio_tokens, output_text_tokens, output_audio_tokens, "
                    "session_seconds, tenant_id) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)",
                    session_id,
                    provider,
                    model,
                    getattr(u, "input_text_tokens", 0),
                    getattr(u, "input_cached_text_tokens", 0),
                    getattr(u, "input_audio_tokens", 0),
                    getattr(u, "input_cached_audio_tokens", 0),
                    getattr(u, "output_text_tokens", 0),
                    getattr(u, "output_audio_tokens", 0),
                    getattr(u, "session_duration", 0.0),
                    tenant.id,
                )
            elif kind == "llm_usage":
                await pool.execute(
                    "INSERT INTO llm_usage (session_id, provider, model, input_tokens, "
                    "input_cached_tokens, input_cache_write_tokens, output_tokens, "
                    "reported_cost, priority, tenant_id) "
                    "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)",
                    session_id,
                    provider,
                    model,
                    getattr(u, "input_tokens", 0),
                    getattr(u, "input_cached_tokens", 0),
                    getattr(u, "input_cache_creation_tokens", 0),
                    getattr(u, "output_tokens", 0),
                    reported_cost.of(provider, model) if reported_cost else None,
                    llm_priority.get((provider, model), False),
                    tenant.id,
                )
            elif kind == "tts_usage":
                await pool.execute(
                    "INSERT INTO tts_usage (session_id, provider, model, characters_count, "
                    "audio_duration, input_tokens, output_tokens, tenant_id) "
                    "VALUES ($1,$2,$3,$4,$5,$6,$7,$8)",
                    session_id,
                    provider,
                    model,
                    getattr(u, "characters_count", 0),
                    getattr(u, "audio_duration", 0.0),
                    getattr(u, "input_tokens", 0),
                    getattr(u, "output_tokens", 0),
                    tenant.id,
                )
            elif kind == "stt_usage":
                await pool.execute(
                    "INSERT INTO stt_usage (session_id, provider, model, audio_duration, "
                    "input_tokens, output_tokens, reported_cost, tenant_id) "
                    "VALUES ($1,$2,$3,$4,$5,$6,$7,$8)",
                    session_id,
                    provider,
                    model,
                    getattr(u, "audio_duration", 0.0),
                    getattr(u, "input_tokens", 0),
                    getattr(u, "output_tokens", 0),
                    reported_cost.of(provider, model) if reported_cost else None,
                    tenant.id,
                )
        except Exception:
            logger.warning("failed to persist %s for %s", kind, session_id, exc_info=True)

    if avatar_state:
        try:
            end = avatar_state.get("stopped_at") or datetime.now(UTC)
            seconds = max((end - avatar_state["started_at"]).total_seconds(), 0.0)
            await pool.execute(
                "DELETE FROM avatar_usage WHERE session_id = $1 AND tenant_id = $2",
                session_id,
                tenant.id,
            )
            await pool.execute(
                "INSERT INTO avatar_usage (session_id, provider, model, avatar_id, "
                "avatar_session_id, seconds, tenant_id) VALUES ($1,$2,$3,$4,$5,$6,$7)",
                session_id,
                avatar_state["provider"],
                avatar_state["model"],
                avatar_state.get("avatar_id"),
                avatar_state.get("avatar_session_id"),
                seconds,
                tenant.id,
            )
        except Exception:
            logger.exception("failed to persist avatar usage for session %s", session_id)

    # Session bag: usage flag + simple per-metric averages (ms) for run detail.
    # Turn-level samples remain on conversation_items for workspace aggregates.
    latency = compute_latency(transcript)
    metrics_payload = {
        "usage_reported": bool(usage) or bool(avatar_state),
        **latency,
    }
    # Truncated because a provider exception carries whole response bodies, and
    # this is read as one sentence in a red banner.
    error_payload = (
        json.dumps({"message": str(error)[:2048], "type": type(error).__name__})
        if error is not None
        else None
    )

    try:
        final_ended_at = ended_at or datetime.now(UTC)
        # One transaction, because the two statements below write the same
        # userdata to two places: a crash between them left a finalized call
        # whose contact record was never updated. Deliberately not a CTE —
        # `WITH s AS (UPDATE … RETURNING …)` would buy one round trip and cost
        # the readability of both statements, and the reason to hold them
        # together is correctness rather than latency.
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute(
                """
                UPDATE sessions
                SET status = CASE
                        WHEN status IN ('completed', 'failed', 'canceled') THEN status
                        ELSE $3
                    END,
                    ended_at = COALESCE(ended_at, $5),
                    close_reason = COALESCE(close_reason, $2),
                    userdata = $4::jsonb,
                    metrics = $7::jsonb,
                    -- Same "first one wins" as close_reason above: a re-finalize
                    -- that carries no error must not erase the one that did.
                    error = COALESCE(error, $8::jsonb),
                    updated_at = now()
                WHERE id = $1 AND tenant_id = $6
                """,
                session_id,
                close_reason,
                terminal_status,
                json.dumps(userdata, default=str),
                final_ended_at,
                tenant.id,
                json.dumps(metrics_payload),
                error_payload,
            )
            # What we now know about this person, written to the IDENTITY — the
            # one place a fact about a caller lives, whichever conversation
            # learned it.
            #
            # Unconditional, whatever the agent's `conversation.context` was:
            # reading the bag at the start of a call is the tenant's choice, but
            # the contact record should be current either way, and
            # `sessions.userdata` above keeps this run's own truth regardless.
            # Refusing to write would leave the identity holding facts we know
            # to be superseded.
            #
            # Resolved through `conversations` rather than through the nullable
            # convenience column `sessions.conversation_ref_id`, because
            # `conversations.conversation_ref_id` is the invariant edge.
            await conn.execute(
                """
                UPDATE conversation_refs r
                SET userdata = $3::jsonb, updated_at = now()
                FROM conversations c
                WHERE c.id = (SELECT conversation_id FROM sessions WHERE id = $1 AND tenant_id = $2)
                  AND c.tenant_id = $2
                  AND r.id = c.conversation_ref_id AND r.tenant_id = c.tenant_id
                """,
                session_id,
                tenant.id,
                json.dumps(userdata, default=str),
            )
    except Exception:
        logger.exception("failed to mark session %s finalized", session_id)
    logger.info(
        "finalized session %s (%s, %s)",
        session_id,
        close_reason,
        terminal_status,
    )
    return transcript
