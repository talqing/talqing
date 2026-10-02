"use client";
import type { HealthSnapshotResponse } from "@talqing/sdk";
import { cn } from "@/lib/cn";
import { fmtMs, LatencyStageBar } from "./LatencyStageBar";
import { ProportionBar } from "./ProportionBar";
import { Tooltip } from "./ui";
import { TurnLatencyChart, type TurnLatencyColumn } from "./TurnLatencyChart";
import type { IssueTargets } from "./timeline/exchanges";

/* The health verdict shared by a single call and a whole conversation — same
   payload, same reading, so the two surfaces cannot drift. */

export const fmtCount = (v: number | null | undefined) =>
  v == null ? "—" : Math.round(v).toLocaleString();

export function fmtDuration(seconds: number | null | undefined): string {
  if (seconds == null) return "—";
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}

/* Cost is null until it is priced. `—` says "not known yet"; `$0.00` would
   claim the call was free. */
export function fmtCost(v: number | null | undefined): string {
  if (v == null) return "—";
  return v >= 1 ? `$${v.toFixed(2)}` : `$${v.toFixed(4)}`;
}

/**
 * One labelled number.
 *
 * `detail` is a tooltip rather than a third line. It is a gloss on the number —
 * what "avg response" measures, what the count is out of — and printing it under
 * every tile made a row of six read as eighteen things, while the longest ones
 * ("caller stops → agent replies") were truncated to nonsense anyway. The app's
 * own Tooltip rather than a native `title`: it opens on keyboard focus and is
 * wired with aria-describedby, so the gloss is not mouse-only.
 */
function Tile({ label, value, detail }: { label: string; value: string; detail?: string }) {
  const body = (
    <div className="grid w-full content-start gap-1 px-4 py-3">
      <div className="truncate text-[11px] font-medium leading-4 text-muted">{label}</div>
      <div className="font-mono text-[14px] font-semibold leading-5 tabular-nums text-ink">
        {value}
      </div>
    </div>
  );
  if (!detail) return body;
  return (
    <Tooltip label={detail} className="w-full">
      {body}
    </Tooltip>
  );
}

const QUALITY_LABEL: Record<HealthSnapshotResponse["connection_quality"], string> = {
  unknown: "—",
  excellent: "Excellent",
  good: "Good",
  poor: "Poor",
  lost: "Lost",
};

/* Which side the worst reading belonged to. "Poor" on the caller's own wifi and
   "poor" on ours are opposite findings, and a tile that cannot tell them apart
   sends the reader to support for a problem that was never ours. */
function connectionDetail(
  connection: HealthSnapshotResponse["connection"],
  overall: HealthSnapshotResponse["connection_quality"],
): string | undefined {
  if (overall === "unknown") return "not reported";
  const worst = [
    connection.agent === overall && "our side",
    connection.caller === overall && "the caller's side",
  ].filter(Boolean);
  return worst.length ? `worst on ${worst.join(" and ")}` : "side not recorded";
}

/**
 * How the call *sounded*: monologues, dead air, talk-over, hold, and the two
 * speech-pipeline misfires that are not platform faults.
 *
 * Separate from the latency block above on purpose. That one measures our
 * stack; this one measures the caller's experience of the conversation, and the
 * two disagree constantly — a 300ms response time is no comfort on a call whose
 * agent then talked for twenty-seven seconds.
 *
 * Every tile here is absent on a call that has nothing to say, and the whole
 * strip disappears rather than drawing a row of dashes.
 */
function PaceStrip({ snapshot }: { snapshot: HealthSnapshotResponse }) {
  const { pace } = snapshot;
  const tiles: [string, string, string][] = [];
  /* The longest thing the agent said without stopping. Nothing else on the page
     measures it: the per-turn "spoke for 9.67 s" chip is there, but finding the
     worst one means reading every bubble on a sixty-turn call, and a monologue
     is exactly the defect a reader cannot spot by scrolling. It was dropped on
     the argument that the transcript states it per turn — which is true and is
     why the number is not enough on its own, not a reason to omit it.
     No "longest silence" beside it: that was the same subtraction as
     `e2e_latency`, and the response-time chart already marks its worst turn. */
  if (pace.longest_agent_turn_ms != null) {
    tiles.push([
      "Longest agent turn",
      fmtMs(pace.longest_agent_turn_ms),
      "the most the agent said without stopping",
    ]);
  }
  if (pace.talk_over_turns > 0) {
    tiles.push([
      "Talk-over",
      fmtCount(pace.talk_over_turns),
      pace.talk_over_turns === 1 ? "turn spoken over" : "turns spoken over",
    ]);
  }
  if (pace.hold_ms != null) {
    tiles.push(["On hold", fmtMs(pace.hold_ms), "hold music during the transfer"]);
  }
  if (snapshot.false_interruptions > 0) {
    tiles.push([
      "False interruptions",
      fmtCount(snapshot.false_interruptions),
      `${snapshot.false_interruptions_resumed} resumed on their own`,
    ]);
  }
  if (tiles.length === 0) return null;
  /* One track per tile. The strip renders between one and four of them, and a
     fixed five-track grid left the row blank on the right. Spelled out because
     Tailwind cannot see a class name assembled at runtime. */
  const columns = [
    "",
    "sm:grid-cols-1",
    "sm:grid-cols-2",
    "sm:grid-cols-3",
    "sm:grid-cols-2 lg:grid-cols-4",
  ][tiles.length];
  return (
    <div className={cn("grid border-t border-line [&>*+*]:border-l [&>*+*]:border-line", columns)}>
      {tiles.map(([label, value, detail]) => (
        <Tile key={label} label={label} value={value} detail={detail} />
      ))}
    </div>
  );
}

