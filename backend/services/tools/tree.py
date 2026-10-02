"""Walking, validating and reading an operation tree.

The *shape* of an operation lives in `defs.py` — eleven typed variants and the
discriminated union over them. This module is what the platform does with a tree
once it has one: walk it (`walk_tree`), validate a stored node against its
variant (`operation_errors`), and answer the questions that are about a tree
rather than about a node — which kinds it contains (`tree_kinds`), whether it can
say anything at all (`tree_is_silent`, whose answer `llm_response` applies).
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping, Sequence
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from .defs import OPERATION_ADAPTER, Operation, ToolDefinition

if TYPE_CHECKING:
    from services.agents.models import AgentConfig

# Kinds after which the chain cannot continue. `if` branches out and branches do
# not rejoin; `handoff` gives the conversation to another agent; `end_call` closes
# the session, so an operation authored after it would run against a session that
# is already draining; `transfer` hands the caller to a person and our session
# ends behind them. Enforced at save (`shape_errors`) and, for tool versions
# published before the rule, at runtime (`compiler/operations.py::_run`).
#
# Behaviour rather than shape — nothing in the type of a `handoff` says what may
# follow it — which is why this is the one kind table still written out by hand.
TERMINAL_KINDS = ("if", "handoff", "end_call", "transfer")


# ─────────────────────────────── walking a tree ──────────────────────────────


def walk_tree(
    tree: Sequence[Operation] | None, path: str = "operations"
) -> Iterator[tuple[Operation, str]]:
    """Every operation in ``tree``, branches included, in document order.

    Yields each node with its positional path (`operations[2].else[0]`). An
    operation has no id, so where it sits is what names it — the same addressing
    a trace step and a validation error use.
    """
    for index, op in enumerate(tree or []):
        node = f"{path}[{index}]"
        yield op, node
        yield from walk_tree(op.get("then"), f"{node}.then")
        yield from walk_tree(op.get("else"), f"{node}.else")


def source_only(tree: Sequence[Operation] | None) -> list[Operation]:
    """A tree as a DRAFT holds it: source only, no build output.

    `compiled_js` is what publishing adds, so a draft that carried it would claim
    an artifact the author's next edit invalidates. Both places that put a
    published tree back into a draft — rolling a tool version back, and the tool
    an inline agent materializes — go through here.
    """
    out: list[Operation] = []
    for op in tree or []:
        copy: Operation = {**op}
        if copy["kind"] == "code":
            copy["config"] = {
                key: value
                for key, value in (copy.get("config") or {}).items()
                if key != "compiled_js"
            }
        if "then" in copy:
            copy["then"] = source_only(copy["then"])
        if "else" in copy:
            copy["else"] = source_only(copy["else"])
        out.append(copy)
    return out


# ────────────────────────────── validating a node ────────────────────────────

# pydantic's two ways of saying "no variant has this `kind`".
_UNKNOWN_KIND = {"union_tag_invalid", "union_tag_not_found"}


def operation_errors(op: Mapping[str, Any]) -> list[str]:
    """One stored operation, validated against the variant its ``kind`` names.

    An authored tree is validated by the same union at the request, which is the
    door most trees come through. This is the other one: a published definition
    comes back out of JSONB as plain dicts, and agent publish re-validates it
    there before a live call can run it.

    Renders pydantic's errors as `kind: where: what`, the shape every other tool
    error takes, so a caller parses one rendering rather than two.
    """
    kind = op.get("kind")
    node = dict(op)
    if kind == "if":
        # The caller walks into the branches and validates each child under its
        # own path; validating them here as well would report every one twice.
        node["then"] = []
        node["else"] = []
    try:
        OPERATION_ADAPTER.validate_python(node)
    except ValidationError as exc:
        return [_error_line(kind, detail) for detail in exc.errors()]
    return []


def _error_line(kind: object, detail: Mapping[str, Any]) -> str:
    # `loc` starts with the variant tag pydantic matched, which is the kind this
    # line already names.
    where = ".".join(str(part) for part in detail["loc"][1:])
    if not where and detail["type"] in _UNKNOWN_KIND:
        return f"unknown operation kind '{kind}'"
    if detail["type"] == "extra_forbidden":
        return f"{kind}: unknown field '{where}'"
    if detail["type"] == "missing":
        return f"{kind}: {where} is required"
    message = detail["msg"].removeprefix("Value error, ")
    # A rule about the config as a whole ("exactly one target") names no field of
    # it, so `config:` in front would be noise.
    if not where or where == "config":
        return f"{kind}: {message}"
    return f"{kind}: {where}: {message}"


# ──────────────────────────── reading a whole tree ───────────────────────────


def tree_is_silent(tree: Sequence[Operation] | None) -> bool:
    """True when no operation in ``tree`` can contribute a response to the LLM.

    Only the three data operations carry `silent` at all, so the rule is that no
    node has one that is false: the other eight kinds never hand the LLM anything
    to begin with, and an `if` is a container whose branches this walk covers.

    A `background_execution` operation counts as silent whatever its own `silent`
    holds, because it *cannot* contribute one: `_spawn_background_op` runs it
    against a fresh `ToolRunResult` that is thrown away
    (`compiler/operations.py`). Derived here rather than forced onto the
    operation at save, so unticking Background restores the author's own choice
    instead of leaving a tick they never made.

    Such a tool is silent whether or not its author ticked the box, because the
    alternative is worse: the model would be handed the bare string ``"Done."``
    and would improvise a sentence on top of whatever the tree already said. A
    tool that is only ``say "Your appointment is confirmed"`` is the common case,
    and the agent saying something else after it is a defect, not a choice.

    Derived rather than defaulted — there is nothing for a builder to untick by
    accident. A builder who wants a closing line adds a `generate_reply`
    operation, which states what to say, is visible in the tree, and costs the
    same turn.
    """
    return all(
        op.get("silent") is not False or bool(op.get("background_execution"))
        for op, _ in walk_tree(tree)
    )


def tree_kinds(tree: Sequence[Operation] | None) -> set[str]:
    """Every operation kind anywhere in ``tree``, branches included.

    For the publish rules that are about a tool as a whole rather than about one
    operation's config — "a tool containing a `transfer` cannot be a lifecycle
    hook" is a fact about the tree, not about the node.
    """
    return {op["kind"] for op, _ in walk_tree(tree)}


def llm_response(
    *,
    silent: bool,
    end_call: bool,
    responses: Sequence[Mapping[str, Any]],
) -> str | None:
    """The literal string the agent's model is handed after a tool run, or None
    when it is told nothing at all.

    The single definition of that rule. The runtime turns None into
    ``StopResponse`` (`compiler/tools.py`) and the dashboard's test panel reports
    it as a null ``llm_response`` (`services/tools/run.py`); those two used to
    carry their own copy of this and were free to disagree about what the model
    would actually see.

    - ``silent`` — the author's flag OR the silence derived from the tree
      (`tree_is_silent`). Nothing this tool did is the model's business.
    - ``end_call`` — the tree hung up. Whatever it collected, there is nobody
      left to narrate it to.
    - otherwise the ordered operation outputs, or ``"Done."`` when a tool that
      *could* have returned something happened not to.
    """
    if silent or end_call:
        return None
    if responses:
        return json.dumps({"responses": list(responses)}, default=str)
    return "Done."


# Full tree validation lives in services.tools.validate.validate_operation_tree.


AGENT_HOOKS = ("on_enter", "on_exit", "on_user_turn_completed")


def hook_trees_from_defs(
    cfg: AgentConfig, defs: Sequence[ToolDefinition]
) -> dict[str, list[Operation]]:
    """Map loaded tool_version definitions onto {hook name: operation tree} for
    the agent's lifecycle hooks (on_enter/on_exit; on_user_turn_completed
    runs before every LLM reply — spoken turns on voice/video, each message on text).

    `defs` are full ToolDefinitions (id + operations), not bare operation nodes.
    A hook wired to an inline tool matches by name — an inline definition has no
    id to match on, and the name is unique across an agent's tools anyway.
    """
    by_id: dict[str, ToolDefinition] = {d["id"]: d for d in defs if d["id"]}
    by_name: dict[str, ToolDefinition] = {d["name"]: d for d in defs if not d["id"]}
    out: dict[str, list[Operation]] = {}
    for hook in AGENT_HOOKS:
        sel = getattr(cfg, hook, None)
        if not sel:
            continue
        d = by_id.get(sel.tool_id) if sel.tool_id else by_name.get(sel.tool.name)
        if d is None:
            continue
        out[hook] = list(d["operations"])
    return out
