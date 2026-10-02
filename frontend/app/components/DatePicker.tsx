"use client";

import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { cn } from "@/lib/cn";
import { MONTHS, formatDate, fromISODate, toISODate } from "@/lib/date";
import { FIELD } from "./ui";

/* One calendar for the whole dashboard.

   The native `<input type="date">` was what we had, and it is three different
   controls depending on the browser: Chrome's dd/mm/yyyy spinner, Safari's
   nothing-at-all, Firefox's own. None of them take the design system, and the
   placeholder reads as a format demand rather than a prompt. This is a plain
   text trigger plus a popover grid, so a date looks like every other field in
   the editor and reads as "31 Jan 2026" rather than as a slash-separated form.

   The value on the wire is an ISO `YYYY-MM-DD` string, never a `Date`: that is
   what providers and our own API take, and it is the one representation with no
   timezone in it. Everything below stays in local calendar terms — a date here
   is a day on a wall calendar, not an instant. */

const WEEKDAYS = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"];

function startOfMonth(d: Date): Date {
  return new Date(d.getFullYear(), d.getMonth(), 1);
}

function addMonths(d: Date, n: number): Date {
  return new Date(d.getFullYear(), d.getMonth() + n, 1);
}

function sameDay(a: Date, b: Date): boolean {
  return toISODate(a) === toISODate(b);
}

/** The 6×7 grid for a month, Monday-first, padded with the neighbouring months'
 *  days so every month is the same height and the popover never resizes. */
function monthGrid(month: Date): Date[] {
  const first = startOfMonth(month);
  // getDay() is Sunday-first; shift so Monday is 0.
  const lead = (first.getDay() + 6) % 7;
  const start = new Date(first.getFullYear(), first.getMonth(), 1 - lead);
  return Array.from({ length: 42 }, (_, i) =>
    new Date(start.getFullYear(), start.getMonth(), start.getDate() + i),
  );
}

