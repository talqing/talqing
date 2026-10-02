"""Deterministically compile a published agent definition → AgentSession.

Providers + prompt + greeting + turn-handling knobs.
No-code tools — each attached tool definition frozen into the
published agent version compiles to one LLM-callable function whose operation
tree chains through `session.userdata`. CompiledAgent is the single
generic Agent subclass.
Lifecycle hooks (`on_enter`/`on_exit` each point to a tool tree),
per-node pronunciation rules, and `compile_node_agent` — the per-flow-node
compile used by `handoff` to build the target agent with its own
prompt/tools/voice.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from livekit.agents import (
    Agent,
    AgentSession,
    StopResponse,
    TurnHandlingOptions,
    inference,
    llm,
)
from livekit.agents import (
    vad as lk_vad,
)
from livekit.agents.types import NOT_GIVEN, NotGivenOr
from livekit.agents.utils import is_given
from livekit.agents.voice.speech_handle import SpeechHandle
from livekit.agents.voice.turn import (
    EndpointingOptions,
    InterruptionOptions,
    PreemptiveGenerationOptions,
    TurnDetectionMode,
)

from compiler.factories import (
    WARM_USER_MESSAGE,
    build_llm,
    build_realtime,
    build_stt,
    build_tts,
    resolve_turn_detection,
    warm_prompt_cache,
)
from compiler.faqs import build_faqs
from compiler.integrations import build_integrations
from compiler.operations import (
    RUNTIME_KEY_CALL_FIELDS,
    RUNTIME_KEY_CONSENT_DISCLOSED,
    RUNTIME_KEY_RECORD_EVENT,
    RUNTIME_KEY_SCREENSHARE,
    RUNTIME_KEY_SESSION_VARS,
    RUNTIME_KEY_TIMEZONE,
    RUNTIME_KEY_VAR_DEFAULTS,
    RUNTIME_KEY_VOICEMAIL,
    ToolRunResult,
    execute_tree,
)
from compiler.provider_tools import build_provider_tools
from compiler.speech import speak
from compiler.tools import (
    build_handoff_tools,
    build_stop_recording,
    build_task_tools,
    build_tools,
    build_voicemail_detected,
)
from services import session_events
from services.agents import (
    CONSENT_TOKEN,
    DEFAULT_MAX_STEPS,
    AgentBase,
    AgentConfig,
    ConversationContext,
    HandoffTarget,
    PinnedTask,
    RecordingSpec,
    STTModelSpec,
    TurnHandlingSpec,
    VoicemailDetectionSpec,
    handoff_tool_name,
)
from services.billing import USERDATA_REPORTED_COST, ReportedCost, collector_in
from services.catalog import TTSEntry, get_catalog
from services.faqs import FaqForPrompt
from services.integrations.models import Integration
from services.system_vars import (
    SYSTEM_VARS_ROOT,
    VARS_ROOT,
    build_system_vars,
    build_vars,
)
from services.tools import (
    HookTrees,
    OperationTree,
    RuntimeContext,
    SystemVars,
    ToolDefinition,
    UserData,
    Vars,
    resolve,
)
from services.user import Tenant
from services.userdata import WINDOW_PARKING_KEY
from utils.bg import spawn

logger = logging.getLogger("talqing.compiler.compile")

AgentTool = llm.Tool | llm.Toolset


# What the agent is told about the screen it can see, appended to the prompt the
# way `_expressive_instructions` appends the TTS dialect: fixed prose, after
# `{{userdata}}` substitution, identical for every session so the cached prompt
# prefix stays stable.
#
# Both halves are load-bearing. An agent that can see but was never told will
# not think to look; one told only the good half invents a screen the moment
# SCREENSHARE_ABSENT_NOTE appears, which is the first thirty seconds of every
# real call.
SCREENSHARE_INSTRUCTIONS = (
    "You can see the person's screen while they are sharing it: each of their turns is "
    "followed by a still image of their screen as it is at that moment. It is a live view, "
    "not a file they sent you, and it replaces anything you saw earlier. When you are told "
    "they are not sharing, you cannot see anything — ask them to share their screen rather "
    "than guessing."
)

# Injected in place of the frame when nobody is sharing. Not a neutral marker
# and not silence: the browser will not open the share picker for us (it needs a
# click), so the agent asking is the only thing that starts the flow — and
# without a counter-signal the prompt's claim that it can see stands unopposed
# and the model confabulates a screen, specifically and plausibly, the moment
# somebody hits Stop sharing.
#
# `system` is safe on every provider we run: OpenAI passes it through in
# position, and Gemini/Anthropic/AWS rewrite it to a positioned `user` message
# wrapped in `<instructions>` (`_provider_format/utils.py`).
SCREENSHARE_ABSENT_NOTE = (
    "The person is not sharing their screen, so you cannot see anything right now. If "
    "answering them needs the screen, ask them to click Share screen before you go on. If it "
    "does not, just answer."
)

# The longest side the frame is sent at, as a bounding box under LiveKit's
# `scale_aspect_fit` — so a 16:9 share arrives as 1280x720. Measured, not
# chosen: below this, 13px source text in an editor stops transcribing exactly
# (0/3 at 1024, 3/3 at 1280), and native buys nothing above it at 2.2x the
# tokens. BOTH numbers or neither — `serialize_image` builds its resize options
# only when both are truthy, so setting one is silently identical to setting
# none and sends the frame at native size.
SCREENSHARE_INFERENCE_PX = 1280


def personalize(
    text: str,
    userdata: UserData,
    system: SystemVars | None = None,
    session_vars: Vars | None = None,
) -> str:
    """Substitute `{{userdata.*}}`, `{{system_vars.*}}` and `{{vars.*}}` in a
    prose field the user wrote — the system prompt and the greeting — against
    this session's state.

    The same resolver the operation tree uses, with the other roots
    deliberately empty: there are no tool `args` outside a tool call, a
    `{{secrets.NAME}}` here would paste a workspace credential into the model's
    context, and `{{consent.notice}}` belongs to the greeting alone (see
    `personalize_greeting`). `validate_agent_config` rejects all three at draft
    time, so what reaches here only ever reads the three bags above.

    `system` is what the platform knows about this session: this call's two phone
    numbers and its direction, plus the clock on the agent's own timezone. Only a
    phone call has the numbers and only a configured agent has the clock — an
    absent key resolves empty, which is why the parameter defaults.

    `session_vars` is the other side of that: what the TENANT filled in, already
    merged by `build_vars` from this agent's declared defaults and whatever the
    request that started the session supplied.

    Resolved **once**, when the agent is built: on a call that is the moment
    before it answers, on a handoff the moment the target takes over, and on text
    the start of a cold conversation window. So the clock reads the time the
    agent started, not the time of the turn. It is not re-resolved mid-session —
    that would rewrite the prompt prefix on every userdata write and invalidate
    the provider prompt cache for the rest of the call. A tool, whose resolve
    happens per run, reads the live clock instead.
    """
    resolved = resolve(
        text,
        {
            "args": {},
            "userdata": userdata,
            SYSTEM_VARS_ROOT: system or {},
            VARS_ROOT: session_vars or {},
            "secrets": {},
            "consent": {},
        },
    )
    # `resolve` returns the typed value when a string is exactly one token
    # (a whole prompt read out of userdata); prose fields want its text.
    return "" if resolved is None else str(resolved)


def personalize_greeting(
    text: str,
    userdata: UserData,
    *,
    recording: RecordingSpec,
    disclose: bool = True,
    system: SystemVars | None = None,
    session_vars: Vars | None = None,
) -> str:
    """`personalize`, plus the recording notice the greeting is allowed to place.

    The greeting owns *where* the disclosure is spoken — an author writes
    "Thanks for calling Acme. {{consent.notice}} How can I help?" — while the
    sentence itself lives on the agent's recording settings, so there is one
    place to audit and reword it.

    With consent off the token resolves to nothing, because turning recording
    disclosure off must not force the author back into the greeting to delete a
    placeholder. That leaves a double space where the sentence was, so runs of
    whitespace collapse afterwards. Whitespace only: fixing up punctuation would
    mean guessing at the author's sentence, and guessing wrong out loud.

    `disclose` is how a handoff leg says the sentence is not its to speak: the
    caller was told by the agent handing over, this agent turned recording off, or
    nothing was recording to begin with. `_follow_recording_policy` decides that,
    and is the only thing that can — by the time a target is speaking it is the
    one agent on the call. The token resolves to nothing there. The agent that
    *answers* always passes true; whether it discloses at all is `consent`'s
    business, one line down.
    """
    notice = recording.consent_notice if disclose and recording.consent == "disclosure" else ""
    resolved = resolve(
        text,
        {
            "args": {},
            "userdata": userdata,
            SYSTEM_VARS_ROOT: system or {},
            VARS_ROOT: session_vars or {},
            "secrets": {},
            "consent": {"notice": notice},
        },
    )
    return " ".join(str(resolved).split()) if resolved is not None else ""


class _HookRunContext:
    """Duck-typed RunContext for hook trees (on_enter/on_exit run outside an LLM
    tool call, so there is no real RunContext/SpeechHandle)."""

    def __init__(
        self,
        session: AgentSession[UserData],
        turn_ctx: llm.ChatContext | None = None,
    ) -> None:
        self.session = session
        self.speech_handle: SpeechHandle | None = None
        self.turn_ctx = turn_ctx

    async def wait_for_playout(self) -> None:
        """No-op: a hook runs outside an LLM turn, so there is no spoken response
        of the model's own to wait for. `end_call` in a hook tree still waits on
        anything the tree itself queued."""


def _expressive_instructions(cfg: AgentConfig, instructions: str) -> str:
    """`instructions` plus the delivery-tag prompt, when this agent asked for one.

    Appended here rather than to `cfg.prompt` so it reaches every agent equally:
    a handoff target is constructed with an explicit `instructions` argument, and
    appending upstream would leave one with an expressive voice and nothing
    teaching it the tags. `instructions` is the personalized prompt with the FAQ
    block already on it (`node_tools_and_instructions`), so this lands after
    both. It comes after `{{userdata}}` substitution on purpose —
    it is fixed prose, not a template, and it is the same string for every
    session on this agent, so the cached prompt prefix stays stable.

    Only the primary voice's dialect is ever taught. It is written once, at
    compile time, and agent validation requires a fallback to match the primary's
    `expressive` setting, so there is no second dialect to describe and nothing
    to rebuild if the call fails over.
    """
    if cfg.tts is None or not cfg.tts.expressive:
        return instructions
    entry = get_catalog().require_entry("tts", cfg.tts.provider.strip().lower(), cfg.tts.model)
    if not isinstance(entry, TTSEntry) or entry.expressive is None:
        # Publish validation refuses the toggle on an entry with no dialect, so
        # reaching here means an agent is running a setting that would silently
        # do nothing — the exact failure the toggle exists to avoid.
        raise ValueError(
            f"agent has expressive delivery on, but text-to-speech "
            f"{cfg.tts.provider}/{cfg.tts.model} has no tag dialect to teach"
        )
    return f"{instructions}\n\n{entry.expressive.prompt}"


def with_screenshare_instructions(prompt: str, runtime_context: RuntimeContext | None) -> str:
    """`prompt` plus the screen-share prompt, when this session can deliver frames.

    Gated on the runtime being able to deliver frames, not on
    `cfg.vision_input.screenshare.enabled`. The same stored `voice` config is
    reachable over a web room and over SIP, and only one of those has a watcher —
    a phone caller cannot share anything, and an agent that spends the call
    asking them to is worse than one that never offers. Fixed for the whole
    session either way, so the cached prefix is unaffected. It also carries
    through a handoff and into a task: the watcher belongs to the call, so
    everything that speaks on it sees the screen, exactly as recording is decided
    once by the agent that answered.
    """
    if RUNTIME_KEY_SCREENSHARE not in (runtime_context or {}):
        return prompt
    return f"{prompt}\n\n{SCREENSHARE_INSTRUCTIONS}"


class _TalqingAgent(Agent):
    """Everything Talqing adds to a LiveKit ``Agent``, shared by its two subclasses.

    ``CompiledAgent`` is an ``Agent``; ``CompiledAgentTask`` (``compiler.tasks``)
    is an ``AgentTask``, which is also an ``Agent`` but with a typed result and a
    different constructor. Both want the same entry behaviour, the same three
    lifecycle hooks and the same runtime state, so it lives here once.

    **This class deliberately defines no ``__init__``.** ``CompiledAgentTask``
    mixes it in *front* of ``AgentTask``, so ``super().__init__`` from there has
    to fall through this class to ``AgentTask.__init__`` and on to
    ``Agent.__init__``. An ``__init__`` here would break that chain and swallow
    the arguments ``AgentTask`` needs. The state is bound by ``_bind`` instead,
    which each subclass calls once its ``super().__init__`` has returned.

    Four seams separate the two. ``_entry_recording_policy``,
    ``_greeting_line`` and ``_greeting_interruptible`` are what an agent has
    and a task does not; and
    ``_entry_instructions`` is what each is told when it opens without a
    scripted line — "you have just been handed this conversation" for an agent,
    and nothing for a task, whose prompt already says what it is there to do.
    """

    cfg: AgentBase
    # The tasks this agent can enter. Only `CompiledAgent` has any: a task enters none.
    _tasks: Sequence[PinnedTask] = ()

    def _bind(
        self,
        cfg: AgentBase,
        *,
        agent_id: str | None = None,
        version: int | None = None,
        tenant: Tenant | None = None,
        tool_secrets: dict[str, str] | None = None,
        hook_trees: HookTrees | None = None,
        participant_identity: str | None = None,
        defer_entry: bool = False,
        runtime_context: RuntimeContext | None = None,
    ) -> None:
        self.cfg = cfg
        # Who is speaking right now, carried on the agent rather than re-read
        # from the session row: items are persisted on concurrent tasks, so a
        # turn emitted just before a handoff must be stamped with the agent that
        # produced it, not with whoever the row names by the time the task runs.
        #
        # `_talqing`-prefixed, unlike everything else on this class, because
        # these two are the only ones read off an object typed as LiveKit's
        # `Agent` rather than as ours — `session.current_agent` in
        # `workers/voice/runtime.py`, through `getattr(..., default)`. We own
        # neither the base class nor its private names, and `Agent` already
        # carries `_id`, so `_agent_id` / `_agent_version` are exactly the names
        # a future LiveKit release might take. A collision would not raise: our
        # assignment would shadow theirs (breaking their feature), or the getattr
        # would find theirs and stamp every transcript item with a value that is
        # not an agent id at all. Everything below is read on `self`, where the
        # subclass owns the name.
        self._talqing_agent_id = agent_id
        self._talqing_version = version
        self._tenant = tenant
        self._tool_secrets = tool_secrets or {}
        self._hook_trees = hook_trees or {}
        self._participant_identity = participant_identity
        self._runtime_context = dict(runtime_context or {})
        # Root agents set defer_entry=True so the worker can run entry after
        # session.start (voice/video after media; text after cold compile).
        # Handoff targets and tasks enter mid-session, so their on_enter runs
        # here directly.
        self._defer_entry = defer_entry

    # ── the four seams ─────────────────────────────────────────────────────

    def _entry_recording_policy(self) -> bool:
        """Make the recorder follow whoever is now speaking, and answer whether
        the caller still owes a disclosure.

        Nothing by default — only something that owns the session's recorder has
        anything to do here, and a task borrowing another agent's floor
        deliberately does not.
        """
        return False

    def _greeting_line(self, owes_disclosure: bool) -> str:
        """The scripted opening line, personalized, or "" when there is none."""
        return ""

    def _greeting_interruptible(self, owes_disclosure: bool) -> bool:
        """Whether the caller may cut the greeting off. Only asked when there is one."""
        return True

    def _entry_instructions(self) -> str | None:
        """What this is told when it enters mid-conversation with no line to speak.

        None generates the opening turn from the prompt and the conversation alone."""
        raise NotImplementedError

    # ── lifecycle hooks ──────────────────────────────────────

    async def on_enter(self) -> None:
        if self._defer_entry:
            return
        await self.run_entry(initial=False)

    async def run_entry(self, initial: bool = False) -> None:
        """The entry behavior: the `on_enter` hook, then one opening line or none.

        The hook runs to completion *first*, so the line that follows can be true
        about what it found — the greeting resolves `{{userdata.…}}` against the
        live session bag the hook just wrote to, and a generated opening is
        composed against whatever the hook added to the context. The price is the
        hook's own latency ahead of the first word; an author who does not want
        the caller sitting through it opens the hook with a `say`, which plays
        while the rest of the tree runs.

        Then exactly one opening line, or none:

        - the greeting, wherever there is one. An agent reached by a handoff
          speaks its own greeting like any other; a task has none at all.
        - failing that, and only mid-conversation, a generated turn: the caller
          has just asked for something and is waiting on an answer. On first
          entry the same silence is the "let the caller speak first" shape, and
          is left alone.
        """
        # Whether this leg still owes the caller the recording notice. The
        # greeting is where it goes — one sentence, in the place its author chose,
        # rather than a disclosure read out and then a greeting. Always true on
        # the leg that answers, where `consent` alone decides; on a handoff the
        # recorder decides, because by then this agent is the only one on the call.
        owes_disclosure = True if initial else self._entry_recording_policy()
        result = None
        tree = self._hook_trees.get("on_enter")
        if tree:
            try:
                result = await self._run_hook(tree, "on_enter")
            except Exception:
                # The opening line still happens: a hook that failed to fetch
                # something leaves a thinner greeting, while no opening line at
                # all on a handoff leaves the caller talking to nobody.
                logger.exception("on_enter hook failed")
        if result is not None and (result.handoff is not None or result.end_call):
            # The hook handed the call on or hung up. There is no opening line
            # left to speak, and the activity it would speak through is already
            # draining.
            return
        greeting = self._greeting_line(owes_disclosure)
        if greeting:
            speak(
                self.session,
                greeting,
                allow_interruptions=(
                    NOT_GIVEN if self._greeting_interruptible(owes_disclosure) else False
                ),
            )
            spawn(self._warm_prompt_cache(greeting))
            self._warm_task_toolsets()
        elif not initial:
            self.session.generate_reply(instructions=self._entry_instructions() or NOT_GIVEN)

    def _warm_task_toolsets(self) -> None:
        """Start connecting the tasks' integrations, alongside the greeting."""
        if not self._tasks or self._tenant is None:
            return
        # Lazy: `compiler.tasks` imports this module.
        from compiler.tasks import warm_task_toolsets

        warm_task_toolsets(
            self.session, self._tasks, tenant=self._tenant, tool_secrets=self._tool_secrets
        )

    async def _warm_prompt_cache(self, greeting: str) -> None:
        """Prefill the provider's prompt cache while the greeting is being spoken.

        Deliberately tied to a greeting that is actually going out, rather than to
        entry in general. The greeting is what makes this free of races: it is
        seconds of audio the human has to sit through, and nothing else on the
        call wants the LLM during it. An agent that opens silently has no such
        window — its first turn is triggered by the human, and a request fired at
        connect would be racing the very turn it exists to speed up, paying for
        an uncached prefix twice as often as it saved one. Text agents never
        reach here at all: they have no greeting, and `run_entry` runs a beat
        before their first real turn. Nor does a task, which has no greeting on
        any channel.

        The context assembled here is the one the human's first turn will send.
        `self.chat_ctx` already carries the system prompt — LiveKit inserts the
        agent's instructions at index 0 when the activity starts, which is before
        the worker calls `run_entry` — followed by whatever earlier calls seeded.
        The greeting is added by hand because `session.say` only commits it to the
        context once it has finished playing, and by then the window is over.
        """
        # This agent's own model, falling back to the session's exactly as
        # `AgentActivity.llm` resolves it. A handoff target always carries its own
        # (`compile_node_agent` passes it as an Agent override), and warming the
        # model the agent that *answered* runs on would spend a request seeding a
        # cache nobody is about to read.
        model = self.llm if is_given(self.llm) else self.session.llm
        if not isinstance(model, llm.LLM):
            # Realtime speaks for itself: no separate LLM request to warm, and no
            # `chat()` on a RealtimeModel to send one with.
            return

        warm_ctx = self.chat_ctx.copy()
        warm_ctx.add_message(role="assistant", content=greeting)
        warm_ctx.add_message(role="user", content=WARM_USER_MESSAGE)
        try:
            await warm_prompt_cache(
                model,
                chat_ctx=warm_ctx,
                # `flatten()` is how the pipeline itself builds the list it sends,
                # so this matches the real turn's tool array — which is part of
                # the cached prefix, and worth nothing if it differs by one entry.
                tools=llm.ToolContext(self.tools).flatten(),
            )
        except Exception:
            # Never fatal, and never silent: the call proceeds on a cold cache,
            # exactly as it did before this existed.
            logger.warning("prompt cache warm failed", exc_info=True)

    async def on_exit(self) -> None:
        tree = self._hook_trees.get("on_exit")
        if not tree:
            return
        # A text chat outlives the warm session that serves it. When only that
        # session is being closed, the chat has not ended and neither has the
        # agent's part in it.
        if self.session.userdata.get(WINDOW_PARKING_KEY):
            return
        try:
            await self._run_hook(tree, "on_exit")
        except Exception:
            # an on_exit fault (e.g. a failing lead-POST) must never crash the call
            logger.exception("on_exit hook failed")

    async def on_user_turn_completed(
        self, turn_ctx: llm.ChatContext, new_message: llm.ChatMessage
    ) -> None:
        """Shows the agent the screen, then runs its on_user_turn_completed tool
        tree — after each user turn, just before the LLM replies. The user's
        words are exposed as {{args.user_message}}. LiveKit invokes this for
        spoken turns; the text worker calls it explicitly before session.run. A
        hook fault never blocks the turn.

        While a task holds the floor this is the TASK's hook, never the calling
        agent's: the task is `session.current_agent`, and the hook belongs to the
        activity that is running.

        **`turn_ctx`, never `new_message`.** They are one argument apart and have
        opposite lifetimes: `new_message` is the real ChatMessage LiveKit inserts
        into stored history once the reply schedules, while `turn_ctx` is a
        mutable copy made for this generation and thrown away after it. Appending
        the frame to `new_message` — which is what LiveKit's own video example
        does — would leave one image per turn in the context for the rest of the
        call. Here the model sees exactly one, forever, and there is nothing to
        prune.

        The image lands AFTER the user's words without any ordering work here:
        `new_message` was built before this hook ran, so it carries the earlier
        `created_at`, and LiveKit inserts it created-at-ordered into the copy the
        reply runs on. Final order is [...history, user_text, screen] — the
        question, then the screen it is about.
        """
        peek = self._runtime_context.get(RUNTIME_KEY_SCREENSHARE)
        if peek is not None:
            frame = peek()  # peeked, never consumed: a static screen sends no new frames
            if frame is None:
                turn_ctx.add_message(role="system", content=[SCREENSHARE_ABSENT_NOTE])
            else:
                # A standalone message rather than an append to whatever happens
                # to be last, which could hang an image off a
                # `function_call_output` — a shape providers mishandle. Two
                # consecutive `user` messages are fine, including on Gemini,
                # which finalizes a turn only when the effective role changes and
                # so merges them into one turn carrying [text, image].
                #
                # The JPEG encode happens later, on the event loop, when the
                # request is built (`llm/utils.py::serialize_image`) — measured
                # at ~15ms for a 1080p frame, once per user turn. Worth knowing
                # before anyone raises the frame rate or the resolution.
                turn_ctx.add_message(
                    role="user",
                    content=[
                        llm.ImageContent(
                            image=frame,
                            inference_width=SCREENSHARE_INFERENCE_PX,
                            inference_height=SCREENSHARE_INFERENCE_PX,
                        )
                    ],
                )

        tree = self._hook_trees.get("on_user_turn_completed")
        if not tree:
            return

        try:
            runtime = dict(self._runtime_context)
            runtime.update(
                {
                    "tenant": self._tenant,
                    "agent_id": self._talqing_agent_id,
                    "participant_identity": self._participant_identity,
                }
            )
            res = await execute_tree(
                tree,
                _HookRunContext(self.session, turn_ctx),
                self._tool_secrets,
                {"user_message": new_message.text_content or ""},
                runtime=runtime,
            )
        except Exception:
            logger.exception("on_user_turn_completed hook failed — continuing turn")
            return
        if res.end_call:
            from workers.session.runtime_kind import is_text_runtime

            if is_text_runtime(self._runtime_context):
                userdata = self.session.userdata if isinstance(self.session.userdata, dict) else {}
                userdata["_talqing_end_call_requested"] = True
                self.session.userdata = userdata
            raise StopResponse()
        if res.handoff is not None:
            # the target agent answers this turn (its on_enter continues the
            # conversation) — suppress the departing agent's reply
            self.session.update_agent(res.handoff)
            raise StopResponse()

    async def _run_hook(self, tree: OperationTree, label: str) -> ToolRunResult:
        """Run one lifecycle tree, and hand back what it did — `run_entry` decides
        whether there is still an agent here to speak an opening line."""
        runtime = dict(self._runtime_context)
        runtime.update(
            {
                "tenant": self._tenant,
                "agent_id": self._talqing_agent_id,
                "participant_identity": self._participant_identity,
            }
        )
        res = await execute_tree(
            tree,
            _HookRunContext(self.session),
            self._tool_secrets,
            {},
            runtime=runtime,
        )
        if res.handoff is not None:
            self.session.update_agent(res.handoff)
            return res
        from workers.session.runtime_kind import is_text_runtime

        if res.end_call and is_text_runtime(self._runtime_context):
            userdata = self.session.userdata if isinstance(self.session.userdata, dict) else {}
            userdata["_talqing_end_call_requested"] = True
            self.session.userdata = userdata
        return res


