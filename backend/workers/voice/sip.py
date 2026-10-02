"""LiveKit SIP room path (inbound dispatch rule or outbound CreateAgentDispatch)."""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from uuid import UUID, uuid4

from livekit import api as lk_api
from livekit import rtc
from livekit.agents import AutoSubscribe, CloseEvent, JobContext

import db
from compiler.operations import (
    RUNTIME_KEY_CALL_FIELDS,
    RUNTIME_KEY_SEND_DTMF,
    RUNTIME_KEY_TRANSFER,
)
from services import credits, session_events
from services.agents import ConversationContext, ConversationSpec
from services.close_reasons import sip_dial_close_reason
from services.conversations import (
    ensure_sip_ref_on_conn,
    sip_conversation_key,
)
from services.system_vars import build_call_fields
from services.telephony import livekit_sip
from services.telephony.e164 import normalize_e164
from services.telephony.models import TELEPHONY_ACCOUNT_COLUMNS, TelephonyAccount
from services.telephony.providers import ProviderError, get_adapter
from workers.session import persistence
from workers.voice import transfer
from workers.voice.keypad import KeypadCollector
from workers.voice.recording import recording_options
from workers.voice.room_options import room_options
from workers.voice.runtime import VoiceRun, VoiceSessionSpec

logger = logging.getLogger("talqing.workers.voice")

# RoomIO auto-closes AgentSession only for CLIENT_INITIATED / ROOM_DELETED /
# USER_REJECTED. SIP mid-call failures use other DisconnectReasons and would
# otherwise leave the session running until the stale sweeper.
_ROOMIO_AUTO_CLOSE_DISCONNECT = frozenset(
    {
        int(rtc.DisconnectReason.CLIENT_INITIATED),
        int(rtc.DisconnectReason.ROOM_DELETED),
        int(rtc.DisconnectReason.USER_REJECTED),
    }
)


def _normalize_sip_e164(
    raw: str | None, *, fallback: str | None = None, did: str | None = None
) -> str | None:
    """Best-effort E.164 for a SIP attribute, read against the DID it arrived on.

    A carrier hands us caller ID in national form — Vobiz sends every domestic
    Indian caller as `09…` — and a national number is national relative to OUR
    number, not to the platform. `did` is what says which country that is; with
    two regions there is no platform-wide answer, and `07911123456` is a UK
    mobile and an Indian national string at the same time.

    Deliberately NOT strict. This is a number off a carrier's INVITE, and
    refusing a real caller because our metadata copy has not caught up with their
    number plan is worse than carrying one we cannot fully verify — the call log
    renders this, and a workspace whose every caller reads "unknown" is not a
    defensible product. Unreadable stays `None`, which skips the cross-call bind
    rather than collapsing every caller onto one identity.
    """
    for candidate in (raw, fallback):
        if not candidate or not str(candidate).strip():
            continue
        try:
            return normalize_e164(str(candidate).strip(), did=did, strict=False)
        except ValueError:
            continue
    return None


def _sip_disconnect_close_reason(reason: object) -> str:
    """Map a LiveKit DisconnectReason to a billable ``*_failed`` close reason."""
    if reason is None:
        return "sip_participant_disconnected_failed"
    name: str | None = None
    try:
        name = rtc.DisconnectReason.Name(int(reason))  # type: ignore[attr-defined, arg-type]
    except (ValueError, AttributeError, TypeError):
        name = getattr(reason, "name", None) or str(reason)
    label = str(name).removeprefix("DisconnectReason.").lower()
    if not label or label in ("unknown_reason", "none"):
        return "sip_participant_disconnected_failed"
    return f"sip_{label}_failed"


def _job_ends_with_session(run: VoiceRun) -> bool:
    """Should closing the AgentSession also end the job?

    Yes on every call but one. After a bridged transfer the session is closed
    deliberately — that is where the meter stops — while the job stays connected
    as the janitor that hangs up the surviving leg when the first human leaves
    (`workers/voice/transfer.py`). Shutting the job down on `close` would
    disconnect us from the room before that can ever happen, and the symptom is
    not a crash: the transfer works, the call bills correctly, and the surviving
    carrier leg stays up on silence for three hours.

    Kept here rather than raced against in the transfer module so that "who ends
    the job" stays answerable in one place.
    """
    return run.transfer_state != transfer.STATE_BRIDGED


