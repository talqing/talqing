"""CoPilot conversation HTTP adapters.

One pair of endpoints per CoPilot, under the id of what it edits:
``/copilot/agents/{agent_id}``, ``/copilot/tools/{tool_id}``,
``/copilot/tasks/{task_id}``. All three stream
the same snapshot/message/turn contract.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter

from api.core.sse import EventStreamResponse, event_stream_responses, redis_event_stream
from api.dataplane.deps import Context, CtxDep, WriteCtxDep
from services import copilot
from services.copilot import (
    AGENT_COPILOT,
    TASK_COPILOT,
    TOOL_COPILOT,
    CopilotEvent,
    CopilotSnapshotEvent,
    CopilotSubject,
    SendMessageRequest,
    SendMessageResponse,
    channel,
)

router = APIRouter(prefix="/copilot", tags=["copilot"])


def _stream(subject: CopilotSubject, subject_id: UUID, ctx: Context) -> EventStreamResponse:
    async def snapshot() -> dict:
        snap = await copilot.get_snapshot(subject, subject_id, ctx)
        return CopilotSnapshotEvent.model_validate(snap.model_dump()).model_dump(mode="json")

    return EventStreamResponse(
        redis_event_stream(channel(subject, str(subject_id)), snapshot=snapshot)
    )


@router.get(
    "/agents/{agent_id}/stream",
    response_class=EventStreamResponse,
    responses=event_stream_responses(CopilotEvent),
)
async def agent_copilot_stream(agent_id: UUID, ctx: Context = CtxDep) -> EventStreamResponse:
    """AgentCoPilot's conversation for one agent, as its editor rail shows it.

    Frames: `snapshot` (the whole conversation, sent first and on every
    reconnect), `message` (one new message), `turn` (that turn's lifecycle —
    `running`, `done`, `error`, `canceled`).
    """
    # Fail-fast 404 before opening the stream.
    await AGENT_COPILOT.require(agent_id, ctx)
    return _stream(AGENT_COPILOT, agent_id, ctx)


@router.post("/agents/{agent_id}/messages", response_model=SendMessageResponse)
async def agent_copilot_send_message(
    agent_id: UUID,
    body: SendMessageRequest,
    ctx: Context = WriteCtxDep,
) -> SendMessageResponse:
    """Say something to AgentCoPilot. The reply arrives on the stream, not here.

    Returns as soon as the turn is queued; watch `turn` and `message` frames
    on the matching `/stream` for what it does.
    """
    return await copilot.send_message(AGENT_COPILOT, agent_id, body, ctx)


@router.get(
    "/tools/{tool_id}/stream",
    response_class=EventStreamResponse,
    responses=event_stream_responses(CopilotEvent),
)
async def tool_copilot_stream(tool_id: UUID, ctx: Context = CtxDep) -> EventStreamResponse:
    """ToolCoPilot's conversation for one tool, as its editor rail shows it.

    Frames: `snapshot` (the whole conversation, sent first and on every
    reconnect), `message` (one new message), `turn` (that turn's lifecycle —
    `running`, `done`, `error`, `canceled`).
    """
    await TOOL_COPILOT.require(tool_id, ctx)
    return _stream(TOOL_COPILOT, tool_id, ctx)


@router.post("/tools/{tool_id}/messages", response_model=SendMessageResponse)
async def tool_copilot_send_message(
    tool_id: UUID,
    body: SendMessageRequest,
    ctx: Context = WriteCtxDep,
) -> SendMessageResponse:
    """Say something to ToolCoPilot. The reply arrives on the stream, not here.

    Returns as soon as the turn is queued; watch `turn` and `message` frames
    on the matching `/stream` for what it does.
    """
    return await copilot.send_message(TOOL_COPILOT, tool_id, body, ctx)


@router.get(
    "/tasks/{task_id}/stream",
    response_class=EventStreamResponse,
    responses=event_stream_responses(CopilotEvent),
)
async def task_copilot_stream(task_id: UUID, ctx: Context = CtxDep) -> EventStreamResponse:
    """TaskCoPilot's conversation for one agent task, as its editor rail shows it.

    Frames: `snapshot` (the whole conversation, sent first and on every
    reconnect), `message` (one new message), `turn` (that turn's lifecycle —
    `running`, `done`, `error`, `canceled`).
    """
    await TASK_COPILOT.require(task_id, ctx)
    return _stream(TASK_COPILOT, task_id, ctx)


@router.post("/tasks/{task_id}/messages", response_model=SendMessageResponse)
async def task_copilot_send_message(
    task_id: UUID,
    body: SendMessageRequest,
    ctx: Context = WriteCtxDep,
) -> SendMessageResponse:
    """Say something to TaskCoPilot. The reply arrives on the stream, not here.

    Returns as soon as the turn is queued; watch `turn` and `message` frames
    on the matching `/stream` for what it does.
    """
    return await copilot.send_message(TASK_COPILOT, task_id, body, ctx)