class CompiledAgent(_TalqingAgent):
    """The ONE generic config-driven Agent. All runtime behavior comes from cfg:
    `on_enter` runs the agent's on-enter tool tree (or speaks the greeting) and
    `on_exit` runs the on-exit tree."""

    cfg: AgentConfig

    def __init__(
        self,
        cfg: AgentConfig,
        tools: list[AgentTool] | None = None,
        instructions: str | None = None,
        *,
        agent_id: str | None = None,
        version: int | None = None,
        tenant: Tenant | None = None,
        tool_secrets: dict[str, str] | None = None,
        hook_trees: HookTrees | None = None,
        participant_identity: str | None = None,
        defer_entry: bool = False,
        chat_ctx: NotGivenOr[llm.ChatContext | None] = NOT_GIVEN,
        entry_context: ConversationContext | None = None,
        entry_has_tail: bool = False,
        runtime_context: RuntimeContext | None = None,
        overrides: dict[str, object] | None = None,
        tasks: Sequence[PinnedTask] = (),
    ) -> None:
        prompt = with_screenshare_instructions(
            _expressive_instructions(cfg, instructions or cfg.prompt), runtime_context
        )
        super().__init__(
            instructions=prompt,
            tools=tools or [],
            # The stored agent's id, or this member's name when there is no
            # `agents` row. Not `None`: LiveKit then falls back to the CLASS
            # name, so every team member on a call would answer to
            # `compiled_agent` — and `AgentHandoff.new_agent_id`, which is the
            # only thing the handoff transcript item carries about its target,
            # would name none of them. Nothing in the SDK routes on this; it is
            # an identity label, in that event and in its own log lines.
            id=str(agent_id) if agent_id else cfg.name,
            chat_ctx=chat_ctx,
            **(overrides or {}),
        )
        self._bind(
            cfg,
            agent_id=agent_id,
            version=version,
            tenant=tenant,
            tool_secrets=tool_secrets,
            hook_trees=hook_trees,
            participant_identity=participant_identity,
            defer_entry=defer_entry,
            runtime_context=runtime_context,
        )
        # What a HANDOFF gave this agent, so its opening line can be true about
        # it. None on the agent that answered, which never says any of them.
        # `_entry_has_tail` only ever separates the two `none` sentences —
        # `summary` always has a tail and `transcript` never asks.
        self._entry_context = entry_context
        self._entry_has_tail = entry_has_tail
        self._tasks = tasks

    def _entry_recording_policy(self) -> bool:
        """A handoff, so the recorder has to follow whoever is now speaking — and
        it has to happen before this agent says anything at all, the hook
        included: what it says is either recorded or not, and the boundary must
        not fall inside its first sentence.

        Returns whether the caller still owes a disclosure, which `run_entry`
        places in the greeting. The one case that cannot wait for the greeting is
        settled here: a version published before the token was required has
        nowhere for the sentence to land, so it is spoken on its own, ahead of
        both the hook and the greeting.
        """
        owes_disclosure = self._follow_recording_policy()
        notice = self.cfg.recording.consent_notice.strip()
        if owes_disclosure and notice and CONSENT_TOKEN not in (self.cfg.greeting or ""):
            speak(self.session, notice, allow_interruptions=False)
            return False
        return owes_disclosure

    def _greeting_line(self, owes_disclosure: bool) -> str:
        if not self.cfg.greeting:
            return ""
        userdata = self.session.userdata if isinstance(self.session.userdata, dict) else {}
        return personalize_greeting(
            self.cfg.greeting,
            userdata,
            recording=self.cfg.recording,
            disclose=owes_disclosure,
            # Both built here rather than handed down: this is where the
            # greeting's resolve happens, and every source is already on
            # hand. The clock it freezes is the moment the agent opened
            # its mouth, which is what a greeting means.
            system=build_system_vars(
                self._runtime_context.get(RUNTIME_KEY_CALL_FIELDS),
                self._runtime_context.get(RUNTIME_KEY_TIMEZONE),
            ),
            session_vars=build_vars(
                self._runtime_context.get(RUNTIME_KEY_VAR_DEFAULTS),
                self._runtime_context.get(RUNTIME_KEY_SESSION_VARS),
            ),
        )

    def _greeting_interruptible(self, owes_disclosure: bool) -> bool:
        """The author's setting, except that a greeting carrying the recording
        notice is always protected: a caller's "hello?" at pickup must not cost
        them the one sentence the law says they hear. Mirrors exactly when
        `personalize_greeting` resolves the token to the sentence."""
        recording = self.cfg.recording
        speaks_notice = (
            owes_disclosure
            and recording.consent == "disclosure"
            and bool(recording.consent_notice.strip())
            and CONSENT_TOKEN in (self.cfg.greeting or "")
        )
        return self.cfg.greeting_interruptible and not speaks_notice

    def _entry_instructions(self) -> str:
        return self._handoff_entry_instructions()

    def _handoff_entry_instructions(self) -> str:
        """What this agent is told on the way in from a handoff.

        Follows what it ACTUALLY got, rather than asserting "full context" at an
        agent that may have been handed a summary or nothing at all — a target
        that thinks it can see the conversation and cannot is the one failure
        this sentence can cause.
        """
        if self._entry_context == "summary":
            return (
                "You have just been handed this conversation. You have a summary of what came "
                "before, and the last few messages in full. Pick it up from there and answer "
                "what the caller is asking; do not greet or re-introduce yourself, and do not "
                "ask them to repeat what you have already been told."
            )
        if self._entry_context == "none":
            if self._entry_has_tail:
                return (
                    "You have just been handed this conversation. You can see the last few "
                    "messages and nothing before them. Answer what the caller is asking; do "
                    "not greet or re-introduce yourself."
                )
            return (
                "You have just been handed this conversation. You have not been told what was "
                "said before. Introduce yourself briefly and find out what the caller needs."
            )
        return (
            "You have just been handed this conversation and you can see everything said so "
            "far. Continue helping the caller — address their most recent message directly; "
            "do not greet or re-introduce yourself."
        )

    def _follow_recording_policy(self) -> bool:
        """Make the recorder match this agent on the way in from a handoff, and
        answer whether the caller still has to be told it is being recorded.

        Recording is a per-agent setting, so a flow can hand a call to an agent
        whose author turned it off — a payment step, say — and the caller's card
        number must not be on the file because the agent that answered had
        recording on. Pausing rather than closing the recorder is what lets the
        call come back: `aclose()` finalises the Ogg container, and a handoff
        back could not reopen it.

        The paused stretch is written as silence, so every transcript timestamp
        after it still lines up with the audio. The two events are what stop that
        silence reading as a bug.

        Nothing here can turn recording *on* mid-call: with recording off on the
        agent that answered, `AgentSession` never wired a recorder at all, so
        there is nothing to resume. Publish warns about that edge
        (`services.agents.media.recording_handoff_warning`) and this records
        `recording.unavailable` on the calls where it bites — the only trace, on
        a call whose `recording.state` otherwise reads `none` and names the
        agent that answered rather than this one.

        The disclosure it returns is `run_entry`'s to place — in the greeting,
        where the author put the token. All three ways to owe nothing are settled
        here and nowhere else: the agent handing over already said it, this agent
        turned recording off, or nothing was recording the call to begin with.
        """
        record = self._runtime_context.get(RUNTIME_KEY_RECORD_EVENT)
        agent = {"agent_id": self._talqing_agent_id, "agent_name": self.cfg.name}

        recorder = getattr(self.session, "_recorder_io", None)
        if recorder is None or not hasattr(recorder, "pause_recording"):
            # No recorder on a voice call means the agent that answered had
            # recording off. Text has no recorder either, and no recording
            # setting to disagree about, so it never reaches the branch below.
            if self.cfg.recording.enabled and self.cfg.channel != "text" and callable(record):
                logger.warning(
                    "agent %s asks to be recorded but nothing is recording this call",
                    self._talqing_agent_id,
                )
                record(session_events.RECORDING_UNAVAILABLE, agent)
            # Nothing is recording this call, so a greeting that says otherwise
            # would be the one lie this feature exists to prevent.
            return False

        if not self.cfg.recording.enabled:
            if not recorder.paused:
                recorder.pause_recording()
                if callable(record):
                    record(session_events.RECORDING_PAUSED, agent)
            return False

        if recorder.paused:
            recorder.resume_recording()
            if callable(record):
                record(session_events.RECORDING_RESUMED, agent)

        # The caller has to be told, and told once — so only when the agent
        # handing over was not already disclosing: recording either just
        # restarted under this agent or was never announced at all.
        return self.cfg.recording.consent == "disclosure" and not self._runtime_context.get(
            RUNTIME_KEY_CONSENT_DISCLOSED
        )


