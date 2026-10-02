"""`AgentOverride` — the same field tree as `AgentConfig`, every field optional.

One grammar, two surfaces: it layers one call onto an agent, and it is the body
of `PATCH /agents/{id}`, layered onto the stored draft.

The override type IS the config type, generated rather than written, so it
cannot lag: adding a field to `AgentConfig` adds it here, to the OpenAPI
document and to the merge, with no second edit. Hand-writing it would duplicate
~250 lines of field definitions that drift the first time somebody adds a knob
to `TurnHandlingSpec`.

Not `dict[str, Any]`, for the same reason: a free-form object publishes as `{}`
in the OpenAPI document, so a generated client would have no types and a
migrating developer reading our spec would see nothing.

Merge semantics live here too, and there are five rules:

===================================  ==========================================
absent key                           the base value is kept
explicit ``null`` on a nullable      the field is CLEARED
object value                         deep-merged, key by key, recursively
object value naming a new provider   starts over (nested specs such as
                                     ``fallback`` are still merged)
list / scalar value                  replaced wholesale
===================================  ==========================================

Deep merge is the only defensible choice for objects: with shallow replace
``{"tts": {"voice": "aditi"}}`` would silently reset provider and model to the
defaults, and the agent would answer in a different voice from a different
vendor with nothing saying so. Its own trap is the provider switch: a voice,
a speed range, a host list all belong to the provider that offered them, so
``{"tts": {"provider": "elevenlabs", "model": "…"}}`` merged key by key would
keep a Sarvam voice id on an ElevenLabs model. A new provider therefore starts
the object over, keeping only the nested specs that name a provider of their own.
"""

from __future__ import annotations

from types import UnionType
from typing import Any, Union, get_args, get_origin

from pydantic import BaseModel, ConfigDict, Field, create_model
from pydantic_core import to_jsonable_python

from .models import AgentConfig, FaqSelection, HandoffTarget, McpSelection, ToolSelection

# Types an override REPLACES rather than deep-merges. Each is a reference —
# "this tool", "this server", "this FAQ", "this destination" — and half a reference is not a
# smaller reference, it is a different one: `{"on_enter": {"tool_id": "b"}}`
# deep-merged onto `{"tool_id": "a", "tool_version": 2}` would attach tool b at
# tool a's version. They stay whole models, so a partial one is a 422 naming the
# field rather than a call that runs the wrong thing.
_ATOMIC: tuple[type[BaseModel], ...] = (ToolSelection, McpSelection, FaqSelection, HandoffTarget)

_generated: dict[type[BaseModel], type[BaseModel]] = {}


def _nested_model(annotation: Any) -> type[BaseModel] | None:
    """The model behind ``M`` or ``M | None``, or None for anything else.

    A ``list[M]`` deliberately does not count: lists replace wholesale, so their
    elements stay complete models.
    """
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    if get_origin(annotation) in (Union, UnionType):
        args = [a for a in get_args(annotation) if a is not type(None)]
        if len(args) == 1 and isinstance(args[0], type) and issubclass(args[0], BaseModel):
            return args[0]
    return None


def deep_partial(model: type[BaseModel], name: str) -> type[BaseModel]:
    """The same field tree as ``model``, every field optional, nested recursed into.

    Generated classes are memoized per source model, so a spec reached from two
    places is one schema in the OpenAPI document rather than two.
    """
    cached = _generated.get(model)
    if cached is not None:
        return cached

    fields: dict[str, Any] = {}
    for field_name, info in model.model_fields.items():
        # `Any` so the `| None` below is an ordinary type expression rather than
        # an operator on `FieldInfo.annotation`, which is optional-typed.
        declared: Any = info.annotation
        nested = _nested_model(declared)
        if nested is not None and not issubclass(nested, _ATOMIC):
            annotation: Any = deep_partial(nested, f"{nested.__name__}Override") | None
        else:
            annotation = declared | None
        fields[field_name] = (annotation, Field(default=None, description=info.description))

    partial = create_model(
        name,
        __config__=ConfigDict(extra="forbid"),
        __module__=__name__,
        __doc__=(
            f"A partial {model.__name__}: only the fields this call changes. "
            "Absent keeps the base value, an explicit null clears it, objects "
            "deep-merge and lists replace."
        ),
        **fields,
    )
    # Read by `override_dump` to tell a partial (walk it, keep absent keys
    # absent) from a whole value (dump it entire, because it replaces).
    partial.__talqing_partial__ = True  # type: ignore[attr-defined]
    _generated[model] = partial
    return partial


