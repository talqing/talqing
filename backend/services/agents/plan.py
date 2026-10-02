"""What runs on one call: name an agent, bring one, or bring a cast of them.

Four endpoints start a call — web token, outbound PSTN, text conversation and
campaign batch — and all four take the same five fields and resolve them here.

    agent_id  agent_version  agent  agent_override        …or… agent_team

Three rules, and they hold at every level:

1. Exactly one of ``agent_id`` and ``agent``. ``agent_version`` requires
   ``agent_id`` — there is no version of a definition you just wrote.
2. ``agent_override`` layers onto whichever base was named (``services.agents.override``).
3. The result must validate as a complete `AgentConfig` for this endpoint's
   channel, by the same ``validate_agent_draft(..., for_publish=True)`` that
   guards publishing. That is the whole reason this is safe: an inline agent is
   validated by the code that guards publish, so nothing had to be written to
   make it trustworthy and there is no second validator to drift.

**Version pinning has one rule: an override pins; a bare id follows published.**
A member with a stored base pins the version at request time, because its
overrides were validated against that exact base and a republish underneath
would leave the merged result unvalidated. A `HandoffTarget.agent_id` outside
the roster carries no override, so it enters the target's latest published
version — unchanged behaviour. Tools pin for the same reason publishing pins
them: a tool republished between dial and answer must not change what runs.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from fastapi import HTTPException
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from api.core.schemas import JsonObject, field_errors, validation_error
from services.system_vars import SESSION_VARS_DESCRIPTION, validate_session_vars
from services.tools import inline_code_configs, transpile_code_configs
from services.tools.service import CODE_EXEC_UNREACHABLE
from services.user import Context

from .models import AgentConfig, missing_required_vars
from .override import AgentOverride, deep_merge, drop_superseded_efforts, override_dump
from .pin import pin_reasoning_efforts, pin_task_selections, pin_tool_selections

# Above ten the shape being asked for is a workflow, not a team — and Vapi
# switched theirs off. Stated rather than left to be discovered.
MAX_TEAM_MEMBERS = 10

# The five config keys that carry a pinned attachment. Listed once: they are what
# request-time pinning rewrites, and what an override touching any of them makes
# us rewrite.
_TOOL_KEYS = ("tools", "on_enter", "on_exit", "on_user_turn_completed", "tasks")

# Every key `_pin_all` resolves, and therefore every key a stored override has to
# carry the resolved value of. `llm` joins the tool keys because
# `pin_reasoning_efforts` writes into it: an override that names a model but no
# thinking level would otherwise reach the worker with the BASE's level on a
# different model, or with none at all on a provider whose default this process
# is the last one able to look up.
#
# `analysis` is deliberately absent. Its effort is not read by anything that
# needs the registry — `services.llm_client` sends nothing when it cannot
# resolve a default, rather than guessing — so pinning it here would only add
# the whole spec, extraction fields and all, to a plan a batch copies per call.
_PINNED_KEYS = (*_TOOL_KEYS, "llm")


# ─────────────────────────────── request shapes ─────────────────────────────


class AgentSelection(BaseModel):
    """Name an agent, or bring one — plus what this call changes about it."""

    agent_id: UUID | None = Field(
        default=None,
        description="An agent in this workspace. Mutually exclusive with `agent`.",
    )
    agent_version: int | Literal["draft"] | None = Field(
        default=None,
        description=(
            'Which version of `agent_id` to run. Omit for the published one. `"draft"` runs '
            "the unpublished working copy — validated and tool-pinned at request time, and "
            "refused with the publish errors if it does not hold together."
        ),
    )
    agent: AgentConfig | None = Field(
        default=None,
        description=(
            "A complete agent definition, run for this call and stored nowhere. Mutually "
            "exclusive with `agent_id`."
        ),
    )
    agent_override: AgentOverride | None = Field(  # type: ignore[valid-type]
        default=None,
        description=(
            "Changes layered on top of whichever base was named. Absent keys keep the base "
            "value, an explicit null clears the field, objects deep-merge and lists replace."
        ),
    )

    @model_validator(mode="after")
    def _one_base(self) -> AgentSelection:
        if self.agent_id is not None and self.agent is not None:
            raise ValueError("set either agent_id or agent, not both")
        if self.agent_version is not None and self.agent_id is None:
            raise ValueError("agent_version needs an agent_id - an inline agent has no versions")
        return self


class AgentTeamMember(AgentSelection):
    """One agent on a multi-agent call, described exactly as a single-agent call is."""

    name: str = Field(
        min_length=1,
        description=(
            "This member's name for this call, winning over the stored agent's own. Handoff "
            "edges reference it and the transcript records it."
        ),
    )

    @model_validator(mode="after")
    def _has_a_base(self) -> AgentTeamMember:
        if self.agent_id is None and self.agent is None:
            raise ValueError(f"team member '{self.name}' needs an agent_id or an inline agent")
        return self


class AgentTeam(BaseModel):
    """A cast of agents for one call, handing off to each other by name.

    An object rather than a bare list so the schema has a name in the OpenAPI
    document and can be widened additively.
    """

    members: list[AgentTeamMember] = Field(
        min_length=1,
        max_length=MAX_TEAM_MEMBERS,
        description=(
            "**`members[0]` answers the call.** Order is semantics. Every member is in the "
            "roster under its `name`, the entry included, which is what lets a member hand the "
            "call back to whoever answered."
        ),
    )


class AgentPlanRequest(AgentSelection):
    """The fields every call-starting endpoint takes: one agent, or a team of them."""

    agent_team: AgentTeam | None = Field(
        default=None,
        description=(
            "Run several agents on this call, defined here. Mutually exclusive with "
            "`agent_id` / `agent` / `agent_version` / `agent_override`."
        ),
    )
    # Declared here rather than on each of the four requests, and not only to
    # delete three copies of a paragraph: `resolve_call_plan` takes an
    # `AgentPlanRequest`, so this is what lets it check the bag against the cast
    # it just merged without a parameter threaded through four call sites. Each
    # subclass keeps its own sentence about what the bag means for it — a text
    # thread's is fixed for the thread's life, a batch's is the campaign's.
    vars: dict[str, str] | None = Field(default=None, description=SESSION_VARS_DESCRIPTION)

    @field_validator("vars")
    @classmethod
    def _check_vars(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        # An empty bag is no bag: `{}` and absent reach the resolver identically
        # and neither is stored.
        return validate_session_vars(value) or None

    @model_validator(mode="after")
    def _team_or_one(self) -> AgentPlanRequest:
        if self.agent_team is not None and (
            self.agent_id is not None
            or self.agent is not None
            or self.agent_version is not None
            or self.agent_override is not None
        ):
            raise ValueError("send either a single agent or an agent_team, not both")
        return self


# ─────────────────────────────── the resolved plan ──────────────────────────


@dataclass(frozen=True, slots=True)
class PlanMember:
    """One agent this call can run, resolved and ready."""

    name: str
    agent_id: str | None
    agent_version_id: str | None
    version: int | None
    config: AgentConfig
    # The layered, tool-pinned changes on top of `version`'s frozen config —
    # or, when there is no version, the whole config. One field for both cases,
    # which is why the worker has no second code path.
    override: dict[str, Any]
    # Whether `version` is simply "whatever is published", rather than a version
    # the caller asked for. Only that case can be left out of the stored plan —
    # see `CallPlan.stored`.
    follows_published: bool


@dataclass(frozen=True, slots=True)
class CallPlan:
    """Everything the four endpoints need to record and dispatch one call."""

    members: tuple[PlanMember, ...]
    warnings: tuple[str, ...]
    # The request's `vars`, validated once and checked against what the cast
    # declares. Read from here rather than off the request again, so the four
    # endpoints cannot each arrive at a slightly different bag.
    vars: dict[str, str]

    @property
    def entry(self) -> PlanMember:
        """The member that answers. `members[0]`, as Vapi's `members[0]` is."""
        return self.members[0]

    def stored(self) -> dict[str, Any] | None:
        """The JSONB to write, or None when this call runs exactly like today's.

        None only for a single member that names an `agent_id`, takes whatever is
        published, and layers nothing on it — which is every call today. The
        worker then takes the path it always has.

        `follows_published` is load-bearing rather than decorative: two of the
        four endpoints have nowhere else to carry a version. A batch re-resolves
        the agent on every dispatcher pass and a text thread on every message, so
        leaving `agent_version: 3` out of the plan would silently run whatever is
        published instead.
        """
        entry = self.entry
        if len(self.members) == 1 and entry.follows_published and not entry.override:
            return None
        return {
            "members": [
                {
                    "name": m.name,
                    "agent_id": m.agent_id,
                    "version": m.version,
                    "override": m.override,
                }
                for m in self.members
            ]
        }

    def team(self) -> list[dict[str, Any]]:
        """The roster as an API reader sees it — `[0]` is the one that answered."""
        return [
            {"name": m.name, "agent_id": m.agent_id, "version": m.version} for m in self.members
        ]


