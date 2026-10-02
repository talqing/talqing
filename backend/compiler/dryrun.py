"""Run one tool once, from the dashboard, and report every step.

This is the same walk a live call performs — `compiler.operations.execute_tree`,
the real HTTP client, the real SSRF guard, the real code-exec isolate, the real
secrets. Only two things differ:

- The eight operation kinds that need a live conversation (`say`,
  `generate_reply`, `add_message`, `end_call`, `handoff`, `transfer`,
  `send_dtmf`, `frontend_rpc`) are swapped for recorders. They resolve their templates and
  report what they *would* have said or done. Refusing to run any tree
  containing a `say` would block most real tools and answer nothing; the
  question an author actually has is whether `{{args.name}}` interpolated
  correctly, and only a resolved string answers it. `transfer` is the one where
  running it for real would ring a stranger's phone.
- Every operation writes a `TraceStep`, so the branch an `if` took and the value
  each operation published are visible instead of guessed at.

Nothing here is reachable from a call. `execute_tree` takes `dry_run=None` on
every live path and none of this runs.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from compiler.operations import (
    HANDLERS,
    TRANSFER_RINGING_TIMEOUT_DEFAULT,
    DryRun,
    OperationHandler,
    OpScope,
    PublishFieldMissing,
    ToolRunResult,
    TraceStep,
    dry_run_state,
    execute_tree,
)
from services.tools import (
    MissingSecretError,
    Operation,
    RuntimeContext,
    filter_secrets,
    resolve,
)

logger = logging.getLogger("talqing.compiler.dryrun")

# A tree of ten HTTP operations at the 20 s default would otherwise hold an API
# worker for 200 s. The cap is on the whole run, not one operation.
RUN_BUDGET_SECONDS = 60.0

REDACTED = "••••••"
# A one- or two-character secret would blank half the trace. Below this length a
# value is not distinctive enough to be worth masking.
MIN_REDACTABLE_SECRET = 4

# What the author sees instead of a failure they cannot act on.
ERROR_TYPES = (
    "tool_config",  # the tree is wrong — bad config, publish path not in the output
    "tool_auth",  # a referenced secret is missing
    "endpoint_error",  # their server answered, and the answer was unusable
    "endpoint_unreachable",  # their server could not be reached at all
    "timeout",
    "code_error",  # the TypeScript threw
    "platform",  # ours — code-exec is down
    "unknown",
)


# ────────────────────────────── the stub context ─────────────────────────────


@dataclass
class _StubSession:
    """The only thing the non-simulated operations touch is `userdata`."""

    userdata: dict[str, Any]


@dataclass
class _StubRunContext:
    """Structurally an `OperationRunContext`, with nothing behind it.

    With the session-bound kinds swapped out, no handler reaches a room, a
    speech handle or a chat context — so this stays three fields rather than a
    fake AgentSession.
    """

    session: _StubSession
    speech_handle: None = None
    turn_ctx: None = None

    async def wait_for_playout(self) -> None:
        """Never reached — `end_call` is one of the simulated kinds. Present so
        the stub satisfies `OperationRunContext` in full."""


# ──────────────────────────── the simulated kinds ────────────────────────────


def _simulated(scope: OpScope, **detail: Any) -> None:
    dry = dry_run_state(scope)
    if dry is None or dry.current is None:  # pragma: no cover - only ever a test run
        raise RuntimeError("a simulated operation ran outside a test run")
    dry.current.status = "simulated"
    dry.current.detail.update(detail)


def _resolved(cfg: dict[str, Any], key: str, scope: OpScope) -> Any:
    """Resolve one templated config field, or None when the author left it empty
    — which is what the live handlers treat as "do nothing"."""
    return resolve(cfg.get(key), scope) if cfg.get(key) else None


async def _say(op: Operation, _ctx: Any, scope: OpScope, _res: ToolRunResult) -> None:
    cfg = op.get("config") or {}
    # A test run has no audio to wait for, so the flag does nothing here — but it
    # is reported, because an author who ticked it should see it reflected rather
    # than wonder whether the trace ran the tree they are looking at.
    _simulated(
        scope,
        text=_resolved(cfg, "text", scope),
        wait_for_playback=bool(cfg.get("wait_for_playback")),
    )


async def _generate_reply(op: Operation, _ctx: Any, scope: OpScope, _res: ToolRunResult) -> None:
    cfg = op.get("config") or {}
    _simulated(
        scope,
        instructions=_resolved(cfg, "instructions", scope),
        wait_for_playback=bool(cfg.get("wait_for_playback")),
    )


async def _add_message(op: Operation, _ctx: Any, scope: OpScope, _res: ToolRunResult) -> None:
    _simulated(scope, text=_resolved(op.get("config") or {}, "text", scope))


async def _end_call(_op: Operation, _ctx: Any, scope: OpScope, res: ToolRunResult) -> None:
    res.end_call = True
    _simulated(scope)


async def _transfer(op: Operation, _ctx: Any, scope: OpScope, res: ToolRunResult) -> None:
    """Reports the destination and mode, and dials nobody.

    The one simulated kind where running it for real would ring a stranger's
    phone. It halts the chain the same way a live transfer does — `res.end_call`
    is what the tool test panel reads as "the tool ended the call"."""
    cfg = op.get("config") or {}
    _simulated(
        scope,
        mode=cfg.get("mode") or "cold",
        destination=cfg.get("destination") or "",
        ringing_timeout=cfg.get("ringing_timeout") or TRANSFER_RINGING_TIMEOUT_DEFAULT,
        on_failure=cfg.get("on_failure") or "continue",
    )
    res.end_call = True


async def _send_dtmf(op: Operation, _ctx: Any, scope: OpScope, _res: ToolRunResult) -> None:
    """Reports the digits it resolved to, and presses nothing.

    Simulated for the same reason `say` is rather than the reason `transfer` is:
    there is no call, so the live handler would refuse with "this call has no
    keypad" and answer none of the question an author has. What they want to know
    is whether `{{args.account_number}}` interpolated, and only the resolved
    string says."""
    _simulated(scope, digits=_resolved(op.get("config") or {}, "digits", scope))


async def _frontend_rpc(op: Operation, _ctx: Any, scope: OpScope, _res: ToolRunResult) -> None:
    cfg = op.get("config") or {}
    _simulated(
        scope,
        method=cfg.get("method") or "",
        payload=resolve(cfg.get("payload") if cfg.get("payload") is not None else {}, scope),
    )


async def _handoff(op: Operation, _ctx: Any, scope: OpScope, _res: ToolRunResult) -> None:
    """Records the target and stops, because that is what a handoff does to the
    chain. The target agent is never loaded or compiled — a test run must not
    build a second agent as a side effect."""
    cfg = op.get("config") or {}
    _simulated(
        scope,
        target_agent_id=resolve(cfg.get("target_agent_id") or "", scope),
        agent_name=_resolved(cfg, "agent_name", scope),
        message=_resolved(cfg, "message", scope),
        context=cfg.get("context") or "transcript",
        # Resolved, not the template: a handoff whose summary reads
        # `{{args.summary}}` is asked one question by its author — what would
        # actually have crossed — and the raw string does not answer it.
        summary=_resolved(cfg, "summary", scope),
        recent_turns=cfg.get("recent_turns"),
    )
    dry = dry_run_state(scope)
    if dry is not None:
        dry.halted = True


SIMULATED_KINDS = (
    "say",
    "generate_reply",
    "add_message",
    "end_call",
    "handoff",
    "transfer",
    "send_dtmf",
    "frontend_rpc",
)

TEST_HANDLERS: dict[str, OperationHandler] = {
    **HANDLERS,
    "say": _say,
    "generate_reply": _generate_reply,
    "add_message": _add_message,
    "end_call": _end_call,
    "handoff": _handoff,
    "transfer": _transfer,
    "send_dtmf": _send_dtmf,
    "frontend_rpc": _frontend_rpc,
}

# A test run swaps handlers, it never adds or drops a kind: an operation the
# dashboard's panel silently skipped would be the one thing a test is for.
assert set(TEST_HANDLERS) == set(HANDLERS), (
    f"the test run and the runtime disagree on kinds: {set(TEST_HANDLERS) ^ set(HANDLERS)}"
)
assert set(SIMULATED_KINDS) <= set(TEST_HANDLERS), (
    f"simulated kinds with no handler: {set(SIMULATED_KINDS) - set(TEST_HANDLERS)}"
)


# ───────────────────────────── error classification ──────────────────────────


def classify(step: TraceStep | None, exc: BaseException) -> str:
    """Name whose problem a failure is, structurally rather than by message.

    An HTTP step tells us most of it by what it managed to record: no request
    means the operation's own config was unusable, a request with no response
    means we never reached them, and a response means they answered.
    """
    if isinstance(exc, PublishFieldMissing):
        return "tool_config"
    if isinstance(exc, MissingSecretError):
        return "tool_auth"
    # asyncio.TimeoutError is TimeoutError from 3.11; httpx's is its own tree.
    if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
        return "timeout"
    if step is not None and step.kind == "code":
        # httpx here is us failing to reach code-exec, not the author's script.
        return "platform" if isinstance(exc, httpx.HTTPError) else "code_error"
    if isinstance(exc, (httpx.ConnectError, httpx.NetworkError)):
        return "endpoint_unreachable"
    if step is not None and step.kind == "http":
        if "request" not in step.detail:
            return "tool_config"
        return "endpoint_error" if "response" in step.detail else "endpoint_unreachable"
    if isinstance(exc, ValueError):
        return "tool_config"
    return "unknown"


# ─────────────────────────────────── redaction ───────────────────────────────


def redactable(secrets: dict[str, str]) -> list[str]:
    """The secret values worth masking, from a {name: value} map.

    Public because every surface that shows a run to a human redacts against the
    same list — a tool test run here, and a task run's trace
    (``services.tasks.run``)."""
    return [v for v in secrets.values() if isinstance(v, str) and len(v) >= MIN_REDACTABLE_SECRET]


def redact(value: Any, values: list[str]) -> Any:
    """Mask every secret value anywhere in a payload bound for the browser.

    Applied to the whole trace, not just headers: a token rides just as easily in
    a query string, a JSON body, a `console.log`, or a value the tool published.
    A test run's response also reaches CoPilot, where it would be written into a
    stored transcript.
    """
    if not values:
        return value
    if isinstance(value, str):
        for secret in values:
            value = value.replace(secret, REDACTED)
        return value
    if isinstance(value, dict):
        return {k: redact(v, values) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, values) for v in value]
    return value


# ─────────────────────────────────── the run ─────────────────────────────────


@dataclass
class TestRun:
    """Everything a test run produced, already redacted."""

    ok: bool
    duration_ms: int
    responses: list[dict[str, Any]] = field(default_factory=list)
    tooldata: dict[str, Any] = field(default_factory=dict)
    userdata: dict[str, Any] = field(default_factory=dict)
    steps: list[TraceStep] = field(default_factory=list)
    error: str | None = None
    error_type: str | None = None
    ended_call: bool = False
    handed_off: bool = False


async def run_tree(
    tree: list[Operation],
    *,
    secrets: dict[str, str],
    arguments: dict[str, Any],
    userdata: dict[str, Any],
    runtime: RuntimeContext,
    budget: float = RUN_BUDGET_SECONDS,
) -> TestRun:
    """Execute `tree` once and report what every operation did.

    `userdata` seeds the session bag and is mutated in place by the run, exactly
    as the live session dict is — the copy the caller passes in is the throwaway
    that stands in for a session.
    """
    # Pre-flight the secrets so a missing one is named precisely. `execute_tree`
    # refuses the same tree before the walk, with the caller's apology rather
    # than the name, and there is no step to attribute it to.
    try:
        filter_secrets(secrets, tree)
    except ValueError as e:
        return TestRun(ok=False, duration_ms=0, error=str(e), error_type="tool_auth")

    dry = DryRun(handlers=TEST_HANDLERS)
    run_ctx = _StubRunContext(session=_StubSession(userdata=userdata))
    started = time.perf_counter()
    error: str | None = None
    error_type: str | None = None
    res = ToolRunResult()

    try:
        async with asyncio.timeout(budget):
            res = await execute_tree(
                tree,
                run_ctx,  # type: ignore[arg-type]  # structurally an OperationRunContext
                secrets,
                arguments,
                runtime=runtime,
                dry_run=dry,
            )
    except TimeoutError:  # asyncio.timeout raises the builtin from 3.11
        error = f"the run exceeded the {budget:g}s limit for a test"
        error_type = "timeout"
    except Exception as e:  # noqa: BLE001 — a test run reports failures, never raises them
        # `_run` wraps an aborting operation in a ToolError carrying the generic
        # line the caller would have heard ("Sorry, I wasn't able to…"). That is
        # the wrong thing to show an author, so prefer the step's own message and
        # then the cause underneath the wrapper.
        cause = e.__cause__ or e
        failed = next((s for s in reversed(dry.steps) if s.status == "failed"), None)
        error = (
            failed.error
            if failed is not None and failed.error
            else str(cause) or type(cause).__name__
        )
        error_type = classify(failed, cause)
        logger.info("test run failed (%s): %s", error_type, error)

    duration_ms = int((time.perf_counter() - started) * 1000)
    # A timeout leaves the in-flight step open; close it so the trace ends
    # somewhere rather than trailing off.
    if error_type == "timeout" and dry.current is not None:
        dry.current.status = "failed"
        dry.current.error = error

    values = redactable(secrets)
    for step in dry.steps:
        step.detail = redact(step.detail, values)
        step.published = redact(step.published, values)
        step.error = redact(step.error, values)

    return TestRun(
        ok=error is None,
        duration_ms=duration_ms,
        responses=redact(res.responses, values),
        tooldata=redact(dry.tooldata, values),
        userdata=redact(userdata, values),
        steps=dry.steps,
        error=redact(error, values),
        error_type=error_type,
        ended_call=res.end_call,
        handed_off=dry.halted,
    )
