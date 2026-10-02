"""Template resolve, publish-field helpers, and dotted path reads."""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from typing import Any

from services.secrets.resolve import MissingSecretError
from services.system_vars import SYSTEM_KEY_LIST, SYSTEM_VARS_ROOT, VARS_ROOT

from .defs import PUBLISH_STORES, PublishStore

# The user-facing template vocabulary. `args`, `tooldata`, `userdata` and
# `secrets` are all *sources of data*; `consent` is the odd one — a single
# platform-provided string that only the greeting may read
# (`compiler.compile.personalize` supplies it, `services.agents.validate` rejects
# it anywhere else). If a second such string ever appears, give them a shared
# root rather than one root per feature.
#
# `tooldata` and `userdata` are the two stores an operation can publish into:
# `tooldata` lives for one tool run, `userdata` for the session. Only a tool's
# operation tree supplies `tooldata` — a prompt or a greeting must reject it.
#
# `system_vars` and `vars` are the two roots nothing inside a session can write.
# They differ in who filled them, and therefore in whether the key space is
# closed. `system_vars` is the platform's, from two sources that
# `services.system_vars` is the one module to know about — a phone call's own
# numbers and direction, and the agent's timezone — so its key space is closed and
# every validator rejects a name the catalog does not have; the call half is empty
# on web and text sessions. `vars` is the tenant's, declared on the agent and
# valued by the request that starts the session, so its key space is open and an
# undeclared name is only a warning.
#
# Adding a root here makes its tokens parse EVERYWHERE templates resolve, so
# every scope that does not supply it must reject it rather than let
# `_resolve_path` substitute an empty string. A token may also legally carry no
# key path at all (`{{userdata}}`, `{{vars}}`), which resolves to the whole
# bag — the validators reject that explicitly rather than let a stringified dict
# reach a model or a tenant's API.
_ROOTS = ("args", "tooldata", "userdata", SYSTEM_VARS_ROOT, VARS_ROOT, "secrets", "consent")
# {{ root.a.b }}  →  group(1)=root, group(2)=".a.b"
#
# The roots become one alternation, so a root that is a PREFIX of another must
# come first or the shorter one swallows its head. `system_vars` and `vars` are
# safe as written — neither is a prefix of the other, and each branch is anchored
# right after `{{\s*` and followed by `((?:\.[…])*)\s*\}\}`, so a partial match
# cannot complete — but the next root with a shared prefix will not be so lucky.
_TOKEN = re.compile(r"\{\{\s*(" + "|".join(_ROOTS) + r")((?:\.[A-Za-z0-9_]+)*)\s*\}\}")
Scope = Mapping[str, Any]


def _resolve_path(root: str, dotted: str, scope: Scope) -> Any:
    obj: Any = scope.get(root, {}) or {}
    segs = [s for s in dotted.split(".") if s]
    if root == "secrets":
        # Missing secret keys must raise — empty substitution hides broken config.
        if not segs:
            raise MissingSecretError("")
        if not isinstance(obj, dict) or segs[0] not in obj:
            raise MissingSecretError(segs[0])
        obj = obj[segs[0]]
        for i, seg in enumerate(segs[1:], start=1):
            if isinstance(obj, dict):
                if seg not in obj:
                    raise MissingSecretError(".".join(segs[: i + 1]))
                obj = obj[seg]
            else:
                obj = getattr(obj, seg, None)
                if obj is None:
                    raise MissingSecretError(".".join(segs[: i + 1]))
        return obj
    for seg in segs:
        if isinstance(obj, dict):
            obj = obj.get(seg)
        else:
            obj = getattr(obj, seg, None)
        if obj is None:
            return None
    return obj


