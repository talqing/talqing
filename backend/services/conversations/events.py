"""Ephemeral cross-process events for persistent conversation SSE streams.

Turns run on the text-worker; the SSE connection is on an API replica. Redis
pub/sub bridges them. Canonical items remain in PostgreSQL — a dropped event
resyncs from the DB snapshot on reconnect.
"""

from __future__ import annotations

import json
import logging
from uuid import UUID

from iredis.client import get_redis as get_redis
from services.conversations.models import ConversationEvent

logger = logging.getLogger("talqing.conversation_events")

_CHANNEL = "talqing:conversation:{tenant_id}:{conversation_id}"


def channel(tenant_id: UUID, conversation_id: UUID) -> str:
    return _CHANNEL.format(tenant_id=tenant_id, conversation_id=conversation_id)


async def publish(tenant_id: UUID, conversation_id: UUID, event: ConversationEvent) -> None:
    """Publish a best-effort live event; canonical items remain in PostgreSQL.

    Takes the event model rather than a name and a dict: the model names itself
    (`event`), so there is no second argument to get out of step with the body.
    """
    try:
        await get_redis().publish(
            channel(tenant_id, conversation_id),
            json.dumps(event.model_dump(mode="json"), default=str),
        )
    except Exception:
        logger.warning(
            "conversation event dropped for %s",
            conversation_id,
            exc_info=True,
        )