def wire_sip_disconnect_safety(
    ctx: JobContext,
    run: VoiceRun,
    *,
    sip_identity: str | None,
) -> None:
    """Force session shutdown when SIP drops for reasons RoomIO ignores.

    The handler filters on the caller's own identity, so on a bridged transfer
    the *human's* leg joining and leaving never reaches it. That is correct: the
    janitor in `workers/voice/transfer.py` owns those departures.
    """

    @ctx.room.on("participant_disconnected")
    def _on_sip_participant_disconnected(participant: rtc.RemoteParticipant) -> None:
        if participant.kind != rtc.ParticipantKind.PARTICIPANT_KIND_SIP:
            return
        if sip_identity and participant.identity != sip_identity:
            return
        if run.transfer_state == transfer.STATE_BRIDGED:
            # The janitor owns the job now, and this departure is its cue. Ending
            # the job here instead would disconnect us before `delete_room` lands
            # and leave the *other* human on an open carrier leg listening to
            # silence — the defect the janitor exists to prevent.
            return
        if run.transfer_state is not None:
            # A caller who leaves during or after a transfer did not fail: on a
            # successful REFER the carrier took them, and mid-handshake the
            # transfer simply overtook them. Without this branch the mapping
            # below files every successful transfer as `sip_*_failed`, which
            # also waives the platform fee (`billing/pricing.py`).
            logger.info(
                "SIP participant left after a transfer (%s); closing as transferred",
                run.transfer_state,
                extra={"identity": participant.identity},
            )
            run.set_close_reason(transfer.CLOSE_REASON_TRANSFERRED)
            run.mark_ended()
            try:
                run.session.shutdown()
            except Exception:
                logger.exception("failed to shutdown AgentSession after transfer")
            ctx.shutdown(transfer.CLOSE_REASON_TRANSFERRED)
            return
        reason = participant.disconnect_reason
        try:
            reason_i = int(reason) if reason is not None else None
        except (TypeError, ValueError):
            reason_i = None
        if reason_i is not None and reason_i in _ROOMIO_AUTO_CLOSE_DISCONNECT:
            return
        close_reason = _sip_disconnect_close_reason(reason_i if reason_i is not None else reason)
        logger.warning(
            "SIP participant disconnected with non-auto-close reason; shutting down (%s)",
            close_reason,
            extra={"identity": participant.identity, "disconnect_reason": reason},
        )
        run.mark_ended()
        try:
            run.session.shutdown()
        except Exception:
            logger.exception("failed to shutdown AgentSession after SIP disconnect")
        ctx.shutdown(close_reason)


# A caller whose number we cannot read. `*_failed` is the platform's convention
# for a billable-shaped reason that waives the platform fee
# (`services/billing/pricing.py::is_failed_close_reason`).
UNIDENTIFIED_CALLER_CLOSE_REASON = "sip_caller_unidentified_failed"


# The version an inbound call answers on and that version's past-conversation
# setting, in one read. An inbound dispatch rule carries no version, so the
# answering agent's published one has to be looked up either way — and the
# `conversation` block the bind below needs lives on exactly that version.
_INBOUND_AGENT_SELECT = """
SELECT a.published_version, av.config->'conversation' AS conversation
FROM agents a
JOIN agent_versions av
    ON av.agent_id = a.id AND av.version = a.published_version AND av.tenant_id = a.tenant_id
WHERE a.id = $1::uuid AND a.tenant_id = $2
"""


def _conversation_spec(raw: object, *, agent_id: object, version: object) -> ConversationSpec:
    """Parse a frozen version's `conversation` block.

    A frozen config always carries it — `AgentConfig` gives it a default and
    dumps every field — so a missing key means the version was written by
    something that is not `AgentConfig`, and answering the call on invented
    defaults would hide that. It aborts the call instead.
    """
    if raw is None:
        raise RuntimeError(
            f"agent {agent_id} version {version} has no conversation settings in its config"
        )
    return ConversationSpec.model_validate(raw)


