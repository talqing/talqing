"""The `session.completed` payload, built once for every producer.

Three places finish a session: the voice/video worker, the text worker, and the
billing reconcile sweep that picks up sessions whose worker died. All three send
the same event, so all three build it here — and they build it by reading the
database rather than from whatever they happen to hold in memory.

That is the point. By the time this runs, everything the payload describes has
already been written: the session row, the analysis columns, the money columns,
the recording columns. Reading them back means the webhook cannot disagree with
what `get_call` will show a minute later, and a fourth producer gets the right
shape for free.

Everything it reads comes from `services/`. This module is imported by
`services.webhooks.__init__`, which the worker reaches through
`services.agents` — so an import of `workers.*` from here closes a cycle that
breaks the worker while leaving the API perfectly happy.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence

import db
from services import recordings
from services.agents.plan import draft_agent_ids
from services.tools import TranscriptRow
from services.transcripts import load_session_transcript
from services.user import Tenant

logger = logging.getLogger("talqing.webhooks")

_SELECT = """
    -- Every column is qualified: `summary`, `userdata` and `created_at` all
    -- exist on the two joined tables too, and mean different things there.
    SELECT s.id, s.conversation_id, ref.conversation_key AS contact_key,
           s.agent_id, s.agent_name, s.channel, s.type, s.status,
           s.close_reason, s.from_e164, s.to_e164, s.userdata, s.duration_s,
           COALESCE(s.started_at, s.created_at) AS started_at, s.ended_at,
           s.provider_cost, s.platform_fee, s.total_charge, s.pricing_snapshot,
           s.billing_status,
           s.recording_status, s.recording_started_at, s.recording_duration_s,
           s.recording_bytes,
           s.screenshare_recording_status, s.screenshare_recording_started_at,
           s.screenshare_recording_duration_s, s.screenshare_recording_bytes,
           -- When the organization's retention policy will delete this call's
           -- content, read from the job that will do it. Never recomputed from
           -- today's policy: changing `retention_days` moves nothing already
           -- scheduled, so arithmetic here would state a date that will not
           -- happen. MIN because deleting the call schedules a second, sooner
           -- purge beside the retention one rather than cancelling it.
           (SELECT min(j.scheduled_at) FROM scheduled_jobs j
             WHERE j.tenant_id = s.tenant_id AND j.subject_id = s.id
               AND j.kind = 'session.purge' AND j.status = 'pending')
             AS content_expires_at,
           s.analysis_status, s.analysis_skip_reason, s.summary, s.outcome,
           s.outcome_rationale, s.analysis_fields, s.transfer, s.batch_id, s.integration_id,
           s.agent_plan, av.version AS agent_version
    FROM sessions s
    -- Which version answered. Null when the call ran a definition sent in the
    -- request that started it, which genuinely has no version.
    LEFT JOIN agent_versions av
        ON av.id = s.agent_version_id AND av.tenant_id = s.tenant_id
    -- The stable id of the person on the call. `conversation_id` is new on every
    -- call once an agent starts them clean, so without this a consumer has
    -- nothing to correlate a customer's calls on. LEFT: a refused or off-thread
    -- session has no conversation to reach an identity through.
    LEFT JOIN conversations c
        ON c.id = s.conversation_id AND c.tenant_id = s.tenant_id
    LEFT JOIN conversation_refs ref
        ON ref.id = c.conversation_ref_id AND ref.tenant_id = c.tenant_id
    WHERE s.id = $1 AND s.tenant_id = $2
"""


def _recording(row, *, prefix: str = "recording") -> dict[str, object]:
    """One of the call's two recordings, in the shape a consumer reads.

    Parameterized by column prefix rather than duplicated: the screen video says
    exactly the same four things about itself as the audio, and a second copy of
    this is how the two start describing the same call differently.
    """
    started_at = row[f"{prefix}_started_at"]
    state = recordings.resolve_state(
        str(row[f"{prefix}_status"]), started_at, session_ended_at=row["ended_at"]
    )
    expires_at = row["content_expires_at"]
    return {
        # The state, not the raw status: "was uploaded" and "can still be
        # played" are different questions, and only the second is useful to a
        # consumer. `services.recordings.resolve_state` answers it in the one
        # place every surface reads.
        "state": state,
        "started_at": started_at.isoformat() if started_at else None,
        "duration_s": row[f"{prefix}_duration_s"],
        "bytes": row[f"{prefix}_bytes"],
        # No URL here, unlike `recording.ready`: this payload is also rebuilt by
        # the reconcile sweep hours after the call, where a link that dies in an
        # hour would be a broken promise. Call `get_call_recording` for one.
        #
        # When the organization's retention policy will delete this call's
        # content; null while retention is unlimited, which is the default.
        "expires_at": expires_at.isoformat() if expires_at else None,
    }


def _plan_entry(row) -> dict[str, object] | None:
    """The entry member of this call's plan, or None when it ran as published."""
    plan = row["agent_plan"]
    if not isinstance(plan, dict):
        return None
    members = plan.get("members") or []
    return members[0] if members else None


