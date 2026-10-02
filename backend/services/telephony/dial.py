"""Placing one outbound call, split at the database/network boundary.

Three functions, and the seam between the second and the third is the design
rather than a refactor convenience:

- :func:`resolve_dial_target` reads the four rows a dial needs — the number, its
  carrier account, the agent, and the agent's **current published** version — and
  refuses with the reason when any of them is unusable. Constant for the whole of
  a batch's pass, so the dispatcher calls it once and reuses the result across
  every recipient it claims; ``create_outbound_call`` calls it per request, which
  is what a one-off dial should do.
- :func:`record_outbound_call` does every database write: the conversation
  thread and the ``sessions`` row. It takes an **open connection**, so the caller
  decides what else commits with it — which is how the batch dispatcher makes
  claiming a recipient and recording its call one transaction.
- :func:`dispatch_outbound_call` does the network: the LiveKit room and the agent
  dispatch, plus the rollback that marks the session ``sip_dispatch_failed`` if
  either fails.

Everything before the commit is undoable by Postgres; everything after has
touched the outside world. That is the whole of this feature's crash story: a
dial either committed with its session or never happened, so there is no
half-claimed state to sweep up and no window in which a crash could lose or
duplicate a call.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from livekit import api as lk_api
from pydantic import ValidationError

from services.agents import ConversationSpec, VarDeclaration, missing_required_vars
from services.agents.plan import CallPlan, load_plan_roster
from services.conversations import ensure_sip_ref_on_conn, sip_conversation_key
from services.user import Context
from services.userdata import RESERVED_KEY_ERROR, is_reserved_key
from settings import get_settings

from . import livekit_sip
from .models import (
    PHONE_NUMBER_COLUMNS,
    TELEPHONY_ACCOUNT_COLUMNS,
    PhoneNumber,
    TelephonyAccount,
)

logger = logging.getLogger("talqing.telephony.dial")


class DialError(Exception):
    """A dial that could not be placed, in the two vocabularies that need it.

    ``status_code`` is what an HTTP caller gets. ``close_reason`` is what a batch
    recipient records and — through ``services.close_reasons.bucket_for`` — what
    decides whether that recipient is retried and whether the batch's circuit
    breaker counts it. Both are set at the raise site because only the raise site
    knows which of "you asked for something impossible" and "the platform could
    not do it right now" it is.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int = 400,
        close_reason: str = "sip_outbound_config_failed",
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.close_reason = close_reason


@dataclass(frozen=True, slots=True)
class DialTarget:
    """Who is calling, from where, running what — everything a dial needs that
    does not vary by recipient.

    Deliberately a value: the batch dispatcher resolves one per pass and hands
    the same object to every claim in that pass, so a 10 000-row batch does four
    reads a minute rather than four per dial. Never cache it *across* passes —
    with no plan the published version is re-read every pass on purpose, so a
    republished prompt reaches the calls that follow.

    ``agent_plan`` is the per-call cast when the caller asked for one (an
    override, an inline agent, a team). NULL means "the entry agent's published
    version", which is every dial that names an `agent_id` and stops there — and
    the three fields above it are then exactly what they always were. With a
    plan, `agent_id` / `agent_version_id` / `published_version` describe the
    ENTRY member's base and are null for an inline or draft one.
    """

    number: PhoneNumber
    account: TelephonyAccount
    agent_id: UUID | None
    agent_version_id: UUID | None
    published_version: int | None
    # The ENTRY agent's resolved config — what decides the conversation context
    # this dial opens against and the name the session is listed under.
    agent_config: dict[str, Any]
    agent_plan: dict[str, Any] | None = None

    @property
    def agent_name(self) -> str | None:
        name = self.agent_config.get("name")
        return name if isinstance(name, str) else None


@dataclass(frozen=True, slots=True)
class RecordedCall:
    """A call that exists in the database and has not yet reached LiveKit."""

    session_id: UUID
    conversation_id: UUID
    conversation_ref_id: UUID
    contact_key: str
    to_e164: str
    userdata: dict[str, Any]


