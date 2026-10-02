"""Session pricing at end-of-session.

Every modality prices the same way: once usage is on the session (or empty),
call ``bill_session``. That loads usage, runs ``price_session``, and writes the
money columns. It sends nothing: whoever finished the session announces it with
``session.completed``, which carries the cost among everything else.

Voice/video: workers call this immediately after ``finalize_session``.
Text: a chat may never end, so it is priced as it goes — see ``settle_chat``.
No queue and no sweep — pricing is pure catalog math and a few SQL writes, done
inline by whoever ended the session.

Unexpected errors raise (callers must not swallow them). Catalog-unpriceable
usage is an explicit product outcome: ``billing_status='unpriceable'``, no raise.

Pricing also DEBITS the workspace's prepaid balance, in the same transaction as
the money columns — see ``services/credits``. Both live in this tenant's data
plane, so a call cannot be priced without being debited or debited without being
priced, and there is nothing a reconcile pass could repair between them. The
debit is ``platform_fee`` alone: the provider cost beside it is the tenant's own
spend on their own key.

**Exactly one process prices a session, and it is the one that ran it.** There is
no background reconcile: a 60 s cross-tenant sweep used to re-price terminal rows
still left ``pending``, and it could not tell "its worker died" from "its worker
is still in the finalize tail", so it priced calls before the analysis LLM row
existed and announced that under-price — then the worker announced the right one.
Deleting it makes ``session.completed`` at-most-once by construction. With the
debit inside the same transaction that paragraph only gets stronger: a sweep
would now have two things to reconcile instead of one, and neither can drift.

**The accepted consequence:** a session whose worker dies is never priced, never
announced, and never debited. That is our platform fee on a crashed call —
literally, now, rather than only in a report, and still never the tenant's money
(BYOK). It is not worth a row per call to recover. A batch call is the
one exception, because a stuck recipient wedges a whole campaign:
``services/telephony/batch/settle.py`` fails it ``stale`` from a predicate in a
query that batch already runs. Nothing closes a crashed non-batch call until a
LiveKit webhook receiver exists.
"""

from __future__ import annotations

import asyncio
import json
import logging
from uuid import UUID

import db
from services import credits
from services.user import Tenant, load_tenant

from .models import (
    AvatarUsage,
    LLMUsage,
    PriceBreakdown,
    RealtimeUsage,
    SessionUsage,
    STTUsage,
    TTSUsage,
)
from .pricing import UnpriceableUsageError, price_session

logger = logging.getLogger("talqing.billing")


def _as_uuid(value: UUID | str) -> UUID:
    return value if isinstance(value, UUID) else UUID(str(value))


async def _resolve_tenant(tenant: Tenant | UUID | str) -> Tenant | None:
    if isinstance(tenant, Tenant):
        return tenant
    return await load_tenant(tenant)


async def _load_usage(tpool, *, tenant_id: UUID, session_id: UUID) -> SessionUsage:
    """Everything this session consumed, in one round trip.

    Five independent lookups on the same session id, so they go out together
    rather than one after the next. Each stays the readable single-table SELECT
    it was and still validates into its own model: the five tables have five
    shapes and five pricing models, and one statement returning all of them
    would have to be read in full to answer a question about any one.
    """
    llm, tts, stt, realtime, avatar = await asyncio.gather(
        tpool.fetch(
            "SELECT id, session_id, provider, model, input_tokens, input_cached_tokens, "
            "input_cache_write_tokens, output_tokens, reported_cost, purpose, priority, "
            "tenant_id FROM llm_usage WHERE session_id = $1 AND tenant_id = $2",
            session_id,
            tenant_id,
        ),
        tpool.fetch(
            "SELECT id, session_id, provider, model, characters_count, audio_duration, "
            "input_tokens, output_tokens, tenant_id "
            "FROM tts_usage WHERE session_id = $1 AND tenant_id = $2",
            session_id,
            tenant_id,
        ),
        tpool.fetch(
            "SELECT id, session_id, provider, model, audio_duration, input_tokens, "
            "output_tokens, reported_cost, tenant_id "
            "FROM stt_usage WHERE session_id = $1 AND tenant_id = $2",
            session_id,
            tenant_id,
        ),
        tpool.fetch(
            "SELECT id, session_id, provider, model, input_text_tokens, "
            "input_cached_text_tokens, input_audio_tokens, input_cached_audio_tokens, "
            "output_text_tokens, output_audio_tokens, session_seconds, tenant_id "
            "FROM realtime_usage WHERE session_id = $1 AND tenant_id = $2",
            session_id,
            tenant_id,
        ),
        tpool.fetch(
            "SELECT id, session_id, provider, model, avatar_id, avatar_session_id, "
            "seconds, tenant_id "
            "FROM avatar_usage WHERE session_id = $1 AND tenant_id = $2",
            session_id,
            tenant_id,
        ),
    )
    return SessionUsage(
        llm=[LLMUsage.model_validate(dict(r)) for r in llm],
        tts=[TTSUsage.model_validate(dict(r)) for r in tts],
        stt=[STTUsage.model_validate(dict(r)) for r in stt],
        realtime=[RealtimeUsage.model_validate(dict(r)) for r in realtime],
        avatar=[AvatarUsage.model_validate(dict(r)) for r in avatar],
    )


