"""Stable conversation_key formats for each surface.

Clients and webhooks resolve chats by ``conversation_key``. Internal
``conversation_id`` is platform-only. Channel keys use reserved prefixes so
tenant-owned web keys cannot collide with platform-built keys.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

WEB_CONVERSATION_KEY_MAX_LEN = 256

ConversationRefKind = Literal[
    "integration",
    "sip",
    "stream",
    "web",
    "agent_copilot",
    "tool_copilot",
    "task_copilot",
]

# One entry per CoPilot: what it edits is an agent, a tool or a task.
COPILOT_REF_KINDS: tuple[ConversationRefKind, ...] = (
    "agent_copilot",
    "tool_copilot",
    "task_copilot",
)

# Customer inbox / public conversation APIs only.
CUSTOMER_REF_KINDS: frozenset[str] = frozenset({"integration", "sip", "stream", "web"})
# Every platform chat is a CoPilot now.
PLATFORM_REF_KINDS: frozenset[str] = frozenset(COPILOT_REF_KINDS)
ALL_REF_KINDS: frozenset[str] = CUSTOMER_REF_KINDS | PLATFORM_REF_KINDS

# Platform-owned prefixes — web clients must not use these. A copilot's prefix is
# its ref kind, so adding a copilot cannot forget to reserve its key space.
RESERVED_CONVERSATION_KEY_PREFIXES: tuple[str, ...] = (
    "telegram:",
    "whatsapp:",
    "sip:",
    "stream:",
    *(f"{kind}:" for kind in COPILOT_REF_KINDS),
)


def telegram_conversation_key(*, bot_id: str, chat_id: str) -> str:
    bot_id = bot_id.strip()
    chat_id = chat_id.strip()
    if not bot_id or not chat_id:
        raise ValueError("Telegram conversation key requires bot_id and chat_id")
    return f"telegram:{bot_id}:{chat_id}"


def whatsapp_conversation_key(*, sender_e164: str, peer: str) -> str:
    """Business sender number + the customer (E.164, or a WhatsApp username id)."""
    sender = sender_e164.strip()
    peer = peer.strip()
    if not sender.startswith("+") or not peer:
        raise ValueError("WhatsApp conversation key requires an E.164 sender and a peer")
    return f"whatsapp:{sender}:{peer}"


def sip_conversation_key(*, did_e164: str, peer_e164: str) -> str:
    """Business DID + peer phone (caller inbound / callee outbound)."""
    did = did_e164.strip()
    peer = peer_e164.strip()
    if not did or not peer:
        raise ValueError("SIP conversation key requires did_e164 and peer_e164")
    if not did.startswith("+") or not peer.startswith("+"):
        raise ValueError("SIP conversation key requires E.164 numbers with leading +")
    return f"sip:{did}:{peer}"


def stream_conversation_key(*, connection_id: UUID | str, peer: str) -> str:
    """Partner stream connection + whoever is on the other end of that socket.

    ``peer`` is the person's normalized E.164 — the callee on an outbound call —
    when there is a usable one, and ``call:{platform_call_id}`` when there is not.
    Deliberately NOT the SIP rule that refuses a call whose caller cannot be
    identified: a SIP call always HAS a caller number and an unreadable one would
    collapse strangers onto one identity, whereas a Twilio ``<Connect><Stream>``
    legitimately sends none at all. An unidentified stream caller gets a
    conversation of its own with no cross-call history, which is exactly what it is.
    """
    peer = peer.strip()
    if not peer:
        raise ValueError("stream conversation key requires a peer")
    return f"stream:{connection_id}:{peer}"


def copilot_conversation_key(kind: ConversationRefKind, subject_id: UUID | str) -> str:
    """One chat per copilot subject: the ref kind doubles as the key prefix."""
    if kind not in COPILOT_REF_KINDS:
        raise ValueError(f"{kind!r} is not a copilot conversation kind")
    return f"{kind}:{subject_id}"


def validate_web_conversation_key(conversation_key: str) -> str:
    """Return a cleaned web key or raise ValueError."""
    if not isinstance(conversation_key, str):
        raise ValueError("conversation_key must be a string")
    key = conversation_key.strip()
    if not key:
        raise ValueError("conversation_key is required")
    if len(key) > WEB_CONVERSATION_KEY_MAX_LEN:
        raise ValueError(
            f"conversation_key must be at most {WEB_CONVERSATION_KEY_MAX_LEN} characters"
        )
    lower = key.lower()
    for prefix in RESERVED_CONVERSATION_KEY_PREFIXES:
        if lower.startswith(prefix):
            raise ValueError(f"conversation_key must not use reserved prefix {prefix!r}")
    if "\x00" in key:
        raise ValueError("conversation_key contains invalid characters")
    return key