def _turn_handling(
    b: TurnHandlingSpec,
    *,
    stt: STTModelSpec,
    language: str | None = None,
    avatar: bool = False,
    listens_for_voicemail: bool = False,
) -> TurnHandlingOptions:
    # build LiveKit's TurnHandlingOptions (a TypedDict) from our typed config.
    # Annotated, not bare: LiveKit merges these TypedDicts as plain dicts
    # (`turn.py` does `InterruptionOptions(**{**defaults, **config})`), so a
    # mistyped key is carried through and then simply never read. The annotation
    # is what makes the key names checked at all.
    interruption: InterruptionOptions = {
        "mode": "vad",
        "enabled": b.interruption.enabled,
        "discard_audio_if_uninterruptible": b.interruption.discard_audio_if_uninterruptible,
        "min_duration": b.interruption.min_speech_duration,
        "min_words": b.interruption.min_words,
        "resume_false_interruption": b.interruption.resume_false_interruption,
        "false_interruption_timeout": b.interruption.false_interruption_timeout,
    }
    if avatar:
        # the avatar DataStream audio sink can't pause — resume-
        # after-false-interruption needs pause, so it's disabled explicitly
        # (the framework would silently disable it anyway; this avoids warnings)
        interruption["resume_false_interruption"] = False
    if listens_for_voicemail:
        # A protected greeting would otherwise feed STT silence while it plays,
        # and a machine's "leave a message" spoken over it is the one thing the
        # model must hear to call `voicemail_detected`.
        interruption["discard_audio_if_uninterruptible"] = False

    # The whole setting, undivided. LiveKit's min_delay is not a wait that starts
    # when the end-of-speech signal arrives — it is a deadline anchored to the
    # instant silence began (AudioRecognition snapshots _last_speaking_time as
    # `now - silence_duration - inference_duration`, then sleeps
    # `min_delay + (last_speaking_time - now)`). So it already means exactly what
    # the public setting promises, and sharing it with the provider's own window
    # would deliver less than the number the user typed, not more.
    endpointing: EndpointingOptions = {"min_delay": b.endpointing.min_silence_duration}
    mode = resolve_turn_detection(stt, language)
    if mode == "livekit-turn-detector-v1-mini":
        # The version is in the mode name and in the constructor for the same
        # reason: bare TurnDetector() picks the cloud v1 model off
        # LIVEKIT_REMOTE_EOT_URL / LIVEKIT_DEV_MODE, env vars this deployment
        # never sets, so which model runs would otherwise depend on the
        # environment. The weights ship inside livekit-local-inference (a
        # livekit-agents dependency) and the forkserver preloads them, so
        # nothing is fetched.
        turn_detection: TurnDetectionMode = inference.TurnDetector(version="v1-mini")
        # max_delay is the wait when a model says "the caller is not finished",
        # so it is set here and left absent for the string modes, where LiveKit
        # never reads it (`_turn_detector` is None). Held at or above min_delay
        # because waiting *less* when unsure than when sure would be perverse —
        # agent validation rejects that pair too; this is the second line.
        endpointing["max_delay"] = max(
            b.endpointing.max_silence_duration, b.endpointing.min_silence_duration
        )
    else:
        turn_detection = mode

    return TurnHandlingOptions(
        turn_detection=turn_detection,
        endpointing=endpointing,
        interruption=interruption,
        # Spelled out rather than model_dump()'d: our field names happening to
        # match LiveKit's is a coincidence worth having the checker enforce, and
        # a dict[str, Any] would let a rename on either side pass silently.
        preemptive_generation=PreemptiveGenerationOptions(
            enabled=b.preemptive_generation.enabled,
            preemptive_tts=b.preemptive_generation.preemptive_tts,
            max_speech_duration=b.preemptive_generation.max_speech_duration,
            max_retries=b.preemptive_generation.max_retries,
        ),
    )


