"""Session trace vocabulary — the durable `session_events` timeline.

One namespace, two readers: the worker writes these types, the call detail API
counts and renders them. Keeping the strings here means neither side can drift.

This trace answers "what did the platform do?" — provider failures and
recoveries, tool timing, connection quality, lifecycle. It deliberately does
NOT carry turns, transcripts or per-message metrics: `conversation_items`
already owns those in a richer form (LiveKit's full `MetricsReport` per
message), and a second copy would be a second truth.

Distinct from `services/webhooks/events.py`, which is the *outbound* vocabulary
a tenant subscribes to. A few strings coincide because they name the same
moment; that is intentional, not a shared constant.
"""

from __future__ import annotations

import json

# ── lifecycle ──
SESSION_STARTED = "session.started"  # session row is live and the agent is compiled
AGENT_READY = "agent.ready"  # greeting/on_enter has run; the agent can respond
# What this call started knowing about earlier calls with the same person:
# {context, summaries, history_items, userdata_initialized}. It lives here
# rather than on the session row because everything else about the setup is
# already one join away (`sessions.agent_version_id` → `agent_versions.config`),
# and copying agent fields into `sessions` to answer a debugging question ends
# with all of them copied. The one fact no config can reconstruct afterwards is
# that `context = summary` found NO summaries — re-running that query later
# returns today's answer, not the one from call time.
CONVERSATION_CONTEXT_LOADED = "conversation.context_loaded"
SESSION_ENDED = "session.ended"  # carries the close reason
SESSION_ERROR = "session.error"  # AgentSession `error` event (LLM/STT/TTS/realtime)

# ── provider health (only emitted when a fallback is configured) ──
# LiveKit's Fallback{STT,LLM,TTS}Adapter flips availability rather than
# reporting individual attempts, so these are transitions, not per-request rows.
PROVIDER_FAILED = "provider.failed"  # primary went unavailable → traffic moved to fallback
PROVIDER_RECOVERED = "provider.recovered"  # provider passed its recovery probe

# ── tools ──
TOOL_STARTED = "tool.started"
TOOL_ENDED = "tool.ended"  # carries duration_ms and whether it errored
# What became of the spoken reply that carries a background tool's result back
# to the caller: `scheduled`, then `completed` / `interrupted` / `skipped`.
# Without it a background tool's result is only half traceable — `tool.ended`
# says what the tool returned, and nothing says whether the agent ever passed it
# on. `skipped` is the model choosing silence because it judged the result
# already covered, which is the failure mode this event exists to make visible.
# One reply may cover several calls, so the payload carries a list.
TOOL_REPLY = "tool.reply"
# One turn ran out of LLM -> tools -> LLM rounds (the agent's `max_steps`).
# LiveKit's only reaction is a log line in our own process: it makes the model
# answer with `tool_choice="none"`, so the caller hears a confident reply
# written WITHOUT the tool it was about to call, and every other surface —
# transcript, webhooks, cost — looks like an ordinary turn. This is the sole
# record that the answer was truncated rather than finished. `limit` is null
# on a realtime call, where LiveKit counts the rounds and never checks them, so
# the event reports depth instead of truncation.
TOOL_STEP_LIMIT_REACHED = "tool.step_limit_reached"

# ── call bounds ──
# The caller went quiet and the agent checked they were still there
# (`workers/voice/call_bounds.py`). {attempt, of}. The check-in itself is in the
# transcript as an ordinary agent line; this is what says why it was spoken. The
# hang-up that may follow is the close reason `silence_timeout`.
CALLER_CHECK_IN = "caller.check_in"

# ── hold ──
# The caller parked on hold music while a bridged transfer dials somebody else
# (`workers/voice/hold.py`). `supports_refer` is False on every carrier we
# support, so every transfer is a bridge and every transferred caller hears this
# — which makes hold time a normal part of a transferred call's duration, its
# recording and its bill. Without these two the silence is indistinguishable
# from conversation on every surface that reads the call.
HOLD_STARTED = "hold.started"
HOLD_ENDED = "hold.ended"  # carries duration_ms and whether the caller came back

