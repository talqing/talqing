"""Close-reason buckets — who owns a session that ended the way it did.

``sessions.close_reason`` is the most specific fact we hold about how a run
ended, and it is written from five places (``workers/voice/sip.py``,
``workers/voice/transfer.py``, ``workers/voice/room_web.py``,
``workers/text/window.py`` and ``services/billing/session.py``). This module is
the one place that says what those strings mean *to a reader*: a caller who hung
up, a trunk that broke, an agent that was never published, or our own crash —
four different owners behind what the dashboard used to draw as one grey bar.

A bucket is for grouping only; it never replaces the reason. Every surface that
calls :func:`bucket_for` keeps the raw string beside it, and a reason this map
has never heard of buckets as ``other`` and shows itself verbatim. That is
deliberate: a close reason added by a future worker must appear on the
observability page as an unlabelled row, because a default that absorbed it
would make the new failure invisible for as long as nobody updated this file.

Distinct from ``services/billing/pricing.py::FAILED_CLOSE_REASONS``, which
answers a different question — "do we charge the platform fee?" — and treats an
unknown reason as a normal completion on purpose. A reason can sit in
``platform`` here and still be billable there; the two sets are not copies.
"""

from __future__ import annotations

from typing import Literal

from services.billing.pricing import normalize_close_reason

CloseReasonBucket = Literal[
    "normal",
    "transferred",
    "caller_unreachable",
    "carrier_fault",
    "configuration",
    "platform",
    "other",
]

# Reading order on the page: what went right, then outward through the parties
# who could fix what went wrong — the caller's dial list, their carrier, their
# configuration, and finally us.
BUCKET_ORDER: tuple[CloseReasonBucket, ...] = (
    "normal",
    "transferred",
    "caller_unreachable",
    "carrier_fault",
    "configuration",
    "platform",
    "other",
)

# ── WebSocket media streams ────────────────────────────────────────────────
#
# Named rather than written inline, because the gateway (`api/stream/`) and the
# worker (`workers/voice/stream.py`) are the two processes that produce them and
# this file is the one that has to have heard of them. Every one of these must be
# bucketed and described below, or it ships as an unlabelled row.
#
# The `*_failed` suffix is load-bearing beyond this file:
# `billing/pricing.py::is_failed_close_reason` treats it as "we failed", which
# waives the platform fee. So a reason that means the CALL ended normally must
# not carry it, and a reason that means WE broke must.

# The partner's platform hung the call up — their caller_hangup, their stop,
# their socket closing. The ordinary ending of a stream call.
STREAM_PLATFORM_HANGUP = "stream_platform_hangup"
# The gateway's own 3h cap (`telephony.streams.max_call_duration_seconds`). A
# completion, not a failure: everything up to it really happened.
STREAM_MAX_DURATION = "stream_max_duration"
# The gateway left the room without saying why — its process died, or the room
# lost it. Ours.
STREAM_GATEWAY_DISCONNECTED = "stream_gateway_disconnected_failed"
# We cut the call: the gateway container was asked to stop and closed every live
# socket. There is no drain to hide behind — the gateway binds a port, so waiting
# would refuse new calls for as long as it waited, and a refused connect on this
# channel is a dropped call the partner cannot see either. So a deploy ends live
# stream calls, and this is the reason that says so instead of filing them as the
# partner hanging up and charging the platform fee for a call we ended.
STREAM_GATEWAY_RESTART = "stream_gateway_restart_failed"
# Room creation or agent dispatch failed, so no agent ever reached the call.
STREAM_DISPATCH_FAILED = "stream_dispatch_failed"
# The connection points at an agent that cannot be run.
STREAM_AGENT_UNRESOLVED = "stream_agent_unresolved_failed"
STREAM_UNPUBLISHED_AGENT = "stream_unpublished_agent_failed"
# Their framing or ours, and the message says which.
STREAM_PROTOCOL_ERROR = "stream_protocol_error_failed"
# They announced an audio format we cannot produce (Opus today). A refusal by
# name, never a silent substitution.
STREAM_UNSUPPORTED_CODEC = "stream_unsupported_codec"
# Two sockets carrying one platform call id.
STREAM_DUPLICATE_CALL = "stream_duplicate_call_failed"

