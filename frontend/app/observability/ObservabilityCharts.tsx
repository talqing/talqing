"use client";

import { useMemo } from "react";
import type {
  ObservabilityBreakdownResponse,
  ObservabilityGranularity,
  ObservabilityLatencyBucketResponse,
  ObservabilityLatencyMetricResponse,
} from "@talqing/sdk";
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { CHART_COLORS as TOKENS } from "@/lib/chartColors";
import { cn } from "@/lib/cn";

/* ── formatting ───────────────────────────────────────────────────────────── */

export type ValueFormat = "count" | "money" | "duration" | "percent";

export function formatValue(value: number, format: ValueFormat): string {
  switch (format) {
    case "money":
      return value === 0 ? "$0" : value < 0.01 ? `$${value.toFixed(4)}` : `$${value.toFixed(2)}`;
    case "duration":
      return value < 3600
        ? `${Math.round(value / 60)}m`
        : `${(value / 3600).toFixed(value < 36000 ? 1 : 0)}h`;
    case "percent":
      return `${Math.round(value * 100)}%`;
    default:
      return value.toLocaleString();
  }
}

/**
 * Tick labels for one axis, in ONE unit and ONE precision for the whole axis.
 *
 * Both have to be decided from the axis's largest value rather than per tick,
 * because a formatter that decides per tick produces the two things that make an
 * axis unreadable: a scale that changes unit halfway up (`30m, 1h, 1h, 2h`) and
 * one that rounds two different ticks to the same label (`$1, $1`).
 */
function axisFormat(format: ValueFormat, max: number) {
  const decimals = max < 1 ? 2 : max < 10 ? 1 : 0;
  const inHours = max >= 3600;
  return (value: number) => {
    if (value === 0) return "0";
    if (format === "money") return `$${value.toFixed(decimals)}`;
    if (format === "duration")
      return inHours ? `${(value / 3600).toFixed(max < 36000 ? 1 : 0)}h` : `${Math.round(value / 60)}m`;
    if (format === "percent") return `${Math.round(value * 100)}%`;
    return value >= 10000 ? `${Math.round(value / 1000)}k` : String(value);
  };
}

/** Widest a tick label gets, so the axis reserves room instead of clipping it. */
function axisWidth(format: ValueFormat, max: number): number {
  if (format === "money") return max < 1 ? 52 : max < 10 ? 48 : 44;
  if (format === "percent") return 44;
  if (format === "duration") return 40;
  return 40;
}

/* A bucket stamp is local wall clock with no zone on it, so it is parsed as
   local — never through Date.parse of a bare "YYYY-MM-DD", which is UTC and
   lands on the previous day for anyone east of Greenwich. */
function parseBucket(value: string): Date {
  const [datePart, timePart = "00:00:00"] = value.split("T");
  const [y, m, d] = datePart.split("-").map(Number);
  const [hh, mm] = timePart.split(":").map(Number);
  return new Date(y, m - 1, d, hh, mm);
}

