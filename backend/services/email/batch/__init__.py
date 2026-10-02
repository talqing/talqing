"""Email outbound batches: upload a list, draft it, review it, send what you picked.

Four modules, split by who writes what:

- ``models`` — the API surface and the three stored rows, plus the one function
  that decides whether a row is sendable.
- ``service`` — everything a human does to a BATCH. Every one of those is a
  write to the policy row, or one guarded move over rows a person named.
- ``sends`` — everything a human does to ONE SEND: create it, reschedule it,
  pause it, resume it, cancel it. Its own policy row, with its own lifetime.
- ``draft`` — the `email.batch.draft` job: one bounded pass, then a decision.

The sending pass is deliberately not here. It is the twin of ``draft`` and lives
in ``services.email.send``.
"""

from __future__ import annotations

from .draft import run_draft_job  # noqa: F401
from .sends import (  # noqa: F401
    cancel_send,
    create_send,
    get_send,
    list_sends,
    load_send_run,
    patch_send,
    pause_send,
    resume_send,
    send_response,
)
from .service import (  # noqa: F401
    act_on_recipients,
    add_recipients,
    batch_response,
    batches_blocking_task_delete,
    batches_broken_by_task_config,
    cancel_batch,
    create_batch,
    delete_batch,
    effective_status,
    get_batch,
    list_batches,
    list_recipients,
    load_batch,
    patch_batch,
    patch_recipient,
    pause_batch,
    redraft_recipients,
    resume_batch,
    verified_senders,
)

__all__ = [
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
    "load_batch",
    "load_send_run",
    "patch_batch",
    "patch_recipient",
    "patch_send",
    "pause_batch",
    "pause_send",
    "redraft_recipients",
    "resume_batch",
    "resume_send",
    "run_draft_job",
    "send_response",
    "verified_senders",
]
