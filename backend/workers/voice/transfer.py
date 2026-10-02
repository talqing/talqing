"""Handing a live PSTN call to a human.

A `transfer` operation in a tool tree ends with the caller talking to a person
and our AgentSession closed, so nothing bills STT/LLM/TTS for a conversation
that stopped being ours.

The builder chooses **cold** (handed straight over) or **warm** (held while the
agent briefs the person answering). Which *transport* runs underneath is the
platform's choice, not theirs:

- **REFER** — cold, on `supports_refer` carriers (Plivo, Twilio, Vobiz). We ask
  the carrier to re-point the caller's leg. The caller leaves the LiveKit room,
  the room empties, and LiveKit reaps it. Nothing else to clean up.
- **Blind bridge** — cold, everywhere else. We dial the human into the caller's
  own room and leave.
- **Consult bridge** — warm, on every carrier, because no REFER shape lets our
  agent speak to the person answering first. `workers/voice/warm_transfer.py`
  owns it; it ends in the same room shape as the blind bridge.

Both bridges leave two SIP participants in one room, who hear each other with no
agent present (`sip/pkg/sip/room.go::participantJoin`) — but **nothing hangs up
the survivor when the first of them leaves** (`participantLeft` only logs, and
`empty_timeout` does not apply to a room with one participant in it). So the
bridge paths keep the *job* alive as a janitor after the *session* has closed,
and delete the room on the first departure. Without it a transferred caller
leaves an open carrier leg listening to silence until `max_call_duration` —
three hours, on the tenant's carrier invoice.

The one thing this module must never do is delete the room *at* transfer time:
that hangs up on both humans. Delete it when the first of them leaves, and not
before.

Two audiences, two strings, and they must not be conflated. `TransferOutcome.
detail` is handed to the LLM and reaches the caller, so it is plain English
about the person being called. `TransferOutcome.sip_status` is the raw carrier
verdict and goes to `sessions.transfer`, where an operator debugging a trunk
will see it. A model handed "403 Forbidden" will eventually tell a caller their
transfer was *refused* — which is both untrue and alarming when the fault was
our own trunk configuration.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from livekit import api as lk_api
from livekit import rtc
from livekit.agents import JobContext

import db
from services.secrets import load_secrets
from services.telephony import livekit_sip
from services.telephony.catalog import get_provider_spec
from services.telephony.models import TELEPHONY_ACCOUNT_COLUMNS, TelephonyAccount
from services.telephony.providers import get_adapter
from services.telephony.providers.base import OutboundInlineConfig
from settings import get_settings
from utils.bg import spawn
from workers.session import persistence
from workers.voice import hold, warm_transfer
from workers.voice.runtime import VoiceRun

logger = logging.getLogger("talqing.workers.voice.transfer")

# The close reason a transferred call lands on. Deliberately not `*_failed` and
# not in `billing.FAILED_CLOSE_REASONS`: a transfer is a completed call that is
# owed the platform fee and worth analysing.
CLOSE_REASON_TRANSFERRED = "transferred"

# `VoiceRun.transfer_state`. The SIP disconnect handler and the session-close
# handler in `workers/voice/sip.py` both read these, so the values are defined
# here rather than invented at each site.
STATE_IN_FLIGHT = "in_flight"  # the REFER or the bridge dial is under way
STATE_REFERRED = "referred"  # the carrier owns the caller now
STATE_BRIDGED = "bridged"  # the janitor below owns the job's lifetime

TRANSPORT_REFER = "refer"
TRANSPORT_BRIDGE = "bridge"

MODE_COLD = "cold"
MODE_WARM = "warm"

# What the caller is told. Never a SIP code — a model handed `"486"` will read
# it aloud — and never a fault of ours dressed up as a fact about the human
# being called.
_BUSY = "the line was busy"
_NO_ANSWER = "nobody answered"
_UNREACHABLE = "that number could not be reached"
# Generic on purpose. 401/403/407 mean *our* trunk is not authorised to
# transfer, which is a configuration fault; 5xx is the carrier having a bad
# day. Neither is the caller's business, and neither is the destination's fault.
_GENERIC = "the transfer could not be completed"

# Warm transfer's own endings, in the same register as the SIP table below: what
# happened to the person we called, never what we think of them. "Declined" in
# particular is not the caller's business — and the operator's own words for why
# are unvetted third-party speech, so they go to the session timeline
# (`transfer.briefing`) and never into the caller's ear.
_WARM_DETAIL: dict[str, str] = {
    warm_transfer.WARM_NO_ANSWER: _NO_ANSWER,
    warm_transfer.WARM_DECLINED: "they aren't able to take the call right now",
    warm_transfer.WARM_VOICEMAIL: "the call went through to their voicemail",
}

_SIP_STATUS_DETAIL: dict[int, str] = {
    486: _BUSY,  # Busy Here
    600: _BUSY,  # Busy Everywhere
    408: _NO_ANSWER,  # Request Timeout
    480: _NO_ANSWER,  # Temporarily Unavailable
    404: _UNREACHABLE,  # Not Found
    410: _UNREACHABLE,  # Gone
    484: _UNREACHABLE,  # Address Incomplete
    604: _UNREACHABLE,  # Does Not Exist Anywhere
}


@dataclass(frozen=True, slots=True)
class TransferOutcome:
    """What a transfer attempt did. Read structurally by `compiler/operations.py`
    — the compiler must not import the worker, which is the whole reason the
    transfer is an injected callable rather than a direct call."""

    ok: bool
    transport: str  # refer | bridge | none (a call that cannot be transferred at all)
    # Plain English for the LLM, and through it the caller. None on success.
    detail: str | None = None
    # The raw carrier verdict, `{"code": 486, "phrase": "Busy Here"}`, for
    # `sessions.transfer` and whoever debugs the trunk. Never spoken.
    sip_status: dict[str, Any] | None = None


def _failure(transport: str, code: int | None, phrase: str | None) -> TransferOutcome:
    return TransferOutcome(
        ok=False,
        transport=transport,
        detail=_SIP_STATUS_DETAIL.get(code or 0, _GENERIC),
        sip_status=None if code is None else {"code": code, "phrase": phrase},
    )


class TransferContext:
    """The `transfer` operation's window onto one SIP call.

    Built in `workers/voice/sip.py` **before** the session is compiled, because
    `compiler/tools.py::_make_dispatcher` copies the runtime context into each
    tool at build time — a key added afterwards would never reach a tool. The
    two facts that are not known that early, the `VoiceRun` and the caller's
    participant identity, arrive through `attach`.

    Only SIP sessions build one. On a web/room or text run the runtime key is
    simply absent, which is what makes `_transfer` refuse those cleanly.
    """

    def __init__(self, job: JobContext) -> None:
        self.job = job
        self.run: VoiceRun | None = None
        self.sip_identity: str | None = None
        # OUR number, which is not the same spec field in both directions:
        # inbound it is `to_e164` (the DID that was called), outbound it is
        # `from_e164`. The bridge dials out as this, so getting it from the
        # wrong end would present the caller's own number as the caller ID.
        self.from_e164: str | None = None

    def attach(self, run: VoiceRun, *, sip_identity: str, from_e164: str | None) -> None:
        self.run = run
        self.sip_identity = sip_identity
        self.from_e164 = from_e164

    # ── the injected callable ───────────────────────────────────────────────

    async def perform(
        self,
        *,
        mode: str,
        destination_e164: str,
        ringing_timeout: float,
        # No default: the record this ends up in is a claim about what the
        # caller experienced, and a caller that forgot to say would silently
        # publish "the call carried on with the agent" about a call that hung up.
        on_failure: str,
    ) -> TransferOutcome:
        """Transfer this call to `destination_e164`. Never deletes the room.

        Deleting it at transfer time would hang up on both humans; the janitor
        below deletes it when the first of them leaves, and not before.
        """
        if mode not in (MODE_COLD, MODE_WARM):
            # Publish validation rejects anything else, so this only fires for a
            # tool version published against a future schema.
            raise RuntimeError(f"transfer mode {mode!r} is not available on this platform")
        run = self.run
        if run is None or not self.sip_identity:
            raise RuntimeError("transfer is unavailable: this call has no SIP caller attached")

        account = await self._account(run)
        # Warm always bridges: there is no REFER shape in which our agent gets to
        # talk to the person answering first. The carrier's REFER capability
        # decides nothing here, which is why it is only consulted for cold.
        transport = (
            TRANSPORT_BRIDGE
            if mode == MODE_WARM or not get_provider_spec(account.provider).supports_refer
            else TRANSPORT_REFER
        )
        logger.info(
            "transfer starting",
            extra={
                "transport": transport,
                "provider": account.provider,
                "destination": destination_e164,
            },
        )

        # Set before either transport runs: a caller who drops during the
        # handshake did not fail the call, the transfer simply overtook them.
        run.transfer_state = STATE_IN_FLIGHT
        try:
            if mode == MODE_WARM:
                outcome = await self._warm(run, account, destination_e164, ringing_timeout)
            elif transport == TRANSPORT_REFER:
                # EXPERIMENT (revert freely): name the trunk this call is
                # actually on rather than always the outbound one. Plivo refuses
                # a REFER on an inbound call with 405 while the same code works
                # outbound, and Vobiz does the reverse — and neither carrier
                # documents which trunk the `Refer-To` host must name.
                direction = "inbound" if run.spec.session_type == "SIP_INBOUND" else "outbound"
                outcome = await self._refer(account, destination_e164, ringing_timeout, direction)
            else:
                outcome = await self._bridge(run, account, destination_e164, ringing_timeout)
        except Exception:
            # A fault of ours, not a fact about the person being called — so it
            # is logged loudly and recorded on the session below, but the caller
            # still gets the plain-English apology. Re-raising instead would
            # abort the tool, leave `sessions.transfer` null (the call then reads
            # as short and unexplained) and hand the model no wording at all.
            logger.exception("transfer failed unexpectedly")
            run.transfer_state = None
            outcome = TransferOutcome(ok=False, transport=transport, detail=_GENERIC)

        await self._record(
            run,
            mode=mode,
            destination=destination_e164,
            outcome=outcome,
            on_failure=on_failure,
        )

        if not outcome.ok:
            # The caller is still with the agent and the call may run for
            # another ten minutes before they hang up normally. Leaving the flag
            # set would file that ending as a transfer.
            run.transfer_state = None
            return outcome

        if transport == TRANSPORT_REFER:
            # The carrier owns the caller now. Whoever notices them leave —
            # RoomIO or `wire_sip_disconnect_safety` — closes the session, and
            # naming the reason here is what stops it reading as a failed call.
            run.transfer_state = STATE_REFERRED
            run.set_close_reason(CLOSE_REASON_TRANSFERRED)
        else:
            await self._hand_over_to_janitor(run)
        return outcome

    # ── transports ──────────────────────────────────────────────────────────

    async def _refer(
        self,
        account: TelephonyAccount,
        destination_e164: str,
        ringing_timeout: float,
        direction: str,
    ) -> TransferOutcome:
        """Ask the carrier to re-point the caller's leg (SIP REFER).

        The `Refer-To` URI is the adapter's to build: Plivo blocks `tel:` and any
        host but its own trunk domain, Twilio accepts either — so both are given
        the same `sip:` shape and there is one code path here.
        """
        uri = get_adapter(account.provider).refer_uri(
            account, e164=destination_e164, direction=direction
        )
        assert self.sip_identity is not None  # checked in `perform`
        try:
            await livekit_sip.transfer_sip_participant(
                room_name=self.job.room.name,
                participant_identity=self.sip_identity,
                transfer_to=uri,
                ringing_timeout_seconds=ringing_timeout,
            )
        except lk_api.SipCallError as e:
            # Not an error path so much as a caller-facing feature: a failed
            # REFER leaves the caller on our line (Plivo answers `NOTIFY 4xx`),
            # which is exactly what `on_failure: continue` assumes.
            logger.warning("transfer REFER refused: %s", e)
            return _failure(TRANSPORT_REFER, e.sip_status_code, e.sip_status)
        except TimeoutError:
            logger.warning("transfer REFER timed out after %.0fs", ringing_timeout)
            return TransferOutcome(ok=False, transport=TRANSPORT_REFER, detail=_NO_ANSWER)
        except Exception:
            logger.exception("transfer REFER failed")
            return TransferOutcome(ok=False, transport=TRANSPORT_REFER, detail=_GENERIC)
        return TransferOutcome(ok=True, transport=TRANSPORT_REFER)

    async def _bridge(
        self,
        run: VoiceRun,
        account: TelephonyAccount,
        destination_e164: str,
        ringing_timeout: float,
    ) -> TransferOutcome:
        """Dial the human into the caller's own room and wait for them to answer.

        The caller goes on hold for the length of the ring — `workers/voice/
        hold.py`, the same parking the warm consult uses. `play_dialtone=False`
        follows from that: LiveKit SIP's dialtone is a synthesised ETSI ringback
        (`sip/pkg/sip/outbound.go`), and mixing it under hold music would be two
        sounds competing on one leg.

        The human sees our DID rather than the caller's number — an outbound leg
        from our own trunk has to present a number we own. It cannot be fixed
        per-transfer: caller ID for a transfer is trunk-level on every carrier.
        """
        from_e164, inline = await self._outbound_trunk(run, account)
        held = hold.CallerHold(self.job, run)
        await held.begin()
        try:
            await livekit_sip.create_sip_participant(
                room_name=self.job.room.name,
                sip_call_to=destination_e164,
                sip_number=from_e164,
                hostname=inline.hostname,
                auth_username=inline.auth_username,
                auth_password=inline.auth_password,
                participant_identity=f"transfer-{destination_e164}",
                transport=livekit_sip.sip_transport(inline.transport),
                wait_until_answered=True,
                play_dialtone=False,
                ringing_timeout_seconds=ringing_timeout,
            )
        except lk_api.SipCallError as e:
            logger.warning("transfer bridge dial refused: %s", e)
            await held.end(restore=True)
            return _failure(TRANSPORT_BRIDGE, e.sip_status_code, e.sip_status)
        except TimeoutError:
            # A caller cannot tell which transport ran and must not be able to
            # infer it from how the failure is worded: an unanswered dial is
            # "nobody answered" on both paths.
            logger.warning("transfer bridge dial timed out after %.0fs", ringing_timeout)
            await held.end(restore=True)
            return TransferOutcome(ok=False, transport=TRANSPORT_BRIDGE, detail=_NO_ANSWER)
        except Exception:
            logger.exception("transfer bridge dial failed")
            await held.end(restore=True)
            return TransferOutcome(ok=False, transport=TRANSPORT_BRIDGE, detail=_GENERIC)
        # Before the janitor takes over, not after: the two humans are in the
        # room from this moment and must not hear hold music over each other.
        await held.end(restore=False)
        return TransferOutcome(ok=True, transport=TRANSPORT_BRIDGE)

    async def _warm(
        self,
        run: VoiceRun,
        account: TelephonyAccount,
        destination_e164: str,
        ringing_timeout: float,
    ) -> TransferOutcome:
        """Hold the caller, brief the person answering, then merge the two.

        The mechanism is `workers/voice/warm_transfer.py`; this method is only
        the translation from what happened into what the caller is told, which
        is the one thing that must not leak out of that module. A merged room has
        the same two-SIP-participants shape the blind bridge produces, so success
        hands over to the same janitor.
        """
        from_e164, inline = await self._outbound_trunk(run, account)
        assert self.sip_identity is not None  # checked in `perform`
        result = await warm_transfer.WarmTransfer(
            job=self.job,
            run=run,
            sip_identity=self.sip_identity,
            destination_e164=destination_e164,
            from_e164=from_e164,
            inline=inline,
            ringing_timeout=ringing_timeout,
        ).run()

        if result.kind == warm_transfer.WARM_CONNECTED:
            return TransferOutcome(ok=True, transport=TRANSPORT_BRIDGE)
        if result.kind == warm_transfer.WARM_DIAL_FAILED:
            # Same table as cold, deliberately: a caller cannot tell which mode
            # ran and must not be able to infer it from how a busy line is worded.
            return _failure(TRANSPORT_BRIDGE, result.sip_code, result.sip_phrase)
        return TransferOutcome(
            ok=False,
            transport=TRANSPORT_BRIDGE,
            # `consult_closed` and `caller_gone` fall through to the generic
            # apology on purpose. Both are our side of the call coming apart —
            # the same rule as an authorisation failure on our own trunk, where
            # blaming the destination for our fault is both untrue and alarming.
            detail=_WARM_DETAIL.get(result.kind, _GENERIC),
        )

    async def _outbound_trunk(
        self, run: VoiceRun, account: TelephonyAccount
    ) -> tuple[str, OutboundInlineConfig]:
        """Our number and the trunk to dial out on, for both bridge shapes.

        `outbound_inline_config` is the adapter method the ordinary outbound-call
        path already uses; trunk credentials are never re-derived here.
        """
        if not self.from_e164:
            raise RuntimeError("transfer bridge has no outbound caller ID for this call")
        secrets = await load_secrets(run.spec.tenant)
        return self.from_e164, get_adapter(account.provider).outbound_inline_config(
            account,
            secrets,
            from_e164=self.from_e164,  # gitleaks:allow
        )

    # ── the janitor ─────────────────────────────────────────────────────────

    async def _hand_over_to_janitor(self, run: VoiceRun) -> None:
        """End the session, keep the job, and reap the room when a human leaves.

        The order is load-bearing. The flag and the room watcher go up **first**,
        because closing the session fires `close`, and the close handler in
        `sip.py` reads the flag to decide whether to shut the job down. Then the
        session closes, which is where the meter stops and where finalize
        (recording, analysis, billing, `session.completed`) is driven from.
        """
        run.transfer_state = STATE_BRIDGED
        self._watch_for_departure()
        run.set_close_reason(CLOSE_REASON_TRANSFERRED)
        run.mark_ended()
        run.session.shutdown()

    def _watch_for_departure(self) -> None:
        job = self.job
        # Set by the room handler below; the watcher task is what acts on it, so
        # there is one path to `delete_room` rather than a handler and a timer
        # racing to call it.
        departed = asyncio.Event()

        @job.room.on("participant_disconnected")
        def _on_participant_left(participant: rtc.RemoteParticipant) -> None:
            if participant.kind != rtc.ParticipantKind.PARTICIPANT_KIND_SIP:
                return
            logger.info(
                "transfer janitor: a bridged party left; hanging up the other leg",
                extra={"identity": participant.identity},
            )
            departed.set()

        cap = float(get_settings().livekit.sip.max_call_duration_seconds)

        async def _watch() -> None:
            try:
                await asyncio.wait_for(departed.wait(), timeout=cap)
            except TimeoutError:
                # A janitor that never fires must not hold a worker slot for
                # ever. Deleting the room here hangs up on both of them, which
                # is what `max_call_duration` means on every other call too.
                logger.warning(
                    "transfer janitor: bridged call reached the %.0fs cap; ending it", cap
                )
            try:
                await job.delete_room()
            except Exception:
                logger.exception("transfer janitor: delete_room failed")
            job.shutdown(reason=CLOSE_REASON_TRANSFERRED)

        spawn(_watch())
        logger.info(
            "transfer janitor armed; the job now outlives its session",
            extra={"room": job.room.name, "cap_seconds": cap},
        )

    # ── persistence ─────────────────────────────────────────────────────────

    async def _account(self, run: VoiceRun) -> TelephonyAccount:
        account_id = run.spec.telephony_account_id
        if not account_id:
            raise RuntimeError("this call has no telephony account, so it cannot be transferred")
        pool = await db.tenant_pool(run.spec.tenant)
        row = await pool.fetchrow(
            f"""
            SELECT {TELEPHONY_ACCOUNT_COLUMNS}
            FROM telephony_accounts
            WHERE id = $1::uuid AND tenant_id = $2
            """,
            account_id,
            run.spec.tenant.id,
        )
        if not row:
            raise RuntimeError(f"telephony account {account_id} is missing; cannot transfer")
        return TelephonyAccount.from_row(row)

    async def _record(
        self,
        run: VoiceRun,
        *,
        mode: str,
        destination: str,
        outcome: TransferOutcome,
        on_failure: str,
    ) -> None:
        """Put the transfer on the session row, before anything else reads it.

        `session.completed` is built by re-reading the session (see
        `services/webhooks/payloads.py`), so a fact that only ever lived in this
        worker's memory could not reach the webhook. Written before the session
        is shut down on the bridge path, so finalize sees it too.
        """
        record = {
            "mode": mode,
            "transport": outcome.transport,
            "destination": destination,
            "outcome": "connected" if outcome.ok else "failed",
            # The same plain-English line the agent was given. Stored so the call
            # detail page, the analysis prompt and `session.completed` all say why a
            # transfer failed instead of guessing "could not reach anyone" —
            # which is wrong for a person who answered and declined.
            "detail": outcome.detail,
            "sip_status": outcome.sip_status,
            # What the tool did next when this did not connect: `continue` hands
            # the caller back to the agent, `end_call` hangs up on them. Only
            # meaningful on a failure, and stored because nothing downstream can
            # work it out — the tool version that decided it is one join away and
            # a reader guessing wrong tells a tenant the opposite of what their
            # customer experienced.
            "on_failure": None if outcome.ok else on_failure,
            "at": datetime.now(UTC).isoformat(),
        }
        run.transfer = record
        await persistence.set_transfer(run.spec.tenant, run.spec.session_id, record)
