"""Plane-aware SSE fan-out for text-turn lifecycle.

Every plane (tenant product and the CoPilots) publishes the same logical turn
event:

    event: turn
    data:  TurnPayload {turn_id, status: running|done|error|canceled, ...}

Redis channel keys differ by plane (conversation vs the id of what a CoPilot
edits); the event name and status vocabulary do not.

Every one of them is also written to the database first — see
``publish_turn_status`` for why the order is load-bearing.

Tenant product also streams token deltas on ``assistant.*`` — those are
orthogonal to turn lifecycle and stay conversation-bus only.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from uuid import UUID

from services import conversations, copilot
from services.messaging import TextTurnJob, TurnPayload, TurnStatus, record_turn_status
from services.user import Tenant
from workers.text.load import resolve_platform_bind_id
from workers.text.planes import plane_for_job
from workers.text.types import TextTurnInput

TurnStatusPublisher = Callable[..., Awaitable[None]]


async def publish_turn_status(
    tenant: Tenant,
    job: TextTurnJob,
    *,
    status: TurnStatus,
    subject_id: UUID | None = None,
    session_id: str | None = None,
    superseded_by_item_id: UUID | None = None,
    error: str | None = None,
    item_ids: list[str] | None = None,
) -> None:
    """Record where the turn has got to, then publish it on the plane's SSE bus.

    That order is the whole reason a client cannot get stuck. A reconnecting
    stream SUBSCRIBEs and only then reads its snapshot (``api.core.sse``), so
    every terminal status lands on one side or the other of the client's gap:
    published after it subscribed, or already in the database when it read.
    Publishing first opens a window in between where a client sees neither, and
    Redis pub/sub never replays — which is exactly how a rate-limited CoPilot
    turn left a rail spinning with a finished turn behind it.
    """
    await record_turn_status(
        tenant,
        turn_id=job.input_item_id,
        status=status,
        error=error,
    )

    event = TurnPayload(
        turn_id=job.input_item_id,
        status=status,
        session_id=session_id,
        superseded_by_item_id=superseded_by_item_id,
        error=error,
        item_ids=item_ids,
    )

    plane = plane_for_job(job)
    if plane.name == "tenant":
        await conversations.publish(job.tenant_id, job.conversation_id, event)
        return

    if plane.copilot is None:
        raise RuntimeError(f"text plane {plane.name} has no turn bus")
    key = str(subject_id if subject_id is not None else await resolve_platform_bind_id(tenant, job))
    await copilot.publish(plane.copilot, key, event)


def make_status_publisher(input: TextTurnInput) -> TurnStatusPublisher:
    """Build an on_status callback that publishes the shared ``turn`` SSE event."""

    async def on_status(
        status: TurnStatus,
        *,
        session_id: str | None = None,
        superseded_by_item_id: UUID | None = None,
        error: str | None = None,
        item_ids: list[str] | None = None,
    ) -> None:
        await publish_turn_status(
            input.tenant,
            input.job,
            status=status,
            subject_id=input.subject_id,
            session_id=session_id,
            superseded_by_item_id=superseded_by_item_id,
            error=error,
            item_ids=item_ids,
        )

    return on_status
