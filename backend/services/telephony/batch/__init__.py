"""Batch outbound calling: upload a list, schedule it, watch it dial.

Four modules, split by who writes what:

- ``models`` — the API surface and the two stored rows.
- ``service`` — everything a human does to a batch. Every one of those is a
  write to the **policy** row and touches no work at all.
  Business hours come from ``services.scheduling.window_state``, shared with
  email outbound so there is one implementation of "may this run right now".
- ``dispatcher`` — the `call.batch.dial` job handler: one pass turning policy
  into calls, then a defer saying when to take the next one.
- ``settle`` — landing a finished call on its recipient, shared by the voice
  worker and the dispatcher's reconcile so there is one transition table.

HTTP routes import this package (``from services.telephony import batch``), not
its submodules.
"""

from __future__ import annotations

from .dispatcher import run_dial_pass  # noqa: F401
from .service import (  # noqa: F401
    add_recipients,
    cancel_batch,
    create_batch,
    delete_batch,
    effective_status,
    get_batch,
    list_batches,
    list_recipients,
    patch_batch,
    pause_batch,
    resume_batch,
)

__all__ = [
    "add_recipients",
    "cancel_batch",
    "delete_batch",
    "create_batch",
    "effective_status",
    "get_batch",
    "list_batches",
    "list_recipients",
    "patch_batch",
    "pause_batch",
    "resume_batch",
    "run_dial_pass",
]
