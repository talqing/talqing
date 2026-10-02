"use client";
import { cn } from "@/lib/cn";
import type { LatencyStage } from "@talqing/sdk";
import { fmtMs, STAGE_SWATCH } from "./LatencyStageBar";
import { Tooltip } from "./ui";

export interface TurnLatencyColumn {
  id: string;
  /** 1-based, matching the "Turn N" headings in the transcript below. */
  index: number;
  totalMs: number;
  /** Stage composition; empty when the turn only reported a total. */
  stages: LatencyStage[];
}

const CHART_HEIGHT = "h-[76px]";

/**
 * Response time for every agent turn, oldest first.
 *
 * One column per turn, stacked by the same stages as the average bar above it,
 * so the legend up there reads both. The scale is floored at the slow
 * threshold, which keeps the rule on the chart and makes a fast call read as
 * headroom instead of filling the frame.
 */
export function TurnLatencyChart({
  columns,
  slowMs,
  onJumpToTurn,
  className,
}: {
  columns: TurnLatencyColumn[];
  slowMs: number;
  /** Jump to the turn a column measures. Optional because it depends entirely
   *  on the page: the call detail view renders an anchor per column id, so
   *  there is somewhere to go — the conversations view keys its columns by
   *  message id and renders no such anchor, and a link there went nowhere while
   *  still writing a dead `#<uuid>` into the address bar. Absent, the columns
   *  are inert. */
  onJumpToTurn?: (id: string) => void;
  className?: string;
}) {
  if (columns.length === 0) return null;
  const slowest = columns.reduce((a, b) => (b.totalMs > a.totalMs ? b : a));
  const scale = Math.max(slowest.totalMs, slowMs);
  const thresholdOffset = `${(slowMs / scale) * 100}%`;
  const slowCount = columns.filter((c) => c.totalMs > slowMs).length;
  const slowLabel = fmtMs(slowMs);

  const detail = (c: TurnLatencyColumn) =>
    [
      `Turn ${c.index}`,
      fmtMs(c.totalMs),
      c.stages.length > 0
        ? c.stages.map((s) => `${s.label} ${fmtMs(s.ms)}`).join(" / ")
        : "no stage breakdown recorded",
      c.totalMs > slowMs ? `over ${slowLabel}` : "",
    ]
      .filter(Boolean)
      .join(" · ");

  return (
    <div className={cn("grid gap-2", className)}>
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-0.5">
        <span className="text-[11px] font-medium text-muted">Response time by turn</span>
        <span className="text-[11px] tabular-nums text-faint">
          {columns.length} {columns.length === 1 ? "turn" : "turns"} · slowest{" "}
          {fmtMs(slowest.totalMs)} at turn {slowest.index}
          {slowCount > 0 ? ` · ${slowCount} over ${slowLabel}` : ""}
        </span>
      </div>
      <div className="flex items-start gap-2">
        <div className={cn("relative w-9 flex-none", CHART_HEIGHT)} aria-hidden>
          <span
            className="absolute right-0 translate-y-1/2 text-[10px] tabular-nums text-faint"
            style={{ bottom: thresholdOffset }}
          >
            {slowLabel}
          </span>
        </div>
        <div className="min-w-0 flex-1 overflow-x-auto scroll-thin">
          <div
            role="img"
            aria-label={`Response time for ${columns.length} agent ${
              columns.length === 1 ? "turn" : "turns"
            } in order. Slowest ${fmtMs(slowest.totalMs)} at turn ${
              slowest.index
            }. ${slowCount} over ${slowLabel}.`}
            className={cn(
              "relative flex w-max min-w-full items-end gap-[2px] border-b border-line",
              CHART_HEIGHT,
            )}
          >
            <span
              aria-hidden
              className="pointer-events-none absolute inset-x-0 border-t border-dashed border-line-strong"
              style={{ bottom: thresholdOffset }}
            />
            {/* Rounding and clipping live on the column so its height always
                states the real duration, however many stages fit inside.

                The app's own Tooltip, not `title`: a native one waits about a
                second before it appears and renders in the OS style, which on a
                chart you are sweeping across reads as broken.

                A column carries you to the turn it measures — the doc comment
                on `index` has always promised these match the "Turn N" headings
                below, and without this a reader who spots the tall bar still
                has to go and count. A button rather than an anchor, because the
                jump also marks the turn on arrival: several fit on one screen,
                so landing with no marker leaves you guessing which one you came
                for, and a bare `#hash` cannot say.

                Neither the Tooltip (`focusable`) nor the column (`tabIndex`)
                takes a tab stop: one per turn would make a sixty-turn call
                unnavigable. Both routes are redundant — the chart's aria-label
                summarises it and the transcript below is the navigable copy. */}
            {columns.map((c) => (
              <Tooltip
                key={c.id}
                label={detail(c)}
                focusable={false}
                className="h-full min-w-[10px] max-w-[26px] flex-1 basis-0 flex-col justify-end"
              >
                <button
                  type="button"
                  tabIndex={-1}
                  aria-hidden
                  disabled={!onJumpToTurn}
                  onClick={onJumpToTurn ? () => onJumpToTurn(c.id) : undefined}
                  className={cn(
                    "flex w-full flex-col-reverse gap-[2px] overflow-hidden rounded-t-[4px]",
                    onJumpToTurn && "cursor-pointer transition-opacity hover:opacity-70",
                  )}
                  style={{ height: `${(c.totalMs / scale) * 100}%` }}
                >
                  {c.stages.length === 0 ? (
                    <span className="min-h-[2px] flex-1 bg-line-strong" />
                  ) : (
                    c.stages.map((s, i) => (
                      <span
                        key={i}
                        className={cn("min-h-[2px] basis-0", STAGE_SWATCH[s.kind])}
                        style={{ flexGrow: s.ms }}
                      />
                    ))
                  )}
                </button>
              </Tooltip>
            ))}
          </div>
          {slowCount > 0 && (
            <div className="flex w-max min-w-full gap-[2px] pt-1">
              {columns.map((c) => (
                <span key={c.id} className="flex min-w-[10px] max-w-[26px] flex-1 basis-0 justify-center">
                  {/* No tooltip of its own: a 6px target is a poor one, and the
                      column above already says which turn and that it is over
                      the threshold. */}
                  {c.totalMs > slowMs && <span className="h-1.5 w-1.5 rounded-full bg-danger" />}
                </span>
              ))}
            </div>
          )}
        </div>
      </div>
      <p className="text-[11px] leading-4 text-faint">
        One column per agent turn, oldest first
        {slowCount > 0 ? `; a red dot marks a turn over ${slowLabel}` : ""}. Hover a column for its
        exact stage times{onJumpToTurn ? ", or click it to jump to that turn" : ""}.
      </p>
    </div>
  );
}