class StoredPlanMember(BaseModel):
    """One member of a frozen plan, as `sessions.agent_plan` stores it."""

    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    name: str | None = None
    agent_id: UUID | None = None
    version: int | None = None
    # The override payload as it was dumped, kept verbatim. Typed as an object
    # rather than `AgentOverride` because it is a snapshot: a later change to
    # what an override may contain must not make an old call unreadable.
    override: JsonObject = Field(default_factory=dict)


class StoredAgentPlan(BaseModel):
    """What a call ran, frozen at the moment it started.

    Written only when the call differs from "the published agent, as-is" — so a
    null `agent_plan` means exactly that. `members[0]` is the agent that
    answered.
    """

    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    members: list[StoredPlanMember] = Field(default_factory=list)


def draft_agent_ids(plan: Mapping[str, Any] | None) -> set[str]:
    """The agents a stored plan ran as their unpublished draft.

    `agent_version: "draft"` is the only way a stored member names an agent and
    no version (`_load_bases`), so readers report such a member's version as
    `"draft"` — the value the request asked for — rather than as a bare null.
    """
    members = (plan or {}).get("members") or []
    return {str(m["agent_id"]) for m in members if m.get("agent_id") and not m.get("version")}


# ────────────────────────────────── resolution ──────────────────────────────


