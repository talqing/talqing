"""Running post-call analysis and persisting what it found.

Runs in-process at the end of a call, between sealing the session and pricing
it. That ordering is load-bearing: the analysis LLM row has to be in
`llm_usage` before `bill_session` reads it, which is what lets analysis be
metered for real without a second pricing pass.

Nothing here is allowed to raise into the finalize path. A call must not end
badly because a summary did.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping

import db
from services.agents.models import AgentConfig, AnalysisSpec, LLMModelSpec
from services.billing.pricing import is_failed_close_reason
from services.byok import load_provider_keys
from services.catalog import get_catalog
from services.llm_client import LLMTarget, chat, extract_json
from services.user import Tenant

from . import prompt as analysis_prompt
from .models import (
    SKIP_CALL_FAILED,
    SKIP_PRODUCES_NOTHING,
    SKIP_TOO_SHORT,
    AnalysisOutcome,
)

logger = logging.getLogger("talqing.analysis")

# Long enough for a real model on a long transcript, short enough not to pin a
# worker slot. Vapi's 5s default is why their summaries come back empty; the
# other end of the trade is that this is the worst case a `session.completed`
# consumer waits, because the payload carries the analysis.
TIMEOUT_S = 30.0

# A call with fewer caller turns than this had no conversation to analyse —
# a wrong number, a hangup on the greeting, a voicemail beep. Fixed platform
# policy rather than a per-agent knob until somebody asks for one.
MIN_USER_TURNS = 2


def resolve_model(cfg: AgentConfig, spec: AnalysisSpec | None = None) -> LLMModelSpec | None:
    """The model spec to analyse this agent's calls with, or None.

    The default is **the agent's own LLM**, not a platform-wide model. That is
    the whole rule, and it exists to protect one property: analysing a call must
    never require a provider key the agent did not already need. A fixed default
    would mean an OpenAI-only workspace could not publish an agent without also
    bringing an xAI key, for a feature that is on by default.

    A realtime agent has no `llm` — one speech-to-speech model does all three
    jobs, and it cannot read a transcript over Chat Completions. It falls back to
    the first catalog LLM from the *same provider*, which keeps the key property
    intact. `spec` overrides the agent's own analysis spec, for previewing a
    definition that has not been saved.

    A whole spec rather than a pair, so the OpenRouter hosts travel with it: an
    agent restricted to some hosts has its analysis restricted to them too,
    which is exactly what a region restriction must not escape.
    """
    spec = spec if spec is not None else cfg.analysis
    if spec.model is not None:
        return spec.model
    if cfg.llm is not None:
        return cfg.llm
    if cfg.realtime is not None:
        provider = cfg.realtime.provider.strip().lower()
        for entry in get_catalog().llm:
            if entry.provider == provider:
                return LLMModelSpec(provider=provider, model=entry.model)
    return None


def skip_reason_for(
    spec: AnalysisSpec,
    *,
    transcript: list[dict[str, object]],
    close_reason: str | None,
) -> str | None:
    """Why this call is not worth an LLM request, or None to go ahead."""
    if spec.produces_nothing():
        return SKIP_PRODUCES_NOTHING
    if is_failed_close_reason(close_reason):
        return SKIP_CALL_FAILED
    if analysis_prompt.user_turn_count(transcript) < MIN_USER_TURNS:
        return SKIP_TOO_SHORT
    return None


async def analyze(
    tenant: Tenant,
    spec: AnalysisSpec,
    *,
    model: LLMModelSpec | None,
    transcript: list[dict[str, object]],
    system_prompt: str,
    close_reason: str | None,
    transfer: Mapping[str, object] | None = None,
    cache_key: str | None = None,
) -> AnalysisOutcome:
    """Analyse one finished call. Never raises.

    ``model`` is ``resolve_model``'s answer — passed in rather than looked up so
    this stays pure with respect to both the database and the agent definition,
    which is what lets the preview endpoint run an unsaved spec and write
    nothing.
    """
    if not spec.enabled:
        return AnalysisOutcome(status="none")

    skip = skip_reason_for(spec, transcript=transcript, close_reason=close_reason)
    if skip is not None:
        return AnalysisOutcome(status="skipped", skip_reason=skip)

    if model is None:
        # Nothing to analyse with: no explicit model, no LLM, no realtime model.
        # Validation rejects this on save, so reaching here means a config that
        # predates the rule — fail visibly rather than silently doing nothing.
        logger.error("analysis has no model to run on (tenant %s)", tenant.id)
        return AnalysisOutcome(status="failed")

    provider, model_id = model.provider.strip().lower(), model.model
    try:
        keys = await load_provider_keys(tenant)
        target = LLMTarget.resolve(provider, model_id, keys.get(provider, ""), hosts=model.hosts)
    except Exception:
        logger.exception("analysis cannot reach %s/%s for tenant %s", provider, model_id, tenant.id)
        return AnalysisOutcome(status="failed", provider=provider, model=model_id)

    messages = analysis_prompt.build_messages(
        spec,
        transcript=transcript,
        system_prompt=system_prompt,
        close_reason=close_reason,
        transfer=transfer,
    )
    try:
        result = await asyncio.wait_for(
            chat(
                target,
                messages,
                json_mode=True,
                timeout=TIMEOUT_S,
                cache_key=cache_key,
                # Only when analysis names its own model. An agent that raised
                # the effort on its *live* model raised it for turn quality on a
                # call; inheriting that here would quietly re-price a feature the
                # author never touched.
                reasoning_effort=spec.model.reasoning_effort if spec.model else None,
            ),
            timeout=TIMEOUT_S,
        )
    except TimeoutError:
        logger.warning("analysis timed out after %.0fs (%s/%s)", TIMEOUT_S, provider, model_id)
        return AnalysisOutcome(status="failed", provider=provider, model=model_id)
    except Exception:
        logger.exception("analysis call failed (%s/%s)", provider, model_id)
        return AnalysisOutcome(status="failed", provider=provider, model=model_id)

    # Past this point the tokens were spent, so `usage` rides along even when
    # the reply turns out to be unparseable — we pay for what the provider ran,
    # not for what we managed to read.
    try:
        parsed = analysis_prompt.parse_reply(spec, extract_json(result.text))
    except Exception:
        logger.warning("analysis reply was not JSON (%s/%s)", provider, model_id, exc_info=True)
        return AnalysisOutcome(
            status="failed", provider=provider, model=model_id, usage=result.usage
        )

    return AnalysisOutcome(
        status="completed",
        summary=parsed["summary"],
        outcome=parsed["outcome"],
        outcome_rationale=parsed["outcome_rationale"],
        fields=parsed["fields"],
        provider=provider,
        model=model_id,
        usage=result.usage,
    )


async def persist(tenant: Tenant, session_id: str, outcome: AnalysisOutcome) -> None:
    """Write one analysis result onto its session, usage included.

    The usage row is inserted before the session row is updated, so a failure
    between the two leaves a session that still says 'pending' — the safe way
    round, since the alternative is a session that claims to be analysed with
    no cost attached to it.
    """
    pool = await db.tenant_pool(tenant)

    if outcome.usage is not None and outcome.provider and outcome.model:
        # Re-analysis (backfill) intentionally adds a row rather than replacing
        # one: both calls really happened and both cost real money.
        #
        # No `priority`: the column defaults to false and agent validation refuses
        # the priority lane on the analysis model outright — it runs after the
        # caller has hung up, so the premium would buy nobody any less waiting.
        #
        # `reported_cost` is not optional plumbing: a provider that prices its
        # own requests is billed from that figure and quarantines the session
        # when a metered row arrives without one. Analysis inherits the agent's
        # own model unless it names another, so dropping this here would
        # quarantine every call an OpenRouter agent handled.
        await pool.execute(
            "INSERT INTO llm_usage (session_id, provider, model, input_tokens, "
            "input_cached_tokens, input_cache_write_tokens, output_tokens, "
            "reported_cost, purpose, tenant_id) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,'analysis',$9)",
            session_id,
            outcome.provider,
            outcome.model,
            outcome.usage.input_tokens,
            outcome.usage.input_cached_tokens,
            outcome.usage.input_cache_write_tokens,
            outcome.usage.output_tokens,
            outcome.usage.reported_cost,
            tenant.id,
        )

    await pool.execute(
        """
        UPDATE sessions
        SET analysis_status = $3,
            analysis_skip_reason = $4,
            summary = $5,
            outcome = $6,
            outcome_rationale = $7,
            analysis_fields = $8::jsonb,
            updated_at = now()
        WHERE id = $1 AND tenant_id = $2
        """,
        session_id,
        tenant.id,
        outcome.status,
        outcome.skip_reason,
        outcome.summary,
        outcome.outcome,
        outcome.outcome_rationale,
        json.dumps(outcome.fields, default=str),
    )


async def run_for_session(
    tenant: Tenant,
    session_id: str,
    cfg: AgentConfig,
    *,
    transcript: list[dict[str, object]],
    close_reason: str | None,
    transfer: Mapping[str, object] | None = None,
    claim: bool = True,
) -> AnalysisOutcome:
    """Analyse a sealed session and store the result. Never raises.

    The one entry point the worker calls. `cache_key` is the session id so a
    re-analysis of the same call reuses the provider's prompt cache.

    ``claim`` writes `analysis_status = 'pending'` before spending anything, so
    a run that dies mid-analysis leaves 'pending' rather than 'none' — 'none'
    says analysis was switched off, which would be a lie. Pass False from the
    worker path, where `create_session` claimed the row when the call started
    and the statement could only ever write the value already there. The
    backfill (`services/calls/analysis_ops.py`) re-analyses a settled call and
    does need it.
    """
    if not cfg.analysis.enabled:
        return AnalysisOutcome(status="none")
    try:
        if claim:
            pool = await db.tenant_pool(tenant)
            await pool.execute(
                "UPDATE sessions SET analysis_status = 'pending', updated_at = now() "
                "WHERE id = $1 AND tenant_id = $2",
                session_id,
                tenant.id,
            )
        outcome = await analyze(
            tenant,
            cfg.analysis,
            model=resolve_model(cfg),
            transcript=transcript,
            system_prompt=cfg.prompt,
            close_reason=close_reason,
            transfer=transfer,
            cache_key=session_id,
        )
        await persist(tenant, session_id, outcome)
        return outcome
    except Exception:
        logger.exception("post-call analysis failed for session %s", session_id)
        return AnalysisOutcome(status="failed")
