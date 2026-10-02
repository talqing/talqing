"""Conversations service — public surface for inbox, refs, and SSE.

Import what you need from here::

    from services.conversations import ConversationResponse, publish
    from services import conversations
    await conversations.list_conversations(ctx)

Submodules are package-internal; external callers should not import them.
"""

from __future__ import annotations

from .events import channel, get_redis, publish  # noqa: F401
from .keys import (  # noqa: F401
    COPILOT_REF_KINDS,
    CUSTOMER_REF_KINDS,
    PLATFORM_REF_KINDS,
    ConversationRefKind,
    copilot_conversation_key,
    sip_conversation_key,
    stream_conversation_key,
    telegram_conversation_key,
    validate_web_conversation_key,
    whatsapp_conversation_key,
)
from .models import (  # noqa: F401
    ConversationActivity,
    ConversationEvent,
    ConversationEventSnapshot,
    ConversationItemResponse,
    ConversationRefSummary,
    ConversationResponse,
    ConversationSessionResponse,
    ConversationSnapshotEvent,
    ConversationTraceEventResponse,
    ConversationTraceResponse,
    DeliveryError,
    PatchConversationRequest,
    SessionError,
    SessionMetrics,
)
from .refs import (  # noqa: F401
    ConversationRef,
    ConversationRefConflict,
    ConversationThread,
    ensure_conversation_ref,
    ensure_copilot_ref,
    ensure_ref,
    ensure_sip_ref,
    ensure_sip_ref_on_conn,
    ensure_stream_ref_on_conn,
    ensure_thread,
    ensure_web_ref,
    get_ref_by_id,
    get_ref_by_key,
    get_ref_by_kind_bind,
    open_conversation,
    require_ref_by_key,
    web_conversation_key,
)
from .service import (  # noqa: F401
    CONVERSATION_ITEM_COLUMNS,
    get_conversation,
    get_conversation_snapshot,
    get_event_snapshot,
    item_created_event,
    list_conversation_items,
    list_conversation_sessions,
    list_conversation_trace,
    list_conversations,
    patch_conversation,
)
