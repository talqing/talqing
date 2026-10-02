"""Workspace observability HTTP adapter."""

from __future__ import annotations

from datetime import date
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Query

from api.dataplane.deps import Context, CtxDep
from services import observability as svc
from services.observability.service import ObservabilityResponse

router = APIRouter(prefix="/observability", tags=["observability"])

Channel = Literal["voice", "video", "text"]


@router.get("", response_model=ObservabilityResponse)
async def get_observability(
    ctx: Context = CtxDep,
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    timezone_name: str = Query(default="UTC", alias="timezone"),
    agent_id: UUID | None = Query(default=None),
    channel: Channel | None = Query(default=None),
) -> ObservabilityResponse:
    """Workspace analytics: volume, endings, recordings, response time and spend.
    One snapshot of a date range, up to 7 days.

    `summary` is the range total, `daily` one row per day, and `volume` the same
    days cut by dimension (agent, phone number, type, status, close reason,
    recording, analysis, transfer) — each capped at eight series with the tail
    rolled into `Other`. `latency` reports response time per model stage and
    `cost` breaks the bill down by stage, provider and model.

    Narrow it with `agent_id` and `channel`; `timezone` decides day boundaries.

    Two traps. `latency.basis` is `per_turn` for voice and video and `per_call`
    for text, so the two are never averaged into one number. And every rate is
    over a subset — `success_rate` over `judged_sessions`, `cost` over
    `priced_sessions`, latency over `measured_sessions`.
    """
    return await svc.get_observability(
        ctx,
        start_date=start_date,
        end_date=end_date,
        timezone_name=timezone_name,
        agent_id=agent_id,
        channel=channel,
    )
