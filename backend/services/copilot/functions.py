"""Bind exposed API functions as the LLM tools a CoPilot calls.

Two words that are easy to confuse, and both are load-bearing here. A *function*
is one entry on the agent-facing API surface (``api.dataplane.functions``) — the
same surface the MCP server serves to a user's Claude Code or Codex. A *tool* is
what the model is handed, which for us is a LiveKit ``function_tool``. This
module turns each of the former into one of the latter. Neither word means the
platform's own *tools*, the things a user builds in the tool editor and fills
with an operation tree; those are what ``create_tool`` creates.

Each call runs the route's own callable in-process as the signed-in user, so a
CoPilot can do neither more nor less than that user's own API calls.

AgentCoPilot gets the whole surface. ToolCoPilot gets the slice that concerns
tools: it edits one tool, and offering it functions it has no business calling
would only invite calls that fail.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from livekit.agents import RunContext, llm
from livekit.agents.llm import function_tool

from api.dataplane.functions import Function, invoke, registry
from services.user import Context

logger = logging.getLogger("talqing.copilot.functions")


def failure(message: str, errors: list[str] | None = None) -> str:
    """A function failure, in the API's own error shape.

    A failed tool call reads exactly like the HTTP response it stands for, so
    the model sees one error format whichever way it reaches the platform.
    """
    return json.dumps({"detail": {"message": message, "errors": errors or []}})


def _error(exc: HTTPException) -> str:
    detail = exc.detail
    if isinstance(detail, dict) and "message" in detail:
        return failure(str(detail["message"]), list(detail.get("errors") or []))
    return failure(str(detail))


async def execute(function: Function, ctx: Context, args: dict[str, Any]) -> str:
    """Run one function and render its result as the tool message.

    Always returns JSON: validation and permission failures come back as errors
    the model can read and correct, never as a raised exception that would kill
    the turn. Results are returned whole — reads exist to give the model the
    state it needs, so truncating them would blind it.
    """
    try:
        return json.dumps(jsonable_encoder(await invoke(function, ctx, args)), default=str)
    except HTTPException as e:
        logger.info("copilot function %s rejected: %s", function.name, e.detail)
        return _error(e)
    except Exception as e:
        # A bug inside a function must not end the conversation.
        logger.exception("copilot function %s failed", function.name)
        return failure(f"{type(e).__name__}: {e}")


def _raw_schema(function: Function) -> dict[str, Any]:
    return {
        "name": function.name,
        # The summary, not the whole docstring: the rest arrives from
        # `describe_function` with the argument schema, at the moment it is
        # needed. Method and path stay because they are how the model reads
        # read-vs-write-vs-destructive, and they cost a dozen tokens.
        "description": f"{function.method} {function.path}\n\n{function.summary}",
        "parameters": function.parameters,
    }


def build_copilot_functions(
    ctx: Context,
    names: frozenset[str] | None,
) -> list[llm.Tool | llm.Toolset]:
    """Bind exposed functions as raw-schema tools for this turn.

    ``names`` narrows the surface to one CoPilot's slice; ``None`` binds all of
    it. Narrowing only ever removes — ``api.dataplane.functions`` stays the one place
    that decides what an AI builder may reach at all.
    """
    tools: list[llm.Tool | llm.Toolset] = []
    for function in registry().values():
        if names is not None and function.name not in names:
            continue

        async def _dispatch(
            raw_arguments: dict[str, object] | None,
            run_ctx: RunContext,
            *,
            _function: Function = function,
        ) -> str:
            del run_ctx  # functions act as the dashboard user, not on session state
            return await execute(_function, ctx, dict(raw_arguments or {}))

        tools.append(function_tool(_dispatch, raw_schema=_raw_schema(function)))
    return tools
