"use client";

import { Suspense, useEffect, useMemo, useState } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import type {
  AgentResponse,
  ObservabilityBreakdownResponse,
  ObservabilityChannel,
  ObservabilityLatencyMetric,
  ObservabilityResponse,
} from "@talqing/sdk";
import { AppShell } from "@/app/components/AppShell";
import {
  CardHead,
  Container,
  PageHead,
  Panel,
  SectionLabel,
  Segment,
  Select,
  Skeleton,
  StatCard,
  SURFACE,
} from "@/app/components/ui";
import { api } from "@/lib/api";
import { apiErrorMessage } from "@/lib/apiError";
import { cn } from "@/lib/cn";
import {
  ANALYSIS_COLORS,
  CHART_COLORS,
  CLOSE_REASON_COLORS,
  RECORDING_COLORS,
  STATUS_COLORS,
  TRANSFER_COLORS,
  TYPE_COLORS,
  categorical,
  semantic,
} from "@/lib/chartColors";
import { addDays, earlierDate, startForDays, today } from "@/lib/date";
import { DatePicker } from "../components/DatePicker";
import {
  ChartCard,
  ChartEmpty,
  DayBarChart,
  DayAreaChart,
  LatencyChart,
  LatencyDistributionChart,
  StackedDayChart,
  type ValueFormat,
  formatValue,
} from "./ObservabilityCharts";
import {
  AgentsTable,
  CostLedger,
  EndingsList,
  duration,
  latency,
  money,
  percent,
} from "./ObservabilitySections";

/* The backend caps the window at a week and rejects anything wider. It also
   buckets a single-day range by hour and anything wider by day, which is why
   Today and Yesterday are their own presets rather than "1d": picking one is
   how you ask for an hour-by-hour reading. */
const MAX_RANGE_DAYS = 7;
const RANGE_PRESETS = [
  { value: "today", label: "Today", start: () => today(), end: () => today() },
  {
    value: "yesterday",
    label: "Yesterday",
    start: () => addDays(today(), -1),
    end: () => addDays(today(), -1),
  },
  { value: "7d", label: "7 days", start: () => startForDays(7), end: () => today() },
] as const;

const LATENCY_META: Record<ObservabilityLatencyMetric, { label: string; description: string }> = {
  e2e_latency: {
    label: "End-to-end response",
    description:
      "From the caller finishing speech until the agent begins responding. The number a caller actually feels.",
  },
  transcription_delay: {
    label: "Transcription",
    description: "From the end of speech until the final transcript is available.",
  },
  end_of_turn_delay: {
    label: "End of turn",
    description: "From the end of speech until the caller's turn is considered complete.",
  },
  llm_node_ttft: {
    label: "LLM first token",
    description:
      "Time for the language model to produce its first token. The one metric a text conversation reports.",
  },
  tts_node_ttfb: {
    label: "Voice first byte",
    description: "Time for text-to-speech to produce its first audio chunk.",
  },
};

/** The legend rows for a breakdown, ranked exactly as the chart stacks it. */
function legendFor(
  breakdown: ObservabilityBreakdownResponse,
  color: (key: string, index: number) => string,
  format: ValueFormat,
) {
  return breakdown.series.map((entry, index) => ({
    key: entry.key,
    label: entry.label,
    color: color(entry.key, index),
    value: formatValue(entry.total, format),
  }));
}

function breakdownTotal(breakdown: ObservabilityBreakdownResponse): number {
  return breakdown.series.reduce((sum, entry) => sum + entry.total, 0);
}

function DashboardSkeleton() {
  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-3.5 lg:grid-cols-3 xl:grid-cols-6">
        {Array.from({ length: 6 }).map((_, index) => (
          <div key={index} className={cn(SURFACE, "p-4")}>
            <Skeleton className="h-4 w-24" />
            <Skeleton className="mt-3 h-7 w-28" />
            <Skeleton className="mt-2 h-3 w-32" />
          </div>
        ))}
      </div>
      <div className="grid gap-4 xl:grid-cols-2">
        {Array.from({ length: 6 }).map((_, index) => (
          <Skeleton key={index} className="h-[280px] rounded-xl" />
        ))}
      </div>
    </div>
  );
}

