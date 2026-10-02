"""What a call and a chat say about themselves in the same words.

Both are one `sessions` row, so the cost, the analysis, the agent and the trace
are one shape on every surface that serves them.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from services.analysis import AnalysisResponse
from services.billing import PriceBreakdown


class CostResponse(BaseModel):
    provider_cost: float
    platform_fee: float
    total_charge: float
    duration_s: int | None = None


class CostDetailResponse(CostResponse):
    """The three totals, plus the priced lines they were summed from.

    ``pricing_snapshot`` is the exact ``PriceBreakdown`` the biller wrote at
    seal, rates and all — declared as one rather than as a bag so every reader
    gets the real shape instead of re-deriving it. Only the ``computed`` branch
    of the biller writes a breakdown here, and this whole model is ``None``
    unless ``billing_status = 'computed'``, so the ``{"error": …}`` shape the
    unpriceable path writes to the same column is unreachable from here.
    """

    # Response-only: every field below is always sent, so a default must not
    # publish it as optional. See HealthSnapshotResponse for the full reason.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    pricing_snapshot: PriceBreakdown | None = None


class AgentMetadata(BaseModel):
    agent_id: UUID | None = None
    name: str | None = None
    channel: str


class SessionEventResponse(BaseModel):
    """One row of the durable runtime trace (`services.session_events`)."""

    seq: int
    type: str
    payload: dict[str, object]
    created_at: datetime


def summary_cost(row: Mapping[str, object]) -> CostResponse | None:
    """The totals, once the session has been priced."""
    if row["billing_status"] != "computed" or row["total_charge"] is None:
        return None
    return CostResponse(
        provider_cost=row["provider_cost"],  # type: ignore[arg-type]
        platform_fee=row["platform_fee"],  # type: ignore[arg-type]
        total_charge=row["total_charge"],  # type: ignore[arg-type]
        duration_s=row["duration_s"],  # type: ignore[arg-type]
    )


def detail_cost(row: Mapping[str, object]) -> CostDetailResponse | None:
    if row["billing_status"] != "computed" or row["total_charge"] is None:
        return None
    return CostDetailResponse(
        provider_cost=row["provider_cost"],  # type: ignore[arg-type]
        platform_fee=row["platform_fee"],  # type: ignore[arg-type]
        total_charge=row["total_charge"],  # type: ignore[arg-type]
        duration_s=row["duration_s"],  # type: ignore[arg-type]
        pricing_snapshot=row["pricing_snapshot"],  # type: ignore[arg-type]
    )


def analysis_from_row(row: Mapping[str, object]) -> AnalysisResponse:
    fields = row["analysis_fields"]
    return AnalysisResponse(
        status=row["analysis_status"],  # type: ignore[arg-type]
        skip_reason=row["analysis_skip_reason"],  # type: ignore[arg-type]
        summary=row["summary"],  # type: ignore[arg-type]
        outcome=row["outcome"],  # type: ignore[arg-type]
        outcome_rationale=row["outcome_rationale"],  # type: ignore[arg-type]
        fields=fields if isinstance(fields, dict) else {},
    )
