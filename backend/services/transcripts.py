"""Reading a session's transcript back out of `conversation_items`.

This lives in `services/` rather than `workers/` because of who needs it: the
voice and text workers at finalize, but also the `session.completed` payload
builder, the analysis preview and the backfill — none of which run inside a
worker. A service importing from `workers` inverts the layering, and it is not
merely untidy: `workers.session.persistence` imports `services.agents`, which
imports `services.webhooks`, so a `services → workers.session` edge closes a
cycle that only fails for the import order the *worker* happens to use.

`conversation_items` is the sole durable timeline. Voice writes rows live via
session events; everything here only reads them back.
"""

from __future__ import annotations

import db
from services import attachments
from services.user import Tenant


async def load_session_transcript(tenant: Tenant, session_id: str) -> list[dict[str, object]]:
    """Every item in one session, oldest first.

    `data` carries the LiveKit extras for the item's type — role/text for a
    message, name/arguments for a tool call, output/is_error for its result.

    `attachments` carries the image METADATA and no link. A presigned URL in a
    stored webhook payload is a credential with an hour's life; a receiver that
    wants the bytes calls the API, which mints one for the request that asked.
    """
    pool = await db.tenant_pool(tenant)
    rows = await pool.fetch(
        """
        SELECT id, type, role, text, attachments, metrics, metadata, created_at
        FROM conversation_items
        WHERE session_id = $1 AND tenant_id = $2
        ORDER BY created_at ASC, id ASC
        """,
        session_id,
        tenant.id,
    )
    transcript: list[dict[str, object]] = []
    for row in rows:
        metadata = row["metadata"] if isinstance(row["metadata"], dict) else {}
        data = metadata.get("data") if isinstance(metadata.get("data"), dict) else {}
        payload = dict(data) if isinstance(data, dict) else {}
        if row["type"] == "message":
            payload.setdefault("role", row["role"])
            payload.setdefault("text", row["text"])
        livekit_item_id = metadata.get("livekit_item_id")
        transcript.append(
            {
                "id": str(row["id"]),
                "item_id": livekit_item_id if isinstance(livekit_item_id, str) else None,
                "type": row["type"],
                "metrics": row["metrics"],
                "attachments": [
                    item.model_dump(mode="json", exclude={"object_key"})
                    for item in attachments.stored(row["attachments"])
                ],
                "data": payload,
            }
        )
    return transcript
