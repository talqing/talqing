"""`session.purge` — delete one call's content, keep the call.

**This is not an audio feature.** No privacy law knows what a "recording" is:
GDPR regulates personal data, HIPAA PHI, PCI DSS cardholder data, the DPDP Act a
Data Principal's personal data — and a transcript is every one of those exactly
as much as the audio. If anything the transcript is sharper: indexed,
machine-readable, and the copy that travels into analysis, the webhook and the
LLM provider. So a purge takes the recording, the transcript, the images people
attached to it, the userdata, the analysis and the phone numbers, and leaves the
metering.

**The line is what an invoice needs, not what a debugger would like.** Anything
a bill is built from stays: the `sessions` row itself, its duration, status,
outcome and every cost column, plus the five usage tables, which hold counts and
no content. Everything else about the call goes — including the whole
`session_events` trace, which no surviving reader consults and which cannot be
redacted safely for long, because a keep-list catches a new event type and
nothing catches a new key on a type already listed.

**Redaction on `sessions`, deletion everywhere else.** The row survives with
`content_deleted_at` stamped because the metering lives on it; `conversation_items`
and `session_events` are deleted outright. Vapi draws the same line: content
goes, "operational metadata, such as call history, cost, and latency" stays.

**Idempotent from the start, not merely twice-safe.** A graceful shutdown can
hand a job back mid-work, so every write here is a set-to-a-fixed-value and
every delete tolerates an absent target. Re-running a finished purge is a
sequence of no-ops.

Both entry points are this one job: retention schedules it for `ended_at +
retention_days`, and `DELETE /v1/calls/{id}` schedules it for now. That is what
stops a 200 000-row purge arriving as 200 000 synchronous deletes.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

import db
from services import attachments, recordings, session_events, storage
from services.jobs import JobContext
from services.recordings import RecordingStatus
from services.user import Tenant

logger = logging.getLogger("talqing.retention")


async def purge_session(tenant: Tenant, args: dict[str, Any], job: JobContext) -> None:
    """Delete the content of one call. See the module docstring for the split.

    `job` is unused: this is one short pass with no claim of its own to guard.
    """
    session_id = UUID(str(args["session_id"]))
    pool = await db.tenant_pool(tenant)

    # Read the object key and the conversation BEFORE redacting: the redaction
    # clears the key, and deleting the conversation nulls the session's pointer
    # to it, so neither is recoverable afterwards.
    row = await pool.fetchrow(
        """
        SELECT s.status, s.recording_object_key, s.screenshare_recording_object_key,
               s.conversation_id, c.conversation_ref_id
        FROM sessions s
        LEFT JOIN conversations c ON c.id = s.conversation_id AND c.tenant_id = s.tenant_id
        WHERE s.id = $1 AND s.tenant_id = $2
        """,
        session_id,
        tenant.id,
    )
    if row is None:
        # The organization deleted the agent, or a previous run already took the
        # row with a cascade. Nothing to purge is a finished purge.
        logger.info("purge %s: no such session; nothing to do", session_id)
        return

    if row["status"] in ("queued", "running"):
        # A call that is still writing. Redacting it now would be undone by
        # finalize — which puts the userdata, the summary and the analysis back
        # on a row already stamped `content_deleted_at` — and the cascade below
        # would take the conversation out from under a live call. `delete_call`
        # refuses this at the API, and retention schedules from `ended_at`, so
        # reaching here means a hand-edited row or a future kind that scans;
        # raise, so the job retries with backoff and succeeds once the call ends
        # rather than silently deleting nothing.
        raise RuntimeError(
            f"session {session_id} is still {row['status']}; not purging a live call"
        )

    # Read before deleting the rows that name them: `conversation_items` goes
    # inside the transaction below and takes the keys with it.
    image_keys: list[str] = []
    for row_with_images in await pool.fetch(
        """
        SELECT attachments
        FROM conversation_items
        WHERE session_id = $1 AND tenant_id = $2 AND jsonb_array_length(attachments) > 0
        """,
        session_id,
        tenant.id,
    ):
        image_keys.extend(
            item.object_key for item in attachments.stored(row_with_images["attachments"])
        )

    # The audio and, when the agent watched one, the screen video. Both are
    # content in exactly the same sense — a screen recording surviving a purge is
    # the most defensible thing here to get wrong.
    for key in (row["recording_object_key"], row["screenshare_recording_object_key"]):
        if not key:
            continue
        if not str(key).startswith(f"{tenant.id}/"):
            # Every key is written as `{tenant_id}/{session_id}[-screen].{ext}`.
            # One that is not is a bug, and deleting somebody else's object
            # because of it is the worst possible way to find out.
            raise ValueError(f"recording key {key!r} is outside tenant {tenant.id}")
        # S3 DELETE is idempotent, so an object a human already removed through
        # `DELETE /v1/calls/{id}/recording` costs one no-op call here.
        await recordings.delete_object(key=str(key))

    for image_key in image_keys:
        if not image_key.startswith(f"{tenant.id}/"):
            # Same rule as the recording key above: every key is written
            # `{tenant_id}/{conversation_id}/{image}.{ext}`, so one that is not
            # is a bug, and deleting someone else's object because of it is the
            # worst possible way to find out.
            raise ValueError(f"attachment key {image_key!r} is outside tenant {tenant.id}")
    await storage.delete_objects(attachments.bucket(), keys=image_keys)

    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            """
            UPDATE sessions
            SET userdata = NULL,
                summary = NULL,
                outcome_rationale = NULL,
                analysis_fields = '{}'::jsonb,
                error = NULL,
                recording_object_key = NULL,
                -- A phone number identifies a natural person, and a table of
                -- every number an agent ever called — kept forever — is the most
                -- obviously regulated thing we hold. Leaving these while
                -- deleting the transcript is not a defensible line.
                from_e164 = NULL,
                to_e164 = NULL,
                -- Holds the transfer `destination`: another number.
                transfer = NULL,
                -- 'expired' only where there was something to expire. A call
                -- that was never recorded, or whose caller withdrew consent,
                -- already says something true and more specific — and the
                -- withdrawal is a consent record, not a storage state.
                recording_status = CASE
                    WHEN recording_status IN ($3, $4) THEN $5 ELSE recording_status
                END,
                screenshare_recording_object_key = NULL,
                -- Same rule for the screen video, and the same reason to leave
                -- the other statuses alone: 'not_shared' and 'none' already say
                -- something truer than 'expired', because there was never an
                -- object to expire.
                screenshare_recording_status = CASE
                    WHEN screenshare_recording_status IN ($3, $4) THEN $5
                    ELSE screenshare_recording_status
                END,
                content_deleted_at = COALESCE(content_deleted_at, now()),
                updated_at = now()
            WHERE id = $1 AND tenant_id = $2
            """,
            session_id,
            tenant.id,
            RecordingStatus.STORED.value,
            RecordingStatus.PENDING.value,
            RecordingStatus.EXPIRED.value,
        )

        # The transcript, every tool call and every tool output.
        await conn.execute(
            "DELETE FROM conversation_items WHERE session_id = $1 AND tenant_id = $2",
            session_id,
            tenant.id,
        )

        # The trace goes with the transcript. Nothing that survives a purge reads
        # it — an invoice is the usage tables plus the cost columns, and
        # observability never fans out over it — so the only thing a redacted
        # trace could serve is a call detail whose every other panel says the
        # content was deleted. `session_events.SURVIVES_PURGE` says why the
        # consent record is the one row that stays.
        await conn.execute(
            "DELETE FROM session_events "
            "WHERE session_id = $1 AND tenant_id = $2 AND type <> ALL($3::text[])",
            session_id,
            tenant.id,
            list(session_events.SURVIVES_PURGE),
        )

        conversation_gone = await _cascade(
            conn, tenant, row["conversation_id"], row["conversation_ref_id"]
        )

    if conversation_gone:
        # After the commit, never inside it: a rolled-back transaction must not
        # leave the bucket emptied for rows that still exist. This sweeps images
        # from sibling sessions in the same thread, whose rows went with the
        # conversation by FK cascade. Idempotent — deleting an absent prefix is
        # a no-op, like every other write here.
        await storage.delete_prefix(
            attachments.bucket(), prefix=f"{tenant.id}/{row['conversation_id']}/"
        )

    logger.info("purge %s: content deleted", session_id)


async def _cascade(conn, tenant: Tenant, conversation_id, conversation_ref_id) -> bool:
    """Take the conversation, and then the identity, once nothing is left on them.

    Returns whether the conversation row was actually deleted, so the caller can
    clear that conversation's image prefix once the transaction has committed.

    **Reached only from a purged session's own `conversation_id`, never from a
    scan.** The CoPilot threads are `conversation_refs` whose
    conversations have no sessions at all, so "no unpurged sessions exist" is
    vacuously true for them — a scan-based cascade would delete every CoPilot
    thread in the workspace on the first purge. Coming through a session makes
    that structurally impossible.

    Each step is one predicate rather than check-then-act, so two concurrent
    purges of sibling sessions cannot both decide they were last.

    Erasing a contact's last conversation resets what the agent remembers about
    them: `conversation_refs.userdata` is the cross-call memory, so the next call
    from that number meets an agent that has never heard of them. Correct under
    GDPR, and stated in the settings help text so it is not discovered in a
    support ticket.
    """
    if conversation_id is None:
        return False

    # `conversation_items` for the whole thread go with this by FK cascade;
    # sibling sessions keep their rows with `conversation_id` nulled (SET NULL).
    # `conversations.summary` is left alone above: nothing in the runtime writes
    # it, so anything there is a note the tenant authored, and it goes with the
    # row rather than before it.
    deleted = await conn.fetchval(
        """
        DELETE FROM conversations c
        WHERE c.id = $1 AND c.tenant_id = $2
          AND NOT EXISTS (
              SELECT 1 FROM sessions s
              WHERE s.conversation_id = c.id AND s.tenant_id = c.tenant_id
                AND s.content_deleted_at IS NULL
          )
        RETURNING c.id
        """,
        conversation_id,
        tenant.id,
    )
    if deleted is None:
        return False
    if conversation_ref_id is None:
        return True

    # The identity itself, once it has no threads left. Not a cascade from the
    # FK — that one runs the other way (deleting a ref takes its conversations).
    await conn.execute(
        """
        DELETE FROM conversation_refs r
        WHERE r.id = $1 AND r.tenant_id = $2
          AND NOT EXISTS (
              SELECT 1 FROM conversations c
              WHERE c.conversation_ref_id = r.id AND c.tenant_id = r.tenant_id
          )
        """,
        conversation_ref_id,
        tenant.id,
    )
    return True
