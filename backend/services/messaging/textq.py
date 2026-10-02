"""Kafka transport for ordered text-agent input items."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from aiokafka import AIOKafkaProducer
from pydantic import BaseModel, model_validator

from settings import get_settings


class TextTurnJob(BaseModel):
    """One unit of work for a conversation's actor: answer a message, or end a chat."""

    tenant_id: UUID
    # Always conversations.id (Kafka partition + actor map + items thread).
    conversation_id: UUID
    # `turn` answers `input_item_id`. `end` finishes the chat `session_id` names:
    # the API has already marked it ended, and the worker owes it its exit hook,
    # its analysis, its bill and its `session.completed`.
    kind: Literal["turn", "end"] = "turn"
    # The inbound item a turn answers. None on an `end` job, which answers nothing.
    input_item_id: UUID | None = None
    # Inbound item created_at (history cutoffs / supersede races in the runtime).
    created_at: datetime
    # The chat this job belongs to — `sessions.id`. None on a CoPilot job: a
    # CoPilot keeps its window in memory and has no session row.
    session_id: UUID | None = None
    # A CoPilot's sentinel id, which is what routes the job to its plane. On a
    # tenant job it is the chat's entry agent (null for one defined inline) and
    # is informational: the worker reads the cast off the chat's session row.
    requested_agent_id: UUID | None = None
    trigger_id: UUID | None = None
    integration_id: UUID | None = None
    # Platform (CoPilot) and provider text set this so the worker can load
    # conversation_refs (bind_id = CoPilot subject / channel identity).
    conversation_ref_id: UUID | None = None
    # Required when requested_agent_id is the platform CoPilot sentinel: tools
    # act as this dashboard user against the same routes as the editor UI.
    user_id: UUID | None = None

    @model_validator(mode="after")
    def _shape(self) -> TextTurnJob:
        if self.kind == "turn" and self.input_item_id is None:
            raise ValueError("a turn job names the item it answers")
        if self.kind == "end" and self.session_id is None:
            raise ValueError("an end job names the chat it ends")
        return self

    @property
    def kafka_key(self) -> bytes:
        return f"{self.tenant_id}:{self.conversation_id}".encode()


@dataclass(frozen=True)
class TextTurnResult:
    """Outcome of one text turn (shared by worker and provider delivery)."""

    input_item_id: UUID
    output_item_ids: list[UUID]
    assistant_text: str | None


_producer: AIOKafkaProducer | None = None
_producer_lock = asyncio.Lock()


async def _get_producer() -> AIOKafkaProducer:
    global _producer
    if _producer is None:
        async with _producer_lock:
            if _producer is None:
                settings = get_settings()
                producer = AIOKafkaProducer(
                    bootstrap_servers=settings.kafka.bootstrap_servers,
                    acks="all",
                    enable_idempotence=True,
                )
                await producer.start()
                _producer = producer
    return _producer


async def publish_turn(job: TextTurnJob) -> None:
    producer = await _get_producer()
    await producer.send_and_wait(
        get_settings().kafka.text_turns_topic,
        key=job.kafka_key,
        value=job.model_dump_json().encode(),
    )


async def close() -> None:
    global _producer
    if _producer is not None:
        await _producer.stop()
        _producer = None