# ── recording ──
# The caller asked, mid-call, not to be recorded, and `stop_recording` discarded
# the audio. `recording_status = consent_withdrawn` already records *that* it
# happened; this records *when*, which is the one thing a compliance reader
# needs and the session row cannot hold. The payload's `at` is authoritative:
# the row is written at finalize, the moment it describes is earlier.
RECORDING_CONSENT_WITHDRAWN = "recording.consent_withdrawn"
# A handoff took the call to an agent with recording off, and back. The stretch
# in between is written to the file as silence, so without these two a listener
# meets an unexplained gap and reads it as a bug. Both carry the agent that
# caused it.
RECORDING_PAUSED = "recording.paused"
RECORDING_RESUMED = "recording.resumed"
# A handoff reached an agent that asks to be recorded on a call where nothing
# is: `AgentSession.start(record=…)` is read once, from the agent that ANSWERED,
# so with recording off there no recorder exists and there is nothing to resume.
# Publish warns about the edge; this is the only trace on a call where it
# actually bit, and without it the answer to "why is there no recording?" is a
# `recording.state` of `none` that names the wrong agent.
RECORDING_UNAVAILABLE = "recording.unavailable"

# ── screen share ──
# The person on a web call started, or stopped, sharing their screen with the
# agent (`workers/voice/screenshare.py`). Without these two, "the agent kept
# saying it could not see" is unanswerable after the fact — the frames
# themselves are never stored, so this pair is the only record that a screen was
# ever there. `started` carries the publication's dimensions and codec — the
# codec because vp9/av1 make the browser trade away the spatial detail this
# feature rests on, so it is the first thing to check when an agent misreads a
# screen. `stopped` carries `duration_ms`, matching `hold.ended`, plus the
# frames delivered, which is where "this client published at 15 fps" shows up.
SCREENSHARE_STARTED = "screenshare.started"
SCREENSHARE_STOPPED = "screenshare.stopped"

# ── handoffs ──
# What the NEXT AGENT started with, and who decided it. The sibling of
# `conversation.context_loaded`, which answers the same question for a new call:
# the one fact no config can reconstruct afterwards is how much actually
# crossed, because an edge asking for five turns on a call that has had two
# crossed two. Carries the summary itself for the reason `transfer.briefing`
# does — it is prose our AI wrote ABOUT the conversation rather than in it, and
# for a `handoff` OPERATION it exists nowhere else at all.
AGENT_HANDOFF = "agent.handoff"

# ── transfers ──
# Warm transfer only: what our AI told the person answering, about this caller,
# on a leg that has no recording and no transcript of its own. It lives here
# rather than in `conversation_items` because that table is replayed unfiltered
# into the next call's chat context — the agent would read its own briefing back
# as if the caller had said it. This is the only answer to "what did we say about
# this customer to a third party?", so it is a compliance record, not telemetry.
TRANSFER_BRIEFING = "transfer.briefing"

# ── audio input ──
# The ai-coustics enhancer catches its own errors (a rejected license key, an
# unsupported stream) by disabling itself and passing the raw audio through, so
# the call survives and nothing else would report it. Recorded at finalize, once
# per session, not per frame.
NOISE_CANCELLATION_FAILED = "noise_cancellation.failed"

# ── speech behaviour ──
# Only fires when the agent config leaves `resume_false_interruption` on; the
# avatar path force-disables it (see `compiler/compile.py`), so a video call
# legitimately never records one.
AGENT_FALSE_INTERRUPTION = "agent.false_interruption"  # agent stopped for a non-utterance

