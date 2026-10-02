"""Chats: text sessions with an agent (thin HTTP adapters)."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Body, HTTPException, Query
from fastapi.responses import Response

from api.core.schemas import Page
from api.core.sse import EventStreamResponse
from api.dataplane.deps import (
    ChatAccess,
    ChatReadDep,
    ChatWriteDep,
    Context,
    CtxDep,
    WriteCtxDep,
)
from services.calls.analysis_ops import PreviewAnalysisRequest, PreviewAnalysisResponse
from services.chats import service as svc
from services.chats.models import (
    CHAT_STREAM_EVENTS,
    ChatDetailResponse,
    ChatMessageRequest,
    ChatResponse,
    ChatTokenResponse,
    ChatTurnResponse,
    CreateChatRequest,
)
from services.conversations import ConversationItemResponse

router = APIRouter(prefix="/chats", tags=["chats"])

# The frames of a streamed turn, as the union a generated client narrows on.
_STREAM_SCHEMA = {
    "oneOf": [{"$ref": f"#/components/schemas/{m.__name__}"} for m in CHAT_STREAM_EVENTS],
    "discriminator": {
        "propertyName": "event",
        "mapping": {
            m.model_fields["event"].default: f"#/components/schemas/{m.__name__}"
            for m in CHAT_STREAM_EVENTS
        },
    },
}


@router.post("", response_model=ChatResponse, status_code=201)
async def create_chat(body: CreateChatRequest, ctx: Context = WriteCtxDep) -> ChatResponse:
    """Start a chat with a text agent. Send it messages with `create_chat_message`.

    It stays open until the agent's `end_call` tool or `end_chat` ends it. Starting
    one ends the contact's open chat with the same agent.
    """
    return await svc.create_chat(body, ctx)


@router.get("", response_model=Page[ChatResponse])
async def list_chats(
    ctx: Context = CtxDep,
    limit: int = Query(100, ge=1, le=200),
    offset: int = Query(0, ge=0),
    agent_id: UUID | None = Query(default=None),
    contact_key: str | None = Query(default=None),
    status: Literal["open", "ended", "all"] = Query(default="all"),
) -> Page[ChatResponse]:
    """List chats, newest first."""
    return await svc.list_chats(
        ctx, limit=limit, offset=offset, agent_id=agent_id, contact_key=contact_key, status=status
    )


@router.get("/{chat_id}", response_model=ChatResponse)
async def get_chat(chat_id: UUID, access: ChatAccess = ChatReadDep) -> ChatResponse:
    """Read one chat: its status, cost so far and, once ended, its analysis."""
    return await svc.get_chat(chat_id, access.tenant, customer_only=access.via_token)


@router.get("/{chat_id}/detail", response_model=ChatDetailResponse)
async def get_chat_detail(chat_id: UUID, access: ChatAccess = ChatReadDep) -> ChatDetailResponse:
    """One chat with its health snapshot, cost lines, vars and event trace."""
    if access.via_token:
        raise HTTPException(status_code=403, detail="a chat token cannot read a chat's detail")
    return await svc.get_chat_detail(chat_id, access.tenant)


@router.get("/{chat_id}/items", response_model=Page[ConversationItemResponse])
async def list_chat_items(
    chat_id: UUID,
    access: ChatAccess = ChatReadDep,
    limit: int = Query(200, ge=1, le=500),
    offset: int = Query(0, ge=0),
    order: str = Query(
        "asc",
        pattern="^(asc|desc)$",
        description="desc returns the newest page (chronological within it).",
    ),
) -> Page[ConversationItemResponse]:
    """The messages, tool calls and handoffs of one chat."""
    return await svc.list_chat_items(
        chat_id,
        access.tenant,
        limit=limit,
        offset=offset,
        order=order,
        customer_only=access.via_token,
    )


@router.post(
    "/{chat_id}/messages",
    response_model=ChatTurnResponse,
    responses={
        200: {
            "content": {"text/event-stream": {"schema": _STREAM_SCHEMA}},
            "description": (
                "The turn. With `stream: true`, a `text/event-stream` of `assistant.*` and "
                "`item.created` frames ending in one `turn.result`."
            ),
        }
    },
)
async def create_chat_message(
    chat_id: UUID, body: ChatMessageRequest, access: ChatAccess = ChatWriteDep
) -> ChatTurnResponse:
    """Send a message and get the agent's reply back on this request.

    A newer message supersedes one still being answered: the older request
    returns `status: canceled` with `superseded_by_item_id`.
    """
    if body.stream:
        return EventStreamResponse(  # type: ignore[return-value]
            await svc.stream_message(chat_id, body, access.tenant, customer_only=access.via_token)
        )
    return await svc.send_message(chat_id, body, access.tenant, customer_only=access.via_token)


@router.post("/{chat_id}/end", response_model=ChatResponse, status_code=202)
async def end_chat(chat_id: UUID, access: ChatAccess = ChatWriteDep) -> ChatResponse:
    """End a chat. Its analysis and `session.completed` webhook follow."""
    return await svc.end_chat(chat_id, access.tenant, customer_only=access.via_token)


@router.post("/{chat_id}/analysis", response_model=ChatResponse)
async def rerun_chat_analysis(chat_id: UUID, ctx: Context = WriteCtxDep) -> ChatResponse:
    """Analyse an ended chat again with its agent's saved definition.

    A real model call: the workspace is charged and the chat re-priced.
    """
    return await svc.rerun_chat_analysis(chat_id, ctx)


@router.post("/{chat_id}/analysis/preview", response_model=PreviewAnalysisResponse)
async def preview_chat_analysis(
    chat_id: UUID, body: PreviewAnalysisRequest = Body(...), ctx: Context = WriteCtxDep
) -> PreviewAnalysisResponse:
    """Try an analysis definition against an ended chat. Nothing is saved; costs one model call."""
    return await svc.preview_chat_analysis(chat_id, body, ctx)


@router.post("/{chat_id}/token", response_model=ChatTokenResponse)
async def create_chat_token(chat_id: UUID, ctx: Context = WriteCtxDep) -> ChatTokenResponse:
    """Mint a one-hour browser token for this chat. Mint another to refresh."""
    return await svc.create_chat_token(chat_id, ctx)


@router.delete(
    "/{chat_id}",
    status_code=202,
    response_class=Response,
    responses={202: {"description": "Erase scheduled. No body."}},
)
async def delete_chat(chat_id: UUID, ctx: Context = WriteCtxDep) -> Response:
    """Erase an ended chat's content; the chat and its cost stay. Cannot be undone."""
    await svc.delete_chat(chat_id, ctx)
    return Response(status_code=202)