export function SnapshotBand({
  snapshot,
  columns,
  durationLabel = "Call length",
  showMetering = true,
  onJumpToTurn,
  issueTarget,
  className,
}: {
  snapshot: HealthSnapshotResponse;
  columns: TurnLatencyColumn[];
  durationLabel?: string;
  /** Scroll to a turn or a moment in the transcript below. Only the call detail
   *  view renders the anchors a jump needs, so it is the only caller that
   *  supplies one. */
  onJumpToTurn?: (id: string) => void;
  /** Where each kind of issue first shows in that transcript. An issue with an
   *  entry gets a "Show" link. */
  issueTarget?: IssueTargets;
  /** Tokens, TTS characters and the token mix. Off where a priced cost ledger
   *  sits below and states the same quantities BESIDE the rate each was charged
   *  at — two views of one fact, and only one of them answers a question. The
   *  conversations page has no such ledger, so it keeps these. */
  showMetering?: boolean;
  className?: string;
}) {
  const slowMs = snapshot.slow_response_threshold_ms;
  const health: [string, string, string?][] = [
    [
      "Cost",
      fmtCost(snapshot.total_cost_usd),
      snapshot.billing_status === "computed" ? undefined : snapshot.billing_status,
    ],
    ["Avg response", fmtMs(snapshot.response_latency.avg_ms), "caller stops → agent replies"],
    [
      "P95 response",
      fmtMs(snapshot.response_latency.p95_ms),
      `over ${snapshot.response_latency.samples} ${
        snapshot.response_latency.samples === 1 ? "turn" : "turns"
      }`,
    ],
    [
      "Interruptions",
      fmtCount(snapshot.interruptions),
      `${fmtCount(snapshot.uninterrupted_responses)} uninterrupted`,
    ],
    [
      "Tool calls",
      snapshot.tool_calls === 0
        ? "—"
        : `${snapshot.tool_calls - snapshot.tool_call_failures}/${snapshot.tool_calls} ok`,
      /* "ok" here means the tool ran and reported no error — it cannot mean the
         tool did what the caller wanted, because a `{"success": false}` body is
         the tool's own vocabulary and not ours. Saying so stops "4/4 ok" from
         reading as a verdict on a call the analysis judged a failure. Nothing
         to gloss on a call that called no tools. */
      snapshot.tool_calls === 0
        ? undefined
        : [
            snapshot.tool_call_failures > 0 ? `${snapshot.tool_call_failures} errored` : null,
            snapshot.unfinished_tool_calls > 0
              ? `${snapshot.unfinished_tool_calls} never finished`
              : null,
            "ran without raising an error",
          ]
            .filter(Boolean)
            .join(" · "),
    ],
    [
      "Connection",
      QUALITY_LABEL[snapshot.connection_quality],
      connectionDetail(snapshot.connection, snapshot.connection_quality),
    ],
  ];

  // A text conversation has no speech at all; showing four "—" audio tiles
  // would be noise rather than information.
  const hasAudio = snapshot.total_audio_ms > 0;
  const audio: [string, string, string][] = [
    [durationLabel, fmtDuration(snapshot.duration_seconds), "wall-clock"],
    ["Caller speech", fmtMs(snapshot.stt_audio_ms), "provider-reported STT audio"],
    ["Agent audio", fmtMs(snapshot.tts_audio_ms), "provider-reported TTS audio"],
    ["Combined", fmtMs(snapshot.total_audio_ms), "caller + agent"],
    // Rehomed from the token row below, which has gone: a message count is not
    // a metered quantity and had no business sitting with the token tiles.
    [
      "Messages",
      fmtCount(snapshot.user_messages + snapshot.agent_messages),
      `${snapshot.user_messages} caller · ${snapshot.agent_messages} agent`,
    ],
  ];

  const avgResponseMs = snapshot.response_latency.avg_ms;

  const freshInput = Math.max(snapshot.input_tokens - snapshot.cached_input_tokens, 0);

  return (
    <div className={cn("overflow-hidden rounded-xl border border-line-2 bg-white", className)}>
      <div
        className={cn(
          "flex items-start gap-2.5 px-5 py-3 text-[13px]",
          snapshot.ok ? "bg-live/[0.05] text-live" : "bg-danger/[0.05] text-danger",
        )}
      >
        <span className="mt-1.5 h-1.5 w-1.5 flex-none rounded-full bg-current" aria-hidden />
        {snapshot.ok ? (
          <p className="font-medium">
            Everything looks OK — no errors, failed tools, slow responses or connection problems.
          </p>
        ) : (
          <ul className="grid gap-1 font-medium">
            {snapshot.issues.map((issue) => {
              const target = issueTarget?.[issue.kind];
              return (
                <li key={issue.kind + issue.message}>
                  {issue.message}
                  {target && onJumpToTurn && (
                    <button
                      type="button"
                      onClick={() => onJumpToTurn(target)}
                      className="ml-2 font-normal underline underline-offset-2"
                    >
                      Show
                    </button>
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </div>

      <div className="grid border-t border-line sm:grid-cols-3 lg:grid-cols-6 [&>*+*]:border-l [&>*+*]:border-line">
        {health.map(([label, value, detail]) => (
          <Tile key={label} label={label} value={value} detail={detail} />
        ))}
      </div>

      {avgResponseMs != null && (
        <div className="grid gap-4 border-t border-line px-5 py-4">
          <LatencyStageBar
            label="Average response time"
            totalMs={avgResponseMs}
            slowMs={slowMs}
            meta={`${snapshot.response_latency.samples} ${
              snapshot.response_latency.samples === 1 ? "turn" : "turns"
            }`}
            stages={snapshot.response_stages}
          />
          <TurnLatencyChart columns={columns} slowMs={slowMs} onJumpToTurn={onJumpToTurn} />
        </div>
      )}

      <PaceStrip snapshot={snapshot} />

      {hasAudio ? (
        <>
          <div className="grid border-t border-line sm:grid-cols-2 lg:grid-cols-5 [&>*+*]:border-l [&>*+*]:border-line">
            {audio.map(([label, value, detail]) => (
              <Tile key={label} label={label} value={value} detail={detail} />
            ))}
          </div>
          <ProportionBar
            className="border-t border-line px-5 py-3"
            label="Speech balance"
            ariaLabel="Share of spoken audio"
            segments={[
              {
                key: "caller",
                label: "Caller",
                value: snapshot.stt_audio_ms,
                swatch: "bg-chart-stt",
                display: fmtMs(snapshot.stt_audio_ms),
              },
              {
                key: "agent",
                label: "Agent",
                value: snapshot.tts_audio_ms,
                swatch: "bg-chart-tts",
                display: fmtMs(snapshot.tts_audio_ms),
              },
            ]}
          />
        </>
      ) : (
        <div className="grid border-t border-line sm:grid-cols-2 [&>*+*]:border-l [&>*+*]:border-line">
          <Tile label={durationLabel} value={fmtDuration(snapshot.duration_seconds)} detail="wall-clock" />
          <Tile
            label="Messages"
            value={fmtCount(snapshot.user_messages + snapshot.agent_messages)}
            detail={`${snapshot.user_messages} caller · ${snapshot.agent_messages} agent`}
          />
        </div>
      )}

      {/* Messages is NOT here — it moved up to the speech row, because a
          message count is not a metered quantity. */}
      {showMetering && (
        <>
          <div className="grid border-t border-line sm:grid-cols-2 lg:grid-cols-4 [&>*+*]:border-l [&>*+*]:border-line">
            <Tile label="Input tokens" value={fmtCount(snapshot.input_tokens)} />
            <Tile label="Cached input" value={fmtCount(snapshot.cached_input_tokens)} />
            <Tile label="Output tokens" value={fmtCount(snapshot.output_tokens)} />
            <Tile label="TTS characters" value={fmtCount(snapshot.tts_characters)} />
          </div>
          <ProportionBar
            className="border-t border-line px-5 py-3"
            label="Token mix"
            ariaLabel="Share of model tokens"
            segments={[
              {
                key: "fresh",
                label: "Fresh input",
                value: freshInput,
                swatch: "bg-chart-llm",
                display: fmtCount(freshInput),
              },
              {
                key: "cached",
                label: "Cached input",
                value: snapshot.cached_input_tokens,
                swatch: "bg-chart-llm/45",
                display: fmtCount(snapshot.cached_input_tokens),
              },
              {
                key: "output",
                label: "Output",
                value: snapshot.output_tokens,
                swatch: "bg-chart-realtime",
                display: fmtCount(snapshot.output_tokens),
              },
            ]}
          />
          {snapshot.providers.length > 0 && (
            <p className="border-t border-line px-5 py-2.5 text-[11px] text-faint">
              Reported by {snapshot.providers.join(" · ")}
            </p>
          )}
        </>
      )}
    </div>
  );
}
