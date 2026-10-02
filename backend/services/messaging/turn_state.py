"""The durable half of a turn's lifecycle.

``TurnPayload`` is what a turn's status looks like on the wire; this is what it
looks like in the database. The two are written together — see
``workers.text.events.publish_turn_status`` — because a client that reconnects
mid-turn reads the row, and a client that stays connected reads the frame, and
neither one may be left believing a finished turn is still running.

The status lives on ``conversation_items`` for the item that opened the turn,
which is the same id as ``TurnPayload.turn_id``.
"""

from __future__ import annotations

import logging
from uuid import UUID

import db
from services.messaging.models import TurnStatus
from services.user import Tenant

logger = logging.getLogger("talqing.messaging.turn_state")

# An error message goes into a database column and onto a narrow rail, so a
# provider that answers a failure with an HTML page cannot be pasted in whole.
# Long enough for any real one-sentence provider error, including a URL.
_MAX_ERROR_CHARS = 1000


def turn_error_text(exc: BaseException) -> str:
    """The most specific message in an exception chain, for a human to read.

    The outermost exception is usually the least informative one: LiveKit wraps
    a provider failure in ``APIConnectionError("Connection error.")`` while the
    cause underneath carries the sentence worth showing — "Rate limit reached
    for gpt-5.6-luna … Please try again in 9.312s". Walk ``__cause__`` (explicit
    ``raise … from``, never the implicit ``__context__``, which collects
    whatever else happened to be in flight) and keep the deepest message there
    is one for.
    """
    deepest = exc
    current: BaseException | None = exc
    while current is not None:
        if str(current).strip():
            deepest = current
        current = current.__cause__
    text = str(deepest).strip() or type(deepest).__name__
    return text if len(text) <= _MAX_ERROR_CHARS else text[: _MAX_ERROR_CHARS - 1] + "…"


async def record_turn_status(
    tenant: Tenant,
    *,
    turn_id: UUID,
    status: TurnStatus,
    error: str | None = None,
) -> None:
    """Write where a turn has got to, on the item that opened it.

    Never raises, and that includes acquiring the pool: this is called from
    inside the handler for a turn that has already failed, so anything it threw
    would replace the failure the caller is in the middle of reporting with one
    about bookkeeping. A lost write costs a stale spinner until the next
    message — the thing this whole mechanism exists to avoid — so it is logged
    loudly rather than swallowed.
    """
    try:
        pool = await db.tenant_pool(tenant)
        await pool.execute(
            """
            UPDATE conversation_items
            SET turn_status = $3,
                turn_error = $4,
                updated_at = now()
            WHERE id = $1 AND tenant_id = $2
            """,
            turn_id,
            tenant.id,
            status,
            error,
        )
    except Exception:
        logger.exception("failed to record turn %s as %s", turn_id, status)
