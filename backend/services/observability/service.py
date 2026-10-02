"""Workspace observability aggregates — one snapshot of a date range.

Everything on this page is read from `sessions`, one row per run, and nothing
else. No query here fans out over `conversation_items`, `session_events` or the
`*_usage` tables: a tenant doing lakhs of calls a day at twenty turns a call
writes tens of millions of turn rows a week, and a dashboard that scans them is
a dashboard that stops loading. The only joins are to small dimension tables —
`agents`, `phone_numbers`.

The page is a wall of time-bucketed charts, so the shape this module returns is
a **series per dimension over a shared timeline**: one scan of `sessions` cut
nine ways by `GROUPING SETS`, rather than nine scans. A range of one day is
bucketed by hour and anything wider by day (`_granularity`), and `range.granularity`
says which, so the client labels an axis without re-deriving the rule. `_breakdown` then does the two things
every one of those charts needs and none of them should reinvent — zero-fill
every day so a chart has no holes, and cap the series count so a workspace with
two hundred DIDs gets a readable chart instead of two hundred colours.

Two statistics here look surprising, and both are consequences of the one-row
rule:

- **Response time is a sample-weighted mean of per-session means.**
  `sessions.metrics` seals `{e2e_latency, transcription_delay, end_of_turn_delay,
  llm_node_ttft, tts_node_ttfb, turns}` at finalize, and `turns` is *exactly* the
  sample count behind `e2e_latency` (`utils/latency.py`), so weighting by it
  reproduces the turn-level average arithmetically. What it cannot reproduce is a
  percentile — a p95 needs the samples, and the bag holds a mean — so the
  distribution across calls stands in for it.
- **Cost comes from `pricing_snapshot`, never re-priced from the catalog.**
  The snapshot is the priced breakdown the biller froze at seal; re-pricing today
  would answer with today's rates for last week's calls.

Every aggregate reads the same `filtered_sessions` CTE, so no two charts can
disagree about which sessions are in range, and every one honours the `agent_id`
and `channel` filters.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import asyncpg
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict

from services.close_reasons import BUCKET_ORDER, CloseReasonBucket, bucket_for
from services.session_snapshot import SLOW_RESPONSE_MS
from services.user import Context
from utils.latency import LATENCY_FIELDS, LatencyField

Channel = Literal["voice", "video", "text"]
LatencyMetric = LatencyField
LatencyBasis = Literal["per_turn", "per_call"]
# How wide one point on the timeline is. Derived from the range, never asked for:
# a single day is read hour by hour, anything longer day by day.
Granularity = Literal["day", "hour"]
CostKind = Literal["llm", "stt", "tts", "realtime", "avatar"]

LATENCY_METRICS: tuple[LatencyMetric, ...] = LATENCY_FIELDS

# A text session never reports `e2e_latency` — LiveKit measures it from speech —
# so it seals `turns = 0` while still carrying a real `llm_node_ttft`. Weighting
# by `turns` across an unfiltered set would silently drop every text session from
# the LLM chart, so the two channels get two statistics with two labels instead.
VOICE_CHANNELS: tuple[Channel, ...] = ("voice", "video")
# Every channel the platform has, in the order the rest of the product lists
# them. `summary.by_channel` reports all three whether or not a workspace uses
# them: a strip that only shows the channels with traffic makes "we run no video"
# and "video is broken today" look identical, and it changes shape underneath a
# reader between one range and the next.
ALL_CHANNELS: tuple[Channel, ...] = ("voice", "video", "text")
TEXT_LATENCY_METRICS: tuple[LatencyMetric, ...] = ("llm_node_ttft",)

# Millisecond edges for the per-session response-time histogram. The widest
# closed edge is SLOW_RESPONSE_MS so "exceeded the threshold" is a boundary
# between bars rather than a line drawn through the middle of one.
LATENCY_BUCKET_EDGES_MS: tuple[float, ...] = (
    500.0,
    1000.0,
    1500.0,
    2000.0,
    2500.0,
    3000.0,
    SLOW_RESPONSE_MS,
    5000.0,
)

# A week. Not a performance ceiling — measured at 2,000 sessions a day the whole
# endpoint lands at ~130 ms of wall clock over 7 days, and would carry 30 days in
# ~250 ms and 90 in ~640 ms. It is a product choice: raising it costs
# milliseconds, so change this number and nothing else if that choice changes.
MAX_RANGE_DAYS = 7

# The categorical palette has eight slots and never cycles one, so a ninth
# series would either share a colour or invent one. It rolls into "Other"
# instead — and `_breakdown` says how many went in, because a chart that
# silently drops the tail reads as a chart that covered everything.
MAX_SERIES = 8
OTHER_KEY = "__other__"

# One dashboard load runs SIX aggregates — summary, timeline, endings, latency,
# agents, cost — plus a seventh only when the summary reported an unpriceable
# session, which is rare and runs after the rest anyway.
#
# `pool_max_size` is 10 per process and is shared with every other request in
# flight, so the six go in two waves rather than all at once: a page that takes
# six of ten connections stalls the API it sits in whenever two people open it.
# The cap costs about 90 ms of wall clock at 30 days — the cost scan is by far
# the slowest query and dominates either way — and keeps six connections free.
MAX_CONCURRENT_QUERIES = 4


class ObservabilityRangeResponse(BaseModel):
    # Response-only: every field below is always sent, so a default must not
    # publish it as optional. See HealthSnapshotResponse for the full reason.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)
    start_date: date
    end_date: date
    timezone: str
    # `hour` when start and end are the same day. Every `bucket` below is a local
    # wall-clock timestamp in `timezone`, at midnight or at the top of the hour,
    # so the client formats it without needing to know which.
    granularity: Granularity


class ObservabilityChannelResponse(BaseModel):
    # Response-only: every field below is always sent, so a default must not
    # publish it as optional. See HealthSnapshotResponse for the full reason.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)
    channel: Channel
    sessions: int
    # Still in progress: calls under way, or chats nobody has ended.
    open_sessions: int
    # Calls only. A chat has no runtime — it stays open until it is ended — so
    # text reports zero here and `answered_messages` instead.
    total_runtime_seconds: float
    average_session_duration_seconds: float | None
    # Text only: the messages its agents answered, which is what a chat is
    # billed on. As of each chat's last settlement.
    answered_messages: int


class ObservabilitySummaryResponse(BaseModel):
    # Response-only: every field below is always sent, so a default must not
    # publish it as optional. See HealthSnapshotResponse for the full reason.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)
    total_sessions: int
    completed_sessions: int
    failed_sessions: int
    canceled_sessions: int
    active_sessions: int
    completion_rate: float | None
    total_runtime_seconds: float
    average_session_duration_seconds: float | None
    total_cost: float
    provider_cost: float
    platform_fee: float
    priced_sessions: int
    pending_sessions: int
    unpriceable_sessions: int
    average_e2e_latency_ms: float | None
    e2e_latency_samples: int
    by_channel: list[ObservabilityChannelResponse]


class ObservabilityTimelineResponse(BaseModel):
    """One bucket, every single-series figure the page charts over time."""

    # Response-only: every field below is always sent, so a default must not
    # publish it as optional. See HealthSnapshotResponse for the full reason.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    bucket: datetime
    total_sessions: int
    completed_sessions: int
    incomplete_sessions: int
    total_cost: float
    provider_cost: float
    platform_fee: float
    talk_time_seconds: float
    recorded_sessions: int
    analysed_sessions: int
    transferred_sessions: int
    # Over this day's LLM lines only. Null on a day nothing priced.
    cache_hit_ratio: float | None


class ObservabilitySeriesPointResponse(BaseModel):
    # Response-only: every field below is always sent, so a default must not
    # publish it as optional. See HealthSnapshotResponse for the full reason.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)
    bucket: datetime
    value: float


class ObservabilitySeriesResponse(BaseModel):
    # Response-only: every field below is always sent, so a default must not
    # publish it as optional. See HealthSnapshotResponse for the full reason.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)
    # Stable across reloads and across days, because it is what the client keys
    # a colour off: colour follows the entity, never its rank, so a filter that
    # drops one series must not repaint the others.
    key: str
    label: str
    total: float
    # Every bucket in the range, zero-filled. A stacked bar chart with a missing
    # bucket does not draw a gap, it draws the wrong shape.
    points: list[ObservabilitySeriesPointResponse]


class ObservabilityBreakdownResponse(BaseModel):
    # Response-only: every field below is always sent, so a default must not
    # publish it as optional. See HealthSnapshotResponse for the full reason.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)
    series: list[ObservabilitySeriesResponse]
    # How many distinct values the tail rolled up into "Other" — 0 when nothing
    # was dropped. Stated rather than implied, so a capped chart cannot be
    # mistaken for a complete one.
    other_count: int


class ObservabilityVolumeResponse(BaseModel):
    # Response-only: every field below is always sent, so a default must not
    # publish it as optional. See HealthSnapshotResponse for the full reason.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)
    agent_data: ObservabilityBreakdownResponse
    phone_number_data: ObservabilityBreakdownResponse
    type_data: ObservabilityBreakdownResponse
    status_data: ObservabilityBreakdownResponse
    # Bucketed by who can fix the ending rather than by the raw reason: twenty
    # SIP failure strings on one chart is a chart nobody reads, and the bucket
    # is the actionable half. `endings` below keeps the raw strings.
    close_reason_data: ObservabilityBreakdownResponse
    recording_data: ObservabilityBreakdownResponse
    analysis_data: ObservabilityBreakdownResponse
    transfer_data: ObservabilityBreakdownResponse


class ObservabilityEndingResponse(BaseModel):
    # Response-only: every field below is always sent, so a default must not
    # publish it as optional. See HealthSnapshotResponse for the full reason.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)
    bucket: CloseReasonBucket
    close_reason: str | None
    sessions: int
    failed_sessions: int


class ObservabilityLatencyPointResponse(BaseModel):
    # Response-only: every field below is always sent, so a default must not
    # publish it as optional. See HealthSnapshotResponse for the full reason.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)
    bucket: datetime
    average_ms: float
    sample_count: int


class ObservabilityLatencyMetricResponse(BaseModel):
    # Response-only: every field below is always sent, so a default must not
    # publish it as optional. See HealthSnapshotResponse for the full reason.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)
    metric: LatencyMetric
    average_ms: float | None
    sample_count: int
    points: list[ObservabilityLatencyPointResponse]


class ObservabilityLatencyBucketResponse(BaseModel):
    # Response-only: every field below is always sent, so a default must not
    # publish it as optional. See HealthSnapshotResponse for the full reason.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)
    lower_ms: float
    upper_ms: float | None
    sessions: int


class ObservabilityLatencyResponse(BaseModel):
    # Response-only: every field below is always sent, so a default must not
    # publish it as optional. See HealthSnapshotResponse for the full reason.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)
    basis: LatencyBasis
    channels: list[Channel]
    measured_sessions: int
    slow_response_threshold_ms: float
    slow_sessions: int
    metrics: list[ObservabilityLatencyMetricResponse]
    distribution: list[ObservabilityLatencyBucketResponse]


class ObservabilityCostLineResponse(BaseModel):
    # Response-only: every field below is always sent, so a default must not
    # publish it as optional. See HealthSnapshotResponse for the full reason.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)
    kind: CostKind
    provider: str
    model: str
    cost: float
    share: float
    purpose: str | None
    priority: bool | None


class ObservabilityUnpriceableReasonResponse(BaseModel):
    # Response-only: every field below is always sent, so a default must not
    # publish it as optional. See HealthSnapshotResponse for the full reason.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)
    message: str | None
    sessions: int


class ObservabilityCostResponse(BaseModel):
    # Response-only: every field below is always sent, so a default must not
    # publish it as optional. See HealthSnapshotResponse for the full reason.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)
    priced_sessions: int
    provider_cost: float
    platform_fee: float
    total_cost: float
    llm_cost: float
    stt_cost: float
    tts_cost: float
    realtime_cost: float
    avatar_cost: float
    analysis_cost: float
    priority_cost: float
    llm_input_tokens: int
    llm_cached_input_tokens: int
    cache_hit_ratio: float | None
    lines: list[ObservabilityCostLineResponse]
    by_provider: ObservabilityBreakdownResponse
    unpriceable_reasons: list[ObservabilityUnpriceableReasonResponse]


class ObservabilityAgentResponse(BaseModel):
    # Response-only: every field below is always sent, so a default must not
    # publish it as optional. See HealthSnapshotResponse for the full reason.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)
    agent_id: UUID | None
    agent_name: str | None
    channel: Channel
    sessions: int
    completed_sessions: int
    completion_rate: float | None
    judged_sessions: int
    successful_sessions: int
    success_rate: float | None
    average_session_duration_seconds: float | None
    total_cost: float
    average_cost_per_session: float | None
    average_e2e_latency_ms: float | None


class ObservabilityResponse(BaseModel):
    range: ObservabilityRangeResponse
    summary: ObservabilitySummaryResponse
    timeline: list[ObservabilityTimelineResponse]
    volume: ObservabilityVolumeResponse
    endings: list[ObservabilityEndingResponse]
    latency: ObservabilityLatencyResponse
    cost: ObservabilityCostResponse
    agents: list[ObservabilityAgentResponse]


# All channels are rows on sessions. Window uses COALESCE(started_at, created_at)
# so queued jobs still count. Cost and duration use the shared money columns.
#
# A text chat has no duration: it is open until somebody ends it, possibly for
# weeks, so summing or averaging its lifetime as "runtime" would bury every call
# on the page. Its measure is the messages it answered, read off the breakdown
# its last settlement froze.
#
# Every aggregate below starts from this one CTE. A second CTE carrying its own
# window predicate is how two charts end up disagreeing about which calls are in
# range, so there is not one.
_EXECUTIONS_CTE = """
WITH filtered_sessions AS (
    SELECT
        e.tenant_id,
        e.id AS session_id,
        e.agent_id,
        e.agent_name,
        e.channel,
        e.type,
        e.status,
        e.close_reason,
        e.outcome,
        e.analysis_status,
        e.recording_status,
        e.transfer,
        e.metrics,
        e.phone_number_id,
        e.billing_status,
        e.pricing_snapshot,
        e.provider_cost,
        e.platform_fee,
        COALESCE(e.started_at, e.created_at) AS started_at,
        CASE WHEN e.channel = 'text' THEN NULL ELSE COALESCE(
            e.duration_s::double precision,
            EXTRACT(EPOCH FROM (e.ended_at - e.started_at))
        ) END AS duration_seconds,
        CASE WHEN e.billing_status = 'computed'
            THEN COALESCE((e.pricing_snapshot->>'answered_messages')::int, 0)
            ELSE 0
        END AS answered_messages,
        CASE
            WHEN e.billing_status = 'computed' AND e.total_charge IS NOT NULL
                THEN e.total_charge::double precision
            ELSE NULL
        END AS total_cost
    FROM sessions e
    WHERE e.tenant_id = $1
        AND COALESCE(e.started_at, e.created_at) >= $2
        AND COALESCE(e.started_at, e.created_at) < $3
        AND ($4::uuid IS NULL OR e.agent_id = $4::uuid)
        AND ($5::text IS NULL OR e.channel = $5::text)
)
"""

_SUMMARY_QUERY = (
    _EXECUTIONS_CTE
    + """
