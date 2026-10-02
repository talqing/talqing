"""Shared types for the text-worker turn path."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from livekit.agents import Agent, AgentSession, io

from services.agents import AgentConfig
from services.attachments import ImageAttachment
from services.messaging import TextTurnJob, TextTurnResult
from services.tools import HandoffTarget
from services.user import Tenant
from workers.session.events import SessionEventLog
from workers.text.planes import TextPlane


@dataclass(frozen=True)
class TextTurnInput:
    # Usually a turn. The job that ENDS a chat stands in for one where the exit
    # hook's output has to be persisted and delivered, with no item it answers.
    job: TextTurnJob
    tenant: Tenant
    plane: TextPlane
    text: str
    # Images sent with this message, already stored. A caption and its photo are
    # one message, one row and one turn, so they travel together.
    images: tuple[ImageAttachment, ...]
    created_at: datetime
    # Platform only: conversation_refs.bind_id (the CoPilot's subject).
    # Resolved once at load; never stored on the Kafka job.
    subject_id: UUID | None = None
    # Channel turns only: the inbound message's id at the provider (Telegram
    # message_id). None for web / platform turns.
    provider_message_id: str | None = None


@dataclass
class TextWindow:
    """A warm LiveKit session kept in memory between messages.

    An implementation detail and nothing more: it has no row, no webhook, no
    hook and no lifecycle meaning. A tenant's chat outlives any number of them —
    one is built when a message finds none, kept for a minute after a turn, and
    thrown away — and all that closing one does is flush what it metered.

    Tenant product: ``session_id`` is the CHAT, a ``sessions`` row.
    Platform (CoPilot): ``session_id`` is ephemeral in-process tracking only.
    """

    session: AgentSession
    agent: Agent
    session_id: str
    tenant: Tenant
    plane: TextPlane
    # This window's own id. Names the billing settlement its close makes, so a
    # retried flush debits nothing twice.
    window_id: UUID = field(default_factory=uuid4)
    # Whether starting this window starts the session it serves, and so runs the
    # agent's entry. False for every window of a chat after its first.
    run_entry: bool = True
    # The agent holding the floor, kept in step with handoffs: what
    # `warm_still_valid` fingerprints and what items are stamped with.
    agent_id: str | None = None
    agent_version_id: str | None = None
    agent_name: str | None = None
    # Whether that agent is a stored one running its published version, which a
    # republish can move under a warm window. A plan member is pinned and an
    # inline agent has no versions, so there is nothing to re-check for either.
    follows_published: bool = False
    # Tenant billing needs compiled config; platform leaves None.
    config: Any = None
    # The agent the chat ENTERED on, whoever holds it now: its analysis spec and
    # prompt are what the chat is judged against when it ends.
    entry_config: AgentConfig | None = None
    # The MCP servers the frozen config named that resolved at cold start. The
    # ids come from the config, but whether each one resolves is live — a
    # disabled integration drops out and invalidates the window.
    integration_ids: frozenset[UUID] = field(default_factory=frozenset)
    # True when reusing a warm LiveKit session (skip session.start).
    already_started: bool = False
    # Tenant web only: LiveKit token deltas → conversation SSE (not CoPilots).
    text_output: io.TextOutput | None = None
    # Durable runtime trace. None on platform planes: those have no sessions row
    # for the FK to point at.
    events: SessionEventLog | None = None
    # {LiveKit agent id: HandoffTarget}, filled by the compiler as this window
    # hands off or enters a task, and read when the `agent_handoff` item is
    # written. Empty on platform planes, which do neither.
    handoff_targets: dict[str, HandoffTarget] = field(default_factory=dict)
    # Watermark for warm chat-context delta sync: all model-facing DB items with
    # created_at <= this are already in the LiveKit session (microsecond
    # timestamptz). Advanced after each turn that ingests the trigger message.
    ctx_synced_through: datetime | None = None

    def park(self) -> TextWindow:
        """Mark the window warm for the next message (clear per-turn streaming)."""
        self.already_started = True
        self.text_output = None
        return self


@dataclass
class TextTurnOutcome:
    """Result of one turn plus the window to park (or None if closed)."""

    result: TextTurnResult | None
    window: TextWindow | None
    # True when the agent ended the chat on this turn (`end_call`).
    end_chat: bool = False