@dataclass(frozen=True, slots=True)
class _Requested:
    """One member as asked for: the selection, plus the name it must answer to."""

    selection: AgentSelection
    # None on a single-agent call, where the resolved config's own name stands.
    name: str | None


def _requested(request: AgentPlanRequest) -> list[_Requested]:
    if request.agent_team is not None:
        return [_Requested(m, m.name) for m in request.agent_team.members]
    if request.agent_id is None and request.agent is None:
        raise HTTPException(
            status_code=400,
            detail="name an agent_id, send an inline agent, or send an agent_team",
        )
    return [_Requested(request, None)]


@dataclass(frozen=True, slots=True)
class _Base:
    """The definition a member's overrides are layered onto.

    ``merge`` is what we merge against now; ``stored`` is what the worker will
    re-read later. They differ for exactly one case — `agent_version: "draft"`,
    where the draft is a moving target and so travels in the plan rather than
    being pointed at.
    """

    merge: dict[str, Any]
    stored_is_empty: bool
    agent_id: str | None
    agent_version_id: str | None
    version: int | None
    # True only when `version` came from `agents.published_version` rather than
    # from the request. See `CallPlan.stored`.
    follows_published: bool


async def _load_bases(ctx: Context, requested: Sequence[_Requested]) -> list[_Base]:
    """Every member's base definition, in two queries however long the roster."""
    pool = await ctx.tenant_pool()
    agent_ids = list(
        dict.fromkeys(str(r.selection.agent_id) for r in requested if r.selection.agent_id)
    )
    agents: dict[str, Any] = {}
    if agent_ids:
        rows = await pool.fetch(
            "SELECT id, name, published_version, config FROM agents "
            "WHERE id = ANY($1::uuid[]) AND tenant_id = $2",
            agent_ids,
            ctx.tenant.id,
        )
        agents = {str(r["id"]): r for r in rows}
        for agent_id in agent_ids:
            if agent_id not in agents:
                raise HTTPException(status_code=404, detail="agent not found")

    # Which frozen versions to read: the published one unless a number was asked
    # for. A draft reads nothing — `agents.config` is already in hand.
    wanted: list[tuple[str, int]] = []
    for r in requested:
        selection = r.selection
        if not selection.agent_id or selection.agent_version == "draft":
            continue
        agent_id = str(selection.agent_id)
        if isinstance(selection.agent_version, int):
            wanted.append((agent_id, selection.agent_version))
            continue
        published = agents[agent_id]["published_version"]
        if published is None:
            # Named rather than bare "publish the agent": on a team the caller
            # has to know WHICH one, and even alone the name is what they wrote.
            raise HTTPException(
                status_code=400,
                detail=f"publish '{agents[agent_id]['name']}' before calling it",
            )
        wanted.append((agent_id, published))

    versions: dict[tuple[str, int], Any] = {}
    if wanted:
        rows = await pool.fetch(
            """
            SELECT av.id, av.agent_id, av.version, av.config
            FROM unnest($2::uuid[], $3::int[]) AS p(agent_id, version)
            JOIN agent_versions av
                ON av.agent_id = p.agent_id AND av.version = p.version AND av.tenant_id = $1
            """,
            ctx.tenant.id,
            [agent_id for agent_id, _ in wanted],
            [version for _, version in wanted],
        )
        versions = {(str(r["agent_id"]), r["version"]): r for r in rows}
        for agent_id, version in wanted:
            if (agent_id, version) not in versions:
                raise HTTPException(status_code=404, detail=f"agent has no version {version}")

    out: list[_Base] = []
    for r in requested:
        selection = r.selection
        if selection.agent is None:
            agent_id = str(selection.agent_id)
            if selection.agent_version == "draft":
                # The draft moves, so it travels IN the plan rather than being
                # pointed at: the base is empty and the override carries the
                # whole thing, exactly as it does for an inline agent.
                out.append(
                    _Base(
                        merge=dict(agents[agent_id]["config"] or {}),
                        stored_is_empty=True,
                        agent_id=agent_id,
                        agent_version_id=None,
                        version=None,
                        follows_published=False,
                    )
                )
                continue
            version = (
                selection.agent_version
                if isinstance(selection.agent_version, int)
                else agents[agent_id]["published_version"]
            )
            row = versions[(agent_id, version)]
            out.append(
                _Base(
                    merge=dict(row["config"]),
                    stored_is_empty=False,
                    agent_id=agent_id,
                    agent_version_id=str(row["id"]),
                    version=version,
                    follows_published=not isinstance(selection.agent_version, int),
                )
            )
        else:
            out.append(
                _Base(
                    merge=selection.agent.model_dump(mode="json"),
                    stored_is_empty=True,
                    agent_id=None,
                    agent_version_id=None,
                    version=None,
                    follows_published=False,
                )
            )
    return out