def _realtime_turn_handling(b: TurnHandlingSpec, *, avatar: bool = False) -> TurnHandlingOptions:
    """Turn handling for a speech-to-speech session.

    ``turn_detection="realtime_llm"`` hands end-of-turn to the model's own server
    VAD, which is where the endpointing budget went — ``build_realtime`` passed it
    to the provider as a silence window. No ``endpointing`` block is set here
    because LiveKit's is a post-*transcript* floor, and a realtime model commits
    the turn before any transcript arrives.

    Preemptive generation is absent because it speculates ahead of a turn LiveKit
    itself would confirm, and here the model has already answered by then.
    ``normalize_channel_media`` pins the config fields off; this leaves the
    option unset so LiveKit uses its own default.
    """
    interruption: InterruptionOptions = {
        "mode": "vad",
        "enabled": b.interruption.enabled,
        "discard_audio_if_uninterruptible": b.interruption.discard_audio_if_uninterruptible,
        "min_duration": b.interruption.min_speech_duration,
        "min_words": b.interruption.min_words,
        "resume_false_interruption": b.interruption.resume_false_interruption,
        "false_interruption_timeout": b.interruption.false_interruption_timeout,
    }
    if avatar:
        # Same reason as the cascade's pin in `_turn_handling`: the avatar's
        # DataStream audio sink cannot pause, and resume-after-false-interruption
        # needs pause. A property of the sink, not of the pipeline.
        interruption["resume_false_interruption"] = False
    return TurnHandlingOptions(turn_detection="realtime_llm", interruption=interruption)