def _cost(row) -> dict[str, object] | None:
    """The stored pricing snapshot — the same breakdown the API serves."""
    if row["billing_status"] != "computed":
        return None
    snapshot = row["pricing_snapshot"]
    if isinstance(snapshot, str):
        try:
            snapshot = json.loads(snapshot)
        except ValueError:
            snapshot = None
    return snapshot if isinstance(snapshot, dict) else None


async def build_session_completed(
    tenant: Tenant,
    session_id: str,
    *,
    transcript: Sequence[TranscriptRow] | None = None,
) -> dict[str, object] | None:
    """The end-of-session report for one session, or None if it has vanished.

    ``transcript`` is the one the caller already loaded — `finalize_session`
    returns it and would otherwise have this read the same hundreds of rows back
    seconds later. Omit it and this loads its own, which is what the reconcile
    sweep and the analysis backfill need: they build this payload for a call
    they were not on.
    """
    pool = await db.tenant_pool(tenant)
    row = await pool.fetchrow(_SELECT, session_id, tenant.id)
    if row is None:
        logger.warning(
            "session.completed: session %s not found in tenant %s", session_id, tenant.id
        )
        return None

    fields = row["analysis_fields"]
    ran_draft = (
        row["agent_version"] is None
        and row["agent_id"] is not None
        and str(row["agent_id"]) in draft_agent_ids(row["agent_plan"])
    )
    return {
        "session_id": str(row["id"]),
        "conversation_id": str(row["conversation_id"]) if row["conversation_id"] else None,
        # Who the call was with, stable across every call from the same person.
        # `conversation_id` identifies THIS call's thread and changes each time.
        "contact_key": row["contact_key"],
        "agent_id": str(row["agent_id"]) if row["agent_id"] else None,
        "agent_name": row["agent_name"],
        # "draft" when the call ran the agent's unpublished draft, as `get_call`
        # reports it.
        "agent_version": "draft" if ran_draft else row["agent_version"],
        # Whether this call ran something other than the agent's published
        # version, and whether the definition existed at all before the request
        # that started it. Flags rather than the config itself: a config is not
        # an event, and it is one `get_call` away.
        # A draft carries its whole config in the plan, but it is the agent's own
        # config rather than changes layered on one — the calls list says the same.
        "agent_overridden": _plan_entry(row) is not None and not ran_draft,
        "agent_inline": (_plan_entry(row) or {}).get("agent_id") is None
        if row["agent_plan"]
        else False,
        "channel": row["channel"],
        "type": row["type"],
        "status": row["status"],
        "close_reason": row["close_reason"],
        "started_at": row["started_at"].isoformat() if row["started_at"] else None,
        "ended_at": row["ended_at"].isoformat() if row["ended_at"] else None,
        "duration_s": row["duration_s"],
        "from_e164": row["from_e164"],
        "to_e164": row["to_e164"],
        # Which outbound batch placed this call, null for every other kind. A CRM
        # needs to know which list a call came from, and it is one column away.
        "batch_id": str(row["batch_id"]) if row["batch_id"] else None,
        # The WhatsApp number a WhatsApp call arrived on, null for every other kind.
        "integration_id": str(row["integration_id"]) if row["integration_id"] else None,
        "cost": _cost(row),
        # What the agent KNEW — tools wrote this during the call.
        "userdata": row["userdata"] if isinstance(row["userdata"], dict) else {},
        "transcript": (
            list(transcript)
            if transcript is not None
            else await load_session_transcript(tenant, session_id)
        ),
        # What a reader INFERRED afterwards. Kept beside `userdata`, never merged
        # into it: merging would let a guess overwrite a tool-confirmed value
        # with nothing downstream able to tell them apart.
        "analysis": {
            "status": row["analysis_status"],
            "skip_reason": row["analysis_skip_reason"],
            "summary": row["summary"],
            "outcome": row["outcome"],
            "outcome_rationale": row["outcome_rationale"],
            "fields": fields if isinstance(fields, dict) else {},
        },
        # Null on every call that never attempted one. There is deliberately no
        # separate `call.transferred` event: a transfer ends our session, so
        # this fires immediately afterwards and carries every fact such an event
        # would have — and two events describing one fact is two things to keep
        # in step (`services/webhooks/events.py`).
        #
        # It is also what explains `duration_s` to a consumer. Both that and the
        # recording measure OUR part of the call; the caller may have stayed
        # with a person for another fifteen minutes.
        "transfer": row["transfer"] if isinstance(row["transfer"], dict) else None,
        "recording": _recording(row),
        # The screen the person shared, when the agent watched one and the author
        # asked for it to be kept. `none` means screen recording was off;
        # `not_shared` means it was on and nobody ever shared.
        "screen_recording": _recording(row, prefix="screenshare_recording"),
    }
