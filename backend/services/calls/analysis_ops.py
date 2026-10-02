"""Re-running post-call analysis over calls that already happened.

Two operations, and the difference between them is the whole point:

``preview_call_analysis`` runs a spec you have not saved against one past call
and returns what it found, writing nothing. Without it, configuring extraction
means shipping a prompt blind and waiting a day to find out whether it worked.

``backfill_call_analysis`` applies an agent's *saved* spec to calls that were
analysed under an older one (or not at all). It is also the only retry path in
a design with no queue, which is why it ships alongside the engine rather than
after it.

Both re-analyses cost real money, because both really call a model. See
``services.analysis.run.persist`` for why a re-run adds a usage row rather than
replacing one.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID, uuid4

from fastapi import HTTPException
from pydantic import BaseModel, Field, ValidationError

from services import analysis, billing
from services.agents.models import AgentConfig, AnalysisSpec
from services.agents.plan import load_plan_roster
from services.analysis import AnalysisOutcome
from services.transcripts import load_session_transcript
from services.user import Context

# One page of the calls list. Vapi caps the same operation at 100; the reason to
# have a cap at all is that each item is an LLM call the tenant pays for, so an
# unbounded request is an unbounded bill.
MAX_BACKFILL_CALLS = 100


class PreviewAnalysisRequest(BaseModel):
    """A spec to try, and the agent whose prompt it should be judged against.

    ``agent_id`` is optional: without it the call's own recorded agent version
    supplies the system prompt, which is what you want when tuning a spec for
    an agent that already exists.
    """

    analysis: AnalysisSpec
    agent_id: UUID | None = None


class PreviewAnalysisResponse(BaseModel):
    """What the spec found. Nothing was written."""

    analysis: analysis.AnalysisResponse
    # So the caller can see which model produced this before saving the spec.
    provider: str | None = None
    model: str | None = None


class BackfillAnalysisRequest(BaseModel):
    call_ids: list[UUID] = Field(min_length=1, max_length=MAX_BACKFILL_CALLS)


class BackfillAnalysisResponse(BaseModel):
    analysed: int = 0
    skipped: int = 0
    failed: int = 0


_CALL_CHANNELS = ("voice", "video")


async def _session_for_analysis(
    session_id: UUID, ctx: Context, channels: Sequence[str] = _CALL_CHANNELS, noun: str = "call"
):
    """One call (or chat) to analyse, refusing any that has not finished.

    Both operations read the stored transcript, which on a live call is however
    much of the conversation has been persisted so far. Analysing that would
    judge half a call, overwrite the real summary with the verdict, and — on the
    backfill path, which re-prices — bill a call that is still running. There is
    no version of "re-run the analysis" that means anything before the call is
    over, so this is a refusal rather than a caveat.
    """
    pool = await ctx.tenant_pool()
    row = await pool.fetchrow(
        "SELECT s.id, s.status, s.close_reason, s.agent_id, s.agent_plan, s.channel, "
        "av.config AS version_config "
        "FROM sessions s "
        "LEFT JOIN agent_versions av "
        "  ON av.id = s.agent_version_id AND av.tenant_id = s.tenant_id "
        "WHERE s.id = $1 AND s.tenant_id = $2 AND s.channel = ANY($3::text[])",
        session_id,
        ctx.tenant.id,
        list(channels),
    )
    if not row:
        raise HTTPException(status_code=404, detail=f"{noun} not found")
    if row["status"] not in ("completed", "failed", "canceled"):
        raise HTTPException(
            status_code=409,
            detail=f"this {noun} has not finished — analysis needs a complete transcript",
        )
    return row


async def _config_for(row, agent_id: UUID | None, ctx: Context) -> AgentConfig:
    """The agent definition whose prompt the analysis judges against.

    Prefers what the call actually ran, because "did this call succeed" is only
    meaningful against the instructions the agent had at the time — so a call
    that carried a plan is judged against its resolved ENTRY config, as it is
    today for a call that handed off. Without that, an overridden or inline call
    had no definition to judge at all and this refused outright.
    """
    pool = await ctx.tenant_pool()
    if agent_id is not None:
        agent = await pool.fetchrow(
            "SELECT config FROM agents WHERE id = $1 AND tenant_id = $2",
            agent_id,
            ctx.tenant.id,
        )
        if not agent:
            raise HTTPException(status_code=404, detail="agent not found")
        return AgentConfig.model_validate(agent["config"])
    if row["agent_plan"]:
        try:
            return (await load_plan_roster(pool, ctx.tenant.id, row["agent_plan"]))[0].config
        except (ValueError, ValidationError) as exc:
            raise HTTPException(
                status_code=400,
                detail=f"this call's agent plan can no longer be resolved: {exc}",
            ) from exc
    if not isinstance(row["version_config"], dict):
        raise HTTPException(
            status_code=400,
            detail="this call has no agent definition to analyse against",
        )
    return AgentConfig.model_validate(row["version_config"])


async def preview_call_analysis(
    session_id: UUID,
    body: PreviewAnalysisRequest,
    ctx: Context,
    *,
    channels: Sequence[str] = _CALL_CHANNELS,
    noun: str = "call",
) -> PreviewAnalysisResponse:
    """Try an analysis spec against one past call without saving anything.

    ``channels`` and ``noun`` make this the preview behind a chat's too.
    """
    row = await _session_for_analysis(session_id, ctx, channels, noun)
    cfg = await _config_for(row, body.agent_id, ctx)
    transcript = await load_session_transcript(ctx.tenant, str(session_id))

    # A preview of a disabled spec is still a preview — the author is asking
    # what it *would* produce, and refusing on `enabled` would make the toggle
    # order matter for no reason.
    spec = body.analysis.model_copy(update={"enabled": True})
    outcome = await analysis.analyze(
        ctx.tenant,
        spec,
        # The transient spec's own model if it names one, otherwise whatever the
        # agent would have used — so a preview reflects the real run.
        model=analysis.resolve_model(cfg, spec),
        transcript=transcript,
        system_prompt=cfg.prompt,
        close_reason=row["close_reason"],
        cache_key=str(session_id),
    )
    return PreviewAnalysisResponse(
        analysis=outcome.response(),
        provider=outcome.provider,
        model=outcome.model,
    )


async def backfill_call_analysis(
    body: BackfillAnalysisRequest,
    ctx: Context,
) -> BackfillAnalysisResponse:
    """Re-analyse past calls with each one's current agent definition.

    Re-prices every call it touches: ``bill_session`` is a full recompute that
    sends nothing, so the new analysis tokens land on the bill without replaying
    the call's lifecycle events at whoever is listening.

    Calls run one at a time: a backfill has nobody waiting on it, and spending a
    tenant's rate limit budget quickly is a cost, not a feature.
    """
    result = BackfillAnalysisResponse()
    for session_id in body.call_ids:
        outcome = await rerun_analysis(session_id, ctx)
        if outcome.status == "completed":
            result.analysed += 1
        elif outcome.status == "failed":
            result.failed += 1
        else:
            result.skipped += 1
    return result


async def rerun_analysis(
    session_id: UUID,
    ctx: Context,
    *,
    channels: Sequence[str] = _CALL_CHANNELS,
    noun: str = "call",
) -> AnalysisOutcome:
    """Analyse one finished session again with its saved definition, and re-price it."""
    row = await _session_for_analysis(session_id, ctx, channels, noun)
    cfg = await _config_for(row, None, ctx)
    outcome = await analysis.run_for_session(
        ctx.tenant,
        str(session_id),
        cfg,
        transcript=await load_session_transcript(ctx.tenant, str(session_id)),
        close_reason=row["close_reason"],
    )
    if outcome.usage is not None:
        if row["channel"] == "text":
            # A chat is debited per settlement; a fresh one takes what this run added.
            await billing.settle_chat(ctx.tenant, session_id, uuid4())
        else:
            await billing.bill_session(ctx.tenant, session_id)
    return outcome


__all__ = [
    "MAX_BACKFILL_CALLS",
    "BackfillAnalysisRequest",
    "BackfillAnalysisResponse",
    "PreviewAnalysisRequest",
    "PreviewAnalysisResponse",
    "backfill_call_analysis",
    "preview_call_analysis",
    "rerun_analysis",
]