def _channel_phrase(channels: Sequence[str]) -> str:
    return " or ".join(channels)


async def resolve_call_plan(
    ctx: Context,
    request: AgentPlanRequest,
    *,
    channels: Sequence[str],
    channel_hint: str = "",
) -> CallPlan:
    """Turn one request into the cast that will run, or say exactly why not.

    ``channel_hint`` is appended to a channel mismatch — the endpoint knows
    where the caller should have gone instead, and this function does not.
    """
    # Lazy: `services.agents.validate` reaches `services.integrations`, which
    # reaches `services.conversations`, whose request models are built on the
    # `AgentPlanRequest` defined above. Importing it at module level would close
    # that loop. Nothing else here needs the validator at import time.
    from .validate import validate_agent_draft

    requested = _requested(request)
    bases = await _load_bases(ctx, requested)

    configs: list[AgentConfig] = []
    overrides: list[dict[str, Any]] = []
    for r, base in zip(requested, bases, strict=True):
        override = override_dump(r.selection.agent_override)
        merged = deep_merge(base.merge, override)
        drop_superseded_efforts(merged, override)
        if r.name is not None:
            # Last word in the cascade: base -> agent -> agent_override -> name.
            merged["name"] = r.name
        try:
            configs.append(AgentConfig.model_validate(merged))
        except ValidationError as exc:
            raise validation_error(
                field_errors(exc.errors()),
                _prefix(r.name, "the resolved agent config is invalid"),
            ) from exc
        overrides.append(override)

    roster = {cfg.name: cfg for cfg in configs}
    if len(roster) != len(configs):
        seen: set[str] = set()
        for cfg in configs:
            if cfg.name in seen:
                raise HTTPException(
                    status_code=422,
                    detail=f"two agents on this call are named '{cfg.name}' - names must be unique",
                )
            seen.add(cfg.name)

    errors: list[str] = []
    warnings: list[str] = []
    named = len(configs) > 1

    for cfg in configs:
        if cfg.channel not in channels:
            errors.append(
                f"this endpoint runs {_channel_phrase(channels)} agents; "
                f"'{cfg.name}' resolves to a {cfg.channel} agent{channel_hint}"
            )
    if errors:
        raise validation_error(errors, "the resolved agent config is invalid")

    # Before anything is compiled and before any provider is called, so a missing
    # value costs nothing. On the MERGED configs, which is what makes an
    # `agent_override` that adds a default — or clears the flag — satisfy this.
    #
    # Reported by variable and deduped, with no member prefix unlike the channel
    # errors above: there is one bag for the whole session, so naming which
    # member wanted a value would imply you could satisfy it per member. The
    # wording is the task path's, verbatim (`services.tasks.service`) — it is the
    # same rule, and a tenant should not have to learn two sentences for it.
    session_vars = request.vars or {}
    needed = sorted(
        {name for cfg in configs for name in missing_required_vars(cfg.vars, session_vars)}
    )
    if needed:
        raise validation_error(
            [f"'{name}' is required and has no default" for name in needed],
            "this session needs values it was not given",
        )

    # Every inline `code` op across the whole roster, compiled in one round trip.
    # Compiling IS how a `code` op is validated, so skipping it here would leave
    # the one part of an inline agent that escaped the publish validator — and
    # it hands us the build artifact for free, which is why the plan carries
    # `compiled_js` beside `source_ts` and the worker never transpiles.
    # The artifact is written onto each authored `CodeConfig`, so it is already
    # there when `operation_from_request` later dumps the tree into the plan.
    code_configs = [
        config
        # An inline TASK brings inline tools of its own, and they escape the
        # publish validator exactly as the agent's do — same walk, one level
        # deeper, which is all `TaskConfig` allows.
        for cfg in configs
        for owner in (cfg, *(sel.task for sel in cfg.tasks if sel.task))
        for sel in owner.tool_selections()
        if sel.tool
        for config in inline_code_configs(sel.tool.operations)
    ]
    if code_configs:
        compile_errors = await transpile_code_configs(code_configs)
        if CODE_EXEC_UNREACHABLE in compile_errors:
            raise HTTPException(status_code=502, detail="the code-execution service is unreachable")
        if compile_errors:
            raise validation_error(compile_errors, "the resolved agent config is invalid")

    results = await asyncio.gather(
        *(validate_agent_draft(ctx, cfg, for_publish=True, roster=roster) for cfg in configs)
    )
    for cfg, result in zip(configs, results, strict=True):
        prefix = f"'{cfg.name}': " if named else ""
        errors.extend(f"{prefix}{e}" for e in result.errors)
        warnings.extend(f"{prefix}{w}" for w in result.warnings)
    if errors:
        raise validation_error(errors, "the resolved agent config is invalid")

    pool = await ctx.tenant_pool()
    pinned = await asyncio.gather(*(_pin_all(pool, ctx.tenant.id, cfg) for cfg in configs))

    members: list[PlanMember] = []
    for cfg, base, override in zip(pinned, bases, overrides, strict=True):
        members.append(
            PlanMember(
                name=cfg.name,
                agent_id=base.agent_id,
                agent_version_id=base.agent_version_id,
                version=base.version,
                config=cfg,
                override=_stored_override(cfg, base, override),
                follows_published=base.follows_published,
            )
        )

    warnings.extend(_unreachable(members))
    plan = CallPlan(
        members=tuple(members),
        warnings=tuple(dict.fromkeys(warnings)),
        vars=session_vars,
    )
    return plan