SELECT
    GROUPING(channel) AS g_channel,
    channel,
    COUNT(*) AS total_sessions,
    COUNT(*) FILTER (WHERE status = 'completed') AS completed_sessions,
    COUNT(*) FILTER (WHERE status = 'failed') AS failed_sessions,
    COUNT(*) FILTER (WHERE status = 'canceled') AS canceled_sessions,
    COUNT(*) FILTER (WHERE status IN ('queued', 'running')) AS active_sessions,
    COALESCE(SUM(duration_seconds) FILTER (WHERE duration_seconds >= 0), 0)
        AS total_runtime_seconds,
    AVG(duration_seconds) FILTER (WHERE duration_seconds >= 0)
        AS average_session_duration_seconds,
    COALESCE(SUM(answered_messages), 0) AS answered_messages,
    COALESCE(SUM(total_cost), 0) AS total_cost,
    COALESCE(SUM(provider_cost) FILTER (WHERE billing_status = 'computed'), 0)
        AS provider_cost,
    COALESCE(SUM(platform_fee) FILTER (WHERE billing_status = 'computed'), 0)
        AS platform_fee,
    COUNT(*) FILTER (WHERE billing_status = 'computed') AS priced_sessions,
    COUNT(*) FILTER (WHERE billing_status = 'pending') AS pending_sessions,
    COUNT(*) FILTER (WHERE billing_status = 'unpriceable') AS unpriceable_sessions