def resolve(value: Any, scope: Scope) -> Any:
    """Substitute every {{root.path}} in `value` (recursing into dicts/lists).

    If a string is EXACTLY one token, the resolved (typed) value is returned —
    so a JSON body field can carry a number/object. Otherwise tokens are
    stringified into the surrounding text.

    Missing ``{{secrets.NAME}}`` references raise ``MissingSecretError``.
    Missing args/userdata still resolve to empty (optional session state).
    """
    if isinstance(value, str):
        m = _TOKEN.fullmatch(value.strip())
        if m:
            return _resolve_path(m.group(1), m.group(2), scope)

        def _sub(mt: re.Match[str]) -> str:
            v = _resolve_path(mt.group(1), mt.group(2), scope)
            return "" if v is None else str(v)

        return _TOKEN.sub(_sub, value)
    if isinstance(value, dict):
        return {k: resolve(v, scope) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve(v, scope) for v in value]
    return value


def template_refs(value: Any) -> Iterator[tuple[str, str]]:
    """Yield (root, first_key) for every template token found in `value`."""
    if isinstance(value, str):
        for m in _TOKEN.finditer(value):
            segs = [s for s in m.group(2).split(".") if s]
            yield m.group(1), (segs[0] if segs else "")
    elif isinstance(value, dict):
        for v in value.values():
            yield from template_refs(v)
    elif isinstance(value, list):
        for v in value:
            yield from template_refs(v)


# Anything shaped like one of our tokens. `_TOKEN` matches only the known roots,
# which is right for resolving and exactly wrong for validating: a token whose
# root is a typo simply does not match, so nothing ever complains and the literal
# braces are handed to the model on every call. (A live agent shipped
# `{{sip.human_phone_number}}` this way and read it out as text for weeks.)
#
# The dotted part is optional, which is what catches the shape both competitors
# use — a bare `{{name}}` is in every prompt anyone pastes in from Vapi or
# ElevenLabs, and until it matched here it was silently forwarded to the model.
# The cost is that there is no escape syntax: prose containing `{{anything}}`
# cannot be saved. A `code` operation's TypeScript is the one exemption, because
# braces there are the author's and never ours.
_ANY_TOKEN = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)((?:\.[A-Za-z0-9_]+)*)\s*\}\}")

# The two spellings this root has already had: `sipdata` before the phone fields
# and the clock were folded together, and `system` before it was paired with the
# tenant's own `vars` and had to say whose variables it holds. Removed rather
# than aliased — two spellings of one thing outlive the migration they were meant
# to ease — so each rename is taught by the error instead, read at exactly the
# moment it is needed.
_RENAMED_ROOTS = {"sipdata": SYSTEM_VARS_ROOT, "system": SYSTEM_VARS_ROOT}


def unknown_template_tokens(value: Any, *, vocabulary: str) -> Iterator[str]:
    """Yield one clause per `{{token}}` whose root is not a template root.

    Each reads as the tail of a sentence whose subject is the field holding it —
    "the greeting …", "tool 'book' / operations[0] / http …", "slack: …" — so one
    wrong token is explained the same way wherever it was written. `vocabulary`
    is the only thing that differs between the callers: the sentence naming which
    roots the surface asking actually supplies.

    Recurses into dicts and lists so an operation's whole config can be handed in,
    exactly like `template_refs`. Dict *keys* are not walked, because `resolve`
    does not substitute them either.
    """
    if isinstance(value, str):
        for m in _ANY_TOKEN.finditer(value):
            root, key = m.group(1), [s for s in m.group(2).split(".") if s]
            if root in _ROOTS:
                continue
            if renamed := _RENAMED_ROOTS.get(root):
                fix = (
                    f"write {{{{{renamed}.{key[0]}}}}} instead"
                    if key
                    else f"the {renamed} variables are: {SYSTEM_KEY_LIST}"
                )
                yield f"reads {m.group(0)}, which was renamed - {fix}"
            else:
                yield (
                    f"reads {m.group(0)}, which is not a variable - it would be sent exactly "
                    f"as written, braces and all. {vocabulary}"
                )
    elif isinstance(value, dict):
        for v in value.values():
            yield from unknown_template_tokens(v, vocabulary=vocabulary)
    elif isinstance(value, list):
        for v in value:
            yield from unknown_template_tokens(v, vocabulary=vocabulary)