# ── WhatsApp calls ─────────────────────────────────────────────────────────
#
# Written by the voice webhook (`providers/whatsapp/calls.py`) as it turns a
# call away. Every other way a WhatsApp call ends reuses the SIP vocabulary,
# because from the moment the leg reaches us it IS a SIP call.

# A call reached a number whose calls trigger is off, or whose integration is.
WHATSAPP_CALLING_OFF = "whatsapp_calling_off"
# Twilio named no caller, so there is no thread to file the call on.
WHATSAPP_CALLER_UNIDENTIFIED = "whatsapp_caller_unidentified_failed"

# ── call bounds ────────────────────────────────────────────────────────────
#
# The agent hung up on a call that had stopped being a conversation
# (`workers/voice/call_bounds.py`): the caller stayed quiet through every
# check-in, or the call reached its length limit. Completions, not failures.
SILENCE_TIMEOUT = "silence_timeout"
MAX_DURATION = "max_duration"
# An outbound call reached voicemail, and the agent left its message (if it has
# one) and hung up. The one `caller_unreachable` reason WITHOUT the `_failed`
# suffix, on purpose: the call connected and ran, so it completes and is billed
# like any other — but the person was not reached, which is what the bucket
# means, and what makes a batch dial them again.
VOICEMAIL = "voicemail"

STREAM_CLOSE_REASONS: frozenset[str] = frozenset(
    {
        STREAM_PLATFORM_HANGUP,
        STREAM_MAX_DURATION,
        STREAM_GATEWAY_DISCONNECTED,
        STREAM_GATEWAY_RESTART,
        STREAM_DISPATCH_FAILED,
        STREAM_AGENT_UNRESOLVED,
        STREAM_UNPUBLISHED_AGENT,
        STREAM_PROTOCOL_ERROR,
        STREAM_UNSUPPORTED_CODEC,
        STREAM_DUPLICATE_CALL,
    }
)


