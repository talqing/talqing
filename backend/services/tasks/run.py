"""Run one task once, for real, and report what it did.

One coroutine, two callers, no queue: the API calls it inline and returns the
outcome in the response, and (from Phase B) a drafting pass calls it once per
row. Both pass a config whose tools are pinned — normally the task's published
version — so a task cannot behave one way in the editor and another in a batch.

``compiler.tasks.compile_task_session`` builds the session and the agent: the
same ``CompiledAgentTask`` an agent enters mid-call, on a session of its own with
no media at all. One compile, proven twice — a task cannot behave one way in the
editor and another on a call.

Deliberately NOT on ``scheduled_jobs``: a manual run is a synchronous
request/response the API answers inline, and a job row would put a queue between
a person and the button they pressed. A batch's drafting pass is the caller that
*is* on ``scheduled_jobs``, and it reaches this the same way — one coroutine,
awaited.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import asyncpg
from livekit.agents import AgentSession, APIError
from livekit.agents.voice.run_result import RunResult

import db
from compiler.dryrun import redact, redactable
from compiler.operations import RUNTIME_KEY_SESSION_VARS
from compiler.tasks import CompiledAgentTask, compile_task_session
from services.agents import missing_required_vars
from services.billing import (
    LLMUsage,
    UnpriceableUsageError,
    collector_in,
    llm_usage_lines,
    price_llm_usage,
)
from services.catalog import get_catalog
from services.faqs import FaqForPrompt
from services.integrations.models import Integration
from services.tools import HookTrees, MissingSecretError, ToolDefinition
from services.user import Tenant
from workers.session import persistence

from .models import (
    SUBMIT_RESULT_TOOL,
    TaskConfig,
    TaskErrorType,
    TaskRunError,
    TaskRunResponse,
    TraceStepResponse,
)

logger = logging.getLogger("talqing.tasks.run")

# A twenty-five-step research run with MCP payloads is large, and the trace is
# stored, returned by the API and rendered in a browser. Both caps are reported
# on the run, so a trace that was cut never looks like a short one.
TRACE_MAX_STEPS = 200
TRACE_FIELD_CHARS = 4000


class _RunFailure(Exception):
    """A failure already classified into the closed vocabulary."""

    def __init__(self, type: TaskErrorType, message: str) -> None:
        super().__init__(message)
        self.type: TaskErrorType = type
        self.message = message


# ────────────────────────────── the input contract ───────────────────────────


def undeclared_vars(cfg: TaskConfig, values: dict[str, str]) -> list[str]:
    """Values supplied for variables the task does not declare.

    Refused rather than dropped: a task's variables ARE its input contract, so a
    caller sending `emial` has a bug, and silently ignoring it means the run
    produces plausible nonsense instead of an error.
    """
    declared = {v.name for v in cfg.vars}
    return sorted(name for name in values if name not in declared)


# ──────────────────────────────── the trace ──────────────────────────────────


def _truncate(value: Any, *, cut: list[bool]) -> Any:
    """Cap one trace field, recording on `cut` whether anything was lost."""
    if isinstance(value, str):
        if len(value) <= TRACE_FIELD_CHARS:
            return value
        cut[0] = True
        return value[:TRACE_FIELD_CHARS] + "…"
    if isinstance(value, dict):
        return {k: _truncate(v, cut=cut) for k, v in value.items()}
    if isinstance(value, list):
        return [_truncate(v, cut=cut) for v in value]
    return value


def _json_or_text(raw: object) -> Any:
    """A tool's arguments and output arrive as JSON text. Parse when they are
    JSON so the panel can render fields; keep the string when they are not."""
    if not isinstance(raw, str):
        return raw
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def build_trace(result: RunResult | None, secrets: dict[str, str]) -> list[TraceStepResponse]:
    """Everything the model did, in order, redacted and truncated.

    Redaction happens here, before the trace is written — not before it is
    rendered. The row is returned by the API and readable by every VIEWER in the
    workspace, so a credential that reached it once has already leaked.
    """
    if result is None:
        return []
    values = redactable(secrets)
    steps: list[TraceStepResponse] = []
    # Tool calls and their outputs arrive as separate events; the output is
    # folded onto the call it belongs to so one round-trip reads as one step.
    by_call_id: dict[str, TraceStepResponse] = {}
    started_at: dict[str, float] = {}
    for event in result.events:
        if len(steps) >= TRACE_MAX_STEPS:
            break
        item = getattr(event, "item", None)
        if event.type == "function_call":
            cut = [False]
            step = TraceStepResponse(
                step=len(steps) + 1,
                kind="tool",
                name=str(item.name),
                args=redact(_truncate(_json_or_text(item.arguments), cut=cut), values),
                truncated=cut[0],
            )
            by_call_id[str(item.call_id)] = step
            started_at[str(item.call_id)] = float(item.created_at)
            steps.append(step)
        elif event.type == "function_call_output":
            step = by_call_id.get(str(item.call_id))
            if step is None:
                continue
            cut = [step.truncated]
            step.result = redact(_truncate(_json_or_text(item.output), cut=cut), values)
            step.truncated = cut[0]
            step.ms = round((float(item.created_at) - started_at[str(item.call_id)]) * 1000)
        elif event.type == "message" and item.role == "assistant":
            text = item.text_content
            if not text:
                continue
            cut = [False]
            steps.append(
                TraceStepResponse(
                    step=len(steps) + 1,
                    kind="message",
                    name="assistant",
                    result=redact(_truncate(text, cut=cut), values),
                    truncated=cut[0],
                )
            )
    return steps


# ──────────────────────────────── the run ────────────────────────────────────


async def _prepare(
    tenant: Tenant, cfg: TaskConfig
) -> tuple[
    dict[str, str],
    list[ToolDefinition],
    list[Integration],
    list[FaqForPrompt],
    dict[str, str],
    HookTrees,
]:
    """Everything the compile needs, and the two checks worth making first.

    Both are the runtime's last line rather than a duplicate of publish
    validation: a key can be deleted after the version was frozen, and
    `configuration` is the closed-vocabulary answer the editor and the email
    batch both already render. BYOK is checked here rather than left to
    `build_llm`'s ValueError so the message names the provider the way the rest
    of the product does.
    """
    provider_keys = await persistence.load_provider_keys(tenant)
    catalog = get_catalog()
    for provider in sorted(cfg.required_providers() - set(provider_keys)):
        label = catalog.provider_label(provider) if provider in catalog.providers else provider
        raise _RunFailure(
            "configuration",
            # Same opening clause as the publish check, and for the same reason:
            # the dashboard reads the provider back out of it (see
            # `services/agents/validate.py` and `frontend/lib/byok.ts`).
            f"no {label} API key for this workspace - add one under BYOK before running this task",
        )
    if not cfg.output:
        # A run reaching here with no output would compile a `submit_result`
        # taking no arguments and "complete" with `{}`.
        raise _RunFailure(
            "configuration",
            "this task declares no output fields, so there is nothing for it to produce",
        )
    # Pinned, on every path: a published version froze its tool versions, and a
    # draft run pins at request time. `resolve_pinned_tools` raises on an
    # unpinned selection rather than reaching for whatever is published now.
    frozen = await persistence.resolve_pinned_tools(tenant, cfg)
    integrations = await persistence.load_mcp_integrations(tenant, cfg.mcps)
    faqs = await persistence.load_faqs(tenant, cfg.faqs)
    secrets = await persistence.load_tool_secrets(tenant)
    # Hooks reach a task since it became an `AgentBase`: a task IS a
    # conversation when an agent enters one.
    hook_trees = await persistence.load_hook_trees(tenant, cfg, frozen)
    tool_defs = persistence.select_tool_definitions(frozen, cfg.tools)
    return provider_keys, tool_defs, integrations, faqs, secrets, hook_trees


def _classify(exc: BaseException, *, steps_used: int, max_steps: int) -> _RunFailure:
    """Whose problem this failure is.

    `step_limit` and `no_output` are the same event to LiveKit and have to be
    told apart here. At the cap, ``AgentActivity`` does not raise: it logs a
    warning and generates a final response with ``tool_choice='none'``, so the
    model is forbidden from calling `submit_result` and the run necessarily ends
    without output. Reported naively, every step-exhausted task would come back
    as `no_output` and its author would go looking at their prompt when the
    answer is a number in their Limits section.
    """
    if isinstance(exc, TimeoutError):
        return _RunFailure("timeout", "the run did not finish in time")
    if isinstance(exc, APIError):
        return _RunFailure("provider_error", f"the model or a tool server failed: {exc}")
    # UnexpectedModelBehavior — the run ended without the expected output, and
    # LiveKit's own retry has already fired.
    name = type(exc).__name__
    if name == "UnexpectedModelBehavior":
        if steps_used >= max_steps:
            return _RunFailure(
                "step_limit",
                f"the run used all {max_steps} of its steps without calling "
                f"{SUBMIT_RESULT_TOOL} - raise the step limit, or narrow what the task does",
            )
        return _RunFailure(
            "no_output",
            f"the model finished without calling {SUBMIT_RESULT_TOOL}, twice - say in the "
            "prompt that calling it is how the task finishes",
        )
    logger.exception("task run failed", exc_info=exc)
    return _RunFailure("platform", f"the run failed unexpectedly: {exc}")


async def open_task_run(
    conn: asyncpg.Connection | asyncpg.Pool,
    *,
    run_id: UUID,
    tenant_id: UUID,
    task_id: UUID | None,
    task_name: str,
    task_version: int | None,
    vars: dict[str, str],
    created_by: UUID | None,
) -> None:
    """Create the `task_runs` row this run will settle.

    Takes a connection so a caller can open the run **inside its own
    transaction** — which is what an email batch's drafting claim does, because
    `email_batch_recipients` has a CHECK saying a row being drafted has a run
    id, and a constraint is worth more than a promise. `run_task` opens the row
    itself for every other caller.
    """
    await conn.execute(
        """
        INSERT INTO task_runs (
            id, task_id, task_name, task_version, vars, created_by, tenant_id
        )
        VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7)
        """,
        run_id,
        task_id,
        task_name,
        task_version,
        json.dumps(vars),
        created_by,
        tenant_id,
    )


async def run_task(
    *,
    tenant: Tenant,
    cfg: TaskConfig,
    vars: dict[str, str],
    run_id: UUID,
    task_id: UUID | None = None,
    task_version: int | None = None,
    created_by: UUID | None = None,
    run_opened: bool = False,
) -> TaskRunResponse:
    """Run one task and record it. Returns the finished `task_runs` row.

    Executes for real: the tools call the tenant's endpoints, the MCP servers
    spend the tenant's credits, and the model spends the tenant's tokens.

    ``cfg`` must be a config whose tools are pinned — a published version, or a
    draft the caller pinned itself. ``task_version`` names which one it is, and
    is null for a draft run.

    ``run_opened`` says the caller already created the `task_runs` row through
    `open_task_run` — the drafting claim does, so that claiming a row and
    recording its run commit together.
    """
    pool = await db.tenant_pool(tenant)
    if not run_opened:
        await open_task_run(
            pool,
            run_id=run_id,
            tenant_id=tenant.id,
            task_id=task_id,
            task_name=cfg.name,
            task_version=task_version,
            vars=vars,
            created_by=created_by,
        )
    started = time.monotonic()
    started_at = datetime.now(UTC)

    session: AgentSession | None = None
    result: RunResult | None = None
    agent: CompiledAgentTask | None = None
    secrets: dict[str, str] = {}
    failure: _RunFailure | None = None
    canceled = False
    usage: list[LLMUsage] = []
    # Tool rounds, one entry per ATTEMPT. Per attempt and not in total, because
    # `session.run`'s output retry starts a fresh speech with a fresh step
    # budget — so the number the cap governs is the busiest attempt's, and a sum
    # would report `step_limit` for two well-behaved attempts in a row.
    #
    # `speech_created` is what marks an attempt: `session.run` fires one, and so
    # does each output retry's `generate_reply`. A tool-response generation
    # reuses the same speech and fires nothing, which is exactly the boundary
    # wanted. (`session.current_speech` is NOT usable here — it reads None
    # inside a `function_tools_executed` handler about half the time, which
    # silently merges attempts and inflates the count.)
    rounds: list[int] = []

    try:
        # Before anything is compiled and before any provider is called, so a
        # missing input costs nothing.
        if missing := missing_required_vars(cfg.vars, vars):
            raise _RunFailure(
                "missing_vars",
                f"this run supplied no value for required variable(s): {', '.join(missing)}",
            )

        # Everything up to the first provider call is the tenant's configuration:
        # a missing BYOK key, a tool deleted or unpublished since the task was
        # saved, an integration whose credential no longer resolves. Each of
        # those raises a plain ValueError deep in the loaders, and left to the
        # general handler below they would come back as `platform` — "ours" —
        # which sends the one person who can fix it to us instead.
        try:
            provider_keys, tool_defs, integrations, faqs, secrets, hooks = await _prepare(
                tenant, cfg
            )
        except _RunFailure:
            raise
        except (ValueError, MissingSecretError) as exc:
            raise _RunFailure("configuration", str(exc)) from exc

        session, agent = compile_task_session(
            cfg,
            provider_keys,
            tool_defs=tool_defs,
            integrations=integrations,
            faqs=faqs,
            tool_secrets=secrets,
            hook_trees=hooks,
            tenant=tenant,
            task_id=str(task_id) if task_id else None,
            # The provider prompt-cache key. The TASK, not this run: every run of
            # one task shares a system prompt, so routing them to the same server
            # is what lets that prefix cache-hit across a batch of thousands.
            runtime_id=str(task_id or run_id),
            runtime_context={
                # Text: long-running tools are awaited rather than detached, and
                # the room-bound operations refuse instead of reaching for a job
                # context that does not exist.
                "channel": "text",
                "session_type": "TASK",
                "runtime_id": str(run_id),
                # What the caller supplied IS the session's var values.
                # `compile_task_session` binds this config's declared defaults
                # beside them, and `build_vars` merges the two.
                RUNTIME_KEY_SESSION_VARS: vars,
            },
        )

        @session.on("speech_created")
        def _new_attempt(_ev: object) -> None:
            rounds.append(0)

        @session.on("function_tools_executed")
        def _count_round(_ev: object) -> None:
            if not rounds:
                rounds.append(0)
            rounds[-1] += 1

        await session.start(agent, record=False)
        # Hook parity with an in-call entry, where LiveKit fires `on_enter` on
        # the activity switch. `initial=True` runs the hook and nothing else —
        # the generated opening turn is for a conversation, and a headless run
        # opens with the values it was given as its first message.
        await agent.run_entry(initial=True)
        result = session.run(
            # The values the run was given, as the message that starts it. The
            # prompt reads them as {{vars.*}} too; this is what a model that was
            # given a variable its prompt never interpolates still sees.
            user_input=json.dumps(vars, indent=2, sort_keys=True),
            output_type=dict,
        )
        # `asyncio.wait_for` is correct here and wrong in the text worker. A
        # RunResult awaits a shielded future, so a timeout abandons the run
        # rather than ending it — which would leave a REUSED session unable to
        # start the next one. This session is used once and closed in the
        # `finally` below, so the abandoned run dies with it.
        await asyncio.wait_for(result, cfg.timeout_seconds)
    except _RunFailure as exc:
        failure = exc
    except asyncio.CancelledError:
        # A shutdown, not a failure of this run. Settled rather than abandoned:
        # the row is what a batch's review table and the task's own history read,
        # and a run that never settles is `running` for ever with nothing to reap
        # it. Settling happens below rather than here because the usage is
        # collected in the `finally`, which runs after this body — pricing from
        # inside it would report every interrupted run as costing zero.
        failure = _RunFailure("canceled", "the worker running this task stopped before it finished")
        canceled = True
    except Exception as exc:  # noqa: BLE001 - every failure is classified, never swallowed
        failure = _classify(exc, steps_used=max(rounds, default=0), max_steps=cfg.max_steps)
    finally:
        if session is not None:
            usage = llm_usage_lines(
                session.usage.model_usage, cfg.llm, collector_in(session.userdata)
            )
            try:
                await session.aclose()
            except Exception:
                logger.exception("closing the session for task run %s failed", run_id)

    payload = agent.payload if agent is not None else None
    if failure is None and payload is None:
        failure = _RunFailure("no_output", f"the run finished without calling {SUBMIT_RESULT_TOOL}")

    # Built once and awaited on exactly one of the two branches below, so the
    # thirteen arguments are written once. A cancelled run reads like a truncated
    # one and not like a blank one: the trace, the usage and `steps_used` it did
    # collect are all recorded.
    settle = _settle(
        tenant,
        run_id=run_id,
        task_id=task_id,
        task_version=task_version,
        cfg=cfg,
        vars=vars,
        payload=None if failure else payload,
        failure=failure,
        trace=build_trace(result, secrets),
        usage=usage,
        steps_used=max(rounds, default=0),
        attempts=len(rounds),
        started_at=started_at,
        duration_ms=round((time.monotonic() - started) * 1000),
    )
    if not canceled:
        return await settle
    # Best-effort, because the `raise` below must happen either way: an exception
    # escaping here would REPLACE the `CancelledError`, the drafting pass's
    # general `except Exception` would read it as a task failure, and with
    # `draft_attempts` at its default of 1 the row would land on `draft_failed`.
    try:
        await settle
    except Exception:
        logger.exception("could not settle cancelled task run %s", run_id)
    # Propagated after the row is written, so `_run_one` and `_dispatch` still see
    # a cancellation and release what they hold.
    raise asyncio.CancelledError


async def _settle(
    tenant: Tenant,
    *,
    run_id: UUID,
    task_id: UUID | None,
    task_version: int | None,
    cfg: TaskConfig,
    vars: dict[str, str],
    payload: dict[str, Any] | None,
    failure: _RunFailure | None,
    trace: list[TraceStepResponse],
    usage: list[LLMUsage],
    steps_used: int,
    attempts: int,
    started_at: datetime,
    duration_ms: int,
) -> TaskRunResponse:
    """Write the finished row and return it.

    Priced through the same `price_llm_usage` a session goes through — there
    must not be two implementations of what a token costs — and quarantined
    rather than priced at zero when the catalog cannot price it.
    """
    provider_cost: Decimal | None = None
    snapshot: dict[str, Any] | None = None
    if not usage:
        # Nothing reached a provider: a missing required variable, a missing BYOK
        # key. There is no spend to price, which is not the same as spend of zero.
        billing_status = "skipped"
    else:
        try:
            provider_cost, lines = price_llm_usage(usage)
            billing_status = "computed"
            snapshot = {"llm": [line.model_dump(mode="json") for line in lines]}
        except UnpriceableUsageError as exc:
            logger.error("task run %s is unpriceable: %s", run_id, exc)
            billing_status = "unpriceable"
            snapshot = {"error": str(exc)}

    status = "completed" if failure is None else "failed"
    error = TaskRunError(type=failure.type, message=failure.message) if failure else None
    ended_at = datetime.now(UTC)

    pool = await db.tenant_pool(tenant)
    await pool.execute(
        """
        UPDATE task_runs
        SET status = $3,
            error = $4::jsonb,
            output = $5::jsonb,
            trace = $6::jsonb,
            usage = $7::jsonb,
            steps_used = $8,
            max_steps = $9,
            attempts = $10,
            provider_cost = $11,
            billing_status = $12,
            pricing_snapshot = $13::jsonb,
            ended_at = $14,
            duration_ms = $15
        WHERE id = $1 AND tenant_id = $2
        """,
        run_id,
        tenant.id,
        status,
        error.model_dump_json() if error else None,
        json.dumps(payload) if payload is not None else None,
        json.dumps([step.model_dump(mode="json") for step in trace]),
        json.dumps([u.model_dump(mode="json") for u in usage]),
        steps_used,
        cfg.max_steps,
        attempts,
        provider_cost,
        billing_status,
        json.dumps(snapshot) if snapshot is not None else None,
        ended_at,
        duration_ms,
    )
    return TaskRunResponse(
        id=run_id,
        task_id=task_id,
        task_name=cfg.name,
        task_version=task_version,
        status=status,
        error=error,
        vars=vars,
        output=payload,
        trace=trace,
        steps_used=steps_used,
        max_steps=cfg.max_steps,
        attempts=attempts,
        usage=usage,
        provider_cost=provider_cost,
        billing_status=billing_status,
        started_at=started_at,
        ended_at=ended_at,
        duration_ms=duration_ms,
    )