_PUBLISH_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def publish_field_path_and_key(field: Mapping[str, Any]) -> tuple[str, str]:
    """Return the response path and the key it publishes under.

    {"path": "path.to.value", "store": "tooldata"} publishes to tooldata.value —
    readable by later operations in the same tool run and gone afterwards.
    {"path": "path.to.value", "key": "explicit_key", "store": "userdata"}
    publishes to userdata.explicit_key, which the session keeps.

    Callers must validate first via `publish_field_error` (or publish-time
    `validate_operation_tree`). Invalid shapes raise — soft empty returns hid corrupt trees.
    """
    path_raw = field.get("path")
    if not isinstance(path_raw, str):
        raise ValueError("publish field path must be a string")
    path = path_raw.strip()
    if not path:
        raise ValueError("publish field path is required")
    key_raw = field.get("key")
    if key_raw is None:
        key = ""
    elif isinstance(key_raw, str):
        key = key_raw.strip()
    else:
        raise ValueError("publish field key must be a string when set")
    return path, key or path.split(".")[-1]


_STORE_CHOICES = " or ".join(f"'{store}'" for store in PUBLISH_STORES)


def publish_field_error(field: object) -> str | None:
    if not isinstance(field, Mapping):
        return "publish_fields entries must be objects with path, optional key, and store"
    unknown = [k for k in field if k not in {"path", "key", "store"}]
    if unknown:
        return f"publish field has unknown field '{unknown[0]}'"
    path = field.get("path")
    if not isinstance(path, str) or not path.strip():
        return "publish field path is required"
    key = field.get("key")
    if key is not None and (not isinstance(key, str) or not _PUBLISH_KEY.match(key)):
        return "publish field key must be a valid identifier"
    if "store" not in field:
        return f"publish field store is required — {_STORE_CHOICES}"
    if field.get("store") not in PUBLISH_STORES:
        return f"publish field store must be {_STORE_CHOICES}"
    try:
        _, resolved_key = publish_field_path_and_key(field)
    except ValueError as e:
        return str(e)
    if not resolved_key:
        return "publish field path must include a key segment or explicit key"
    return None


def publish_field_store(field: Mapping[str, Any]) -> PublishStore:
    """Return which store a validated publish field writes into."""
    store = field.get("store")
    if store not in PUBLISH_STORES:
        raise ValueError(f"publish field store must be {_STORE_CHOICES}")
    return store  # type: ignore[return-value]


def published_keys(op: Mapping[str, Any]) -> dict[PublishStore, set[str]]:
    """The keys an operation writes, grouped by the store each lands in.

    A background operation never waits for a result, so it publishes nothing.
    Malformed entries are skipped rather than raised on: the only caller is the
    validator, which reports each one through `publish_field_error` in the same
    pass — a raise here would replace that list with a single exception.
    """
    out: dict[PublishStore, set[str]] = {store: set() for store in PUBLISH_STORES}
    if op.get("background_execution"):
        return out
    kind = op.get("kind")
    if kind == "set_variable":
        cfg = op.get("config") or {}
        key_raw = cfg.get("key")
        key = key_raw.strip() if isinstance(key_raw, str) else ""
        store = cfg.get("store")
        if key and store in PUBLISH_STORES:
            out[store].add(key)
        return out
    # No kind check: only the three data operations have `publish_fields` at all
    # (see the variants in `defs.py`), so on the other eight this loop is empty.
    for field in op.get("publish_fields") or []:
        if publish_field_error(field) is not None:
            continue
        _, key = publish_field_path_and_key(field)
        if key:
            out[publish_field_store(field)].add(key)
    return out


# What `dotted_get` answers for a path that is not there, which is a different
# answer from a path whose value is JSON `null`.
MISSING: Any = object()


def dotted_get(obj: Any, path: str) -> Any:
    """Read a dotted path out of a parsed-JSON object, or `MISSING` if it is absent.

    A `null` anywhere on the path answers `None`, like optional chaining:
    `customer.name` against `{"customer": null}` is an empty answer, not a typo.
    """
    cur = obj
    for seg in [s for s in path.split(".") if s]:
        if cur is None:
            return None
        if isinstance(cur, dict) and seg in cur:
            cur = cur[seg]
        elif isinstance(cur, list) and seg.isdigit() and int(seg) < len(cur):
            cur = cur[int(seg)]
        else:
            return MISSING
    return cur