async def resolve_dial_number(
    ctx: Context, from_phone_number_id: UUID
) -> tuple[PhoneNumber, TelephonyAccount]:
    """The number this dial leaves from, and the carrier account behind it."""
    pool = await ctx.tenant_pool()
    number_row = await pool.fetchrow(
        f"""
        SELECT {PHONE_NUMBER_COLUMNS}
        FROM phone_numbers
        WHERE id = $1 AND tenant_id = $2
        """,
        from_phone_number_id,
        ctx.tenant.id,
    )
    if not number_row:
        raise DialError("phone number not found", status_code=404)
    number = PhoneNumber.from_row(number_row)
    if not number.can_outbound:
        raise DialError("this number is not enabled for outbound")
    if number.status != "active":
        raise DialError("phone number must be active (provisioned) for outbound calls")

    account_row = await pool.fetchrow(
        f"""
        SELECT {TELEPHONY_ACCOUNT_COLUMNS}
        FROM telephony_accounts
        WHERE id = $1 AND tenant_id = $2
        """,
        number.telephony_account_id,
        ctx.tenant.id,
    )
    if not account_row:
        raise DialError("telephony account not found", status_code=404)
    account = TelephonyAccount.from_row(account_row)
    if account.status != "ready":
        raise DialError("telephony account is not ready for outbound; provision it first")
    return number, account


def dial_target_for_plan(
    number: PhoneNumber, account: TelephonyAccount, plan: CallPlan
) -> DialTarget:
    """A target for a cast that has already been resolved and validated.

    No reads and no re-checks: `resolve_call_plan` has just done both, against
    the same rules publishing uses, and doing them again here would only be able
    to agree.
    """
    entry = plan.entry
    return DialTarget(
        number=number,
        account=account,
        agent_id=UUID(entry.agent_id) if entry.agent_id else None,
        agent_version_id=UUID(entry.agent_version_id) if entry.agent_version_id else None,
        published_version=entry.version,
        agent_config=entry.config.model_dump(mode="json"),
        agent_plan=plan.stored(),
    )


async def resolve_dial_target(
    ctx: Context,
    *,
    agent_id: UUID | None,
    from_phone_number_id: UUID,
    session_vars: dict[str, str],
    agent_plan: dict[str, Any] | None = None,
) -> DialTarget:
    """Load and check the number, carrier account, agent and published version.

    The one implementation of "may this agent call from this number?", shared by
    batch create/patch validation and the dispatcher's per-pass resolve.

    With an ``agent_plan`` the cast was resolved and validated when the batch was
    created, so this rehydrates the entry member rather than re-reading a
    published version — which is exactly the pinning promise: a republish under a
    running campaign must not change what it dials with.

    ``session_vars`` is the batch's stored bag, and it is checked here because
    this is the one place a campaign's agent can change under it: a `PATCH` that
    re-points it, and every dispatcher pass on a batch that follows published.
    A batch stores its bag once, at create, and `PatchCallBatchRequest` has no
    `vars` — so an agent that gained a requirement is a campaign that can no
    longer run, and saying so beats spending real money on calls whose author
    already said they could not work.

    Raises :class:`DialError`.
    """
    number, account = await resolve_dial_number(ctx, from_phone_number_id)
    pool = await ctx.tenant_pool()

    if agent_plan is not None:
        try:
            roster = await load_plan_roster(pool, ctx.tenant.id, agent_plan)
        except (ValueError, ValidationError) as exc:
            raise DialError(f"this batch's agent plan can no longer run: {exc}") from exc
        entry = roster[0]
        _refuse_missing_vars(entry.config.vars, session_vars)
        version_id = None
        if entry.agent_id and entry.version:
            version_id = await pool.fetchval(
                "SELECT id FROM agent_versions "
                "WHERE agent_id = $1 AND version = $2 AND tenant_id = $3",
                UUID(entry.agent_id),
                entry.version,
                ctx.tenant.id,
            )
        return DialTarget(
            number=number,
            account=account,
            agent_id=UUID(entry.agent_id) if entry.agent_id else None,
            agent_version_id=version_id,
            published_version=entry.version,
            agent_config=entry.config.model_dump(mode="json"),
            agent_plan=agent_plan,
        )

    if agent_id is None:
        raise DialError("this batch has no agent to call with")
    agent = await pool.fetchrow(
        """
        SELECT id, published_version FROM agents
        WHERE id = $1 AND tenant_id = $2
        """,
        agent_id,
        ctx.tenant.id,
    )
    if not agent:
        raise DialError("agent not found", status_code=404)
    if agent["published_version"] is None:
        raise DialError("publish the agent before calling")
    version_row = await pool.fetchrow(
        """
        SELECT id, config FROM agent_versions
        WHERE agent_id = $1 AND version = $2 AND tenant_id = $3
        """,
        agent_id,
        agent["published_version"],
        ctx.tenant.id,
    )
    if not version_row or not isinstance(version_row["config"], dict):
        raise DialError("published agent version is missing")
    if version_row["config"].get("channel") != "voice":
        raise DialError("outbound SIP requires a voice agent")
    # Parsed here rather than through `AgentConfig`: this is per dispatcher pass,
    # and a stored config that no longer parses in some unrelated way is not this
    # check's business to raise about.
    declared = [VarDeclaration.model_validate(v) for v in version_row["config"].get("vars") or []]
    _refuse_missing_vars(declared, session_vars)

    return DialTarget(
        number=number,
        account=account,
        agent_id=agent_id,
        agent_version_id=version_row["id"],
        published_version=agent["published_version"],
        agent_config=version_row["config"],
    )


