"""What post-call analysis produces, and the vocabulary for not producing it."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from services.llm_client import TokenUsage

# ── status vocabulary (mirrors the sessions.analysis_status CHECK) ──
# There is no 'running': analysis is one bounded call inside finalize, so a
# session that dies mid-analysis is left at 'pending', which already reads as
# "never finished" and needs no sweeper to correct it.
AnalysisStatus = Literal["none", "pending", "completed", "failed", "skipped"]

# Why a call was not worth an LLM request. Recorded rather than folded into
# 'none' because "we chose not to" and "it ran and found nothing" are different
# answers to the reader's question, and only one of them is a configuration
# problem they can fix.
SKIP_TOO_SHORT = "too_short"
SKIP_CALL_FAILED = "call_failed"
SKIP_PRODUCES_NOTHING = "produces_nothing"

Outcome = Literal["success", "failure", "unknown"]


class AnalysisResponse(BaseModel):
    """The analysis of one call, as every reader sees it.

    Shared verbatim by the calls API and the `session.completed` webhook so an
    integration that reads one can read the other.
    """

    # Response-only: every field below is always sent, so a default must not
    # publish it as optional. See HealthSnapshotResponse for the full reason.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    status: AnalysisStatus = "none"
    # Set only when status is 'skipped' — which gate refused the call.
    skip_reason: str | None = None
    summary: str | None = None
    # Three-valued on purpose: an evaluator that cannot say "I don't know" will
    # invent a verdict for wrong numbers and hangups.
    outcome: Outcome | None = None
    outcome_rationale: str | None = None
    # Tenant-defined extractions, keyed by field name. Deliberately NOT merged
    # into the session's userdata: userdata is what the agent knew (tools wrote
    # it, during the call), this is what a reader inferred afterwards, and a
    # merge would let a guess overwrite a tool-confirmed value.
    fields: dict[str, object] = Field(default_factory=dict)


class AnalysisOutcome(BaseModel):
    """One analysis run's result, on its way to the database.

    ``usage`` is what the call consumed; the runner writes it to `llm_usage`
    with purpose='analysis' so it prices like any other LLM row. A run that
    never reached the provider carries no usage and therefore costs nothing.
    """

    status: AnalysisStatus
    skip_reason: str | None = None
    summary: str | None = None
    outcome: Outcome | None = None
    outcome_rationale: str | None = None
    fields: dict[str, object] = Field(default_factory=dict)
    provider: str | None = None
    model: str | None = None
    usage: TokenUsage | None = None

    def response(self) -> AnalysisResponse:
        return AnalysisResponse(
            status=self.status,
            skip_reason=self.skip_reason,
            summary=self.summary,
            outcome=self.outcome,
            outcome_rationale=self.outcome_rationale,
            fields=self.fields,
        )
