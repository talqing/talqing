"use client";

import { useState } from "react";
import type { FixedDailyCap, RampDailyCap } from "@talqing/sdk";
import { Input, Segment } from "@/app/components/ui";

/* How fast a batch goes: the gap between two sends, and the daily limit.
 *
 * Email, WhatsApp and call batches all pace the same way, so the two controls
 * are drawn once. Both take and return the API's own values. */

export type DailyCap = FixedDailyCap | RampDailyCap;

/** A saved limit and today's value under it — what a ramp being edited has
 *  already climbed to. */
export type CapProgress = { cap: DailyCap | null; today: number | null };

const DEFAULT_RAMP: RampDailyCap = { kind: "ramp", start: 10, end: 50, step: 1, interval_days: 1 };

/** Sending days the saved ramp has behind it. An edit keeps them unless it
 *  moves `start`, which is the API's own rule. */
function daysDone(cap: RampDailyCap, saved?: CapProgress): number {
  if (saved?.cap?.kind !== "ramp" || saved.today === null || saved.cap.start !== cap.start) {
    return 0;
  }
  return Math.round((saved.today - saved.cap.start) / saved.cap.step) * saved.cap.interval_days;
}

/** Sending days from `start` to `end`. */
const rampLength = (cap: RampDailyCap) =>
  Math.ceil((cap.end - cap.start) / cap.step) * cap.interval_days;

/** The limit in force today: null when there is none. */
export function limitToday(cap: DailyCap | null, saved?: CapProgress): number | null {
  if (cap === null) return null;
  if (cap.kind === "fixed") return cap.limit;
  const steps = Math.floor(daysDone(cap, saved) / cap.interval_days);
  return Math.min(cap.end, cap.start + cap.step * steps);
}

/** A ramp that never rises is not a ramp; the API refuses it. */
export const rampNeverRises = (cap: DailyCap | null) =>
  cap?.kind === "ramp" && cap.end <= cap.start;

/** "12 of 14 today · ramping to 50", or "12 today" when nothing limits it. */
export function usedToday(used: number, today: number | null, cap: DailyCap | null): string {
  if (today === null) return `${used.toLocaleString()} today`;
  const ramping = cap?.kind === "ramp" && today < cap.end;
  return `${used.toLocaleString()} of ${today.toLocaleString()} today${
    ramping ? ` · ramping to ${cap.end.toLocaleString()}` : ""
  }`;
}

export function clamp(raw: string, min: number, max: number): number {
  const n = Number(raw);
  if (!Number.isFinite(n)) return min;
  return Math.min(max, Math.max(min, Math.round(n)));
}

/** One setting: its name on the left, its controls on the right. */
export function TermsRow({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="grid gap-x-4 gap-y-2 border-t border-line px-4 py-3 first:border-t-0 sm:grid-cols-[112px_minmax(0,1fr)]">
      <span className="pt-2.5 text-[13px] font-medium leading-5 text-ink">{label}</span>
      <div className="flex min-w-0 flex-wrap items-center gap-2">{children}</div>
    </div>
  );
}