/** An axis tick: the hour on a one-day range, the date on anything wider. */
export function bucketLabel(value: string, granularity: ObservabilityGranularity): string {
  const at = parseBucket(value);
  if (granularity === "hour") return `${String(at.getHours()).padStart(2, "0")}:00`;
  return at.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

/** A tooltip heading: the same instant, said in full. */
export function bucketTitle(value: string, granularity: ObservabilityGranularity): string {
  const at = parseBucket(value);
  const day = at.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  if (granularity === "hour") {
    return `${day}, ${String(at.getHours()).padStart(2, "0")}:00–${String((at.getHours() + 1) % 24).padStart(2, "0")}:00`;
  }
  return at.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
}

/* ── shared chart furniture ───────────────────────────────────────────────── */

const AXIS_TICK = { fill: TOKENS.axis, fontSize: 11 } as const;
const CURSOR = { fill: "rgba(15,15,16,0.035)" } as const;

/* Recharts renders its own tooltip chrome; giving it ours keeps the hover layer
   inside the design system instead of beside it. */
const TOOLTIP_STYLE = {
  contentStyle: {
    borderRadius: 10,
    border: `1px solid ${TOKENS.grid}`,
    boxShadow: "0 6px 20px -6px rgba(12,13,15,0.12)",
    fontSize: 12,
    padding: "8px 10px",
  },
  labelStyle: { color: TOKENS.axis, fontSize: 11, marginBottom: 4 },
  itemStyle: { padding: 0 },
} as const;

/**
 * The frame every chart on this page sits in: a title, the range total beside
 * it, the plot, and — for anything with more than one band — a legend.
 *
 * The legend is not optional furniture. Three of the categorical hues fall below
 * 3:1 against a white panel, so colour alone is not the encoding: the text
 * beside each swatch is what carries identity, and its number is what makes the
 * legend worth reading rather than just decoding.
 */
export function ChartCard({
  title,
  total,
  note,
  legend,
  children,
  className,
}: {
  title: string;
  total?: string;
  note?: string;
  legend?: { key: string; label: string; color: string; value: string }[];
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("rounded-xl border border-line-2 bg-white p-4", className)}>
      <div className="flex items-baseline gap-3">
        <h3 className="min-w-0 flex-1 truncate text-[13px] font-medium leading-5 text-ink">
          {title}
        </h3>
        {total && (
          <span className="flex-none font-display text-[16px] font-semibold leading-5 tracking-tight text-ink tabular-nums">
            {total}
          </span>
        )}
      </div>
      {note && <p className="mt-0.5 text-[11.5px] leading-4 text-faint">{note}</p>}
      <div className="mt-3">{children}</div>
      {legend && legend.length > 0 && (
        <div className="mt-2.5 flex flex-wrap gap-x-3 gap-y-1">
          {legend.map((entry) => (
            <span
              key={entry.key}
              className="inline-flex min-w-0 items-center gap-1.5 text-[11.5px] leading-4 text-muted"
            >
              <span
                className="h-2 w-2 flex-none rounded-[2px]"
                style={{ background: entry.color }}
                aria-hidden
              />
              <span className="truncate">{entry.label}</span>
              <span className="flex-none tabular-nums text-faint">{entry.value}</span>
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

/** Where a plot would be, when there is nothing to plot. */
export function ChartEmpty({
  height = 200,
  children,
}: {
  height?: number;
  children: React.ReactNode;
}) {
  return (
    <div
      style={{ height }}
      className="grid place-items-center rounded-lg border border-dashed border-line-strong px-4 text-center"
    >
      <span className="max-w-[46ch] text-[12.5px] leading-5 text-muted">{children}</span>
    </div>
  );
}

/* ── the workhorse: one stacked bar per day ───────────────────────────────── */

/**
 * A dimension stacked over the range, one bar a day.
 *
 * `color(key, index)` is passed in rather than derived here because the two
 * kinds of dimension want opposite things: an agent or a DID has no meaning of
 * its own and takes the next categorical slot, while a status or an ending
 * bucket means something and must wear the same hue on every chart.
 *
 * Segments carry a 1px surface stroke, which reads as a 2px gap between two
 * touching fills — without it a stack of six is one bar with colour changes in
 * it rather than six quantities.
 */
export function StackedDayChart({
  breakdown,
  color,
  granularity,
  format = "count",
  height = 200,
  ariaLabel,
  empty = "Nothing to show for this range.",
}: {
  breakdown: ObservabilityBreakdownResponse;
  color: (key: string, index: number) => string;
  granularity: ObservabilityGranularity;
  format?: ValueFormat;
  height?: number;
  ariaLabel: string;
  /** Said out loud when the dimension has no values at all — a chart frame with
   *  no marks in it reads as broken rather than as empty. */
  empty?: string;
}) {
  const { series } = breakdown;
  const data = useMemo(() => {
    const stamps = series[0]?.points.map((point) => point.bucket) ?? [];
    return stamps.map((bucket, index) => {
      const row: Record<string, string | number> = { bucket };
      for (const entry of series) row[entry.key] = entry.points[index]?.value ?? 0;
      return row;
    });
  }, [series]);

  const labels = useMemo(
    () => Object.fromEntries(series.map((entry) => [entry.key, entry.label])),
    [series],
  );
  // The tallest stack decides the axis's unit and precision (see axisFormat).
  const max = useMemo(
    () =>
      data.reduce(
        (best, row) =>
          Math.max(
            best,
            series.reduce((sum, entry) => sum + Number(row[entry.key] ?? 0), 0),
          ),
        0,
      ),
    [data, series],
  );

  if (series.length === 0) return <ChartEmpty height={height}>{empty}</ChartEmpty>;

  return (
    <div style={{ height }} className="w-full" aria-label={ariaLabel}>
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={data} margin={{ top: 6, right: 4, left: 0, bottom: 0 }}>
          <CartesianGrid vertical={false} stroke={TOKENS.grid} />
          <XAxis
            dataKey="bucket"
            tickFormatter={(value) => bucketLabel(String(value), granularity)}
            tick={AXIS_TICK}
            tickLine={false}
            axisLine={false}
            minTickGap={28}
          />
          <YAxis
            tickFormatter={axisFormat(format, max)}
            tick={AXIS_TICK}
            tickLine={false}
            axisLine={false}
            allowDecimals={format === "money"}
            width={axisWidth(format, max)}
          />
          <Tooltip
            {...TOOLTIP_STYLE}
            cursor={CURSOR}
            labelFormatter={(label) => bucketTitle(String(label), granularity)}
            formatter={(value, name) => [
              formatValue(Number(value), format),
              labels[String(name)] ?? String(name),
            ]}
          />
          {series.map((entry, index) => (
            <Bar
              key={entry.key}
              dataKey={entry.key}
              name={entry.key}
              stackId="stack"
              fill={color(entry.key, index)}
              stroke="#ffffff"
              strokeWidth={1}
              radius={index === series.length - 1 ? [3, 3, 0, 0] : undefined}
              isAnimationActive={false}
            />
          ))}
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

/* ── one series over the range ────────────────────────────────────────────── */

export function DayBarChart({
  data,
  dataKey,
  label,
  granularity,
  format = "count",
  height = 200,
  ariaLabel,
  color = TOKENS.ink,
}: {
  data: { bucket: string }[];
  dataKey: string;
  label: string;
  granularity: ObservabilityGranularity;
  format?: ValueFormat;
  height?: number;
  ariaLabel: string;
  color?: string;
}) {
  const max = data.reduce(
    (best, row) => Math.max(best, Number((row as Record<string, unknown>)[dataKey] ?? 0)),
    0,
  );
  return (
    <div style={{ height }} className="w-full" aria-label={ariaLabel}>
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={data} margin={{ top: 6, right: 4, left: 0, bottom: 0 }}>
          <CartesianGrid vertical={false} stroke={TOKENS.grid} />
          <XAxis
            dataKey="bucket"
            tickFormatter={(value) => bucketLabel(String(value), granularity)}
            tick={AXIS_TICK}
            tickLine={false}
            axisLine={false}
            minTickGap={28}
          />
          <YAxis
            tickFormatter={axisFormat(format, max)}
            tick={AXIS_TICK}
            tickLine={false}
            axisLine={false}
            width={axisWidth(format, max)}
          />
          <Tooltip
            {...TOOLTIP_STYLE}
            cursor={CURSOR}
            labelFormatter={(value) => bucketTitle(String(value), granularity)}
            formatter={(value) => [formatValue(Number(value), format), label]}
          />
          <Bar
            dataKey={dataKey}
            fill={color}
            radius={[3, 3, 0, 0]}
            isAnimationActive={false}
          />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

/**
 * A rate over the range, drawn as a filled level rather than a bare line.
 *
 * A cache hit rate belongs on a full 0–100% scale — "it holds at 65%" is the
 * finding, and an auto-scaled axis would turn two points of ordinary noise into
 * a dramatic slope. But a thread at 65% of an empty frame reads as no data, so
 * the area under it carries the level and the line carries the movement.
 *
 * A day that priced nothing carries `null`, not `0`, and the shape breaks across
 * it — a zero would draw a day on which the cache missed every single time.
 */
export function DayAreaChart({
  data,
  dataKey,
  label,
  granularity,
  format = "percent",
  height = 200,
  ariaLabel,
  domain,
}: {
  data: { bucket: string }[];
  dataKey: string;
  label: string;
  granularity: ObservabilityGranularity;
  format?: ValueFormat;
  height?: number;
  ariaLabel: string;
  domain?: [number, number];
}) {
  const max = domain
    ? domain[1]
    : data.reduce(
        (best, row) => Math.max(best, Number((row as Record<string, unknown>)[dataKey] ?? 0)),
        0,
      );
  return (
    <div style={{ height }} className="w-full" aria-label={ariaLabel}>
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={data} margin={{ top: 6, right: 8, left: 0, bottom: 0 }}>
          <defs>
            <linearGradient id={`fill-${dataKey}`} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={TOKENS.ink} stopOpacity={0.14} />
              <stop offset="100%" stopColor={TOKENS.ink} stopOpacity={0.02} />
            </linearGradient>
          </defs>
          <CartesianGrid vertical={false} stroke={TOKENS.grid} />
          <XAxis
            dataKey="bucket"
            tickFormatter={(value) => bucketLabel(String(value), granularity)}
            tick={AXIS_TICK}
            tickLine={false}
            axisLine={false}
            minTickGap={28}
          />
          <YAxis
            tickFormatter={axisFormat(format, max)}
            tick={AXIS_TICK}
            tickLine={false}
            axisLine={false}
            width={axisWidth(format, max)}
            domain={domain}
          />
          <Tooltip
            {...TOOLTIP_STYLE}
            labelFormatter={(value) => bucketTitle(String(value), granularity)}
            formatter={(value) => [formatValue(Number(value), format), label]}
          />
          <Area
            type="monotone"
            dataKey={dataKey}
            stroke={TOKENS.ink}
            strokeWidth={2}
            fill={`url(#fill-${dataKey})`}
            dot={{ r: 2, fill: TOKENS.ink, strokeWidth: 0 }}
            activeDot={{ r: 4, fill: TOKENS.ink, strokeWidth: 0 }}
            connectNulls={false}
            isAnimationActive={false}
          />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}

/* ── response time ────────────────────────────────────────────────────────── */

function msTick(value: number): string {
  return value >= 1000 ? `${(value / 1000).toFixed(1)}s` : `${Math.round(value)}ms`;
}

export function LatencyChart({
  buckets,
  metric,
  label,
  unit,
  granularity,
  thresholdMs,
  height = 220,
}: {
  buckets: string[];
  metric: ObservabilityLatencyMetricResponse;
  label: string;
  unit: string;
  granularity: ObservabilityGranularity;
  thresholdMs?: number;
  height?: number;
}) {
  const measured = new Map(metric.points.map((point) => [point.bucket, point]));
  const data = buckets.map((bucket) => ({
    bucket,
    average_ms: measured.get(bucket)?.average_ms ?? null,
    sample_count: measured.get(bucket)?.sample_count ?? 0,
  }));

  return (
    <div style={{ height }} className="w-full" aria-label={`Daily average ${label}`}>
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={data} margin={{ top: 10, right: 30, left: 0, bottom: 0 }}>
          <CartesianGrid vertical={false} stroke={TOKENS.grid} />
          <XAxis
            dataKey="bucket"
            tickFormatter={(value) => bucketLabel(String(value), granularity)}
            tick={AXIS_TICK}
            tickLine={false}
            axisLine={false}
            minTickGap={28}
          />
          <YAxis
            tickFormatter={msTick}
            tick={AXIS_TICK}
            tickLine={false}
            axisLine={false}
            width={54}
          />
          <Tooltip
            {...TOOLTIP_STYLE}
            labelFormatter={(value) => bucketTitle(String(value), granularity)}
            formatter={(value, name, item) => {
              if (name !== "average_ms") return [value, name];
              const samples = Number(item.payload?.sample_count ?? 0);
              return [`${msTick(Number(value))} · ${samples.toLocaleString()} ${unit}`, label];
            }}
          />
          {thresholdMs !== undefined && (
            <ReferenceLine
              y={thresholdMs}
              stroke={TOKENS.danger}
              strokeDasharray="4 4"
              strokeWidth={1}
              // Without this the rule silently vanishes on exactly the ranges
              // where it matters least — and reappears only once something is
              // already slow enough to stretch the axis past it.
              ifOverflow="extendDomain"
              label={{
                value: `${(thresholdMs / 1000).toFixed(1)}s`,
                position: "right",
                fill: TOKENS.danger,
                fontSize: 10,
              }}
            />
          )}
          <Line
            type="monotone"
            dataKey="average_ms"
            name="average_ms"
            stroke={TOKENS.ink}
            strokeWidth={2}
            dot={{ r: 2.5, fill: TOKENS.ink, strokeWidth: 0 }}
            activeDot={{ r: 4, fill: TOKENS.ink, strokeWidth: 0 }}
            connectNulls={false}
            isAnimationActive={false}
          />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

/**
 * How the calls themselves were spread, which a mean cannot show.
 *
 * One series, so no legend — the bars past the threshold wear the status colour
 * and the axis marks where that line falls. This is what stands in for the p95
 * the session bag cannot hold.
 */
export function LatencyDistributionChart({
  buckets,
  thresholdMs,
  height = 200,
}: {
  buckets: ObservabilityLatencyBucketResponse[];
  thresholdMs: number;
  height?: number;
}) {
  const data = buckets.map((bucket) => ({
    label:
      bucket.upper_ms == null
        ? `${(bucket.lower_ms / 1000).toFixed(1)}+`
        : `${(bucket.lower_ms / 1000).toFixed(1)}`,
    range:
      bucket.upper_ms == null
        ? `over ${(bucket.lower_ms / 1000).toFixed(1)}s`
        : `${(bucket.lower_ms / 1000).toFixed(1)}–${(bucket.upper_ms / 1000).toFixed(1)}s`,
    sessions: bucket.sessions,
    slow: bucket.lower_ms >= thresholdMs,
  }));

  return (
    <div style={{ height }} className="w-full" aria-label="Distribution of per-call response time">
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={data} margin={{ top: 10, right: 4, left: 0, bottom: 0 }}>
          <CartesianGrid vertical={false} stroke={TOKENS.grid} />
          <XAxis dataKey="label" tick={AXIS_TICK} tickLine={false} axisLine={false} interval={0} />
          <YAxis allowDecimals={false} tick={AXIS_TICK} tickLine={false} axisLine={false} width={40} />
          <Tooltip
            {...TOOLTIP_STYLE}
            cursor={CURSOR}
            labelFormatter={(_, payload) => payload?.[0]?.payload?.range ?? ""}
            formatter={(value) => [`${Number(value).toLocaleString()} calls`, "In this band"]}
          />
          <Bar dataKey="sessions" radius={[3, 3, 0, 0]} isAnimationActive={false}>
            {data.map((entry) => (
              <Cell key={entry.label} fill={entry.slow ? TOKENS.danger : TOKENS.ink} />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}