async def _inbound_conversation_spec(
    *,
    pool,
    tenant_id: UUID,
    agent_id: UUID,
    version: int,
) -> ConversationSpec:
    """The answering agent's past-conversation setting, for a NAMED version.

    The ordinary inbound path never reaches this: it has to look the published
    version up anyway, and `_INBOUND_AGENT_SELECT` returns both in one read.
    This is for the case where the dispatch metadata named a version — which a
    dispatch rule does not do, and which is why this stays.
    """
    row = await pool.fetchrow(
        """
        SELECT av.config->'conversation' AS conversation
        FROM agent_versions av
        WHERE av.agent_id = $1 AND av.version = $2 AND av.tenant_id = $3
        """,
        agent_id,
        version,
        tenant_id,
    )
    if not row:
        raise RuntimeError(f"agent {agent_id} version {version} is missing")
    return _conversation_spec(row["conversation"], agent_id=agent_id, version=version)


async def _bind_inbound_conversation(
    *,
    pool,
    tenant_id: UUID,
    fallback_agent_id: UUID,
    phone_number_id: UUID,
    did_e164: str,
    peer_e164: str,
    context: ConversationContext,
) -> tuple[str, str, UUID, int | None]:
    """Shared conversation entry for inbound SIP.

    Returns (conversation_id, conversation_ref_id, run_agent_id, published_version|None).
    Each call starts on the dispatch/trigger agent; handoffs stay in-call.
    When published_version is None the caller should load it from the agent row.
    """
    key = sip_conversation_key(did_e164=did_e164, peer_e164=peer_e164)
    async with pool.acquire() as conn:
        async with conn.transaction():
            thread = await ensure_sip_ref_on_conn(
                conn,
                tenant_id=tenant_id,
                conversation_key=key,
                phone_number_id=phone_number_id,
                context=context,
                metadata={
                    "phone_e164": peer_e164,
                    "did_e164": did_e164,
                    "direction": "inbound",
                },
            )
            return (
                str(thread.conversation_id),
                str(thread.ref.id),
                fallback_agent_id,
                None,
            )


def make_dtmf_sender(ctx: JobContext, run_holder: list[VoiceRun]) -> object:
    """A callable that presses one key on this call's keypad.

    Both ends were already wired by livekit-sip long before this existed —
    `WriteInboundDTMFTo` on the way in, an inbound `SipDTMF` data packet handled
    on the way out — so there is no transport work here at all. What was missing
    was ever listening and ever sending.

    A `list` holding the run rather than the run itself, because the runtime
    context has to exist before `VoiceRun.prepare` compiles the tools that
    capture it. The list is filled the moment prepare returns.
    """

    async def send(digit: str) -> None:
        from livekit.agents.beta.workflows.utils import DtmfEvent, dtmf_event_to_code

        await ctx.room.local_participant.publish_dtmf(
            code=dtmf_event_to_code(DtmfEvent(digit)), digit=digit
        )
        if run_holder:
            run_holder[0].events.record(session_events.DTMF_SENT, {"digit": digit})

    return send


def _attach_keypad(ctx: JobContext, run: VoiceRun) -> None:
    """Let the caller answer with the keypad, when this agent asked for that."""
    if not run.cfg.keypad_input.enabled:
        return

    def _trace(digit: str) -> None:
        run.events.record(session_events.DTMF_RECEIVED, {"digit": digit})

    KeypadCollector(run.session, run.cfg.keypad_input, on_digit=_trace).attach(ctx.room)


def wire_call_lifecycle(ctx: JobContext, run: VoiceRun) -> None:
    """Tie a prepared run to the job: finalize on shutdown, end with the session.

    Every SIP-shaped call does this the moment `prepare` returns, before the
    caller is connected, so a call that fails to connect still finalizes.
    """

    async def _finalize(reason: str) -> None:
        await run.finalize_with_timeout(reason or "job_shutdown")

    ctx.add_shutdown_callback(_finalize)
    run.wire_room_events(ctx.room)

    @ctx.room.on("disconnected")
    def _on_room_disconnected(*_: object) -> None:
        run.mark_ended()

    @run.session.on("close")
    def _on_close(ev: CloseEvent) -> None:
        if not _job_ends_with_session(run):
            return
        reason = getattr(getattr(ev, "reason", None), "value", None) or str(
            getattr(ev, "reason", "")
        )
        ctx.shutdown(reason)


