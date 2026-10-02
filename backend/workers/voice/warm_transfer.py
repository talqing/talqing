"""Warm transfer — hold the caller, brief the person answering, then merge them.

The mechanism only. What the caller is *told* when this fails belongs to
`workers/voice/transfer.py`, which maps the `WarmResult` below onto plain
English; this module deliberately never composes a caller-facing sentence.

Everyone shares **one room** — the caller's — from the first ring. What changes
is who can hear whom:

    briefing                       ┌───────────────────────────────────┐
                                   │ caller (SIP)   ←── hold music     │
                                   │ our agent      ─── hold music     │
                                   │ briefing agent ←─→ them (SIP)     │
                                   └───────────────────────────────────┘
    merged, one `UpdateSubscriptions` later
                                   ┌───────────────────────────────────┐
                                   │ caller (SIP)   ←─→ them (SIP)     │
                                   └───────────────────────────────────┘
    our session shuts down; the janitor in transfer.py hangs up the survivor.

**There is no consult room, and no `MoveParticipant`.** The documented warm
transfer — LiveKit's guide and its own `WarmTransferTask` — briefs in a second
room and then moves the participant across. That API does not exist outside
LiveKit Cloud: `livekit/pkg/service/roommanager.go::MoveParticipant` returns
`errors.New("not implemented")`, added by a commit titled *"Stub MoveParticipant
so that cloud can include the latest protocol"*. Verified against a live call —
`twirp error unknown: not implemented, status=500` — so the prebuilt task would
fail here in exactly the same place.

What is implemented is `UpdateSubscriptions`, and it is enough: put both legs in
one room and the merge becomes a subscription change rather than a move. It also
costs nothing the move would not have — nobody is re-dialled, no second carrier
leg is billed, and neither party hears a gap.

**Why we run the flow ourselves rather than using the prebuilt task**, beyond
the fact that it cannot work here. Reading it (`livekit/agents/beta/workflows/
warm_transfer.py`, 1.6.8) turned up four more things we would have had to reach
around, and reaching around them costs about as much code as owning the flow:

1. `_dial_human_agent` starts the briefing `AgentSession` *before* dialling, and
   nothing closes it when the dial fails — which is the ordinary path, nobody
   answers. A second session with live speech-to-text then sits in an empty room
   for the rest of the call, and metering it (which we do) turns that into a
   charge.
2. Its consult dial carries no `max_call_duration`. Every other leg we place
   does.
3. Its four failure modes are distinguishable only by matching the text of a
   `ToolError`. Here they are three tools of our own and one dial exception.
4. It publishes its own hold-music track named `background_audio` — the same
   name our ambient bed uses — and `BackgroundAudioPlayer.aclose()` resolves the
   publication *by name*, so the two players can unpublish each other. Parking
   the caller is `workers/voice/hold.py` instead, shared with the blind bridge
   so both transports sound the same.

Owning it also means the briefing session is ours, so metering (`VoiceRun.
briefing_sessions`) and the compliance record (`transfer.briefing`) are ordinary
reads rather than hooks into somebody else's object.

**The briefing leaves a trace on purpose.** In warm mode our AI talks *about the
caller* to a third party, on a leg with no recording and no transcript of its
own. That would otherwise be the one part of the call nobody could ever review,
so it is written to the session timeline as a `transfer.briefing` event. It is
deliberately NOT written to `conversation_items`: that table is replayed
unfiltered into the next call's chat context
(`workers/session/transcript.py::load_conversation_chat_context`), so the agent
would read its own briefing back as if the caller had said it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass

from livekit import api as lk_api
from livekit import rtc
from livekit.agents import Agent, AgentSession, JobContext, llm
from livekit.agents.llm import function_tool
from livekit.agents.voice.room_io import RoomOptions

from services import session_events
from services.telephony import livekit_sip
from services.telephony.providers.base import OutboundInlineConfig
from settings import get_settings
from workers.voice import hold
from workers.voice.runtime import VoiceRun

logger = logging.getLogger("talqing.workers.voice.warm_transfer")

# How the consult leg ended. `transfer.py` owns the translation into something a
# caller can hear; these are only ever logged and stored.
WARM_CONNECTED = "connected"
WARM_DIAL_FAILED = "dial_failed"  # the carrier refused, and said why
WARM_NO_ANSWER = "no_answer"  # the ring window elapsed with no carrier verdict
WARM_DECLINED = "declined"  # they answered and said no
WARM_VOICEMAIL = "voicemail"  # we reached a greeting, not a person
WARM_CONSULT_CLOSED = "consult_closed"  # they hung up during the briefing
WARM_CALLER_GONE = "caller_gone"  # the caller hung up while on hold

# How long the briefing itself may run once they have answered. The operation's
# `ringing_timeout` bounds the dial and nothing after it, so without this a
# colleague who answers and then wanders off — or a model that never calls one of
# the three tools — leaves the caller in hold music with no way back.
#
# The real ceiling is **the SIP service's `media_timeout` (120s)**: a
# held caller says nothing, a carrier doing silence suppression then sends no
# RTP, and livekit-sip tears their leg down. Measured on Vobiz — 23 packets in
# 26 seconds while we were still playing hold music at them.
#
# That clock starts when hold does, so what has to fit under it is
# **`ringing_timeout` + this**, not this alone. 30s (the default ring) + 90s
# lands exactly on 120s, which is why a builder who raises `ringing_timeout`
# toward its 120s maximum eats the margin. Coming off hold early is a recoverable
# disappointment; losing the caller is not, so this is the number that gives.
BRIEFING_TIMEOUT = 90.0

# The briefing agent's identity in the consult room. The person we dial keeps the
# identity the blind bridge uses (`transfer-<e164>`), so a merged room looks the
# same to the janitor and reads the same in the logs whichever bridge produced it.
_BRIEFING_IDENTITY = "briefing-agent"


@dataclass(frozen=True, slots=True)
class WarmResult:
    """What the consult leg did. Mechanism, not wording.

    `decline_reason` is the operator's own free text. It is recorded for whoever
    reviews the call and is **never** handed to the caller-facing LLM: it is
    unvetted speech from a third party, and the caller hears a fixed line
    instead.
    """

    kind: str
    sip_code: int | None = None
    sip_phrase: str | None = None
    decline_reason: str | None = None


@dataclass(frozen=True, slots=True)
class _Decision:
    kind: str
    reason: str | None = None


class _BriefingAgent(Agent):
    """The agent that talks to the person we dialled, and to nobody else.

    Its three tools are the whole of the warm-transfer protocol, which is why
    they are ours rather than string-matched out of somebody else's error: each
    one is a different thing to tell the caller afterwards.
    """

    def __init__(self, *, instructions: str, decided: asyncio.Future[_Decision]) -> None:
        super().__init__(instructions=instructions)
        self._decided = decided

    def _decide(self, decision: _Decision) -> None:
        # First answer wins. A model that calls two of these in one turn, or
        # calls one after the caller has already hung up, must not overwrite an
        # outcome the orchestrator is already acting on.
        if not self._decided.done():
            self._decided.set_result(decision)

    @function_tool
    async def connect_to_caller(self) -> None:
        """Put the caller through, ending your call with this person.

        Only after they have agreed to take the caller, in words. Greeting you,
        going quiet, asking a question or talking over you is not agreement — ask
        "shall I put them through?" and wait instead."""
        self._decide(_Decision(WARM_CONNECTED))

    @function_tool
    async def decline_transfer(self, reason: str) -> None:
        """Abandon this transfer because the person you are briefing will not take
        the call. The caller stays with you and you can offer them something else.

        Args:
            reason: Their own short explanation of why they cannot take it.
        """
        self._decide(_Decision(WARM_DECLINED, reason=reason))

    @function_tool
    async def voicemail_detected(self) -> None:
        """Abandon this transfer because this is a voicemail greeting rather than a
        person. Call it only after you have actually heard the greeting."""
        self._decide(_Decision(WARM_VOICEMAIL))


_PERSONA = """\
You are a voice assistant that has been talking to a caller, and you are now on a
separate line to a colleague you want to hand that caller over to. The caller is
on hold and cannot hear any of this.