def _refuse_missing_vars(declared: Sequence[VarDeclaration], session_vars: dict[str, str]) -> None:
    """The same refusal the four session-start endpoints make, as a DialError."""
    missing = missing_required_vars(declared, session_vars)
    if missing:
        raise DialError(
            "this batch supplies no value for "
            + ", ".join(f"'{name}'" for name in missing)
            + ", which the agent requires and has no default for - give the variable a "
            "default, make it optional, or re-create the batch with a value"
        )


def validate_userdata(userdata: dict[str, Any] | None) -> dict[str, Any]:
    """What this call starts knowing, checked. Raises :class:`DialError`."""
    if not userdata:
        return {}
    for key in userdata:
        if not isinstance(key, str) or not key.strip():
            raise DialError("userdata keys must be non-empty strings")
        if is_reserved_key(key):
            raise DialError(RESERVED_KEY_ERROR)
    return dict(userdata)


async def record_outbound_call(
    conn,
    *,
    ctx: Context,
    target: DialTarget,
    to_e164: str,
    userdata: dict[str, Any],
    session_vars: dict[str, str],
    batch_id: UUID | None = None,
) -> RecordedCall:
    """Write the conversation thread and the ``queued`` session, on ``conn``.

    The two bags go to different rows, because they mean different things.
    ``userdata`` seeds the CONTACT's identity row, where it outlives this call.
    ``session_vars`` goes on the session and nowhere else — it describes this
    call, not the person, and putting deployment configuration on somebody's
    contact record would replay it into their next call.

    ``conn`` is an OPEN connection inside the caller's transaction, and that is
    the point: the batch dispatcher commits this together with the ``UPDATE``
    that marks its recipient ``dialing``, so a recipient is ``dialing`` if and
    only if a call was recorded for it — a ``CHECK`` constraint rather than a
    promise. Nothing here has contacted a carrier, so a rollback costs nothing
    but a wasted id.

    Raises :class:`DialError`; anything the database itself raises propagates.
    """
    contact_key = sip_conversation_key(did_e164=target.number.e164, peer_e164=to_e164)
    # What this call starts knowing, from the version it will run — resolved here
    # rather than in the worker because the caller is already promising a
    # conversation id and the queued session row is written against it.
    conversation_spec = ConversationSpec.model_validate(target.agent_config["conversation"])
    thread = await ensure_sip_ref_on_conn(
        conn,
        tenant_id=ctx.tenant.id,
        conversation_key=contact_key,
        phone_number_id=target.number.id,
        context=conversation_spec.context,
        metadata={
            "phone_e164": to_e164,
            "did_e164": target.number.e164,
            "direction": "outbound",
        },
        userdata_seed=userdata or None,
        outbound=True,
    )

    session_id = uuid4()
    await conn.execute(
        """
        INSERT INTO sessions (
            id, tenant_id, conversation_id, agent_id, agent_version_id,
            agent_name, channel, type, status,
            conversation_ref_id, phone_number_id, telephony_account_id,
            livekit_room, from_e164, to_e164, batch_id, billing_status, agent_plan,
            vars
        )
        VALUES (
            $1, $2, $3, $4, $5,
            $6, 'voice', 'SIP_OUTBOUND', 'queued',
            $7, $8, $9,
            $10, $11, $12, $13, 'pending', $14::jsonb,
            $15::jsonb
        )
        """,
        session_id,
        ctx.tenant.id,
        thread.conversation_id,
        target.agent_id,
        target.agent_version_id,
        target.agent_name,
        thread.ref.id,
        target.number.id,
        target.account.id,
        str(session_id),
        target.number.e164,
        to_e164,
        batch_id,
        json.dumps(target.agent_plan) if target.agent_plan is not None else None,
        json.dumps(session_vars) if session_vars else None,
    )
    return RecordedCall(
        session_id=session_id,
        conversation_id=thread.conversation_id,
        conversation_ref_id=thread.ref.id,
        contact_key=thread.ref.conversation_key,
        to_e164=to_e164,
        userdata=userdata,
    )


