"""Pin a config's moving parts to what they resolve to right now.

Publishing an agent does this and freezes the result; resolving a call plan does
it and writes the result into the plan; publishing a task does it for the task's
own tools. Same rule, one implementation: nothing a config names may change
under a running call between the pin and the answer.

Three things move, for three different reasons. A **tool** or a **task** moves
because somebody republished it. A **reasoning effort** moves because the
catalog decides the default, and for one provider the catalog is a live registry
of ~260 models that OpenRouter changes weekly — so an agent whose author never
touched the control could find its thinking level altered under it, and on a
voice call that is the difference between answering and pausing on every turn.

An OpenRouter **speech-to-text** model needs no pin, and none is missing: the
runtime reads nothing per model for it — whether it streams comes from the
provider's `models.stt` block, and no language is ever sent.

Every function here is generic over ``AgentBase`` and returns the type it was
given, so an agent stays an agent and a task stays a task.
"""

from __future__ import annotations

from typing import Any, Protocol, TypeVar, cast
from uuid import UUID

from services.catalog import LLMEntry, RealtimeEntry, ReasoningEffort, TTSEntry, get_catalog

from .models import (
    AgentBase,
    AgentConfig,
    LLMModelSpec,
    RealtimeSpec,
    TaskSelection,
    ToolSelection,
    TTSModelSpec,
)

C = TypeVar("C", bound=AgentBase)


class _Fetcher(Protocol):
    async def fetch(self, query: str, *args: Any) -> list[Any]: ...


async def pin_tool_selections(
    executor: _Fetcher,
    tenant_id: UUID,
    cfg: C,
) -> C:
    """Return a copy of ``cfg`` with every stored tool pinned to its live version.

    The definitions themselves stay in ``tool_versions``, which is already
    immutable and already keyed by exactly (tool_id, version) — copying them in
    would be a second source of truth that could only ever agree with the first.

    An inline tool passes through untouched: it *is* the definition, so there is
    nothing to pin it to.
    """
    stored = [sel.tool_id for sel in cfg.tool_selections() if sel.tool_id]
    if not stored:
        return cfg
    rows = await executor.fetch(
        "SELECT id, name, published_version FROM tools "
        "WHERE id = ANY($1::uuid[]) AND tenant_id = $2",
        stored,
        tenant_id,
    )
    live = {str(r["id"]): r for r in rows}

    def pin(sel: ToolSelection | None) -> ToolSelection | None:
        if sel is None or sel.tool_id is None:
            return sel
        row = live.get(sel.tool_id)
        # Validation with for_publish=True has already rejected a tool that is
        # missing or unpublished, so reaching here means the two disagree.
        if row is None or not row["published_version"]:
            raise RuntimeError(f"tool {sel.tool_id} is unpublished but passed publish validation")
        return ToolSelection(tool_id=sel.tool_id, tool_version=row["published_version"])

    return cfg.model_copy(
        update={
            "tools": [pin(sel) for sel in cfg.tools],
            "on_enter": pin(cfg.on_enter),
            "on_exit": pin(cfg.on_exit),
            "on_user_turn_completed": pin(cfg.on_user_turn_completed),
        }
    )


def unpin_tool_selections(cfg: C) -> C:
    """The same config with every tool version dropped.

    A frozen version pins its tools; a draft never does — it tracks whatever is
    published now. Restoring a version into the draft therefore strips the pins,
    the way restoring a tool version strips `compiled_js`: publishing adds the
    derived part, and the draft stays intent-only.
    """

    def bare(sel: ToolSelection | None) -> ToolSelection | None:
        if sel is None or sel.tool_id is None:
            return sel
        return ToolSelection(tool_id=sel.tool_id)

    return cfg.model_copy(
        update={
            "tools": [bare(sel) for sel in cfg.tools],
            "on_enter": bare(cfg.on_enter),
            "on_exit": bare(cfg.on_exit),
            "on_user_turn_completed": bare(cfg.on_user_turn_completed),
        }
    )


def _pinned_effort(spec: LLMModelSpec | None) -> LLMModelSpec | None:
    """One model spec with the effort it will actually run at written onto it."""
    if spec is None:
        return spec
    entry = get_catalog().entry("llm", spec.provider, spec.model)
    if not isinstance(entry, LLMEntry):
        # Validation with for_publish=True has already rejected an unknown model,
        # and a call plan resolves a config that published. Reaching here means
        # the two disagree.
        raise RuntimeError(f"llm {spec.provider}/{spec.model} is not a catalog model")
    effort = entry.effort_for(spec.reasoning_effort)
    return spec.model_copy(update={"reasoning_effort": cast(ReasoningEffort | None, effort)})


