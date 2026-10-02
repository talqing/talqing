"""Canonical talqing event types.

The full lifecycle vocabulary lives here so producers and the webhook config UI
agree on one set of strings. Some types are declared before anything emits them,
so a subscription made today keeps working once they are and the UI can
list them.
"""

from __future__ import annotations

# ── agent lifecycle (emitted by the API) ──
AGENT_CREATED = "agent.created"
AGENT_UPDATED = "agent.updated"
AGENT_PUBLISHED = "agent.published"
AGENT_DELETED = "agent.deleted"

# ── session / call lifecycle ──
SESSION_STARTED = "session.started"  # worker entrypoint, after session row creation
SESSION_QUEUED = "session.queued"  # telephony
SESSION_IN_PROGRESS = "session.in_progress"  # telephony
# Worker, after the call audio reaches storage. Carries a ready-to-use signed
# link (`url`) and its expiry, so a consumer can take its own copy without a
# round trip back to the API — short-lived on purpose, because this payload
# lands in somebody's logs. `session.completed` deliberately carries no link: the
# reconcile sweep rebuilds it hours later, where a URL that dies in an hour
# would be a broken promise.
RECORDING_READY = "recording.ready"

# The end-of-session report: transcript + analysis + cost + recording in one
# payload, dispatched once everything it describes is true. This is THE event to
# subscribe to for a CRM integration, and the only one that carries the outcome.
# It closes the pair with `session.started`, and it fires for a text chat as
# well as a call — every run this platform bills or reports ends with exactly one
# of these.
#
# One event, not two: there is deliberately no second "session.ended" carrying
# just the cost. It would be a strict subset of this on every code path —
# including the billing reconcile sweep, which builds the same payload from the
# database rather than from a worker's memory (see `webhooks.payloads`). Two
# events describing one fact is two things to keep in step.
#
# `recording.ready` survives because it is NOT a subset: it fires only when audio
# actually reached storage, which many calls skip, and it fires as soon as the
# upload lands rather than waiting on analysis.
SESSION_COMPLETED = "session.completed"  # after finalize + recording + analysis + billing

# ── batch outbound calling ──
# All three carry the same `CallBatchResponse` body a `get_call_batch` would
# return, so a consumer parses one shape whichever arrives.
BATCH_STARTED = "batch.started"  # the dispatcher began dialling this list
BATCH_COMPLETED = "batch.completed"  # every recipient reached a terminal state
# The one that matters most: a batch stopped by the circuit breaker after ten
# consecutive setup failures — an unpublished agent, an expired carrier
# credential, a trunk refusing everything. `failure_reason` says which. Shipping
# the two happy-path events without this would leave the only silent outcome the
# bad one, and a breaker firing at 3 a.m. is precisely what needs pushing.
BATCH_FAILED = "batch.failed"

# ── email outbound ──
# The first three carry the same `EmailBatchResponse` body a `get_email_batch`
# would return and are about DRAFTING; the last three carry an
# `EmailSendResponse` and are about one send. Two shapes because they are two
# things — a batch is a list being written, a send is a list going out — and a
# CRM integration cares about the second half.
EMAIL_BATCH_STARTED = "email_batch.started"  # the first drafting pass claimed a row
# The one an operator actually wants pushed to them — "your 412 drafts are ready
# to review". It is the moment the product needs them back, and it is also the
# moment the batch's own life ends: nothing about it is scheduled any more until
# a human presses send.
EMAIL_BATCH_DRAFTED = "email_batch.drafted"
# Drafting stopped for good: a deleted task, a field map an edit broke, a
# creator who left the organization. `failure_reason` says which. Deterministic
# by definition — trying again would fail the same way.
EMAIL_BATCH_FAILED = "email_batch.failed"
# Drafting STOPPED ITSELF after ten consecutive failures and can be continued.
# Its rows are untouched and `resume` carries on from where it stopped, which is
# why this is not `email_batch.failed`: a payload whose `status` reads `paused`
# arriving on a `failed` event is a lie in the one place a tenant automates
# against. A breaker firing at 3 a.m. is exactly what needs pushing.
EMAIL_BATCH_PAUSED = "email_batch.paused"

