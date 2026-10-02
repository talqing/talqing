"""Validation for a task config.

Almost all of it is the shared validator: a missing BYOK key, an unpublished
tool, a non-active MCP integration, an unknown ``{{token}}`` and a bad
`reasoning_effort` all fail for a task with the same message they fail with for
an agent, because ``validate_base_draft`` takes an ``AgentBase`` and a task is
one. What is added here is the handful of rules an agent has no equivalent for,
because an agent has no typed output — and the one an agent's tools are allowed
to break, because an agent can hand the conversation on and a task cannot.

Two grades, exactly as ``validate_agent_draft`` has them. ``for_publish=False``
is what a draft write runs: the rules a half-written task can still be judged
on. ``for_publish=True`` is the publish gate, and it is where the checks that
need a finished config land — a workspace key for the model, and at least one
output field for the run to produce.
"""

from __future__ import annotations

from collections import Counter

from services.agents import (
    FINISH_WITHOUT_RESULT_TOOL,
    SUBMIT_RESULT_TOOL,
    TaskConfig,
    task_handoff_errors,
    validate_base_draft,
)
from services.tools import ValidationResult
from services.user import Context


def validate_task_config(cfg: TaskConfig, *, for_publish: bool = False) -> ValidationResult:
    """The rules an agent has no equivalent for. Needs no database."""
    out = ValidationResult()

    duplicates = sorted(name for name, n in Counter(f.name for f in cfg.output).items() if n > 1)
    if duplicates:
        out.errors.append(
            f"these output fields are declared more than once: {', '.join(duplicates)} - "
            "each name gets one type and one description, and the later entry would "
            "silently win"
        )

    # Inputs and outputs share one flat space wherever a run is consumed — the
    # values it was given beside the values it produced — so a collision makes
    # one of the two unreachable.
    collisions = sorted(
        name for name, n in cfg.column_space().items() if n > 1 and _is_both(cfg, name)
    )
    if collisions:
        out.errors.append(
            f"these names are both a variable and an output field: {', '.join(collisions)} - "
            "a run's inputs and its output are read side by side, so one of the two would be "
            "unreachable. Rename one of each pair"
        )

    # Publish grade: a draft is allowed to be unfinished, and "what does this
    # produce" is the last question most authors answer. A published task with
    # no output would compile a `submit_result` taking no arguments and complete
    # every run with `{}`.
    if for_publish and not cfg.output:
        out.errors.append(
            "this task declares no output fields, so a run has nothing to produce - "
            "add at least one under Inputs & output"
        )

    # Checked again here even though the field is required and non-empty on the
    # way in. Nothing is appended to a task's prompt, so these two sentences are
    # the entirety of what the model is told about finishing — that makes this
    # the same grade of rule as "a published task must produce something", and
    # it belongs where a publish is refused rather than only where a write is.
    if for_publish:
        for field, tool in (
            ("submit_result_description", SUBMIT_RESULT_TOOL),
            ("finish_without_result_description", FINISH_WITHOUT_RESULT_TOOL),
        ):
            if not getattr(cfg, field).strip():
                out.errors.append(
                    f"this task does not say what `{tool}` is for, so the model would be "
                    "given a tool with no description and left to guess when to call it - "
                    "write one under Inputs & output -> Finishing"
                )

    if not cfg.prompt.strip():
        out.warnings.append(
            "this task has no prompt, so the only thing the model is told is what its tools "
            f"do - nothing is added to the prompt, and even `{SUBMIT_RESULT_TOOL}` is only "
            "described by its own sentence. Say what the task is and how to find each value"
        )

    return out


def _is_both(cfg: TaskConfig, name: str) -> bool:
    """True when `name` appears on both sides rather than twice on one.

    A name repeated within `vars` or within `output` already has its own,
    better error; reporting it as a collision too would be the same typo told
    twice in two different ways.
    """
    return any(v.name == name for v in cfg.vars) and any(f.name == name for f in cfg.output)


async def validate_task_draft(
    ctx: Context, cfg: TaskConfig, *, for_publish: bool = False
) -> ValidationResult:
    """Everything: the shared rules, then the task-only ones.

    Named for the config it takes, not the id it does not — ``validate_task`` in
    ``services.tasks.service`` is the stored-id twin, exactly as
    ``validate_agent_draft`` and ``validate_agent`` divide the work for agents.

    No ``channel`` is passed, and that is the point: a task declares no media, so
    there is no channel to hold its models to. It runs on whatever the session
    that enters it is running, or on nothing at all for a standalone run.
    """
    out = await validate_base_draft(ctx, cfg, for_publish=for_publish)
    out.extend(validate_task_config(cfg, for_publish=for_publish))
    # Checked on both sides of the attachment: here, so a task cannot be
    # published into a shape that cannot run, and on the agent, so an attachment
    # to a task published before this rule existed is still refused.
    out.errors.extend(await task_handoff_errors(ctx, cfg))
    return out