FROM filtered_sessions
WHERE tenant_id = $1
GROUP BY GROUPING SETS ((), (channel))
"""
)

# The whole chart wall in one scan. Nine grouping sets over the same rows: the
# day's own totals, then the day cut by each dimension the page stacks. Doing it
# as nine queries would be nine passes over the window for the same counters.
#
# GROUPING(col) is 0 exactly when that column is part of the set that produced
# the row, which is what tells the shapes apart on the way out. `phone_numbers`
# is joined for the DID's own label — the tenant reads their numbers as +91…,
# not as a uuid — and it is a small table.
_TIMELINE_QUERY = (
    _EXECUTIONS_CTE
    + """
, dated AS (
    SELECT
        date_trunc($7, f.started_at AT TIME ZONE $6) AS bucket,
        f.agent_id,
        f.agent_name,
        f.type,
        f.status,
        f.close_reason,
        f.recording_status,
        f.analysis_status,
        f.transfer->>'outcome' AS transfer_outcome,
        f.phone_number_id,
        pn.e164,
        pn.label,
        f.duration_seconds,
        f.total_cost,
        f.provider_cost,
        f.platform_fee,
        f.billing_status
    FROM filtered_sessions f
    LEFT JOIN phone_numbers pn
        ON pn.id = f.phone_number_id AND pn.tenant_id = f.tenant_id
    WHERE f.tenant_id = $1
)
SELECT
    GROUPING(agent_id) AS g_agent,
    GROUPING(phone_number_id) AS g_phone,
    GROUPING(type) AS g_type,
    GROUPING(status) AS g_status,
    GROUPING(close_reason) AS g_reason,
    GROUPING(recording_status) AS g_recording,
    GROUPING(analysis_status) AS g_analysis,
    GROUPING(transfer_outcome) AS g_transfer,
    bucket,
    agent_id,
    agent_name,
    phone_number_id,
    e164,
    label,
    type,
    status,
    close_reason,
    recording_status,
    analysis_status,
    transfer_outcome,
    COUNT(*) AS sessions,
    COUNT(*) FILTER (WHERE status = 'completed') AS completed_sessions,
    COUNT(*) FILTER (WHERE status IN ('failed', 'canceled')) AS incomplete_sessions,
    COUNT(*) FILTER (WHERE recording_status = 'stored') AS recorded_sessions,
    COUNT(*) FILTER (WHERE analysis_status = 'completed') AS analysed_sessions,
    COUNT(*) FILTER (WHERE transfer_outcome IS NOT NULL) AS transferred_sessions,
    COALESCE(SUM(duration_seconds) FILTER (WHERE duration_seconds >= 0), 0)
        AS talk_time_seconds,
    COALESCE(SUM(total_cost), 0) AS total_cost,
    COALESCE(SUM(provider_cost) FILTER (WHERE billing_status = 'computed'), 0)
        AS provider_cost,
    COALESCE(SUM(platform_fee) FILTER (WHERE billing_status = 'computed'), 0)
        AS platform_fee
FROM dated
GROUP BY GROUPING SETS (
    (bucket),
    (bucket, agent_id, agent_name),
    (bucket, phone_number_id, e164, label),
    (bucket, type),
    (bucket, status),
    (bucket, close_reason),
    (bucket, recording_status),
    (bucket, analysis_status),
    (bucket, transfer_outcome)
)
"""
)

# The raw endings, range-wide. Kept beside the bucketed chart because the
# bucket says who to go to and the raw string is what they will ask you for.
_ENDINGS_QUERY = (
    _EXECUTIONS_CTE
    + """
