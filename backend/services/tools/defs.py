"""Shared runtime definition shapes for tools and session state.

Published tool versions and operation trees are stored as JSONB and threaded
through the compiler/worker as dicts. These TypedDicts are the single source of
truth for those keys.

The second half of the file is the *authored* side of the same thing: the eleven
typed operation variants a caller writes, and the discriminated union over them
that is the platform's contract for what an operation is.

Integrations use ``services.integrations.models.Integration`` (Pydantic domain
model), not a TypedDict here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any, Literal, NotRequired, Required, TypedDict, get_args

from pydantic import (
    AfterValidator,
    BaseModel,
    Field,
    StringConstraints,
    TypeAdapter,
    model_validator,
)

from services.conversation_context import ConversationContext
from services.userdata import RESERVED_KEY_ERROR, is_reserved_key

# ───────────────────────────── operations / tools ────────────────────────────

OnError = Literal["abort", "continue"]
# The same three words as `AgentConfig.conversation.context`, on purpose: one
# question ("what does this start with?") with one set of answers, whether the
# thing starting is a new call or the next agent. See services/conversation_context.py.
HandoffContextPolicy = ConversationContext

# Where an operation writes a value it publishes. The two names are also the two
# template roots that read them back, so the config and the read site agree:
# `{"store": "tooldata"}` is read as `{{tooldata.key}}`.
PublishStore = Literal["tooldata", "userdata"]
PUBLISH_STORES: tuple[PublishStore, ...] = get_args(PublishStore)


class PublishField(TypedDict):
    """One publish_fields entry on an http/code/frontend_rpc operation."""

    path: str
    store: PublishStore
    key: NotRequired[str]


# Functional form so the branch key can be the Python keyword `else`.
Operation = TypedDict(
    "Operation",
    {
        "kind": Required[str],
        "config": NotRequired[dict[str, Any]],
        "silent": NotRequired[bool],
        "publish_fields": NotRequired[list[PublishField]],
        "background_execution": NotRequired[bool],
        "on_error": NotRequired[OnError],
        "then": NotRequired[list["Operation"]],
        "else": NotRequired[list["Operation"]],
    },
)

OperationTree = list[Operation]
HookTrees = dict[str, OperationTree]


class ToolDefinition(TypedDict):
    """Frozen `tool_versions.definition` (and the same shape in agent_versions).

    ``id`` is None for a tool defined inline in the request that started the
    call: it has no `tools` row, and inventing one would put a phantom id in the
    transcript, the call detail and every tool webhook.
    """

    id: str | None
    name: str
    description: str
    json_schema: dict[str, Any]
    long_running_task: bool
    silent: bool
    disable_interruptions: bool
    operations: OperationTree


# ───────────────────────────── session / events ─────────────────────────────

# Session userdata is intentionally open: tools and ops write arbitrary keys.
UserData = dict[str, Any]
SessionUserData = UserData
# The same shape, but scoped to one tool run: created empty when a tool's
# operation tree starts and dropped when it returns.
ToolData = dict[str, Any]
# The two roots nothing inside a session can write. `CallFields` is the
# phone-derived half of `{{system_vars.*}}` that rides the runtime context — a
# call's two numbers and its direction; `SystemVars` is the whole bag a resolve
# sees, that half plus the clock rendered from the agent's timezone, on a closed
# key space. `Vars` is the tenant's own bag, this agent's declared defaults with
# the starting request's values merged over them, on an open one. The catalog and
# all three builders live in `services.system_vars`; only the shapes are here,
# beside the two stores, because every reader of one reads the others.
CallFields = dict[str, str]
SystemVars = dict[str, str]
Vars = dict[str, str]

# Worker-side bookkeeping that is not yet modeled as a Pydantic shape.
AvatarState = dict[str, Any]
TranscriptRow = dict[str, Any]

# Non-templatable call context threaded into tool/hook execution
# (tenant, agent_id, participant_identity, session_id, channel, session_type, …).
RuntimeContext = dict[str, Any]


@dataclass(frozen=True, slots=True)
class HandoffTarget:
    """Who an activity switch handed the conversation to.

    Recorded on the runtime context by ``compiler.handoff.build_handoff_agent``
    and by ``compiler.tasks.build_task_agent`` as each target is resolved, and
    read by the worker when it writes the `agent_handoff` transcript item. It has
    to be recorded, because the item itself carries only LiveKit agent ids and
    the agent stamp the worker captures at emit time names the SOURCE.

    ``agent_id`` and ``version`` are both None for a member of this call's
    team: it has no `agents` row and therefore no published version, so the item
    keeps the caller's stamp and this ``name`` is the only thing that says who
    took over. The same is true of a task, which has no `agents` row at all.

    ``kind`` is what the row is called. LiveKit writes an `AgentHandoff` on BOTH
    switches a task causes — in on entry, back on return — so without it a call
    that entered one task reads as two anonymous handoffs, as if the agent had
    changed twice.
    """

    name: str
    agent_id: str | None
    version: int | None
    kind: Literal["handoff", "task", "task_return"] = "handoff"
    # On both rows of a task: the tool call that entered it. It is what pairs
    # the "Started" row with its "Back to" row when a call enters several tasks,
    # or the same one twice.
    entry_call_id: str | None = None


# ─────────────────────── authored operation-tree shapes ─────────────────────
# The request-side mirror of the `Operation` above: what a caller writes when
# they author a tool. It lives here rather than in `service.py` because
# `services.agents.models` embeds it — an inline tool carries its own tree — and
# that module cannot import the API layer `service.py` pulls in. `service.py`
# re-exports these, so every existing import site is unchanged.
#
# Layer 0: nothing here may import upward. The one thing that has to
# (`normalize_e164`, on a transfer destination) does it lazily, inside the
# validator.

# a valid function name: letters/digits/underscore, not starting with a digit
ToolName = Annotated[str, StringConstraints(pattern=r"^[a-zA-Z_][a-zA-Z0-9_]*$")]
# The prefix an MCP server's tools are presented to the model under, as
# `<namespace>_<tool>`. Empty is a real value and means "no prefix" — a
# deliberate escape hatch, not an unset field. Lowercase because the exposed
# name is read beside tenant tool names, which are snake_case by `ToolName`;
# leading letter because Gemini rejects a function name starting with a digit;
# capped well short of OpenAI's documented 64 so a long tool name still fits
# under it.
#
# Here rather than in `services.integrations.models`, where the column lives,
# because `services.agents.models` needs it too (an inline MCP server carries
# one) and cannot import that module — `providers.telegram` imports back.
ToolsNamespace = Annotated[str, StringConstraints(pattern=r"^$|^[a-z][a-z0-9_]{0,23}$")]


def exposed_tool_name(namespace: str, tool: str) -> str:
    """What the model is shown for one integration tool.

    Empty namespace = the server's own name, untouched. A tool that already
    carries the prefix keeps it rather than doubling it — Tavily's own tools are
    `tavily_search` / `tavily_extract` and Exa's are `web_search_exa`, so a blind
    prefix ships `tavily_tavily_search`. Deterministic in (namespace, tool)
    alone, so adding a second server never moves an existing name.

    Next to ``ToolsNamespace`` rather than in the compiler because both compile
    paths apply it — a hosted MCP server's tools and our own native ones — and
    because approval matches on the unprefixed name either way.
    """
    if not namespace or tool.startswith(f"{namespace}_"):
        return tool
    return f"{namespace}_{tool}"


PublishKey = Annotated[str, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")]
PublishPath = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


def _not_blank(value: str) -> str:
    if not value.strip():
        raise ValueError("is required")
    return value.strip()


# A value the operation cannot run without. Whitespace alone is the same as
# leaving it out — a `say` whose text is three spaces speaks nothing — so both
# fail the same way, at the request that tried to save them.
RequiredText = Annotated[str, AfterValidator(_not_blank)]


class PublishFieldRequest(BaseModel):
    path: PublishPath
    # Which store the value lands in — and the template root that reads it back.
    store: PublishStore
    key: PublishKey | None = None

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def _check_reserved_key(self) -> PublishFieldRequest:
        # Without `key`, the path's last segment is the key (`publish_field_path_and_key`).
        if self.store == "userdata" and is_reserved_key(self.key or self.path.split(".")[-1]):
            raise ValueError(RESERVED_KEY_ERROR)
        return self


# Every timeout in a tree is in SECONDS, and every one of them is capped: an
# operation runs mid-conversation with a caller on the line, so an unbounded
# value does not mean "patient", it means the call hangs. The ceilings are
# generous — `long_running_task` is the answer to a genuinely slow endpoint —
# and they also catch the commonest authoring mistake, writing milliseconds.
CODE_TIMEOUT_MAX = 30.0  # the code-exec service hard-caps here too
HTTP_TIMEOUT_MAX = 120.0
FRONTEND_RPC_TIMEOUT_MAX = 60.0
# Seconds a transfer destination may ring. Mirrors
# `compiler/operations.py::TRANSFER_RINGING_TIMEOUT_DEFAULT`, and deliberately
# NOT bounded by the ≤80s validator on `livekit.sip.ringing_timeout_seconds`,
# which is a deployment setting for a different dial. Below 5s no phone has
# finished its first ring; past 120s a caller has had time to hang up twice.
TRANSFER_RINGING_TIMEOUT_MIN = 5
TRANSFER_RINGING_TIMEOUT_MAX = 120
# How many recent turns may cross verbatim alongside a summary — and, under
# `none`, in place of one. Mirrors the bounds on `HandoffTarget.recent_turns`:
# 1 rather than 0 is what makes "unset" unambiguously mean "the policy's own
# answer", and past ten turns the honest answer is `transcript`.
HANDOFF_RECENT_TURNS_MIN = 1
HANDOFF_RECENT_TURNS_MAX = 10

# `strict=True` on every number: without it pydantic reads `true` as 1 and
# `"20"` as 20.0, and a timeout that came from a checkbox by accident is worse
# than one that was refused.
Seconds = Annotated[float, Field(strict=True, gt=0)]

# What the *caller* experiences, which is the only part of a transfer a builder
# gets to choose: `cold` hands them straight over, `warm` holds them while the
# agent briefs whoever answers. The transport underneath (SIP REFER or a bridge)
# is the platform's and is not expressible here.
TransferMode = Literal["cold", "warm"]
TransferFailurePolicy = Literal["continue", "end_call"]


# ───────────────────────────── operation configs ─────────────────────────────
# One model per kind. These are the contract: what the dashboard renders as a
# form, what the MCP schema shows an agent writing a tool, and what publish
# re-validates out of JSONB. `extra: forbid` throughout, so a stray `message` on
# a `say` is an error naming the field rather than a line the caller never hears.


class HttpConfig(BaseModel):
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
    url: RequiredText
    # Templated like everything else, which is why credentials belong in
    # `{{secrets.…}}` rather than in an argument the model wrote.
    headers: dict[str, str] = Field(default_factory=dict)
    query: dict[str, str] = Field(default_factory=dict)
    body: Any = None
    timeout: Annotated[Seconds, Field(le=HTTP_TIMEOUT_MAX)] | None = Field(
        default=None, description="Seconds, not milliseconds. Defaults to 20."
    )

    model_config = {"extra": "forbid"}


# ``compiled_js`` is build output, not authorship: publishing compiles
# `source_ts` and writes it here, and the frozen definition carries it — so a
# stored config has to round-trip through this model with it present. A separate
# `StoredCodeConfig` carrying that one extra field was considered and rejected —
# two models for one field is a worse trade than one nullable field the author
# is told not to send.
class CodeConfig(BaseModel):
    """TypeScript run on the code-exec service."""

    source_ts: RequiredText = Field(
        description="Must `export default async function handler(input)` and return an object."
    )
    timeout: Annotated[Seconds, Field(le=CODE_TIMEOUT_MAX)] | None = Field(
        default=None, description="Seconds, not milliseconds. Defaults to 10."
    )
    compiled_js: str | None = Field(
        default=None,
        description="Server-written build output. Do not send it; publishing compiles `source_ts`.",
    )

    model_config = {"extra": "forbid"}


class FrontendRpcConfig(BaseModel):
    # Mirrors JS identifier rules — this names a handler the web client registered.
    method: Annotated[str, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_.-]*$")]
    payload: Any = None
    timeout: Annotated[Seconds, Field(le=FRONTEND_RPC_TIMEOUT_MAX)] | None = Field(
        default=None, description="Seconds, not milliseconds. Defaults to 5."
    )

    model_config = {"extra": "forbid"}


class IfConfig(BaseModel):
    # `Any`, and required: an author comparing against a literal `null` is doing
    # something meaningful, and leaving `left` out is not.
    left: Any
    op: Literal["eq", "neq", "gt", "lt", "exists", "in"]
    right: Any = None

    model_config = {"extra": "forbid"}


class SetVariableConfig(BaseModel):
    key: RequiredText
    value: Any
    # Required, not defaulted: this is the choice between a value that dies with
    # the tool and one the whole session carries.
    store: PublishStore

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def _check_reserved_key(self) -> SetVariableConfig:
        if self.store == "userdata" and is_reserved_key(self.key):
            raise ValueError(RESERVED_KEY_ERROR)
        return self


class SayConfig(BaseModel):
    text: RequiredText
    wait_for_playback: bool = False

    model_config = {"extra": "forbid"}


class GenerateReplyConfig(BaseModel):
    instructions: RequiredText
    wait_for_playback: bool = False

    model_config = {"extra": "forbid"}


class AddMessageConfig(BaseModel):
    text: RequiredText

    model_config = {"extra": "forbid"}


class EndCallConfig(BaseModel):
    """No settings: an `end_call` ends the call."""

    model_config = {"extra": "forbid"}


class HandoffConfig(BaseModel):
    target_agent_id: str | None = Field(
        default=None, description="A published agent in this workspace."
    )
    agent_name: str | None = Field(
        default=None, description="A member of the team this call runs, by name."
    )
    context: HandoffContextPolicy = "transcript"
    summary: str | None = Field(
        default=None,
        description="Required under context 'summary'. A template, not a request for one.",
    )
    recent_turns: int | None = Field(
        default=None,
        strict=True,
        ge=HANDOFF_RECENT_TURNS_MIN,
        le=HANDOFF_RECENT_TURNS_MAX,
        description=(
            "Turns that cross verbatim alongside a summary, or in place of one under 'none'. "
            "Past ten, the honest answer is context 'transcript'."
        ),
    )
    message: str | None = Field(
        default=None, description="Spoken while connecting, before the target takes over."
    )

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def _check_target_and_context(self) -> HandoffConfig:
        # A stored agent, or a member of whatever team this call runs. Exactly
        # one: two targets in one operation is an authoring mistake, not a
        # fallback chain.
        named = [
            name
            for name in ("target_agent_id", "agent_name")
            if str(getattr(self, name) or "").strip()
        ]
        if not named:
            raise ValueError("set target_agent_id (a stored agent) or agent_name (a team member)")
        if len(named) == 2:
            raise ValueError("set either target_agent_id or agent_name, not both")
        # The operation's tree runs AFTER the model's tool call, so there is no
        # model-written argument to take a summary from — the author points this
        # at where the text comes from instead. It is an ordinary template, so
        # the token walk in `services/tools/validate.py` checks its roots.
        if self.context == "summary":
            if not str(self.summary or "").strip():
                raise ValueError(
                    "context 'summary' needs a `summary` - point it at where the text "
                    "comes from, e.g. {{args.summary}}"
                )
        elif str(self.summary or "").strip():
            raise ValueError(f"`summary` only applies to context 'summary', not '{self.context}'")
        # Rejected rather than ignored under `transcript`, which already carries
        # every turn: a number there is a misunderstanding worth an error rather
        # than a setting that silently does nothing.
        if self.recent_turns is not None and self.context == "transcript":
            raise ValueError(
                "`recent_turns` does not apply to context 'transcript', which "
                "already carries every turn - use context 'summary' or 'none'"
            )
        return self


def _canonical_e164(value: str) -> str:
    """A destination the worker can dial verbatim.

    Must already BE canonical, not merely normalizable. The worker dials this
    string as written, and `normalize_e164` would read a bare 10-digit number
    against the platform's default region — so a builder in one country could
    publish a number that means something else. Say which number we think they
    meant rather than silently rewriting what they typed.
    """
    # Lazy: `services.telephony` imports this package for tool validation
    # elsewhere, and a module-level import would close the loop.
    from services.telephony.e164 import normalize_e164

    try:
        normalized = normalize_e164(value)
    except ValueError:
        raise ValueError(
            f"destination {value!r} is not a valid phone number — "
            "use full international format, e.g. +14155550101"
        ) from None
    if normalized != value:
        raise ValueError(
            f"destination must be written in full international format — "
            f"write '{normalized}' rather than '{value}'"
        )
    return value


class TransferConfig(BaseModel):
    destination: Annotated[RequiredText, AfterValidator(_canonical_e164)] = Field(
        description="A literal phone number in full international format, e.g. +14155550101."
    )
    mode: TransferMode = "cold"
    ringing_timeout: (
        Annotated[
            float,
            Field(strict=True, ge=TRANSFER_RINGING_TIMEOUT_MIN, le=TRANSFER_RINGING_TIMEOUT_MAX),
        ]
        | None
    ) = Field(default=None, description="Seconds the destination may ring. Defaults to 30.")
    on_failure: TransferFailurePolicy = "continue"

    model_config = {"extra": "forbid"}


# What a keypad can produce, plus the two pauses and a space for readability.
# `w` (half a second) and `W` (one second) are ElevenLabs' spelling, and they are
# what make real IVR navigation possible: "press 2, wait, then the account
# number" is one string rather than three operations.
DTMF_KEYS = frozenset("0123456789*#ABCDwW ")
DTMF_DIGITS_MAX = 64


def _keypad_digits(value: str) -> str:
    """Refuse a key no telephone has — unless the value is a template.

    A `pattern` on the field cannot do this, and that is the whole reason this is
    a validator: `digits` IS template-resolved, so `{{args.account_number}}` is a
    legitimate value and any keypad pattern would reject it at save. The literal
    case still gets its feedback in the editor, and a template gets the same
    check at runtime against what it actually resolved to
    (`compiler/operations.py::_send_dtmf`).
    """
    if "{{" in value:
        return value
    unknown = sorted(set(value) - DTMF_KEYS)
    if unknown:
        raise ValueError(
            f"a keypad has no {''.join(unknown)!r} - use 0-9, *, #, A-D, or w/W to pause"
        )
    return value


class SendDtmfConfig(BaseModel):
    digits: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=DTMF_DIGITS_MAX),
        AfterValidator(_keypad_digits),
    ] = Field(
        description=(
            "Keys to press: 0-9, *, #, A-D. 'w' waits half a second and 'W' one second, "
            "for menus that need a pause. Supports {{args.*}} and {{userdata.*}}."
        )
    )

    model_config = {"extra": "forbid"}


# ──────────────────────────── operation variants ────────────────────────────
# The envelope is per-kind. `then`/`else` belong to an `if` and to nothing else;
# `silent`, `publish_fields` and `background_execution` belong to the three
# kinds that produce a result, because on the other eight there is no result to
# hide, to publish out of, or to stop waiting for.


class OperationBase(BaseModel):
    """What every variant shares: unknown fields are an error, not ignored."""

    model_config = {"extra": "forbid"}


class HttpOperation(OperationBase):
    """Call a REST API. The right operation for any plain request/response call."""

    kind: Literal["http"]
    config: HttpConfig
    on_error: OnError = "abort"
    silent: bool = False
    publish_fields: list[PublishFieldRequest] = Field(default_factory=list)
    background_execution: bool = False


class CodeOperation(OperationBase):
    """Run TypeScript to transform or shape data an `http` operation cannot.

    Its sandbox has `fetch`, but reaching an API with it instead of an `http`
    operation costs a V8 isolate per call and hides the request from the editor.
    """

    kind: Literal["code"]
    config: CodeConfig
    on_error: OnError = "abort"
    silent: bool = False
    publish_fields: list[PublishFieldRequest] = Field(default_factory=list)
    background_execution: bool = False


class FrontendRpcOperation(OperationBase):
    kind: Literal["frontend_rpc"]
    config: FrontendRpcConfig
    on_error: OnError = "abort"
    silent: bool = False
    publish_fields: list[PublishFieldRequest] = Field(default_factory=list)
    background_execution: bool = False


class IfOperation(OperationBase):
    """The one branching operation, and the only one with children.

    Branches do not rejoin: an `if` is terminal in its chain, so work needed on
    both paths is written once per path.
    """

    kind: Literal["if"]
    config: IfConfig
    on_error: OnError = "abort"
    then: list[OperationRequest] | None = None
    # `serialization_alias` as well as `alias`, so the validation and
    # serialization JSON schemas are the same document. Without it pydantic
    # emits an `-Input` / `-Output` pair for this model and for every model that
    # contains one — which includes `AgentConfig`, the type every SDK and the
    # dashboard are written against. It also makes the spec honest: FastAPI
    # serializes responses `by_alias=True`, so the wire has always said `else`.
    else_: list[OperationRequest] | None = Field(
        default=None, alias="else", serialization_alias="else"
    )

    # `json_schema_mode_override` collapses what would otherwise be an
    # `-Input` / `-Output` pair for this SELF-REFERENTIAL model — and for every
    # model that contains one, which is `AgentConfig`, the type every SDK and
    # the dashboard are written against. The two schemas are already identical
    # field for field; they differ only in the name each self-reference points
    # at, which pydantic cannot collapse on its own. Measured: dropping this
    # brings back `IfOperation-Input`/`-Output`, `AgentConfig-Input`/`-Output`,
    # `InlineTool-…` and `ToolSelection-…`, and the aliases above do not save it
    # — a self-referential model splits with no alias on it at all.
    model_config = {
        "extra": "forbid",
        "populate_by_name": True,
        "json_schema_mode_override": "validation",
    }


class SetVariableOperation(OperationBase):
    kind: Literal["set_variable"]
    config: SetVariableConfig
    on_error: OnError = "abort"


class SayOperation(OperationBase):
    kind: Literal["say"]
    config: SayConfig
    on_error: OnError = "abort"


class GenerateReplyOperation(OperationBase):
    kind: Literal["generate_reply"]
    config: GenerateReplyConfig
    on_error: OnError = "abort"


class AddMessageOperation(OperationBase):
    kind: Literal["add_message"]
    config: AddMessageConfig
    on_error: OnError = "abort"


class EndCallOperation(OperationBase):
    kind: Literal["end_call"]
    # The one config that may be omitted, because there is nothing to put in it.
    config: EndCallConfig = Field(default_factory=EndCallConfig)
    on_error: OnError = "abort"


class HandoffOperation(OperationBase):
    kind: Literal["handoff"]
    config: HandoffConfig
    on_error: OnError = "abort"


class TransferOperation(OperationBase):
    kind: Literal["transfer"]
    config: TransferConfig
    on_error: OnError = "abort"


class SendDtmfOperation(OperationBase):
    """Press keys on the call's keypad, for an IVR on the other end.

    Not terminal, unlike `end_call` and `transfer`: sending digits and then
    saying something is the normal case, so a chain carries on afterwards.
    """

    kind: Literal["send_dtmf"]
    config: SendDtmfConfig
    on_error: OnError = "abort"


OperationRequest = Annotated[
    HttpOperation
    | CodeOperation
    | FrontendRpcOperation
    | IfOperation
    | SetVariableOperation
    | SayOperation
    | GenerateReplyOperation
    | AddMessageOperation
    | EndCallOperation
    | HandoffOperation
    | TransferOperation
    | SendDtmfOperation,
    Field(discriminator="kind"),
]

IfOperation.model_rebuild()

# Read off the union, so a kind can only exist by having a variant of its own.
# The runtime's table of handlers is asserted against this in
# `compiler/operations.py`, which is the last hand-maintained link between what
# publish accepts and what a call can execute.
TREE_KINDS: frozenset[str] = frozenset(
    get_args(variant.model_fields["kind"].annotation)[0]
    for variant in get_args(get_args(OperationRequest)[0])
)

OPERATION_ADAPTER: TypeAdapter[OperationRequest] = TypeAdapter(OperationRequest)
