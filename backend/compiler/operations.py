"""Runtime operation-tree runner.

Executes a compiled tool's operation tree against a LiveKit `RunContext`.

An operation publishes into one of two stores, and the store it names is the
template root that reads it back:

- ``tooldata`` — created empty for this execution and dropped when it returns.
  Only later operations in the same tree can read it.
- ``userdata`` — the live session bag. Other tools, later turns and the agent's
  prompt read it, and the session persists it.

An `if` op branches into its terminal then/else child list. Tool-level
long-running/silent + the graceful-error policy live in `compiler/tools.py`;
this module is the pure tree executor.

A test run (`POST /v1/tools/{id}/run`, driven from `compiler/dryrun.py`) is the
same walk with a `DryRun` in scope: each operation records what it actually did
into a `TraceStep`, and the kinds that need a live session are swapped for a
recorder. When `dry_run` is None — every live call — none of that runs.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx
from livekit.agents import (
    Agent,
    AgentSession,
    CloseEvent,
    ConversationItemAddedEvent,
    get_job_context,
    llm,
)
from livekit.agents.llm import ToolError
from livekit.agents.voice.speech_handle import SpeechHandle

from compiler.speech import SPEECH_WAIT_TIMEOUT, disable_interruptions, speak
from services.system_vars import (
    SYSTEM_VARS_ROOT,
    VARS_ROOT,
    build_system_vars,
    build_vars,
)
from services.tools import (
    DTMF_KEYS,
    MISSING,
    PUBLISH_STORES,
    TREE_KINDS,
    Operation,
    RuntimeContext,
    UserData,
    dotted_get,
    filter_secrets,
    publish_field_error,
    publish_field_path_and_key,
    publish_field_store,
    resolve,
    resolve_and_pin,
)
from services.userdata import is_reserved_key
from utils.bg import spawn

logger = logging.getLogger("talqing.compiler.ops")

# Workers inject ``build_handoff_agent`` under this RuntimeContext key so the
# tree runner never imports compiler.handoff / compiler.compile (breaks the cycle).
RUNTIME_KEY_BUILD_HANDOFF_AGENT = "build_handoff_agent"
# The SIP worker injects its transfer callable here — same reason, and one more:
# which transport a transfer uses is carrier knowledge, and the compiler must
# not learn that the telephony layer exists. Only a phone call has one, so the
# key's absence IS the check that refuses a transfer on a web or text run.
RUNTIME_KEY_TRANSFER = "perform_transfer"
# The SIP worker injects this call's two phone numbers and its direction here,
# built once in `workers/voice/sip.py`. It is the *phone-derived half* of the
# `{{system_vars.*}}` template root — the clock half is rendered at resolve time
# from the timezone below, and `services.system_vars.build_system_vars` is the one
# place that knows the bag has two sources. It rides the runtime context rather
# than `session.userdata` because nothing persists, serializes or hands a tenant
# a `RuntimeContext`, and because a tool must not be able to overwrite the
# caller's number. Absent on web and text runs, where those keys resolve empty.
RUNTIME_KEY_CALL_FIELDS = "call_fields"
# The agent's IANA timezone, the other half of `{{system_vars.*}}`. Unlike every
# other key in this block no worker sets it: it comes off the config the compiler
# already holds, and `compiler.compile` injects it so tools and hooks — which see
# a runtime dict and never see `cfg` — can render the clock when they run.
RUNTIME_KEY_TIMEZONE = "timezone"
# The `{{vars.*}}` values the request that started this session supplied. Set
# ONCE by the worker, before the entry agent compiles, and **never rebound** —
# that is the whole mechanism behind "a variable describes the session, not one
# agent": `compiler.handoff.next_runtime_context` copies the context forward, so
# the values reach every team member and every handoff target, including one that
# was never in the plan. Empty on an inbound phone call or message, which has no
# request of ours to carry them.
#
# `VarDeclaration.required` is a DOOR rule and no worker enforces it: it is
# checked on the four requests that start a session, on the three that let a
# voice agent answer a phone number, and nowhere else. A handoff target that
# needs a value nobody supplied resolves it to nothing, exactly as a missing
# `userdata` key does — killing a connected call because the NEXT agent wanted
# something is a bigger harm than a blank substitution, and it is the one
# behaviour of ElevenLabs' dynamic variables we deliberately did not copy.
RUNTIME_KEY_SESSION_VARS = "session_vars"
# The declared defaults of the agent running right NOW, off `cfg.vars`. Bound by
# `compiler.compile` per compiled agent, exactly as the timezone above is and for
# the same reason: a handoff target must fall back to ITS defaults rather than
# inherit the source agent's. The pair is merged by `build_vars` wherever a
# template resolves — session values over these.
RUNTIME_KEY_VAR_DEFAULTS = "var_defaults"
# Workers inject their `SessionEventLog.record` here so a tool can put something
# on the call's own trace. Same reason as the keys above — the compiler must not
# import a worker — and it is what makes a detached `long_running_task` failure
# visible on the call it happened during: the webhook alone reaches only tenants
# who run a receiver, and never the dashboard.
RUNTIME_KEY_RECORD_EVENT = "record_session_event"
# The web-room worker injects `ScreenshareWatcher.peek` here — a callable that
# returns the newest screen-share frame, or None when nobody is sharing. Same
# reason as the keys above (the compiler must not import a worker), plus the two
# that decide the design: the frames must never touch `session.userdata`, which
# is serialized to the session row, handed to the browser over RPC and
# deep-copied per background op; and the key's ABSENCE is what tells the
# compiled agent it cannot see. That is what keeps the same `voice` config
# honest over SIP, where a caller has no screen to share and there is no watcher
# — the prompt sentence and the injection are both gated on this key, never on
# `cfg.vision_input.screenshare.enabled`.
RUNTIME_KEY_SCREENSHARE = "peek_screenshare_frame"
# The voice workers inject a callable that presses keys on the call's own keypad
# — `publish_dtmf` on a phone call, and on a media stream the same thing, since
# the gateway translates a room DTMF packet into whatever command that platform
# speaks. Same reason as `RUNTIME_KEY_TRANSFER` above, and the same mechanism:
# the key's ABSENCE is what makes `send_dtmf` refuse itself, on a web room, a
# chat, and a stream whose platform has no send-DTMF command at all. It is
# injected per CALL rather than per channel for exactly that last case: whether
# the agent can dial is a property of which platform is on the other end of the
# socket, not of the channel the agent was built for.
RUNTIME_KEY_SEND_DTMF = "send_dtmf"
# `VoiceRun` injects `CallBounds.voicemail` here — leave a message, if one is
# given, and hang up as `voicemail` — on a call whose direction is `outbound`,
# SIP or media stream alike. Only a call the agent placed can be answered by a
# machine, so the key's ABSENCE is what keeps the `voicemail_detected` tool off
# every inbound, web and text run of an agent that has detection on.
RUNTIME_KEY_VOICEMAIL = "end_on_voicemail"

# Set by `build_handoff_agent` when the agent handing over was already telling
# callers the call is recorded. A target that also discloses reads it and stays
# quiet, because the caller has been told and hearing it twice is worse than not
# hearing it at all. The one fact a handoff target cannot work out for itself:
# by the time it enters, `session.current_agent` is already the target.
RUNTIME_KEY_CONSENT_DISCLOSED = "recording_consent_disclosed"
# The cast this call runs, {member name: AgentConfig}, injected by the worker
# that resolved the plan. It is what a handoff destination with no `agent_id`
# resolves against — `AgentConfig.handoffs[].name`, and the `handoff`
# operation's `agent_name`. Absent (or a single entry) on an ordinary call,
# which is why a destination that names a member is a validation error there.
RUNTIME_KEY_AGENT_ROSTER = "agent_roster"
# The tenant's BYOK provider keys and tool secrets, loaded once when the call
# started and carried forward so `build_handoff_agent` does not read them again
# with the caller waiting. Both are tenant-scoped rather than agent-scoped, so
# the target's copy could only ever be the same rows — and a call that ran on
# two different credential sets because a key was rotated mid-call was the
# worse of the two behaviours. What a handoff still reads is what belongs to the
# TARGET: its integrations and its hooks.
RUNTIME_KEY_PROVIDER_KEYS = "provider_keys"
RUNTIME_KEY_TOOL_SECRETS = "tool_secrets"
# {LiveKit agent id: HandoffTarget} for every agent this call has handed off to,
# written by `build_handoff_agent` as it resolves each one. A MUTABLE dict,
# shared by reference through `next_runtime_context`, because the reader is the
# worker: the `agent_handoff` transcript item carries only the target's LiveKit
# agent id, and the agent stamp captured at emit time names the SOURCE — so who
# took over, and on which published version, is knowable nowhere else.
RUNTIME_KEY_HANDOFF_TARGETS = "handoff_targets"

HTTP_TIMEOUT_DEFAULT = 20.0
HTTP_MAX_RESPONSE_BYTES = 1 * 1024 * 1024  # 1 MB — matches code-exec fetch cap
CODE_TIMEOUT_DEFAULT = 10.0  # seconds; the code-exec service hard-caps at 30
CODE_TIMEOUT_MAX = 30.0
FRONTEND_RPC_METHOD = "talqing.frontend_rpc"  # the client SDK's envelope method
FRONTEND_RPC_TIMEOUT = 5.0
MAX_HANDOFFS = 25  # runaway-loop backstop for LLM-decided handoffs
# Seconds a transfer destination is allowed to ring. Deliberately NOT
# `livekit.sip.ringing_timeout_seconds`: that one is a deployment setting
# covering the caller's own dial, and its ≤80s validator does not apply here.
TRANSFER_RINGING_TIMEOUT_DEFAULT = 30.0
# Last-resort wording when the transport gave us no reason of its own. The
# worker normally supplies one (`workers/voice/transfer.py`), and it is always a
# fact about the person being called rather than a fault of ours.
_TRANSFER_FAILED = "the transfer could not be completed"
# Mutable scope built for one tool execution: the template roots
# (args/tooldata/userdata/system/secrets) plus the non-templatable runtime context.
OpScope = dict[str, object]


class PublishFieldMissing(ValueError):
    """A publish field's dotted path is absent from the operation's output.

    Typed because it is the most common authoring mistake in a tool — a path one
    level too deep — and a test run classifies it as the author's problem rather
    than the endpoint's.
    """


class OperationRunContext(Protocol):
    session: AgentSession[UserData]
    speech_handle: SpeechHandle | None
    turn_ctx: llm.ChatContext | None

    async def wait_for_playout(self) -> None:
        """Wait for the spoken response the LLM produced *before* this tool ran.

        LiveKit's own `RunContext.wait_for_playout`. Awaiting
        `run_ctx.speech_handle` instead raises: that handle is waiting for this
        tool to return, so the two would wait on each other. Hook contexts
        (compiler/compile.py) run outside an LLM turn and implement this as a
        no-op — there is no prior response to wait for.
        """


OperationHandler = Callable[
    [Operation, OperationRunContext, OpScope, "ToolRunResult"], Awaitable[None]
]


@dataclass
class ToolRunResult:
    """What the tree produced: the LLM-facing result + control side effects.

    `responses` is the ordered list returned to the LLM (the function_call_output).
    Each non-silent operation that produces a response appends one entry. This
    keeps multi-operation tools understandable and avoids key collisions between
    unrelated operation outputs. It is deliberately NOT gated by `publish_fields`:
    publish fields select result data for the tool's own plumbing (tooldata) or
    for the session to keep (userdata), which is a separate question from what
    the model is told."""

    responses: list[dict[str, object]] = field(
        default_factory=list
    )  # ordered op outputs for the LLM
    end_call: bool = False
    # LiveKit Agent (typically a CompiledAgent) for session.update_agent / tool return.
    handoff: Agent | None = None
    last_speech_handle: SpeechHandle | None = None


# ──────────────────────────────── test runs ─────────────────────────────────

# Where the DryRun sits in the scope. Underscore-prefixed like `_runtime`: not a
# template root, so `resolve()` never sees it.
SCOPE_KEY_DRY_RUN = "_dry_run"


@dataclass
class TraceStep:
    """What one operation did, on a test run only.

    `path` locates the operation in the tree: the node's index, joined to its
    parent by `.then.` or `.else.` — so the third operation's else-branch second
    child is `2.else.1`. The dashboard walks the tree the same way to line each
    step up with the node that produced it; if the two ever disagree the marks
    land on the wrong nodes silently, so change both or neither.
    """

    path: str
    kind: str
    status: str  # ok | failed | simulated
    duration_ms: int = 0
    detail: dict[str, Any] = field(default_factory=dict)
    published: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None


@dataclass
class DryRun:
    """Present only on a test run; `None` on every live call.

    `handlers` is the production table with the session-bound kinds replaced by
    recorders (`compiler/dryrun.py`), which is what keeps a test run from
    needing a room, a speech handle or a chat context.
    """

    handlers: dict[str, OperationHandler]
    steps: list[TraceStep] = field(default_factory=list)
    # The tool-local store as it stood when the run ended. `execute_tree` drops
    # its scope, so this is the only way to see what a tool published to itself.
    tooldata: dict[str, Any] = field(default_factory=dict)
    # The step being filled in, so a handler can record its own detail without
    # threading an extra parameter through the handler signature.
    current: TraceStep | None = None
    # A simulated handoff can't produce an Agent, so it says "stop here" this way
    # instead of through `ToolRunResult.handoff`.
    halted: bool = False


def dry_run_state(scope: OpScope) -> DryRun | None:
    """The test-run recorder, if this is a test run."""
    dry = scope.get(SCOPE_KEY_DRY_RUN)
    return dry if isinstance(dry, DryRun) else None


def record(scope: OpScope, **detail: Any) -> None:
    """Record what this operation actually did. A no-op on a live call."""
    dry = dry_run_state(scope)
    if dry is not None and dry.current is not None:
        dry.current.detail.update(detail)


def _record_published(scope: OpScope, store: str, key: str, value: Any) -> None:
    dry = dry_run_state(scope)
    if dry is not None and dry.current is not None:
        dry.current.published.append({"store": store, "key": key, "value": value})


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _describe(exc: BaseException) -> str:
    """A failure a human can read.

    httpx's timeout and network errors carry an empty ``str()``, which would put
    a blank line in the trace where the reason should be. The class name is thin
    but it is the difference between `ReadTimeout` and nothing at all.
    """
    return str(exc) or type(exc).__name__


# ─────────────────────────────── entry point ────────────────────────────────


async def execute_tree(
    tree: list[Operation],
    run_ctx: OperationRunContext,
    secrets: dict[str, str],
    args: dict[str, object] | None = None,
    runtime: RuntimeContext | None = None,
    dry_run: DryRun | None = None,
) -> ToolRunResult:
    """`runtime` carries non-templatable call context ({tenant, agent_id}) the
    handoff operation needs to load + compile its target agent.

    Only secrets referenced by this tree (``{{secrets.NAME}}`` templates and
    code ``input.secrets.NAME`` access) are placed in scope / sent to code-exec.

    `dry_run` is set only by a test run from the dashboard; it collects a
    per-operation trace and supplies the handler table.
    """
    args = args or {}
    try:
        tree_secrets = filter_secrets(secrets, tree)
    except ValueError as e:
        # `delete_secret` refuses while a live tree references the name, so this
        # is a version it could not see — an agent rolled back onto an old one,
        # say. No operation has run, so `on_error` has nothing to sit on: the
        # tool aborts, and sounds to the caller like any other aborted tool.
        logger.error("tool tree cannot run: %s", e)
        raise ToolError("Sorry, I wasn't able to complete that just now.") from e
    session_userdata = run_ctx.session.userdata
    if not isinstance(session_userdata, dict):
        raise TypeError(f"session.userdata must be a dict, got {type(session_userdata).__name__}")
    scope: OpScope = {
        "args": args,
        # Fresh per execution — nothing this tool writes here outlives the run.
        "tooldata": {},
        # The live session bag, not a copy: a userdata write is visible to the
        # rest of this tree exactly as it is to the next tool.
        "userdata": session_userdata,
        # Both read-only, and both assembled from the runtime context rather than
        # the session bag: nothing an operation does can write either one —
        # neither is a publish store. Rendered here rather than passed in, so the
        # clock is the time this operation ran (a prompt freezes its copy at agent
        # build, which is right there and wrong in a `created_at` sent to a CRM),
        # and so `vars` merges the session's values over whichever agent is
        # running this tool right now.
        SYSTEM_VARS_ROOT: build_system_vars(
            (runtime or {}).get(RUNTIME_KEY_CALL_FIELDS),
            (runtime or {}).get(RUNTIME_KEY_TIMEZONE),
        ),
        VARS_ROOT: build_vars(
            (runtime or {}).get(RUNTIME_KEY_VAR_DEFAULTS),
            (runtime or {}).get(RUNTIME_KEY_SESSION_VARS),
        ),
        "secrets": tree_secrets,
        "_runtime": runtime or {},
    }
    if dry_run is not None:
        scope[SCOPE_KEY_DRY_RUN] = dry_run
    res = ToolRunResult()
    try:
        await _run(tree, run_ctx, scope, res)
    finally:
        if dry_run is not None:
            # Captured even when the walk raised: a failure halfway through is
            # exactly when you want to know what was written before it.
            dry_run.tooldata = dict(scope["tooldata"])  # type: ignore[arg-type]
    return res


async def _run(
    ops: list[Operation],
    run_ctx: OperationRunContext,
    scope: OpScope,
    res: ToolRunResult,
    path: str = "",
) -> None:
    dry = dry_run_state(scope)
    for index, op in enumerate(ops):
        kind = op["kind"]
        node = f"{path}{index}"
        background = bool(op.get("background_execution"))
        step: TraceStep | None = None
        if dry is not None:
            step = TraceStep(path=node, kind=kind, status="ok", detail={})
            if background:
                # In production this op is fire-and-forget: it publishes nothing
                # and a failure only reaches the log. That makes it the one
                # operation an author cannot observe, so the test runs it in the
                # chain — minus its publish fields, so the rest stays faithful.
                step.detail["background"] = True
                op = {**op, "publish_fields": []}
            dry.steps.append(step)
            dry.current = step
        started = time.perf_counter()
        # Set by an `if`, and read after the try below: evaluating the condition
        # belongs inside the error handling, descending into the branch it
        # picked does not — a failure down there is that operation's, and its
        # own `on_error` already answered for it.
        branch: tuple[str, list[Operation]] | None = None
        try:
            if kind == "if":
                cfg = op.get("config") or {}
                taken = _eval_if(cfg, scope)
                branch_name = "then" if taken else "else"
                if step is not None:
                    # Resolved a second time here (cheap, and only on a test
                    # run): the comparison an author gets wrong is almost always
                    # one they cannot see the two sides of.
                    step.detail = {
                        "left": resolve(cfg.get("left"), scope),
                        "right": resolve(cfg.get("right"), scope),
                        "op": str(cfg.get("op") or "eq").lower(),
                        "branch": branch_name,
                    }
                branch = (branch_name, op.get(branch_name) or [])
            else:
                handler = (dry.handlers if dry is not None else HANDLERS).get(kind)
                if handler is None:
                    # Unreachable for anything this build can publish
                    # (`TREE_KINDS` is asserted against `HANDLERS` below), so
                    # this is a version frozen by a build that had a kind we do
                    # not. Raised in here, so it fails as the operation rather
                    # than as the whole call.
                    raise ValueError(f"unknown operation kind {kind!r}")
                if background and dry is None:
                    _spawn_background_op(op, handler, run_ctx, scope)
                    continue
                await handler(op, run_ctx, scope, res)
        except ToolError as e:
            if step is not None:
                step.status, step.error = "failed", _describe(e)
                step.duration_ms = _elapsed_ms(started)
            raise
        except Exception as e:
            if step is not None:
                step.status, step.error = "failed", _describe(e)
                step.duration_ms = _elapsed_ms(started)
            if (op.get("on_error") or "abort") == "continue":
                logger.warning("op %s failed (on_error=continue): %s", kind, e)
                if kind == "if":
                    # An `if` is terminal in its chain, so there is no next
                    # operation to carry on to: `continue` on a condition that
                    # failed means neither branch runs and the tree ends here.
                    return
                continue
            logger.exception("op %s failed (abort)", kind)
            raise ToolError("Sorry, I wasn't able to complete that just now.") from e
        finally:
            if dry is not None:
                dry.current = None
        if step is not None:
            step.duration_ms = _elapsed_ms(started)
        if branch is not None:
            await _run(branch[1], run_ctx, scope, res, f"{node}.{branch[0]}.")
            return  # an `if` is terminal in its chain
        # The chain is over: the conversation moved to another agent, or the call
        # ended. Saving a tree already refuses operations after an `end_call`
        # (`services.tools.TERMINAL_KINDS`); this is here for versions published
        # before that rule, which are immutable and whose trailing operations
        # would otherwise run against a session that is already draining.
        if res.handoff is not None or res.end_call or (dry is not None and dry.halted):
            return


def _publish_and_output(op: Operation, scope: OpScope, res: ToolRunResult, data: object) -> None:
    for publish_field in op.get("publish_fields") or []:
        if error := publish_field_error(publish_field):
            raise ValueError(error)
        path, key = publish_field_path_and_key(publish_field)
        value = dotted_get(data, path)
        # A `null` publishes as `None`: it is the endpoint's answer, and
        # `exists` and templates already read it as nothing.
        if value is MISSING:
            raise PublishFieldMissing(
                f"publish field path {path!r} was not found in operation output"
            )
        store = publish_field_store(publish_field)
        _write(scope, store, key, value)
        _record_published(scope, store, key, value)

    _append_response(op, res, data)


def _write(scope: OpScope, store: str, key: str, value: object) -> None:
    """Put one published value in its store — `tooldata` or `userdata`."""
    bag = scope[store]
    if not isinstance(bag, dict):
        raise RuntimeError(f"{store} must be a dictionary")
    bag[key] = value


def _append_response(op: Operation, res: ToolRunResult, data: object) -> None:
    if op.get("silent"):
        return
    res.responses.append(
        {
            "operation": op.get("kind") or "operation",
            "response": data,
        }
    )


def _spawn_background_op(
    op: Operation,
    handler: OperationHandler,
    run_ctx: OperationRunContext,
    scope: OpScope,
) -> None:
    """Run a wait-heavy operation in the background.

    Background operations are fire-and-forget: they never publish, never add to
    the LLM-facing responses, and any later failure is logged instead of changing
    the already-completed tool run. Both stores are deep-copied, so a write that
    slips through cannot reach the tool that has already moved on — nor the
    session.
    """
    kind = op.get("kind")
    bg_op = deepcopy(op)
    bg_op["publish_fields"] = []
    bg_scope: OpScope = {
        "args": deepcopy(scope.get("args") or {}),
        "tooldata": deepcopy(scope.get("tooldata") or {}),
        "userdata": deepcopy(scope.get("userdata") or {}),
        # Read-only and already rendered, so both copies are the parent's: a
        # background op that POSTs the caller's number, or the tenant's API
        # domain, must send the same one the foreground op did.
        SYSTEM_VARS_ROOT: scope.get(SYSTEM_VARS_ROOT) or {},
        VARS_ROOT: scope.get(VARS_ROOT) or {},
        "secrets": scope.get("secrets") or {},
        "_runtime": scope.get("_runtime") or {},
    }

    async def _run_bg() -> None:
        try:
            await handler(bg_op, run_ctx, bg_scope, ToolRunResult())
        except Exception:
            logger.exception("background operation %s failed", kind)

    spawn(_run_bg())


# ─────────────────────────────── if condition ───────────────────────────────


def _eval_if(cfg: dict[str, object], scope: OpScope) -> bool:
    left = resolve(cfg.get("left"), scope)
    right = resolve(cfg.get("right"), scope)
    op = (cfg.get("op") or "eq").lower()
    if op == "exists":
        return left not in (None, "", [], {})
    if op == "eq":
        return left == right or str(left) == str(right)
    if op == "neq":
        return not (left == right or str(left) == str(right))
    if op in ("gt", "lt"):
        try:
            lf, rf = float(left), float(right)
        except (TypeError, ValueError) as e:
            # Raised, not answered `False`: a comparison that cannot be made has
            # no true side and no false side, and silently taking `else` sent
            # every caller down the wrong branch with nothing to see for it.
            # `{{system_vars.time}}` against "09:00" is how authors meet this.
            raise ValueError(
                f"`{op}` needs two numbers, and cannot compare {left!r} with {right!r}"
            ) from e
        return lf > rf if op == "gt" else lf < rf
    if op == "in":
        choices = right if isinstance(right, list) else str(right or "").split(",")
        return str(left) in [str(c).strip() for c in choices]
    raise ValueError(f"unknown `if` operator {op!r}")


# ─────────────────────────────── op handlers ────────────────────────────────


def _http_request_body(body: object) -> tuple[object | None, str | bytes | None]:
    """Map a resolved body value to (json_body, content).

    Dict/list → JSON. str/bytes → raw content. Other scalars (typed single-token
    resolves like a number) → string content. None → empty body.
    """
    if body is None:
        return None, None
    if isinstance(body, (dict, list)):
        return body, None
    if isinstance(body, (str, bytes)):
        return None, body
    return None, str(body)


def _decoded_body(raw: bytes) -> str:
    """A response body a human can read, for a trace step that has no parsed JSON
    to show — an error status, or a body that isn't JSON at all."""
    text = raw.decode("utf-8", errors="replace")
    return text if len(text) <= 4000 else f"{text[:4000]}… (truncated)"


