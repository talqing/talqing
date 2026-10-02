"""Runtime behavior derived from session type + channel.

Executor is not stored. Call sites branch on these helpers (or on
``channel`` / ``session_type`` in runtime context) instead.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def _session_type_of(runtime: Mapping[str, Any] | None) -> str | None:
    if not runtime:
        return None
    value = runtime.get("session_type")
    return value if isinstance(value, str) and value else None


def _channel_of(runtime: Mapping[str, Any] | None) -> str | None:
    if not runtime:
        return None
    value = runtime.get("channel")
    return value if isinstance(value, str) and value else None


def is_text_runtime(runtime: Mapping[str, Any] | None) -> bool:
    """Text chats (browser, Telegram, etc.)."""
    if _channel_of(runtime) == "text":
        return True
    return _session_type_of(runtime) == "TEXT"


def item_source_for_session_type(session_type: str) -> str:
    """Default conversation_items.source for agent-produced items in a session."""
    if session_type == "WEB":
        return "web"
    if session_type in ("SIP_INBOUND", "SIP_OUTBOUND"):
        return "sip"
    if session_type == "TEXT":
        return "text"
    if session_type == "WHATSAPP_INBOUND":
        return "whatsapp"
    return session_type.lower()
