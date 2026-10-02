"""Email outbound — the batch, its two jobs, and the provider seam.

Import what you need from here::

    from services import email
    await email.create_batch(body, ctx)

Three things this package is built on, and each of them is a rule rather than a
detail:

1. **A row has one flat column space, filled from two directions.** The CSV
   supplies some columns, the task's output supplies the rest, and a human's
   edits sit on top. A row is sendable when `to`, `subject` and `body` each
   resolve to a non-empty string in that merged space, whichever direction the
   value came from — which is what makes "the CSV has emails" and "feed it a
   phone number and let the task find the email" the same feature.

2. **Generating and sending are two jobs with two lifetimes, and a human stands
   between them.** They are the same shape: one job per subject, taking a
   bounded pass and then deferring. `email.batch.draft` runs until the list is
   drafted and then stops existing; `email.send` is created when a person
   presses send and runs until that send's rows have gone. Nothing stays alive
   across the gate, and nothing between passes — no coroutine, no timer, no
   lock.

   **Which row a setting lives on decides what it steers.** The batch carries
   drafting policy and the DEFAULTS a send inherits; a send run carries its own
   copy of the sending half. So editing a batch mid-send steers the next send,
   and `patch_send` is what re-steers the one already going out.

3. **Talqing never asks a model to send an email.** Sending happens after the
   review gate, from our backend, over the provider's REST API, with an
   idempotency key and a pace. Resend's MCP server is a different, unrelated use
   of the same credential.
"""

from __future__ import annotations

from .batch import (  # noqa: F401
    act_on_recipients,
    add_recipients,
    batch_response,
    batches_blocking_task_delete,
    batches_broken_by_task_config,
    cancel_batch,
    cancel_send,
    create_batch,
    create_send,
    delete_batch,
    effective_status,
    get_batch,
    get_send,
    list_batches,
    list_recipients,
    list_sends,
    patch_batch,
    patch_recipient,
    patch_send,
    pause_batch,
    pause_send,
    redraft_recipients,
    resume_batch,
    resume_send,
    run_draft_job,
    verified_senders,
)
from .batch.models import (  # noqa: F401
    MAX_RECIPIENTS_PER_REQUEST,
    AddEmailRecipientsRequest,
    AddEmailRecipientsResponse,
    CreateEmailBatchRequest,
    CreateEmailSendRequest,
    CreateEmailSendResponse,
    EmailBatchResponse,
    EmailRecipientResponse,
    EmailSendResponse,
    PatchEmailBatchRequest,
    PatchEmailRecipientRequest,
    PatchEmailSendRequest,
    RecipientActionResponse,
    RedraftRequest,
    SelectRecipientsRequest,
    VerifiedSendersResponse,
)
from .providers import EMAIL_PROVIDERS  # noqa: F401
from .send import run_send_job  # noqa: F401

__all__ = [
    "EMAIL_PROVIDERS",
    "MAX_RECIPIENTS_PER_REQUEST",
    "AddEmailRecipientsRequest",
    "AddEmailRecipientsResponse",
    "CreateEmailBatchRequest",
    "CreateEmailSendRequest",
    "CreateEmailSendResponse",
    "EmailBatchResponse",
    "EmailRecipientResponse",
    "EmailSendResponse",
    "PatchEmailBatchRequest",
    "PatchEmailRecipientRequest",
    "PatchEmailSendRequest",
    "RecipientActionResponse",
    "RedraftRequest",
    "SelectRecipientsRequest",
    "VerifiedSendersResponse",
    "act_on_recipients",
    "add_recipients",
    "batch_response",
    "batches_blocking_task_delete",
    "batches_broken_by_task_config",
    "cancel_batch",
    "delete_batch",
    "cancel_send",
    "create_batch",
    "create_send",
    "effective_status",
    "get_batch",
    "get_send",
    "list_batches",
    "list_recipients",
    "list_sends",
    "patch_batch",
    "patch_recipient",
    "patch_send",
    "pause_batch",
    "pause_send",
    "redraft_recipients",
    "resume_batch",
    "resume_send",
    "run_draft_job",
    "run_send_job",
    "verified_senders",
]
