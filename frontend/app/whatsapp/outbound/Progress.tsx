import type { WhatsAppBatchResponse } from "@talqing/sdk";
import { Tooltip } from "@/app/components/ui";
import { cn } from "@/lib/cn";
import { deliveredCount, sentCount } from "./shared";

/* One bar for the whole list, painted by how far each row got: read, delivered,
 * still on its way, failed. What has not left yet is the unpainted track, so a
 * draft reads as an empty bar. Each stretch names itself under the pointer. Replies are a count beside it, not a segment —
 * a replied row is also a read one. */
export function Progress({
  batch,
  compact,
  bare,
  className,
}: {
  batch: WhatsAppBatchResponse;
  compact?: boolean;
  /** The bar alone, for a place that already says the counts. */
  bare?: boolean;
  className?: string;
}) {
  const c = batch.counts;
  const sendable = Math.max(c.total - c.skipped, 1);
  const parts = [
    { key: "read", label: "Read", swatch: "bg-live", n: c.read },
    { key: "delivered", label: "Delivered, not read", swatch: "bg-live/50", n: c.delivered },
    { key: "sent", label: "Sent, not delivered", swatch: "bg-info/60", n: c.sending + c.queued + c.sent },
    { key: "failed", label: "Failed", swatch: "bg-danger", n: c.failed + c.undelivered },
    { key: "ready", label: "Not sent yet", swatch: "", n: c.ready },
  ].filter((p) => p.n > 0);
  const sent = sentCount(batch);

  return (
    <div className={cn("grid min-w-0 gap-1.5", className)}>
      <div
        className={cn(
          "flex w-full overflow-hidden rounded-full bg-subtle ring-1 ring-inset ring-line",
          compact ? "h-1.5" : "h-2",
        )}
        role="img"
        aria-label={parts.map((p) => `${p.n} ${p.label.toLowerCase()}`).join(", ")}
      >
        {parts.map((p) => (
          <Tooltip
            key={p.key}
            label={`${p.label}: ${p.n.toLocaleString()}`}
            focusable={false}
            className={cn("h-full cursor-default", p.swatch)}
            style={{ width: `${(p.n / sendable) * 100}%` }}
          />
        ))}
      </div>
      {!bare && <div className="flex flex-wrap gap-x-2 text-[11.5px] leading-4 text-faint tabular-nums">
        <span>
          <span className="text-ink-soft">{sent.toLocaleString()}</span> of {(c.total - c.skipped).toLocaleString()} sent
        </span>
        {sent > 0 && (
          <>
            <span aria-hidden>·</span>
            <span>{deliveredCount(batch).toLocaleString()} delivered</span>
            <span aria-hidden>·</span>
            <span>{c.read.toLocaleString()} read</span>
            <span aria-hidden>·</span>
            <span className={c.replied ? "text-live" : undefined}>{c.replied.toLocaleString()} replied</span>
          </>
        )}
      </div>}
    </div>
  );
}