# Open before they say anything

They have just picked up a ringing phone to silence and will hang up within
seconds if nobody is there. Do not wait for them to speak. Your first words carry
three things:

  who you are · that someone is holding · the ask

For example: "Hi, this is the assistant on the support line — I have a caller
holding who has asked to speak to a person. Can you take them?" Use your own
words, keep it to one sentence, and never open with filler like "Hi, it's me."

# Then brief them

- **Say why they are being transferred first.** That is the whole point of this
  call. The caller asked for a person — lead with that and with what prompted
  it, before anything else.
- Then only what they need in order to take over: what the caller wants, what
  you have already tried, and anything you have promised.
- A few sentences. Not a recital of the conversation. Answer whatever they ask.

# Ending it

- `connect_to_caller` — **only once they have agreed, in words.** "Hello",
  silence, a question, or them talking over you is NOT agreement. If you are not
  certain, ask "shall I put them through?" and wait for the answer. Putting a
  caller through to somebody who never accepted them is worse than not
  transferring at all — the caller was promised a briefed human. Say a short
  line such as "Putting them through now" immediately before you call it.
- `decline_transfer` — they have said they cannot take it. Pass on their reason.
- `voicemail_detected` — you reached a recorded greeting rather than a person.
  Do not leave a message.

Never speak as though the caller can hear you, and never invent detail that is
not in the conversation below."""


def _briefing_instructions(chat_ctx: llm.ChatContext, *, prior_call_messages: int = 0) -> str:
    """The briefing prompt, with the call so far rendered into it.

    Rendered into the instructions rather than passed as `chat_ctx`, because the
    briefing is a *different* conversation: replaying the caller's turns as the
    briefing session's own history would leave the model addressing whoever
    answers as if they were the caller.

    ``prior_call_messages`` is how many of the leading messages came from
    EARLIER calls, which an agent set to continue one conversation across every
    call seeds into its context. They go under their own heading: the failure
    mode this prevents is attribution — a fact from three weeks ago briefed to a
    human as something the caller said a moment ago. The call's own markers
    cannot do this job, because they are `system` messages and only
    `user`/`assistant` ones are rendered here.
    """

    def render(messages: list[llm.ChatMessage]) -> list[str]:
        lines: list[str] = []
        for msg in messages:
            if msg.role not in ("user", "assistant"):
                continue
            text = (msg.text_content or "").strip()
            if not text:
                continue
            lines.append(f"{'Caller' if msg.role == 'user' else 'You'}: {text}")
        return lines

    spoken = [m for m in chat_ctx.messages() if m.role in ("user", "assistant")]
    earlier, current = spoken[:prior_call_messages], spoken[prior_call_messages:]

    sections = [_PERSONA]
    if earlier:
        sections.append(
            "# Earlier calls\n\n"
            "Said on PREVIOUS calls with this caller, not on this one. Background only - "
            "never repeat it as something they have just told you.\n\n" + "\n".join(render(earlier))
        )
    sections.append(
        "# The call so far\n\n"
        + ("\n".join(render(current)) or "(the caller had not said anything yet)")
    )
    return "\n\n".join(sections)


class WarmTransfer:
    """One warm transfer attempt, from hold to merge.

    Built and run by `workers/voice/transfer.py`; nothing else should construct
    one. `run()` leaves the caller exactly where it found them unless it returns
    `WARM_CONNECTED` — off hold, with their ambient bed back — so a declined or
    unanswered transfer is recoverable rather than a call that has quietly
    changed shape.
    """

    def __init__(
        self,
        *,
        job: JobContext,
        run: VoiceRun,
        sip_identity: str,
        destination_e164: str,
        from_e164: str,
        inline: OutboundInlineConfig,
        ringing_timeout: float,
    ) -> None:
        self._job = job
        self._run = run
        self._sip_identity = sip_identity
        self._destination = destination_e164
        self._from_e164 = from_e164
        self._inline = inline
        self._ringing_timeout = ringing_timeout

        self._caller_room = job.room
        self._human_identity = f"transfer-{destination_e164}"
        # The caller session's own agent — the one publishing hold music, which
        # the person we dial must not hear under the briefing.
        self._agent_identity = job.room.local_participant.identity

        self._consult_room: rtc.Room | None = None
        self._briefing: AgentSession | None = None
        self._caller_left_handler: Callable[[rtc.RemoteParticipant], None] | None = None
        # The briefing conversation, buffered here and written to the session
        # timeline once, when the attempt is over.
        self._items: list[dict[str, str]] = []

    # ── the whole attempt ───────────────────────────────────────────────────

    async def run(self) -> WarmResult:
        held = hold.CallerHold(self._job, self._run)
        await held.begin()
        logger.info("warm transfer: caller on hold", extra={"destination": self._destination})
        # The default covers a raised exception, which is the same situation as a
        # briefing that ended without a decision: the caller is on hold, nobody
        # is coming, and they have to be given back to the agent.
        result = WarmResult(WARM_CONSULT_CLOSED)
        try:
            result = await self._consult()
        finally:
            await self._teardown(merged=result.kind == WARM_CONNECTED)
            # Not restored on success — the session is about to be shut down and
            # the two humans own the room — nor when the caller has already left,
            # where re-enabling audio on a closing session only logs warnings.
            await held.end(restore=result.kind not in (WARM_CONNECTED, WARM_CALLER_GONE))
            self._record_briefing(result)
        return result

    # ── the consult leg ─────────────────────────────────────────────────────

    async def _consult(self) -> WarmResult:
        settings = get_settings()
        decided: asyncio.Future[_Decision] = asyncio.get_running_loop().create_future()

        token = (
            lk_api.AccessToken(settings.livekit.api_key, settings.livekit.api_secret)
            .with_identity(_BRIEFING_IDENTITY)
            .with_kind("agent")
            .with_grants(
                lk_api.VideoGrants(
                    room_join=True,
                    room=self._caller_room.name,
                    can_publish=True,
                    can_subscribe=True,
                    can_update_own_metadata=True,
                )
            )
            .to_jwt()
        )
        room = rtc.Room()
        self._consult_room = room
        await room.connect(settings.livekit.url, token)

        caller_session = self._run.session
        briefing = AgentSession(
            vad=caller_session.vad,
            stt=caller_session.stt,
            llm=caller_session.llm,
            tts=caller_session.tts,
            turn_detection=caller_session.turn_detection,
        )
        self._briefing = briefing
        # Metering: this leg runs a full speech-to-text + LLM + text-to-speech
        # conversation and would otherwise be invisible, because usage is read
        # off one session at finalize. `AgentSession.usage` survives `shutdown()`
        # — the collector is only replaced in `start()` — so finalize can read it
        # long after the briefing is over.
        self._run.briefing_sessions.append(briefing)

        @briefing.on("conversation_item_added")
        def _on_item(ev: object) -> None:
            item = getattr(ev, "item", None)
            text = (getattr(item, "text_content", None) or "").strip()
            role = getattr(item, "role", None)
            if not text or role not in ("user", "assistant"):
                return
            # Relabelled rather than stored raw: on this leg "user" is the person
            # we dialled, so the LiveKit roles read backwards to anyone looking
            # at the timeline afterwards.
            self._items.append({"role": "operator" if role == "user" else "agent", "text": text})

        @briefing.on("close")
        def _on_briefing_closed(_: object) -> None:
            # `close_on_disconnect` fires this when they hang up mid-briefing.
            # Harmless after a decision — `_decide`'s first-answer-wins covers
            # the shutdown we perform ourselves.
            if not decided.done():
                decided.set_result(_Decision(WARM_CONSULT_CLOSED))

        await briefing.start(
            agent=_BriefingAgent(
                instructions=_briefing_instructions(
                    caller_session.history,
                    prior_call_messages=self._run.prior_call_messages,
                ),
                decided=decided,
            ),
            room=room,
            room_options=RoomOptions(
                participant_identity=self._human_identity,
                close_on_disconnect=True,
                # NEVER true here: this is the caller's own room, and deleting it
                # would hang up on them. Only `_teardown` decides who leaves.
                delete_room_on_close=False,
            ),
            # D7: recording covers the agent's portion of the *caller's* call.
            # This leg's record is the `transfer.briefing` timeline event.
            record=False,
        )

        # Watching starts before the dial, not after it: a caller who hangs up
        # during a thirty-second ring would otherwise leave us ringing a
        # colleague's phone for nobody, and then merging them into an empty room.
        caller_gone = self._watch_caller()
        try:
            async with self._api() as lk:
                # The briefing agent is publishing into the caller's room now, so
                # deafen the caller to it before it can say a word.
                await self._gate(lk, merged=False)

                dial = asyncio.ensure_future(self._dial())
                # A SIP participant joins the room and publishes its track a beat
                # before the phone is answered (measured: ~7s earlier), so gating
                # on the track rather than on the answer closes the window in
                # which the caller could hear them say "hello?".
                gated = asyncio.ensure_future(self._gate_when_present(lk))
                await asyncio.wait((dial, caller_gone), return_when=asyncio.FIRST_COMPLETED)
                gated.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await gated
                if caller_gone.done():
                    dial.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await dial
                    logger.info("warm transfer: the caller hung up while we were dialling")
                    return WarmResult(WARM_CALLER_GONE)
                dial_failure = dial.result()
                if dial_failure is not None:
                    return dial_failure
                # Belt and braces: if the poll above lost its race, this is the
                # point at which both legs certainly exist.
                await self._gate(lk, merged=False)

                # They have just picked up. Start talking immediately rather than
                # waiting for them to say "hello" — measured on a live call, a
                # colleague who answered to silence gave up after 22 seconds,
                # because waiting costs their "hello", the end-of-turn delay and
                # a reasoning model's first token before a single word is
                # spoken. A phone answered to dead air reads as a robocall.
                briefing.generate_reply(
                    instructions=(
                        "They have just answered. Say your opening line now, then brief them."
                    )
                )

                await asyncio.wait(
                    (decided, caller_gone),
                    timeout=BRIEFING_TIMEOUT,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if caller_gone.done():
                    logger.info("warm transfer: the caller hung up while on hold")
                    return WarmResult(WARM_CALLER_GONE)
                if not decided.done():
                    logger.warning(
                        "warm transfer: nobody decided anything in %.0fs; "
                        "taking the caller off hold",
                        BRIEFING_TIMEOUT,
                    )
                    return WarmResult(WARM_CONSULT_CLOSED)

                decision = decided.result()
                if decision.kind != WARM_CONNECTED:
                    logger.info("warm transfer: not connecting (%s)", decision.kind)
                    return WarmResult(decision.kind, decline_reason=decision.reason)

                # The merge. Both of them have been in this room all along; all
                # that changes is that they can finally hear each other.
                try:
                    await self._gate(lk, merged=True)
                except Exception:
                    # Most likely they hung up in the second between agreeing
                    # and being put through. A failure here is the transfer not
                    # happening, so say so — raising would abort the tool and
                    # cost the caller the plain-English reason.
                    logger.exception("warm transfer: the merge failed")
                    return WarmResult(WARM_CONSULT_CLOSED)
        finally:
            self._unwatch_caller()

        logger.info("warm transfer: merged", extra={"identity": self._human_identity})
        return WarmResult(WARM_CONNECTED)

    # ── who can hear whom ───────────────────────────────────────────────────

    def _api(self) -> lk_api.LiveKitAPI:
        s = get_settings()
        return lk_api.LiveKitAPI(
            url=s.livekit.url, api_key=s.livekit.api_key, api_secret=s.livekit.api_secret
        )

    async def _audio_tracks(self, lk: lk_api.LiveKitAPI) -> dict[str, list[str]]:
        res = await lk.room.list_participants(
            lk_api.ListParticipantsRequest(room=self._caller_room.name)
        )
        return {
            p.identity: [t.sid for t in p.tracks if t.type == lk_api.TrackType.AUDIO]
            for p in res.participants
        }

    async def _gate(self, lk: lk_api.LiveKitAPI, *, merged: bool) -> None:
        """Open or close the two ears that decide whether this is a briefing.

        This is what replaces `MoveParticipant`, which does not exist outside
        LiveKit Cloud (`livekit/pkg/service/roommanager.go` returns "not
        implemented"). Everyone is in the caller's room from the start and the
        merge is a subscription change, so nothing is ever re-dialled and neither
        party hears a gap.

        Server-authoritative, and it sticks: LiveKit SIP subscribes to every
        audio track it sees, but only from `OnParticipantConnected` and
        `OnTrackPublished` (`sip/pkg/sip/room.go::subscribeTo`), so once those
        have fired there is nothing left to undo this. Measured on a live call —
        a forced unsubscribe held for 15s with no re-subscribe attempt.
        """
        tracks = await self._audio_tracks(lk)
        caller = tracks.get(self._sip_identity, [])
        human = tracks.get(self._human_identity, [])
        agent = tracks.get(self._agent_identity, [])
        briefing = tracks.get(_BRIEFING_IDENTITY, [])

        async def sub(listener: str, sids: list[str], subscribe: bool) -> None:
            # The listener has to be *in the room*, not merely expected. LiveKit
            # routes this call to the node holding that participant, so naming
            # one who has not joined yet fails the whole request with
            # `503 no response from servers` rather than a not-found. The
            # pre-dial pass names the person we are about to ring, and they are
            # not there yet — which is the ordinary case, not an error.
            if listener not in tracks or not sids:
                return
            await lk.room.update_subscriptions(
                lk_api.UpdateSubscriptionsRequest(
                    room=self._caller_room.name,
                    identity=listener,
                    track_sids=sids,
                    subscribe=subscribe,
                )
            )

        if merged:
            await sub(self._sip_identity, human, True)
            await sub(self._human_identity, caller, True)
            return
        # The caller hears the agent's hold music and nothing else. The person we
        # dialled hears the briefing agent and nothing else — not the caller they
        # are being told about, and not the hold music playing at them.
        await sub(self._sip_identity, human + briefing, False)
        await sub(self._human_identity, caller + agent, False)

    async def _gate_when_present(self, lk: lk_api.LiveKitAPI) -> None:
        """Gate the moment the dialled party's track appears, and not before.

        Best effort, and it swallows its own failures on purpose: this only
        exists to shave the window in which the caller could overhear a "hello?",
        and the pass after the dial is the one that has to be right. Letting an
        exception out here would surface as the whole transfer aborting, from a
        task nobody is meaningfully awaiting.
        """
        while True:
            try:
                tracks = await self._audio_tracks(lk)
                if tracks.get(self._human_identity):
                    await self._gate(lk, merged=False)
                    return
            except Exception:
                logger.warning("warm transfer: early gate failed; retrying", exc_info=True)
            await asyncio.sleep(0.2)

    async def _dial(self) -> WarmResult | None:
        """Ring them into the caller's room. A failure, or None if they answered.

        They land in the room deafened — `_gate_when_present` is racing this and
        wins, because a SIP participant publishes its track seconds before the
        phone is picked up.
        """
        try:
            await livekit_sip.create_sip_participant(
                room_name=self._caller_room.name,
                sip_call_to=self._destination,
                sip_number=self._from_e164,
                hostname=self._inline.hostname,
                auth_username=self._inline.auth_username,
                auth_password=self._inline.auth_password,
                participant_identity=self._human_identity,
                transport=livekit_sip.sip_transport(self._inline.transport),
                wait_until_answered=True,
                # The caller is on hold music and cannot hear this leg anyway;
                # ringback would only play to the briefing agent.
                play_dialtone=False,
                ringing_timeout_seconds=self._ringing_timeout,
            )
        except lk_api.SipCallError as e:
            logger.warning("warm transfer: consult dial refused: %s", e)
            return WarmResult(WARM_DIAL_FAILED, sip_code=e.sip_status_code, sip_phrase=e.sip_status)
        except TimeoutError:
            # No carrier verdict to record, so this is its own kind rather than a
            # `dial_failed` with an invented SIP code — the raw status column must
            # only ever hold something a carrier actually said.
            logger.warning(
                "warm transfer: consult dial timed out after %.0fs", self._ringing_timeout
            )
            return WarmResult(WARM_NO_ANSWER)
        except Exception:
            logger.exception("warm transfer: consult dial failed")
            return WarmResult(WARM_DIAL_FAILED)
        return None

    # ── the caller, while all this is going on ──────────────────────────────

    def _watch_caller(self) -> asyncio.Future[None]:
        """Resolve when the caller hangs up, so hold is never an infinite wait.

        Without this the briefing runs to completion and merges somebody into an
        empty room. `wire_sip_disconnect_safety` in `sip.py` is already closing
        the caller's session by then; this is what unblocks *us* so the consult
        leg gets torn down rather than being left ringing in a room nobody owns.
        """
        gone: asyncio.Future[None] = asyncio.get_running_loop().create_future()

        def _on_left(participant: rtc.RemoteParticipant) -> None:
            if participant.identity != self._sip_identity:
                return
            if not gone.done():
                gone.set_result(None)

        self._caller_left_handler = _on_left
        self._caller_room.on("participant_disconnected", _on_left)
        return gone

    def _unwatch_caller(self) -> None:
        handler, self._caller_left_handler = self._caller_left_handler, None
        if handler is not None:
            self._caller_room.off("participant_disconnected", handler)

    # ── teardown ────────────────────────────────────────────────────────────

    async def _teardown(self, *, merged: bool) -> None:
        """Take the briefing agent out of the caller's room, on any outcome.

        **Never deletes the room** — it is the caller's, and everyone has been in
        it the whole time. That is the one difference from the consult-room shape
        this replaced, and getting it wrong hangs up on the caller.

        When the transfer did not land, the person we dialled is removed too:
        they must not be left holding an open carrier leg to an agent that has
        stopped talking. It is also what ends the leg on the path that matters
        most for cost — a briefing that reached voicemail, where the session
        would otherwise keep billing speech-to-text at a recording.

        `drain=False` because everything here is on the caller's hold clock, and
        the polite line — "putting them through now" — has already been said.
        """
        briefing, self._briefing = self._briefing, None
        if briefing is not None:
            try:
                briefing.shutdown(drain=False)
            except Exception:
                logger.exception("warm transfer: briefing session would not shut down")
        if not merged:
            try:
                async with self._api() as lk:
                    await lk.room.remove_participant(
                        lk_api.RoomParticipantIdentity(
                            room=self._caller_room.name, identity=self._human_identity
                        )
                    )
            except Exception:
                # A dial nobody answered never put them in the room, so a
                # not-found here is the ordinary case and not worth a traceback.
                logger.info("warm transfer: nobody to remove from the caller's room")
        room, self._consult_room = self._consult_room, None
        if room is not None:
            try:
                await room.disconnect()
            except Exception:
                logger.exception("warm transfer: briefing agent would not disconnect")

    def _record_briefing(self, result: WarmResult) -> None:
        """Put what our AI told a third party about this caller on the timeline.

        The only trace this leg leaves: it is not recorded (D7) and its turns are
        kept out of `conversation_items` on purpose — see the module docstring.
        """
        if not self._items and result.kind in (WARM_DIAL_FAILED, WARM_NO_ANSWER):
            # Nobody picked up, so there was no briefing to record.
            return
        self._run.events.record(
            session_events.TRANSFER_BRIEFING,
            {
                "destination": self._destination,
                "outcome": result.kind,
                # The operator's own words, kept here and nowhere else: the
                # caller is told a fixed line instead.
                "reason": result.decline_reason,
                "items": self._items,
            },
        )
