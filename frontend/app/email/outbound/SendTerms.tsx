"use client";

import type { EmailSendResponse } from "@talqing/sdk";
import { Input, Segment, Select } from "@/app/components/ui";
import { DatePicker } from "@/app/components/DatePicker";
import {
  type CapProgress,
  type DailyCap,
  DailyLimit,
  GapInput,
  TermsRow,
} from "@/app/components/Pacing";
import { cn } from "@/lib/cn";
import { localTimezone, timezones, today } from "@/lib/date";
import {
  ISO_WEEKDAYS,
  hhmm,
  instantToWallTime,
  wallTimeToInstant,
} from "@/app/telephony/outbound-calling/shared";

/* A send's terms — when it starts, the hours it may send in, its pace and its
 * daily ceiling — as one set of controls.
 *
 * Creating a send and re-steering one edit the same five things, and two copies
 * of these controls would drift the first time either dialog changed. */

export type SendTermsDraft = {
  startMode: "now" | "at";
  startDate: string;
  startTime: string;
  timezone: string;
  windowOn: boolean;
  windowStart: string;
  windowEnd: string;
  windowDays: number[];
  gap: number;
  cap: DailyCap | null;
};

/** The terms to start from: a send's own, or a batch's defaults. `startAt` is
 *  passed apart because a batch's `start_at` is when DRAFTING starts, which a
 *  new send must not inherit. */
export function termsFrom(
  source: Pick<EmailSendResponse, "timezone" | "window" | "send_gap_seconds" | "send_daily_cap">,
  startAt: string | null,
): SendTermsDraft {
  const timezone = source.timezone || localTimezone();
  const start = startAt ? instantToWallTime(startAt, timezone) : null;
  return {
    startMode: start ? "at" : "now",
    startDate: start?.date ?? today(),
    startTime: start?.time ?? "09:00",
    timezone,
    windowOn: !!source.window,
    windowStart: source.window ? hhmm(source.window.start) : "09:00",
    windowEnd: source.window ? hhmm(source.window.end) : "17:00",
    windowDays: source.window?.days ?? [1, 2, 3, 4, 5],
    gap: source.send_gap_seconds,
    cap: source.send_daily_cap,
  };
}

/** The fields both create and patch send as they are. */
export function termsBody(d: SendTermsDraft) {
  return {
    timezone: d.timezone,
    window: d.windowOn ? { start: d.windowStart, end: d.windowEnd, days: d.windowDays } : null,
    send_gap_seconds: d.gap,
    send_daily_cap: d.cap,
  };
}

export const startAtOf = (d: SendTermsDraft): string | null =>
  d.startMode === "at" ? wallTimeToInstant(d.startDate, d.startTime, d.timezone) : null;

/** Sending hours that start and end at the same minute never open. */
export const windowNeverOpens = (d: SendTermsDraft) => d.windowOn && d.windowStart === d.windowEnd;

