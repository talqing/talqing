"use client";

import type { JsonValue } from "@talqing/sdk";
import { cn } from "@/lib/cn";
import { diffLines, isMultiline, type FieldChange, type Status } from "./diff";

/* ── the shared look of a diff ───────────────────────────────────────────────
   Used by the tool diff (an operation tree) and the agent diff (a config), so
   the two read as one feature rather than two that happen to be near each other.

   Colour is the token ramp with opacity washes, never a tint of its own:
   `live` = added, `danger` = removed, `warn` = changed. The same triple `Badge`
   uses, so one ramp covers the app. */

export const STATUS_MARK: Record<Status, string> = { added: "+", removed: "−", changed: "~", unchanged: "" };

export const STATUS_TEXT: Record<Status, string> = {
  added: "text-live",
  removed: "text-danger",
  changed: "text-warn",
  unchanged: "text-faint",
};

export const STATUS_ROW: Record<Status, string> = {
  added: "border-live/25 bg-live/[0.06]",
  removed: "border-danger/25 bg-danger/[0.05]",
  changed: "border-warn/25 bg-warn/[0.06]",
  unchanged: "border-line bg-white",
};

/** How a single value reads on one line. `undefined` means the field was not
    there at all, which is different from being empty. */
export function valueText(value: unknown): string {
  if (value === undefined) return "—";
  if (value === null) return "none";
  if (typeof value === "string") return value.trim() ? value : '""';
  if (Array.isArray(value)) return value.length ? value.map((v) => valueText(v as JsonValue)).join(", ") : "none";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

export function FieldRow({ change, initial }: { change: FieldChange; initial?: boolean }): JSX.Element {
  // Nothing preceded a first version, so there is no "from" side to strike out.
  const multiline = !initial && isMultiline(change);
  return (
    <div className="flex min-w-0 flex-col gap-1 py-1">
      <div className="flex min-w-0 items-baseline gap-2">
        <span className="flex-none font-mono text-[11.5px] leading-4 text-muted">{change.field}</span>
        {initial ? (
          <span className="min-w-0 whitespace-pre-wrap text-[12px] leading-4 text-ink-soft">{valueText(change.after)}</span>
        ) : !multiline && (
          <span className="min-w-0 text-[12px] leading-4">
            <span className="text-danger/85 line-through decoration-danger/40">{valueText(change.before)}</span>
            <span className="px-1.5 text-faint">→</span>
            <span className="text-live">{valueText(change.after)}</span>
          </span>
        )}
      </div>
      {multiline && (
        <div className="overflow-x-auto rounded-lg border border-line">
          {diffLines(String(change.before ?? ""), String(change.after ?? "")).map((line, i) => (
            <div
              key={i}
              className={cn(
                "whitespace-pre px-2 py-[1px] font-mono text-[11.5px] leading-[18px]",
                line.status === "added" && "bg-live/[0.08] text-live",
                line.status === "removed" && "bg-danger/[0.06] text-danger",
                line.status === "unchanged" && "text-muted",
              )}
            >
              <span className="select-none pr-2 text-faint">
                {line.status === "added" ? "+" : line.status === "removed" ? "−" : " "}
              </span>
              {line.text || " "}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export function Section({ title, count, children }: { title: string; count: number; children: React.ReactNode }): JSX.Element | null {
  if (!count) return null;
  return (
    <section className="flex min-w-0 flex-col gap-2">
      <h3 className="text-[12px] font-semibold uppercase leading-4 tracking-[0.04em] text-ink-soft">{title}</h3>
      {children}
    </section>
  );
}

/** The row idiom every diff entry shares: a status wash, a fixed gutter for the
    mark, then whatever identifies the thing that changed. */
export function StatusRow({
  status,
  head,
  children,
}: {
  status: Status;
  head: React.ReactNode;
  children?: React.ReactNode;
}): JSX.Element {
  return (
    <div className={cn("rounded-lg border px-3 py-2", STATUS_ROW[status])}>
      <div className="flex min-w-0 items-baseline gap-2">
        <span className={cn("w-3 flex-none text-center font-mono text-[12px] font-semibold leading-5", STATUS_TEXT[status])}>
          {STATUS_MARK[status]}
        </span>
        {head}
      </div>
      {children}
    </div>
  );
}

/** What a pane says when the two sides match. Worded by the caller, because
    "identical" is only useful when it names what was compared. */
export function DiffEmpty({ label, className }: { label: string; className?: string }): JSX.Element {
  return (
    <div className={cn("rounded-lg border border-line bg-canvas px-3.5 py-3 text-[13px] leading-5 text-muted", className)}>
      {label}
    </div>
  );
}