def _dispatch_metadata(ctx: Context, target: DialTarget, recorded: RecordedCall) -> str:
    """What the voice worker is told about a call before it dials.

    Built here rather than at each call site so a batch dial and a one-off dial
    cannot end up describing themselves differently to the same worker. The
    batch id is deliberately absent: it is on the session row, which is where
    everything that needs it reads it.
    """
    return json.dumps(
        {
            "kind": "sip_call",
            "direction": "outbound",
            "tenant": str(ctx.tenant.id),
            # Both null on an inline entry agent, which has no row and no
            # version. The worker reads the plan off the session row for those.
            "agent": str(target.agent_id) if target.agent_id else None,
            "version": target.published_version,
            "channel": "voice",
            "session": str(recorded.session_id),
            "conversation_id": str(recorded.conversation_id),
            "contact_key": recorded.contact_key,
            "conversation_ref_id": str(recorded.conversation_ref_id),
            "phone_number_id": str(target.number.id),
            "telephony_account_id": str(target.account.id),
            "from_e164": target.number.e164,
            "to_e164": recorded.to_e164,
            "userdata": recorded.userdata,
        }
    )


async def dispatch_outbound_call(
    *, ctx: Context, target: DialTarget, recorded: RecordedCall
) -> None:
    """Create the room and dispatch the agent into it. Network only.

    On failure the session it was given is marked ``sip_dispatch_failed`` and the
    room is best-effort deleted, then :class:`DialError` is raised. That write is
    the one database access on this side of the seam, and it happens only when
    something has already gone wrong.
    """
    s = get_settings()
    metadata = _dispatch_metadata(ctx, target, recorded)
    room = str(recorded.session_id)
    try:
        async with livekit_sip.lk_client() as lk:
            await lk.room.create_room(
                lk_api.CreateRoomRequest(name=room, empty_timeout=60, metadata="")
            )
            await lk.agent_dispatch.create_dispatch(
                lk_api.CreateAgentDispatchRequest(
                    agent_name=s.livekit.agent_name,
                    room=room,
                    metadata=metadata,
                )
            )
    except Exception as exc:
        logger.exception("LiveKit room/dispatch failed for outbound %s", recorded.session_id)
        pool = await ctx.tenant_pool()
        await pool.execute(
            """
            UPDATE sessions
            SET status = 'failed',
                close_reason = 'sip_dispatch_failed',
                ended_at = COALESCE(ended_at, now()),
                updated_at = now()
            WHERE id = $1 AND tenant_id = $2 AND status = 'queued'
            """,
            recorded.session_id,
            ctx.tenant.id,
        )
        try:
            async with livekit_sip.lk_client() as lk:
                await lk.room.delete_room(lk_api.DeleteRoomRequest(room=room))
        except Exception:
            logger.exception("best-effort delete of outbound room %s failed", room)
        raise DialError(
            f"failed to dispatch outbound call: {exc}",
            status_code=502,
            close_reason="sip_dispatch_failed",
        ) from exc
