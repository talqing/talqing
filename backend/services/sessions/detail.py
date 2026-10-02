"""The reads behind one session's detail view, whichever channel it ran on.

`get_call` and `get_chat_detail` describe a session from the same three things:
its transcript, its trace and its usage. Loading them here is what keeps a call
and a chat from being described by two queries that drift.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from uuid import UUID

import asyncpg

from services.conversations.service import CONVERSATION_ITEM_COLUMNS


@dataclass(frozen=True)
class SessionReads:
    # `CONVERSATION_ITEM_COLUMNS`, oldest first.
    transcript: list[asyncpg.Record]
    events: list[asyncpg.Record]
    # One list per usage table: llm, tts, stt, realtime, avatar.
    usage: dict[str, list[asyncpg.Record]]


async def load_session_reads(pool: asyncpg.Pool, tenant_id: UUID, session_id: UUID) -> SessionReads:
    """One session's transcript, trace and usage, in one round trip."""
    transcript, events, llm, tts, stt, realtime, avatar = await asyncio.gather(
        pool.fetch(
            f"""
            SELECT {CONVERSATION_ITEM_COLUMNS}
            FROM conversation_items
            WHERE session_id = $1 AND tenant_id = $2
            ORDER BY created_at, id
            """,
            session_id,
            tenant_id,
        ),
        pool.fetch(
            "SELECT seq, type, payload, created_at FROM session_events "
            "WHERE session_id = $1 AND tenant_id = $2 ORDER BY seq",
            session_id,
            tenant_id,
        ),
        pool.fetch(
            "SELECT provider, model, input_tokens, input_cached_tokens, "
            "input_cache_write_tokens, output_tokens, reported_cost, purpose, "
            "priority FROM llm_usage WHERE session_id = $1 AND tenant_id = $2 "
            # The session itself first, then the analysis that read it afterwards
            # — the order they happened in. Sorting on `purpose` alone would put
            # 'analysis' first, alphabetically, which is exactly backwards.
            "ORDER BY (purpose <> 'conversation'), provider, model",
            session_id,
            tenant_id,
        ),
        pool.fetch(
            "SELECT provider, model, characters_count, audio_duration, input_tokens, output_tokens "
            "FROM tts_usage WHERE session_id = $1 AND tenant_id = $2",
            session_id,
            tenant_id,
        ),
        pool.fetch(
            "SELECT provider, model, audio_duration, input_tokens, output_tokens, "
            "reported_cost FROM stt_usage WHERE session_id = $1 AND tenant_id = $2",
            session_id,
            tenant_id,
        ),
        pool.fetch(
            "SELECT provider, model, input_text_tokens, input_cached_text_tokens, "
            "input_audio_tokens, input_cached_audio_tokens, output_text_tokens, "
            "output_audio_tokens, session_seconds "
            "FROM realtime_usage WHERE session_id = $1 AND tenant_id = $2",
            session_id,
            tenant_id,
        ),
        pool.fetch(
            "SELECT provider, model, seconds, avatar_id, avatar_session_id "
            "FROM avatar_usage WHERE session_id = $1 AND tenant_id = $2",
            session_id,
            tenant_id,
        ),
    )
    return SessionReads(
        transcript=transcript,
        events=events,
        usage={"llm": llm, "tts": tts, "stt": stt, "realtime": realtime, "avatar": avatar},
    )