REASONS: dict[CloseReasonBucket, frozenset[str]] = {
    # Somebody chose to end it: the caller, the agent, or the tenant's own API call.
    "normal": frozenset(
        {
            "user_initiated",
            "task_completed",
            "participant_disconnected",
            "end_call",
            # A chat, ended through the API — or by a newer chat taking its place.
            "api_end",
            "replaced",
            SILENCE_TIMEOUT,
            MAX_DURATION,
            # The partner's platform ended it, or our own cap did. Both are
            # completions: the call ran, and everything up to the ending was real.
            STREAM_PLATFORM_HANGUP,
            STREAM_MAX_DURATION,
        }
    ),
    # Its own bucket rather than a kind of "normal": a handed-off call is the
    # escalation story, and folding it into completions hides the rate at which
    # the agent gives up.
    "transferred": frozenset({"transferred"}),
    # We reached the carrier and the carrier could not reach the person. The
    # four `sip_busy` / `sip_no_answer` / `sip_declined` / `sip_unallocated`
    # reasons come off the SIP response code on an outbound dial
    # (`sip_dial_close_reason` below) — before them, every one of those arrived
    # as a single `sip_dial_failed` and a busy signal was indistinguishable from
    # a broken trunk.
    "caller_unreachable": frozenset(
        {
            VOICEMAIL,
            "sip_user_unavailable_failed",
            "sip_connection_timeout_failed",
            "sip_busy_failed",
            "sip_no_answer_failed",
            "sip_declined_failed",
            "sip_unallocated_failed",
        }
    ),
    # The trunk itself misbehaved — the tenant's carrier configuration. Most of
    # these are `sip_<LiveKit DisconnectReason>_failed` from
    # `workers/voice/sip.py::_sip_disconnect_close_reason`; the last two are the
    # outbound dial path refusing before a leg ever existed.
    "carrier_fault": frozenset(
        {
            "sip_sip_trunk_failure_failed",
            "sip_media_failure_failed",
            "sip_join_failure_failed",
            "sip_signal_close_failed",
            "sip_state_mismatch_failed",
            "sip_duplicate_identity_failed",
            "sip_migration_failed",
            "sip_participant_disconnected_failed",
            "sip_caller_unidentified_failed",
            WHATSAPP_CALLER_UNIDENTIFIED,
            "sip_dispatch_failed",
            "sip_trunk_config_failed",
            "sip_dial_failed",
            # The partner's platform, which is this channel's "carrier": their
            # framing or ours, and `sessions.error` says which.
            STREAM_PROTOCOL_ERROR,
        }
    ),
    # The call arrived and there was nothing valid to run — the tenant's to fix.
    "configuration": frozenset(
        {
            "missing_published_definition",
            "unpublished_agent_failed",
            "unknown_tenant",
            "invalid_dispatch_metadata",
            "missing_dispatch_metadata",
            "invalid_sip_direction_failed",
            "sip_outbound_missing_dial_failed",
            "sip_outbound_config_failed",
            "sip_account_missing_failed",
            "prepare_failed",
            "agent_start_failed",
            "rejected",
            "avatar_start_failed",
            # The workspace ran out of credit. Here rather than in a bucket of
            # its own because a bucket is "who owns the fix", and this one is the
            # tenant's — they top up and the next call connects. A new bucket
            # would also mean repainting the observability stack, whose palette
            # is validated per stacking order.
            "insufficient_credits",
            WHATSAPP_CALLING_OFF,
            # A stream connection pointing at an agent that cannot answer, or a
            # partner configured for an audio format we do not produce. All three
            # are fixed in the workspace, not by us.
            STREAM_AGENT_UNRESOLVED,
            STREAM_UNPUBLISHED_AGENT,
            STREAM_UNSUPPORTED_CODEC,
        }
    ),
    # Ours. Every one of these is a call a tenant paid for in reputation.
    "platform": frozenset(
        {
            "error",
            "stale",
            "job_shutdown",
            "job crashed",
            "unknown",
            "avatar_disconnected",
            "conversation_bind_failed",
            "sip_duplicate_call_failed",
            "sip_server_shutdown_failed",
            "sip_agent_error_failed",
            "sip_room_deleted_failed",
            "sip_room_closed_failed",
            "sip_participant_removed_failed",
            STREAM_GATEWAY_DISCONNECTED,
            STREAM_GATEWAY_RESTART,
            STREAM_DISPATCH_FAILED,
            STREAM_DUPLICATE_CALL,
        }
    ),
    "other": frozenset(),
}

# Deliberately absent: `sip_client_initiated_failed` and `sip_user_rejected_failed`.
# LiveKit's RoomIO auto-closes CLIENT_INITIATED / ROOM_DELETED / USER_REJECTED
# (`workers/voice/sip.py::_ROOMIO_AUTO_CLOSE_DISCONNECT`) before the disconnect
# safety wire can name them, so a rejected or busy caller reaches us as a plain
# `participant_disconnected` and is indistinguishable from a normal hangup. If
# either string ever does appear, it means that assumption broke — and showing it
# raw in `other` is exactly how we would want to find out.

_BUCKET_BY_REASON: dict[str, CloseReasonBucket] = {
    reason: bucket for bucket, reasons in REASONS.items() for reason in reasons
}

# Why an outbound dial never became a call, read off the carrier's SIP response.
#
# Lives here rather than in the worker that raises, because the strings it
# produces are the strings the map above buckets — a private copy in
# `workers/voice/sip.py` would be a second vocabulary that only one of these two
# files ever got updated. `workers/voice/transfer.py::_SIP_STATUS_DETAIL` reads
# the same codes for a different audience: it turns them into a sentence the
# agent says out loud, where this turns them into a fact the database records.
_SIP_DIAL_CLOSE_REASON: dict[int, str] = {
    486: "sip_busy_failed",  # Busy Here
    600: "sip_busy_failed",  # Busy Everywhere
    408: "sip_no_answer_failed",  # Request Timeout
    480: "sip_no_answer_failed",  # Temporarily Unavailable
    603: "sip_declined_failed",  # Decline — they said no
    404: "sip_unallocated_failed",  # Not Found
    410: "sip_unallocated_failed",  # Gone
    484: "sip_unallocated_failed",  # Address Incomplete
    604: "sip_unallocated_failed",  # Does Not Exist Anywhere
}


