"""The shapes an Agent Task's API speaks.

``TaskConfig`` itself lives in ``services.agents.models``, beside the
``AgentBase`` it extends and the ``AgentConfig`` that embeds it: a task IS
LiveKit's ``AgentTask`` — an agent with a typed return value — and
``AgentConfig.tasks`` carries one inline the way ``ToolSelection`` carries an
inline tool. It is re-exported here so every existing import still reads
``from services.tasks import TaskConfig``.

What is left in this module is what only a *run* has: how a run failed, what it
traced, and what it cost.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator

from services.agents import (  # noqa: F401 - re-exported; see the module docstring
    FINISH_WITHOUT_RESULT_TOOL,
    SUBMIT_RESULT_TOOL,
    TaskConfig,
    TaskOutputField,
)
from services.agents.override import deep_partial
from services.billing import LLMUsage
from services.system_vars import validate_session_vars

# The eight ways a run can fail — a closed vocabulary, because "whose problem is
# this" is the question the editor answers. See ``services.tasks.run``.
TaskErrorType = Literal[
    "missing_vars",
    "no_output",
    "step_limit",
    "timeout",
    "provider_error",
    "configuration",
    "platform",
    "canceled",
]


# ─────────────────────────── service request / response ─────────────────────
# Shared by HTTP routes and CoPilot.


class CreateTaskRequest(BaseModel):
    config: TaskConfig
    model_config = ConfigDict(extra="forbid")


# The same merge `PATCH /agents/{id}` takes (`services.agents.override`).
TaskPatch = deep_partial(TaskConfig, "TaskPatch")


class PatchTaskRequest(BaseModel):
    config: TaskPatch = Field(  # type: ignore[valid-type]
        description="Only the fields to change: the same shape as TaskConfig, every field optional."
    )
    model_config = ConfigDict(extra="forbid")


class TaskRunError(BaseModel):
    type: TaskErrorType
    message: str


class TaskRunSummary(BaseModel):
    """A run, as the list page shows it: what happened, when, and what it cost.

    `error` is the same object a full run carries rather than a bare type, so
    one reader answers "how did it go" for both — a summary that spelled the
    same fact differently would be two shapes for one question.
    """

    id: UUID
    status: Literal["running", "completed", "failed"]
    error: TaskRunError | None = None
    started_at: datetime
    duration_ms: int | None = None
    provider_cost: Decimal | None = None

    @field_serializer("provider_cost")
    def _ser_cost(self, v: Decimal | None) -> float | None:
        return None if v is None else float(v)


class TaskVersionResponse(BaseModel):
    version: int
    published_at: datetime
    # The email of whoever published it. Null once that account is gone — the
    # version outlives the person, and saying so is better than inventing a name.
    published_by: str | None = None


class TaskVersionDetailResponse(TaskVersionResponse):
    """One frozen version, unpacked: the definition that ran, beside its metadata.

    `config` comes back through `TaskConfig` rather than as raw JSONB, so a
    version and the draft are normalized by the same validator and a diff of the
    two cannot report a difference a default invented.

    It is the whole definition: the tool versions this publish pinned live in
    `config.tools`, and their definitions in `tool_versions`.
    """

    config: TaskConfig


class TaskResponse(BaseModel):
    # Response-only: every field below is always sent, so a default must not
    # publish it as optional. See HealthSnapshotResponse for the full reason.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)
    id: UUID
    config: TaskConfig
    published_version: int | None = None
    # When `published_version` was frozen. Null while the task is a draft. Read
    # against `updated_at` it answers the question a draft/publish model always
    # raises: has the draft moved on from what runs?
    published_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    # Who created this, as a control-plane user id. Attribution only — resolve it
    # to a name against GET /v1/org/members, which is where people live.
    created_by: UUID | None = None
    # Only `get_task` fills this; the list endpoint leaves it null rather than
    # running a history query per task.
    versions: list[TaskVersionResponse] | None = None
    # The newest run, so a list can say "last run: failed, step_limit" without a
    # second request per row. Null on a task that has never run.
    last_run: TaskRunSummary | None = None


class PublishTaskResponse(BaseModel):
    task_id: UUID
    version: int
    published_at: datetime
    # The half of validation that must not block a publish — a prompt that never
    # mentions `submit_result`, a credential built out of `{{args.…}}`.
    warnings: list[str] = []


class RunTaskRequest(BaseModel):
    vars: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Values for the variables this task declares, by name. A `required` variable "
            "with no value and no default is refused before the run starts; a name the task "
            "does not declare is refused too, rather than silently dropped."
        ),
    )
    version: int | Literal["draft"] | None = Field(
        default=None,
        description=(
            "Which definition to run. Omitted runs the published version. A number runs that "
            "frozen version. 'draft' runs the unpublished config, validated and tool-pinned at "
            "request time and refused with the publish errors if it does not hold together."
        ),
    )

    model_config = ConfigDict(extra="forbid")

    @field_validator("vars")
    @classmethod
    def _check_vars(cls, value: dict[str, str]) -> dict[str, str]:
        return validate_session_vars(value)


class TraceStepResponse(BaseModel):
    """One thing the model did, in order.

    `kind` is `message` for something it wrote and `tool` for something it
    called. Arguments and results are truncated and every workspace secret in
    them is masked; `truncated` says when a field was cut, so a short trace is
    never mistaken for a complete one.
    """

    step: int
    kind: Literal["message", "tool"]
    # The tool's name, or the role of the message ('assistant').
    name: str
    args: Any = None
    result: Any = None
    # How long the tool took, from the call to its output. Null on a message,
    # which is written progressively and has no such pair of moments.
    ms: int | None = None
    truncated: bool = False


class TaskRunResponse(BaseModel):
    # Response-only: every field below is always sent, so a default must not
    # publish it as optional. See HealthSnapshotResponse for the full reason.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)
    id: UUID
    task_id: UUID | None = None
    task_name: str | None = None
    # Which published version ran. Null when the run was of an unpublished draft.
    task_version: int | None = None
    status: Literal["running", "completed", "failed"]
    error: TaskRunError | None = None
    vars: dict[str, str] = Field(default_factory=dict)
    # The validated `submit_result` payload. Null unless status is 'completed'.
    output: dict[str, Any] | None = None
    trace: list[TraceStepResponse] = Field(default_factory=list)
    # Rounds used by the BUSIEST attempt, and the cap it ran under. Reported on
    # every run, success included: "used 25 of 25 steps" is the sentence that
    # ends an investigation into a `step_limit`.
    steps_used: int = 0
    max_steps: int = 0
    # How many times the model was prompted to produce the result. More than one
    # means an attempt ended without calling `submit_result` and LiveKit
    # re-prompted it — **and each re-prompt gets a fresh step budget**, so a run
    # with three attempts may have used three times `max_steps` rounds in total.
    # Without this, a run that hit the cap twice and recovered would report
    # "2 of 2" and look like a comfortable finish.
    attempts: int = 0
    usage: list[LLMUsage] = Field(default_factory=list)
    provider_cost: Decimal | None = None
    # pending | computed | unpriceable | skipped. `unpriceable` means the run
    # happened but its model is missing from the catalog, so its cost is
    # quarantined rather than reported as zero.
    billing_status: str = "pending"
    started_at: datetime
    ended_at: datetime | None = None
    duration_ms: int | None = None

    @field_serializer("provider_cost")
    def _ser_cost(self, v: Decimal | None) -> float | None:
        return None if v is None else float(v)
