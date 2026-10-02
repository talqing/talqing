"""What a session's models reported their turns actually cost.

One provider bills from what it reports rather than from a rate card: OpenRouter
picks an upstream host per request and returns the exact charge in the same
`usage` object LiveKit already reads. That number is the only honest one — the
model-level rate is the cheapest endpoint's, and two consecutive requests for the
same model measured 2.4x apart — so `price_llm_usage` uses it in place of
arithmetic whenever it is present.

Reading the total off `AgentSession.llm` at the end would be simpler and wrong.
A call that hands off runs more than one LLM (`compiler.handoff` builds a fresh
one per target, a `FallbackAdapter` holds two more, an agent task may bring its
own), so the session's *current* model has only the last stretch of the call in
it. The under-report would grow with exactly the agents most likely to be
expensive. So there is one collector per session instead: every LLM built for
that session adds into it, and finalize reads the collector rather than any
model object.

It rides on `session.userdata` under a `_talqing` key — the same bus
`_talqing_participant_identity` uses, stripped before userdata is persisted —
because that is the one object a handoff target, an entered task and all three
finalize paths can each reach without a new argument between them.
"""

from __future__ import annotations

from decimal import Decimal

# The `session.userdata` key. `_talqing`-prefixed keys are platform plumbing and
# are stripped from what a session stores (`finalize_session`).
USERDATA_REPORTED_COST = "_talqing_reported_cost"


class ReportedCost:
    """Per (provider, model), what that model said this session cost so far.

    Not thread-safe and does not need to be: every write happens on the session's
    own event loop, from the LLM stream that is reading the response.
    """

    def __init__(self) -> None:
        self._totals: dict[tuple[str, str], Decimal] = {}

    def add(self, provider: str, model: str, cost: Decimal) -> None:
        key = (provider.lower(), model)
        self._totals[key] = self._totals.get(key, Decimal(0)) + cost

    def of(self, provider: str, model: str) -> Decimal | None:
        """What this model reported, or None if it never reported anything.

        None and zero are different answers and the caller has to tell them
        apart: zero is a free model, None is a model that bills from its own
        reported cost and did not report one — a bug, which pricing quarantines
        rather than billing as free.
        """
        return self._totals.get((provider.lower(), model))

    def __bool__(self) -> bool:
        return bool(self._totals)


def collector_in(userdata: object) -> ReportedCost | None:
    """The collector on a session's userdata, or None when there is none.

    None is the normal answer for anything the platform pays for itself — a
    CoPilot — which is metered nowhere.
    """
    if isinstance(userdata, dict):
        found = userdata.get(USERDATA_REPORTED_COST)
        if isinstance(found, ReportedCost):
            return found
    return None
