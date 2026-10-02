"use client";
import type { LatencyStage } from "@talqing/sdk";
import { cn } from "@/lib/cn";
import { Tooltip } from "./ui";

/**
 * Where a wait's time went, stage by stage.
 *
 * The stages are computed on the server (`utils/latency.py::reply_breakdowns`)
 * and drawn as given, in the order they happened: the caller stops talking, we
 * transcribe (STT), we decide the turn is over (Endpoint), the model may call
 * tools before it says anything (LLM → tool call, Tool), then it starts
 * producing the reply (LLM) and the first audio comes back (TTS). This
 * component adds nothing of its own — the same list feeds the health band's
 * averages, so a bar and the band above it cannot disagree.
 */

type StageKind = LatencyStage["kind"];

/* Two legs reuse a neighbour's hue at reduced opacity rather than claiming one:
   Endpoint is the STT leg split in two, and "LLM → tool call" is the model
   again. `tailwind.config.ts` explains why a new hue is expensive; the tool's
   own was validated against the other three. */
export const STAGE_SWATCH: Record<StageKind, string> = {
  stt: "bg-chart-stt",
  endpoint: "bg-chart-stt/45",
  hook: "bg-chart-tool/45",
  llm_step: "bg-chart-llm/45",
  tool: "bg-chart-tool",
  llm: "bg-chart-llm",
  tts: "bg-chart-tts",
  other: "bg-line-strong",
};

const HINT: Record<StageKind, string> = {
  stt: "Speech to text: the caller stops talking until the final transcript arrives",
  endpoint:
    "After the transcript: waiting for turn detection to agree the caller is done. Tune the endpointing delay to shrink this.",
  hook: "This agent's On user turn hook, which runs before the model is asked for a reply",
  llm_step: "The model deciding to call a tool, before it had said anything",
  tool: "Waiting for the tool to return",
  llm: "The model: asked for the reply until its first token",
  tts: "Text to speech: first token until the first byte of audio",
  other: "Scheduling, network and buffering between the stages above",
};

export function fmtMs(ms: number | null | undefined): string {
  if (ms == null) return "—";
  return ms >= 1000 ? `${(ms / 1000).toFixed(2)} s` : `${Math.round(ms)} ms`;
}

export function LatencyStageBar({
  label,
  totalMs,
  stages,
  meta,
  slowMs,
  bare,
  className,
}: {
  label: string;
  totalMs: number;
  stages: LatencyStage[];
  meta?: React.ReactNode;
  slowMs: number;
  /* Only the bar and its legend, where the caller already states the total. */
  bare?: boolean;
  className?: string;
}) {
  // Stages that overlapped (preemptive generation, a tool that outlasted the
  // line spoken over it) can add up to more than the wait itself.
  const scale = Math.max(totalMs, stages.reduce((sum, s) => sum + s.ms, 0));
  const slowest =
    stages.length > 1 ? stages.reduce((a, b) => (b.ms > a.ms ? b : a)) : null;

  return (
    <div className={cn("grid gap-1.5", className)}>
      {!bare && (
        <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-0.5">
          <span className="text-[11px] font-medium text-muted">{label}</span>
          <span className="flex items-baseline gap-2">
            {meta && <span className="text-[11px] text-faint">{meta}</span>}
            <strong
              className={cn(
                "font-mono text-[13px] font-semibold tabular-nums",
                totalMs > slowMs ? "text-danger" : "text-ink",
              )}
            >
              {fmtMs(totalMs)}
            </strong>
          </span>
        </div>
      )}
      {stages.length > 0 && (
        <>
          <div
            className="flex h-1.5 w-full gap-[2px]"
            role="img"
            aria-label={`${label}: ${fmtMs(totalMs)} total, ${stages
              .map((s) => `${s.label} ${fmtMs(s.ms)}`)
              .join(", ")}`}
          >
            {stages.map((s, i) => (
              <span
                key={i}
                title={HINT[s.kind]}
                className={cn(
                  "h-full min-w-[3px]",
                  STAGE_SWATCH[s.kind],
                  i === 0 && "rounded-l-full",
                  i === stages.length - 1 && "rounded-r-full",
                )}
                style={{ width: `${(s.ms / scale) * 100}%` }}
              />
            ))}
          </div>
          <div className="flex flex-wrap gap-x-3 gap-y-1 text-[11px] tabular-nums">
            {stages.map((s, i) => (
              <Tooltip key={i} label={HINT[s.kind]}>
                <span
                  className={cn(
                    "inline-flex items-center gap-1.5",
                    s === slowest ? "font-semibold text-ink" : "text-muted",
                  )}
                >
                  <span className={cn("h-1.5 w-1.5 rounded-full", STAGE_SWATCH[s.kind])} aria-hidden />
                  {s.label} {fmtMs(s.ms)}
                  {s === slowest ? " · slowest" : ""}
                </span>
              </Tooltip>
            ))}
          </div>
        </>
      )}
    </div>
  );
}
