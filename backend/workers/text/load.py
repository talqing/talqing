"""Load inbound text-turn inputs from the tenant DB."""

from __future__ import annotations

from uuid import UUID

import db
from services import attachments
from services.messaging import TextTurnJob
from services.user import Tenant
from workers.text.planes import plane_for_job
from workers.text.types import TextTurnInput


async def resolve_platform_bind_id(tenant: Tenant, job: TextTurnJob) -> UUID:
    """Subject resource for platform planes: ``conversation_refs.bind_id``.

    AgentCoPilot → agents.id; ToolCoPilot → tools.id;
    TaskCoPilot → agent_tasks.id.
    Requires ``job.conversation_ref_id`` (set at enqueue).
    """
    if job.conversation_ref_id is None:
        raise RuntimeError("platform text job requires conversation_ref_id")
    pool = await db.tenant_pool(tenant)
    bind_id = await pool.fetchval(
        """
        SELECT bind_id
        FROM conversation_refs
        WHERE id = $1 AND tenant_id = $2
        """,
        job.conversation_ref_id,
        tenant.id,
    )
    if bind_id is None:
        raise RuntimeError("platform conversation ref not found or missing bind_id")
    return bind_id


async def load_text_turn(tenant: Tenant, job: TextTurnJob) -> TextTurnInput | None:
    """Load inbound if it is still the latest user message, unanswered.

    `turn_status = 'running'` is what makes a re-published job safe: a turn that
    already finished — whatever it produced, including nothing — is not run a
    second time.
    """
    plane = plane_for_job(job)
    pool = await db.tenant_pool(tenant)
    row = await pool.fetchrow(
        """
        SELECT ci.text, ci.attachments, ci.created_at, ci.provider_message_id
        FROM conversation_items ci
        WHERE ci.id = $1 AND ci.tenant_id = $2 AND ci.conversation_id = $3
            AND ci.direction = 'inbound' AND ci.role = 'user'
            AND ci.turn_status = 'running'
            AND NOT EXISTS (
                SELECT 1
                FROM conversation_items newer
                WHERE newer.tenant_id = ci.tenant_id
                    AND newer.conversation_id = ci.conversation_id
                    AND newer.direction = 'inbound'
                    AND newer.role = 'user'
                    AND (newer.created_at, newer.id) > (ci.created_at, ci.id)
            )
            AND NOT EXISTS (
                SELECT 1
                FROM conversation_items output
                WHERE output.tenant_id = ci.tenant_id
                    AND output.conversation_id = ci.conversation_id
                    AND output.trigger_item_id = ci.id
                    AND output.type = 'message'
            )
        """,
        job.input_item_id,
        tenant.id,
        job.conversation_id,
    )
    if not row:
        return None
    images = tuple(attachments.stored(row["attachments"]))
    text = row["text"] if isinstance(row["text"], str) else ""
    if not text.strip() and not images:
        raise RuntimeError("text input item must contain text or an image")
    subject_id: UUID | None = None
    if plane.name != "tenant":
        subject_id = await resolve_platform_bind_id(tenant, job)
    return TextTurnInput(
        job=job,
        tenant=tenant,
        plane=plane,
        text=text,
        images=images,
        created_at=row["created_at"],
        subject_id=subject_id,
        provider_message_id=row["provider_message_id"],
    )