def _configure_vad(base_vad: lk_vad.VAD, turn_handling: TurnHandlingSpec) -> lk_vad.VAD:
    """Give the process-prewarmed VAD this agent's silence setting, and return it.

    One number, no branch. On a batch model this VAD *is* the endpointer — it
    cuts the utterances the StreamAdapter posts — so it has to carry the setting.
    On a streaming model it does not decide the turn, and the temptation is to
    leave it at the tight process default; the reason not to is
    ``interruption.min_speech_duration``. That threshold is measured against
    ``VADEvent.speech_duration``, which Silero accumulates within a speech
    segment and resets only at end-of-speech. A tight window ends a segment at
    every inter-word gap, so the accumulator keeps resetting and the threshold
    is never reached; matching the window to the endpointing setting makes one
    segment ≈ one turn, which is the span the threshold is meant to describe.

    Retuning the shared instance is safe because a LiveKit job owns its process
    for the job's whole life — the child rejects a second StartJobRequest and the
    pool closes the executor when the job ends — so this VAD belongs to exactly
    one call. ``update_options`` also propagates to already-running streams, so a
    mid-call handoff to a node with a different setting takes effect at once, and
    the value is always assigned rather than left as found.

    Not called for realtime agents: their model detects turns server-side, so
    that VAD keeps the process default (``settings.vad.min_silence_duration``).
    """
    base_vad.update_options(min_silence_duration=turn_handling.endpointing.min_silence_duration)
    return base_vad


