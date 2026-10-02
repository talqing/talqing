"""Agent tasks HTTP adapter over services.tasks."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Body, Query

from api.core.schemas import OkResponse, Page, ValidateResponse
from api.dataplane.deps import Context, CtxDep, WriteCtxDep
from services import tasks as svc
from services.tasks import (
    CreateTaskRequest,
    PatchTaskRequest,
    PublishTaskResponse,
    RunTaskRequest,
    TaskResponse,
    TaskRunResponse,
    TaskVersionDetailResponse,
)

router = APIRouter(prefix="/tasks", tags=["tasks"])


@router.get("", response_model=Page[TaskResponse])
async def list_tasks(
    ctx: Context = CtxDep,
    limit: int = Query(200, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> Page[TaskResponse]:
    """List the workspace's agent tasks.

    A task is an agent nobody talks to: a prompt, one LLM, tools and MCP
    servers, the variables it takes as input, and the typed result it must
    produce. Each item carries the full draft config, the published version
    number (null while it has never been published), and `last_run`.
    """
    return await svc.list_tasks(ctx, limit, offset)


@router.post("", status_code=201, response_model=TaskResponse)
async def create_task(body: CreateTaskRequest, ctx: Context = WriteCtxDep) -> TaskResponse:
    """Create an agent task: an agent nobody talks to, which takes named inputs
    and returns a typed result.

    `output` is what makes this a task rather than a chat agent — declare the
    fields the model must produce and it is given a `submit_result` tool built
    from them. `vars` are its inputs, read as `{{vars.name}}`. The task starts
    unpublished; publish it before running it.
    """
    return await svc.create_task(body, ctx)


@router.get("/{task_id}", response_model=TaskResponse)
async def get_task(task_id: UUID, ctx: Context = CtxDep) -> TaskResponse:
    """Fetch one task's draft config, published version, history and last run."""
    return await svc.get_task(task_id, ctx)


@router.patch("/{task_id}", response_model=TaskResponse)
async def update_task(
    task_id: UUID, body: PatchTaskRequest, ctx: Context = WriteCtxDep
) -> TaskResponse:
    """Update the task's draft config. Send only the fields to change.

    Omitted fields are kept, an explicit null clears a field, objects merge key
    by key and lists replace whole. Writes land on the draft only — runs and
    email batches keep using the published version until you publish again.
    """
    return await svc.update_task(task_id, body, ctx)


@router.delete("/{task_id}", response_model=OkResponse)
async def delete_task(task_id: UUID, ctx: Context = WriteCtxDep) -> OkResponse:
    """Delete a task and its published versions. Its runs survive, still named
    after it."""
    return await svc.delete_task(task_id, ctx)


@router.post("/{task_id}/validate", response_model=ValidateResponse)
async def validate_task(task_id: UUID, ctx: Context = CtxDep) -> ValidateResponse:
    """Run publish-grade validation on the draft without publishing it.

    Covers everything a draft write checks plus what publishing adds: a
    workspace API key for the model, at least one output field, the operation
    trees of the attached tools, and the email batches already drafting with
    this task. Returns errors (blocking) and warnings (advisory).
    """
    return await svc.validate_task(task_id, ctx)


@router.post("/{task_id}/publish", response_model=PublishTaskResponse)
async def publish_task(task_id: UUID, ctx: Context = WriteCtxDep) -> PublishTaskResponse:
    """Freeze the current draft as a new immutable version and make it live.

    Validation must pass. Publishing pins every attached tool to the tool
    version live right now, so republishing a tool does not change this task
    until the task is published again.
    """
    return await svc.publish_task(task_id, ctx)


@router.get("/{task_id}/versions/{version}", response_model=TaskVersionDetailResponse)
async def get_task_version(
    task_id: UUID, version: int, ctx: Context = CtxDep
) -> TaskVersionDetailResponse:
    """Fetch what one published version of a task actually contains.

    `get_task` lists the versions and when each was published; this returns the
    frozen definition itself — prompt, model, output fields and the tool
    versions it pinned, exactly as they were at that publish.
    """
    return await svc.get_task_version(task_id, version, ctx)


@router.post("/{task_id}/versions/{version}/rollback", response_model=TaskResponse)
async def rollback_task_version(
    task_id: UUID, version: int, ctx: Context = WriteCtxDep
) -> TaskResponse:
    """Put an earlier published version of a task back into production.

    That version becomes live **immediately** and **replaces the current draft**,
    so any unpublished edits are lost. No new version is created. The restored
    draft tracks each tool's latest published version, not the ones this version
    pinned.
    """
    return await svc.rollback_task_version(task_id, version, ctx)


@router.post("/{task_id}/runs", response_model=TaskRunResponse)
async def run_task(
    task_id: UUID, body: RunTaskRequest = Body(...), ctx: Context = WriteCtxDep
) -> TaskRunResponse:
    """Run the task once with the variables you supply, and wait for the result.

    **This executes for real**: the tools call your endpoints with your secrets,
    the MCP servers spend your credits, and the model spends your tokens. A task
    that books a slot, charges a card or sends a message will do so.

    Runs the published version unless `version` names another one, or `"draft"`
    to run the unpublished config. `vars` must match what that version declares
    — an unknown name is refused rather than dropped. The response is the whole
    run: the structured `output`, a `trace` of every tool call, the steps used
    and what the tokens cost. On failure `error.type` says whose problem it is.
    Secrets are redacted.
    """
    return await svc.run_task_once(task_id, body, ctx)


@router.get("/{task_id}/runs", response_model=Page[TaskRunResponse])
async def list_task_runs(
    task_id: UUID,
    ctx: Context = CtxDep,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> Page[TaskRunResponse]:
    """This task's runs, newest first, each with its output, trace and cost.

    `task_version` says which published definition each one ran, and is null for
    a run of the draft.
    """
    return await svc.list_task_runs(task_id, ctx, limit, offset)


@router.get("/{task_id}/runs/{run_id}", response_model=TaskRunResponse)
async def get_task_run(task_id: UUID, run_id: UUID, ctx: Context = CtxDep) -> TaskRunResponse:
    """One run in full: what it produced, every tool it called on the way, and
    what it cost.

    This is what answers "why did it write that" for a caller holding one run id
    — an email batch's review table links each drafted row here. Secret values
    are redacted from the trace, and a trace that was cut reports it.
    """
    return await svc.get_task_run(task_id, run_id, ctx)
