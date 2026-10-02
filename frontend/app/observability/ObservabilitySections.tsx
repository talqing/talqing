"use client";

import { useMemo, useState } from "react";
import Link from "next/link";
import type {
  ObservabilityAgentResponse,
  ObservabilityCloseReasonBucket,
  ObservabilityCostLineResponse,
  ObservabilityCostResponse,
  ObservabilityEndingResponse,
} from "@talqing/sdk";
import { cn } from "@/lib/cn";
import { CardHead, Panel } from "@/app/components/ui";
import { CLOSE_REASON_COLORS } from "@/lib/chartColors";

/* ── shared formatting ────────────────────────────────────────────────────── */

const count = (value: number) => value.toLocaleString();

export function money(value: number): string {
  if (value === 0) return "$0";
  return value < 0.01 ? `$${value.toFixed(4)}` : `$${value.toFixed(2)}`;
}

export function percent(value: number | null | undefined): string {
  return value == null ? "—" : `${(value * 100).toFixed(1)}%`;
}

export function duration(seconds: number | null | undefined): string {
  if (seconds == null) return "—";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ${Math.round(seconds % 60)}s`;
  const hours = Math.floor(minutes / 60);
  const remainder = minutes % 60;
  return remainder ? `${hours}h ${remainder}m` : `${hours}h`;
}

export function latency(value: number | null | undefined): string {
  if (value == null) return "—";
  return value >= 1000 ? `${(value / 1000).toFixed(2)}s` : `${Math.round(value)}ms`;
}

/** A close reason as a sentence fragment: `sip_media_failure_failed` → "Sip media failure". */
function readableReason(reason: string | null | undefined): string {
  if (!reason) return "No reason recorded";
  const words = reason.replace(/_failed$/, "").replace(/_/g, " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}

function Empty({ children }: { children: React.ReactNode }) {
  return (
    <div className="grid place-items-center rounded-lg border border-dashed border-line-strong px-4 py-8 text-center text-[13px] text-muted">
      <span className="max-w-[46ch]">{children}</span>
    </div>
  );
}

/* ── the raw endings, beside the bucketed chart ───────────────────────────── */

const BUCKET_LABEL: Record<ObservabilityCloseReasonBucket, string> = {
  normal: "Ended normally",
  transferred: "Transferred",
  caller_unreachable: "Caller unreachable",
  carrier_fault: "Carrier fault",
  configuration: "Configuration",
  platform: "Platform",
  other: "Unrecognised",
};

/**
 * Every distinct ending, ranked, each row a link into the calls behind it.
 *
 * The chart beside this one stacks the seven owner buckets, because twenty SIP
 * failure strings is not a readable chart. This is the other half: the raw
 * string, which is what you grep the logs for and what support will ask you
 * for. A reason no version of the platform has mapped keeps its raw string and
 * shows up here rather than vanishing into a bucket that swallowed it.
 */
export function EndingsList({
  endings,
  callsHref,
}: {
  endings: ObservabilityEndingResponse[];
  callsHref: (params: Record<string, string>) => string;
}) {
  const total = endings.reduce((sum, ending) => sum + ending.sessions, 0);

  return (
    <Panel className="p-4">
      <div className="mb-3 flex items-baseline gap-3">
        <h3 className="min-w-0 flex-1 truncate text-[13px] font-medium leading-5 text-ink">
          Every ending, in full
        </h3>
        <span className="flex-none text-[11.5px] text-faint tabular-nums">
          {count(total)} ended
        </span>
      </div>
      {total === 0 ? (
        <Empty>No session in this range has ended yet.</Empty>
      ) : (
        <div className="grid gap-px">
          {endings.map((ending) => (
            <Link
              key={`${ending.bucket}-${ending.close_reason ?? "none"}`}
              href={callsHref(
                ending.close_reason ? { close_reason: ending.close_reason } : {},
              )}
              className="group flex items-baseline gap-2 rounded-md px-1.5 py-1 transition-colors hover:bg-hover"
              title={`${BUCKET_LABEL[ending.bucket]} · open these calls`}
            >
              <span
                className="mt-[3px] h-2 w-2 flex-none self-start rounded-[2px]"
                style={{ background: CLOSE_REASON_COLORS[ending.bucket] }}
                aria-hidden
              />
              <span className="truncate text-[12.5px] text-ink-soft group-hover:text-ink">
                {readableReason(ending.close_reason)}
              </span>
              {ending.failed_sessions > 0 && (
                <span className="flex-none text-[11px] text-danger">failed</span>
              )}
              <span className="ml-auto flex-none font-mono text-[12.5px] tabular-nums text-ink">
                {count(ending.sessions)}
              </span>
              <span className="w-10 flex-none text-right font-mono text-[11.5px] tabular-nums text-placeholder">
                {Math.round((ending.sessions / total) * 100)}%
              </span>
            </Link>
          ))}
        </div>
      )}
      <p className="mt-3 text-[11.5px] leading-4 text-faint">
        A caller who rejects the call or is busy is closed by LiveKit before we can name it and
        arrives as an ordinary hangup — so no-answer is visible here and rejected is not.
      </p>
    </Panel>
  );
}

/* ── cost by model ────────────────────────────────────────────────────────── */

/* The stage hues from tailwind.config.ts, validated all-pairs for colour
   blindness. Post-call analysis wears bronze because it is not a stage of the
   call, and the platform fee wears violet because it is not a provider at all. */
function lineSwatch(line: ObservabilityCostLineResponse): string {
  if (line.kind === "llm" && line.purpose === "analysis") return "bg-chart-analysis";
  return {
    llm: "bg-chart-llm",
    stt: "bg-chart-stt",
    tts: "bg-chart-tts",
    realtime: "bg-chart-realtime",
    avatar: "bg-chart-avatar",
  }[line.kind];
}

const KIND_LABEL: Record<ObservabilityCostLineResponse["kind"], string> = {
  llm: "LLM",
  stt: "STT",
  tts: "TTS",
  realtime: "Realtime",
  avatar: "Avatar",
};

/**
 * Which model the money went to — the question the per-provider chart cannot
 * answer, because one provider serves several models at different rates.
 *
 * Ranked across all five stages rather than grouped by stage: the ranking IS the
 * finding. "TTS is 57% of your provider cost" is not visible in a list that
 * always puts LLM first.
 */
export function CostLedger({ cost }: { cost: ObservabilityCostResponse }) {
  return (
    <Panel className="p-4">
      <div className="mb-3 flex items-baseline gap-3">
        <h3 className="min-w-0 flex-1 truncate text-[13px] font-medium leading-5 text-ink">
          Cost by model
        </h3>
        <span className="flex-none font-display text-[16px] font-semibold leading-5 tracking-tight text-ink tabular-nums">
          {money(cost.total_cost)}
        </span>
      </div>

      {cost.lines.length === 0 ? (
        <Empty>No call in this range has been priced yet.</Empty>
      ) : (
        <div className="grid gap-px">
          {cost.lines.map((line, index) => (
            <div
              key={`${line.kind}-${line.provider}-${line.model}-${index}`}
              className="flex items-baseline gap-2 px-1.5 py-1"
            >
              <span
                className={cn("mt-[3px] h-2 w-2 flex-none self-start rounded-[2px]", lineSwatch(line))}
                aria-hidden
              />
              <span className="flex-none font-mono text-[10.5px] uppercase tracking-[0.06em] text-placeholder">
                {KIND_LABEL[line.kind]}
              </span>
              <span className="truncate font-mono text-[12.5px] text-ink">{line.model}</span>
              <span className="flex-none text-[11.5px] text-faint">{line.provider}</span>
              {line.purpose === "analysis" && <Tag>analysis</Tag>}
              {line.priority && <Tag>priority</Tag>}
              <span className="ml-auto flex-none font-mono text-[12.5px] tabular-nums text-ink">
                {money(line.cost)}
              </span>
              <span className="w-10 flex-none text-right font-mono text-[11.5px] tabular-nums text-placeholder">
                {Math.round(line.share * 100)}%
              </span>
            </div>
          ))}
          <div className="mt-1 flex items-baseline gap-2 border-t border-line px-1.5 pt-1.5">
            <span className="h-2 w-2 flex-none rounded-[2px] bg-chart-platform" aria-hidden />
            <span className="text-[12.5px] text-muted">Platform fee</span>
            <span className="ml-auto flex-none font-mono text-[12.5px] tabular-nums text-ink">
              {money(cost.platform_fee)}
            </span>
            <span className="w-10 flex-none text-right font-mono text-[11.5px] tabular-nums text-placeholder">
              {cost.total_cost > 0
                ? `${Math.round((cost.platform_fee / cost.total_cost) * 100)}%`
                : "—"}
            </span>
          </div>
        </div>
      )}

      <p className="mt-3 text-[11.5px] leading-4 text-faint">
        Over {count(cost.priced_sessions)} priced sessions. Shares are of the total charge, so the
        platform fee is the segment the provider lines leave over.
      </p>

      {cost.unpriceable_reasons.length > 0 && (
        <div className="mt-3 rounded-lg border border-danger/25 bg-danger/[0.04] p-2.5">
          <div className="text-[12px] font-medium text-danger">
            Billing could not price{" "}
            {count(cost.unpriceable_reasons.reduce((sum, r) => sum + r.sessions, 0))} session(s)
          </div>
          <ul className="mt-1 grid gap-0.5">
            {cost.unpriceable_reasons.map((reason) => (
              <li key={reason.message ?? "unknown"} className="flex items-baseline gap-2">
                <span className="min-w-0 flex-1 break-words font-mono text-[11px] leading-4 text-ink-soft">
                  {reason.message ?? "no message recorded"}
                </span>
                <span className="flex-none font-mono text-[11.5px] tabular-nums text-ink-soft">
                  {count(reason.sessions)}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </Panel>
  );
}

function Tag({ children }: { children: string }) {
  return (
    <span className="flex-none rounded border border-line-2 px-1 py-px font-mono text-[9.5px] uppercase tracking-[0.06em] text-muted">
      {children}
    </span>
  );
}

/* ── agents ───────────────────────────────────────────────────────────────── */

type AgentSort = "sessions" | "success_rate" | "total_cost" | "average_e2e_latency_ms";

const AGENT_COLUMNS: { key: AgentSort; label: string }[] = [
  { key: "sessions", label: "Sessions" },
  { key: "success_rate", label: "Success" },
  { key: "average_e2e_latency_ms", label: "Response" },
  { key: "total_cost", label: "Cost" },
];

/**
 * One row per agent, in the columns a stacked volume chart cannot carry.
 *
 * The chart above answers "how much traffic did each agent take"; this answers
 * "and which of them is the problem" — success rate, response time and cost per
 * call side by side, sortable by whichever of those you came here about.
 */
export function AgentsTable({
  agents,
  latencyLabel,
}: {
  agents: ObservabilityAgentResponse[];
  latencyLabel: string;
}) {
  const [sort, setSort] = useState<AgentSort>("sessions");
  const sorted = useMemo(
    () =>
      [...agents].sort((a, b) => {
        const left = a[sort];
        const right = b[sort];
        if (left == null && right == null) return 0;
        if (left == null) return 1;
        if (right == null) return -1;
        return right - left;
      }),
    [agents, sort],
  );

  return (
    <Panel>
      <CardHead
        title="By agent"
        desc={`Success is over the calls post-call analysis judged, shown after the slash. Response is ${latencyLabel}.`}
      />
      {sorted.length === 0 ? (
        <Empty>No sessions ran in this range.</Empty>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[680px] border-collapse text-left">
            <thead>
              <tr className="border-b border-line text-[11px] uppercase tracking-[0.06em] text-muted">
                <th className="py-2 pr-3 font-medium">Agent</th>
                <th className="px-3 py-2 font-medium">Channel</th>
                {AGENT_COLUMNS.map((column) => (
                  <th key={column.key} className="px-3 py-2 text-right font-medium">
                    <button
                      type="button"
                      onClick={() => setSort(column.key)}
                      className={cn(
                        "inline-flex items-center gap-1 uppercase tracking-[0.06em] transition-colors hover:text-ink",
                        sort === column.key && "text-ink",
                      )}
                      aria-pressed={sort === column.key}
                    >
                      {column.label}
                      {sort === column.key && <span aria-hidden>↓</span>}
                    </button>
                  </th>
                ))}
                <th className="px-3 py-2 text-right font-medium">Completion</th>
                <th className="py-2 pl-3 text-right font-medium">Avg length</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-line">
              {sorted.map((row) => (
                <tr key={`${row.agent_id}-${row.channel}`} className="transition-colors hover:bg-hover">
                  <td className="max-w-[220px] truncate py-2.5 pr-3 text-[13px] font-medium text-ink">
                    {row.agent_name ?? "Deleted agent"}
                  </td>
                  <td className="px-3 py-2.5 text-[12.5px] capitalize text-muted">{row.channel}</td>
                  <td className="px-3 py-2.5 text-right font-mono text-[12.5px] tabular-nums text-ink">
                    {count(row.sessions)}
                  </td>
                  <td className="px-3 py-2.5 text-right font-mono text-[12.5px] tabular-nums text-ink">
                    {row.judged_sessions === 0 ? (
                      <span className="text-placeholder" title="No call was judged">
                        —
                      </span>
                    ) : (
                      <>
                        {percent(row.success_rate)}
                        <span className="ml-1 text-[11px] text-placeholder">
                          /{count(row.judged_sessions)}
                        </span>
                      </>
                    )}
                  </td>
                  <td className="px-3 py-2.5 text-right font-mono text-[12.5px] tabular-nums text-ink">
                    {latency(row.average_e2e_latency_ms)}
                  </td>
                  <td className="px-3 py-2.5 text-right font-mono text-[12.5px] tabular-nums text-ink">
                    {money(row.total_cost)}
                    {row.average_cost_per_session != null && (
                      <span className="ml-1 text-[11px] text-placeholder">
                        {money(row.average_cost_per_session)}/call
                      </span>
                    )}
                  </td>
                  <td className="px-3 py-2.5 text-right font-mono text-[12.5px] tabular-nums text-ink">
                    {percent(row.completion_rate)}
                  </td>
                  <td className="py-2.5 pl-3 text-right font-mono text-[12.5px] tabular-nums text-ink">
                    {duration(row.average_session_duration_seconds)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  );
}
