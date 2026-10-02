"""WhatsApp template campaigns: upload a CSV, review each row, send one template each.

- ``models`` — the API surface, the two stored rows, and rendering.
- ``service`` — everything a human does to a batch, and ``reconcile``, the one
  rule for whether a row is `ready`.
- ``send`` — the `whatsapp.send` job: one coroutine per batch, to the last row.
- ``status`` — receipts and replies arriving on the sender's webhook.

The channel itself — an agent answering a WhatsApp number — is an integration:
``services.integrations.providers.whatsapp``.
"""

from __future__ import annotations

from .send import run_send_job  # noqa: F401
from .service import (  # noqa: F401
    act_on_recipients,
    add_recipients,
    cancel_batch,
    create_batch,
    delete_batch,
    get_batch,
    list_batches,
    list_recipients,
    list_templates,
    patch_batch,
    patch_recipient,
    pause_batch,
    resume_batch,
    send_batch,
)
from .status import record_reply, record_status  # noqa: F401

__all__ = [
    "act_on_recipients",
    "add_recipients",
    "cancel_batch",
    "create_batch",
    "delete_batch",
    "get_batch",
    "list_batches",
    "list_recipients",
    "list_templates",
    "patch_batch",
    "patch_recipient",
    "pause_batch",
    "record_reply",
    "record_status",
    "resume_batch",
    "run_send_job",
    "send_batch",
]
