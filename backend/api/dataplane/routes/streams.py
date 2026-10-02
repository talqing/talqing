"""Stream connections HTTP adapter over services.streams."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Query

from api.core.schemas import OkResponse, Page
from api.dataplane.deps import Context, CtxDep, WriteCtxDep
from services import streams as svc
from services.streams import (
    CreateStreamConnectionRequest,
    PatchStreamConnectionRequest,
    StreamConnectionResponse,
)

router = APIRouter(prefix="/streams", tags=["telephony"])


@router.get("", response_model=Page[StreamConnectionResponse])
async def list_stream_connections(
    ctx: Context = CtxDep,
    limit: int = Query(200, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> Page[StreamConnectionResponse]:
    """List WebSocket media-stream connections a partner can dial."""
    return await svc.list_stream_connections(ctx, limit, offset)


@router.post("", status_code=201, response_model=StreamConnectionResponse)
async def create_stream_connection(body: CreateStreamConnectionRequest, ctx: Context = WriteCtxDep):
    """Create a connection a streaming partner can dial.

    One connection per agent per dialect: the URL names both, so a second would
    only be the same URL again.
    """
    return await svc.create_stream_connection(body, ctx)


@router.get("/{connection_id}", response_model=StreamConnectionResponse)
async def get_stream_connection(connection_id: UUID, ctx: Context = CtxDep):
    """One stream connection, with the URL to hand the partner."""
    return await svc.get_stream_connection(connection_id, ctx)


@router.patch("/{connection_id}", response_model=StreamConnectionResponse)
async def patch_stream_connection(
    connection_id: UUID, body: PatchStreamConnectionRequest, ctx: Context = WriteCtxDep
):
    """Rename a connection, point it at another agent, or disable it.

    Disabling refuses new calls immediately and is the only way to stop a URL
    answering; calls already running are untouched.
    """
    return await svc.patch_stream_connection(connection_id, body, ctx)


@router.delete("/{connection_id}", response_model=OkResponse)
async def delete_stream_connection(connection_id: UUID, ctx: Context = WriteCtxDep):
    """Delete a stream connection. Calls it already carried are kept."""
    return await svc.delete_stream_connection(connection_id, ctx)