# A send now has a lifetime worth reporting: it can be scheduled for Monday,
# paced over days, paused and resumed. Sending emitted nothing at all before it
# became a row, which left the half that actually mails people invisible.
EMAIL_SEND_STARTED = "email_batch.send.started"  # this send began working through its rows
EMAIL_SEND_COMPLETED = "email_batch.send.completed"  # every row has gone or failed
# The send stopped for good on an account-level refusal: a revoked key, an
# unverified domain, an exhausted quota. Its unsent rows are back in the review
# table as drafts, so the work is recoverable once the account is — which is
# exactly what the `failure_reason` needs to reach somebody to say.
EMAIL_SEND_FAILED = "email_batch.send.failed"
# The send STOPPED ITSELF after ten consecutive provider failures. Its rows are
# still queued — nothing was lost and nothing was handed back — and `resume`
# continues from where it stopped.
EMAIL_SEND_PAUSED = "email_batch.send.paused"

# ── WhatsApp outbound ──
# All four carry the `WhatsAppBatchResponse` a `get_whatsapp_batch` returns.
WHATSAPP_BATCH_STARTED = "whatsapp_batch.started"  # the first message is going out
WHATSAPP_BATCH_COMPLETED = "whatsapp_batch.completed"  # every row has gone or failed
# Stopped by itself and resumable: a broken credential or account, a template
# WhatsApp paused, an empty BSP wallet, a messaging limit, or sends or
# deliveries failing in a row. `failure_reason` says which and what to do.
WHATSAPP_BATCH_PAUSED = "whatsapp_batch.paused"
# Stopped for good: the sender was deleted, or the batch's creator may no longer send.
WHATSAPP_BATCH_FAILED = "whatsapp_batch.failed"

# ── tool lifecycle ──
TOOL_INVOKED = "tool.invoked"
TOOL_FAILED = "tool.failed"

# ── meta ──
WEBHOOK_TEST = "webhook.test"  # the "test-fire" button

# Every type a tenant can subscribe to (the UI multi-select reads this).
ALL: list[str] = [
    AGENT_CREATED,
    AGENT_UPDATED,
    AGENT_PUBLISHED,
    AGENT_DELETED,
    SESSION_STARTED,
    SESSION_QUEUED,
    SESSION_IN_PROGRESS,
    SESSION_COMPLETED,
    RECORDING_READY,
    BATCH_STARTED,
    BATCH_COMPLETED,
    BATCH_FAILED,
    EMAIL_BATCH_STARTED,
    EMAIL_BATCH_DRAFTED,
    EMAIL_BATCH_FAILED,
    EMAIL_BATCH_PAUSED,
    EMAIL_SEND_STARTED,
    EMAIL_SEND_COMPLETED,
    EMAIL_SEND_FAILED,
    EMAIL_SEND_PAUSED,
    WHATSAPP_BATCH_STARTED,
    WHATSAPP_BATCH_COMPLETED,
    WHATSAPP_BATCH_PAUSED,
    WHATSAPP_BATCH_FAILED,
    TOOL_INVOKED,
    TOOL_FAILED,
]

# Types actually emitted today (the rest light up in their phase).
LIVE: list[str] = [
    AGENT_CREATED,
    AGENT_UPDATED,
    AGENT_PUBLISHED,
    AGENT_DELETED,
    SESSION_STARTED,
    SESSION_COMPLETED,
    RECORDING_READY,
    BATCH_STARTED,
    BATCH_COMPLETED,
    BATCH_FAILED,
    EMAIL_BATCH_STARTED,
    EMAIL_BATCH_DRAFTED,
    EMAIL_BATCH_FAILED,
    EMAIL_BATCH_PAUSED,
    EMAIL_SEND_STARTED,
    EMAIL_SEND_COMPLETED,
    EMAIL_SEND_FAILED,
    EMAIL_SEND_PAUSED,
    WHATSAPP_BATCH_STARTED,
    WHATSAPP_BATCH_COMPLETED,
    WHATSAPP_BATCH_PAUSED,
    WHATSAPP_BATCH_FAILED,
    TOOL_INVOKED,
    TOOL_FAILED,
]