async def _pin_all(pool: Any, tenant_id: UUID, cfg: AgentConfig) -> AgentConfig:
    """Every pin, in one place: a call plan freezes tool versions, task versions
    and thinking levels alike, so nothing a member names can move under a running
    call.

    The third one matters most on the draft path. A test call resolves an
    unpublished config here, in a process that holds the model registry, and runs
    it in a voice job process that does not — so this is where an OpenRouter
    model's default effort gets decided while there is still something to decide
    it from."""
    return pin_reasoning_efforts(
        await pin_task_selections(pool, tenant_id, await pin_tool_selections(pool, tenant_id, cfg))
    )


def _prefix(name: str | None, message: str) -> str:
    return f"'{name}': {message}" if name else message


def _stored_override(cfg: AgentConfig, base: _Base, override: dict[str, Any]) -> dict[str, Any]:
    """What goes in the plan beside this member's base.

    With no base — an inline agent, or a draft, which moves — the whole resolved
    config travels, because there is nothing for the worker to re-read.

    Otherwise it is what the caller sent, with every key `_pin_all` resolves
    replaced by the resolved value: a published base is pinned by definition, so
    this only rewrites a key when an override actually touched one.

    That rewrite is what keeps the invariant the runtime rests on — a config
    reaching a voice job process is always pinned. The worker re-merges this
    override onto the base itself (`load_plan_roster`), in a process that holds
    no model registry, so a half-pinned `llm` here becomes a model running at a
    thinking level nobody chose.
    """
    if base.stored_is_empty:
        return cfg.model_dump(mode="json")
    touched = [key for key in _PINNED_KEYS if key in override]
    if not touched:
        return override
    pins = cfg.model_dump(mode="json", include=set(_PINNED_KEYS))
    return {**override, **{key: pins[key] for key in touched}}