# `channel` included: on a call, the endpoint checks the MERGED config against
# the channels it runs (`resolve_call_plan`), so voice <-> video on /calls/token
# is an override like any other and text there is refused by name.
AgentOverride = deep_partial(AgentConfig, "AgentOverride")


class UpdateAgentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    config: AgentOverride = Field(  # type: ignore[valid-type]
        description="Only the fields to change."
    )


def override_dump(override: BaseModel | None) -> dict[str, Any]:
    """The fields this override actually set, ready to deep-merge onto a config.

    Generated partials are walked, so an absent nested key stays absent and the
    base's value survives. Everything else — a tool selection, a list, a scalar —
    is dumped whole, because those replace rather than merge.
    """
    if override is None:
        return {}
    out: dict[str, Any] = {}
    for name in override.model_fields_set:
        value = getattr(override, name)
        if getattr(type(value), "__talqing_partial__", False):
            out[name] = override_dump(value)
        else:
            out[name] = to_jsonable_python(value)
    return out


def deep_merge(
    base: dict[str, Any],
    override: dict[str, Any],
    model: type[BaseModel] = AgentConfig,
) -> dict[str, Any]:
    """``override`` layered onto ``base``: objects merge, everything else replaces.

    Walked against ``model`` rather than against the values, because a dumped
    override is plain JSON — an atomic reference and a free-form object like an
    inline tool's ``json_schema`` are dicts too, and merging them would keep keys
    the caller removed. Only a field `deep_partial` recursed into merges.
    """
    out = dict(base)
    for key, value in override.items():
        current = out.get(key)
        field = model.model_fields.get(key)
        nested = _nested_model(field.annotation) if field else None
        if (
            nested is not None
            and not issubclass(nested, _ATOMIC)
            and isinstance(value, dict)
            and isinstance(current, dict)
        ):
            if "provider" in value and value["provider"] != current.get("provider"):
                current = {
                    name: kept
                    for name, kept in current.items()
                    if name in nested.model_fields
                    and _nested_model(nested.model_fields[name].annotation) is not None
                }
            out[key] = deep_merge(current, value, nested)
        else:
            out[key] = value
    return out


# Where a thinking level can sit: the agent's model, its failover, and the model
# post-call analysis runs on. `pin_reasoning_efforts` writes all three.
_EFFORT_PATHS = (("llm",), ("llm", "fallback"), ("analysis", "model"))


def drop_superseded_efforts(merged: dict[str, Any], override: dict[str, Any]) -> None:
    """Forget a pinned thinking level whose model the caller just replaced.

    A config carries the effort resolved or chosen FOR ITS MODEL. An override
    (or a PATCH) that names a different model and no effort of its
    own would otherwise inherit it, and the two are not interchangeable: the
    levels a model accepts are its own. Left in place the request either runs at
    a level nobody chose or — on the ~63 models that cannot stop thinking — is
    refused for a `reasoning_effort: "none"` the caller never sent.

    So the stale value is dropped and the new model's own default applies — the
    fastest it offers, pinned by a call plan's `_pin_all` or by publish. One that
    names an effort keeps it, and one that leaves the model alone keeps the pin:
    a catalog edit still must not move a live agent's thinking level.
    """
    for path in _EFFORT_PATHS:
        asked: Any = override
        now: Any = merged
        for key in path:
            asked = asked.get(key) if isinstance(asked, dict) else None
            now = now.get(key) if isinstance(now, dict) else None
        if not isinstance(asked, dict) or not isinstance(now, dict):
            continue
        if "reasoning_effort" in asked or not {"provider", "model"} & set(asked):
            continue
        # In place, and safe: reaching here means the override carried a dict at
        # this path, so `deep_merge` built a fresh one over the base's — the only
        # case where it does not is a base with nothing there, and then `now` IS
        # `asked`, whose `reasoning_effort` we have just established is absent.
        now.pop("reasoning_effort", None)
