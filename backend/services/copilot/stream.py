"""Redis pub/sub for a CoPilot's live event stream.

The CoPilot text-worker path PUBLISHes timeline events (message, turn) to a
per-subject Redis channel. The SSE endpoint relays them to the browser. Keyed by
the CoPilot's kind and the id of what it edits, so an agent id and a tool id can
never land on the same channel.

Why Redis: turns run on the text-worker; the SSE connection is on an API
replica. Same pattern as services.conversations for customer text.
"""

from __future__ import annotations

import json
import logging

from iredis.client import get_redis as get_redis

from .models import CopilotEvent
from .subjects import CopilotSubject

logger = logging.getLogger("talqing.copilot")

_CHANNEL = "talqing:copilot:{kind}:{key}"


def channel(subject: CopilotSubject, key: str) -> str:
    return _CHANNEL.format(kind=subject.kind, key=key)


async def publish(subject: CopilotSubject, key: str, event: CopilotEvent) -> None:
    """Fan an event out to every open stream for `key`, across replicas. A Redis
    hiccup must never break the turn loop: a dropped event resyncs from the DB
    snapshot when the client reconnects, so we log and move on.

    Takes the event model rather than a name and a payload — the model names
    itself, so the two cannot disagree."""
    try:
        await get_redis().publish(
            channel(subject, key),
            json.dumps(event.model_dump(mode="json"), default=str),
        )
    except Exception:
        logger.warning(
            "%s: redis publish failed for %s — event dropped",
            subject.label,
            key,
            exc_info=True,
        )
