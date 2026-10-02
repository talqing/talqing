import type { EmailBatchResponse } from "@talqing/sdk";
import { Tooltip } from "@/app/components/ui";
import { cn } from "@/lib/cn";
import { reachable } from "./shared";

/* Two bars, never merged into one.
 *
 * "4 000 drafted" and "50 sent" are different facts about one list, and a single
 * bar showing their sum would say neither — which is exactly the confusion the
 * whole two-job split exists to avoid. The drafted bar fills on its own while
 * the batch runs; the sent bar only moves when a person presses send, and on a
 * fresh batch it is deliberately, visibly empty.
 *
 * `pending` is not a segment: it is the unpainted track, so a batch that has not
 * started reads as an empty bar rather than a full one in a colour nobody can
 * tell from the groove. */

/* What each bar DRAWS is every state a row in it can be in, so the bar accounts
   for all of them. What its numerator COUNTS is narrower: "Drafted 2 / 3" beside
   a legend reading "Draft failed 1" is the number and the words disagreeing, and
   the number is the one people read.

   And both are measured against what the bar can actually REACH. A row nothing
   could ever send is named beside the bar rather than hidden inside it: inside
   it, the bar never fills and the fraction reads as a miscount. */

type Part = { key: string; label: string; swatch: string; n: number };

export function BatchProgress({
  batch,
  className,
  compact,
}: {
  batch: EmailBatchResponse;
  className?: string;
  /** Bars only, for a list row — the counts are in the row's own cells. */
  compact?: boolean;
}) {
  const { counts, skips } = batch;
  const reach = reachable(batch);
  // A person's skip and a duplicate were drafted first; an `unfillable` row
  // never was, and is outside this bar altogether.
  const skippedDrafts = counts.skipped - skips.unfillable;
  const neverSent = counts.total - reach.sending;
  return (
    <div className={cn("grid min-w-0 gap-x-8 gap-y-2.5", !compact && "lg:grid-cols-2", className)}>
      <Bar
        title="Drafted"
        total={reach.drafting}
        covered={
          counts.draft + skippedDrafts + counts.queued + counts.sending + counts.sent + counts.send_failed
        }
        parts={[
          { key: "draft", label: "Draft", swatch: "bg-info", n: counts.draft },
          { key: "drafting", label: "Drafting", swatch: "bg-live", n: counts.drafting },
          { key: "skipped", label: "Skipped", swatch: "bg-placeholder", n: skippedDrafts },
          { key: "queued", label: "Queued", swatch: "bg-warn", n: counts.queued },
          { key: "sending", label: "Sending", swatch: "bg-live", n: counts.sending },
          { key: "sent", label: "Sent", swatch: "bg-ink", n: counts.sent },
          { key: "send_failed", label: "Send failed", swatch: "bg-danger", n: counts.send_failed },
          { key: "draft_failed", label: "Draft failed", swatch: "bg-danger", n: counts.draft_failed },
          { key: "canceled", label: "Canceled", swatch: "bg-placeholder", n: counts.canceled },
        ]}
        note={
          skips.unfillable > 0
            ? {
                text: `${skips.unfillable.toLocaleString()} left out`,
                why: "These rows can never be sent — a column the task does not write is empty — so they are skipped rather than drafted.",
              }
            : null
        }
        empty="nothing drafted yet"
        compact={compact}
      />
      <Bar
        title="Sent"
        total={reach.sending}
        covered={counts.sent + counts.send_failed}
        parts={[
          { key: "sent", label: "Sent", swatch: "bg-ink", n: counts.sent },
          { key: "sending", label: "Sending", swatch: "bg-live", n: counts.sending },
          { key: "queued", label: "Queued", swatch: "bg-warn", n: counts.queued },
          { key: "send_failed", label: "Failed", swatch: "bg-danger", n: counts.send_failed },
        ]}
        note={
          neverSent > 0
            ? {
                text: `${neverSent.toLocaleString()} will not be sent`,
                why: "Skipped rows, rows whose draft failed, and rows a cancel stopped before drafting.",
              }
            : null
        }
        empty="nothing sent yet"
        compact={compact}
      />
    </div>
  );
}

function Bar({
  title,
  total,
  covered,
  parts,
  note,
  empty,
  compact,
}: {
  title: string;
  total: number;
  covered: number;
  parts: Part[];
  note: { text: string; why: string } | null;
  empty: string;
  compact?: boolean;
}) {
  const shown = parts.filter((p) => p.n > 0);
  const width = Math.max(total, 1);
  const bar = (
    <div
      className={cn(
        "flex min-w-0 flex-1 overflow-hidden rounded-full bg-subtle ring-1 ring-inset ring-line",
        compact ? "h-1.5" : "h-2",
      )}
      role="img"
      aria-label={
        shown.length
          ? `${title}: ${shown.map((p) => `${p.n} ${p.label.toLowerCase()}`).join(", ")}`
          : `${title}: ${empty}`
      }
    >
      {shown.map((part) => (
        <Tooltip
          key={part.key}
          label={`${part.label}: ${part.n.toLocaleString()}`}
          focusable={false}
          className={cn("h-full cursor-default", part.swatch)}
          style={{ width: `${(part.n / width) * 100}%` }}
        />
      ))}
    </div>
  );
  if (compact) return bar;

  // One line: what the bar counts, the bar, and what it leaves out. Each
  // stretch names itself under the pointer, and the figures above carry the
  // numbers, so there is no legend to read across to.
  return (
    <div className="flex min-w-0 items-center gap-3 text-[12.5px] leading-4">
      <span className="flex-none font-medium text-ink-soft">{title}</span>
      <span className="flex-none font-mono tabular-nums text-muted">
        {covered.toLocaleString()} / {total.toLocaleString()}
      </span>
      {bar}
      {note && (
        <Tooltip label={note.why} className="flex-none text-faint">
          {note.text}
        </Tooltip>
      )}
    </div>
  );
}