export function SendTermsFields({
  value,
  onChange,
  showStart,
  leading,
  trailing,
  noun = "email",
  savedCap,
}: {
  value: SendTermsDraft;
  onChange: (next: SendTermsDraft) => void;
  /** Off once a send has begun: its start is history, not a setting. */
  showStart: boolean;
  /** Rows above the terms — the sender, when creating a send. */
  leading?: React.ReactNode;
  /** Rows below them — a WhatsApp batch's resend policy. */
  trailing?: React.ReactNode;
  /** What is being sent, singular: WhatsApp batches reuse these controls. */
  noun?: "email" | "message";
  /** The limit as saved and today's value under it, when editing one. */
  savedCap?: CapProgress;
}) {
  const set = <K extends keyof SendTermsDraft>(key: K, v: SendTermsDraft[K]) =>
    onChange({ ...value, [key]: v });
  const perHour = Math.round(3600 / value.gap);

  function toggleDay(day: number) {
    const next = value.windowDays.includes(day)
      ? value.windowDays.filter((d) => d !== day)
      : [...value.windowDays, day].sort((a, b) => a - b);
    if (next.length) set("windowDays", next);
  }

  return (
    <div className="overflow-hidden rounded-xl border border-line-2 bg-white">
      {leading}
      {showStart && (
        <TermsRow label="Starts">
          <Segment
            value={value.startMode}
            onChange={(m) => set("startMode", m)}
            options={[
              { value: "now", label: "Now" },
              { value: "at", label: "At a time" },
            ]}
          />
          {value.startMode === "at" && (
            <div className="flex basis-full flex-wrap items-center gap-2">
              <div className="w-[160px]">
                <DatePicker
                  value={value.startDate}
                  min={today()}
                  clearable={false}
                  ariaLabel="Send start date"
                  onChange={(d) => d && set("startDate", d)}
                />
              </div>
              <Input
                type="time"
                value={value.startTime}
                onChange={(e) => set("startTime", e.target.value)}
                aria-label="Send start time"
                className="w-[136px]"
              />
            </div>
          )}
        </TermsRow>
      )}

      <TermsRow label="Timezone">
        <Select
          value={value.timezone}
          onChange={(e) => set("timezone", e.target.value)}
          searchable
          aria-label="Send timezone"
          className="w-full max-w-[320px]"
        >
          {timezones(value.timezone).map((z) => (
            <option key={z} value={z}>
              {z.replace(/_/g, " ")}
            </option>
          ))}
        </Select>
      </TermsRow>

      <TermsRow label="Sending hours">
        <Segment
          value={value.windowOn ? "set" : "any"}
          onChange={(v) => set("windowOn", v === "set")}
          options={[
            { value: "any", label: "Any time" },
            { value: "set", label: "Set hours" },
          ]}
        />
        {value.windowOn && (
          <>
            <div className="flex basis-full flex-wrap items-center gap-2">
              <Input
                type="time"
                value={value.windowStart}
                onChange={(e) => set("windowStart", e.target.value)}
                aria-label="Sending hours start"
                className="w-[136px]"
              />
              <span className="text-[13px] text-faint">to</span>
              <Input
                type="time"
                value={value.windowEnd}
                onChange={(e) => set("windowEnd", e.target.value)}
                aria-label="Sending hours end"
                className="w-[136px]"
              />
            </div>
            <div className="flex basis-full gap-1" role="group" aria-label="Sending days">
              {ISO_WEEKDAYS.map((day) => {
                const on = value.windowDays.includes(day.value);
                return (
                  <button
                    key={day.value}
                    type="button"
                    aria-pressed={on}
                    onClick={() => toggleDay(day.value)}
                    className={cn(
                      "h-8 w-9 rounded-lg border text-[12.5px] font-medium transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-ink/10",
                      on
                        ? "border-ink bg-ink text-white"
                        : "border-line-2 bg-white text-muted hover:border-line-strong hover:text-ink",
                    )}
                  >
                    {day.short}
                  </button>
                );
              })}
            </div>
          </>
        )}
      </TermsRow>

      <TermsRow label="Pace">
        <span className="text-[13.5px] text-ink-soft">One {noun} every</span>
        <GapInput value={value.gap} onChange={(gap) => set("gap", gap)} noun={noun} />
        <span className="ml-auto text-[12.5px] tabular-nums text-faint">
          {perHour.toLocaleString()} an hour
        </span>
      </TermsRow>

      <TermsRow label="Daily cap">
        <DailyLimit
          value={value.cap}
          onChange={(cap) => set("cap", cap)}
          noun={noun}
          defaultLimit={noun === "email" ? 200 : 250}
          saved={savedCap}
        />
      </TermsRow>

      {trailing}

      {/* An email batch can have several sends; a WhatsApp batch has one. */}
      {noun === "email" && (
        <p className="border-t border-line bg-canvas px-4 py-2 text-[12px] leading-4 text-faint">
          The pace and the daily cap belong to the batch: every send of it shares them.
        </p>
      )}
    </div>
  );
}
