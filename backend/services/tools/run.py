"""Run one draft tool and report every step.

The authoring loop this exists to close: a tool is a deterministic function of
its arguments, and the only way to exercise one used to be to publish it, attach
it to an agent, publish the agent, place a call, and say something that made the
model choose it. `validate_tool` catches unknown references and TypeScript that
will not compile, but it never opens a socket — and everything that actually
breaks is downstream of that line.

This runs the **draft**, so you never have to publish a broken tool to find out
it is broken, and it runs it **for real**: the tenant's endpoints, the tenant's
secrets. Nothing is saved, no webhook fires, and nothing is metered.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from jsonschema import Draft202012Validator
from pydantic import BaseModel, Field, field_validator

from api.core.schemas import validation_error
from services.secrets import load_secrets
from services.system_vars import validate_iana_timezone, validate_session_vars
from services.user import Context

from .service import json_schema_errors, stored_tree, transpile_code_ops
from .tree import llm_response, tree_is_silent
from .validate import normalized_tool_schema

# ─────────────────────────────── request / response ──────────────────────────


class RunToolRequest(BaseModel):
    arguments: dict[str, Any] = Field(default_factory=dict)
    # Seeds the session bag, so a tool that reads a value an earlier tool wrote
    # can be exercised in the state it will actually run in.
    userdata: dict[str, Any] = Field(default_factory=dict)
    # There is no agent behind a test run, so the clock has to come from the
    # caller. The dashboard sends the browser's zone and prints which one it
    # used; UTC is what an API or MCP caller gets if it says nothing.
    timezone: str = Field(
        default="UTC",
        description=(
            "The IANA timezone {{system_vars.now}}, {{system_vars.date}} and "
            "{{system_vars.time}} resolve against for this run. On a real call this comes "
            "from the agent."
        ),
    )
    # No declared defaults to merge these over: there is no agent behind a test
    # run, which is the same reason `timezone` is a request field here. Whatever
    # is sent IS the bag.
    vars: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Values for {{vars.name}} in this run. On a real session these come from the "
            "agent's declared defaults with the starting request's values merged over them; "
            "here there is no agent, so what you send is the whole bag."
        ),
    )

    model_config = {"extra": "forbid"}

    @field_validator("vars")
    @classmethod
    def _check_vars(cls, value: dict[str, str]) -> dict[str, str]:
        return validate_session_vars(value)

    @field_validator("timezone")
    @classmethod
    def _check_timezone(cls, value: str) -> str:
        return validate_iana_timezone(value)


class PublishedValueResponse(BaseModel):
    store: str
    key: str
    value: Any = None


class RunStepResponse(BaseModel):
    """One operation's outcome.

    `path` locates the operation in the tree — the node's index, joined to its
    parent by `.then.` or `.else.`, so `2.else.1` is the second child of the
    else-branch of the third operation.
    """

    path: str
    kind: str
    # ok | failed | simulated. `simulated` means the operation needs a live
    # conversation, so its resolved text was recorded instead of performed.
    status: str
    duration_ms: int
    # Kind-specific. http: {request, response}. code: {logs, result}.
    # if: {left, right, op, branch}. A simulated kind: its resolved config.
    detail: dict[str, Any] = Field(default_factory=dict)
    published: list[PublishedValueResponse] = Field(default_factory=list)
    error: str | None = None


class RunToolResponse(BaseModel):
    ok: bool
    duration_ms: int
    error: str | None = None
    # tool_config | tool_auth | endpoint_error | endpoint_unreachable |
    # timeout | code_error | platform | unknown — whose problem the failure is.
    error_type: str | None = None
    # The literal string the agent's model would be handed. Null when it is told
    # nothing at all: a silent tool, a tree that ended the call, or a failed run.
    llm_response: str | None = None
    responses: list[dict[str, Any]] = Field(default_factory=list)
    tooldata: dict[str, Any] = Field(default_factory=dict)
    userdata: dict[str, Any] = Field(default_factory=dict)
    steps: list[RunStepResponse] = Field(default_factory=list)
    # True when the tree reached an `end_call` / `handoff`. Both are simulated,
    # so this is what "the conversation would have moved on here" looks like.
    ended_call: bool = False
    handed_off: bool = False


# ─────────────────────────────────── the run ────────────────────────────────


def _argument_errors(schema: object, arguments: dict[str, Any]) -> list[str]:
    """The same check the model's own tool call goes through
    (``compiler.tools._validate_raw_arguments``), reported as a list."""
    if not isinstance(schema, dict):
        return ["tool json_schema must be a JSON object"]
    validator = Draft202012Validator(normalized_tool_schema(schema))
    errors: list[str] = []
    for e in sorted(validator.iter_errors(arguments), key=lambda x: list(x.absolute_path)):
        path = ".".join(str(p) for p in e.absolute_path)
        errors.append(f"{path}: {e.message}" if path else e.message)
    return errors


async def run_tool(tool_id: UUID, body: RunToolRequest, ctx: Context) -> RunToolResponse:
    pool = await ctx.tenant_pool()
    tool = await pool.fetchrow(
        "SELECT id, name, json_schema, silent, operations "
        "FROM tools WHERE id = $1 AND tenant_id = $2",
        tool_id,
        ctx.tenant.id,
    )
    if not tool:
        raise HTTPException(status_code=404, detail="tool not found")

    tree = stored_tree(tool["operations"])
    if not tree:
        raise HTTPException(status_code=400, detail="add at least one operation before running")

    schema = tool["json_schema"] or {}
    if schema_errors := json_schema_errors(schema):
        raise validation_error(schema_errors, "this tool's parameters are not valid")
    if arg_errors := _argument_errors(schema, body.arguments):
        raise validation_error(arg_errors, "these arguments do not match the tool's parameters")

    # A draft carries only `source_ts`; the compiled JS is frozen at publish. Do
    # the transpile against a copy so a test run never writes to the draft — the
    # same thing `validate_tool` does, which is what lets a draft be run at all.
    tree = deepcopy(tree)
    if code_errors := await transpile_code_ops(tree):
        raise validation_error(code_errors, "this tool has code that does not compile")

    # Imported here, not at module scope: `compiler.operations` imports
    # `services.tools`, so a top-level import would close the cycle. Same idiom
    # `compiler/operations.py` uses for `settings` and the runtime-kind helpers.
    from compiler.dryrun import run_tree
    from compiler.operations import RUNTIME_KEY_SESSION_VARS, RUNTIME_KEY_TIMEZONE

    result = await run_tree(
        tree,
        secrets=await load_secrets(ctx.tenant),
        arguments=body.arguments,
        # A copy: the run writes into this bag exactly as it writes into a live
        # session's, and the caller's request body is not ours to mutate.
        userdata=dict(body.userdata),
        # `code` needs a tenant with an id; nothing here belongs to an agent. No
        # call fields either — there is no call behind a test run, so those keys
        # resolve empty and the panel says so rather than inventing a number. The
        # clock does resolve, on whichever zone the caller named — and `vars` on
        # whatever the caller sent, with no declared defaults under it for the
        # same reason the clock has no agent to come from.
        runtime={
            "tenant": ctx.tenant,
            "agent_id": None,
            RUNTIME_KEY_TIMEZONE: body.timezone,
            RUNTIME_KEY_SESSION_VARS: body.vars,
        },
    )

    # The rule the runtime applies, not a restatement of it — including the
    # silence publishing would derive from the tree, so the panel reports what the
    # model would actually be handed rather than what the box says. A failed run
    # never reaches the model at all; it gets the graceful error instead.
    reply = (
        llm_response(
            silent=tool["silent"] or tree_is_silent(tree),
            end_call=result.ended_call,
            responses=result.responses,
        )
        if result.ok
        else None
    )

    return RunToolResponse(
        ok=result.ok,
        duration_ms=result.duration_ms,
        error=result.error,
        error_type=result.error_type,
        llm_response=reply,
        responses=result.responses,
        tooldata=result.tooldata,
        userdata=result.userdata,
        steps=[
            RunStepResponse(
                path=step.path,
                kind=step.kind,
                status=step.status,
                duration_ms=step.duration_ms,
                detail=step.detail,
                published=[PublishedValueResponse(**p) for p in step.published],
                error=step.error,
            )
            for step in result.steps
        ],
        ended_call=result.ended_call,
        handed_off=result.handed_off,
    )