def _cache_key(runtime_id: str | None) -> str:
    """Provider prompt-cache key for runtime LLM calls.

    Voice/video: the LiveKit session id (one cache bucket per call).
    Text: the persistent conversation id (one bucket across turns).
    """
    if not runtime_id:
        raise ValueError("runtime_id is required for LiveKit LLM prompt cache key")
    return runtime_id


def node_runtime_context(cfg: AgentBase, runtime_context: RuntimeContext | None) -> RuntimeContext:
    """The runtime context this agent's own subtree runs on.

    The agent's clock and its declared variable defaults, carried to everything
    that resolves a template without ever seeing `cfg`: each tool's dispatcher,
    the lifecycle hooks, and the prompt. Rebound rather than mutated — the
    caller's context is not ours to write into — and both set unconditionally,
    so a handoff or a task entry replaces the source agent's zone and defaults
    with its own rather than inheriting them.

    The session's `vars` VALUES are deliberately not touched here: they belong to
    the session, not to any one agent, and are set once by the worker. A task
    entry is the single exception, and it rebinds them itself
    (`compiler.tasks`), for the reason stated there.
    """
    return {
        **(runtime_context or {}),
        RUNTIME_KEY_TIMEZONE: cfg.timezone,
        RUNTIME_KEY_VAR_DEFAULTS: {v.name: v.default for v in cfg.vars if v.default is not None},
    }