def _duration_s(session) -> int | None:
    if session["started_at"] and session["ended_at"]:
        # whole-second precision matches the billing column / API (int)
        return round((session["ended_at"] - session["started_at"]).total_seconds())
    if session["duration_s"] is not None:
        return int(session["duration_s"])
    return None


async def bill_session(
    tenant: Tenant | UUID | str,
    session_id: UUID | str,
) -> PriceBreakdown | None:
    """Price one terminal session from usage rows already in the tenant DB.

    Idempotent: safe to call again on the same session (rewrites money columns).
    Sends no webhook — see the module docstring. Callers that are finishing a
    session (the workers, the reconcile sweep) dispatch ``session.completed``
    themselves once this returns.

    Returns the breakdown on success, ``None`` when the session is missing,
    unpriceable, or the tenant cannot be resolved.
    """
    resolved = await _resolve_tenant(tenant)
    if resolved is None:
        logger.warning("bill_session: unknown tenant %s", tenant)
        return None

    session_uuid = _as_uuid(session_id)
    tpool = await db.tenant_pool(resolved)

    session = await tpool.fetchrow(
        """
        SELECT id, agent_id, channel, status, close_reason, billing_status,
               started_at, ended_at, duration_s, userdata, conversation_id
        FROM sessions
        WHERE id = $1 AND tenant_id = $2
        """,
        session_uuid,
        resolved.id,
    )
    if not session:
        logger.warning(
            "bill_session: session %s not found in tenant %s",
            session_uuid,
            resolved.id,
        )
        return None

    usage = await _load_usage(tpool, tenant_id=resolved.id, session_id=session_uuid)
    duration_s = _duration_s(session)

    try:
        breakdown = price_session(
            usage, duration_s, session["close_reason"], session["channel"], None
        )
    except UnpriceableUsageError as e:
        # Quarantine so reconcile does not retry forever. Re-price after a
        # catalog fix by resetting billing_status to 'pending'.
        logger.error(
            "session %s tenant %s is unpriceable: %s",
            session_uuid,
            resolved.id,
            e,
        )
        await tpool.execute(
            """
            UPDATE sessions
            SET duration_s = COALESCE($3, duration_s),
                pricing_snapshot = $4::jsonb,
                billing_status = 'unpriceable',
                billed_at = now(),
                updated_at = now()
            WHERE id = $1 AND tenant_id = $2
            """,
            session_uuid,
            resolved.id,
            duration_s,
            json.dumps({"error": str(e)}),
        )
        return None

    # The debit commits with the money columns it is derived from. Both live in
    # this tenant's data plane, so this is a single local transaction — a call
    # cannot be priced without being debited, or debited without being priced,
    # and there is nothing for a sweep to reconcile afterwards.
    async with tpool.acquire() as conn, conn.transaction():
        await conn.execute(
            """
            UPDATE sessions
            SET duration_s = COALESCE($3, duration_s),
                provider_cost = $4,
                platform_fee = $5,
                total_charge = $6,
                pricing_snapshot = $7::jsonb,
                billing_status = 'computed',
                billed_at = now(),
                updated_at = now()
            WHERE id = $1 AND tenant_id = $2
            """,
            session_uuid,
            resolved.id,
            duration_s,
            breakdown.provider_cost,
            breakdown.platform_fee,
            breakdown.total_charge,
            breakdown.model_dump_json(),
        )
        # The PLATFORM FEE, never `total_charge`: the provider cost beside it is
        # the tenant's own spend on their own key under BYOK, already paid to the
        # provider, and taking it here would charge them for it twice.
        await credits.debit_session_fee(conn, resolved.id, session_uuid, breakdown.platform_fee)
    logger.info(
        "billed session %s tenant %s total=%s",
        session_uuid,
        resolved.id,
        breakdown.total_charge,
    )

    return breakdown


