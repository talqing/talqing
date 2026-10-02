"""Tools HTTP adapter over services.tools."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Body, Query

from api.core.schemas import OkResponse, Page, ValidateResponse
from api.dataplane.deps import Context, CtxDep, WriteCtxDep
from services import tools as svc
from services.tools import (
    CreateToolRequest,
    PatchToolRequest,
    PublishResponse,
    PublishToolRequest,
    RunToolRequest,
    RunToolResponse,
    ToolResponse,
    ToolVersionDetailResponse,
)

router = APIRouter(prefix="/tools", tags=["tools"])


@router.get("", response_model=Page[ToolResponse])
async def list_tools(
    ctx: Context = CtxDep,
    limit: int = Query(200, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> Page[ToolResponse]:
    """List the workspace's tools with their draft operation trees.

    `published_version` is null for a tool that has never been published; only
    published tools can be attached to an agent or used as a lifecycle hook.
    """
    return await svc.list_tools(ctx, limit, offset)


@router.post("", status_code=201, response_model=ToolResponse)
async def create_tool(body: CreateToolRequest, ctx: Context = WriteCtxDep) -> ToolResponse:
    """Create a draft tool.

    `name` is the function name the agent's LLM sees, so it must be a valid
    identifier and describe the capability. `json_schema` declares the arguments
    that LLM supplies. `operations` may be sent here or added later with a
    patch. The tool is a draft until published.
    """
    return await svc.create_tool(body, ctx)


@router.get("/{tool_id}", response_model=ToolResponse)
async def get_tool(tool_id: UUID, ctx: Context = CtxDep) -> ToolResponse:
    """Fetch one tool: behaviour flags, argument schema, draft operation tree
    and the list of published versions."""
    return await svc.get_tool(tool_id, ctx)


@router.patch("/{tool_id}", response_model=ToolResponse)
async def patch_tool(
    tool_id: UUID, body: PatchToolRequest, ctx: Context = WriteCtxDep
) -> ToolResponse:
    """Update a draft tool. Omitted fields are left unchanged.

    `operations` is the one exception: sending it replaces the entire tree, so
    include every node you want to keep. Changes land on the draft; publish to
    make them live.
    """
    return await svc.patch_tool(tool_id, body, ctx)


@router.delete("/{tool_id}", response_model=OkResponse)
async def delete_tool(tool_id: UUID, ctx: Context = WriteCtxDep) -> OkResponse:
    """Delete a tool and its published versions permanently."""
    return await svc.delete_tool(tool_id, ctx)


@router.post("/{tool_id}/validate", response_model=ValidateResponse)
async def validate_tool(tool_id: UUID, ctx: Context = CtxDep) -> ValidateResponse:
    """Check the draft operation tree without publishing it.

    Reports unknown argument and secret references, unreachable published
    fields, handoff targets that are not published, code that fails to
    transpile, and userdata read before it is written.
    """
    return await svc.validate_tool(tool_id, ctx)


@router.post("/{tool_id}/run", response_model=RunToolResponse)
async def run_tool(
    tool_id: UUID, body: RunToolRequest = Body(...), ctx: Context = WriteCtxDep
) -> RunToolResponse:
    """Run the draft tool once with arguments you supply and see every step.

    This executes for real: HTTP operations call your endpoints with your
    secrets, so a tool that books a slot or charges a card will do so.
    Conversational operations (`say`, `generate_reply`, `add_message`,
    `handoff`, `end_call`, `frontend_rpc`) are recorded with their resolved text
    rather than performed.

    Each step reports the resolved request and response, the branch an `if`
    took, and everything published into `tooldata` or `userdata`. Secrets are
    redacted. Nothing is saved, so there is no need to publish first.
    """
    return await svc.run_tool(tool_id, body, ctx)


@router.post("/{tool_id}/publish", response_model=PublishResponse)
async def publish_tool(
    tool_id: UUID, body: PublishToolRequest, ctx: Context = WriteCtxDep
) -> PublishResponse:
    """Validate the draft and freeze it as a new immutable tool version.

    Required before the tool can be attached to an agent or used as a lifecycle
    hook. Agents already published keep the definition they were published with
    until they are republished.
    """
    return await svc.publish_tool(tool_id, body, ctx)


@router.get("/{tool_id}/versions/{version}", response_model=ToolVersionDetailResponse)
async def get_tool_version(
    tool_id: UUID, version: int, ctx: Context = CtxDep
) -> ToolVersionDetailResponse:
    """Fetch what one published version of a tool actually contains.

    `get_tool` lists the versions and when each was published; this returns the
    frozen definition itself — the arguments, behaviour flags and operation tree
    exactly as they were at that publish. Use it to see what changed between two
    versions, or between a version and the current draft.
    """
    return await svc.get_tool_version(tool_id, version, ctx)


@router.post("/{tool_id}/versions/{version}/rollback", response_model=ToolResponse)
async def rollback_tool_version(
    tool_id: UUID, version: int, ctx: Context = WriteCtxDep
) -> ToolResponse:
    """Put an earlier published version of a tool back into production.

    That version becomes live immediately and **replaces the current draft**, so
    any unpublished edits are lost. No new version is created.

    Agents already published keep the tool definition they were published with,
    so republish them for the rollback to reach live calls.
    """
    return await svc.rollback_tool_version(tool_id, version, ctx)
