"""What an ``AgentSession`` actually spent, as rows the catalog can price.

LiveKit reports per-model totals under the names its plugins use for the
*transport*; the catalog prices the names it knows the models by, and the
priority lane is not on the wire at all. Turning one into the other is the same
work on every path that bills an LLM — a text chat's window, a task run —
so it lives here rather than beside either of them.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from services.catalog import get_catalog

from .models import LLMUsage
from .reported_cost import ReportedCost

if TYPE_CHECKING:
    from services.agents import LLMSpec


def llm_usage_lines(
    model_usage: Sequence[Any],
    spec: LLMSpec | None,
    reported_cost: ReportedCost | None = None,
) -> list[LLMUsage]:
    """One ``LLMUsage`` row per model a session actually ran.

    ``model_usage`` is ``session.usage.model_usage``, read by duck-typing
    because the shape belongs to the framework. ``spec`` is the LLM the config
    named, and it is what makes those totals priceable:

    - **Canonicalization.** Passing the configured pair narrows the match
      (``Catalog.canonicalize_usage``), so a session that failed over bills the
      fallback as the fallback rather than as the primary.
    - **The priority lane.** LiveKit's totals carry no tier, and two of three
      providers would report it while the third never does — so the lane is taken
      from what the agent ASKED for, per model, which is why a failover does not
      inherit the primary's lane.

    A model the catalog does not recognize is dropped rather than guessed at: the
    session's real spend is not ours to invent, and pricing would only raise on it
    a moment later.

    A session that reported nothing at all still gets one zero-token line for the
    model it was configured with — so the priced breakdown says which model was
    on the session rather than showing an empty list, and a configured model the
    catalog has since dropped still quarantines the row instead of pricing an
    absence at $0.

    ``reported_cost`` is the session's collector, for the providers that price a
    request themselves. It is keyed on the same canonical names as the rows, so a
    session that failed over gets each model's own charge on its own line.
    """
    catalog = get_catalog()
    models = [s for s in ((spec, spec.fallback) if spec is not None else ()) if s is not None]
    configured = [(s.provider, s.model) for s in models]
    priority: dict[tuple[str, str], bool] = {
        (entry.provider, entry.model): s.priority
        for s in models
        for entry in [catalog.entry("llm", s.provider, s.model)]
        if entry is not None
    }
    out: list[LLMUsage] = []
    for item in model_usage:
        if getattr(item, "type", "") != "llm_usage":
            continue
        provider = str(getattr(item, "provider", "") or "")
        model = str(getattr(item, "model", "") or "")
        canonical = (
            catalog.canonicalize_usage("llm", provider, model, configured)
            if configured
            else catalog.canonicalize("llm", provider, model)
        )
        if canonical is None:
            continue
        provider, model = canonical
        out.append(
            LLMUsage(
                provider=provider,
                model=model,
                input_tokens=int(getattr(item, "input_tokens", 0) or 0),
                input_cached_tokens=int(getattr(item, "input_cached_tokens", 0) or 0),
                input_cache_write_tokens=int(getattr(item, "input_cache_creation_tokens", 0) or 0),
                output_tokens=int(getattr(item, "output_tokens", 0) or 0),
                reported_cost=reported_cost.of(provider, model) if reported_cost else None,
                priority=priority.get((provider, model), False),
            )
        )
    if not out and spec is not None:
        provider, model = catalog.canonicalize("llm", spec.provider, spec.model) or (
            spec.provider,
            spec.model,
        )
        out.append(
            LLMUsage(
                provider=provider, model=model, priority=priority.get((provider, model), False)
            )
        )
    return out
