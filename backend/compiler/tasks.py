"""Agent tasks: the LiveKit `AgentTask` a Talqing task compiles into, and the
two ways it is started.

A task is an agent with a typed return value. LiveKit's `AgentTask` has two
halves and we use both:

* the **typed result** — `complete(v)`, read back by `session.run(output_type=…)`,
  which is what a standalone run (`POST /tasks/{id}/run`, an email batch's
  drafting pass) has always used; and
* the **floor swap** — `await`ing the instance from inside a tool takes the
  inline slot, pauses the calling agent, makes the task `session.current_agent`,
  and on `complete()` merges its side of the conversation back and resumes the
  caller.

Both start from the same compile, so a task cannot behave one way in the editor
and another on a call. What differs is only how the session is obtained: a
standalone run builds one (`compile_task_session`), and an entry borrows the
caller's (`enter_task`).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Collection, Sequence
from typing import Any
from weakref import WeakKeyDictionary

from jsonschema import Draft202012Validator
from livekit.agents import AgentSession, ModelSettings, RunContext, llm, utils
from livekit.agents.llm import RealtimeModel, ToolError, function_tool
from livekit.agents.llm.utils import make_function_call_output
from livekit.agents.voice.agent import AgentTask

from compiler.compile import (
    AgentTool,
    _cache_key,
    _TalqingAgent,
    node_runtime_context,
    node_tools_and_instructions,
    with_screenshare_instructions,
)
from compiler.factories import build_llm
from compiler.integrations import build_integrations
from compiler.operations import (
    RUNTIME_KEY_HANDOFF_TARGETS,
    RUNTIME_KEY_PROVIDER_KEYS,
    RUNTIME_KEY_SESSION_VARS,
    RUNTIME_KEY_TOOL_SECRETS,
    RUNTIME_KEY_VAR_DEFAULTS,
)
from compiler.speech import speak
from services.agents import (
    FINISH_WITHOUT_RESULT_TOOL,
    SUBMIT_RESULT_TOOL,
    PinnedTask,
    TaskConfig,
    missing_required_vars,
)
from services.billing import USERDATA_REPORTED_COST, ReportedCost, collector_in
from services.faqs import FaqForPrompt
from services.integrations.models import Integration
from services.system_vars import build_vars
from services.tools import (
    HandoffTarget,
    HookTrees,
    RuntimeContext,
    ToolDefinition,
    UserData,
    Vars,
)
from services.user import Tenant
from utils.bg import spawn

logger = logging.getLogger("talqing.compiler.tasks")

# ───────────────────────── what the model is told and shown ─────────────────

# NOTHING is appended to a task's prompt. There used to be a generated paragraph
# here saying that calling `submit_result` is how a run ends, and a sentence
# saying when the model could give up and call `finish_without_result`.
#
# Both are gone, and the argument is the same one that had already pulled the
# output field list out of that paragraph: it is duplication, and the copy in
# the prompt is the harmful one. The prompt is assembled as
# `author's text` + `generated text`, so anything generated lands LAST and wins
# on recency — an author whose task kept abandoning work could write
# "DO NOT FINISH WITHOUT RESULT" in capitals and still be arguing upstream of
# our own sentence. Each tool's `description` says these things once, in the
# schema the model reads them from, and the author owns both descriptions
# (`TaskConfig.submit_result_description`, `finish_without_result_description` —
# required and non-empty, so there is always something there to say it).


def submit_result_schema(cfg: TaskConfig) -> dict[str, Any]:
    """The `submit_result` tool the model is shown, built from the task's output.

    Every field is **required**: an optional one lets a model silently omit the
    address it failed to find, and "omitted" and "not generated yet" then look
    identical to everything downstream. Whether `null` is an accepted answer is
    the field's own `required` flag — a required field refuses it, so the model
    has to produce the value or keep working.

    The tool's own description is the author's and there is no fallback for it.
    Nothing is added to the prompt about finishing, so this sentence is the whole
    of what the model is told about how a run ends — which is why the field is
    required and why publishing checks it again.
    """
    return {
        "name": SUBMIT_RESULT_TOOL,
        "description": cfg.submit_result_description,
        "parameters": {
            "type": "object",
            "properties": {
                field.name: {
                    "type": field.type if field.required else [field.type, "null"],
                    "description": field.description,
                    # An empty string is a model answering without an answer.
                    **({"minLength": 1} if field.required and field.type == "string" else {}),
                }
                for field in cfg.output
            },
            "required": [field.name for field in cfg.output],
            "additionalProperties": False,
        },
    }


# ───────────────────────────── the compiled agent ───────────────────────────


class CompiledAgentTask(_TalqingAgent, AgentTask[dict]):
    """A Talqing task as the LiveKit `AgentTask` it is.

    `_TalqingAgent` comes first so its `on_enter` / `on_exit` /
    `on_user_turn_completed` win over `Agent`'s no-ops, and so
    `super().__init__` below still reaches `AgentTask.__init__` — that mixin
    deliberately defines no `__init__` of its own.

    ``inline`` says which way in this is, and it decides exactly one thing:
    whether `finish_without_result` is injected. A standalone run has no caller
    to change their mind, and a `ToolError` surfacing at `session.run` there
    would be classified `platform` — reported as our fault.
    """

    cfg: TaskConfig

    def __init__(
        self,
        cfg: TaskConfig,
        tools: list[AgentTool],
        instructions: str,
        *,
        inline: bool,
        task_id: str | None = None,
        tool_name: str | None = None,
        agent_id: str | None = None,
        version: int | None = None,
        tenant: Tenant | None = None,
        tool_secrets: dict[str, str] | None = None,
        hook_trees: HookTrees | None = None,
        participant_identity: str | None = None,
        defer_entry: bool = False,
        chat_ctx: llm.ChatContext | None = None,
        runtime_context: RuntimeContext | None = None,
        overrides: dict[str, object] | None = None,
        entry_call_id: str | None = None,
    ) -> None:
        self.payload: dict[str, Any] | None = None
        self._tool_name = tool_name or cfg.name
        self._entry_call_id = entry_call_id
        prompt = with_screenshare_instructions(instructions, runtime_context)
        # Built before `super().__init__`, because LiveKit takes the tool list
        # there — so `cfg` is read from the argument rather than from `self.cfg`,
        # which `_bind` has not set yet.
        injected: list[AgentTool] = [self._submit_result_tool(cfg)]
        if inline:
            injected.append(self._finish_without_result_tool(cfg))
        super().__init__(
            instructions=prompt,
            tools=[*tools, *injected],
            **({"chat_ctx": chat_ctx} if chat_ctx is not None else {}),
            **(overrides or {}),
        )
        # LiveKit's `AgentTask.__init__` takes no `id`, so its default is the
        # class name — which would make every task on a call answer to
        # `compiled_agent_task`, including in the two `AgentHandoff` items that
        # bracket it in the transcript. The stored task's id is the stable,
        # unique answer, and an inline task falls back to the tool name the
        # author gave the attachment. Assigning the private is the only way in;
        # `Agent.id` is read-only.
        self._id = task_id or self._tool_name
        self._bind(
            cfg,
            # The agent that ENTERED this task, never the task itself. A task has
            # no `agents` row, and `conversation_items.agent_id` is a uuid
            # foreign key to that table — but the attribution is also simply
            # right: a sub-conversation is still that agent's stretch of the
            # call, which is what lets a reader collapse it. None on a standalone
            # run, where there is no agent at all.
            agent_id=agent_id,
            version=version,
            tenant=tenant,
            tool_secrets=tool_secrets,
            hook_trees=hook_trees,
            participant_identity=participant_identity,
            defer_entry=defer_entry,
            runtime_context=runtime_context,
        )

    # A task has no greeting (`_greeting_line` stays the mixin's ""), so entry
    # always falls through to a generated opening turn — with no instructions of
    # ours. A task always sees the whole transcript, so there is nothing about its
    # context to tell it, and how it opens is its prompt's job. A generic nudge
    # here competes with that prompt on recency: "call submit_result as soon as
    # you have it" once made a booking task that already had its inputs submit
    # an all-null result on entry, before asking the caller anything.
    def _entry_instructions(self) -> str | None:
        return None

    def llm_node(
        self,
        chat_ctx: llm.ChatContext,
        tools: list[llm.Tool],
        model_settings: ModelSettings,
    ) -> Any:
        # Before every inference LiveKit adds each still-running tool call to the
        # context with the output "The tool call is still in progress.", so the
        # model does not re-issue it. An inline task runs INSIDE such a call — the
        # one that entered it — so it was shown itself as a busy tool, and read that
        # as its booking tool being unavailable: it quit before asking anything
        # (measured, SimUp calls d433cadc and the one after). The task's own context
        # never holds its caller's calls, so this id matches only that placeholder.
        if self._entry_call_id is not None:
            chat_ctx.items[:] = [
                item
                for item in chat_ctx.items
                if getattr(item, "call_id", None) != self._entry_call_id
            ]
        return super().llm_node(chat_ctx, tools, model_settings)

    # `_entry_recording_policy` stays the mixin's `False`, and that is a decision
    # rather than an omission: a task inherits the call's recording and never
    # touches the recorder. It also makes `CompiledAgent._follow_recording_policy`
    # unreachable from here. The consequence is documented, not softened — a task
    # that collects a card number IS on the tape if the call is recorded. See
    # `documentation/agents/agent-tasks.mdx`.

    def _submit_result_tool(self, cfg: TaskConfig) -> llm.Tool:
        schema = submit_result_schema(cfg)
        validator = Draft202012Validator(schema["parameters"])

        # `raw_arguments` by name is how LiveKit hands a raw-schema tool its
        # parsed arguments; a parameter hinted `RunContext` gets the live context
        # injected. Same contract every Talqing tool is dispatched under.
        async def submit(raw_arguments: dict[str, Any] | None, _ctx: RunContext) -> None:
            if self.done():
                raise ToolError("the result has already been submitted; do not call this again")
            args = raw_arguments or {}
            errors = [
                f"{'.'.join(str(p) for p in e.absolute_path)}: {e.message}".lstrip(": ")
                for e in sorted(validator.iter_errors(args), key=lambda e: list(e.absolute_path))
            ]
            if errors:
                raise ToolError(
                    f"the result does not match the expected fields: {'; '.join(errors)}"
                )
            self.payload = args
            self.complete(args)
            # None, so no reply is generated: this is over the moment the result
            # is in, and anything said afterwards is tokens nobody reads.
            return None

        return function_tool(submit, raw_schema=schema)

    def _finish_without_result_tool(self, cfg: TaskConfig) -> llm.Tool:
        """The way out when the caller changes their mind.

        LiveKit's own pattern, shipped twice in its reference examples
        (`get_card.py`'s `decline_card_capture`, `task_group.py`'s
        `out_of_scope`): complete with a `ToolError`, which surfaces at the
        calling agent's `await` as an ordinary tool failure. The agent already
        has the sub-conversation in its context by then, so it knows what
        happened without the caller repeating themselves.

        The name pairs with `submit_result` — both are *finish* + *result* — and
        describes what happens rather than how the model feels about it.
        `give_up` carries valence a model may resist acting on, `abort_task`
        leaks our own noun (from the model's side this is not "a task", it is
        collecting an address), and anything starting `end_` sits too close to
        the hang-up tool the agent may already have.

        The description is the only thing telling the model when this is allowed,
        which is why the author writes it. A task whose caller must be talked out
        of leaving and one whose caller may leave at a word are the same shape
        with two different sentences here.
        """
        schema = {
            "name": FINISH_WITHOUT_RESULT_TOOL,
            "description": cfg.finish_without_result_description,
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {"type": "string", "description": "Why this could not finish."}
                },
                "required": ["reason"],
                "additionalProperties": False,
            },
        }

        async def finish(raw_arguments: dict[str, Any] | None, _ctx: RunContext) -> None:
            if self.done():
                raise ToolError("this has already finished; do not call this again")
            reason = str((raw_arguments or {}).get("reason") or "").strip()
            self.complete(
                ToolError(f"{self._tool_name} was not completed: {reason or 'no reason given'}")
            )
            return None

        return function_tool(finish, raw_schema=schema)


# ──────────────────────────────── compiling ─────────────────────────────────


def compile_task_agent(
    cfg: TaskConfig,
    *,
    provider_keys: dict[str, str],
    inline: bool,
    tool_defs: Sequence[ToolDefinition] = (),
    integrations: Sequence[Integration] = (),
    connected_toolsets: Sequence[llm.Toolset] = (),
    faqs: Sequence[FaqForPrompt] = (),
    spoken: bool,
    tool_secrets: dict[str, str] | None = None,
    hook_trees: HookTrees | None = None,
    tenant: Tenant | None = None,
    task_id: str | None = None,
    tool_name: str | None = None,
    agent_id: str | None = None,
    version: int | None = None,
    runtime_id: str | None = None,
    userdata: UserData | None = None,
    chat_ctx: llm.ChatContext | None = None,
    participant_identity: str | None = None,
    defer_entry: bool = False,
    inherit_llm: bool = False,
    runtime_context: RuntimeContext | None = None,
    entry_call_id: str | None = None,
) -> CompiledAgentTask:
    """One task, compiled into the agent that runs it.

    The same assembly `compile_node_agent` does for a handoff target, minus every
    media override: a task declares no STT, TTS, realtime model, language or turn
    handling, so `AgentActivity` resolves all of them to the **session's own
    instances** (`agent_activity.py`'s `self._agent.stt if is_given(...) else
    self._session.stt`). Entered mid-call that means no pipeline is torn down and
    rebuilt — the task speaks with the call's voice and hears with its ears.

    The one override it does keep is `llm`, because it is the only one that costs
    nothing: an LLM is stateless per request, so swapping a cheap model in for a
    simple collection rebuilds nothing. A standalone run needs it anyway, having
    no session to inherit from.

    ``inherit_llm`` is the exception, and it is not optional. On a **realtime**
    call the session's model IS the ears and the mouth, and the task declares no
    STT and no TTS — so putting a text LLM on the agent would leave the activity
    with a model that cannot hear, a caller it cannot answer, and nothing to
    synthesize with. There the task runs the call's own speech-to-speech model
    and its declared `llm` is ignored, which is the only coherent answer: a task
    inherits the whole stack, and on that pipeline the stack is one object.
    """
    runtime_context = node_runtime_context(cfg, runtime_context)
    tools, instructions = node_tools_and_instructions(
        cfg,
        tool_defs,
        integrations,
        tool_secrets or {},
        tenant,
        # The agent that entered this task, so its tools' webhooks name the same
        # agent the transcript does. None on a standalone run.
        agent_id,
        userdata or {},
        participant_identity=participant_identity,
        runtime_context=runtime_context,
        faqs=faqs,
        spoken=spoken,
    )
    # Integrations already built and connected, standing in for `integrations`.
    tools = [*tools, *connected_toolsets]
    return CompiledAgentTask(
        cfg,
        tools=tools,
        instructions=instructions,
        inline=inline,
        task_id=task_id,
        tool_name=tool_name,
        agent_id=agent_id,
        version=version,
        tenant=tenant,
        tool_secrets=tool_secrets or {},
        hook_trees=hook_trees or {},
        participant_identity=participant_identity,
        defer_entry=defer_entry,
        chat_ctx=chat_ctx,
        runtime_context=runtime_context,
        entry_call_id=entry_call_id,
        overrides={}
        if inherit_llm
        else {
            "llm": build_llm(
                cfg.llm,
                provider_keys,
                cache_key=_cache_key(runtime_id),
                has_tools=bool(tools),
                # Entered mid-call this is the calling session's collector, off
                # the live userdata; on a standalone run it is the one
                # `compile_task_session` seeded. Either way the task's own spend
                # lands where that session's finalize will read it.
                reported_cost=collector_in(userdata),
            )
        },
    )


def compile_task_session(
    cfg: TaskConfig,
    provider_keys: dict[str, str],
    *,
    tool_defs: Sequence[ToolDefinition] = (),
    integrations: Sequence[Integration] = (),
    faqs: Sequence[FaqForPrompt] = (),
    tool_secrets: dict[str, str] | None = None,
    hook_trees: HookTrees | None = None,
    tenant: Tenant | None = None,
    task_id: str | None = None,
    runtime_id: str | None = None,
    runtime_context: RuntimeContext | None = None,
) -> tuple[AgentSession[UserData], CompiledAgentTask]:
    """A standalone run: the task, plus the session it is the only agent on.

    No media of any kind — an LLM, its tools and nobody to talk to. `max_steps`
    is the author's, minus one: LiveKit forces a tool-less final response once
    `speech_handle.num_steps >= max_tool_steps + 1`, so its knob is one less than
    the number of LLM -> tools -> LLM rounds it actually allows.

    `defer_entry` is True so the caller can run entry after `session.start`,
    exactly as the voice and text workers do for a root agent — `run_task` calls
    `run_entry(initial=True)` there, which fires the `on_enter` hook and speaks
    nothing. The opening turn is for a conversation; a headless run opens with
    the values it was given as its first user message.
    """
    # ONE dict for the agent and the session, so the collector the task's model
    # writes into is the one `run_task` reads at the end. Two empty dicts here
    # would meter a standalone run at nothing.
    userdata: UserData = {USERDATA_REPORTED_COST: ReportedCost()}
    agent = compile_task_agent(
        cfg,
        provider_keys=provider_keys,
        inline=False,
        tool_defs=tool_defs,
        integrations=integrations,
        faqs=faqs,
        # A headless run: nobody is listening, so there is nothing to say
        # while an answer is fetched.
        spoken=False,
        tool_secrets=tool_secrets,
        hook_trees=hook_trees,
        tenant=tenant,
        task_id=task_id,
        runtime_id=runtime_id,
        # `userdata` starts empty of the caller's values. It is the tools' own
        # scratch space, and nothing about the input contract belongs in it —
        # putting it there is what would let a task's own tools overwrite,
        # mid-run, the input it was given. The one thing in it is platform
        # plumbing under a `_talqing` key, as it is on every other session.
        userdata=userdata,
        defer_entry=True,
        runtime_context=runtime_context,
    )
    session: AgentSession[UserData] = AgentSession(
        llm=agent.llm,
        userdata=userdata,
        max_tool_steps=cfg.max_steps - 1,
    )
    return session, agent


# ──────────────────────────────── entering one ──────────────────────────────


def resolve_entry_vars(
    task: TaskConfig, caller_vars: Vars, arguments: dict[str, object], answered: Collection[str]
) -> Vars:
    """The `{{vars.*}}` bag this entry runs on.

    One namespace, one extra layer on `build_vars`::

        the task's own declared default
          <- overridden by the bag the CALLING AGENT reads as {{vars.*}}
             (its declared defaults, with the call's values merged over them)
          <- overridden by whatever the model supplied.

    The middle layer is the calling agent's *resolved* bag rather than the raw
    session values, and that is deliberate. Everywhere else — a handoff, a team
    member — an agent contributes only its own defaults, because those agents are
    peers with declarations of their own. A task is not a peer: it is a
    subroutine the agent calls, and "the call already knows the customer id"
    means the value the calling agent would itself substitute. The editor says
    *"from the call"* about exactly this bag, and it has to be telling the truth.

    The last two layers can never collide — `task_tool_schema` removes from the
    tool schema every var this bag already supplies, so the model can only ever
    name one nothing else filled. There is no precedence question to answer and
    no field to configure one.

    `undeclared_vars` deliberately does NOT apply here. That is request
    validation for `POST /tasks/{id}/run`; entered by an agent there is no
    request, and the bag is full of names this task never declared and never
    asked for.
    """
    # Exactly the properties `task_tool_schema` offered, and nothing else — the
    # same `answered` set, which is why it is passed rather than recomputed. A
    # key outside it is a model inventing one (MEASURED: given `customer_id` and
    # asked for `name`, a model sent the customer id as the name), and letting it
    # win would put invented text where the call's own value belongs.
    askable = {v.name for v in task.vars if v.name not in answered}
    bag = build_vars({v.name: v.default for v in task.vars if v.default is not None}, caller_vars)
    bag.update(
        {
            name: str(value)
            for name, value in arguments.items()
            if name in askable and isinstance(value, str | int | float | bool)
        }
    )
    return bag


# ─────────────────────────── connecting ahead of entry ────────────────────────

# Per call: task name → its MCP integrations, being connected for its first entry.
_warm_toolsets: WeakKeyDictionary[AgentSession, dict[str, asyncio.Task[list[llm.Toolset]]]] = (
    WeakKeyDictionary()
)


def warm_task_toolsets(
    session: AgentSession,
    tasks: Sequence[PinnedTask],
    *,
    tenant: Tenant,
    tool_secrets: dict[str, str],
) -> None:
    """Start connecting every task's MCP integrations, so entering one does not wait.

    A hosted MCP server is connected when the task's activity starts: a session
    opened, initialized and its tools listed before the task can say a word.
    Calendly measured 2.2-2.6 s for that against 0.2 s for a plain request, and
    the caller sat through all of it at the handoff. Done here instead, it
    overlaps the conversation that comes before the task.

    Each connection serves ONE entry: leaving a task closes its toolsets
    (`AgentActivity.aclose`), so a second entry connects on the spot as before.
    What is never entered is closed with the session.
    """
    warm = _warm_toolsets.setdefault(session, {})
    for pinned in tasks:
        name = pinned.selection.name
        if pinned.config.mcps and name not in warm:
            warm[name] = asyncio.create_task(
                _connect_task_toolsets(pinned, tenant, tool_secrets),
                name=f"warm_task_toolsets_{name}",
            )
    if warm:
        session.once("close", lambda _ev: _close_unused(warm))


async def _connect_task_toolsets(
    pinned: PinnedTask, tenant: Tenant, tool_secrets: dict[str, str]
) -> list[llm.Toolset]:
    # Lazy for the same cycle `build_task_agent` defers it for.
    from workers.session import persistence

    integrations = await persistence.load_mcp_integrations(tenant, pinned.config.mcps)
    toolsets = build_integrations(integrations, tool_secrets, tenant=tenant)
    try:
        await asyncio.gather(*(toolset.setup() for toolset in toolsets))
    except BaseException:
        await asyncio.gather(*(toolset.aclose() for toolset in toolsets), return_exceptions=True)
        raise
    return toolsets


async def _take_warm_toolsets(session: AgentSession, name: str) -> list[llm.Toolset] | None:
    """The connected toolsets for this entry, or None to connect on the spot."""
    warm = _warm_toolsets.get(session, {}).pop(name, None)
    if warm is None:
        return None
    try:
        return await warm
    except Exception:
        logger.warning("connecting task %r ahead of entry failed", name, exc_info=True)
        return None


def _close_unused(warm: dict[str, asyncio.Task[list[llm.Toolset]]]) -> None:
    async def close(task: asyncio.Task[list[llm.Toolset]]) -> None:
        if not task.done():
            task.cancel()
            return
        if task.cancelled() or task.exception() is not None:
            return
        await asyncio.gather(*(t.aclose() for t in task.result()), return_exceptions=True)

    for task in warm.values():
        spawn(close(task))
    warm.clear()


async def enter_task(
    pinned: PinnedTask,
    run_ctx: RunContext,
    runtime: RuntimeContext,
    *,
    arguments: dict[str, object],
    message: str | None,
    answered: Collection[str],
) -> object:
    """Enter one task from the calling agent's tool call, and return its result.

    `await`ing the `AgentTask` is legal here because LiveKit marks every
    tool-execution task `inline_task=True` with its `function_call` attached
    (`voice/generation.py`), which is the precondition `AgentTask.__await_impl`
    checks. It pauses the calling agent's activity, makes this the current agent,
    and on `complete()` merges the sub-conversation back into the caller's
    context — messages only, function calls excluded — and resumes it without
    re-running its `on_enter`, so the agent does not re-greet.
    """
    session = run_ctx.session
    task_cfg = pinned.config
    # The same expression `node_tools_and_instructions` built this tool's schema
    # from, read off the same captured context — so a var the schema hid because
    # the call already supplies it is a var that is actually filled here.
    caller_vars = build_vars(
        runtime.get(RUNTIME_KEY_VAR_DEFAULTS), runtime.get(RUNTIME_KEY_SESSION_VARS)
    )
    entry_vars = resolve_entry_vars(task_cfg, caller_vars, arguments, answered)
    # Only over what the model was actually ASKED for. A required variable the
    # calling agent declares is the agent's own door rule — checked when the
    # session started, and resolving to empty inside it exactly as `{{vars.x}}`
    # does everywhere else. Refusing here would kill a task over a blank the
    # model was never offered a way to fill. What is left is the real backstop:
    # a model that omitted a required property it WAS shown, told which one, free
    # to call again.
    asked_for = [v for v in task_cfg.vars if v.name not in answered]
    if missing := missing_required_vars(asked_for, entry_vars):
        raise ToolError(
            f"cannot start '{pinned.selection.name}': no value for {', '.join(missing)}"
        )

    # Queued before the agent is built, exactly as a handoff's line is: building
    # the task loads its tools and compiles its prompt, and the caller should
    # hear "one moment" during that rather than after it.
    if message:
        speak(session, message)
    agent = await build_task_agent(
        pinned,
        session,
        runtime,
        entry_vars=entry_vars,
        entry_call_id=run_ctx.function_call.call_id,
    )

    # `timeout_seconds` is the only bound on how long a task may hold the caller,
    # so it is enforced with the framework's own `cancel()` rather than with
    # `asyncio.wait_for`. The difference matters: `cancel()` interrupts whatever
    # the task is saying and then completes its future with a `ToolError`, which
    # is what resumes the calling agent promptly and tells its model why;
    # `wait_for` would abandon the await while the task's activity drained the
    # sentence it was in the middle of, and the caller would sit through it with
    # nobody listening.
    timer = asyncio.get_running_loop().call_later(task_cfg.timeout_seconds, agent.cancel)
    try:
        result = await agent
    except ToolError as err:
        await _repeat_after_task(run_ctx, output=None, exception=err)
        raise
    finally:
        timer.cancel()
    await _repeat_after_task(run_ctx, output=result, exception=None)
    return result


async def _repeat_after_task(
    run_ctx: RunContext, *, output: object, exception: ToolError | None
) -> None:
    """Repeat the entry call and its result after the task's conversation.

    Providers want a result directly after its call, so LiveKit files this call's
    output at the moment the model made the call — before everything said inside
    the task, which the return merged in after it. The caller's model then reads
    the result first and the task's last user turn ("yes, book two pm") last,
    unanswered, and enters the task again (measured on a SimUp call: the demo was
    booked twice). The copy puts the result last. Runtime context only: nothing
    persists it, and the original pair stays where it is.
    """
    call = run_ctx.function_call
    repeat = llm.FunctionCall(
        call_id=utils.shortuuid("call_"), name=call.name, arguments=call.arguments
    )
    result = make_function_call_output(fnc_call=repeat, output=output, exception=exception)
    # `enter_task` returns only after the task has handed back, so the current
    # agent is the caller again.
    agent = run_ctx.session.current_agent
    chat_ctx = agent.chat_ctx.copy()
    chat_ctx.insert([repeat, result.fnc_call_out])
    await agent.update_chat_ctx(chat_ctx)


async def build_task_agent(
    pinned: PinnedTask,
    session: AgentSession[UserData],
    runtime: RuntimeContext,
    *,
    entry_vars: Vars,
    entry_call_id: str,
) -> CompiledAgentTask:
    """Compile the task this call is about to enter.

    Shaped on `compiler.handoff.build_handoff_agent` — the same loads, failing
    the same way — and different in two ways that matter.

    **Context.** A task always starts with the conversation so far, minus tool
    mechanics, and there is no policy to choose. A handoff has three because it
    is permanent: how much the target knows shapes the rest of the call. A task
    is a bounded detour that hands back, and a task that does not know what was
    just said asks the caller to repeat it — the worst thing a voice agent does.
    LiveKit agrees in practice: every call site in its reference example passes
    `speech_only(self.chat_ctx)`, five of five. It also keeps the boundary
    symmetric, which is the real argument — the way *out* is fixed by
    `AgentTask`'s merge (`exclude_function_call=True`), so one fixed rule going
    in makes the whole boundary one sentence: **messages always cross, function
    calls never do, in either direction.**

    **Variables.** `RUNTIME_KEY_SESSION_VARS` is rebound for this subtree only.
    Everywhere else it is set once by the worker and never touched, because those
    values belong to the session; here the task's own bag — its defaults, the
    call's values, the model's arguments — is what its prompt and its tools must
    resolve against. It cannot leak back: the calling agent resumes with its own
    agent object and its own captured context.
    """
    # Lazy import: `workers.session.persistence` is the shared data-access layer,
    # and importing it at module scope cycles back through the workers into the
    # compiler. Same reason `compiler.handoff` defers it.
    from workers.session import persistence
    from workers.session.runtime_kind import is_text_runtime

    tenant = runtime.get("tenant")
    if not isinstance(tenant, Tenant):
        raise ToolError("I'm sorry, I can't do that right now.")
    provider_keys = runtime.get(RUNTIME_KEY_PROVIDER_KEYS)
    tool_secrets = runtime.get(RUNTIME_KEY_TOOL_SECRETS)
    if not isinstance(provider_keys, dict) or not isinstance(tool_secrets, dict):
        raise RuntimeError("task entry: the runtime context carries no credentials")

    cfg = pinned.config
    # Fail the entry rather than run the task without something it was built
    # with — the same rule the worker applies at session start. The task's own
    # tool DEFINITIONS were resolved at session start (`resolve_pinned_tasks`)
    # and are not re-read here.
    hook_trees = await persistence.load_hook_trees(tenant, cfg, list(pinned.tools))
    # Connected while the conversation before this ran (`warm_task_toolsets`);
    # None when nothing was, or it failed, and they are connected on entry instead.
    connected = await _take_warm_toolsets(session, pinned.selection.name)
    integrations, faqs = await asyncio.gather(
        persistence.load_mcp_integrations(tenant, cfg.mcps if connected is None else []),
        persistence.load_faqs(tenant, cfg.faqs),
    )

    caller = _register_bracket_items(pinned, session, runtime, entry_call_id)

    userdata = session.userdata if isinstance(session.userdata, dict) else {}
    participant_identity = userdata.get("_talqing_participant_identity")
    return compile_task_agent(
        cfg,
        provider_keys=provider_keys,
        inline=True,
        tool_defs=persistence.select_tool_definitions(list(pinned.tools), cfg.tools),
        integrations=integrations,
        connected_toolsets=connected or (),
        faqs=faqs,
        # The calling session's channel, not the task's: a task has none, and
        # entered on a call it speaks with that call's voice.
        spoken=not is_text_runtime(runtime),
        tool_secrets=tool_secrets,
        hook_trees=hook_trees,
        tenant=tenant,
        task_id=pinned.task_id,
        tool_name=pinned.selection.name,
        agent_id=caller[0],
        version=caller[1],
        runtime_id=str(runtime.get("runtime_id")) if runtime.get("runtime_id") else None,
        userdata=userdata,
        # `exclude_instructions` on top of LiveKit's own `speech_only` idiom: the
        # calling agent's system prompt must not become part of the task's
        # context — the task has its own. `exclude_handoff` earns its place too,
        # or the bracket items each entry writes would accumulate across entries.
        chat_ctx=session.history.copy(
            exclude_instructions=True,
            exclude_function_call=True,
            exclude_handoff=True,
        ),
        participant_identity=participant_identity
        if isinstance(participant_identity, str)
        else None,
        # A speech-to-speech call has one model for all three stages, and the
        # task declares no STT and no TTS to pair a text model with — see
        # `compile_task_agent`.
        inherit_llm=isinstance(session.llm, RealtimeModel),
        runtime_context={**runtime, RUNTIME_KEY_SESSION_VARS: entry_vars},
        entry_call_id=entry_call_id,
    )


def _register_bracket_items(
    pinned: PinnedTask,
    session: AgentSession[UserData],
    runtime: RuntimeContext,
    entry_call_id: str,
) -> tuple[str | None, int | None]:
    """Name both `AgentHandoff` items this entry is about to produce, and return
    the calling agent's stamp.

    That stamp is what the task's own items are attributed to: a task has no
    `agents` row, and the sub-conversation is still the calling agent's stretch
    of the call.

    LiveKit writes one on the way in and one on the way back — an entry is two
    activity switches — and each carries nothing but a pair of LiveKit agent ids.
    Without this a call transcript shows two anonymous "Agent handoff" rows per
    task and reads as if the agent changed twice.

    Registered against the two ids those items will carry: the task's, which the
    entry row names as its target, and the calling agent's, which the return row
    names as its. The second entry is transient by design — a genuine handoff
    into that agent later overwrites it through `build_handoff_agent`, which is
    the same map and the same key.
    """
    try:
        caller = session.current_agent
    except RuntimeError:
        return None, None
    stamp = (
        getattr(caller, "_talqing_agent_id", None),
        getattr(caller, "_talqing_version", None),
    )
    targets = runtime.get(RUNTIME_KEY_HANDOFF_TARGETS)
    if not isinstance(targets, dict):
        # No map on this plane: a standalone task run and the CoPilot write no
        # handoff rows a reader would see. Nothing to register.
        return stamp
    targets[pinned.task_id or pinned.selection.name] = HandoffTarget(
        # The attachment's name, not the task's: it is what this agent's author
        # called the job, and it is what the model called.
        name=pinned.selection.name,
        agent_id=None,
        version=None,
        kind="task",
        entry_call_id=entry_call_id,
    )
    caller_cfg = getattr(caller, "cfg", None)
    targets[caller.id] = HandoffTarget(
        name=caller_cfg.name if caller_cfg is not None else caller.id,
        agent_id=stamp[0],
        version=stamp[1],
        kind="task_return",
        entry_call_id=entry_call_id,
    )
    return stamp
