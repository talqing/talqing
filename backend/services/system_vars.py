"""The two read-only template roots — `system_vars` (ours) and `vars` (theirs).

One module because there is one question — *what is filled in for this session
that nothing inside it can write* — with two answers, split by who filled it.
Splitting them would put the same name rules in two files.

**`{{system_vars.*}}` — the platform wrote this, and nothing else can.** Two
sources feed it and exactly one function knows that. A phone call supplies who
is on the other end, which of our numbers the call is on and which way it was
placed; the agent's `timezone` supplies the clock, so an agent asked *"what time
do you close?"* can work out whether it is open. Its key space is **closed**: a
name outside the catalog is a typo, refused at save.

**`{{vars.*}}` — the tenant wrote this, and nothing inside the session can.** An
agent declares its variables with descriptions and defaults; the request that
starts the session supplies values that override them. Its key space is **open**:
a request may carry a key no agent declared, which is what lets an agent defined
inline in that same request bring its own. An undeclared read is a warning, not
an error.

Both are *projections*, never stored things. The call fields are already on the
session row (``from_e164`` / ``to_e164`` / ``type``) and the clock is the clock,
so a second copy could only drift; the vars bag is merged per agent from a column
and a config. The call half is built once per call in ``workers/voice/sip.py``
and never mutated — both ride into the compiler on the runtime context,
deliberately NOT in ``session.userdata``, which is tool-writable, persisted onto
the caller's identity and replayed into their next call.

A leaf module, for the same reason as ``services.conversation_context``: the one
side that WRITES this (``services.telephony``) and the several that READ it
(``services.agents`` and ``services.tools`` at validation, ``services.catalog``
to serve the list) already import one another, and ``services.telephony``'s
package init pulls in ``services.agents``. Owned by none of them, so there is no
cycle and no lazy import hiding one. Stdlib only, and ``ValueError`` for every
failure, so each caller maps it to its own surface's error shape.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

SYSTEM_VARS_ROOT = "system_vars"
VARS_ROOT = "vars"

# The `vars` field on every request that starts a session — the call token, the
# outbound dial, the batch and the text conversation. One string because the
# field means exactly the same thing on all four, and four copies of a paragraph
# this load-bearing would drift.
SESSION_VARS_DESCRIPTION = (
    "Values for the variables the agents on this session declare, read as "
    "{{vars.name}} in a prompt, a greeting and a tool. They override each agent's "
    "declared default and reach every agent the session runs, including a handoff "
    "target. Unlike `userdata`, which describes the PERSON and is kept on their "
    "contact record, these describe THIS session and are gone with it. Strings "
    "only; an empty string is a deliberate blank, not a request for the default. "
    "The model can see them, so put credentials in a workspace secret and read "
    "{{secrets.NAME}} from a tool instead."
)

# Lowercase, because it is substituted into prose a model reads and a TTS may
# speak — "INBOUND" would be shouted or spelled out. It also matches every other
# internal spelling of the same fact (SIP dispatch metadata, conversation-ref
# metadata); `sessions.type` (SIP_INBOUND/SIP_OUTBOUND) is a different vocabulary.
SipDirection = Literal["inbound", "outbound"]


@dataclass(frozen=True)
class SystemField:
    key: str
    description: str  # one line, written for a prompt author
    example: str  # what it actually renders as, for the editor hint
    voice_only: bool  # only a `voice` agent is ever dispatched onto a SIP call


# The two halves are separate tuples because they have different rules — a call
# field is empty off a phone call, a clock field needs the agent's timezone — and
# the validators need to tell them apart. Closed on purpose: a fixed key space is
# what lets `{{system_vars.typo}}` fail at save instead of resolving to silence,
# and it keeps the editor's variable list short enough to read.
#
# Adding a field is one entry here, but only a fact the *platform* knows belongs;
# anything that changes mid-call (`sip.callStatus`) belongs in a tool that reads
# it when asked, not in a bag resolved into the prompt before the call connects.
CALL_FIELDS: tuple[SystemField, ...] = (
    SystemField(
        key="human_phone_number",
        description=(
            "The phone number of the person on the call — the caller on an inbound call, "
            "the person you called on an outbound one."
        ),
        example="+919876543210",
        voice_only=True,
    ),
    SystemField(
        key="agent_phone_number",
        description=(
            "Your number on this call — the one they dialled, or the one you called from."
        ),
        example="+912248900123",
        voice_only=True,
    ),
    SystemField(
        key="direction",
        description=(
            "Which way this call was placed: 'inbound' when someone called you, "
            "'outbound' when you called them."
        ),
        example="inbound",
        voice_only=True,
    ),
)

# Three fixed formats rather than a template language, split by who reads them:
# an ISO stamp read aloud by a TTS is unusable, and "7:26 PM" is unparseable by an
# API. Always English — the model re-speaks the date in whatever language it is
# talking, and a localized string would be one more thing to get wrong.
CLOCK_FIELDS: tuple[SystemField, ...] = (
    SystemField(
        key="now",
        description=(
            "The current time as a machine-readable ISO 8601 timestamp in the agent's "
            "timezone — send this to an API, do not read it aloud."
        ),
        example="2026-08-19T19:26:59+05:30",
        voice_only=False,
    ),
    SystemField(
        key="date",
        description="Today's date in the agent's timezone, written the way it is spoken.",
        example="Wednesday, 19 August 2026",
        voice_only=False,
    ),
    SystemField(
        key="time",
        description="The current time in the agent's timezone, written the way it is spoken.",
        example="7:26 PM",
        voice_only=False,
    ),
)

SYSTEM_FIELDS: tuple[SystemField, ...] = CALL_FIELDS + CLOCK_FIELDS

CALL_KEYS: frozenset[str] = frozenset(f.key for f in CALL_FIELDS)
CLOCK_KEYS: frozenset[str] = frozenset(f.key for f in CLOCK_FIELDS)
SYSTEM_KEYS: frozenset[str] = frozenset(f.key for f in SYSTEM_FIELDS)
# Rendered into every validation error, so the order matches the catalog above.
SYSTEM_KEY_LIST: str = ", ".join(f.key for f in SYSTEM_FIELDS)


def validate_iana_timezone(value: str) -> str:
    """The one definition of "is that a real zone", shared by the agent config
    and outbound batch calling — two features, one answer and one error string."""
    name = value.strip()
    if not name:
        raise ValueError("timezone is required")
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"unknown IANA timezone: {name!r}") from exc
    return name


def build_call_fields(
    *,
    direction: SipDirection | None,
    human_e164: str | None,
    agent_e164: str | None,
) -> dict[str, str]:
    """The phone-derived half of `{{system_vars.*}}`, from the direction and the
    two normalized numbers.

    The only place the direction → field mapping lives: on an inbound call the
    human is who rang us and the agent's number is the DID they dialled; on an
    outbound call the two swap. A prompt author never has to know that.

    An unknown number is omitted rather than emitted as ``""`` — an absent key
    and a blank one read identically in a prompt, and only one of them is honest.

    ``direction`` follows the same rule and may be ``None``, which a phone call
    never is and a media stream often is: the partner owns the dialling, so the
    direction is something we are told (SparkTG states it) or do not know, never
    something we did. Defaulting it to ``inbound`` would be a guess that reads as
    a fact, and it would be wrong on exactly the calls a partner cares most
    about — the outbound campaign they are running through us.

    Built once per call and never written to again, by anyone. That is what makes
    it safe for the runtime context to be copied on its way to each tool and to a
    handoff target without anybody having to know whether those copies are deep.
    A plain dict rather than a frozen mapping deliberately: the template resolver
    and the code-exec payload both branch on ``dict``, so a read-only proxy would
    resolve to nothing instead of failing — enforcement that costs more than the
    rule it enforces.
    """
    bag: dict[str, str] = {}
    if direction:
        bag["direction"] = direction
    if human_e164:
        bag["human_phone_number"] = human_e164
    if agent_e164:
        bag["agent_phone_number"] = agent_e164
    return bag


def build_system_vars(
    call_fields: Mapping[str, str] | None,
    timezone: str | None,
) -> dict[str, str]:
    """The whole `{{system_vars.*}}` bag, assembled from its two sources.

    The only place that knows the bag has two sources. The clock is rendered from
    ``datetime.now(ZoneInfo(timezone))`` at the moment this is called, which is
    what makes the freshness rule fall out with no second mechanism: called from
    ``compiler.compile.personalize`` it freezes at agent build, called from
    ``compiler.operations.execute_tree`` it is the time the tool ran. A prompt
    resolved once per turn would rewrite the cached prefix on every turn; a
    ``created_at`` stamped forty minutes early is wrong data in a tenant's CRM.

    An absent source contributes nothing rather than empty strings — an absent
    key and a blank one read identically in a prompt, and only one is honest. So
    a web or text session has no call fields, and an agent with no timezone has
    no clock (which validation refuses the moment a clock key is written).
    """
    bag: dict[str, str] = dict(call_fields or {})
    if timezone:
        # `%-d` / `%-I` strip the leading zero (glibc and BSD both have them):
        # "Wednesday, 9 August" and "7:05 PM", not "09" and "07".
        at = datetime.now(ZoneInfo(timezone))
        bag["now"] = at.isoformat(timespec="seconds")
        bag["date"] = at.strftime("%A, %-d %B %Y")
        bag["time"] = at.strftime("%-I:%M %p")
    return bag


# The names `_TOKEN` in `services.tools.resolve` can match, and therefore the
# only ones a bag can be read back out of. A variable called `my-key` or `2fa`
# could be written into a config or a request and would never resolve, so both
# sides refuse it at the door instead.
VAR_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def validate_var_name(name: str) -> str:
    """One rule, one error string, for both sides of `{{vars.*}}`: the agent's
    declarations and the values a request supplies."""
    if not VAR_NAME_RE.match(name):
        raise ValueError(
            f"{name!r} is not a valid variable name - use letters, digits and "
            "underscores, starting with a letter or underscore"
        )
    return name


def validate_session_vars(bag: Mapping[str, object] | None) -> dict[str, str]:
    """The `vars` a request supplies, checked.

    Open key space by design: a value whose name no agent declared is accepted,
    because an agent or a tool defined inline in this same request is free to
    read one nothing declared beforehand. So the only rules are the two a value
    could not survive without — a name the resolver could read back, and a string
    to substitute.

    An empty string is **kept**, not dropped. `{"greeting_suffix": ""}` is a
    caller deliberately blanking a default, and silently restoring the default
    instead would be the platform overruling them.
    """
    if not bag:
        return {}
    out: dict[str, str] = {}
    for name, value in bag.items():
        if not isinstance(name, str):
            raise ValueError("vars keys must be strings")
        validate_var_name(name)
        if not isinstance(value, str):
            raise ValueError(
                f"vars['{name}'] must be a string - substitution is textual, and a "
                'reference number like "007" is not the integer 7'
            )
        out[name] = value
    return out


def build_vars(
    defaults: Mapping[str, str] | None,
    session_values: Mapping[str, str] | None,
) -> dict[str, str]:
    """The `{{vars.*}}` bag: what THIS agent declared, overridden by what the
    request that started the session supplied.

    Two arguments because the two halves have different lifetimes. The session
    values are set once by the worker and reach every agent the session runs —
    the entry agent, a team member, a handoff target that was never in the plan.
    The defaults are the running agent's own, rebound at each compile, so a
    handoff target falls back to ITS defaults rather than inheriting the source
    agent's.

    A plain merge: an empty string in the request is a value, so it wins over a
    default exactly as any other string does.
    """
    return {**(defaults or {}), **(session_values or {})}


def declared_vars_clause(declared: Iterable[str]) -> str:
    """The sentence that names an agent's declared variables, for the warning a
    `{{vars.x}}` nobody declared earns. Shared by the prose walk and the
    operation-tree walk so one typo is explained the same way in both."""
    names = sorted(declared)
    return (
        f"This agent declares: {', '.join(names)}."
        if names
        else "This agent declares no variables."
    )
