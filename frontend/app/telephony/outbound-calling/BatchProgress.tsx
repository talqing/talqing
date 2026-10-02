import type { CallBatchResponse } from "@talqing/sdk";
import { cn } from "@/lib/cn";
import { settledShare } from "./shared";

/* How far through the list a batch is, and where everyone ended up.
 *
 * A stacked bar rather than a percentage: "62 % done" hides the difference
 * between a batch reaching people and a batch failing on every row at the same
 * speed, and that difference is the only reason anyone opens this page mid-run.
 * The counts read straight off the API, which computes them for a cancelled
 * batch without having rewritten a single recipient row.
 *
 * `pending` is deliberately not a segment: it is the unpainted track, so a batch
 * that has not started reads as an empty bar rather than a full one in a colour
 * nobody can tell from the groove. It still gets a legend entry, because the
 * number matters even when the mark does not. */

const PARTS = [
  { key: "completed", label: "Reached", swatch: "bg-info" },
  { key: "dialing", label: "Calling", swatch: "bg-live" },
  { key: "failed", label: "Failed", swatch: "bg-danger" },
  { key: "canceled", label: "Canceled", swatch: "bg-placeholder" },
] as const;

export function BatchProgress({
  batch,
  className,
  compact,
}: {
  batch: CallBatchResponse;
  className?: string;
  /** Bar only, for a list row — the counts are in the row's own cells. */
  compact?: boolean;
}) {
  const { counts } = batch;
  const total = Math.max(counts.total, 1);
  const parts = PARTS.filter((p) => counts[p.key] > 0);

  return (
    <div className={cn("grid gap-2", className)}>
      <div
        className={cn(
          "flex w-full overflow-hidden rounded-full bg-subtle ring-1 ring-inset ring-line",
          compact ? "h-1.5" : "h-2",
        )}
        role="img"
        aria-label={`${Math.round(settledShare(batch) * 100)}% done: ${
          parts.length
            ? parts.map((p) => `${counts[p.key]} ${p.label.toLowerCase()}`).join(", ")
            : "nobody called yet"
        }`}
      >
        {parts.map((part) => (
          <span
            key={part.key}
            className={cn("h-full", part.swatch)}
            style={{ width: `${(counts[part.key] / total) * 100}%` }}
            title={`${part.label}: ${counts[part.key].toLocaleString()}`}
          />
        ))}
      </div>
      {!compact && (
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[12.5px] text-muted">
          {PARTS.map((part) => (
            <span key={part.key} className="inline-flex items-center gap-1.5">
              <span className={cn("h-2 w-2 rounded-full", part.swatch)} />
              {part.label}
              <span className="font-mono tabular-nums text-ink-soft">
                {counts[part.key].toLocaleString()}
              </span>
            </span>
          ))}
          <span className="inline-flex items-center gap-1.5">
            <span className="h-2 w-2 rounded-full bg-subtle ring-1 ring-inset ring-line-strong" />
            Not called
            <span className="font-mono tabular-nums text-ink-soft">
              {counts.pending.toLocaleString()}
            </span>
          </span>
          {/* A tally across the segments rather than one of them — a voicemail
              row is Failed, or Not called while a retry is pending — so it gets
              no swatch, and a rule sets it apart from the ones that add up. */}
          {counts.voicemail > 0 && (
            <span
              className="inline-flex items-center gap-1.5 border-l border-line pl-4"
              title="Their latest call reached voicemail. Retried while attempts remain."
            >
              Voicemail
              <span className="font-mono tabular-nums text-ink-soft">
                {counts.voicemail.toLocaleString()}
              </span>
            </span>
          )}
          <span className="ml-auto font-mono tabular-nums text-faint">
            {counts.total.toLocaleString()} total
          </span>
        </div>
      )}
    </div>
  );
}