def sip_dial_close_reason(sip_status_code: int | None) -> str:
    """Close reason for an outbound dial the carrier refused.

    A code this map has never heard of — and a failure that carried no SIP code
    at all, which is what a broken trunk or an unreachable SIP server looks like
    — stays ``sip_dial_failed`` in the ``carrier_fault`` bucket. That default is
    deliberate: an unrecognised 5xx is the carrier's problem, not the callee's,
    and putting it in ``caller_unreachable`` would tell a batch to give up on a
    perfectly good phone number.
    """
    if sip_status_code is None:
        return "sip_dial_failed"
    return _SIP_DIAL_CLOSE_REASON.get(sip_status_code, "sip_dial_failed")


def bucket_for(close_reason: str | None) -> CloseReasonBucket:
    """Which owner this reason points at, or ``other`` when it points at nobody.

    ``other`` covers two cases and hides neither: a reason no version of this
    file has mapped, and a terminal session that recorded no reason at all.
    Callers keep the raw string alongside the bucket so both read as themselves.
    """
    return _BUCKET_BY_REASON.get(normalize_close_reason(close_reason), "other")


# One plain sentence per reason, served to every surface that shows an ending.
#
# Lives beside the buckets because the two answer the same reader's question in
# two grains, off one list — a dashboard that keeps its own copy goes stale the
# first time a worker learns a new string, which is how the call detail page came
# to render the whole `sip_*_failed` family as "Sip user unavailable failed".
#
# The wording is about the CALL, not about our code: a tenant reading this is
# asking what happened to their customer, not which coroutine returned.
_DESCRIPTIONS: dict[str, str] = {
    # ── normal ──
    # LiveKit's `USER_INITIATED` names the caller of `session.shutdown()`, which
    # on this platform is only ever the `end_call` operation — so this is the
    # AGENT hanging up, not the person. Reading the name at face value and
    # calling it "ended by the caller" gets the actor exactly backwards.
    "user_initiated": "The agent ended the call",
    "task_completed": "The agent finished what it was doing and ended the call",
    "participant_disconnected": "The caller hung up",
    "end_call": "The agent ended the conversation",
    "api_end": "Ended through the API",
    "replaced": "A newer chat with this contact took its place",
    SILENCE_TIMEOUT: "The caller stayed silent, so the agent said goodbye and hung up",
    MAX_DURATION: "The call reached its time limit, so the agent said goodbye and hung up",
    # ── transferred ──
    "transferred": "The caller was handed to a human",
    # ── the caller could not be reached ──
    VOICEMAIL: "The call reached voicemail, so the agent hung up",
    "sip_user_unavailable_failed": "Nobody answered",
    "sip_connection_timeout_failed": "The call rang without ever connecting",
    "sip_busy_failed": "The line was busy",
    "sip_no_answer_failed": "The phone rang and nobody picked up",
    "sip_declined_failed": "The person declined the call",
    "sip_unallocated_failed": "That number is not in service",
    "sip_user_rejected_failed": "The call was declined",
    "sip_client_initiated_failed": "The caller's phone ended the call",
    # ── the trunk misbehaved ──
    "sip_sip_trunk_failure_failed": "The phone trunk failed",
    "sip_media_failure_failed": "The audio path broke mid-call",
    "sip_join_failure_failed": "The call could not join the room",
    "sip_signal_close_failed": "The carrier dropped its signalling connection",
    "sip_state_mismatch_failed": "The carrier and the platform disagreed about the call's state",
    "sip_duplicate_identity_failed": "Two legs claimed the same identity on this call",
    "sip_migration_failed": "The call failed while moving between servers",
    "sip_participant_disconnected_failed": "The phone leg dropped, and the carrier gave no reason",
    "sip_caller_unidentified_failed": "No caller number — the call was refused",
    WHATSAPP_CALLER_UNIDENTIFIED: "WhatsApp did not say who was calling, so the call was refused",
    "sip_dispatch_failed": "No agent could be dispatched to the call",
    "sip_trunk_config_failed": "The trunk is not configured correctly",
    "sip_dial_failed": "The outbound call could not be dialled",
    "sip_unknown_reason_failed": "The phone leg dropped for an unknown reason",
    # ── the tenant's configuration ──
    "missing_published_definition": "The agent has never been published",
    "unpublished_agent_failed": "The agent has never been published",
    "unknown_tenant": "The call did not resolve to a workspace",
    "invalid_dispatch_metadata": "The call arrived with unusable routing data",
    "missing_dispatch_metadata": "The call arrived with no routing data",
    "invalid_sip_direction_failed": "The call's direction did not match the number's setup",
    "sip_outbound_missing_dial_failed": "The outbound call had no number to dial",
    # Raised before a session exists, so it never reaches `sessions.close_reason`
    # — it is what a batch records when the agent or number it dials with stops
    # being usable mid-run (unpublished, deleted, carrier account no longer
    # ready). Here because the batch's retry and circuit-breaker rules are
    # written in these buckets, and this one has to land in `configuration`.
    "sip_outbound_config_failed": "The agent or phone number for this call is no longer usable",
    "sip_account_missing_failed": "No telephony account was attached to the number",
    "prepare_failed": "The agent could not start",
    # The step after `prepare_failed`: the agent compiled, and then died on the
    # way to its first word — most often two attached tools presenting one name
    # to the model. Everything inside that block is "the agent never got going",
    # and not quite all of it is the tenant's (a LiveKit connect failure buckets
    # here too) — a small mis-bucket in exchange for removing a large one, and
    # `sessions.error` beside it says which happened.
    "agent_start_failed": "The agent could not start - see the error below",
    "rejected": "The call was refused before the agent ran",
    "avatar_start_failed": "The avatar never joined, so the call was ended",
    "insufficient_credits": "This workspace was out of credits, so the call was not answered",
    WHATSAPP_CALLING_OFF: "Calls are turned off on this WhatsApp number, so the call was refused",
    # ── ours ──
    "error": "The call ended on an error",
    "stale": "The call was ended after the process running it stopped reporting",
    "job_shutdown": "The worker shut down mid-call",
    "job crashed": "The worker crashed",
    "unknown": "The call ended for a reason nothing recorded",
    "avatar_disconnected": "The avatar dropped out of the call",
    "conversation_bind_failed": "The call could not be attached to a conversation",
    "sip_duplicate_call_failed": "The same call arrived twice",
    "sip_server_shutdown_failed": "The media server shut down mid-call",
    "sip_agent_error_failed": "The agent errored on the phone leg",
    "sip_room_deleted_failed": "The call's room was deleted underneath it",
    "sip_room_closed_failed": "The call's room closed underneath it",
    "sip_participant_removed_failed": "The phone leg was removed from the call",
    # ── media streams ──
    STREAM_PLATFORM_HANGUP: "The call ended on your streaming partner's platform",
    STREAM_MAX_DURATION: "The call reached the three-hour limit and was ended",
    STREAM_GATEWAY_DISCONNECTED: "The media stream dropped mid-call",
    STREAM_GATEWAY_RESTART: "The call was ended by a Talqing deployment",
    STREAM_DISPATCH_FAILED: "No agent could be dispatched to the streamed call",
    STREAM_AGENT_UNRESOLVED: "The stream connection's agent could not be loaded",
    STREAM_UNPUBLISHED_AGENT: "The stream connection's agent has never been published",
    STREAM_PROTOCOL_ERROR: "The media stream broke - see the error below",
    STREAM_UNSUPPORTED_CODEC: "The partner streamed an audio format Talqing cannot play",
    STREAM_DUPLICATE_CALL: "The same streamed call arrived twice",
}


def describe(close_reason: str | None) -> str:
    """One plain sentence for how a call ended.

    A reason this file has never heard of is de-underscored and shown rather
    than swallowed, on the same principle as ``bucket_for``'s ``other``: a new
    string from a future worker must be legible on arrival, not invisible until
    somebody remembers to come back here.
    """
    reason = normalize_close_reason(close_reason)
    if not reason:
        return "No ending was recorded"
    described = _DESCRIPTIONS.get(reason)
    if described is not None:
        return described
    return reason.replace("_", " ").capitalize()