async def _read_http_response_capped(resp: httpx.Response, *, max_bytes: int) -> bytes:
    """Read the response body with a hard size cap (matches code-exec fetch)."""
    chunks: list[bytes] = []
    size = 0
    async for chunk in resp.aiter_bytes():
        size += len(chunk)
        if size > max_bytes:
            await resp.aclose()
            raise ValueError(f"HTTP response exceeds {max_bytes} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


def _header_value(name: str, value: object) -> str:
    """One resolved header, checked for what an HTTP header cannot carry.

    A header takes every template root, so its value can be a caller's name as
    easily as an API key — and `José` raises `UnicodeEncodeError` deep inside
    httpx while a value carrying a newline is refused by h11 further down. Both
    would surface to the author as the tool's generic apology and to the caller
    as an unexplained failure. Named here instead, where the message can say
    which header and why. (No header injection is possible either way; this is
    about the error being legible.)

    A boolean is spelled the way JSON and the query string spell it, not as
    Python's `True`.
    """
    text = ("true" if value else "false") if isinstance(value, bool) else str(value)
    try:
        text.encode("ascii")
    except UnicodeEncodeError:
        raise ValueError(
            f"the '{name}' header resolved to a value an HTTP header cannot carry "
            f"(non-ASCII): {text!r}"
        ) from None
    if any(c in text for c in "\r\n\0"):
        raise ValueError(
            f"the '{name}' header resolved to a value an HTTP header cannot carry "
            f"(a line break): {text!r}"
        )
    return text


async def _http(
    op: Operation, run_ctx: OperationRunContext, scope: OpScope, res: ToolRunResult
) -> None:
    cfg = op.get("config") or {}
    method = (cfg.get("method") or "GET").upper()
    url = resolve(cfg.get("url") or "", scope)
    if not isinstance(url, str) or not url:
        raise ValueError("HTTP operation has no URL")
    # A header or query value that is exactly one token resolving to nothing — an
    # optional argument the model left out, a userdata key never written — is left
    # out of the request. Sent, it would read as a value: the header `None`, or a
    # `?status=` filter that matches only blanks. Absent is what the template meant.
    resolved_headers = resolve(cfg.get("headers") or {}, scope)
    headers = {k: _header_value(k, v) for k, v in resolved_headers.items() if v is not None}
    resolved_query = resolve(cfg.get("query") or {}, scope)
    params = {k: v for k, v in resolved_query.items() if v is not None} or None
    body = resolve(cfg.get("body"), scope)
    timeout = float(cfg.get("timeout") or HTTP_TIMEOUT_DEFAULT)
    json_body, content = _http_request_body(body)
    # Recorded before the request goes out, so a DNS failure or an SSRF refusal
    # still shows the author what we were about to send. `url` is what their
    # template resolved to, not the IP we pin it to below — the pinning is our
    # guard's business, not theirs.
    record(
        scope,
        request={
            "method": method,
            "url": url,
            "headers": headers,
            "query": params or {},
            "body": body,
            # The number that explains a timeout, next to the request it killed.
            "timeout": timeout,
        },
    )

    # SSRF guard: async DNS (never blocks the call's event loop) + the request is
    # pinned to the vetted IP so a rebinding second resolution can't redirect it.
    # follow_redirects=False — redirects re-resolve DNS unpinned (TOCTOU).
    pinned_url, pin_headers, pin_extensions = await resolve_and_pin(url)
    headers.update(pin_headers)

    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        req = client.build_request(
            method,
            pinned_url,
            headers=headers or None,
            params=params,
            json=json_body,
            content=content,
            extensions=pin_extensions or None,
        )
        resp = await client.send(req, stream=True)
        try:
            raw = await _read_http_response_capped(resp, max_bytes=HTTP_MAX_RESPONSE_BYTES)
            status = resp.status_code
        finally:
            await resp.aclose()

    if status >= 400:
        # Record the failing body before raising: "your server said 500" is half
        # an answer, and the other half is in the body we would otherwise drop.
        record(scope, response={"status": status, "body": _decoded_body(raw)})
        raise ValueError(f"HTTP request failed with status {status}")
    if not raw:
        data: object = {}
    else:
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            record(scope, response={"status": status, "body": _decoded_body(raw)})
            raise ValueError("HTTP operation returned a non-JSON response") from e
    record(scope, response={"status": status, "body": data})
    _publish_and_output(op, scope, res, data)


def _emit_code_logs(logs: object) -> None:
    """Print every console line captured by code-exec so authors can debug."""
    if not isinstance(logs, list) or not logs:
        return
    for line in logs:
        logger.info("code op console: %s", line)


async def _code(
    op: Operation, run_ctx: OperationRunContext, scope: OpScope, res: ToolRunResult
) -> None:
    """AI-authored TypeScript, transpiled to JS at tool publish,
    executed on the separate code-exec service — never in this worker process.
    JSON in / JSON out: the script's handler receives
    {args, tooldata, userdata, system_vars, vars, secrets} and selected fields of
    its returned object publish into tooldata or userdata via publish_fields,
    exactly like an HTTP op's response. `system_vars` and `vars` are read-only
    there like everywhere else — neither is a publish store, and `system_vars`'
    phone-call keys are empty on anything that is not a call.

    That input object **is** the whole interface: a code op has no template
    syntax, so `{{…}}` in the TypeScript is neither substituted nor validated. It
    is `input.system_vars.now`, never `{{system_vars.now}}` — and braces written
    here are the author's own, free to build a template for something
    downstream."""
    from settings import get_settings

    cfg = op.get("config") or {}
    js = cfg.get("compiled_js") or ""
    if not js:
        raise ValueError("code operation has no compiled script — re-publish the tool")
    timeout = min(float(cfg.get("timeout") or CODE_TIMEOUT_DEFAULT), CODE_TIMEOUT_MAX)

    rt = scope.get("_runtime") or {}
    tenant = rt.get("tenant")
    tenant_id = getattr(tenant, "id", None)
    if tenant is None or tenant_id is None:
        raise RuntimeError(
            "code operation is unavailable in this context (no tenant) — "
            "runtime must supply a tenant with an id"
        )

    payload = {
        "js": js,
        "input": {
            "args": scope["args"],
            "tooldata": scope["tooldata"],
            # `_talqing*` keys are the runtime's own session bookkeeping, never a
            # tool author's to read. tooldata never holds them.
            "userdata": {k: v for k, v in scope["userdata"].items() if not is_reserved_key(k)},
            SYSTEM_VARS_ROOT: scope[SYSTEM_VARS_ROOT],
            VARS_ROOT: scope[VARS_ROOT],
            "secrets": scope["secrets"],
        },
        "timeout_ms": int(timeout * 1000),
    }
    url = get_settings().app.code_exec_url.rstrip("/") + "/execute"
    async with httpx.AsyncClient(timeout=timeout + 5.0) as client:
        resp = await client.post(
            url,
            content=json.dumps(payload, default=str),  # userdata may hold non-JSON values
            headers={"content-type": "application/json"},
        )
    resp.raise_for_status()
    data = resp.json()
    logs = data.get("logs")
    _emit_code_logs(logs)
    # On a live call these only ever reach the worker log, where the tool's
    # author cannot see them. On a test run they are the whole point of a
    # `console.log`.
    record(scope, logs=logs if isinstance(logs, list) else [])
    if not data.get("ok"):
        err = data.get("error") or "unknown error"
        if isinstance(logs, list) and logs:
            tail = " | ".join(str(line) for line in logs[-10:])
            raise RuntimeError(f"code operation failed: {err} (console: {tail})")
        raise RuntimeError(f"code operation failed: {err}")

    result = data.get("result")
    if not isinstance(result, (dict, list)):
        result = {"result": result}  # a bare return value publishes as `result`
    record(scope, result=result)
    _publish_and_output(op, scope, res, result)


async def _end_call(
    op: Operation, run_ctx: OperationRunContext, scope: OpScope, res: ToolRunResult
) -> None:
    from workers.session.runtime_kind import is_text_runtime

    rt = scope.get("_runtime") or {}
    if is_text_runtime(rt):
        res.end_call = True
        return

    job = get_job_context()

    def _on_session_close(ev: CloseEvent) -> None:
        async def _del() -> None:
            try:
                await job.delete_room()
            except Exception:
                logger.exception("delete_room failed on end_call")

        job.add_shutdown_callback(_del)
        reason = getattr(getattr(ev, "reason", None), "value", "end_call")
        job.shutdown(reason=reason)

    run_ctx.session.once("close", _on_session_close)

    # Wait for EVERYTHING outstanding, not just for whichever handle happens to
    # be last. Two separate things can be mid-flight, and the caller is owed both:
    #
    #   1. the LLM's own speech from before this tool ran ("sure, I'll close this
    #      out for you") — `run_ctx.wait_for_playout()`, LiveKit's answer to the
    #      fact that `await run_ctx.speech_handle` raises on a circular wait;
    #   2. anything the tree itself queued (a `say` goodbye) — that handle was
    #      created here, so awaiting it directly is fine.
    #
    # Speech is FIFO, so (2) transitively implies (1) — but that is an
    # undocumented property of someone else's scheduler, and this operation is
    # not the place to depend on it silently.
    #
    # Awaits rather than a done-callback because of the failure mode: if a handle
    # never resolves (stalled TTS), a callback never fires, `shutdown()` is never
    # called, and the session lives until `max_call_duration` — three hours, with
    # no recovery path. The timeout is what makes the `finally` unconditional,
    # so the one operation whose entire job is ending the call always ends it.
    # Measured equivalent to the callback for the caller once the follow-up reply
    # is suppressed (compiler/tools.py raises StopResponse for end_call).
    try:
        await asyncio.wait_for(run_ctx.wait_for_playout(), timeout=SPEECH_WAIT_TIMEOUT)
        if res.last_speech_handle is not None:
            await asyncio.wait_for(res.last_speech_handle, timeout=SPEECH_WAIT_TIMEOUT)
    except Exception:
        logger.warning("end_call: speech did not finish; hanging up anyway", exc_info=True)
    finally:
        run_ctx.session.shutdown()

    res.end_call = True


async def _transfer(
    op: Operation, run_ctx: OperationRunContext, scope: OpScope, res: ToolRunResult
) -> None:
    """Hand the caller to a human being and get out of the way.

    Shaped on `_end_call`, not on `_handoff`: like ending the call, this is a
    terminal act whose whole correctness rests on the caller having heard
    whatever was queued before it.

    `mode` is the builder's only choice about *how*: `cold` hands the caller
    straight over, `warm` holds them while the agent briefs the person answering.
    Which transport carries either — SIP REFER, or a bridge into the caller's own
    room — is the platform's, decided from the carrier's capability inside
    `workers/voice/transfer.py`. Neither the builder nor this module needs to
    know which one ran.

    `destination` is used literally and is never passed through `resolve()`.
    That is what makes it a deliberate, attributed, reviewable act: a
    template-resolved number is a toll-fraud vector via prompt injection, and a
    published tool version is the only place a destination should be decidable.

    There is no `message` key. A line before the transfer is a `say` placed
    before it, exactly as a goodbye before `end_call` is — one way to make the
    agent speak, visible as its own node in the editor's flowchart, and
    `{{args.*}}` templating for free.
    """
    cfg = op.get("config") or {}
    mode = str(cfg.get("mode") or "cold")
    destination = str(cfg.get("destination") or "").strip()
    if not destination:
        raise ValueError("transfer operation has no destination")
    ringing_timeout = float(cfg.get("ringing_timeout") or TRANSFER_RINGING_TIMEOUT_DEFAULT)
    on_failure = str(cfg.get("on_failure") or "continue")

    rt = scope.get("_runtime") or {}
    perform = rt.get(RUNTIME_KEY_TRANSFER) if isinstance(rt, dict) else None
    if not callable(perform):
        # Only the SIP worker injects the key, so this is every web-room and
        # text run. Worded for the builder reading it in a tool test, not for
        # the caller — publish validation cannot catch it, because the same tool
        # may be attached to a phone agent and a chat agent.
        raise RuntimeError("transfer is only available on phone calls")

    # Not the builder's `disable_interruptions` flag, and not optional. A transfer
    # dial runs for up to `ringing_timeout` (30s by default) while the caller
    # hears ringback and nothing else — long enough that "hello?" into that gap is
    # the normal thing for a caller to do, and an interrupted speech handle is
    # dead 5s later (`compiler/speech.py::disable_interruptions`). The handle is
    # the only way the outcome reaches the caller, so losing it means a human's
    # phone rings and neither party is ever told what happened. Nothing about a
    # transfer is abandonable half-way, so nothing is given up by pinning it.
    #
    # Before the wait below, not after: the wait is itself inside the window an
    # interruption can land in, and once the handle is interrupted this raises
    # rather than proceeding into a dial whose result is already unspeakable.
    disable_interruptions(run_ctx)

    # Same wait as `_end_call`, for the same reason and with the same bound: the
    # caller is owed both the LLM's own speech from before this tool ran and
    # anything the tree queued (a "connecting you now" `say`). The timeout is
    # what stops a stalled TTS provider hanging the call — and it is why a
    # preceding `say` does not need `wait_for_playback`.
    try:
        await asyncio.wait_for(run_ctx.wait_for_playout(), timeout=SPEECH_WAIT_TIMEOUT)
        if res.last_speech_handle is not None:
            await asyncio.wait_for(res.last_speech_handle, timeout=SPEECH_WAIT_TIMEOUT)
    except Exception:
        logger.warning("transfer: speech did not finish; transferring anyway", exc_info=True)

    outcome = await perform(
        mode=mode,
        destination_e164=destination,
        ringing_timeout=ringing_timeout,
        # What happens next if this does not connect. Passed down so the record
        # on the session says how the call actually ended rather than leaving
        # every reader to assume the default: the call detail page used to state
        # "the call carried on with the agent" under a policy that hung up.
        on_failure=on_failure,
    )

    if outcome.ok:
        # Not because we are hanging up — the transport already did — but
        # because this is the flag that stops the model narrating into a session
        # with no caller left in it. A `transfer` having no `silent` field of
        # its own is not enough: a perfectly ordinary tool of [http, transfer]
        # is not a silent tree, so without this the model is handed the HTTP
        # response and generates a reply to an empty room.
        res.end_call = True
        return

    if on_failure == "end_call":
        # Delegated rather than flagged: `res.end_call = True` alone does not
        # hang up a voice call. `_end_call` is what registers the close →
        # delete_room callback and shuts the session down, and it reads nothing
        # from its own `op`, so handing it this node is correct.
        await _end_call(op, run_ctx, scope, res)
        return

    # The caller is still on the line and the agent can recover. `detail` is
    # plain English about the person we could not reach — never a SIP code.
    #
    # Told to the model as an instruction, not as a bare fragment. Handed only
    # "nobody answered", a model that has just watched a tool fail mid-turn will
    # sometimes re-open the conversation instead of reporting it — observed on a
    # live call, where a failed transfer was followed by the agent greeting the
    # caller from scratch, three times, in the wrong language. The last two
    # sentences exist to stop exactly that.
    raise ToolError(
        f"You could not put the caller through: {outcome.detail or _TRANSFER_FAILED}. "
        "Tell the caller in your own words, in the language you have been speaking, "
        "then carry on helping them yourself. Do not greet them and do not start the "
        "conversation over."
    )


async def _set_variable(
    op: Operation, run_ctx: OperationRunContext, scope: OpScope, res: ToolRunResult
) -> None:
    """Write one templated value into the store named by ``store``. Same choice
    as a publish field, for a value the author supplies rather than one an
    operation returned. Value supports {{args/tooldata/userdata/secrets}}
    templating and keeps typed single-token resolves.

    Nothing goes back to the LLM. The `{key: value}` echo this used to append was
    redundant — the value is either static config or came from `args`, both of
    which the model already has — and it kept whole tools out of the derived
    all-silent case for no gain. The durable path (the store write) is untouched."""
    cfg = op.get("config") or {}
    key = str(cfg.get("key") or "").strip()
    if not key:
        raise ValueError("set_variable: a variable name is required")
    store = cfg.get("store")
    if store not in PUBLISH_STORES:
        raise ValueError(f"set_variable: store must be one of {', '.join(PUBLISH_STORES)}")
    value = resolve(cfg.get("value"), scope)
    _write(scope, store, key, value)
    _record_published(scope, store, key, value)
    record(scope, store=store, key=key, value=value)


async def _await_playback(op: Operation, handle: SpeechHandle) -> None:
    """Hold the tree until this line has finished playing, if the author asked.

    Off by default, and deliberately so: the archetypal `say` is a filler that
    exists *to cover* the next operation's latency ("let me pull that up" →
    `http` → "your balance is …"). Waiting there would serialise every call for
    no benefit.

    Ticked, it is a gate — the next operation does not start until the caller
    has heard this line. Two properties worth knowing:

    - **The wait is transitive.** Speech is FIFO, so awaiting our own line also
      waits for whatever the LLM queued earlier in the same turn. On a line that
      must land before a side effect that is the point; on a filler it stalls
      the call for the length of the model's preamble as well as our own.
    - **An interrupted handle completes.** This waits for the line to finish
      *or be cut off*, so it is not a guarantee the caller heard it. Pair it
      with tool-level `disable_interruptions` for a compliance disclosure.

    Awaiting is safe here because the handle was created inside this tool. The
    one that raises is `run_ctx.speech_handle`, which owns this tool call.
    """
    if not (op.get("config") or {}).get("wait_for_playback"):
        return
    try:
        await asyncio.wait_for(handle, timeout=SPEECH_WAIT_TIMEOUT)
    except TimeoutError as e:
        # Bounded so a stalled TTS provider fails the operation instead of
        # hanging the turn forever. Loud, per AGENTS.md: `_run` turns this into
        # the tool's graceful error and honours the operation's on_error.
        raise RuntimeError(
            f"the spoken line did not finish playing within {SPEECH_WAIT_TIMEOUT:g}s"
        ) from e


async def _say(
    op: Operation, run_ctx: OperationRunContext, scope: OpScope, res: ToolRunResult
) -> None:
    """Speak fixed text verbatim (approximately so on a realtime pipeline)."""
    cfg = op.get("config") or {}
    text = resolve(cfg.get("text"), scope) if cfg.get("text") else None
    if not text:
        return
    res.last_speech_handle = speak(run_ctx.session, str(text))  # queued; the tree keeps running
    await _await_playback(op, res.last_speech_handle)


async def _generate_reply(
    op: Operation, run_ctx: OperationRunContext, scope: OpScope, res: ToolRunResult
) -> None:
    """Generate a reply via LiveKit's session.generate_reply."""
    cfg = op.get("config") or {}
    instructions = resolve(cfg.get("instructions"), scope) if cfg.get("instructions") else None
    if not instructions:
        return
    res.last_speech_handle = run_ctx.session.generate_reply(instructions=str(instructions))
    await _await_playback(op, res.last_speech_handle)


async def _add_message(
    op: Operation, run_ctx: OperationRunContext, scope: OpScope, res: ToolRunResult
) -> None:
    """Add a templated system message to chat context without triggering a reply."""
    cfg = op.get("config") or {}
    text = resolve(cfg.get("text"), scope) if cfg.get("text") else None
    if not text:
        return

    message = llm.ChatMessage(role="system", content=[str(text)])
    session = run_ctx.session
    if getattr(run_ctx, "turn_ctx", None) is not None:
        run_ctx.turn_ctx.insert(message)

    # Public AgentSession APIs only: current_agent + history + event emit.
    # Avoid session._agent / session._conversation_item_added (private).
    try:
        agent = session.current_agent
    except RuntimeError:
        agent = None
    if agent is not None:
        chat_ctx = agent.chat_ctx.copy()
        chat_ctx.insert(message)
        await agent.update_chat_ctx(chat_ctx)

    if not any(getattr(item, "id", None) == message.id for item in session.history.items):
        session.history.insert(message)
    # Notify transcript listeners the same way LiveKit does for framework items.
    session.emit("conversation_item_added", ConversationItemAddedEvent(item=message))


async def perform_handoff(
    session: Any,
    runtime: RuntimeContext,
    userdata: UserData,
    *,
    target_agent_id: str | None,
    target_name: str | None,
    context_policy: str = "transcript",
    summary: str | None = None,
    recent_turns: int | None = None,
    via: str = "handoffs",
    message: str | None = None,
) -> tuple[Any, SpeechHandle | None]:
    """Build the agent this call is handing to, and say the line while it loads.

    The one implementation, shared by the generated `handoff_to_*` tools and by
    the `handoff` operation, so a routing handoff and a conditional one cannot
    end up behaving differently. Returns the compiled target plus the speech
    handle for the line, if there was one.

    Under `context_policy="summary"`, `summary` is the text that crosses — the
    argument the model wrote on its `handoff_to_*` tool, or the operation's own
    resolved `summary` template. `via` tells the two apart in the trace.

    The builder itself is injected on the RuntimeContext under
    RUNTIME_KEY_BUILD_HANDOFF_AGENT (workers wire
    ``compiler.handoff.build_handoff_agent``), so the tree runner never imports
    compiler.handoff / compiler.compile.
    """
    if not isinstance(runtime, dict):
        raise ValueError("handoff is unavailable in this context (invalid runtime)")
    tenant = runtime.get("tenant")
    if tenant is None:
        raise ValueError("handoff is unavailable in this context (no tenant)")
    builder = runtime.get(RUNTIME_KEY_BUILD_HANDOFF_AGENT)
    if builder is None or not callable(builder):
        raise ValueError("handoff is unavailable in this context (no handoff builder)")
    if not isinstance(userdata, dict):
        raise RuntimeError("userdata must be a dictionary")

    # Session bookkeeping: the cap is on the call, not on one tool's tree — and
    # it matters more on a team, which is where a routing loop actually happens.
    count = int(userdata.get("_talqing_handoffs", 0)) + 1
    if count > MAX_HANDOFFS:
        # "Transfer" now means handing the caller to a *person*. Saying it here
        # would make an agent→agent handoff and an agent→human transfer
        # indistinguishable in the transcript and in post-call analysis.
        raise ToolError("I'm sorry, I can't pass you to another assistant again on this call.")
    userdata["_talqing_handoffs"] = count

    # Queued BEFORE the target is built, which is the whole point of the message:
    # building it loads the target's definition and compiles its prompt, tools
    # and voice, and the caller should hear "connecting you to billing…" during
    # that, not after it. No flag to finish speaking first — there is nothing to
    # gate. `session.update_agent` drains queued speech before the target's
    # `on_enter` runs, so this line always plays to the end regardless. If the
    # build then fails the caller hears the promise followed by the graceful
    # apology — the honest order, and a target that cannot be compiled is a
    # deploy-time error, not a routine outcome.
    handle = speak(session, message) if message else None

    agent = await builder(
        tenant,
        session,
        target_agent_id=target_agent_id,
        target_name=target_name,
        context_policy=context_policy,
        summary=summary,
        recent_turns=recent_turns,
        via=via,
        runtime_id=str(runtime.get("runtime_id")) if runtime.get("runtime_id") else None,
        runtime_context=runtime,
    )
    return agent, handle


async def _handoff(
    op: Operation, run_ctx: OperationRunContext, scope: OpScope, res: ToolRunResult
) -> None:
    """Hand the conversation to another agent from inside an operation tree.

    The routing case — "billing questions go to Billing" — belongs on
    `AgentConfig.handoffs`, which the compiler turns into one tool per
    destination. This operation is the PROCEDURAL case: look the account up, and
    *if* it is enterprise, hand to the enterprise desk. It can name either a
    stored agent or a member of this call's team.

    Its tree runs AFTER the model's tool call, so there is no model-written
    argument to take a summary from at that moment. Instead the `summary` config
    key is an ordinary template: `{{args.summary}}` — a property on the tool's
    own schema that the model filled — or `{{tooldata.brief}}` from an earlier
    `http` op, or fixed prose. Same meaning as the config field's summary, text
    handed across the boundary and never a second LLM call, and strictly more
    powerful, which is the right relationship between the procedural form and
    the routing one.
    """
    cfg = op.get("config") or {}
    target_agent_id = str(resolve(cfg.get("target_agent_id") or "", scope) or "") or None
    target_name = str(resolve(cfg.get("agent_name") or "", scope) or "") or None
    if not target_agent_id and not target_name:
        raise ValueError("handoff operation has no target_agent_id or agent_name")
    message = resolve(cfg.get("message"), scope) if cfg.get("message") else None
    summary = resolve(cfg.get("summary"), scope) if cfg.get("summary") else None
    recent_turns = cfg.get("recent_turns")
    res.handoff, res.last_speech_handle = await perform_handoff(
        run_ctx.session,
        scope.get("_runtime") or {},
        scope["userdata"],
        target_agent_id=target_agent_id,
        target_name=target_name,
        context_policy=(cfg.get("context") or "transcript"),
        summary=str(summary) if summary else None,
        recent_turns=int(recent_turns) if recent_turns is not None else None,
        via="operation",
        message=str(message) if message else None,
    )


async def _frontend_rpc(
    op: Operation, run_ctx: OperationRunContext, scope: OpScope, res: ToolRunResult
) -> None:
    """Call the connected client's frontend RPC handler.

    The Talqing browser SDK registers one envelope method, `talqing.frontend_rpc`,
    and dispatches by envelope.method. This awaits the client response so tools
    can read UI state and return
    acknowledgements to the LLM.
    """
    from workers.session.runtime_kind import is_text_runtime

    cfg = op.get("config") or {}
    rt = scope.get("_runtime") or {}
    if is_text_runtime(rt):
        raise RuntimeError("frontend_rpc is unavailable for text runs")
    method = cfg.get("method") or ""
    if not method:
        raise ValueError("frontend_rpc operation has no method")
    payload = resolve(cfg.get("payload") if cfg.get("payload") is not None else {}, scope)
    timeout = float(cfg.get("timeout") or FRONTEND_RPC_TIMEOUT)
    envelope = json.dumps({"method": method, "payload": payload}, default=str)

    from livekit import rtc

    job = get_job_context()
    desired_identity = (scope.get("_runtime") or {}).get("participant_identity")
    if isinstance(desired_identity, str) and desired_identity:
        target = job.room.remote_participants.get(desired_identity)
        if target is None:
            raise RuntimeError(f"frontend_rpc target {desired_identity!r} is not connected")
        if target.kind != rtc.ParticipantKind.PARTICIPANT_KIND_STANDARD:
            raise RuntimeError("frontend_rpc target is not a standard web participant")
    else:
        standard = [
            p
            for p in job.room.remote_participants.values()
            if p.kind == rtc.ParticipantKind.PARTICIPANT_KIND_STANDARD
        ]
        if len(standard) > 1:
            raise RuntimeError("frontend_rpc has multiple web clients but no target identity")
        target = standard[0] if standard else None
    if target is None:
        raise RuntimeError("frontend_rpc has no connected web client participant")

    response = await job.room.local_participant.perform_rpc(
        destination_identity=target.identity,
        method=FRONTEND_RPC_METHOD,
        payload=envelope,
        response_timeout=timeout,
    )

    if not isinstance(response, str):
        raise TypeError(f"frontend_rpc response must be str, got {type(response).__name__}")
    try:
        data = json.loads(response)
    except json.JSONDecodeError:
        # Non-JSON string replies are a supported client contract: wrap once.
        data = {"result": response}

    _publish_and_output(op, scope, res, data)


# Between digits, matching the agents SDK's own `DEFAULT_DTMF_PUBLISH_DELAY`.
# An IVR that misses a digit sent too fast is the failure this prevents, and
# copying the constant rather than choosing one keeps us on whatever the SDK
# learns about real telephony.
DTMF_DIGIT_DELAY = 0.3
# What `w` and `W` mean, in seconds. ElevenLabs' spelling, and the reason a
# single `digits` string can navigate a real menu.
DTMF_PAUSES: dict[str, float] = {"w": 0.5, "W": 1.0}


async def _send_dtmf(
    op: Operation, run_ctx: OperationRunContext, scope: OpScope, res: ToolRunResult
) -> None:
    """Press keys on the call's keypad — for an IVR on the other end of it.

    `digits` IS template-resolved, unlike `transfer`'s `destination`, and the
    distinction is worth stating because it looks inconsistent. A transfer
    destination is a *who*: it is decided at publish time, and resolving it would
    make prompt injection a toll-fraud vector. IVR digits are a *what*, and they
    are only useful when they come from the call — an account number out of
    `userdata`, a menu choice the model just heard read out. Both Vapi and
    ElevenLabs let the model choose them outright.
    """
    rt = scope.get("_runtime") or {}
    send = rt.get(RUNTIME_KEY_SEND_DTMF) if isinstance(rt, dict) else None
    if not callable(send):
        # Worded for the builder reading it in a tool test, not for the caller.
        # "phone calls only" would be wrong for a Twilio stream, which IS a phone
        # call — the protocol simply has no command for this.
        raise RuntimeError(
            "this call has no keypad to press - the agent can send DTMF on a phone call, "
            "and on a media stream only where the partner's platform supports it"
        )
    cfg = op.get("config") or {}
    digits = str(resolve(cfg.get("digits"), scope) or "").strip()
    if not digits:
        raise ValueError("send_dtmf operation has no digits")
    # The literal case was already refused at save; this is the template case,
    # checked against what `{{args.account_number}}` actually resolved to.
    unknown = sorted(set(digits) - DTMF_KEYS)
    if unknown:
        raise ValueError(
            f"a keypad has no {''.join(unknown)!r} - the digits resolved to {digits!r}"
        )

    for key in digits:
        pause = DTMF_PAUSES.get(key)
        if pause is not None:
            await asyncio.sleep(pause)
            continue
        if key == " ":
            continue
        await send(key)
        await asyncio.sleep(DTMF_DIGIT_DELAY)


HANDLERS: dict[str, OperationHandler] = {
    "http": _http,
    "code": _code,
    "end_call": _end_call,
    "say": _say,
    "generate_reply": _generate_reply,
    "add_message": _add_message,
    "set_variable": _set_variable,
    "handoff": _handoff,
    "transfer": _transfer,
    "send_dtmf": _send_dtmf,
    "frontend_rpc": _frontend_rpc,
}

# What publish accepts and what the runtime can execute, held together. A kind
# in the first and not the second passes validation and then dies mid-call at
# `_run`'s raise; `if` is the one kind with no handler because `_run` is its
# implementation. `TREE_KINDS` is derived from the operation variants, so after
# this assertion the two tables are linked rather than merely equal today.
assert TREE_KINDS == set(HANDLERS) | {"if"}, (
    f"operation kinds and runtime handlers disagree: {TREE_KINDS ^ (set(HANDLERS) | {'if'})}"
)
