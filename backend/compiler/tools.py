"""Compile published tool definitions → LiveKit tools the agent can call.

Each tool becomes ONE `function_tool(raw_schema=...)` backed by a generic
dispatcher that runs its operation tree (compiler/operations.py). Tool-level
long-running/silent and the graceful-error policy are applied here.

Three kinds of tool are built here and they share one namespace, because they
end up in one LiveKit `ToolContext`: the tenant's own published tools, one
`handoff_to_*` per destination, and one per task attachment. Publish validation
checks all three against each other for exactly that reason.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Collection, Sequence
from datetime import UTC, datetime

from jsonschema import (
    Draft202012Validator,
)
from jsonschema import (
    ValidationError as JSONSchemaValidationError,
)
from livekit.agents import RunContext, llm
from livekit.agents.llm import StopResponse, ToolError, ToolFlag, function_tool
from livekit.agents.utils.aio import cancel_and_wait

from compiler.operations import ToolRunResult, execute_tree, perform_handoff
from compiler.speech import disable_interruptions
from services import webhooks
from services.agents import (
    DEFAULT_SUMMARY_PROMPT,
    HandoffTarget,
    PinnedTask,
    TaskConfig,
    handoff_tool_name,
)
from services.recordings import WITHDRAWN_USERDATA_KEY
from services.tools import (
    RESERVED_TOOL_NAMES,
    RuntimeContext,
    ToolDefinition,
    llm_response,
    normalized_tool_schema,
)
from services.user import Tenant
from services.webhooks import events
from utils.bg import spawn

logger = logging.getLogger("talqing.compiler.tools")

# How long a `long_running_task` tool is given to finish before the turn is
# handed back. Short enough to disappear inside the agent's own time-to-first-
# word, long enough that a tool answering (or failing) at once is treated as
# what it is: an ordinary tool call. See the rule in `_dispatch`.
BACKGROUND_GRACE_SECONDS = 1.0

# Handed to the model as the tool's output when the turn IS released. Not
# spoken: the template below wraps it and the model paraphrases. What the agent
# actually SAYS while a background tool runs is steered by the tool's
# description and the agent's prompt, which is where the author's voice belongs
# — see documentation/tools/overview.mdx.
BACKGROUND_DISPATCHED = "Started. It runs in the background and reports back separately."

# Replaces LiveKit's `UPDATE_TEMPLATE`, whose second line reads "The task is
# still running, so DON'T make up or give information not included in the
# message above." That sentence is true when it is written and false for the
# rest of the call — and it is never rewritten, so a model reading back the
# transcript is told the work is in flight long after it finished. This wording
# says the same thing about the same moment without asserting a state that
# expires.
BACKGROUND_UPDATE_TEMPLATE = (
    "The tool `{function_name}` was dispatched: {message}\n"
    "That is not its result. The result arrives later as a separate tool output; "
    "until it does, do not state or guess how it turned out."
)

# `RESERVED_TOOL_NAMES` — the names the runtime injects itself (such as
# `build_stop_recording` below) — is defined once, in
# `services.tools`, because publish validation and this compile-time refusal have
# to be looking at the same list.


def build_stop_recording() -> llm.Tool:
    """The caller's way out of being recorded, mid-call.

    Injected whenever the agent records, disclosure or not: someone can object at
    any point, and the right to object does not depend on us having announced it
    first.

    Stopping is the easy direction. `RecorderIO.aclose()` is public, idempotent,
    and safe here — its IO wrappers stay in the chain and keep passing frames to
    the agent, they just stop accumulating. Starting the recorder late is the
    thing the SDK cannot do, which is why disclosure (spoken before the caller
    says anything) carries the consent story and this only has to end it.
    """

    # `caller_words` exists to stop misfires: the recording is unrecoverable once
    # this runs, and a no-argument tool was being called after unrelated turns.
    # Having to quote the request makes the model ground the call in one.
    @function_tool
    async def stop_recording(context: RunContext, caller_words: str) -> str:
        """Stop recording this call and discard the audio so far. Call it ONLY when
        the user has explicitly said they do not want to be recorded, or asked for
        the recording to be stopped or deleted. Never call it for any other reason:
        not after an error, an interruption, or a change of topic. The call
        continues normally afterwards.

        Args:
            caller_words: The user's exact words asking not to be recorded.
        """
        if not caller_words.strip():
            raise ToolError("Only call this when the user has asked not to be recorded.")
        session = context.session
        if isinstance(session.userdata, dict):
            # The MOMENT, not a flag. Both readers want truthiness, and finalize
            # additionally needs to say when the caller asked — which is the one
            # thing a compliance reader looks for and the session row, written
            # after the call, cannot reconstruct.
            session.userdata[WITHDRAWN_USERDATA_KEY] = datetime.now(UTC).isoformat()
        recorder = session._recorder_io
        if recorder is not None:
            await recorder.aclose()
        logger.info("recording stopped at the caller's request: %r", caller_words)
        return "Recording has been stopped and the audio so far discarded."

    return stop_recording


VOICEMAIL_TOOL_NAME = "voicemail_detected"


def build_voicemail_detected(
    message: str | None, end_on_voicemail: Callable[[str | None], Awaitable[None]]
) -> llm.Tool:
    """The model's way to say "a machine answered", on a call the agent placed.

    ElevenLabs' `voicemail_detection` system tool, rather than LiveKit's AMD
    classifier: the model already hears everything said on the call, so asking it
    costs no extra model call and — the reason it was chosen — no delay before a
    person who picks up hears the greeting. AMD holds every greeting until it has
    classified the other end.

    The ending runs detached (`CallBounds.voicemail`), not awaited here: it
    speaks the message and waits for it to finish, and a tool that waits on
    speech queued behind its own reply is waiting on itself. `StopResponse`
    keeps the model from adding a line of its own on top.
    """
    ending = (
        "Your voicemail message is then left for you, word for word, and the call ends."
        if message
        else "The call then ends without a message."
    )

    async def voicemail_detected() -> None:
        spawn(end_on_voicemail(message))
        raise StopResponse()

    return function_tool(
        voicemail_detected,
        name=VOICEMAIL_TOOL_NAME,
        description=(
            "Call this as soon as it is clear the call was answered by voicemail or an "
            'answering machine rather than a person - a recorded greeting such as "leave a '
            'message after the tone", "the person you are calling is not available", or a '
            f"full or unset mailbox. Wait for the recorded greeting to finish before calling "
            f"it. {ending} Do not call it for a live person, however "
            "hesitant, or for a phone menu that asks you to press a key. Do not say anything "
            "yourself when you call it."
        ),
    )


def _emit(
    tenant: Tenant | None,
    event_type: str,
    agent_id: str | None,
    data: dict[str, object],
) -> None:
    """Fire-and-forget tool lifecycle webhook; never blocks the call."""
    if tenant is None:
        return
    try:
        spawn(webhooks.dispatch(tenant, event_type, agent_id, data))
    except Exception:
        logger.exception("tool webhook dispatch failed")


def _session_id() -> str | None:
    try:
        from livekit.agents import get_job_context

        return get_job_context().room.name
    except Exception:
        return None


def _validate_raw_arguments(tool_def: ToolDefinition, args: dict[str, object]) -> None:
    schema = tool_def["json_schema"]
    if not isinstance(schema, dict):
        raise ToolError(f"Tool {tool_def['name']!r} has a corrupt json_schema (expected object)")
    try:
        Draft202012Validator(normalized_tool_schema(schema)).validate(args)
    except JSONSchemaValidationError as e:
        path = ".".join(str(p) for p in e.absolute_path)
        suffix = f" at {path}" if path else ""
        raise ToolError(f"Tool arguments are invalid{suffix}: {e.message}") from e


def _make_dispatcher(
    tool_def: ToolDefinition,
    secrets: dict[str, str],
    tenant: Tenant | None,
    agent_id: str | None,
    participant_identity: str | None = None,
    runtime_context: RuntimeContext | None = None,
) -> Callable[..., Awaitable[object]]:
    name = tool_def["name"]
    tree = tool_def["operations"]
    long_running_task = tool_def["long_running_task"]
    runtime: RuntimeContext = {
        "tenant": tenant,
        "agent_id": agent_id,
        "participant_identity": participant_identity,
    }
    if runtime_context:
        runtime.update(runtime_context)

    def _failure_payload(
        *,
        sid: object,
        error: str,
    ) -> dict[str, object]:
        return {
            "tool": name,
            "session_id": sid,
            "conversation_id": runtime.get("conversation_id"),
            "error": error,
        }

    # raw-schema tools receive `raw_arguments` (parsed dict) by name; a param
    # type-hinted RunContext gets the live context injected (llm/utils.py).
    async def _dispatch(raw_arguments: dict[str, object] | None, run_ctx: RunContext) -> object:
        sid = runtime.get("session_id") or _session_id()
        args = raw_arguments or {}
        event_payload = {
            "tool": name,
            "session_id": sid,
            "conversation_id": runtime.get("conversation_id"),
            "arguments": args,
        }
        _emit(tenant, events.TOOL_INVOKED, agent_id, event_payload)

        # Text sessions are intentionally scoped to one message. Await the
        # operation tree so its result, userdata, handoff, and conversation items
        # are committed before that session closes; on voice and video the turn
        # may be released while it runs, below.
        from workers.session.runtime_kind import is_text_runtime

        # True once the turn has actually been handed back — a fact about this
        # run, not about the flag. The grace window below is why they differ.
        released = False
        tree_task: asyncio.Task[ToolRunResult] | None = None
        try:
            # Inside the `try` so both refusals reach `tool.failed`: a call the
            # model got wrong is the tool failure a tenant most needs to hear
            # about, and it is still an invocation — `tool.invoked` above
            # carries the arguments that were rejected.
            _validate_raw_arguments(tool_def, args)
            if tool_def["disable_interruptions"]:
                disable_interruptions(run_ctx)
            if long_running_task and not is_text_runtime(runtime):
                tree_task = asyncio.create_task(
                    execute_tree(tree, run_ctx, secrets, args, runtime=runtime),
                    name=f"tool_tree_{name}",
                )
                # `long_running_task` says the work is slow, so give the turn
                # back. Whether it IS slow is only knowable by running it, so the
                # turn is released only once the tree has actually outlasted this
                # window; a tree that finishes inside it answers in its own turn,
                # exactly like any other tool.
                #
                # Not an optimisation — a correctness rule. Releasing the turn
                # for work that is already done inverts the transcript: LiveKit
                # inserts the result the moment it lands, so it precedes the
                # sentence announcing it, `_deliver_reply` then sees newer items
                # and picks REPLY_INSTRUCTIONS_MAYBE_COVERED, and the model —
                # whose own last line says "I'm starting that" — takes the
                # "you may have already mentioned this" escape hatch and says
                # nothing. MEASURED on a live call: a tool that failed 22ms in
                # was never reported to the caller, and the agent went on
                # insisting the work was still running because its own sentence
                # sat later in the context than the failure.
                #
                # `asyncio.wait` rather than `wait_for`: on timeout it leaves the
                # task running, which is the whole point.
                done, _ = await asyncio.wait({tree_task}, timeout=BACKGROUND_GRACE_SECONDS)
                if not done:
                    released = True
                    await run_ctx.update(BACKGROUND_DISPATCHED, template=BACKGROUND_UPDATE_TEMPLATE)
                res = await tree_task
            else:
                res = await execute_tree(tree, run_ctx, secrets, args, runtime=runtime)
        except ToolError as e:
            _emit(
                tenant,
                events.TOOL_FAILED,
                agent_id,
                _failure_payload(
                    sid=sid,
                    error=str(e),
                ),
            )
            raise
        except asyncio.CancelledError:
            # `drain()` cancels a background tool instead of waiting for it
            # (ToolFlag.CANCELLABLE), and so does the model-facing
            # `lk_agents_cancel_task`. The tree runs in a task of our own, so it
            # has to be told as well — otherwise the work this cancellation
            # exists to stop carries on to the end of its own timeout budget.
            if tree_task is not None:
                await cancel_and_wait(tree_task)
            raise
        if res.handoff is not None:
            if released:
                # Refused at publish since this shipped, so this only catches
                # versions frozen before the rule. The ack already went back to
                # the model, so whatever we raise here is narrated to a live
                # caller — hence the same caller-safe line every aborting
                # operation gets (compiler/operations.py), with the real reason
                # in the log where an operator can act on it. Without the guard
                # LiveKit raises its own RuntimeError about handing off after a
                # progress update, which is a worse error in a worse place.
                logger.error(
                    "tool %s is long-running and tried to hand off; refusing — "
                    "republish it to see the publish error",
                    name,
                )
                raise ToolError("Sorry, I wasn't able to complete that just now.")
            # a tool returning exactly one Agent IS the framework hand-off:
            # AgentActivity calls session.update_agent; the target's on_enter speaks
            return res.handoff
        if res.end_call and is_text_runtime(runtime):
            userdata = run_ctx.session.userdata
            if isinstance(userdata, dict):
                userdata["_talqing_end_call_requested"] = True

        # `llm_response` is the one definition of what the model is told, shared
        # with the dashboard's test panel. None means "nothing at all", which the
        # framework spells `StopResponse`.
        #
        # Two things put us here. `tool_def["silent"]` is the author's flag OR'd
        # with the silence publishing derived from the tree, so a tool that only
        # speaks is not also handed `"Done."` to improvise on top of. `end_call`
        # is a per-execution fact rather than a published one, which is what gets
        # `if <done>: end_call else: http GET /status` right for free — the branch
        # that hung up goes quiet, the branch that did not still gets narrated —
        # and what covers tool versions published before the rule existed.
        reply = llm_response(
            silent=tool_def["silent"], end_call=res.end_call, responses=res.responses
        )
        if reply is None:
            raise StopResponse()
        return reply

    return _dispatch


def _raw_schema(tool_def: ToolDefinition) -> dict[str, object]:
    schema = tool_def["json_schema"]
    if not isinstance(schema, dict):
        raise ValueError(f"tool {tool_def['name']!r} has a corrupt json_schema (expected object)")
    return {
        "name": tool_def["name"],
        "description": tool_def["description"],
        "parameters": normalized_tool_schema(schema),
    }


def build_handoff_tools(
    targets: Sequence[HandoffTarget],
    *,
    personalize: Callable[[str], str],
    tenant: Tenant | None = None,
    runtime_context: RuntimeContext | None = None,
    taken_names: Collection[str] = (),
) -> list[llm.Tool]:
    """One tool per handoff destination, named `handoff_to_<slug>`.

    One tool per destination is the shape OpenAI's Agents SDK uses and the one
    Vapi recommends for OpenAI models; the single-tool-with-an-enum alternative
    is deliberately not built, because routing quality lives in the per-tool
    description and one shape is one thing to explain.

    The tool takes no arguments — except on a destination whose `context` is
    `summary`, where it takes exactly one: the summary of the conversation so
    far, written by THIS agent in the same turn as the decision to hand over.
    That is what makes the mode cost nothing beyond the tokens of that turn, and
    what lets a realtime agent use it. The schema is fixed at compile time for
    the whole session either way, so the source agent's cached prompt prefix is
    unaffected.

    The tool's whole body is the handoff: `perform_handoff` returns the compiled
    target, and a tool returning exactly one Agent IS the framework hand-off.

    ``personalize`` substitutes `{{userdata.…}}`, `{{system_vars.…}}` and
    `{{vars.…}}` in the three prose fields, at build time, exactly as the prompt
    and greeting are — the description and the summary prompt are baked into the
    schema the model is shown, and the spoken line is the same kind of authored
    sentence the greeting is.
    """
    tools: list[llm.Tool] = []
    seen: set[str] = set(taken_names) | set(RESERVED_TOOL_NAMES)
    for target in targets:
        name = handoff_tool_name(target.name)
        if name in seen:
            raise ValueError(f"tool name {name!r} is duplicate or reserved — refuse to compile")
        seen.add(name)
        parameters: dict[str, object] = {"type": "object", "properties": {}}
        if target.context == "summary":
            prompt = target.summary_prompt or DEFAULT_SUMMARY_PROMPT.format(name=target.name)
            parameters = {
                "type": "object",
                "properties": {"summary": {"type": "string", "description": personalize(prompt)}},
                "required": ["summary"],
            }
        tools.append(
            function_tool(
                _make_handoff(
                    target,
                    personalize(target.message) if target.message else None,
                    tenant,
                    runtime_context,
                ),
                raw_schema={
                    "name": name,
                    "description": personalize(target.description),
                    "parameters": parameters,
                },
            )
        )
    return tools


def _make_handoff(
    target: HandoffTarget,
    message: str | None,
    tenant: Tenant | None,
    runtime_context: RuntimeContext | None,
) -> Callable[..., Awaitable[object]]:
    # Same shape `_make_dispatcher` builds, and captured by value for the same
    # reason: the context is read when the tool runs, and the caller's dict is
    # not ours to hold a live reference into.
    runtime: RuntimeContext = {"tenant": tenant}
    if runtime_context:
        runtime.update(runtime_context)

    async def _dispatch(raw_arguments: dict[str, object] | None, run_ctx: RunContext) -> object:
        session = run_ctx.session
        userdata = session.userdata if isinstance(session.userdata, dict) else {}
        # Only a `summary` destination declares an argument, and a model that
        # sends something else — or nothing — is treated exactly as a blank
        # summary is: `build_handoff_agent` passes the full transcript instead
        # and logs, rather than failing a handoff the caller is waiting on.
        written = (raw_arguments or {}).get("summary")
        agent, _handle = await perform_handoff(
            session,
            runtime,
            userdata,
            target_agent_id=target.agent_id,
            target_name=None if target.agent_id else target.name,
            context_policy=target.context,
            summary=written if isinstance(written, str) else None,
            recent_turns=target.recent_turns,
            message=message,
        )
        return agent

    return _dispatch


def build_tools(
    tool_defs: Sequence[ToolDefinition],
    secrets: dict[str, str],
    tenant: Tenant | None = None,
    agent_id: str | None = None,
    participant_identity: str | None = None,
    runtime_context: RuntimeContext | None = None,
) -> list[llm.Tool | llm.Toolset]:
    """tool_defs = the compiled `tool_versions.definition` dicts for each attached tool.

    Corrupt / reserved / duplicate names raise — a published definition that
    cannot become a tool is a deploy-time bug and must not partially start a call.
    """
    from workers.session.runtime_kind import is_text_runtime

    # The same predicate `_dispatch` branches on, so the flag and the behaviour
    # cannot disagree: `long_running_task` is a no-op on text, and a text agent
    # should not pay for LiveKit's two auto-injected cancellation tools.
    text = is_text_runtime(runtime_context)

    tools: list[llm.Tool | llm.Toolset] = []
    seen_names: set[str] = set(RESERVED_TOOL_NAMES)
    for td in tool_defs:
        name = td["name"]
        if not name:
            raise ValueError("published tool definition is missing name")
        if name in seen_names:
            raise ValueError(f"tool name {name!r} is duplicate or reserved — refuse to compile")
        seen_names.add(name)
        dispatcher = _make_dispatcher(
            td, secrets, tenant, agent_id, participant_identity, runtime_context
        )
        if td["long_running_task"] and not text:
            # CANCELLABLE is what lets a handoff or a call ending drain this
            # instead of blocking on it — AgentActivity.aclose() awaits
            # non-cancellable tasks under its lock — and it is what makes "stop
            # that" answerable. It also auto-injects lk_agents_cancel_task /
            # lk_agents_get_running_tasks into the schema, which is why both are
            # in RESERVED_TOOL_NAMES.
            tools.append(
                function_tool(
                    dispatcher,
                    raw_schema=_raw_schema(td),
                    flags=ToolFlag.CANCELLABLE,
                )
            )
        else:
            tools.append(function_tool(dispatcher, raw_schema=_raw_schema(td)))
    return tools


def task_tool_schema(task: TaskConfig, answered: Collection[str]) -> dict[str, object]:
    """The parameters the model is shown for one task attachment.

    **A task variable the call can answer for itself is not in the schema at
    all** — not optional, absent, and filled at entry. That is the whole rule,
    and it exists because an optional property is an invitation to invent: a task
    declaring `user_id` on a call that already knows the user must never show
    `user_id` to the model, because a model filling in a tool call will sometimes
    emit `u_12345`, and that value is then either used or silently discarded —
    both wrong. Removing the property removes the failure. MEASURED on a live
    call: shown one property and holding another variable's value, the model
    copied that value straight into it.

    "Can answer for itself" is **every name the calling agent declares, plus
    every name the session was started with** — not only the ones that ended up
    holding a value. A declared variable the caller left blank resolves to empty
    for the agent, so it resolves to empty for the task too; asking the model to
    fill that gap is asking it to invent the one thing nobody on the call knows.
    It is also what the editor promises, which it decides from the agent's
    declarations before any call exists.

    What is left becomes a model argument, `required` when the task marks it
    required and has no default of its own. Every property is `type: "string"` —
    substitution is textual, and a reference number like `007` is not the integer
    7. The task's *output* is typed; its input is not.

    Both halves are fixed at session start, so this schema is fixed for the whole
    call and the prompt-cache prefix is stable.
    """
    asked = [v for v in task.vars if v.name not in answered]
    return {
        "type": "object",
        "properties": {v.name: {"type": "string", "description": v.description} for v in asked},
        "required": [v.name for v in asked if v.required and v.default is None],
        "additionalProperties": False,
    }


def build_task_tools(
    tasks: Sequence[PinnedTask],
    *,
    personalize: Callable[[str], str],
    tenant: Tenant | None = None,
    runtime_context: RuntimeContext | None = None,
    answered: Collection[str],
    taken_names: Collection[str] = (),
) -> list[llm.Tool]:
    """One tool per task attachment: enter it, wait, hand back what it produced.

    No `handoff_to_`-style prefix on the name, unlike a handoff destination. A
    handoff changes who is speaking, permanently; a task call returns, so from
    the model's side this is an ordinary tool — typed in, typed out, control
    comes back. The author names it, exactly as they name a handoff destination,
    so renaming the task cannot change the tool name a published prompt was
    written against.

    ``personalize`` substitutes `{{userdata.…}}`, `{{system_vars.…}}` and
    `{{vars.…}}` in the description and the spoken line, at build time, exactly
    as the prompt and the greeting are — the description is baked into the schema
    the model is shown, and the line is the same kind of authored sentence.

    ``answered`` is every variable name this call can fill on its own — see
    `task_tool_schema`. It is captured into each dispatcher too, because the
    schema and the entry have to agree on that set exactly.
    """
    tools: list[llm.Tool] = []
    seen: set[str] = set(taken_names) | set(RESERVED_TOOL_NAMES)
    for pinned in tasks:
        name = pinned.selection.name
        if name in seen:
            raise ValueError(f"tool name {name!r} is duplicate or reserved — refuse to compile")
        seen.add(name)
        tools.append(
            function_tool(
                _make_task_entry(
                    pinned,
                    personalize(pinned.selection.message) if pinned.selection.message else None,
                    tenant,
                    runtime_context,
                    frozenset(answered),
                ),
                raw_schema={
                    "name": name,
                    "description": personalize(pinned.selection.description),
                    "parameters": task_tool_schema(pinned.config, answered),
                },
            )
        )
    return tools


def _make_task_entry(
    pinned: PinnedTask,
    message: str | None,
    tenant: Tenant | None,
    runtime_context: RuntimeContext | None,
    answered: Collection[str],
) -> Callable[..., Awaitable[object]]:
    # Same shape `_make_dispatcher` builds, and captured by value for the same
    # reason: the context is read when the tool runs, and the caller's dict is
    # not ours to hold a live reference into.
    runtime: RuntimeContext = {"tenant": tenant}
    if runtime_context:
        runtime.update(runtime_context)

    async def _dispatch(raw_arguments: dict[str, object] | None, run_ctx: RunContext) -> object:
        # Local import: `compiler.tasks` builds on `compiler.compile`, which
        # imports this module, so a module-scope import here would be a cycle.
        from compiler.tasks import enter_task

        return await enter_task(
            pinned,
            run_ctx,
            runtime,
            arguments=raw_arguments or {},
            message=message,
            answered=answered,
        )

    return _dispatch