SELECT close_reason, status, COUNT(*) AS sessions
FROM filtered_sessions
WHERE tenant_id = $1
GROUP BY close_reason, status
"""
)

# One pair of columns per metric: the sample-weighted mean and the samples
# behind it. Written from LATENCY_FIELDS rather than by hand — five copies of the
# same eight lines is where a metric quietly gets left out of one of them.
_VOICE_METRIC_COLUMNS = ",\n".join(
    f"""    SUM(CASE WHEN jsonb_typeof(metrics->'{field}') = 'number'
             THEN (metrics->>'{field}')::double precision * turns END)
        / NULLIF(SUM(CASE WHEN jsonb_typeof(metrics->'{field}') = 'number'
                          THEN turns END), 0) AS {field}_average,
    COALESCE(SUM(CASE WHEN jsonb_typeof(metrics->'{field}') = 'number'
                      THEN turns END), 0) AS {field}_samples"""
    for field in LATENCY_FIELDS
)

# Voice/video response time, weighted by the turns each session measured. The
# range total, the per-bucket series and the distribution across calls are the
# same scan cut three ways. Sessions whose worker died carry `{}` and are excluded
# from the denominator rather than counted as zero.
_VOICE_LATENCY_QUERY = (
    _EXECUTIONS_CTE
    + f"""
, bagged AS (
    SELECT
        date_trunc($7, started_at AT TIME ZONE $6) AS bucket,
        metrics,
        (metrics->>'turns')::int AS turns,
        CASE WHEN jsonb_typeof(metrics->'e2e_latency') = 'number'
             THEN (metrics->>'e2e_latency')::double precision END AS response_ms
    FROM filtered_sessions
    WHERE tenant_id = $1
        AND channel = ANY($8::text[])
        AND jsonb_typeof(metrics->'turns') = 'number'
        AND (metrics->>'turns')::int > 0
)
SELECT
    GROUPING(bucket) AS g_bucket,
    GROUPING(band) AS g_band,
    bucket,
    band,
    COUNT(*) AS sessions,
    COUNT(*) FILTER (WHERE response_ms > $9) AS slow_sessions,
{_VOICE_METRIC_COLUMNS}
FROM (
    SELECT b.*, width_bucket(b.response_ms, $10::double precision[]) AS band
    FROM bagged b
) s
GROUP BY GROUPING SETS ((), (bucket), (band))
"""
)

# Text response time. `llm_node_ttft` is the only metric a text session reports,
# and it has no turn count behind it, so this is an unweighted mean of
# per-session means — a different statistic, labelled as one.
_TEXT_LATENCY_QUERY = (
    _EXECUTIONS_CTE
    + """
, bagged AS (
    SELECT
        date_trunc($7, started_at AT TIME ZONE $6) AS bucket,
        (metrics->>'llm_node_ttft')::double precision AS response_ms
    FROM filtered_sessions
    WHERE tenant_id = $1
        AND jsonb_typeof(metrics->'llm_node_ttft') = 'number'
)
SELECT
    GROUPING(bucket) AS g_bucket,
    GROUPING(band) AS g_band,
    bucket,
    band,
    COUNT(*) AS sessions,
    COUNT(*) FILTER (WHERE response_ms > $8) AS slow_sessions,
    AVG(response_ms) AS llm_node_ttft_average,
    COUNT(response_ms) AS llm_node_ttft_samples
FROM (
    SELECT b.*, width_bucket(b.response_ms, $9::double precision[]) AS band
    FROM bagged b
) s
GROUP BY GROUPING SETS ((), (bucket), (band))
"""
)

# One row per agent: which of six agents is the problem, in the columns a
# stacked volume chart cannot carry. `agent_name` is denormalised onto sessions
# precisely so this needs no join.
_AGENTS_QUERY = (
    _EXECUTIONS_CTE
    + """
SELECT
    agent_id,
    agent_name,
    channel,
    COUNT(*) AS sessions,
    COUNT(*) FILTER (WHERE status = 'completed') AS completed_sessions,
    COUNT(*) FILTER (WHERE status IN ('completed', 'failed', 'canceled'))
        AS terminal_sessions,
    COUNT(*) FILTER (WHERE outcome IS NOT NULL) AS judged_sessions,
    COUNT(*) FILTER (WHERE outcome = 'success') AS successful_sessions,
    AVG(duration_seconds) FILTER (WHERE duration_seconds >= 0)
        AS average_session_duration_seconds,
    COALESCE(SUM(total_cost), 0) AS total_cost,
    COUNT(total_cost) AS priced_sessions,
    SUM(CASE WHEN jsonb_typeof(metrics->'e2e_latency') = 'number'
                  AND jsonb_typeof(metrics->'turns') = 'number'
             THEN (metrics->>'e2e_latency')::double precision
                  * (metrics->>'turns')::int END)
        / NULLIF(SUM(CASE WHEN jsonb_typeof(metrics->'e2e_latency') = 'number'
                               AND jsonb_typeof(metrics->'turns') = 'number'
                          THEN (metrics->>'turns')::int END), 0)
        AS average_e2e_latency_ms
FROM filtered_sessions
WHERE tenant_id = $1
GROUP BY agent_id, agent_name, channel
"""
)

# The page's slowest query — three to six unnested rows per session — and the
# only source for any of this that does not need a new column. `pricing_snapshot`
# is the breakdown the biller froze; the lines are read, never re-priced, because
# re-pricing would answer with today's rates for last week's calls.
#
# Filtered to priced sessions, which is required for correctness and also keeps
# the `{"error": ...}` shape written on unpriceable ones out of the unnest.
#
# Three cuts, one unnest: spend per provider per day (the stacked cost chart),
# the day's cached/uncached input split (the cache-hit line), and the range-wide
# provider/model ledger.
_COST_QUERY = (
    _EXECUTIONS_CTE
    + """
, priced AS (
    SELECT
        date_trunc($7, started_at AT TIME ZONE $6) AS bucket,
        pricing_snapshot
    FROM filtered_sessions
    WHERE tenant_id = $1
        AND total_cost IS NOT NULL
        AND jsonb_typeof(pricing_snapshot) = 'object'
), lines AS (
    SELECT
        p.bucket,
        k.kind,
        l.line->>'provider' AS provider,
        l.line->>'model' AS model,
        l.line->>'purpose' AS purpose,
        (l.line->>'priority')::boolean AS priority,
        (l.line->>'cost')::double precision AS cost,
        COALESCE((l.line->>'uncached_input_tokens')::bigint, 0) AS uncached_input_tokens,
        COALESCE((l.line->>'input_cached_tokens')::bigint, 0) AS input_cached_tokens
    FROM priced p
    CROSS JOIN LATERAL (
        VALUES ('llm'), ('stt'), ('tts'), ('realtime'), ('avatar')
    ) AS k(kind)
    CROSS JOIN LATERAL jsonb_array_elements(
        CASE WHEN jsonb_typeof(p.pricing_snapshot->k.kind) = 'array'
             THEN p.pricing_snapshot->k.kind
             ELSE '[]'::jsonb END
    ) AS l(line)
)
SELECT
    GROUPING(bucket) AS g_bucket,
    GROUPING(provider) AS g_provider,
    GROUPING(model) AS g_model,
    bucket,
    kind,
    provider,
    model,
    purpose,
    priority,
    SUM(cost) AS cost,
    SUM(uncached_input_tokens) FILTER (WHERE kind = 'llm') AS uncached_input_tokens,
    SUM(input_cached_tokens) FILTER (WHERE kind = 'llm') AS input_cached_tokens