# ── media streams ──
#
# A stream call's failure modes are invisible everywhere else, and the partner's
# own spec warns that *they* cannot see some of them either — a handshake we
# accept too slowly is a dropped call with no event on either side. Every one of
# these is written by the WORKER, not by the gateway: `session_events.seq` comes
# from an in-process counter and the table has exactly one writer per session, so
# what the gateway learns reaches the worker as dispatch metadata (before the
# call) or as a data packet on `talqing.stream` (during it). See
# `services/streams/signals.py`.
#
# `stream.connected` carries the negotiated shape of the call: the dialect, the
# codec and rate the platform announced, their own call id, and which metadata
# keys became `{{vars.*}}`. That last one is the answer to "why is
# {{vars.flowName}} empty?", which has no other trace at all.
STREAM_CONNECTED = "stream.connected"
# The partner reported a failure of their own, with their code and message
# verbatim — they are diagnostic text rather than a stable contract, so they are
# never mapped.
STREAM_ERROR = "stream.error"
# The partner said the call is over, in THEIR vocabulary (`caller_hangup`,
# `stasis_end`, `callended`). Its own row rather than folded into
# `session.ended`, which finalize already writes once with OUR close reason:
# two rows saying the same thing in two vocabularies is how a timeline stops
# being readable, and the partner's string is the one a support ticket quotes.
STREAM_ENDED = "stream.ended"
# The partner acknowledged a barge-in `clear`. Only some platforms answer one,
# so its absence is not a fault.
STREAM_CLEARED = "stream.cleared"
# Ours, and reported by no protocol: the gateway fell further behind real time,
# so the caller heard silence where the agent was speaking. `behind_ms` is the
# total drift and a second row means it grew again. The symptom at the caller's
# end is clipped speech with clean logs on both sides — the partner's channel
# drops what it cannot take without an error — which is exactly why it needs a
# row of its own, and why it is the number a concurrency test watches.
STREAM_UNDERRUN = "stream.underrun"
# Ours: caller audio reached the gateway late — a burst after a network stall —
# and `dropped_ms` of it was discarded instead of being delivered behind real
# time. The answer to "the agent missed what the caller said" on a stream call.
STREAM_INBOUND_DROPPED = "stream.inbound_dropped"

# ── keypad (DTMF) ──
#
# Out-of-band both ways: we receive RFC 4733 / SIP INFO events and platform DTMF
# events, and never detect tones in the audio. One consequence worth knowing —
# digits entered on a keypad are absent from the call recording by construction,
# because they never entered the audio path.
DTMF_RECEIVED = "dtmf.received"
DTMF_SENT = "dtmf.sent"

# ── transport (room paths only) ──
RTC_QUALITY = "telemetry.rtc.quality"
PARTICIPANT_JOINED = "livekit.participant.joined"
PARTICIPANT_LEFT = "livekit.participant.left"

# Worst-first, so "the worst quality this call saw" is a max() over ranks.
CONNECTION_QUALITY_RANK: dict[str, int] = {
    "excellent": 0,
    "good": 1,
    "poor": 2,
    "lost": 3,
}

# A provider error body can be a whole HTML page from a proxy. Long enough to
# hold a real JSON error, short enough that the trace stays readable.
_ERROR_BODY_MAX_CHARS = 800


def error_cause(error: BaseException | None) -> dict[str, object] | None:
    """The `cause` payload on a `session.error`: what the provider actually said.

    Lives here rather than in either worker because both write this event and
    the readers are shared — a voice call and a text chat must not describe
    the same provider failure differently.

    Every `*Error` model on LiveKit's `ErrorEvent` (`LLMError`, `STTError`,
    `TTSError`, `RealtimeModelError`, `InterruptionDetectionError`) declares
    ``error: Exception = Field(..., exclude=True)``, so `model_dump()` keeps the
    plugin label and drops the message, the HTTP status and the response body.
    This event is the only record a provider failure leaves on a call, so
    without this a tenant reads "Raya/m1 raised an error" and has nowhere else
    to look.

    `APIStatusError` and its siblings (`livekit.agents._exceptions`) carry the
    detail as named attributes; anything else is a plugin's own exception, where
    only `str()` is guaranteed.
    """
    if error is None:
        return None
    # `error.message` on an `APIError`, `str()` on anything else. NOT `str()`
    # throughout: `APIStatusError.__str__` composes the status, retryability,
    # request id and body into one line, so using it here would restate every
    # field below inside this one and leave the reader picking the message back
    # out of a blob.
    message = getattr(error, "message", None)
    cause: dict[str, object] = {
        "type": type(error).__name__,
        "message": (message if isinstance(message, str) else str(error)) or None,
    }
    for attribute in ("status_code", "request_id", "retryable"):
        value = getattr(error, attribute, None)
        # -1 is `APIStatusError`'s "no status" sentinel, not a status code.
        if value is not None and value != -1:
            cause[attribute] = value
    body = getattr(error, "body", None)
    if body is not None:
        text = body if isinstance(body, str) else json.dumps(body, default=str)
        cause["body"] = text[:_ERROR_BODY_MAX_CHARS]
    return cause