async def settle_chat(
    tenant: Tenant, session_id: UUID | str, segment_id: UUID | str
) -> PriceBreakdown | None:
    """Price a text chat so far, and debit what that adds to what it already paid.

    A chat can stay open for ever, so it cannot wait for its end to be billed:
    each warm window settles when it closes, and the end settles once more for
    whatever came after (the analysis call). Every settlement re-prices the whole
    chat from all of its usage rows and its answered messages, and debits only
    the DIFFERENCE from the fees already taken — so the ledger always sums to the
    chat's fee, however many windows it took.

    ``segment_id`` names the settlement: a window's own id, or the chat's id for
    the final one. ``uq_credit_ledger_session_segment`` makes a repeat a no-op.

    ``billing_status = 'computed'`` on an open chat means "priced so far".
    """
    session_uuid = _as_uuid(session_id)
    tpool = await db.tenant_pool(tenant)
    usage = await _load_usage(tpool, tenant_id=tenant.id, session_id=session_uuid)
    async with tpool.acquire() as conn, conn.transaction():
        # The lock is what makes the delta below safe to compute: two settlements
        # of one chat would otherwise both read the same "already debited".
        session = await conn.fetchrow(
            "SELECT close_reason FROM sessions "
            "WHERE id = $1 AND tenant_id = $2 AND channel = 'text' FOR UPDATE",
            session_uuid,
            tenant.id,
        )
        if session is None:
            logger.warning("settle_chat: chat %s not found in tenant %s", session_uuid, tenant.id)
            return None
        answered = await conn.fetchval(
            """
            SELECT count(*) FROM conversation_items
            WHERE tenant_id = $1 AND session_id = $2
                AND direction = 'inbound' AND turn_status = 'done'
            """,
            tenant.id,
            session_uuid,
        )
        try:
            breakdown = price_session(usage, None, session["close_reason"], "text", answered)
        except UnpriceableUsageError as e:
            logger.error("chat %s tenant %s is unpriceable: %s", session_uuid, tenant.id, e)
            await conn.execute(
                """
                UPDATE sessions
                SET pricing_snapshot = $3::jsonb, billing_status = 'unpriceable',
                    billed_at = now(), updated_at = now()
                WHERE id = $1 AND tenant_id = $2
                """,
                session_uuid,
                tenant.id,
                json.dumps({"error": str(e)}),
            )
            return None
        await conn.execute(
            """
            UPDATE sessions
            SET provider_cost = $3, platform_fee = $4, total_charge = $5,
                pricing_snapshot = $6::jsonb, billing_status = 'computed',
                billed_at = now(), updated_at = now()
            WHERE id = $1 AND tenant_id = $2
            """,
            session_uuid,
            tenant.id,
            breakdown.provider_cost,
            breakdown.platform_fee,
            breakdown.total_charge,
            breakdown.model_dump_json(),
        )
        debited = await conn.fetchval(
            "SELECT COALESCE(-sum(amount), 0) FROM credit_ledger "
            "WHERE tenant_id = $1 AND session_id = $2 AND kind = 'usage'",
            tenant.id,
            session_uuid,
        )
        await credits.debit_session_fee(
            conn,
            tenant.id,
            session_uuid,
            breakdown.platform_fee - debited,
            segment_id=_as_uuid(segment_id),
        )
    return breakdown