def node_tools_and_instructions(
    cfg: AgentBase,
    tool_defs: Sequence[ToolDefinition],
    integrations: Sequence[Integration],
    tool_secrets: dict[str, str],
    tenant: Tenant | None,
    agent_id: str | None,
    userdata: UserData,
    participant_identity: str | None = None,
    runtime_context: RuntimeContext | None = None,
    *,
    handoffs: Sequence[HandoffTarget] = (),
    tasks: Sequence[PinnedTask] = (),
    records: bool = False,
    voicemail: VoicemailDetectionSpec | None = None,
    faqs: Sequence[FaqForPrompt] = (),
    spoken: bool,
) -> tuple[list[AgentTool], str]:
    """Shared assembly: LLM-callable tools + the personalized prompt.

    Takes an ``AgentBase``, so a task compiles through it unchanged. The three
    things only an agent has are passed explicitly rather than read off the
    config — a task has no `handoffs`, no `tasks` of its own and no recorder, and
    a silent default read through `getattr` here would be a session that quietly
    loses its `stop_recording` tool. `voicemail` is the fourth: a task never
    places a call.

    ``spoken`` is whether this session talks — a voice or video call, including
    a task entered on one. It is the session's fact and not the config's (a task
    has no channel), so every caller states it.
    """
    tools = build_tools(
        tool_defs or [],
        tool_secrets or {},
        tenant=tenant,
        agent_id=agent_id,
        participant_identity=participant_identity,
        runtime_context=runtime_context,
    )
    # One tool per handoff destination, sharing the namespace above — which is
    # why publish validation checks generated names against attached ones.
    #
    # Both prose fields are substituted at build time, exactly as the prompt and
    # greeting below are: the description is baked into the schema the model is
    # shown, and the spoken line is the same kind of authored sentence.
    system_vars = build_system_vars(
        (runtime_context or {}).get(RUNTIME_KEY_CALL_FIELDS),
        (runtime_context or {}).get(RUNTIME_KEY_TIMEZONE),
    )
    # This agent's declared defaults were bound onto the context by the caller
    # just above; the session's values were bound once by the worker and reach
    # every agent the session runs.
    session_vars = build_vars(
        (runtime_context or {}).get(RUNTIME_KEY_VAR_DEFAULTS),
        (runtime_context or {}).get(RUNTIME_KEY_SESSION_VARS),
    )
    taken_names = [td["name"] for td in (tool_defs or [])]

    def personalize_prose(text: str) -> str:
        return personalize(text, userdata, system_vars, session_vars)

    tools.extend(
        build_handoff_tools(
            handoffs,
            personalize=personalize_prose,
            tenant=tenant,
            runtime_context=runtime_context,
            taken_names=taken_names,
        )
    )
    # After the handoff tools and sharing their namespace, which is why publish
    # validation folds both generated sets into one name check.
    tools.extend(
        build_task_tools(
            tasks,
            personalize=personalize_prose,
            tenant=tenant,
            runtime_context=runtime_context,
            # Every variable this call can fill for a task without asking the
            # model: the names THIS agent declares, plus whatever the session was
            # started with. Declarations count even when the caller left them
            # blank — see `task_tool_schema`.
            answered=frozenset({v.name for v in cfg.vars} | set(session_vars)),
            taken_names=[*taken_names, *[handoff_tool_name(t.name) for t in handoffs]],
        )
    )
    tools.extend(build_integrations(integrations or [], tool_secrets or {}, tenant=tenant))
    # The provider's own server-side tools. They sit in the same list because
    # LiveKit sorts them out (`ToolContext.provider_tools`), but nothing here
    # ever calls them — the provider does, mid-turn, and folds the result into
    # its reply. The primary's and the fallback's both go in: each LLM matches
    # only its own provider's subclass when it serializes, so the two never end
    # up in one another's request (see compiler/provider_tools).
    tools.extend(build_provider_tools(cfg.llm))
    if cfg.llm and cfg.llm.fallback:
        tools.extend(build_provider_tools(cfg.llm.fallback))
    if records:
        # Gated on recording, not on consent: a caller can object whether or not
        # the agent announced anything.
        tools.append(build_stop_recording())
    # Only where the worker offered an ending — a call this agent placed. The
    # message is substituted now, like the greeting, because it is spoken word
    # for word.
    end_on_voicemail = (runtime_context or {}).get(RUNTIME_KEY_VOICEMAIL)
    if voicemail is not None and voicemail.enabled and end_on_voicemail is not None:
        message = personalize_prose(voicemail.message) if voicemail.message else None
        tools.append(build_voicemail_detected(message, end_on_voicemail))
    instructions = personalize(cfg.prompt, userdata, system_vars, session_vars)
    # After substitution, never before: FAQ text is the tenant's data, and a
    # `{{` inside an answer must not be read as a template.
    faq_block, faq_tool = build_faqs(faqs, spoken=spoken)
    if faq_tool is not None:
        tools.append(faq_tool)
        instructions = f"{instructions}\n\n{faq_block}"
    return tools, instructions


def compile_node_agent(
    cfg: AgentConfig,
    *,
    provider_keys: dict[str, str],
    tool_defs: list[ToolDefinition] | None = None,
    integrations: list[Integration] | None = None,
    tool_secrets: dict[str, str] | None = None,
    tasks: Sequence[PinnedTask] | None = None,
    faqs: Sequence[FaqForPrompt] = (),
    hook_trees: HookTrees | None = None,
    tenant: Tenant | None = None,
    agent_id: str | None = None,
    version: int | None = None,
    runtime_id: str | None = None,
    session_id: str | None = None,
    userdata: UserData | None = None,
    chat_ctx: NotGivenOr[llm.ChatContext | None] = NOT_GIVEN,
    entry_context: ConversationContext | None = None,
    entry_has_tail: bool = False,
    llm_instance: llm.LLM | None = None,
    vad: lk_vad.VAD | None = None,
    participant_identity: str | None = None,
    runtime_context: RuntimeContext | None = None,
) -> CompiledAgent:
    """Compile one flow node into a CompiledAgent with its OWN prompt/tools/
    voice/turn handling as Agent-level overrides (Agent-over-Session precedence).
    Used by the `handoff` operation; `chat_ctx` carries the
    context-passing policy's result and `entry_context`/`entry_has_tail` name
    which policy produced it, so the target's opening line is true about what it
    can actually see. A base VAD is not copied — it is the one per-call
    instance, retuned for this node and re-declared as an Agent-level override
    so LiveKit rebuilds its recognition stream around it. `userdata` is
    the live session state the target's prompt and greeting resolve against, so
    a node entered mid-call sees everything earlier tools published."""
    runtime_context = node_runtime_context(cfg, runtime_context)

    cache_key = _cache_key(runtime_id or session_id)
    tools, instructions = node_tools_and_instructions(
        cfg,
        tool_defs or [],
        integrations or [],
        tool_secrets or {},
        tenant,
        agent_id,
        userdata or {},
        participant_identity=participant_identity,
        runtime_context=runtime_context,
        handoffs=cfg.handoffs,
        tasks=tasks or (),
        records=cfg.recording.enabled,
        voicemail=cfg.voicemail_detection,
        faqs=faqs,
        spoken=cfg.channel != "text",
    )
    if cfg.realtime is not None:
        # A realtime node overrides only the brain — it *is* the ears and the
        # mouth, so there is no STT or TTS to declare. `llm_instance` is ignored:
        # a realtime target has no separate text LLM, and `build_handoff_agent`
        # passes None for exactly that reason.
        return CompiledAgent(
            cfg,
            tools=tools,
            instructions=instructions,
            agent_id=agent_id,
            version=version,
            tenant=tenant,
            tool_secrets=tool_secrets or {},
            hook_trees=hook_trees or {},
            participant_identity=participant_identity,
            chat_ctx=chat_ctx,
            entry_context=entry_context,
            entry_has_tail=entry_has_tail,
            runtime_context=runtime_context,
            tasks=tasks or (),
            overrides={
                "llm": build_realtime(
                    cfg.realtime,
                    provider_keys,
                    agent_language=cfg.language,
                    turn_handling=cfg.turn_handling,
                ),
                "turn_handling": _realtime_turn_handling(
                    cfg.turn_handling, avatar=cfg.channel == "video"
                ),
                **({"vad": vad} if vad is not None else {}),
            },
        )

    assert cfg.llm is not None
    overrides: dict[str, object] = {
        "llm": llm_instance
        or build_llm(
            cfg.llm,
            provider_keys,
            cache_key=cache_key,
            has_tools=bool(tools),
            # The session's collector, off the live userdata this node was handed
            # — a handoff is a second model on the same call and its spend is the
            # same call's spend.
            reported_cost=collector_in(userdata),
        ),
    }
    if cfg.channel != "text":
        assert cfg.stt is not None and cfg.tts is not None
        # Retune before building: a batch STT is wrapped around this same VAD.
        node_vad = _configure_vad(vad, cfg.turn_handling) if vad is not None else None
        overrides["stt"] = build_stt(
            cfg.stt,
            provider_keys,
            agent_language=cfg.language,
            turn_handling=cfg.turn_handling,
            vad=node_vad,
            reported_cost=collector_in(userdata),
        )
        overrides["tts"] = build_tts(cfg.tts, provider_keys, agent_language=cfg.language)
        if node_vad is not None:
            overrides["vad"] = node_vad
        overrides["turn_handling"] = _turn_handling(
            cfg.turn_handling,
            stt=cfg.stt,
            language=cfg.language,
            avatar=cfg.channel == "video",
        )
    return CompiledAgent(
        cfg,
        tools=tools,
        instructions=instructions,
        agent_id=agent_id,
        version=version,
        tenant=tenant,
        tool_secrets=tool_secrets or {},
        hook_trees=hook_trees or {},
        participant_identity=participant_identity,
        chat_ctx=chat_ctx,
        entry_context=entry_context,
        entry_has_tail=entry_has_tail,
        runtime_context=runtime_context,
        tasks=tasks or (),
        overrides=overrides,
    )