ALL: list[str] = [
    SESSION_STARTED,
    AGENT_READY,
    CONVERSATION_CONTEXT_LOADED,
    SESSION_ENDED,
    SESSION_ERROR,
    PROVIDER_FAILED,
    PROVIDER_RECOVERED,
    TOOL_STARTED,
    TOOL_ENDED,
    TOOL_REPLY,
    CALLER_CHECK_IN,
    HOLD_STARTED,
    HOLD_ENDED,
    RECORDING_CONSENT_WITHDRAWN,
    RECORDING_PAUSED,
    RECORDING_RESUMED,
    RECORDING_UNAVAILABLE,
    AGENT_HANDOFF,
    TRANSFER_BRIEFING,
    NOISE_CANCELLATION_FAILED,
    AGENT_FALSE_INTERRUPTION,
    RTC_QUALITY,
    PARTICIPANT_JOINED,
    PARTICIPANT_LEFT,
    STREAM_CONNECTED,
    STREAM_ERROR,
    STREAM_ENDED,
    STREAM_CLEARED,
    STREAM_UNDERRUN,
    STREAM_INBOUND_DROPPED,
    DTMF_RECEIVED,
    DTMF_SENT,
]

# ── retention ──
# A `session.purge` DELETES this session's rows. It does not redact them.
#
# **Why deletion rather than a keep-list.** Nothing that outlives a purge reads
# this table: an invoice is built from the five usage tables and the cost
# columns on `sessions`, and `services.observability` deliberately never fans
# out over it. The only readers are the call and conversation detail pages —
# where, on a purged call, the transcript, the recording and the analysis panels
# all already say the content was deleted. So a surviving trace buys a health
# band and an event log describing a conversation nobody can read.
#
# Against that: every redaction scheme has to be re-audited whenever anyone adds
# a key. A type-level keep-list stops a NEW EVENT TYPE leaking, and nothing
# stops a new KEY on a type already on the list — put `arguments` on
# `tool.ended` one afternoon and it survives every purge from then on, silently.
# That is a slow fuse to carry for a diagnostic with no consumer.
#
# Deleting also removes a subtler failure. Blanking payloads left
# `session_snapshot._tool_facts` seeing every `tool.started` and no `tool.ended`,
# so a purged call's health band announced "3 tool calls never finished" about
# three tools that finished fine. Erasing somebody's data must not leave the
# platform asserting things about the call that did not happen, and no rows is
# the only version of that with nothing left to get wrong.
#
# The one exception below is not an oversight and is not about telemetry.
SURVIVES_PURGE: frozenset[str] = frozenset(
    {
        # The caller asked not to be recorded, and when. `recording_status`
        # already carries *that* it happened; this is the only record of the
        # moment. It is evidence the content was handled correctly, which is
        # exactly what a reviewer asks for AFTER the content is gone — so
        # deleting the consent trail along with the content would destroy the
        # proof that the deletion was the right thing to do. Its payload is one
        # ISO timestamp: no transcript, no identity, nothing derived from either.
        RECORDING_CONSENT_WITHDRAWN,
    }
)