FROM lines
GROUP BY GROUPING SETS (
    (),
    (bucket),
    (bucket, provider),
    (kind, provider, model, purpose, priority)
)
"""
)

# Only reached when the summary already said there is something to explain, so
# the page does not pay for a seventh scan on the overwhelming majority of loads
# where billing priced everything cleanly.
_UNPRICEABLE_QUERY = (
    _EXECUTIONS_CTE
    + """
SELECT pricing_snapshot->>'error' AS message, COUNT(*) AS sessions
FROM filtered_sessions
WHERE tenant_id = $1 AND billing_status = 'unpriceable'
GROUP BY message
ORDER BY sessions DESC
"""
)


def _zoneinfo(timezone_name: str) -> ZoneInfo:
    """Resolve an IANA zone. Browsers may send legacy keys (e.g. Asia/Calcutta)."""
    key = (timezone_name or "UTC").strip() or "UTC"
    try:
        return ZoneInfo(key)
    except ZoneInfoNotFoundError:
        raise HTTPException(
            status_code=400,
            detail=f"timezone must be a valid IANA name (got {key!r})",
        ) from None


def _resolve_range(
    start_date: date | None,
    end_date: date | None,
    timezone_name: str,
) -> tuple[date, date, datetime, datetime, str]:
    key = (timezone_name or "UTC").strip() or "UTC"
    tz = _zoneinfo(key)

    today = datetime.now(tz).date()
    resolved_end = end_date or today
    resolved_start = start_date or (resolved_end - timedelta(days=MAX_RANGE_DAYS - 1))
    if resolved_start > resolved_end:
        raise HTTPException(status_code=400, detail="start_date must be on or before end_date")
    if (resolved_end - resolved_start).days > MAX_RANGE_DAYS - 1:
        raise HTTPException(
            status_code=400, detail=f"date range cannot exceed {MAX_RANGE_DAYS} days"
        )
    if resolved_end == date.max:
        raise HTTPException(status_code=400, detail="end_date is out of range")

    start_utc = datetime.combine(resolved_start, time.min, tzinfo=tz).astimezone(UTC)
    end_utc = datetime.combine(resolved_end + timedelta(days=1), time.min, tzinfo=tz).astimezone(
        UTC
    )
    return resolved_start, resolved_end, start_utc, end_utc, key


def _granularity(start: date, end: date) -> Granularity:
    """One day is read hour by hour; anything wider, day by day.

    Derived from the range rather than asked for, so a caller cannot request an
    hourly reading of a month and get 720 points, and the two cannot contradict
    each other.
    """
    return "hour" if start == end else "day"


def _buckets(start: date, end: date, granularity: Granularity) -> list[datetime]:
    """Every bucket the range covers, as the local wall-clock stamps the queries
    return — `date_trunc` over a timestamp already shifted into the request's
    zone, so these are naive on both sides and compare directly.

    A single day always yields all 24 hours, including the ones today has not
    reached yet: a chart that stops at the current hour reads as a day that
    ended early.
    """
    if granularity == "hour":
        midnight = datetime.combine(start, time.min)
        return [midnight + timedelta(hours=hour) for hour in range(24)]
    return [
        datetime.combine(start + timedelta(days=offset), time.min)
        for offset in range((end - start).days + 1)
    ]


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def _ms(value: object) -> float | None:
    return round(float(value), 1) if value is not None else None


def _breakdown(
    rows: Iterable[tuple[str, str, datetime, float]],
    buckets: list[datetime],
    *,
    order: tuple[str, ...] | None = None,
) -> ObservabilityBreakdownResponse:
    """Turn `(key, label, day, value)` tuples into ranked, zero-filled series.

    Every chart on the page needs the same two things and none of them should
    reinvent either. **Zero-fill**, because a stacked bar chart with a missing
    bucket does not draw a gap — it draws the wrong shape. And a **cap**, because
    the categorical palette has eight slots and never cycles one: a workspace
    with two hundred DIDs gets its eight busiest and an "Other" band, with the
    count of what went into it reported rather than implied.

    `order` pins a dimension whose values have a natural reading order — call
    status, an ending's owner — so those charts stack the same way every load
    instead of reshuffling whenever yesterday's counts change.
    """
    totals: dict[str, float] = {}
    labels: dict[str, str] = {}
    per_bucket: dict[str, dict[datetime, float]] = {}
    for key, label, bucket, value in rows:
        totals[key] = totals.get(key, 0.0) + value
        labels[key] = label
        counts = per_bucket.setdefault(key, {})
        counts[bucket] = counts.get(bucket, 0.0) + value

    if order is not None:
        ranked = [key for key in order if key in totals]
        ranked += sorted(set(totals) - set(order), key=lambda key: -totals[key])
    else:
        ranked = sorted(totals, key=lambda key: -totals[key])

    kept, tail = ranked[:MAX_SERIES], ranked[MAX_SERIES:]
    series = [
        ObservabilitySeriesResponse(
            key=key,
            label=labels[key],
            total=round(totals[key], 6),
            points=[
                ObservabilitySeriesPointResponse(
                    bucket=bucket, value=round(per_bucket[key].get(bucket, 0.0), 6)
                )
                for bucket in buckets
            ],
        )
        for key in kept
    ]
    if tail:
        series.append(
            ObservabilitySeriesResponse(
                key=OTHER_KEY,
                label="Other",
                total=round(sum(totals[key] for key in tail), 6),
                points=[
                    ObservabilitySeriesPointResponse(
                        bucket=bucket,
                        value=round(sum(per_bucket[key].get(bucket, 0.0) for key in tail), 6),
                    )
                    for bucket in buckets
                ],
            )
        )
    return ObservabilityBreakdownResponse(series=series, other_count=len(tail))


# Fixed stacking orders. `type` and `status` read in a fixed sequence rather than
# by volume, and the ending buckets read outward from "nothing went wrong" to
# "ours" — which is also the order somebody triaging works through them.
# A streamed call sits with the two phone kinds because that is what it is to a
# tenant — somebody else's phone call — and before the web/text pair for the same
# reason. An unknown type still renders (ranked by volume, labelled with its own
# string), which is deliberate; none of ours should ever arrive that way.
_TYPE_ORDER = ("SIP_INBOUND", "SIP_OUTBOUND", "WHATSAPP_INBOUND", "STREAM", "WEB", "TEXT")
_STATUS_ORDER = ("completed", "failed", "canceled", "running", "queued")
_RECORDING_ORDER = ("stored", "pending", "failed", "consent_withdrawn", "deleted", "none")
_ANALYSIS_ORDER = ("completed", "failed", "skipped", "pending", "none")
_TRANSFER_ORDER = ("connected", "failed")

_TYPE_LABEL = {
    "SIP_INBOUND": "Inbound phone",
    "SIP_OUTBOUND": "Outbound phone",
    "WHATSAPP_INBOUND": "WhatsApp",
    # Not "Inbound stream": the partner owns the dialling, so the direction is
    # something we are told or do not know, never something we did.
    "STREAM": "Media stream",
    "WEB": "Web call",
    "TEXT": "Text",
}
_STATUS_LABEL = {
    "completed": "Completed",
    "failed": "Failed",
    "canceled": "Canceled",
    "running": "Running",
    "queued": "Queued",
}
_RECORDING_LABEL = {
    "stored": "Recorded",
    "pending": "Pending",
    "failed": "Recording failed",
    "consent_withdrawn": "Consent withdrawn",
    "deleted": "Deleted",
    "none": "Not recorded",
}
_ANALYSIS_LABEL = {
    "completed": "Analysed",
    "failed": "Analysis failed",
    "skipped": "Skipped",
    "pending": "Pending",
    "none": "Not configured",
}
_BUCKET_LABEL: dict[CloseReasonBucket, str] = {
    "normal": "Ended normally",
    "transferred": "Transferred",
    "caller_unreachable": "Caller unreachable",
    "carrier_fault": "Carrier fault",
    "configuration": "Configuration",
    "platform": "Platform",
    "other": "Unrecognised",
}


def _volume_response(
    rows: list[asyncpg.Record], buckets: list[datetime]
) -> ObservabilityVolumeResponse:
    """Split the one timeline scan back into the eight breakdowns it carries."""

    def cut(
        flag: str, key_of, label_of, *, order: tuple[str, ...] | None = None
    ) -> ObservabilityBreakdownResponse:
        tuples = []
        for row in rows:
            if row[flag] != 0:
                continue
            key = key_of(row)
            if key is None:
                continue
            tuples.append((key, label_of(row), row["bucket"], float(row["sessions"])))
        return _breakdown(tuples, buckets, order=order)

    return ObservabilityVolumeResponse(
        agent_data=cut(
            "g_agent",
            lambda row: str(row["agent_id"]) if row["agent_id"] else None,
            # `agent_name` is the name frozen on the session, so an agent renamed
            # mid-range keeps whatever it was called on the day it ran.
            lambda row: row["agent_name"] or "Unnamed agent",
        ),
        phone_number_data=cut(
            "g_phone",
            lambda row: str(row["phone_number_id"]) if row["phone_number_id"] else None,
            lambda row: row["label"] or row["e164"] or "Released number",
        ),
        type_data=cut("g_type", lambda row: row["type"], _type_label, order=_TYPE_ORDER),
        status_data=cut("g_status", lambda row: row["status"], _status_label, order=_STATUS_ORDER),
        close_reason_data=cut(
            "g_reason",
            # Bucketed, not raw: twenty SIP failure strings on one stacked chart
            # is a chart nobody reads, and the bucket is the actionable half.
            lambda row: bucket_for(row["close_reason"]) if row["close_reason"] else None,
            lambda row: _BUCKET_LABEL[bucket_for(row["close_reason"])],
            order=BUCKET_ORDER,
        ),
        recording_data=cut(
            "g_recording",
            lambda row: row["recording_status"],
            _recording_label,
            order=_RECORDING_ORDER,
        ),
        analysis_data=cut(
            "g_analysis",
            lambda row: row["analysis_status"],
            _analysis_label,
            order=_ANALYSIS_ORDER,
        ),
        transfer_data=cut(
            "g_transfer",
            lambda row: row["transfer_outcome"],
            lambda row: "Connected" if row["transfer_outcome"] == "connected" else "Failed",
            order=_TRANSFER_ORDER,
        ),
    )


def _type_label(row: asyncpg.Record) -> str:
    return _TYPE_LABEL.get(row["type"], row["type"])


def _status_label(row: asyncpg.Record) -> str:
    return _STATUS_LABEL.get(row["status"], row["status"])


def _recording_label(row: asyncpg.Record) -> str:
    return _RECORDING_LABEL.get(row["recording_status"], row["recording_status"])


def _analysis_label(row: asyncpg.Record) -> str:
    return _ANALYSIS_LABEL.get(row["analysis_status"], row["analysis_status"])


def _by_channel(summary_rows: list[asyncpg.Record]) -> list[ObservabilityChannelResponse]:
    """All three channels, in a fixed order, zero-filled.

    `GROUP BY` only emits the channels that ran, so a workspace with no video
    would otherwise be missing that row entirely — and a channel the reader can
    see is empty is a different fact from one that is not on the page.
    """
    rows = {row["channel"]: row for row in summary_rows if row["g_channel"] == 0}
    out = []
    for channel in ALL_CHANNELS:
        row = rows.get(channel)
        duration = row["average_session_duration_seconds"] if row else None
        out.append(
            ObservabilityChannelResponse(
                channel=channel,
                sessions=int(row["total_sessions"]) if row else 0,
                open_sessions=int(row["active_sessions"]) if row else 0,
                total_runtime_seconds=round(float(row["total_runtime_seconds"]), 1) if row else 0.0,
                average_session_duration_seconds=(
                    round(float(duration), 1) if duration is not None else None
                ),
                answered_messages=int(row["answered_messages"]) if row else 0,
            )
        )
    return out


def _endings_response(rows: list[asyncpg.Record]) -> list[ObservabilityEndingResponse]:
    """Rank every distinct ending, keeping unmapped reasons visible as themselves.

    A session that has not ended yet has no reason to report and belongs to the
    KPI row's active count, so `(no reason, still running)` is dropped. A
    *terminal* session with no reason is kept: that is a worker path that forgot
    to set one, and it is worth seeing.
    """
    totals: dict[tuple[CloseReasonBucket, str | None], list[int]] = {}
    for row in rows:
        reason = row["close_reason"]
        status = row["status"]
        if reason is None and status in ("queued", "running"):
            continue
        key = (bucket_for(reason), reason)
        entry = totals.setdefault(key, [0, 0])
        entry[0] += int(row["sessions"])
        if status == "failed":
            entry[1] += int(row["sessions"])

    return [
        ObservabilityEndingResponse(
            bucket=bucket,
            close_reason=reason,
            sessions=sessions,
            failed_sessions=failed,
        )
        for (bucket, reason), (sessions, failed) in sorted(
            totals.items(), key=lambda item: (BUCKET_ORDER.index(item[0][0]), -item[1][0])
        )
    ]


def _latency_response(
    rows: list[asyncpg.Record],
    *,
    basis: LatencyBasis,
    channels: list[Channel],
    metrics: tuple[LatencyMetric, ...],
    buckets: list[datetime],
) -> ObservabilityLatencyResponse:
    """Assemble the three cuts the one latency scan returns.

    Two different "buckets" meet here: a point on the timeline, and a band of the
    response-time histogram. The query calls the second one `band` so neither
    side has to guess which is meant.
    """
    totals = next((row for row in rows if row["g_bucket"] == 1 and row["g_band"] == 1), None)
    by_bucket = {row["bucket"]: row for row in rows if row["g_bucket"] == 0}
    by_band = {
        int(row["band"]): row for row in rows if row["g_band"] == 0 and row["band"] is not None
    }

    def points(metric: LatencyMetric) -> list[ObservabilityLatencyPointResponse]:
        # A bucket with no sample of this metric is left out rather than plotted
        # as zero: the line connects across the gap, which is honest, where a
        # zero would draw an hour the agent answered instantly.
        out = []
        for bucket in buckets:
            row = by_bucket.get(bucket)
            average = row[f"{metric}_average"] if row else None
            if average is None:
                continue
            out.append(
                ObservabilityLatencyPointResponse(
                    bucket=bucket,
                    average_ms=round(float(average), 1),
                    sample_count=int(row[f"{metric}_samples"]),
                )
            )
        return out

    return ObservabilityLatencyResponse(
        basis=basis,
        channels=channels,
        measured_sessions=int(totals["sessions"]) if totals else 0,
        slow_response_threshold_ms=SLOW_RESPONSE_MS,
        slow_sessions=int(totals["slow_sessions"]) if totals else 0,
        metrics=[
            ObservabilityLatencyMetricResponse(
                metric=metric,
                average_ms=_ms(totals[f"{metric}_average"]) if totals else None,
                sample_count=int(totals[f"{metric}_samples"]) if totals else 0,
                points=points(metric),
            )
            for metric in metrics
        ],
        distribution=[
            ObservabilityLatencyBucketResponse(
                lower_ms=0.0 if index == 0 else LATENCY_BUCKET_EDGES_MS[index - 1],
                upper_ms=(
                    LATENCY_BUCKET_EDGES_MS[index] if index < len(LATENCY_BUCKET_EDGES_MS) else None
                ),
                sessions=int(by_band[index]["sessions"]) if index in by_band else 0,
            )
            for index in range(len(LATENCY_BUCKET_EDGES_MS) + 1)
        ],
    )


def _cost_response(
    rows: list[asyncpg.Record],
    buckets: list[datetime],
    *,
    priced_sessions: int,
    provider_cost: float,
    platform_fee: float,
    total_cost: float,
    unpriceable_rows: list[asyncpg.Record],
) -> ObservabilityCostResponse:
    """Roll the priced lines up into the shapes the page asks its questions in.

    `share` is of `total_cost`, not of the provider passthrough, so the platform
    fee is the one segment the lines do not cover and the shares add to a whole.
    """
    totals = next((row for row in rows if row["g_bucket"] == 1 and row["g_model"] == 1), None)
    by_kind = dict.fromkeys(("llm", "stt", "tts", "realtime", "avatar"), 0.0)
    analysis_cost = 0.0
    priority_cost = 0.0
    lines: list[ObservabilityCostLineResponse] = []

    for row in rows:
        if row["g_model"] != 0:
            continue
        kind = row["kind"]
        cost = float(row["cost"])
        by_kind[kind] += cost
        if kind == "llm":
            if row["purpose"] == "analysis":
                analysis_cost += cost
            if row["priority"]:
                priority_cost += cost
        lines.append(
            ObservabilityCostLineResponse(
                kind=kind,
                provider=row["provider"],
                model=row["model"],
                cost=round(cost, 6),
                share=round(cost / total_cost, 4) if total_cost else 0.0,
                purpose=row["purpose"],
                priority=row["priority"],
            )
        )
    lines.sort(key=lambda line: -line.cost)

    cached = int(totals["input_cached_tokens"] or 0) if totals else 0
    uncached = int(totals["uncached_input_tokens"] or 0) if totals else 0
    llm_input_tokens = cached + uncached

    return ObservabilityCostResponse(
        priced_sessions=priced_sessions,
        provider_cost=provider_cost,
        platform_fee=platform_fee,
        total_cost=total_cost,
        llm_cost=round(by_kind["llm"], 6),
        stt_cost=round(by_kind["stt"], 6),
        tts_cost=round(by_kind["tts"], 6),
        realtime_cost=round(by_kind["realtime"], 6),
        avatar_cost=round(by_kind["avatar"], 6),
        analysis_cost=round(analysis_cost, 6),
        priority_cost=round(priority_cost, 6),
        llm_input_tokens=llm_input_tokens,
        llm_cached_input_tokens=cached,
        # Reported as a ratio rather than a "₹ saved" figure: the rates are per
        # line, so one savings number across several models is a blend nobody can
        # act on.
        cache_hit_ratio=(round(cached / llm_input_tokens, 4) if llm_input_tokens else None),
        lines=lines,
        by_provider=_breakdown(
            [
                (row["provider"], row["provider"], row["bucket"], float(row["cost"]))
                for row in rows
                if row["g_bucket"] == 0 and row["g_provider"] == 0 and row["provider"] is not None
            ],
            buckets,
        ),
        unpriceable_reasons=[
            ObservabilityUnpriceableReasonResponse(
                message=row["message"], sessions=int(row["sessions"])
            )
            for row in unpriceable_rows
        ],
    )


async def get_observability(
    ctx: Context,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
    timezone_name: str = "UTC",
    agent_id: UUID | None = None,
    channel: Channel | None = None,
) -> ObservabilityResponse:
    """Return one complete dashboard snapshot for the selected session range."""
    resolved_start, resolved_end, start_utc, end_utc, tz_key = _resolve_range(
        start_date, end_date, timezone_name
    )
    granularity = _granularity(resolved_start, resolved_end)
    params = (ctx.tenant.id, start_utc, end_utc, agent_id, channel)
    # $6 and $7 together are the bucket: which clock to read the timestamp in,
    # and how wide one point on the timeline is.
    bucket_params = (tz_key, granularity)
    pool = await ctx.tenant_pool()
    gate = asyncio.Semaphore(MAX_CONCURRENT_QUERIES)

    async def fetch(query: str, *args: object) -> list[asyncpg.Record]:
        async with gate:
            return await pool.fetch(query, *args)

    # The latency section is voice-first with a text mode, because the metrics
    # bag does not mean the same thing on both channels (see the module
    # docstring). A workspace with no channel filter gets the voice reading and
    # the UI says so, rather than one number with a silent denominator switch.
    if channel == "text":
        latency_basis: LatencyBasis = "per_call"
        latency_channels: list[Channel] = ["text"]
        latency_metrics = TEXT_LATENCY_METRICS
        latency_call = fetch(
            _TEXT_LATENCY_QUERY,
            *params,
            *bucket_params,
            SLOW_RESPONSE_MS,
            list(LATENCY_BUCKET_EDGES_MS),
        )
    else:
        latency_basis = "per_turn"
        latency_channels = [channel] if channel else list(VOICE_CHANNELS)
        latency_metrics = LATENCY_METRICS
        latency_call = fetch(
            _VOICE_LATENCY_QUERY,
            *params,
            *bucket_params,
            latency_channels,
            SLOW_RESPONSE_MS,
            list(LATENCY_BUCKET_EDGES_MS),
        )

    (
        summary_rows,
        timeline_rows,
        ending_rows,
        latency_rows,
        agent_rows,
        cost_rows,
    ) = await asyncio.gather(
        fetch(_SUMMARY_QUERY, *params),
        fetch(_TIMELINE_QUERY, *params, *bucket_params),
        fetch(_ENDINGS_QUERY, *params),
        latency_call,
        fetch(_AGENTS_QUERY, *params),
        fetch(_COST_QUERY, *params, *bucket_params),
    )

    totals = next(row for row in summary_rows if row["g_channel"] == 1)
    total_sessions = int(totals["total_sessions"])
    completed = int(totals["completed_sessions"])
    terminal = completed + int(totals["failed_sessions"]) + int(totals["canceled_sessions"])
    priced_sessions = int(totals["priced_sessions"])
    unpriceable_sessions = int(totals["unpriceable_sessions"])
    average_duration = totals["average_session_duration_seconds"]
    total_cost = round(float(totals["total_cost"]), 6)
    provider_cost = round(float(totals["provider_cost"]), 6)
    platform_fee = round(float(totals["platform_fee"]), 6)

    unpriceable_rows = await fetch(_UNPRICEABLE_QUERY, *params) if unpriceable_sessions > 0 else []

    buckets = _buckets(resolved_start, resolved_end, granularity)
    latency = _latency_response(
        latency_rows,
        basis=latency_basis,
        channels=latency_channels,
        metrics=latency_metrics,
        buckets=buckets,
    )
    e2e = next((item for item in latency.metrics if item.metric == "e2e_latency"), None)

    # The `(bucket)` grouping set: one row per bucket the range actually saw.
    bucket_totals = {
        row["bucket"]: row
        for row in timeline_rows
        if all(
            row[flag] == 1
            for flag in (
                "g_agent",
                "g_phone",
                "g_type",
                "g_status",
                "g_reason",
                "g_recording",
                "g_analysis",
                "g_transfer",
            )
        )
    }
    # The cache-hit line is charted per bucket, so it comes off the cost scan's
    # own bucketed cut rather than being apportioned from the range total.
    cache_by_bucket = {
        row["bucket"]: (
            int(row["input_cached_tokens"] or 0),
            int(row["uncached_input_tokens"] or 0),
        )
        for row in cost_rows
        if row["g_bucket"] == 0 and row["g_provider"] == 1
    }

    timeline: list[ObservabilityTimelineResponse] = []
    for bucket in buckets:
        row = bucket_totals.get(bucket)
        cached, uncached = cache_by_bucket.get(bucket, (0, 0))
        timeline.append(
            ObservabilityTimelineResponse(
                bucket=bucket,
                total_sessions=int(row["sessions"]) if row else 0,
                completed_sessions=int(row["completed_sessions"]) if row else 0,
                incomplete_sessions=int(row["incomplete_sessions"]) if row else 0,
                total_cost=round(float(row["total_cost"]), 6) if row else 0.0,
                provider_cost=round(float(row["provider_cost"]), 6) if row else 0.0,
                platform_fee=round(float(row["platform_fee"]), 6) if row else 0.0,
                talk_time_seconds=round(float(row["talk_time_seconds"]), 1) if row else 0.0,
                recorded_sessions=int(row["recorded_sessions"]) if row else 0,
                analysed_sessions=int(row["analysed_sessions"]) if row else 0,
                transferred_sessions=int(row["transferred_sessions"]) if row else 0,
                cache_hit_ratio=(
                    round(cached / (cached + uncached), 4) if cached + uncached else None
                ),
            )
        )

    return ObservabilityResponse(
        range=ObservabilityRangeResponse(
            start_date=resolved_start,
            end_date=resolved_end,
            timezone=tz_key,
            granularity=granularity,
        ),
        summary=ObservabilitySummaryResponse(
            total_sessions=total_sessions,
            completed_sessions=completed,
            failed_sessions=int(totals["failed_sessions"]),
            canceled_sessions=int(totals["canceled_sessions"]),
            active_sessions=int(totals["active_sessions"]),
            completion_rate=_rate(completed, terminal),
            total_runtime_seconds=round(float(totals["total_runtime_seconds"]), 1),
            average_session_duration_seconds=(
                round(float(average_duration), 1) if average_duration is not None else None
            ),
            total_cost=total_cost,
            provider_cost=provider_cost,
            platform_fee=platform_fee,
            priced_sessions=priced_sessions,
            # Split, because a call still being priced and a call billing could
            # not price are opposite states: the first resolves itself, the
            # second is an incident with a message attached (see cost).
            pending_sessions=int(totals["pending_sessions"]),
            unpriceable_sessions=unpriceable_sessions,
            average_e2e_latency_ms=e2e.average_ms if e2e else None,
            e2e_latency_samples=e2e.sample_count if e2e else 0,
            by_channel=_by_channel(summary_rows),
        ),
        timeline=timeline,
        volume=_volume_response(timeline_rows, buckets),
        endings=_endings_response(ending_rows),
        latency=latency,
        cost=_cost_response(
            cost_rows,
            buckets,
            priced_sessions=priced_sessions,
            provider_cost=provider_cost,
            platform_fee=platform_fee,
            total_cost=total_cost,
            unpriceable_rows=unpriceable_rows,
        ),
        agents=sorted(
            (
                ObservabilityAgentResponse(
                    agent_id=row["agent_id"],
                    agent_name=row["agent_name"],
                    channel=row["channel"],
                    sessions=int(row["sessions"]),
                    completed_sessions=int(row["completed_sessions"]),
                    completion_rate=_rate(
                        int(row["completed_sessions"]), int(row["terminal_sessions"])
                    ),
                    judged_sessions=int(row["judged_sessions"]),
                    successful_sessions=int(row["successful_sessions"]),
                    success_rate=_rate(
                        int(row["successful_sessions"]), int(row["judged_sessions"])
                    ),
                    average_session_duration_seconds=(
                        round(float(row["average_session_duration_seconds"]), 1)
                        if row["average_session_duration_seconds"] is not None
                        else None
                    ),
                    total_cost=round(float(row["total_cost"]), 6),
                    average_cost_per_session=(
                        round(float(row["total_cost"]) / int(row["priced_sessions"]), 6)
                        if int(row["priced_sessions"])
                        else None
                    ),
                    average_e2e_latency_ms=_ms(row["average_e2e_latency_ms"]),
                )
                for row in agent_rows
            ),
            key=lambda item: -item.sessions,
        ),
    )
