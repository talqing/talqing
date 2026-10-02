"use client";
import { cn } from "@/lib/cn";

export interface ProportionSegment {
  key: string;
  /** Legend text — never wears the segment colour. */
  label: string;
  value: number;
  /** Background utility for the mark, e.g. `bg-chart-stt`. */
  swatch: string;
  /** Pre-formatted value for the legend and the hover title. */
  display: string;
}

/**
 * One bar for a part-to-whole split, with a swatch-dot legend.
 *
 * Renders nothing below two non-zero parts: a single full-width segment carries
 * no comparison, and whatever tile sits beside it already states the number.
 *
 * `legend` turns the swatch row off for the one caller that already has one:
 * the cost ledger repeats every segment as a row directly below the bar, in the
 * same order and wearing the same swatch, so the legend would state each line
 * twice within forty pixels. The aria-label still carries the full reading.
 */
export function ProportionBar({
  label,
  segments,
  ariaLabel,
  legend = true,
  className,
}: {
  label: string;
  segments: ProportionSegment[];
  ariaLabel: string;
  legend?: boolean;
  className?: string;
}) {
  const parts = segments.filter((s) => s.value > 0);
  const total = parts.reduce((sum, s) => sum + s.value, 0);
  if (parts.length < 2 || total <= 0) return null;
  const share = (value: number) => `${Math.round((value / total) * 100)}%`;
  return (
    <div className={cn("grid gap-1.5", className)}>
      <span className="text-[11px] font-medium text-muted">{label}</span>
      <div
        className="flex h-1.5 w-full gap-[2px]"
        role="img"
        aria-label={`${ariaLabel}: ${parts
          .map((s) => `${s.label} ${s.display}, ${share(s.value)}`)
          .join("; ")}`}
      >
        {parts.map((s, i) => (
          <span
            key={s.key}
            title={`${s.label} · ${s.display} · ${share(s.value)}`}
            className={cn(
              "h-full min-w-[3px]",
              s.swatch,
              i === 0 && "rounded-l-full",
              i === parts.length - 1 && "rounded-r-full",
            )}
            style={{ width: `${(s.value / total) * 100}%` }}
          />
        ))}
      </div>
      {legend && (
        <div className="flex flex-wrap gap-x-3 gap-y-1 text-[11px] tabular-nums text-muted">
          {parts.map((s) => (
            <span key={s.key} className="inline-flex items-center gap-1.5">
              <span className={cn("h-1.5 w-1.5 rounded-full", s.swatch)} aria-hidden />
              {s.label} {s.display} · {share(s.value)}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}
