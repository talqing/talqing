"""Who is on a stream call: the direction, the person, and the business.

On three of the five platforms the numbers are metadata the partner typed into
their own markup, so a placeholder nobody filled in (`{{From}}`, `CALLER_NUMBER`)
arrives looking like a number — and as a conversation key, that one string would
give every caller on the connection the same history and userdata. A number is
therefore used only once it normalizes; otherwise the call is answered as an
unidentified caller, never refused, since a Twilio stream may carry no number.

Resolved once, in the gateway, so the dispatch metadata, a refused call's row and
the worker all read the same answer.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

from services.streams.dialects import Started, StreamDialect
from services.telephony.e164 import normalize_e164

CallDirection = Literal["inbound", "outbound"]

# `direction` in the partner's metadata, for the platforms whose protocol does not
# state one. Twilio's own spellings are accepted so `{{Direction}}` in a TwiML Bin
# works as it is.
_METADATA_DIRECTIONS: Mapping[str, CallDirection] = {
    "inbound": "inbound",
    "outbound": "outbound",
    "outbound-api": "outbound",
    "outbound-dial": "outbound",
}

# How much of an unusable value the call's trace keeps. Partner-controlled text,
# and the start of it is all anyone needs to recognise a placeholder.
_UNUSABLE_VALUE_CHARS = 64


@dataclass(frozen=True, slots=True)
class CallParties:
    """The call's numbers, normalized, and which of them is the person."""

    # None where neither the protocol nor the metadata said.
    direction: CallDirection | None
    # The person on the call: who rang in, or who was rung. With an unknown
    # direction this is `from`, since a stream is almost always a call someone
    # made to the business.
    human_e164: str | None
    # The business's own number on the call.
    agent_e164: str | None
    # Wire field → the value that was not a phone number, for the call's trace.
    unusable_numbers: Mapping[str, str] = field(default_factory=dict)

    @property
    def from_e164(self) -> str | None:
        return self.agent_e164 if self.direction == "outbound" else self.human_e164

    @property
    def to_e164(self) -> str | None:
        return self.human_e164 if self.direction == "outbound" else self.agent_e164


def resolve_parties(started: Started, dialect: StreamDialect) -> CallParties:
    """Orient and normalize the numbers a stream call started with."""
    # The protocol's own direction wins; the metadata key is for the platforms
    # that have none.
    direction = _METADATA_DIRECTIONS.get(
        (started.direction or started.params.get("direction", "")).strip().lower()
    )
    unusable: dict[str, str] = {}

    def normalize(wire_field: str, raw: str | None, *, did: str | None) -> str | None:
        if not raw:
            return None
        value = raw.strip()
        if dialect.bare_numbers_are_international and value.isdigit():
            value = f"+{value}"
        try:
            # Loose, as inbound caller ID is on a SIP call: a number our copy of
            # the numbering plan has not caught up with is still that caller.
            return normalize_e164(value, did=did, strict=False)
        except ValueError:
            unusable[wire_field] = raw[:_UNUSABLE_VALUE_CHARS]
            return None

    # The business number first: a national-format caller number is national to
    # the country of the number it reached.
    if direction == "outbound":
        agent = normalize("from", started.from_number, did=None)
        human = normalize("to", started.to_number, did=agent)
    else:
        agent = normalize("to", started.to_number, did=None)
        human = normalize("from", started.from_number, did=agent)
    return CallParties(
        direction=direction,
        human_e164=human,
        agent_e164=agent,
        unusable_numbers=unusable,
    )
