"""Single operation-tree validator.

One walker covers draft shape checks, publish validation, secret/arg refs,
userdata warnings, and handoff target checks. Call sites:

- ``shape_errors`` — save-time checks that need only the tree (unknown kinds,
  terminal placement, tokens that are not variables)
- ``validate_operation_tree`` — full publish / agent-attach validation
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from services.system_vars import (
    CALL_KEYS,
    CLOCK_KEYS,
    SYSTEM_KEY_LIST,
    SYSTEM_KEYS,
    SYSTEM_VARS_ROOT,
    VARS_ROOT,
    declared_vars_clause,
)

from .defs import TREE_KINDS
from .resolve import (
    publish_field_error,
    published_keys,
    template_refs,
    unknown_template_tokens,
)
from .tree import TERMINAL_KINDS, operation_errors, walk_tree

if TYPE_CHECKING:
    from services.agents.models import AgentConfig

EXTERNAL_USERDATA_KEYS = {"external_user_id"}


def normalized_tool_schema(schema: Mapping[str, Any]) -> dict[str, Any]:
    """A tool's ``json_schema`` as a whole JSON Schema object.

    A bare ``{"properties": {...}}`` is a common authoring shape, so fill in the
    object wrapper rather than silently dropping every declared parameter. Always
    returns a copy — the caller's stored schema is not ours to mutate.

    Every place that reads a tool's argument schema goes through here: the raw
    schema the model is shown, the check on the arguments it sends back, publish
    validation, and a test run. They used to each carry their own copy of this
    rule, which left them free to disagree about what a valid tool schema is.
    """
    if "type" in schema:
        return dict(schema)
    return {"type": "object", "properties": {}, **schema}


# `{{consent.notice}}` parses everywhere, because the template roots are one
# global vocabulary, but only the greeting is given a value for it. In a tool
# tree the scope has no `consent` key, so it would resolve to an empty string and
# quietly send a blank field — reject it here instead.
_CONSENT_ROOT_ERROR = (
    "{{consent.…}} only resolves in an agent's greeting, not in a tool. "
    "It carries the spoken recording notice, which is not data a tool can send."
)

# Headers whose value is a credential. A credential built out of `{{args.…}}` is
# one the *model* wrote — Vapi grade that Tier 3 and say never to trust it — so
# publish says so. A warning, not an error: a short-lived token an earlier tool
# published into args is a real, if rare, pattern, and it is the author's call.
_CREDENTIAL_HEADERS = {"authorization", "x-api-key", "api-key", "cookie"}

# What an operation may read, named in every unknown-token error so the fix is in
# the message rather than in the docs.
_TOOL_VOCABULARY = (
    "An operation can read {{args.…}}, {{tooldata.…}}, {{userdata.…}}, "
    "{{system_vars.…}}, {{vars.…}} and {{secrets.…}}"
)

# Code ops access secrets as input.secrets.NAME (not {{secrets.NAME}} templates).
_CODE_SECRET_ACCESS = re.compile(
    r"(?:input\s*\.\s*)?secrets\s*\.\s*([A-Za-z_][A-Za-z0-9_]*)"
    r"|secrets\s*\[\s*['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]\s*\]"
)


@dataclass
class ValidationResult:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def extend(self, other: ValidationResult) -> None:
        self.errors.extend(other.errors)
        self.warnings.extend(other.warnings)

    def as_dict(self) -> dict[str, list[str]]:
        return {"errors": self.errors, "warnings": self.warnings}


def _op_label(tool_name: str | None, op: Mapping[str, Any], chain: str | None = None) -> str:
    bits: list[str] = []
    if tool_name:
        bits.append(f"tool '{tool_name}'")
    if chain:
        bits.append(chain)
    bits.append(str(op.get("kind") or "operation"))
    return " / ".join(bits)


def _secret_names_from_code(source: str) -> set[str]:
    names: set[str] = set()
    for m in _CODE_SECRET_ACCESS.finditer(source):
        names.add(m.group(1) or m.group(2))
    return names


def collect_secret_names(tree: Sequence[Mapping[str, Any]] | None) -> set[str]:
    """Secret names this tree needs at runtime.

    Collects ``{{secrets.NAME}}`` templates from every operation config, and
    ``input.secrets.NAME`` / ``secrets.NAME`` property access in code operations.

    Property access is the *only* way a code op reaches a secret — its TypeScript
    has no template syntax at all — so that regex is the whole scan for a code
    op, templates included. Being text-based it also matches `secrets.X` inside a
    string literal, which is deliberate: `validate_operation_tree` checks a code
    op against the same regex, so what is collected here and what is validated at
    publish are identical by construction. `filter_secrets` raises on a name it
    cannot find, and a set collected by one rule and validated by another would
    turn that into a failure on every live call and none at publish.
    """
    names: set[str] = set()
    for op, _ in walk_tree(tree):
        cfg = op.get("config") or {}
        if op.get("kind") == "code" and isinstance(cfg, Mapping):
            source = str(cfg.get("source_ts") or "")
            compiled = cfg.get("compiled_js")
            if isinstance(compiled, str) and compiled:
                source = f"{source}\n{compiled}"
            names.update(_secret_names_from_code(source))
        else:
            for root, key in template_refs(cfg):
                if root == "secrets" and key:
                    names.add(key)
    return names


def filter_secrets(
    secrets: Mapping[str, str],
    tree: Sequence[Mapping[str, Any]] | None,
) -> dict[str, str]:
    """Return only secrets referenced by ``tree``.

    Raises ``ValueError`` if any referenced secret is missing from ``secrets`` —
    never silently drop credentials so code/HTTP ops run without them.
    """
    needed = collect_secret_names(tree)
    if not needed:
        return {}
    missing = sorted(name for name in needed if name not in secrets)
    if missing:
        if len(missing) == 1:
            raise ValueError(f"secret '{missing[0]}' does not exist")
        raise ValueError(f"secrets do not exist: {', '.join(missing)}")
    return {k: secrets[k] for k in needed}


def shape_errors(
    tree: Sequence[Mapping[str, Any]] | None,
    *,
    tool_name: str | None = None,
) -> list[str]:
    """Errors decidable from the tree alone: kinds, terminal placement, and
    tokens that are not variables.

    That last one belongs here rather than in the publish walk because it needs
    nothing else — no argument names, no secret list, no database — which is the
    boundary this function draws. It runs at save as well as at publish, so a
    `{{customer.number}}` never reaches a draft.

    Keeps its own recursion rather than using `walk_tree`: whether a terminal
    operation is misplaced is a fact about its POSITION among its siblings, and
    a walker that yields one node at a time cannot see them.
    """
    errors: list[str] = []

    def walk(ops: Sequence[Mapping[str, Any]], chain: str) -> None:
        for i, op in enumerate(ops):
            if not isinstance(op, Mapping):
                errors.append(f"{chain}[{i}]: operation must be an object")
                continue
            kind = op.get("kind")
            label = _op_label(tool_name, op, f"{chain}[{i}]")
            if kind not in TREE_KINDS:
                errors.append(f"{label}: unknown operation kind '{kind}'")
                continue
            if kind in TERMINAL_KINDS and i != len(ops) - 1:
                later = ", ".join(
                    str(n.get("kind") if isinstance(n, Mapping) else "?") for n in ops[i + 1 :]
                )
                # `end_call` gets its own fix, because "move it into a branch" is
                # the wrong advice: the work has to happen while there is still a
                # call to do it on.
                fix = (
                    "Move them before the `end_call` — the session is already "
                    "closing by the time it returns, so work placed after it runs "
                    "against a conversation that has ended."
                    if kind == "end_call"
                    else "Move later operations into branches, move the terminal "
                    "operation to the end, or wrap the rest of the flow in an else "
                    "branch. Branches do not rejoin."
                )
                errors.append(
                    f"{label}: terminal `{kind}` operation has later sibling(s) in the same "
                    f"chain: {later}. {fix}"
                )
            if kind != "code":
                # A `code` op is exempt: its config is the TypeScript source, and
                # braces there are the author's — a script is free to build a
                # mustache template for something downstream. Nothing substitutes
                # them, so nothing gets to judge them either.
                cfg = op.get("config") or {}
                for clause in unknown_template_tokens(cfg, vocabulary=_TOOL_VOCABULARY):
                    errors.append(f"{label}: {clause}")
                for root, key in template_refs(cfg):
                    if key:
                        continue
                    names = (
                        f"one of: {SYSTEM_KEY_LIST}"
                        if root == SYSTEM_VARS_ROOT
                        else "the name of the one you want"
                    )
                    errors.append(
                        f"{label}: reads {{{{{root}}}}}, which is the whole {root} bag rather "
                        f"than one variable - add a dot and {names}"
                    )
            if kind == "if":
                walk(op.get("then") or [], f"{chain}[{i}].then")
                walk(op.get("else") or [], f"{chain}[{i}].else")

    walk(tree or [], "operations")
    return errors


async def validate_operation_tree(
    tree: Sequence[Mapping[str, Any]] | None,
    *,
    arg_names: set[str],
    secret_names: set[str],
    tool_name: str | None = None,
    ctx=None,
    source_config: AgentConfig | None = None,
    allowed_external_userdata: set[str] | None = None,
    check_configs: bool = True,
    check_shape: bool = True,
    check_handoff_targets: bool = True,
) -> ValidationResult:
    """Validate one operation tree (draft publish or agent attach).

    Errors block publish. Warnings (userdata ordering) do not.
    """
    out = ValidationResult()
    ops_list = list(tree or [])
    external = set(allowed_external_userdata or EXTERNAL_USERDATA_KEYS)

    if check_shape:
        out.errors.extend(shape_errors(ops_list, tool_name=tool_name))

    # Two stores, two availability sets. `tooldata` starts empty on every tool
    # run; `userdata` starts with whatever the platform guarantees is in the
    # session. Reading an unpublished key is therefore an error in the first case
    # and only a warning in the second.
    # The calling agent's declared variables, or None when there is no agent —
    # a standalone tool publish, which cannot answer "is this declared" at all.
    declared_vars = None if source_config is None else {v.name for v in source_config.vars}

    async def walk(
        ops: Sequence[Mapping[str, Any]],
        available_tool: set[str],
        available_session: set[str],
        chain: str,
    ) -> None:
        tool_keys = set(available_tool)
        session_keys = set(available_session)
        for i, op in enumerate(ops):
            if not isinstance(op, Mapping):
                continue
            kind = op.get("kind")
            label = _op_label(tool_name, op, f"{chain}[{i}]")
            if kind not in TREE_KINDS:
                # shape_errors already recorded unknown kinds
                continue

            cfg = op.get("config") if "config" in op else {}
            if check_configs:
                # The variant covers the envelope as well as the config, so
                # `then` on a `say` or `publish_fields` on a `handoff` are
                # unknown fields here rather than rules of their own.
                for err in operation_errors(op):
                    out.errors.append(f"{label}: {err.removeprefix(f'{kind}: ')}")

            if op.get("background_execution") and op.get("publish_fields"):
                out.errors.append(f"{label}: background_execution operations cannot publish fields")
            for publish_field in op.get("publish_fields") or []:
                if err := publish_field_error(publish_field):
                    out.errors.append(f"{label}: {err}")

            # A code op reaches every root through its handler's `input` and has
            # no template syntax at all, so its config — which is the TypeScript
            # source — is not walked for tokens here or in `shape_errors`.
            source = str((cfg or {}).get("source_ts") or "") if kind == "code" else ""
            for root, key in () if kind == "code" else template_refs(cfg):
                if not key:
                    continue  # `shape_errors` reports a whole bag read as a variable
                if root == SYSTEM_VARS_ROOT:
                    # A closed key space: the platform fills it, so a name outside
                    # the catalog is a typo, not a value someone might publish
                    # later. No "reads it before anything writes it" warning
                    # either — nothing writes `system_vars`, by design.
                    if key not in SYSTEM_KEYS:
                        out.errors.append(
                            f"{label}: reads {{{{system_vars.{key}}}}}, which is not a system "
                            f"variable - the system variables are: {SYSTEM_KEY_LIST}"
                        )
                    elif source_config is not None:
                        # Only an agent publish can answer these two: a tool is
                        # workspace-level and does not know which agent will call
                        # it, so a standalone publish never asks.
                        if key in CLOCK_KEYS and source_config.timezone is None:
                            out.errors.append(
                                f"{label}: reads {{{{system_vars.{key}}}}}, but this agent has "
                                f'no timezone - set one so the agent knows what "now" means '
                                f"(for example 'Asia/Kolkata')"
                            )
                        elif key in CALL_KEYS and source_config.channel != "voice":
                            out.warnings.append(
                                f"{label}: reads {{{{system_vars.{key}}}}}, but a "
                                f"{source_config.channel} agent never takes a phone call - "
                                f"this will always be empty"
                            )
                    continue
                if root == VARS_ROOT:
                    # An OPEN key space, so an undeclared name is a warning rather
                    # than an error: the request that starts the session may carry
                    # a value nothing declared, which is what lets an agent or a
                    # tool defined inline in that same request read one.
                    #
                    # Asked only under an agent, like the two checks above and for
                    # a sharper version of the same reason: a standalone tool does
                    # not know which agent will call it, so the warning would fire
                    # on the correct spelling exactly as it fires on a typo —
                    # noise carrying no signal about either.
                    if declared_vars is not None and key not in declared_vars:
                        out.warnings.append(
                            f"{label}: reads {{{{vars.{key}}}}}, which this agent does not "
                            f"declare - it will be empty unless the request that starts the "
                            f"session supplies it. {declared_vars_clause(declared_vars)}"
                        )
                    continue
                if root == "args" and key not in arg_names:
                    out.errors.append(
                        f"{label}: {{{{args.{key}}}}} is not a declared tool parameter"
                    )
                elif root == "secrets" and key not in secret_names:
                    out.errors.append(f"{label}: secret '{key}' does not exist")
                elif root == "tooldata" and key not in tool_keys:
                    # tooldata starts empty on every run, so nothing can have put
                    # it there — this read resolves to nothing, always.
                    out.errors.append(
                        f"{label}: reads tooldata.{key}, which no operation before it in this "
                        f"tool publishes; publish it first, or read it from userdata if another "
                        f"tool sets it"
                    )
                elif root == "userdata" and key not in session_keys and key not in external:
                    out.warnings.append(
                        f"{label}: reads userdata.{key} before this tool publishes it; "
                        f"ensure session userdata contains '{key}' before this tool is called"
                    )
                elif root == "consent":
                    out.errors.append(f"{label}: {_CONSENT_ROOT_ERROR}")
            # Code uses input.secrets.NAME rather than a template.
            if kind == "code":
                for secret_key in _secret_names_from_code(source):
                    if secret_key not in secret_names:
                        out.errors.append(f"{label}: secret '{secret_key}' does not exist")

            if kind == "http":
                for name, value in ((cfg or {}).get("headers") or {}).items():
                    if str(name).lower() in _CREDENTIAL_HEADERS and any(
                        root == "args" for root, _ in template_refs(value)
                    ):
                        out.warnings.append(
                            f"{label}: the '{name}' header is built from a tool argument, which "
                            f"is a value the model wrote - put credentials in {{{{secrets.…}}}} "
                            f"so the model never sees or chooses one"
                        )

            if kind == "handoff" and check_handoff_targets:
                if (
                    not str((cfg or {}).get("target_agent_id") or "").strip()
                    and str((cfg or {}).get("agent_name") or "").strip()
                ):
                    # A team member, and the roster is unknowable here: it is
                    # decided by the call this tool ends up running on. The call
                    # plan is where that resolves, and where a name nothing
                    # defines is refused.
                    out.warnings.append(
                        f"{label}: hands off to '{(cfg or {}).get('agent_name')}', which resolves "
                        "only when this agent runs in a team that defines it"
                    )
                target = str((cfg or {}).get("target_agent_id") or "").strip()
                if target and ctx is not None:
                    pool = await ctx.tenant_pool()
                    row = await pool.fetchrow(
                        """
                        SELECT a.published_version, av.config
                        FROM agents a
                        LEFT JOIN agent_versions av
                            ON av.agent_id = a.id
                            AND av.version = a.published_version
                            AND av.tenant_id = a.tenant_id
                        WHERE a.id = $1::uuid AND a.tenant_id = $2
                        """,
                        target,
                        ctx.tenant.id,
                    )
                    if not row:
                        out.errors.append(f"{label}: handoff target agent {target} not found")
                    elif not row["published_version"]:
                        out.errors.append(
                            f"{label}: handoff target agent {target} is not published"
                        )
                    elif source_config is not None and row["config"]:
                        # Lazy: agents package imports this module for ValidationResult.
                        from services.agents.media import (
                            handoff_media_error,
                            recording_handoff_warning,
                        )
                        from services.agents.models import AgentConfig

                        target_config = AgentConfig.model_validate(row["config"])
                        if err := handoff_media_error(source_config, target_config):
                            out.errors.append(f"{label}: {err}")
                        # A warning rather than an error, because whether this
                        # edge is a problem depends on which agent answers the
                        # call — see `recording_handoff_warning`.
                        if warning := recording_handoff_warning(source_config, target_config):
                            out.warnings.append(f"{label}: {warning}")

            published = published_keys(op)
            if kind == "if":
                branch_tool = tool_keys | published["tooldata"]
                branch_session = session_keys | published["userdata"]
                await walk(op.get("then") or [], branch_tool, branch_session, f"{chain}[{i}].then")
                await walk(op.get("else") or [], branch_tool, branch_session, f"{chain}[{i}].else")
                return

            tool_keys |= published["tooldata"]
            session_keys |= published["userdata"]
            if kind in TERMINAL_KINDS:
                return  # `if` returned above; nothing can follow the others

    await walk(ops_list, set(), set(external), "operations")
    # Deduplicate while preserving order
    out.errors = list(dict.fromkeys(out.errors))
    out.warnings = list(dict.fromkeys(out.warnings))
    return out


# Back-compat alias — prefer validate_operation_tree.
async def validate_tool_tree(
    tree: list[dict],
    *,
    arg_names: set[str],
    secret_names: set[str],
    tool_name: str | None = None,
    ctx=None,
    source_config: AgentConfig | None = None,
    allowed_external_userdata: set[str] | None = None,
    check_operation_configs: bool = True,
) -> ValidationResult:
    return await validate_operation_tree(
        tree,
        arg_names=arg_names,
        secret_names=secret_names,
        tool_name=tool_name,
        ctx=ctx,
        source_config=source_config,
        allowed_external_userdata=allowed_external_userdata,
        check_configs=check_operation_configs,
    )