def pin_reasoning_efforts(cfg: C) -> C:
    """Return a copy of ``cfg`` whose every model states its own thinking level.

    The author's choice where they made one, the catalog's default where they did
    not, and null for a model with no such knob — resolved once, here, instead of
    on every turn of every future call.

    Two things follow, and both are the point. A `catalog.yaml` edit stops
    silently changing what a published agent does; and a **voice job process can
    run an OpenRouter model at all**, because that process holds no registry to
    resolve a default out of (see `services.catalog.SearchedModels`). The
    invariant the runtime rests on is stated in `compiler.factories`: a config
    reaching a voice job process is always pinned.

    Accepted cost: a default can no longer be improved for already-published
    agents by editing one line — they have to be republished. And publishing an
    agent whose thinking level was never touched produces a version that differs
    from the draft by exactly that field, which is correct and shows in the diff.
    """
    llm = cfg.llm
    if llm is None:
        return cfg
    pinned = _pinned_effort(llm)
    assert pinned is not None
    updates: dict[str, Any] = {
        "llm": pinned.model_copy(update={"fallback": _pinned_effort(llm.fallback)})
    }
    # The one model that is not part of the pipeline: post-call analysis. Its
    # `model` is optional and unset means the agent's own LLM, which the line
    # above has already pinned.
    analysis = getattr(cfg, "analysis", None)
    if analysis is not None and analysis.model is not None:
        updates["analysis"] = analysis.model_copy(update={"model": _pinned_effort(analysis.model)})
    return cfg.model_copy(update=updates)


S = TypeVar("S", bound=TTSModelSpec | RealtimeSpec)


def _defaulted_voice(spec: S | None, kind: str) -> S | None:
    if spec is None or spec.voice:
        return spec
    entry = get_catalog().entry(kind, spec.provider, spec.model)
    # An unknown model is validation's to name; leave it for that.
    if not isinstance(entry, TTSEntry | RealtimeEntry) or not entry.default_voice:
        return spec
    return spec.model_copy(update={"voice": entry.default_voice})


def pin_default_voices(cfg: AgentConfig) -> AgentConfig:
    """Write each unset voice as its model's catalog default, on save.

    So `GET` shows the voice that will speak, and a `catalog.yaml` edit cannot
    change a saved agent's voice. A per-call config skips this: the compiler
    falls back to the same default (`compiler.factories._entry_voice`).
    """
    updates: dict[str, Any] = {"realtime": _defaulted_voice(cfg.realtime, "realtime")}
    tts = _defaulted_voice(cfg.tts, "tts")
    if tts is not None:
        updates["tts"] = tts.model_copy(update={"fallback": _defaulted_voice(tts.fallback, "tts")})
    return cfg.model_copy(update=updates)


async def pin_task_selections(
    executor: _Fetcher,
    tenant_id: UUID,
    cfg: AgentConfig,
) -> AgentConfig:
    """Return a copy of ``cfg`` with every stored task pinned to its live version.

    The tool pair's sibling, and a sharper pin than theirs: a task's ``vars`` ARE
    the schema of the tool the model calls it with and its ``output`` IS that
    tool's return type, so a task republished under a running agent would change
    what the model is shown mid-call.

    An inline task has no version to pin — it *is* the definition — but the tools
    IT names still do, so they are pinned here too. Miss that and
    `resolve_pinned_tasks` raises at session start on a call plan that carried
    one.
    """
    if not cfg.tasks:
        return cfg
    stored = [sel.task_id for sel in cfg.tasks if sel.task_id]
    rows = (
        await executor.fetch(
            "SELECT id, name, published_version FROM agent_tasks "
            "WHERE id = ANY($1::uuid[]) AND tenant_id = $2",
            stored,
            tenant_id,
        )
        if stored
        else []
    )
    live = {str(r["id"]): r for r in rows}

    async def pin(sel: TaskSelection) -> TaskSelection:
        if sel.task is not None:
            return sel.model_copy(
                update={"task": await pin_tool_selections(executor, tenant_id, sel.task)}
            )
        row = live.get(sel.task_id or "")
        # Validation with for_publish=True has already rejected a task that is
        # missing or unpublished, so reaching here means the two disagree.
        if row is None or not row["published_version"]:
            raise RuntimeError(f"task {sel.task_id} is unpublished but passed publish validation")
        return sel.model_copy(update={"task_version": row["published_version"]})

    return cfg.model_copy(update={"tasks": [await pin(sel) for sel in cfg.tasks]})


def unpin_task_selections(cfg: AgentConfig) -> AgentConfig:
    """The same config with every task version dropped — the draft's shape.

    A frozen version pins its tasks; a draft never does. Restoring a version into
    the draft therefore strips the pins, exactly as it does for tools.
    """

    def bare(sel: TaskSelection) -> TaskSelection:
        if sel.task is not None:
            return sel.model_copy(update={"task": unpin_tool_selections(sel.task)})
        return sel.model_copy(update={"task_version": None})

    return cfg.model_copy(update={"tasks": [bare(sel) for sel in cfg.tasks]})