/** The daily limit: none, one number, or a ramp. Renders into a wrapping row. */
export function DailyLimit({
  value,
  onChange,
  noun,
  defaultLimit,
  saved,
}: {
  value: DailyCap | null;
  onChange: (next: DailyCap | null) => void;
  /** What is being limited, singular. */
  noun: "email" | "message" | "call";
  /** The number "Fixed" opens on when the value has none. */
  defaultLimit: number;
  /** On an edit form: the limit as saved, so a ramp in progress says where it is. */
  saved?: CapProgress;
}) {
  // What the other two modes last held, so switching away and back loses nothing.
  const [fixed, setFixed] = useState<FixedDailyCap>(
    value?.kind === "fixed" ? value : { kind: "fixed", limit: defaultLimit },
  );
  const [ramp, setRamp] = useState<RampDailyCap>(
    value?.kind === "ramp" ? value : saved?.cap?.kind === "ramp" ? saved.cap : DEFAULT_RAMP,
  );
  const day = noun === "call" ? "calling day" : "sending day";

  function setRampField(field: "start" | "end" | "step" | "interval_days", raw: string) {
    const next = { ...ramp, [field]: clamp(raw, 1, field === "interval_days" ? 30 : 5000) };
    setRamp(next);
    onChange(next);
  }

  return (
    <>
      <Segment
        value={value?.kind ?? "none"}
        onChange={(mode) => onChange(mode === "none" ? null : mode === "fixed" ? fixed : ramp)}
        options={[
          { value: "none", label: "No limit" },
          { value: "fixed", label: "Fixed" },
          { value: "ramp", label: "Ramp up" },
        ]}
      />
      {value?.kind === "fixed" && (
        <>
          <Input
            type="number"
            min={1}
            max={5000}
            value={value.limit}
            onChange={(e) => {
              const next: FixedDailyCap = { kind: "fixed", limit: clamp(e.target.value, 1, 5000) };
              setFixed(next);
              onChange(next);
            }}
            aria-label={`${noun}s per day`}
            className="w-[88px] text-right tabular-nums"
          />
          <span className="text-[13.5px] text-ink-soft">{noun}s a day</span>
        </>
      )}
      {value?.kind === "ramp" && (
        <>
          {/* Two phrases, each kept whole: a narrow dialog wraps between them. */}
          <div className="flex basis-full flex-wrap items-center gap-x-3 gap-y-2 text-[13.5px] text-ink-soft">
            <div className="flex items-center gap-2 whitespace-nowrap">
              <span>From</span>
              <Input
                type="number"
                min={1}
                max={5000}
                value={value.start}
                onChange={(e) => setRampField("start", e.target.value)}
                aria-label={`${noun}s a day at the start`}
                className="w-[72px] text-right tabular-nums"
              />
              <span>to</span>
              <Input
                type="number"
                min={2}
                max={5000}
                value={value.end}
                onChange={(e) => setRampField("end", e.target.value)}
                aria-label={`${noun}s a day at the end`}
                className="w-[72px] text-right tabular-nums"
              />
              <span>a day,</span>
            </div>
            <div className="flex items-center gap-2 whitespace-nowrap">
              <span>+</span>
              <Input
                type="number"
                min={1}
                max={5000}
                value={value.step}
                onChange={(e) => setRampField("step", e.target.value)}
                aria-label="Raise by"
                className="w-[56px] text-right tabular-nums"
              />
              <span>every</span>
              <Input
                type="number"
                min={1}
                max={30}
                value={value.interval_days}
                onChange={(e) => setRampField("interval_days", e.target.value)}
                aria-label={`${day}s between raises`}
                className="w-[56px] text-right tabular-nums"
              />
              <span>{value.interval_days === 1 ? day : `${day}s`}</span>
            </div>
          </div>
          {rampNeverRises(value) ? (
            <p className="basis-full text-[12.5px] leading-5 text-danger">
              A ramp has to end higher than it starts.
            </p>
          ) : (
            <p className="basis-full text-[12.5px] leading-5 tabular-nums text-faint">
              {rampSentence(value, day, saved)}
            </p>
          )}
        </>
      )}
    </>
  );
}

function rampSentence(cap: RampDailyCap, day: string, saved?: CapProgress): string {
  const days = (n: number) => `${n.toLocaleString()} ${n === 1 ? day : `${day}s`}`;
  const done = daysDone(cap, saved);
  const left = Math.max(rampLength(cap) - done, 0);
  const end = cap.end.toLocaleString();
  if (done > 0) {
    const today = limitToday(cap, saved);
    return left === 0
      ? `${end} a day: this ramp has finished.`
      : `${today?.toLocaleString()} a day today. Reaches ${end} after ${left.toLocaleString()} more ${left === 1 ? day : `${day}s`}.`;
  }
  const restarts = saved?.cap?.kind === "ramp" && saved.cap.start !== cap.start;
  return `${restarts ? `Starts again at ${cap.start.toLocaleString()}. ` : ""}Reaches ${end} after ${days(left)}.`;
}

/** The gap between two sends, in seconds, typed in whichever unit reads best. */
export function GapInput({
  value,
  onChange,
  noun,
  min = 1,
}: {
  value: number;
  onChange: (seconds: number) => void;
  noun: string;
  /** 0 where no gap at all is allowed. */
  min?: 0 | 1;
}) {
  // Stored in seconds and read in whichever unit it is a whole number of:
  // "30 min" is a pace, "1800" is arithmetic.
  const [unit, setUnit] = useState<"sec" | "min">(
    value >= 60 && value % 60 === 0 ? "min" : "sec",
  );

  function switchUnit(next: "sec" | "min") {
    setUnit(next);
    if (next === "min") onChange(clamp(String(Math.round(value / 60)), 1, 60) * 60);
  }

  return (
    <>
      <Input
        type="number"
        min={unit === "min" ? 1 : min}
        max={unit === "min" ? 60 : 3600}
        value={unit === "min" ? value / 60 : value}
        onChange={(e) =>
          onChange(
            unit === "min" ? clamp(e.target.value, 1, 60) * 60 : clamp(e.target.value, min, 3600),
          )
        }
        aria-label={unit === "min" ? `Minutes between ${noun}s` : `Seconds between ${noun}s`}
        className="w-[76px] text-right tabular-nums"
      />
      <Segment
        value={unit}
        onChange={switchUnit}
        options={[
          { value: "sec", label: "sec" },
          { value: "min", label: "min" },
        ]}
      />
    </>
  );
}
