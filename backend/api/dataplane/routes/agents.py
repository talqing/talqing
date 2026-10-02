"""Agent draft CRUD + publish — HTTP adapter over services.agents."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Query

from api.core.schemas import OkResponse, Page, ValidateResponse
from api.dataplane.deps import Context, CtxDep, WriteCtxDep
from services import agents
from services.agents import (
    AgentResponse,
    AgentVersionDetailResponse,
    CreateAgentRequest,
    PublishAgentResponse,
    UpdateAgentRequest,
)

router = APIRouter(prefix="/agents", tags=["agents"])


@router.get("", response_model=Page[AgentResponse])
async def list_agents(
    ctx: Context = CtxDep,
    limit: int = Query(200, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """List the workspace's agents, newest edit first.

    Each item carries the full draft config and the published version number
    (null while the agent has never been published).
    """
    return await agents.list_agents(ctx, limit, offset)


@router.post("", status_code=201, response_model=AgentResponse)
async def create_agent(body: CreateAgentRequest, bg: BackgroundTasks, ctx: Context = WriteCtxDep):
    """Create an agent from a complete config.

    The config is validated the same way an update is, so unknown models or
    unpublished tool references are rejected here. Names are unique per
    workspace. The agent starts unpublished.
    """
    return await agents.create_agent(body, bg, ctx)


@router.get("/{agent_id}", response_model=AgentResponse)
async def get_agent(agent_id: UUID, ctx: Context = CtxDep):
    """Fetch one agent's draft config, published version and publish history."""
    return await agents.get_agent(agent_id, ctx)


@router.patch("/{agent_id}", response_model=AgentResponse)
async def update_agent(
    agent_id: UUID,
    body: UpdateAgentRequest,
    bg: BackgroundTasks,
    ctx: Context = WriteCtxDep,
):
    """Update the agent's draft config. Send only the fields to change.

    Omitted fields are kept, an explicit null clears a field, objects merge key
    by key and lists replace whole. Writes land on the draft only — live
    traffic keeps running the published version until you publish again.

    Switching `channel` normalizes the media stack: text agents drop stt, tts,
    realtime, greeting, turn handling and avatar.

    Setting `realtime` to a speech-to-speech model makes it the whole pipeline —
    `stt`, `llm` and `tts` are cleared and it handles turn detection itself.
    Realtime agents have no fallback model, cannot use the 'after each user
    turn' hook, and only approximate a fixed greeting.
    """
    return await agents.update_agent(agent_id, body, bg, ctx)


@router.delete("/{agent_id}", response_model=OkResponse)
async def delete_agent(agent_id: UUID, bg: BackgroundTasks, ctx: Context = WriteCtxDep):
    """Delete an agent and its versions permanently; 409 while anything still points at it."""
    return await agents.delete_agent(agent_id, bg, ctx)


@router.post("/{agent_id}/validate", response_model=ValidateResponse)
async def validate_agent(agent_id: UUID, ctx: Context = CtxDep) -> ValidateResponse:
    """Run publish-grade validation on the draft without publishing it.

    Covers everything a draft write checks plus the deeper checks publishing
    adds: a workspace API key for every provider the agent needs, and the
    operation trees of attached and hook tools. Returns errors (blocking) and
    warnings (advisory).
    """
    return await agents.validate_agent(agent_id, ctx)


@router.post("/{agent_id}/publish", response_model=PublishAgentResponse)
async def publish_agent(agent_id: UUID, bg: BackgroundTasks, ctx: Context = WriteCtxDep):
    """Freeze the current draft as a new immutable version and make it live.

    Validation must pass. Publishing pins every attached tool (and lifecycle
    hook) to the tool version that is live right now, so republishing a tool does
    not change this agent's behaviour until the agent is published again.
    """
    return await agents.publish_agent(agent_id, bg, ctx)


@router.get("/{agent_id}/versions/{version}", response_model=AgentVersionDetailResponse)
async def get_agent_version(
    agent_id: UUID, version: int, ctx: Context = CtxDep
) -> AgentVersionDetailResponse:
    """Fetch what one published version of an agent actually contains.

    `get_agent` lists the versions and when each was published; this returns the
    frozen definition itself — prompt, models, turn handling and the tool
    versions it pinned, exactly as they were at that publish. Use it to see what
    changed between two versions, or between a version and the current draft.
    """
    return await agents.get_agent_version(agent_id, version, ctx)


@router.post("/{agent_id}/versions/{version}/rollback", response_model=AgentResponse)
async def rollback_agent_version(
    agent_id: UUID, version: int, bg: BackgroundTasks, ctx: Context = WriteCtxDep
) -> AgentResponse:
    """Put an earlier published version of an agent back into production.

    That version becomes live **immediately** and **replaces the current draft**,
    so any unpublished edits are lost. Calls already in progress finish on the
    version they started.

    No new version is created: `published_version` simply points at the older
    one. The restored draft tracks each tool's latest published version, not the
    ones this version pinned.
    """
    return await agents.rollback_agent_version(agent_id, version, bg, ctx)