export function DatePicker({
  value,
  onChange,
  min,
  max,
  disabled,
  placeholder = "Pick a date",
  clearable = true,
  className,
  ariaLabel,
}: {
  /** `YYYY-MM-DD`, or null/"" for unset. */
  value: string | null | undefined;
  onChange: (value: string | null) => void;
  /** `YYYY-MM-DD` bounds, inclusive. */
  min?: string | null;
  max?: string | null;
  disabled?: boolean;
  placeholder?: string;
  clearable?: boolean;
  className?: string;
  ariaLabel?: string;
}) {
  const selected = fromISODate(value);
  const [open, setOpen] = useState(false);
  const [pos, setPos] = useState<React.CSSProperties | null>(null);
  const [month, setMonth] = useState<Date>(() => startOfMonth(selected ?? new Date()));
  // The day the keyboard is on. Distinct from the selection: you can walk the
  // grid without committing, exactly as in a native picker.
  const [cursor, setCursor] = useState<Date>(() => selected ?? new Date());
  const ref = useRef<HTMLDivElement>(null);
  const popRef = useRef<HTMLDivElement>(null);

  const minDate = fromISODate(min);
  const maxDate = fromISODate(max);
  const today = new Date();

  function outOfRange(d: Date): boolean {
    if (minDate && toISODate(d) < toISODate(minDate)) return true;
    if (maxDate && toISODate(d) > toISODate(maxDate)) return true;
    return false;
  }

  function openPopup() {
    if (disabled) return;
    const r = ref.current!.getBoundingClientRect();
    const height = 336;
    const spaceBelow = window.innerHeight - r.bottom;
    const openUp = spaceBelow < height && r.top > spaceBelow;
    setPos({
      position: "fixed",
      // Left-aligned to the trigger, nudged in if that would overflow.
      left: Math.min(Math.max(8, r.left), Math.max(8, window.innerWidth - 296)),
      ...(openUp ? { bottom: window.innerHeight - r.top + 6 } : { top: r.bottom + 6 }),
    });
    const anchor = selected ?? new Date();
    setMonth(startOfMonth(anchor));
    setCursor(anchor);
    setOpen(true);
  }

  useEffect(() => {
    if (!open) return;
    function onDoc(e: MouseEvent) {
      const t = e.target as Node;
      if (ref.current?.contains(t) || popRef.current?.contains(t)) return;
      // Consumes the dismissing press, as Select's and Menu's popups do.
      e.stopPropagation();
      setOpen(false);
    }
    function onAway(e: Event) {
      if (popRef.current?.contains(e.target as Node)) return;
      setOpen(false);
    }
    document.addEventListener("mousedown", onDoc, true);
    window.addEventListener("scroll", onAway, true);
    window.addEventListener("resize", onAway);
    return () => {
      document.removeEventListener("mousedown", onDoc, true);
      window.removeEventListener("scroll", onAway, true);
      window.removeEventListener("resize", onAway);
    };
  }, [open]);

  function commit(d: Date) {
    if (outOfRange(d)) return;
    onChange(toISODate(d));
    setOpen(false);
  }

  function moveCursor(days: number, months = 0) {
    const next = months
      ? new Date(cursor.getFullYear(), cursor.getMonth() + months, cursor.getDate())
      : new Date(cursor.getFullYear(), cursor.getMonth(), cursor.getDate() + days);
    setCursor(next);
    setMonth(startOfMonth(next));
  }

  function onKeyDown(e: React.KeyboardEvent) {
    if (!open) {
      if (e.key === "Enter" || e.key === " " || e.key === "ArrowDown") {
        e.preventDefault();
        openPopup();
      }
      return;
    }
    switch (e.key) {
      case "ArrowLeft":
        e.preventDefault();
        return moveCursor(-1);
      case "ArrowRight":
        e.preventDefault();
        return moveCursor(1);
      case "ArrowUp":
        e.preventDefault();
        return moveCursor(-7);
      case "ArrowDown":
        e.preventDefault();
        return moveCursor(7);
      case "PageUp":
        e.preventDefault();
        return moveCursor(0, -1);
      case "PageDown":
        e.preventDefault();
        return moveCursor(0, 1);
      case "Enter":
      case " ":
        e.preventDefault();
        return commit(cursor);
      case "Escape":
        e.preventDefault();
        // Dismiss the calendar only, not a Modal it may be sitting inside.
        e.stopPropagation();
        setOpen(false);
        return;
      case "Tab":
        setOpen(false);
        return;
      default:
        return;
    }
  }

  const label = formatDate(value);

  return (
    <div className={cn("relative", className)} ref={ref}>
      <button
        type="button"
        disabled={disabled}
        aria-label={ariaLabel}
        aria-haspopup="dialog"
        aria-expanded={open}
        onClick={() => (open ? setOpen(false) : openPopup())}
        onKeyDown={onKeyDown}
        className={cn(FIELD, "flex items-center gap-2 text-left", !label && "text-placeholder")}
      >
        <svg className="h-4 w-4 flex-none text-muted" viewBox="0 0 16 16" fill="none" aria-hidden>
          <rect x="2" y="3" width="12" height="11" rx="2" stroke="currentColor" strokeWidth="1.3" />
          <path d="M2 6.5h12M5.5 2v2M10.5 2v2" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" />
        </svg>
        <span className="min-w-0 flex-1 overflow-hidden text-ellipsis whitespace-nowrap">
          {label || placeholder}
        </span>
        {clearable && label && !disabled && (
          // A span, not a button: a button inside a button is invalid markup and
          // browsers drop one of them. The outer button's click is stopped here.
          <span
            role="button"
            tabIndex={-1}
            aria-label="Clear date"
            className="grid h-4 w-4 flex-none place-items-center rounded text-muted transition-colors hover:bg-subtle hover:text-danger"
            onClick={(e) => {
              e.stopPropagation();
              onChange(null);
            }}
          >
            <svg className="h-2.5 w-2.5" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" aria-hidden>
              <path d="m3 3 6 6M9 3 3 9" />
            </svg>
          </span>
        )}
      </button>
      {open &&
        pos &&
        createPortal(
          <div
            ref={popRef}
            role="dialog"
            aria-label="Choose a date"
            style={pos}
            // Above Modal's z-[120], for the same reason as Select's popup.
            className="z-[130] w-[288px] rounded-[10px] border border-line-2 bg-surface p-3 shadow-pop animate-slide-up"
          >
            <div className="mb-2 flex items-center justify-between">
              <MonthArrow dir={-1} onClick={() => setMonth(addMonths(month, -1))} />
              <div className="text-[13px] font-semibold text-ink">
                {MONTHS[month.getMonth()]} {month.getFullYear()}
              </div>
              <MonthArrow dir={1} onClick={() => setMonth(addMonths(month, 1))} />
            </div>
            <div className="mb-1 grid grid-cols-7 gap-0.5">
              {WEEKDAYS.map((d) => (
                <div key={d} className="grid h-7 place-items-center text-[11px] font-medium text-faint">
                  {d}
                </div>
              ))}
            </div>
            <div className="grid grid-cols-7 gap-0.5">
              {monthGrid(month).map((day) => {
                const isOutside = day.getMonth() !== month.getMonth();
                const isSelected = selected != null && sameDay(day, selected);
                const isToday = sameDay(day, today);
                const isCursor = sameDay(day, cursor);
                const blocked = outOfRange(day);
                return (
                  <button
                    key={toISODate(day)}
                    type="button"
                    disabled={blocked}
                    tabIndex={-1}
                    aria-current={isToday ? "date" : undefined}
                    aria-pressed={isSelected}
                    onClick={() => commit(day)}
                    className={cn(
                      "grid h-9 place-items-center rounded-lg text-[13px] tabular-nums transition-colors",
                      "focus:outline-none",
                      blocked && "cursor-not-allowed text-placeholder opacity-50",
                      !blocked && !isSelected && "text-ink hover:bg-hover",
                      isOutside && !isSelected && "text-faint",
                      isSelected && "bg-ink font-semibold text-surface",
                      // The keyboard's position, drawn only while it differs from
                      // the selection so the two never fight for attention.
                      isCursor && !isSelected && "ring-2 ring-inset ring-ink/15",
                      isToday && !isSelected && "font-semibold",
                    )}
                  >
                    {day.getDate()}
                  </button>
                );
              })}
            </div>
            <div className="mt-2 flex items-center justify-between border-t border-line pt-2">
              <button
                type="button"
                className="rounded-md px-2 py-1 text-[12.5px] font-medium text-ink-soft transition-colors hover:bg-subtle disabled:opacity-40"
                disabled={outOfRange(today)}
                onClick={() => commit(today)}
              >
                Today
              </button>
              {clearable && (
                <button
                  type="button"
                  className="rounded-md px-2 py-1 text-[12.5px] font-medium text-muted transition-colors hover:bg-subtle hover:text-ink"
                  onClick={() => {
                    onChange(null);
                    setOpen(false);
                  }}
                >
                  Clear
                </button>
              )}
            </div>
          </div>,
          document.body,
        )}
    </div>
  );
}

function MonthArrow({ dir, onClick }: { dir: 1 | -1; onClick: () => void }) {
  return (
    <button
      type="button"
      aria-label={dir === 1 ? "Next month" : "Previous month"}
      onClick={onClick}
      className="grid h-7 w-7 place-items-center rounded-md text-muted transition-colors hover:bg-subtle hover:text-ink focus:outline-none focus-visible:ring-2 focus-visible:ring-ink/10"
    >
      <svg className={cn("h-3.5 w-3.5", dir === 1 && "rotate-180")} viewBox="0 0 12 12" fill="none" aria-hidden>
        <path d="M7.5 2.5 4 6l3.5 3.5" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
    </button>
  );
}