def _unreachable(members: Sequence[PlanMember]) -> list[str]:
    """Members nothing on the call hands off to.

    A warning rather than a refusal: a member reachable only after two hops is
    legitimate, and a typo is not — naming it is how the author tells them apart.
    """
    if len(members) < 2:
        return []
    reached = {
        target.name
        for member in members
        for target in member.config.handoffs
        if not target.agent_id
    }
    return [
        f"nothing hands off to '{m.name}', so it will never take a turn on this call"
        for m in members[1:]
        if m.name not in reached
    ]


# ──────────────────────────── reading a stored plan ─────────────────────────


@dataclass(frozen=True, slots=True)
class RosterMember:
    """One member of a stored plan, resolved back into the config that runs."""

    name: str
    agent_id: str | None
    version: int | None
    config: AgentConfig


async def load_plan_roster(
    executor: Any,
    tenant_id: UUID,
    plan: Mapping[str, Any],
) -> list[RosterMember]:
    """Re-resolve a stored `agent_plan` into the cast it names. `[0]` answers.

    One batched read for every member that has a stored base — `unnest` over the
    (agent_id, version) pairs, the same shape `resolve_pinned_tools` uses. A
    member with no version contributes no pair: its override IS the whole config.

    Every member is resolved here, not lazily at handoff. Resolving one needs its
    base version config, so a lazy resolve would put a database read in the
    middle of a handoff — and a member whose base has since been deleted would
    only be discovered with the caller already asking to be transferred. One
    query at answer time turns that class of failure into something the run can
    report up front. What stays lazy is everything expensive: a member's tools,
    hooks and MCP servers load only when the call hands off to it.
    """
    members = list(plan.get("members") or [])
    if not members:
        raise ValueError("agent plan has no members")

    wanted = [
        (str(m["agent_id"]), int(m["version"]))
        for m in members
        if m.get("agent_id") and m.get("version")
    ]
    bases: dict[tuple[str, int], dict[str, Any]] = {}
    if wanted:
        rows = await executor.fetch(
            """
            SELECT av.agent_id, av.version, av.config
            FROM unnest($2::uuid[], $3::int[]) AS p(agent_id, version)
            JOIN agent_versions av
                ON av.agent_id = p.agent_id AND av.version = p.version AND av.tenant_id = $1
            """,
            tenant_id,
            [agent_id for agent_id, _ in wanted],
            [version for _, version in wanted],
        )
        bases = {(str(r["agent_id"]), r["version"]): dict(r["config"]) for r in rows}
        missing = [f"{a} v{v}" for a, v in wanted if (a, v) not in bases]
        if missing:
            # The agent was deleted after the call was planned, taking its
            # versions with it. Nothing sane to run — say which one is gone.
            raise ValueError(f"agent plan names version(s) that no longer exist: {missing}")

    out: list[RosterMember] = []
    for m in members:
        agent_id = str(m["agent_id"]) if m.get("agent_id") else None
        version = int(m["version"]) if m.get("version") else None
        base = bases[(agent_id, version)] if agent_id and version else {}
        out.append(
            RosterMember(
                name=m["name"],
                agent_id=agent_id,
                version=version,
                # The member name is the last word in the cascade, here as it was
                # when the plan was resolved: it is what handoff edges reference
                # and what the transcript records, so re-merging without it would
                # put the STORED agent's name on a member the caller renamed.
                config=AgentConfig.model_validate(
                    {**deep_merge(base, m.get("override") or {}), "name": m["name"]}
                ),
            )
        )
    return out