function ObservabilityDashboard() {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const todayDate = today();
  const startDate = searchParams.get("start") || startForDays(MAX_RANGE_DAYS);
  const endDate = searchParams.get("end") || todayDate;
  const agentId = searchParams.get("agent") || "";
  const channel = (searchParams.get("channel") || "") as ObservabilityChannel | "";

  const [timezone, setTimezone] = useState("");
  const [agents, setAgents] = useState<AgentResponse[]>([]);
  const [data, setData] = useState<ObservabilityResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  function updateQuery(updates: Record<string, string | null>) {
    const next = new URLSearchParams(searchParams.toString());
    for (const [key, value] of Object.entries(updates)) {
      if (value) next.set(key, value);
      else next.delete(key);
    }
    const query = next.toString();
    router.replace(query ? `${pathname}?${query}` : pathname, { scroll: false });
  }

  useEffect(() => {
    setTimezone(Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC");
    api
      .listAllAgents()
      .then(setAgents)
      .catch((caught) => setError(apiErrorMessage(caught, "Unable to load the agent filter.")));
  }, []);

  useEffect(() => {
    if (!timezone) return;
    let current = true;
    setLoading(true);
    api
      .observability({
        start_date: startDate,
        end_date: endDate,
        timezone,
        agent_id: agentId || undefined,
        channel: channel || undefined,
      })
      .then((response) => {
        if (!current) return;
        setData(response);
        setError("");
      })
      .catch((caught) => {
        if (!current) return;
        setError(apiErrorMessage(caught, "Unable to load observability data."));
      })
      .finally(() => {
        if (current) setLoading(false);
      });
    return () => {
      current = false;
    };
  }, [agentId, channel, endDate, startDate, timezone]);

  const activePreset = RANGE_PRESETS.find(
    (preset) => startDate === preset.start() && endDate === preset.end(),
  );

  /* Drill-downs carry the reader's current question with them: the same window
     and agent they were looking at, plus whatever the row narrowed it to.
     `/calls` is a query-string page — the dashboard is a static export and has
     no dynamic route segments to spend. */
  function callsHref(params: Record<string, string>): string {
    const query = new URLSearchParams({ start: startDate, end: endDate, ...params });
    if (agentId) query.set("agent", agentId);
    return `/calls?${query.toString()}`;
  }

  return (
    <AppShell>
      <Container>
        <PageHead
          title="Observability"
          sub="Volume, endings, recordings, response time and spend over time. A single day is charted hour by hour."
        />

        <div className="mb-5 flex flex-wrap items-center gap-3">
          <Segment
            value={activePreset?.value ?? ""}
            onChange={(next) => {
              const preset = RANGE_PRESETS.find((option) => option.value === next);
              if (preset) updateQuery({ start: preset.start(), end: preset.end() });
            }}
            options={RANGE_PRESETS.map(({ value, label }) => ({ value, label }))}
          />
          <div className="flex items-center gap-2">
            {/* The backend refuses a window wider than a week, so moving one end
                drags the other along rather than letting the user pick a range
                the server will reject. Bounds are handed to the calendar too,
                which greys out the days outside them. */}
            <DatePicker
              value={startDate}
              max={earlierDate(endDate, todayDate)}
              clearable={false}
              ariaLabel="Start date"
              className="w-[150px]"
              onChange={(next) => {
                if (!next) return;
                const latestEnd = earlierDate(addDays(next, MAX_RANGE_DAYS - 1), todayDate);
                updateQuery({
                  start: next,
                  end: endDate > latestEnd || endDate < next ? latestEnd : endDate,
                });
              }}
            />
            <span className="text-placeholder">→</span>
            <DatePicker
              value={endDate}
              min={startDate}
              max={todayDate}
              clearable={false}
              ariaLabel="End date"
              className="w-[150px]"
              onChange={(next) => {
                if (!next) return;
                const earliestStart = addDays(next, -(MAX_RANGE_DAYS - 1));
                updateQuery({
                  start: startDate < earliestStart || startDate > next ? earliestStart : startDate,
                  end: next,
                });
              }}
            />
          </div>
          <Select
            value={agentId}
            onChange={(event) => updateQuery({ agent: event.target.value || null })}
            className="w-auto min-w-[210px]"
          >
            <option value="">All agents</option>
            {agents.map((agent) => (
              <option key={agent.id} value={agent.id}>
                {agent.config.name || agent.id.slice(0, 8)}
              </option>
            ))}
          </Select>
          <Select
            value={channel}
            onChange={(event) => updateQuery({ channel: event.target.value || null })}
            className="w-auto min-w-[150px]"
          >
            <option value="">All channels</option>
            <option value="voice">Voice</option>
            <option value="video">Video</option>
            <option value="text">Text</option>
          </Select>
        </div>

        {error && (
          <div className="mb-5 rounded-lg border border-danger/25 bg-danger/[0.05] px-3.5 py-2.5 text-[13.5px] text-danger">
            {error}
          </div>
        )}

        {loading ? (
          <DashboardSkeleton />
        ) : !data ? null : (
          <Dashboard data={data} callsHref={callsHref} />
        )}
      </Container>
    </AppShell>
  );
}

function Dashboard({
  data,
  callsHref,
}: {
  data: ObservabilityResponse;
  callsHref: (params: Record<string, string>) => string;
}) {
  const { summary, volume, cost, latency: response } = data;
  const granularity = data.range.granularity;
  /* The mode's headline metric is always first: end-to-end on voice, LLM first
     token on text. Both are "when the reply starts", measured the only way that
     channel can measure it. */
  const headline = response.metrics[0] ?? null;
  const [metric, setMetric] = useState<ObservabilityLatencyMetric | null>(null);
  const selected = response.metrics.find((item) => item.metric === metric) ?? headline;

  const buckets = useMemo(() => data.timeline.map((point) => point.bucket), [data.timeline]);
  const basisNoun = response.basis === "per_turn" ? "turns" : "conversations";
  const basisLabel = response.basis === "per_turn" ? "per turn" : "per call";
  const channelsLabel =
    response.channels.length === 2
      ? "voice and video calls"
      : `${response.channels[0] ?? "voice"} sessions`;

  const statusColor = semantic(STATUS_COLORS);
  const typeColor = semantic(TYPE_COLORS);
  const reasonColor = semantic(CLOSE_REASON_COLORS);
  const recordingColor = semantic(RECORDING_COLORS);
  const analysisColor = semantic(ANALYSIS_COLORS);
  const transferColor = semantic(TRANSFER_COLORS);

  const talkTime = data.timeline.reduce((sum, point) => sum + point.talk_time_seconds, 0);
  const recorded = data.timeline.reduce((sum, point) => sum + point.recorded_sessions, 0);
  const transferred = breakdownTotal(volume.transfer_data);

  /* "Other" is only ever the tail of an unbounded dimension, so it is worth
     saying how much went into it — a capped chart that says nothing reads as a
     chart that covered everything. */
  const otherNote = (breakdown: ObservabilityBreakdownResponse) =>
    breakdown.other_count > 0
      ? `Top ${breakdown.series.length - 1}; ${breakdown.other_count} more in Other`
      : undefined;

  return (
    <>
      {summary.total_sessions === 0 && (
        <div className="mb-4 rounded-lg border border-line-2 bg-subtle px-3.5 py-2.5 text-[13.5px] text-muted">
          No sessions in this range. Widen the dates, clear the agent or channel filter, or run an
          agent to start collecting data.
        </div>
      )}

      {/* ── the six figures, and the channel split under them ────────────── */}
      <div className="grid grid-cols-2 gap-3.5 lg:grid-cols-3 xl:grid-cols-6">
        <StatCard
          label="Sessions"
          value={summary.total_sessions.toLocaleString()}
          detail={`${summary.active_sessions} still running`}
        />
        <StatCard
          label="Completion rate"
          value={percent(summary.completion_rate)}
          detail={`${summary.completed_sessions} of ${
            summary.completed_sessions + summary.failed_sessions + summary.canceled_sessions
          } ended`}
          help="Completed sessions over every session that reached a terminal state. Sessions still running are excluded from both sides."
        />
        <StatCard
          label="Talk time"
          value={duration(summary.total_runtime_seconds)}
          detail={`${duration(summary.average_session_duration_seconds)} average`}
          help="Calls only. A chat has no length — it stays open until it is ended — so text is counted in messages, in the split below."
        />
        <StatCard
          label="Spend"
          value={money(summary.total_cost)}
          detail={`${money(summary.platform_fee)} platform fee`}
          help="Read off the pricing frozen on each call at the moment it was billed, not re-priced at today's catalog rates."
        />
        <StatCard
          label="Prompt cache"
          value={cost.cache_hit_ratio == null ? "—" : percent(cost.cache_hit_ratio)}
          detail={`${cost.llm_cached_input_tokens.toLocaleString()} of ${cost.llm_input_tokens.toLocaleString()} tokens`}
          help="Share of LLM input tokens served from the provider's prompt cache. Reported as a ratio rather than a saving, because the rate differs per model and one blended figure would be a number nobody can act on."
        />
        <StatCard
          label={`Response ${basisLabel}`}
          value={latency(headline?.average_ms)}
          detail={`${(headline?.sample_count ?? 0).toLocaleString()} ${basisNoun} · ${channelsLabel}`}
          help={headline ? LATENCY_META[headline.metric].description : undefined}
        />
      </div>

      {/* All three channels, always — the backend zero-fills them. A strip that
          dropped the empty ones would make "we run no video" and "video is
          broken today" look the same, and would change shape underneath the
          reader from one range to the next. */}
      <div className="mt-3.5 flex flex-wrap items-stretch overflow-hidden rounded-xl border border-line-2 bg-white sm:flex-nowrap">
        {summary.by_channel.map((entry) => (
          <div
            key={entry.channel}
            className="min-w-0 flex-1 border-b border-line px-4 py-3 last:border-b-0 sm:border-b-0 sm:border-r sm:last:border-r-0"
          >
            <div className="text-[12px] font-medium capitalize leading-4 text-muted">
              {entry.channel}
            </div>
            <div
              className={cn(
                "mt-1 truncate font-display text-[18px] font-semibold leading-6 tracking-tight tabular-nums",
                entry.sessions > 0 ? "text-ink" : "text-placeholder",
              )}
            >
              {entry.sessions.toLocaleString()}
              <span className="ml-1.5 text-[12px] font-normal text-faint">
                {entry.channel === "text" ? "chats" : "calls"}
              </span>
            </div>
            {/* A call is measured in time. A chat is not — it is open until
                somebody ends it — so it is measured in what it answered. */}
            <div className="mt-0.5 truncate text-[11.5px] leading-4 text-faint">
              {entry.sessions === 0
                ? "none in this range"
                : entry.channel === "text"
                  ? `${entry.answered_messages.toLocaleString()} messages answered · ${entry.open_sessions.toLocaleString()} open`
                  : `${duration(entry.average_session_duration_seconds)} average · ${duration(entry.total_runtime_seconds)} total`}
            </div>
          </div>
        ))}
      </div>

      {/* ── volume: who ran, how it went, and where it came from ────────── */}
      <SectionLabel>Volume</SectionLabel>
      <div className="grid gap-4 xl:grid-cols-2">
        <ChartCard
          title="Sessions by agent"
          total={summary.total_sessions.toLocaleString()}
          note={otherNote(volume.agent_data)}
          legend={legendFor(volume.agent_data, categorical, "count")}
        >
          <StackedDayChart
            breakdown={volume.agent_data}
            color={categorical}
            granularity={granularity}
            ariaLabel="Daily sessions by agent"
          />
        </ChartCard>

        <ChartCard
          title="Sessions by status"
          total={summary.total_sessions.toLocaleString()}
          legend={legendFor(volume.status_data, statusColor, "count")}
        >
          <StackedDayChart
            breakdown={volume.status_data}
            color={statusColor}
            granularity={granularity}
            ariaLabel="Daily sessions by status"
          />
        </ChartCard>

        <ChartCard
          title="Sessions by channel type"
          total={summary.total_sessions.toLocaleString()}
          legend={legendFor(volume.type_data, typeColor, "count")}
        >
          <StackedDayChart
            breakdown={volume.type_data}
            color={typeColor}
            granularity={granularity}
            ariaLabel="Daily sessions by channel type"
          />
        </ChartCard>

        <ChartCard
          title="Sessions by phone number"
          total={breakdownTotal(volume.phone_number_data).toLocaleString()}
          note={
            otherNote(volume.phone_number_data) ??
            "Your DIDs — web and text sessions have no number and are not counted here"
          }
          legend={legendFor(volume.phone_number_data, categorical, "count")}
        >
          <StackedDayChart
            breakdown={volume.phone_number_data}
            color={categorical}
            granularity={granularity}
            ariaLabel="Daily sessions by phone number"
            empty="No call in this range came in on, or went out from, one of your numbers."
          />
        </ChartCard>

        {/* Full width, and last in its section: seven bands need the room, and
            an odd chart out in a two-column grid would leave a hole beside it. */}
        <ChartCard
          className="xl:col-span-2"
          title="How calls ended"
          total={breakdownTotal(volume.close_reason_data).toLocaleString()}
          note="Grouped by who can fix it — the full list of reasons is below"
          legend={legendFor(volume.close_reason_data, reasonColor, "count")}
        >
          <StackedDayChart
            breakdown={volume.close_reason_data}
            color={reasonColor}
            granularity={granularity}
            height={220}
            ariaLabel="Daily sessions by how the call ended"
          />
        </ChartCard>

      </div>

      {/* ── operations: what the platform did with those calls ───────────── */}
      <SectionLabel>Operations</SectionLabel>
      <div className="grid gap-4 xl:grid-cols-2">
        <ChartCard
          title="Talk time"
          total={duration(talkTime)}
          note="Wall-clock length of every session that has one"
        >
          <DayBarChart
            data={data.timeline}
            granularity={granularity}
            dataKey="talk_time_seconds"
            label="Talk time"
            format="duration"
            ariaLabel="Daily talk time"
          />
        </ChartCard>

        <ChartCard
          title="Recordings"
          total={recorded.toLocaleString()}
          note="Stored objects, not what is still in the bucket — retention expires them on a fixed window"
          legend={legendFor(volume.recording_data, recordingColor, "count")}
        >
          <StackedDayChart
            breakdown={volume.recording_data}
            color={recordingColor}
            granularity={granularity}
            ariaLabel="Daily sessions by recording state"
          />
        </ChartCard>

        <ChartCard
          title="Post-call analysis"
          total={data.timeline.reduce((sum, point) => sum + point.analysed_sessions, 0).toLocaleString()}
          note="Skipped is a choice the gates made, not a failure"
          legend={legendFor(volume.analysis_data, analysisColor, "count")}
        >
          <StackedDayChart
            breakdown={volume.analysis_data}
            color={analysisColor}
            granularity={granularity}
            ariaLabel="Daily sessions by post-call analysis state"
          />
        </ChartCard>

        <ChartCard
          title="Transferred to a human"
          total={transferred.toLocaleString()}
          note="Every call the agent handed over, and whether the destination picked up"
          legend={legendFor(volume.transfer_data, transferColor, "count")}
        >
          <StackedDayChart
            breakdown={volume.transfer_data}
            color={transferColor}
            granularity={granularity}
            ariaLabel="Daily transfers to a human"
            empty="No call in this range was handed to a human."
          />
        </ChartCard>

      </div>

      {/* ── cost: what it was billed at, and to whom ─────────────────────── */}
      <SectionLabel>Cost</SectionLabel>
      <div className="grid gap-4 xl:grid-cols-2">
        <ChartCard
          title="Prompt cache hit rate"
          total={cost.cache_hit_ratio == null ? "—" : percent(cost.cache_hit_ratio)}
          note={`Share of LLM input tokens served from the provider's cache, per ${granularity}`}
        >
          <DayAreaChart
            data={data.timeline}
            granularity={granularity}
            dataKey="cache_hit_ratio"
            label="Cache hit rate"
            format="percent"
            domain={[0, 1]}
            ariaLabel="Daily prompt cache hit rate"
          />
        </ChartCard>

        <ChartCard
          title="Platform fee"
          total={money(summary.platform_fee)}
          note="Our margin — charged per minute, and waived on a call that failed"
        >
          <DayBarChart
            data={data.timeline}
            granularity={granularity}
            dataKey="platform_fee"
            label="Platform fee"
            format="money"
            color={CHART_COLORS.info}
            ariaLabel="Daily platform fee"
          />
        </ChartCard>

        <ChartCard
          className="xl:col-span-2"
          title="AI provider cost"
          total={money(summary.provider_cost)}
          note={otherNote(cost.by_provider) ?? "Passed through at the rate frozen on each call"}
          legend={legendFor(cost.by_provider, categorical, "money")}
        >
          <StackedDayChart
            breakdown={cost.by_provider}
            color={categorical}
            granularity={granularity}
            format="money"
            height={220}
            ariaLabel="Daily provider cost by provider"
            empty="No call in this range has been priced yet."
          />
        </ChartCard>
      </div>

      {/* ── response time ────────────────────────────────────────────────── */}
      <SectionLabel>Response time</SectionLabel>
      <Panel>
        <CardHead desc={`Measured ${basisLabel} over ${channelsLabel}. Seconds on both axes.`}>
          <Select
            value={selected?.metric ?? ""}
            onChange={(event) => setMetric(event.target.value as ObservabilityLatencyMetric)}
            className="w-[210px]"
            disabled={response.metrics.length < 2}
          >
            {response.metrics.map((item) => (
              <option key={item.metric} value={item.metric}>
                {LATENCY_META[item.metric].label}
              </option>
            ))}
          </Select>
        </CardHead>

        {response.measured_sessions === 0 ? (
          <ChartEmpty height={220}>
            {response.channels.includes("text")
              ? "No text conversation in this range sealed an LLM first-token measurement."
              : "This panel covers voice and video calls. A text-only workspace should switch the channel filter to Text, which reports LLM first token instead."}
          </ChartEmpty>
        ) : (
          <div className="grid gap-5 xl:grid-cols-[minmax(0,1.6fr)_minmax(0,1fr)]">
            <div>
              {selected && (
                <LatencyChart
                  buckets={buckets}
                  granularity={granularity}
                  metric={selected}
                  label={LATENCY_META[selected.metric].label}
                  unit={basisNoun}
                  thresholdMs={
                    selected.metric === response.metrics[0]?.metric
                      ? response.slow_response_threshold_ms
                      : undefined
                  }
                />
              )}
              {response.metrics.length > 1 && (
                <div className="mt-3 grid gap-2 sm:grid-cols-4">
                  {response.metrics.slice(1).map((item) => (
                    <button
                      key={item.metric}
                      type="button"
                      onClick={() => setMetric(item.metric)}
                      className={cn(
                        "rounded-lg border px-3 py-2 text-left transition-colors",
                        selected?.metric === item.metric
                          ? "border-line-strong bg-subtle"
                          : "border-line-2 hover:border-line-strong",
                      )}
                      title={LATENCY_META[item.metric].description}
                    >
                      <div className="truncate text-[11.5px] font-medium leading-4 text-muted">
                        {LATENCY_META[item.metric].label}
                      </div>
                      <div className="mt-0.5 font-mono text-[15px] font-semibold leading-5 tabular-nums text-ink">
                        {latency(item.average_ms)}
                      </div>
                    </button>
                  ))}
                </div>
              )}
            </div>
            <div>
              <div className="text-[11.5px] font-medium leading-4 text-muted">
                How the calls themselves were spread
              </div>
              <LatencyDistributionChart
                buckets={response.distribution}
                thresholdMs={response.slow_response_threshold_ms}
              />
              <div className="mt-2 rounded-lg border border-line-2 px-3 py-2.5">
                <div className="text-[12.5px] text-ink-soft">
                  <strong className="font-mono text-[15px] font-semibold tabular-nums text-ink">
                    {response.slow_sessions.toLocaleString()}
                  </strong>{" "}
                  of {response.measured_sessions.toLocaleString()} calls averaged over{" "}
                  {(response.slow_response_threshold_ms / 1000).toFixed(1)}s
                </div>
                <div className="mt-1 text-[11px] leading-4 text-faint">
                  Averages, not percentiles — a session seals one mean per metric, so the samples a
                  p95 needs are not here. A call that was mostly fast with two nine-second stalls
                  has a healthy average and will not appear above.
                </div>
              </div>
            </div>
          </div>
        )}
      </Panel>

      {/* ── the three things a chart cannot say ──────────────────────────── */}
      <SectionLabel>Detail</SectionLabel>
      <div className="grid gap-4 xl:grid-cols-2">
        <EndingsList endings={data.endings} callsHref={callsHref} />
        <CostLedger cost={cost} />
      </div>

      <div className="mt-4">
        <AgentsTable
          agents={data.agents}
          latencyLabel={
            headline
              ? `${LATENCY_META[headline.metric].label.toLowerCase()}, ${basisLabel}`
              : basisLabel
          }
        />
      </div>
    </>
  );
}

export default function ObservabilityPage() {
  return (
    <Suspense fallback={null}>
      <ObservabilityDashboard />
    </Suspense>
  );
}
