"""The shapes an AI builder's tool arguments defer to.

A handful of our request types are whole product surfaces, and MCP gives no way
to share definitions between tools — so inlining them everywhere they are
accepted was most of a tool surface that no longer fit in a 200k-context client.
Those arguments carry a stub naming what to fetch instead, and this route serves
it. `api.dataplane.functions.DEFERRED_SCHEMAS` decides which, and is the only
place that has to change to add or remove one.

`describe_function` is the same trade one level up: a shared type cannot be
factored out of a model whose own fields are the weight, so every function on
the surface defers its whole argument schema to this route instead. The two
exceptions, and why there can only be two, are in
`api.dataplane.functions.ALWAYS_INLINE`.

Only the agent-facing view defers: the OpenAPI document and the SDKs generated
from it keep every type in full, and so does request validation.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from api.core.schemas import JsonObject
from api.dataplane.deps import Context, CtxDep
from api.dataplane.functions import SchemaName, require_function, schema_document

router = APIRouter(prefix="/schemas", tags=["schemas"])


class SchemaDocumentResponse(BaseModel):
    name: SchemaName
    # Not `schema`, which shadows a deprecated BaseModel classmethod in
    # Pydantic v2 — and `json_schema` is what a tool's own is already called.
    json_schema: JsonObject


@router.get("/{name}", response_model=SchemaDocumentResponse)
async def describe_schema(name: SchemaName, _ctx: Context = CtxDep):
    """The full JSON Schema of one of the five large request types.

    An argument whose description ends in `describe_schema('X')` carries only a
    placeholder for its shape — fetch the real one here before writing it.

    - `AgentConfig` — a whole agent definition; what `create_agent` takes.
    - `AgentOverride` — the same tree, every field optional: one call, or an update.
    - `AgentTeam` — several agents on one call, handing off to each other.
    - `TaskConfig` — an agent task: inputs, output shape, model stack.
    - `ToolOperations` — the operation tree a custom tool runs.

    These compose, so a document you fetch may itself defer to another.
    """
    return SchemaDocumentResponse(name=name, json_schema=schema_document(name))


class FunctionArgumentsResponse(BaseModel):
    name: str
    # The whole docstring. A tool definition carries only its first paragraph,
    # so this is where the caveats live — what a call costs, what it cannot
    # undo, whose terms it runs under — and it arrives while the caller is
    # still choosing arguments rather than thousands of tokens earlier.
    description: str
    # Named as in SchemaDocumentResponse, for the same reason.
    json_schema: JsonObject


@router.get("/functions/{name}", response_model=FunctionArgumentsResponse)
async def describe_function(name: str, _ctx: Context = CtxDep):
    """The full argument schema of one function, by name.

    Every function's arguments are fetched here rather than shown up front, so
    call this before the first time you use one. Two exceptions, which carry
    their own shape because they would otherwise be uncallable: this function
    and `describe_schema`.
    """
    fn = require_function(name)
    return FunctionArgumentsResponse(
        name=name, description=fn.description, json_schema=fn.full_parameters
    )