def compile_agent(
    cfg: AgentConfig,
    vad: lk_vad.VAD | None,
    provider_keys: dict[str, str],
    *,
    tool_defs: list[ToolDefinition] | None = None,
    integrations: list[Integration] | None = None,
    tool_secrets: dict[str, str] | None = None,
    tasks: Sequence[PinnedTask] | None = None,
    faqs: Sequence[FaqForPrompt] = (),
    hook_trees: HookTrees | None = None,
    tenant: Tenant | None = None,
    agent_id: str | None = None,
    version: int | None = None,
    runtime_id: str | None = None,
    session_id: str | None = None,
    userdata: UserData | None = None,
    participant_identity: str | None = None,
    runtime_context: RuntimeContext | None = None,
    chat_ctx: NotGivenOr[llm.ChatContext | None] = NOT_GIVEN,
) -> tuple[AgentSession[UserData], CompiledAgent]:
    """`provider_keys` = the tenant's own BYOK AI-provider keys, one per provider
    the config names — Talqing holds no platform key for an agent run, so a
    missing one raises here. `tool_secrets` = the tenant's decrypted tool
    credentials for {{secrets.NAME}} substitution.
    `hook_trees` = the compiled operation trees of the agent's hook tools
    (on_enter / on_exit / on_user_turn_completed).
    `userdata` = the session's opening state — whatever the caller seeded (SIP
    attributes, dispatch metadata, the API's `userdata`) plus what a text
    conversation has persisted. It both seeds the variable bus tools chain
    through and personalizes the prompt and greeting via `{{userdata.field}}`.

    `cfg.max_steps` = how many LLM -> tools -> LLM rounds one turn may take,
    handed to LiveKit minus one exactly as a task run does (`compile_task_run`).
    NOTE: LiveKit enforces it on the cascade pipeline only. A realtime
    (speech-to-speech) agent increments the same counter and never checks it
    (`voice/agent_activity.py::_realtime_reply_task`), so on that path the number
    is inert — passing it is not what bounds the turn, and nothing here does.

    Text channel: the same definition runs with STT/TTS/VAD skipped. It
    uses only the LLM, tools, integrations, lifecycle hooks, and persistent
    conversation state; no LiveKit room is created.

    Realtime pipeline: when the config names a speech-to-speech model it takes
    the place of all three cascade models — one object on `llm=`, no STT, no TTS,
    and turn detection handed to the model's own server VAD. Everything above
    the media layer (tools, hooks, userdata) is untouched."""

    # per-session conversation id → the provider pins this call's turns to one
    # server, so the growing prefix (prompt + history) cache-hits
    cache_key = _cache_key(runtime_id or session_id)
    initial_userdata = dict(userdata or {})
    if participant_identity:
        initial_userdata["_talqing_participant_identity"] = participant_identity
    # One per session, and it rides on the userdata bus so that everything built
    # LATER for this same session — a handoff target, an entered task, a
    # failover — adds into the same object without a new argument threaded
    # through each of them. Finalize reads it; see `services.billing.ReportedCost`.
    reported_cost = ReportedCost()
    initial_userdata[USERDATA_REPORTED_COST] = reported_cost

    runtime_context = node_runtime_context(cfg, runtime_context)

    tools, instructions = node_tools_and_instructions(
        cfg,
        tool_defs or [],
        integrations or [],
        tool_secrets or {},
        tenant,
        agent_id,
        initial_userdata,
        participant_identity=participant_identity,
        runtime_context=runtime_context,
        handoffs=cfg.handoffs,
        tasks=tasks or (),
        records=cfg.recording.enabled,
        voicemail=cfg.voicemail_detection,
        faqs=faqs,
        spoken=cfg.channel != "text",
    )

    agent = CompiledAgent(
        cfg,
        tools=tools,
        instructions=instructions,
        agent_id=agent_id,
        version=version,
        tenant=tenant,
        tool_secrets=tool_secrets or {},
        hook_trees=hook_trees or {},
        participant_identity=participant_identity,
        defer_entry=True,
        chat_ctx=chat_ctx,
        runtime_context=runtime_context,
        tasks=tasks or (),
    )

    max_steps = cfg.max_steps if cfg.max_steps is not None else DEFAULT_MAX_STEPS[cfg.channel]

    # Only the media layer differs between the three pipelines. Everything
    # session-wide is passed once, below: this used to be three separate
    # `AgentSession(...)` calls and `max_tool_steps` reached exactly one of them.
    if cfg.channel == "text":
        assert cfg.llm is not None
        media: dict[str, Any] = {
            "llm": build_llm(
                cfg.llm,
                provider_keys,
                cache_key=cache_key,
                has_tools=bool(tools),
                reported_cost=reported_cost,
            ),
        }
    else:
        if vad is None:
            raise ValueError("voice/video agents require a VAD")
        b = cfg.turn_handling
        if cfg.realtime is not None:
            # The VAD is still handed over: it drives the user-speaking indicator and
            # the interruption knobs (min_duration / min_words). End-of-turn is not
            # its job here — turn_detection="realtime_llm" gives that to the model.
            media = {
                "llm": build_realtime(
                    cfg.realtime, provider_keys, agent_language=cfg.language, turn_handling=b
                ),
                "vad": vad,
                "turn_handling": _realtime_turn_handling(b, avatar=cfg.channel == "video"),
            }
        else:
            assert cfg.stt is not None and cfg.tts is not None and cfg.llm is not None
            session_vad = _configure_vad(vad, cfg.turn_handling)
            media = {
                "stt": build_stt(
                    cfg.stt,
                    provider_keys,
                    agent_language=cfg.language,
                    turn_handling=cfg.turn_handling,
                    vad=session_vad,
                    reported_cost=reported_cost,
                ),
                "llm": build_llm(
                    cfg.llm,
                    provider_keys,
                    cache_key=cache_key,
                    has_tools=bool(tools),
                    reported_cost=reported_cost,
                ),
                "tts": build_tts(cfg.tts, provider_keys, agent_language=cfg.language),
                "vad": session_vad,
                "turn_handling": _turn_handling(
                    b,
                    stt=cfg.stt,
                    language=cfg.language,
                    avatar=cfg.channel == "video",
                    listens_for_voicemail=cfg.voicemail_detection.enabled
                    and RUNTIME_KEY_VOICEMAIL in (runtime_context or {}),
                ),
            }

    session = AgentSession(
        **media,
        userdata=initial_userdata,  # the session-wide variable bus tools chain through
        # LiveKit forces the tool-less answer once `num_steps >= max_tool_steps + 1`,
        # so its knob is one less than the rounds it allows.
        max_tool_steps=max_steps - 1,
        # What marks the caller `away`, which `workers/voice/call_bounds.py`
        # answers with a check-in. None stops LiveKit arming its own 15s timer
        # for a signal nobody would read — and on text there is no caller to go
        # quiet (the window's idle close is its counterpart).
        user_away_timeout=(
            cfg.silence.timeout if cfg.silence.enabled and cfg.channel != "text" else None
        ),
    )
    return session, agent
