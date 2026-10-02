"""What a tenant chat's warm window owes when it closes: its usage, and its price.

A window is internal — see ``TextWindow``. Closing one changes nothing about the
chat it served: no status, no webhook, no analysis. It only writes down what it
metered, because the next window starts counting from zero.
"""

from __future__ import annotations

import json
import logging

from livekit.agents import AgentSession

import db
from services import billing, session_events, webhooks
from services.agents import AgentConfig
from services.billing import SessionUsage, collector_in, llm_usage_lines
from services.user import Tenant
from services.userdata import WINDOW_PARKING_KEY
from services.webhooks import events as webhook_events
from utils.bg import spawn
from utils.latency import compute_latency, reply_breakdowns
from workers.session.events import SessionEventLog
from workers.text.types import TextWindow

logger = logging.getLogger("talqing.workers.text.window")


def emit_session_started(
    tenant: Tenant,
    *,
    session_id: str,
    conversation_id: str | None,
    agent_id: str | None,
    version: int | None,
    channel: str,
    session_type: str,
    events: SessionEventLog,
) -> None:
    """Fire-and-forget session.started, once, when a chat's first window opens.

    Writes both transports of the same moment: the tenant's webhook and the
    durable session timeline.
    """
    events.record(
        session_events.SESSION_STARTED,
        {"agent_id": agent_id, "version": version, "channel": channel, "type": session_type},
    )

    async def _dispatch() -> None:
        try:
            await webhooks.dispatch(
                tenant,
                webhook_events.SESSION_STARTED,
                agent_id,
                {
                    "session_id": session_id,
                    "conversation_id": conversation_id,
                    "agent_id": agent_id,
                    "version": version,
                    "channel": channel,
                    "type": session_type,
                },
            )
        except Exception:
            logger.exception("session.started webhook dispatch failed")

    spawn(_dispatch())


def _window_usage(session: AgentSession, config: AgentConfig | None) -> SessionUsage:
    """This window's LLM spend. Text runs no other kind of model.

    The collector rides on the session's own userdata, so a window that handed
    off to a second agent on a different model reports both — see
    `services.billing.ReportedCost`.
    """
    return SessionUsage(
        llm=llm_usage_lines(
            session.usage.model_usage,
            config.llm if config else None,
            collector_in(session.userdata),
        )
    )


async def flush_window(window: TextWindow) -> None:
    """Write down what this window metered, and settle the chat's bill so far.

    Never raises: this runs on the way out of a window that is closing whatever
    happens here, and a failed flush costs one window's usage rather than the
    chat.
    """
    session_id = window.session_id
    tenant = window.tenant
    try:
        usage = _window_usage(
            window.session, window.config if isinstance(window.config, AgentConfig) else None
        )
        pool = await db.tenant_pool(tenant)
        # Appended, never replaced: many windows meter into one chat.
        await pool.executemany(
            """
            INSERT INTO llm_usage (
                session_id, provider, model, input_tokens, input_cached_tokens,
                input_cache_write_tokens, output_tokens, reported_cost,
                priority, tenant_id
            )
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
            """,
            [
                (
                    session_id,
                    u.provider,
                    u.model,
                    u.input_tokens,
                    u.input_cached_tokens,
                    u.input_cache_write_tokens,
                    u.output_tokens,
                    u.reported_cost,
                    u.priority,
                    tenant.id,
                )
                for u in usage.llm
                if u.input_tokens > 0 or u.input_cached_tokens > 0 or u.output_tokens > 0
            ],
        )
        # Over the whole chat, not this window: the row describes the session.
        if window.events is not None:
            await window.events.drain()
        items = await pool.fetch(
            """
            SELECT id, type, role, created_at, metrics, metadata
            FROM conversation_items
            WHERE session_id = $1 AND tenant_id = $2
            ORDER BY created_at, id
            """,
            session_id,
            tenant.id,
        )
        tool_endings = await pool.fetch(
            "SELECT type, payload, created_at FROM session_events "
            "WHERE session_id = $1 AND tenant_id = $2 AND type = $3",
            session_id,
            tenant.id,
            session_events.TOOL_ENDED,
        )
        # A chat's response time is a message arriving → the first token back,
        # the same breakdown its transcript draws per reply. LiveKit reports no
        # `e2e_latency` for a reply nobody spoke.
        replies = [
            wait.total_ms
            for wait in reply_breakdowns(items, tool_endings).values()
            if wait.kind == "reply"
        ]
        latency = compute_latency(items) | {
            "e2e_latency": round(sum(replies) / len(replies), 1) if replies else None,
            "turns": len(replies),
        }
        usage_reported = await pool.fetchval(
            "SELECT EXISTS (SELECT 1 FROM llm_usage WHERE session_id = $1 AND tenant_id = $2)",
            session_id,
            tenant.id,
        )
        await pool.execute(
            "UPDATE sessions SET metrics = $3::jsonb, updated_at = now() "
            "WHERE id = $1 AND tenant_id = $2",
            session_id,
            tenant.id,
            json.dumps({"usage_reported": usage_reported, **latency}),
        )
        await billing.settle_chat(tenant, session_id, window.window_id)
    except Exception:
        logger.exception("text window flush failed for chat %s", session_id)


async def close_window(window: TextWindow) -> None:
    """Throw a warm window away, leaving whatever it served exactly as it was.

    Idle timeout, a republished agent, a worker shutdown — none of them is the
    end of a chat, so the agent's exit hook does not run. A CoPilot's window has
    no session row behind it and nothing to flush.
    """
    if window.plane.records_session:
        await flush_window(window)
        window.session.userdata[WINDOW_PARKING_KEY] = True
    try:
        await window.session.aclose()
    except Exception:
        logger.exception("text session aclose failed for %s", window.session_id)


async def mark_chat_ended(tenant: Tenant, session_id: str, close_reason: str) -> None:
    """Make a chat's row say it has ended. The first writer wins.

    A chat the API ended (or a newer chat replaced) arrives here already marked,
    with its own reason and time, and neither may be overwritten.
    """
    pool = await db.tenant_pool(tenant)
    await pool.execute(
        """
        UPDATE sessions
        SET status = CASE WHEN status IN ('queued', 'running') THEN 'completed' ELSE status END,
            close_reason = COALESCE(close_reason, $3),
            ended_at = COALESCE(ended_at, now()),
            updated_at = now()
        WHERE id = $1 AND tenant_id = $2
        """,
        session_id,
        tenant.id,
        close_reason,
    )