async def start_call(
    ctx: JobContext,
    run: VoiceRun,
    sip_participant: rtc.RemoteParticipant,
    *,
    before_entry: Callable[[], Awaitable[None]] | None = None,
) -> None:
    """Put the agent on a connected SIP leg and let it speak first.

    ``before_entry`` runs between the session starting and the greeting — for a
    leg that is answered before its audio path is (a WhatsApp call).
    """
    _attach_keypad(ctx, run)
    await run.session.start(
        run.agent,
        room=ctx.room,
        record=recording_options(run.cfg),
        room_options=room_options(
            sip_participant.identity,
            noise_cancellation=run.build_noise_cancellation(),
        ),
    )
    await run.start_background_audio(ctx)
    if before_entry is not None:
        await before_entry()
    await run.begin_entry()


async def fail_unprepared_call(tenant, session_id: str, exc: BaseException) -> None:
    """Close the row of a call that died before `prepare` gave it a run."""
    pool = await db.tenant_pool(tenant)
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
        logger.exception("failed to mark SIP session failed after early error")


async def run_sip_call(ctx: JobContext, meta: dict[str, object]) -> None:
    """LiveKit SIP room path (inbound dispatch rule or outbound CreateAgentDispatch)."""
    direction = str(meta.get("direction") or "").strip()
    tenant_id = meta.get("tenant")
    agent_id = meta.get("agent")
    phone_number_id = meta.get("phone_number_id")
    telephony_account_id = meta.get("telephony_account_id")
    did_meta = str(meta.get("did_e164") or meta.get("from_e164") or "").strip()
    # OUR number on this call, and therefore the country every national-format
    # number on it is national to. Inbound it is `to_e164`; outbound it is
    # `did_meta`, which the dispatch carries. Either way it comes from
    # `phone_numbers.e164` — a number we own — and never from a carrier's
    # attributes.
    to_e164 = _normalize_sip_e164(str(meta.get("to_e164") or "") or None, did=did_meta or None)
    our_did = to_e164 if direction == "inbound" else (did_meta or to_e164)
    # No `did_meta` fallback: it holds *our* DID, and an inbound dispatch rule
    # carries no `from_e164`, so falling back filed every inbound call as the
    # business phoning itself. The SIP conversation key is `sip:{did}:{peer}`, so
    # a caller we cannot read collapsed onto `sip:{did}:{did}` and inherited a
    # stranger's history. That is still why we would rather refuse than guess:
    # an inbound caller we cannot identify is turned away (see the branch below),
    # not answered anonymously.
    from_e164 = _normalize_sip_e164(str(meta.get("from_e164") or "") or None, did=our_did)

    def _abort(reason: str, message: str, **fields: object) -> None:
        logger.error(
            "aborting sip dispatch: %s",
            message,
            extra={"dispatch_metadata": meta, "reason": reason, **fields},
        )
        ctx.shutdown(reason)

    if direction not in ("inbound", "outbound"):
        _abort("invalid_sip_direction_failed", f"unknown SIP direction {direction!r}")
        return
    if not tenant_id:
        _abort("missing_dispatch_metadata", "SIP metadata missing tenant")
        return
    # An inbound dispatch rule always names the agent that answers. An outbound
    # dial may not: a call placed with an agent defined in the request has no
    # `agents` row, and `sessions.agent_plan` — written before the dial — is
    # what the worker resolves instead.
    if direction == "inbound" and not agent_id:
        _abort("missing_dispatch_metadata", "SIP metadata missing agent")
        return

    tenant = await persistence.load_tenant(str(tenant_id))
    if not tenant:
        _abort("unknown_tenant", "unknown tenant", tenant=tenant_id)
        return

    pool = await db.tenant_pool(tenant)
    # Both are pure reads off the job, and both are needed by the credit gate
    # below — which runs before the version lookup, so a call we are not going to
    # answer costs no query, no compile, no conversation and no room work.
    room_name = ctx.job.room.name
    session_id = str(meta.get("session") or "").strip() or str(uuid4())

    if direction == "inbound" and not await credits.has_credit(tenant):
        # A workspace with nothing left. Refused at the door, with the same
        # visible row an unreadable caller number leaves — the tenant has to be
        # able to see in the product why their number stopped answering.
        #
        # Outbound is deliberately NOT gated here: it was already gated before
        # the dial (`services/telephony`), and a second check in the worker would
        # refuse a call the carrier has already connected.
        await persistence.record_refused_call(
            tenant,
            session_id=session_id,
            type_="SIP_INBOUND",
            agent_id=str(agent_id),
            close_reason=credits.INSUFFICIENT_CREDITS_CLOSE_REASON,
            phone_number_id=str(phone_number_id) if phone_number_id else None,
            telephony_account_id=str(telephony_account_id) if telephony_account_id else None,
            livekit_room=room_name,
            from_e164=from_e164,
            to_e164=to_e164,
        )
        _abort(
            credits.INSUFFICIENT_CREDITS_CLOSE_REASON,
            "workspace is out of credits",
            did_e164=to_e164,
        )
        return

    version_meta = meta.get("version")
    version: int | None
    # Loaded with the published version below, on the one path that looks it up.
    inbound_conversation: ConversationSpec | None = None
    if not agent_id or direction == "outbound":
        # An outbound dial always carries its version explicitly — it was written
        # beside the session row before anything was dialled — so a missing one
        # means the plan on that row decides: an unpublished draft, or an agent
        # defined in the request, neither of which HAS a published version to
        # look up. An inbound dispatch rule carries no version at all, which is
        # why the lookup below stays.
        try:
            version = int(version_meta) if version_meta is not None else None
        except (TypeError, ValueError):
            _abort("invalid_dispatch_metadata", "malformed version")
            return
    elif version_meta is None:
        row = await pool.fetchrow(_INBOUND_AGENT_SELECT, str(agent_id), tenant.id)
        if not row or row["published_version"] is None:
            _abort("unpublished_agent_failed", "agent has no published version")
            return
        version = int(row["published_version"])
        try:
            inbound_conversation = _conversation_spec(
                row["conversation"], agent_id=agent_id, version=version
            )
        except Exception as exc:
            logger.exception("inbound SIP agent has unreadable conversation settings")
            _abort("conversation_bind_failed", str(exc))
            return
    else:
        try:
            version = int(version_meta)
        except (TypeError, ValueError):
            _abort("invalid_dispatch_metadata", "malformed version")
            return

    initial_userdata = meta.get("userdata") if isinstance(meta.get("userdata"), dict) else {}
    conversation_id = str(meta["conversation_id"]) if meta.get("conversation_id") else None
    conversation_ref_id = (
        str(meta["conversation_ref_id"]) if meta.get("conversation_ref_id") else None
    )
    run_agent_id = str(agent_id) if agent_id else None
    session_type = "SIP_OUTBOUND" if direction == "outbound" else "SIP_INBOUND"
    ctx.log_context_fields = {
        "session_type": session_type,
        "tenant": str(tenant_id),
        "agent": run_agent_id,
        "version": version,
        "session": session_id,
        "room": room_name,
        "direction": direction,
    }

    run: VoiceRun | None = None
    # Built before either session is compiled: `compiler/tools.py` copies the
    # runtime context into each tool at build time, so the transfer callable has
    # to exist by then. The caller's identity and this call's own DID arrive via
    # `attach` once they are known.
    transfer_ctx = transfer.TransferContext(ctx)
    # Filled the moment `prepare` returns; see `make_dtmf_sender`.
    run_holder: list[VoiceRun] = []
    dtmf_sender = make_dtmf_sender(ctx, run_holder)

    try:
        # Connect first so we can wait for the SIP participant (and, for inbound,
        # resolve conversation history) before compiling the agent session.
        await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)

        if direction == "outbound":
            # Both numbers were normalized off the dispatch metadata above, so
            # the bag is complete before we dial — which is what lets the prompt
            # read `{{system_vars.*}}` at compile time. It is never rebuilt after
            # the callee answers: `sip.phoneNumber` / `sip.trunkPhoneNumber` on an
            # outbound leg are our own request echoed back by LiveKit, so there
            # is nothing to correct, and the dialled number is the one the tenant
            # asked us to call.
            call_fields = build_call_fields(
                direction="outbound", human_e164=to_e164, agent_e164=from_e164
            )
            # Stay queued while ringing so ring time is not billable wall-clock.
            try:
                run = await VoiceRun.prepare(
                    VoiceSessionSpec(
                        tenant=tenant,
                        session_id=session_id,
                        agent_id=run_agent_id,
                        agent_version=version,
                        session_type=session_type,
                        conversation_id=conversation_id,
                        initial_userdata=initial_userdata,
                        allowed_channels=("voice",),
                        phone_number_id=str(phone_number_id) if phone_number_id else None,
                        telephony_account_id=(
                            str(telephony_account_id) if telephony_account_id else None
                        ),
                        livekit_room=room_name,
                        from_e164=from_e164,
                        to_e164=to_e164,
                        conversation_ref_id=conversation_ref_id,
                    ),
                    ctx.proc.userdata["vad"],
                    session_status="queued",
                    runtime_extra={
                        RUNTIME_KEY_TRANSFER: transfer_ctx.perform,
                        RUNTIME_KEY_CALL_FIELDS: call_fields,
                        # Always on SIP: livekit-sip emits RFC 4733 for an
                        # inbound `SipDTMF` packet on both directions of a call.
                        RUNTIME_KEY_SEND_DTMF: dtmf_sender,
                    },
                )
            except RuntimeError as exc:
                logger.exception("sip outbound prepare failed")
                _abort("prepare_failed", str(exc))
                return
            run_holder.append(run)
            wire_call_lifecycle(ctx, run)

            if not to_e164 or not from_e164 or not telephony_account_id:
                await run.finalize_with_timeout("sip_outbound_missing_dial_failed")
                ctx.shutdown("sip_outbound_missing_dial_failed")
                return
            # The shared column list, not a copy: TelephonyAccount.from_row indexes
            # every column by name, so a hand-written SELECT that misses one fails
            # at dial time with a bare KeyError. sip_uri was added with the SIP
            # edge indirection and this query alone did not get it.
            account_row = await pool.fetchrow(
                f"""
                SELECT {TELEPHONY_ACCOUNT_COLUMNS}
                FROM telephony_accounts
                WHERE id = $1::uuid AND tenant_id = $2
                """,
                str(telephony_account_id),
                tenant.id,
            )
            if not account_row:
                await run.finalize_with_timeout("sip_account_missing_failed")
                ctx.shutdown("sip_account_missing_failed")
                return
            account = TelephonyAccount.from_row(account_row)
            try:
                # The same rows `prepare` decrypted moments ago, not a second
                # read of the `secrets` table.
                inline = get_adapter(account.provider).outbound_inline_config(
                    account, run.tool_secrets, from_e164=from_e164
                )
            except ProviderError:
                logger.exception("outbound trunk config failed")
                await run.finalize_with_timeout("sip_trunk_config_failed")
                ctx.shutdown("sip_trunk_config_failed")
                return
            # Carrier facts on every subsequent record from this job, not just
            # the failure line. tenant/agent/session/room are already injected
            # (set above); none of these are.
            ctx.log_context_fields = {
                **ctx.log_context_fields,
                "provider": account.provider,
                "telephony_account": str(account.id),
                "from_e164": from_e164,
                "to_e164": to_e164,
                "sip_host": inline.hostname,
                "sip_transport": inline.transport,
            }
            transport = livekit_sip.sip_transport(inline.transport)
            try:
                await livekit_sip.create_sip_participant(
                    room_name=room_name,
                    sip_call_to=to_e164,
                    sip_number=from_e164,
                    hostname=inline.hostname,
                    auth_username=inline.auth_username,
                    auth_password=inline.auth_password,
                    participant_identity=f"sip-out-{to_e164}",
                    transport=transport,
                    wait_until_answered=True,
                    play_dialtone=True,
                )
            except lk_api.SipCallError as e:
                # The carrier answered our INVITE with a status, and that status
                # is the difference between "call them back in half an hour" and
                # "never call this number again". Catching it ahead of the bare
                # `except Exception` below is what stops busy, no-answer,
                # declined, disconnected-number and broken-trunk from all
                # arriving as one indistinguishable `sip_dial_failed`.
                reason = sip_dial_close_reason(e.sip_status_code)
                logger.warning(
                    "CreateSIPParticipant refused: sip_status_code=%s sip_status=%s reason=%s",
                    e.sip_status_code,
                    e.sip_status,
                    reason,
                )
                await run.finalize_with_timeout(reason)
                ctx.shutdown(reason)
                return
            except Exception:
                # No formatting needed: TwirpError.__str__ renders code,
                # message, status and metadata (including sip_status_code),
                # and the traceback logger.exception appends ends on that line.
                logger.exception("CreateSIPParticipant failed")
                await run.finalize_with_timeout("sip_dial_failed")
                ctx.shutdown("sip_dial_failed")
                return

            sip_participant = await ctx.wait_for_participant(
                kind=rtc.ParticipantKind.PARTICIPANT_KIND_SIP
            )
            attrs = dict(sip_participant.attributes or {})
            # Outbound, both attributes are our own request echoed back
            # (protocol/rpc/sip.go), so `our_did` is what we dialled out as.
            peer = _normalize_sip_e164(str(attrs.get("sip.phoneNumber") or "") or None, did=our_did)
            trunk_num = _normalize_sip_e164(
                str(attrs.get("sip.trunkPhoneNumber") or "") or None, did=our_did
            )
            to_e164 = peer or to_e164
            from_e164 = trunk_num or from_e164
            call_id = str(attrs.get("sip.callID") or attrs.get("sip.callId") or "").strip() or None
            # What the dial turned out to be, carried into the promote rather
            # than written by an UPDATE just ahead of it: `promote_running` is a
            # full upsert of this same row and COALESCEs every one of these.
            await run.promote_running(
                from_e164=from_e164,
                to_e164=to_e164,
                livekit_room=room_name,
                idempotency_key=call_id,
            )
            wire_sip_disconnect_safety(ctx, run, sip_identity=sip_participant.identity)
            # Outbound: our own number is `from_e164` — the DID we dialled out
            # as. A bridged transfer dials the human from the same number.
            transfer_ctx.attach(run, sip_identity=sip_participant.identity, from_e164=from_e164)
        else:
            # Inbound: wait for caller, bind conversation (handoff-aware), then prepare.
            sip_participant = await ctx.wait_for_participant(
                kind=rtc.ParticipantKind.PARTICIPANT_KIND_SIP
            )
            attrs = dict(sip_participant.attributes or {})
            # Inbound, `sip.phoneNumber` is the caller straight off the
            # carrier's INVITE — the one number on a SIP call we do not already
            # hold — and is read against OUR DID's country.
            peer = _normalize_sip_e164(str(attrs.get("sip.phoneNumber") or "") or None, did=our_did)
            trunk_num = _normalize_sip_e164(
                str(attrs.get("sip.trunkPhoneNumber") or "") or None,
                fallback=did_meta or from_e164,
                did=our_did,
            )
            from_e164 = peer or from_e164
            to_e164 = trunk_num or to_e164
            call_id = str(attrs.get("sip.callID") or attrs.get("sip.callId") or "").strip() or None

            if call_id:
                existing = await pool.fetchrow(
                    """
                    SELECT id FROM sessions
                    WHERE tenant_id = $1 AND idempotency_key = $2
                    LIMIT 1
                    """,
                    tenant.id,
                    call_id,
                )
                if existing and str(existing["id"]) != session_id:
                    logger.warning(
                        "duplicate sip.callID %s already session %s; shutting down",
                        call_id,
                        existing["id"],
                    )
                    ctx.shutdown("sip_duplicate_call_failed")
                    return

            if not from_e164:
                # We could not read the caller's number: they withheld it, the
                # leg did not come from the PSTN with one, or their carrier sent
                # a national format we cannot place. Every conversation belongs
                # to exactly one identity, so there is nobody to file this call
                # against — and answering anyway is the WORSE outcome we are
                # leaving behind: it used to run, cost money, and store no
                # transcript, no analysis and no inbox row, because
                # `persist_conversation_item` returns early without a
                # conversation. Refuse at the door and leave a visible row.
                await persistence.record_refused_call(
                    tenant,
                    session_id=session_id,
                    type_="SIP_INBOUND",
                    agent_id=str(agent_id),
                    close_reason=UNIDENTIFIED_CALLER_CLOSE_REASON,
                    phone_number_id=str(phone_number_id) if phone_number_id else None,
                    telephony_account_id=(
                        str(telephony_account_id) if telephony_account_id else None
                    ),
                    livekit_room=room_name,
                    to_e164=to_e164,
                    idempotency_key=call_id,
                )
                _abort(
                    UNIDENTIFIED_CALLER_CLOSE_REASON,
                    "inbound caller number unreadable",
                    did_e164=to_e164,
                )
                return

            if phone_number_id and to_e164:
                try:
                    conversation = (
                        inbound_conversation
                        if inbound_conversation is not None
                        else await _inbound_conversation_spec(
                            pool=pool,
                            tenant_id=tenant.id,
                            agent_id=UUID(str(agent_id)),
                            version=version,
                        )
                    )
                    (
                        conversation_id,
                        conversation_ref_id,
                        resolved_agent_id,
                        resolved_version,
                    ) = await _bind_inbound_conversation(
                        pool=pool,
                        tenant_id=tenant.id,
                        fallback_agent_id=UUID(str(agent_id)),
                        phone_number_id=UUID(str(phone_number_id)),
                        did_e164=to_e164,
                        peer_e164=from_e164,
                        context=conversation.context,
                    )
                except Exception as exc:
                    logger.exception("inbound SIP conversation bind failed")
                    _abort("conversation_bind_failed", str(exc))
                    return
                run_agent_id = str(resolved_agent_id)
                if resolved_version is not None:
                    version = resolved_version
                elif resolved_agent_id != UUID(str(agent_id)):
                    row = await pool.fetchrow(
                        """
                        SELECT published_version FROM agents
                        WHERE id = $1 AND tenant_id = $2
                        """,
                        resolved_agent_id,
                        tenant.id,
                    )
                    if not row or row["published_version"] is None:
                        _abort(
                            "unpublished_agent_failed",
                            "resolved conversation agent has no published version",
                        )
                        return
                    version = int(row["published_version"])
                ctx.log_context_fields = {
                    **ctx.log_context_fields,
                    "agent": run_agent_id,
                    "version": version,
                    "conversation_id": conversation_id,
                }

            try:
                run = await VoiceRun.prepare(
                    VoiceSessionSpec(
                        tenant=tenant,
                        session_id=session_id,
                        agent_id=run_agent_id,
                        agent_version=version,
                        session_type=session_type,
                        conversation_id=conversation_id,
                        initial_userdata=initial_userdata,
                        allowed_channels=("voice",),
                        phone_number_id=str(phone_number_id) if phone_number_id else None,
                        telephony_account_id=(
                            str(telephony_account_id) if telephony_account_id else None
                        ),
                        livekit_room=room_name,
                        from_e164=from_e164,
                        to_e164=to_e164,
                        conversation_ref_id=conversation_ref_id,
                        idempotency_key=call_id,
                    ),
                    ctx.proc.userdata["vad"],
                    session_status="running",
                    runtime_extra={
                        RUNTIME_KEY_TRANSFER: transfer_ctx.perform,
                        # The caller's number and our DID, exactly as the session
                        # row records them: inbound reads both off the
                        # participant's attributes above, before anything is
                        # compiled, so the prompt and the greeting both see them.
                        # `from_e164` is guaranteed non-empty here — a caller we
                        # cannot identify was turned away further up.
                        RUNTIME_KEY_CALL_FIELDS: build_call_fields(
                            direction="inbound", human_e164=from_e164, agent_e164=to_e164
                        ),
                        RUNTIME_KEY_SEND_DTMF: dtmf_sender,
                    },
                )
            except RuntimeError as exc:
                logger.exception("sip inbound prepare failed")
                _abort("prepare_failed", str(exc))
                return
            run_holder.append(run)
            wire_call_lifecycle(ctx, run)
            run.emit_session_started()

            wire_sip_disconnect_safety(ctx, run, sip_identity=sip_participant.identity)
            # Inbound: our own number is `to_e164` — the DID that was called.
            # `from_e164` here is the caller's, and dialling a transfer out as
            # the caller's own number is spoofing that carriers reject.
            transfer_ctx.attach(run, sip_identity=sip_participant.identity, from_e164=to_e164)

        assert run is not None
        await start_call(ctx, run, sip_participant)
    except Exception as exc:
        logger.exception("sip session failed; finalizing session %s", session_id)
        if run is not None:
            await run.finalize_with_timeout("agent_start_failed", error=exc)
        else:
            await fail_unprepared_call(tenant, session_id, exc)
        ctx.shutdown("agent_start_failed")
