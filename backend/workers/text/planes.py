"""Text-agent planes: tenant product, and one per CoPilot.

Runtime backbone is shared (Kafka + actor + LiveKit). All planes persist to
``conversations`` / ``conversation_items`` via ``conversation_refs.kind``.
Only the tenant product has ``sessions`` rows — a chat is one — and the API
creates them; the CoPilots keep an in-process warm LiveKit window only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from services.copilot import (
    AGENT_COPILOT,
    TASK_COPILOT,
    TOOL_COPILOT,
    CopilotSubject,
)
from services.messaging import TextTurnJob

TextPlaneName = Literal["tenant", "agent_copilot", "tool_copilot", "task_copilot"]

SESSION_TYPE_TEXT = "TEXT"

TENANT_TEXT_SOURCE = "text"

# After the last turn finishes with no further inbound message, keep the warm
# window this long, then throw it away. A new message cancels the timer and
# reuses it. Nothing about the chat it serves changes when it closes.
TEXT_SESSION_IDLE_TIMEOUT_SECONDS = 60.0

# How long a SHUTDOWN gives one window to close — write its usage and close its
# LiveKit session — before giving up on that window's usage. The shutdown path
# only: an idle close and a chat's end have nobody waiting on them and must not
# inherit a deadline. It is what `text-worker`'s `stop_grace_period` is derived
# from; the windows close concurrently, so the tail is one of these.
TEXT_SESSION_SHUTDOWN_FINALIZE_SECONDS = 15.0


@dataclass(frozen=True)
class TextPlane:
    name: TextPlaneName
    # Tenant product: sessions.type. Platform never writes sessions.
    session_type: str
    # Written to conversation_items.source for agent-produced items.
    item_source: str
    # Whether this plane's windows serve a `sessions` row. On the tenant plane
    # they do — a chat — so closing one flushes its usage and settles its bill so
    # far. The CoPilot planes keep their window in memory only: nothing to
    # settle, nothing to report, nothing to retain.
    records_session: bool
    deliver_to_provider: bool
    # Default visibility for deliverable outbound assistant messages
    outbound_visibility: str
    # Set on a CoPilot plane, and the only thing that differs between them:
    # who this CoPilot is and what it edits. None on the tenant plane.
    copilot: CopilotSubject | None = None


TENANT_PLANE = TextPlane(
    name="tenant",
    session_type=SESSION_TYPE_TEXT,
    item_source=TENANT_TEXT_SOURCE,
    records_session=True,
    deliver_to_provider=True,
    outbound_visibility="customer_visible",
)


def _copilot_plane(name: TextPlaneName, subject: CopilotSubject) -> TextPlane:
    """Every CoPilot plane is the same plane but for who is speaking."""
    return TextPlane(
        name=name,
        session_type=name.upper(),  # not written to sessions
        item_source=subject.item_source,
        records_session=False,
        deliver_to_provider=False,
        outbound_visibility="internal",
        copilot=subject,
    )


AGENT_COPILOT_PLANE = _copilot_plane("agent_copilot", AGENT_COPILOT)
TOOL_COPILOT_PLANE = _copilot_plane("tool_copilot", TOOL_COPILOT)
TASK_COPILOT_PLANE = _copilot_plane("task_copilot", TASK_COPILOT)

# A CoPilot sentinel on the Kafka job routes the turn to its plane; anything
# else is a real tenant agent id.
_PLANES_BY_SENTINEL: dict[UUID, TextPlane] = {
    plane.copilot.sentinel_id: plane  # type: ignore[union-attr]
    for plane in (AGENT_COPILOT_PLANE, TOOL_COPILOT_PLANE, TASK_COPILOT_PLANE)
}


def plane_for_job(job: TextTurnJob) -> TextPlane:
    return _PLANES_BY_SENTINEL.get(job.requested_agent_id, TENANT_PLANE)
