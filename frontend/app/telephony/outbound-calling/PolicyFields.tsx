"use client";

import type {
  AgentResponse,
  CallBatchResponse,
  CreateCallBatchRequest,
  PatchCallBatchRequest,
  PhoneNumberResponse,
  TelephonyAccountResponse,
  VarDeclaration,
} from "@talqing/sdk";
import { BoxCheckbox, Field, Input, Label, Segment, Select, Tooltip } from "@/app/components/ui";
import {
  SessionVarFields,
  missingVars,
  missingVarsSentence,
  suppliedVars,
} from "@/app/components/SessionVars";
import { DatePicker } from "@/app/components/DatePicker";
import {
  type CapProgress,
  type DailyCap,
  DailyLimit,
  GapInput,
  TermsRow,
  clamp,
  rampNeverRises,
} from "@/app/components/Pacing";
import { cn } from "@/lib/cn";
import { localTimezone, timezones, today } from "@/lib/date";
import { agentLabel } from "../phone-numbers/shared";
import { ISO_WEEKDAYS, hhmm, instantToWallTime, wallTimeToInstant } from "./shared";

/* Everything about a batch except who is on the list — the same fields at
   create time and when editing a live one, because they are the same fields.
   The state lives in the caller so both screens can validate and submit their
   own way; this only draws it. */

export type PolicyDraft = {
  name: string;
  agentId: string;
  fromPhoneNumberId: string;
  startMode: "now" | "at";
  startDate: string;
  startTime: string;
  timezone: string;
  windowOn: boolean;
  windowStart: string;
  windowEnd: string;
  windowDays: number[];
  maxConcurrency: number;
  maxAttempts: number;
  retryAfterMinutes: number;
  dialGapSeconds: number;
  dialDailyCap: DailyCap | null;
  /** One bag for the whole campaign, decided when it is created. `PATCH` has no
   *  `vars`, so on an existing batch this is what it was created with and is
   *  shown rather than edited. */
  vars: Record<string, string>;
};

export function emptyDraft(): PolicyDraft {
  return {
    name: "",
    agentId: "",
    fromPhoneNumberId: "",
    startMode: "now",
    startDate: today(),
    startTime: "10:00",
    timezone: localTimezone(),
    windowOn: false,
    windowStart: "10:00",
    windowEnd: "18:00",
    windowDays: [1, 2, 3, 4, 5],
    // One call at a time is the safe reading of "they did not say", and it is
    // what the API assumes too. An operator who wants ten says so.
    maxConcurrency: 1,
    maxAttempts: 1,
    retryAfterMinutes: 30,
    dialGapSeconds: 0,
    dialDailyCap: null,
    vars: {},
  };
}

export function draftFromBatch(batch: CallBatchResponse): PolicyDraft {
  const base = emptyDraft();
  const start = batch.start_at ? instantToWallTime(batch.start_at, batch.timezone) : null;
  return {
    ...base,
    name: batch.name,
    agentId: batch.agent_id ?? "",
    fromPhoneNumberId: batch.from_phone_number_id ?? "",
    startMode: start ? "at" : "now",
    startDate: start?.date ?? base.startDate,
    startTime: start?.time ?? base.startTime,
    timezone: batch.timezone,
    windowOn: !!batch.calling_window,
    windowStart: batch.calling_window ? hhmm(batch.calling_window.start) : base.windowStart,
    windowEnd: batch.calling_window ? hhmm(batch.calling_window.end) : base.windowEnd,
    windowDays: batch.calling_window?.days ?? base.windowDays,
    maxConcurrency: batch.max_concurrency,
    maxAttempts: batch.max_attempts,
    retryAfterMinutes: batch.retry_after_minutes,
    dialGapSeconds: batch.dial_gap_seconds,
    dialDailyCap: batch.dial_daily_cap,
    vars: batch.vars ?? {},
  };
}

/** The schedule and window halves of a request body, shared by create and patch. */
function scheduleOf(draft: PolicyDraft) {
  return {
    start_at:
      draft.startMode === "at"
        ? wallTimeToInstant(draft.startDate, draft.startTime, draft.timezone)
        : null,
    timezone: draft.timezone,
    calling_window: draft.windowOn
      ? { start: draft.windowStart, end: draft.windowEnd, days: draft.windowDays }
      : null,
  };
}

export function toCreateRequest(
  draft: PolicyDraft,
  recipients: CreateCallBatchRequest["recipients"],
  declared: VarDeclaration[],
): CreateCallBatchRequest {
  const vars = suppliedVars(declared, draft.vars);
  return {
    name: draft.name.trim(),
    agent_id: draft.agentId,
    from_phone_number_id: draft.fromPhoneNumberId,
    recipients,
    ...scheduleOf(draft),
    max_concurrency: draft.maxConcurrency,
    max_attempts: draft.maxAttempts,
    retry_after_minutes: draft.retryAfterMinutes,
    dial_gap_seconds: draft.dialGapSeconds,
    dial_daily_cap: draft.dialDailyCap,
    vars: Object.keys(vars).length ? vars : null,
  };
}

/** Sends every field, including explicit nulls — which is how the API is told
 *  to *clear* a calling window, a start time or the daily limit rather than
 *  leave it alone. */
export function toPatchRequest(draft: PolicyDraft, startEditable: boolean): PatchCallBatchRequest {
  const { start_at, ...rest } = scheduleOf(draft);
  return {
    name: draft.name.trim(),
    agent_id: draft.agentId,
    from_phone_number_id: draft.fromPhoneNumberId,
    ...rest,
    ...(startEditable ? { start_at } : {}),
    max_concurrency: draft.maxConcurrency,
    max_attempts: draft.maxAttempts,
    retry_after_minutes: draft.retryAfterMinutes,
    dial_gap_seconds: draft.dialGapSeconds,
    dial_daily_cap: draft.dialDailyCap,
  };
}

/** Numbers this workspace can actually dial out of, in dialling order. */
export function outboundNumbers(
  numbers: PhoneNumberResponse[],
  accounts: TelephonyAccountResponse[],
): PhoneNumberResponse[] {
  const ready = new Set(accounts.filter((a) => a.status === "ready").map((a) => a.id));
  return numbers
    .filter(
      (n) => n.can_outbound && n.status === "active" && ready.has(n.telephony_account_id),
    )
    .sort((a, b) => a.e164.localeCompare(b.e164));
}

export function PolicyFields({
  draft,
  onChange,
  agents,
  numbers,
  declared,
  /** False once a batch has started: moving the start time of something already
   *  running means nothing, and the API refuses it. */
  startEditable = true,
  /** False on an existing batch: `PatchCallBatchRequest` has no `vars`, so the
   *  bag is shown as what the campaign was created with rather than offered as
   *  something this screen could change. Re-creating the batch is the way to
   *  change it. */
  varsEditable = true,
  savedCap,
  className,
}: {
  draft: PolicyDraft;
  onChange: (next: PolicyDraft) => void;
  agents: AgentResponse[];
  numbers: PhoneNumberResponse[];
  /** What the selected agent's PUBLISHED version declares — the caller loads it
   *  with `usePublishedVars`, because it also needs the missing half for
   *  `policyProblems`. */
  declared: VarDeclaration[];
  startEditable?: boolean;
  varsEditable?: boolean;
  /** The daily limit as saved and today's value under it, on the edit form. */
  savedCap?: CapProgress;
  className?: string;
}) {
  const set = <K extends keyof PolicyDraft>(key: K, value: PolicyDraft[K]) =>
    onChange({ ...draft, [key]: value });

  const toggleDay = (day: number) => {
    const next = draft.windowDays.includes(day)
      ? draft.windowDays.filter((d) => d !== day)
      : [...draft.windowDays, day].sort((a, b) => a - b);
    // An empty day set is a window that never opens, so the last one on cannot
    // be turned off — refusing the click reads better than accepting it and
    // failing at submit.
    if (next.length) set("windowDays", next);
  };

  const crossesMidnight = draft.windowStart > draft.windowEnd;

  return (
    <div className={cn("grid gap-5", className)}>
      <Field label="Name" htmlFor="batch-name" hint="What this list is for — only you see it.">
        <Input
          id="batch-name"
          value={draft.name}
          onChange={(e) => set("name", e.target.value)}
          placeholder="March renewals"
          maxLength={200}
        />
      </Field>

      <div className="grid gap-5 sm:grid-cols-2">
        <Field
          label="Agent"
          hint="Published voice agents only. Every call runs the version published at the time it is placed."
        >
          <Select
            value={draft.agentId}
            onChange={(e) => set("agentId", e.target.value)}
            searchable={agents.length > 20}
            aria-label="Agent"
          >
            <option value="">Choose an agent</option>
            {agents.map((a) => (
              <option key={a.id} value={a.id}>
                {agentLabel(a)}
              </option>
            ))}
          </Select>
        </Field>
        <Field label="Call from" hint="One number for the whole batch.">
          <Select
            value={draft.fromPhoneNumberId}
            onChange={(e) => set("fromPhoneNumberId", e.target.value)}
            aria-label="Phone number to call from"
          >
            <option value="">Choose a number</option>
            {numbers.map((n) => (
              <option key={n.id} value={n.id}>
                {n.label ? `${n.e164} · ${n.label}` : n.e164}
              </option>
            ))}
          </Select>
        </Field>
      </div>

      {declared.length > 0 && (
        <div className="grid gap-2">
          <Label>Variables</Label>
          <SessionVarFields
            declared={declared}
            values={draft.vars}
            onChange={(vars) => set("vars", vars)}
            idPrefix="batch-var"
            disabled={!varsEditable}
            className="sm:grid-cols-2"
          />
          <p className="text-[13px] leading-5 text-muted">
            One bag for the whole campaign, copied onto every call it places
            {varsEditable ? "" : ", and fixed when it was created"}. What the agent should know
            about the deployment rather than about a person — anything that differs per recipient
            belongs in their user data on the list above.
          </p>
        </div>
      )}

      <div className="grid gap-2">
        <div className="flex flex-wrap items-start gap-x-4 gap-y-3">
          <Field label="Start">
            <Segment
              value={draft.startMode}
              onChange={(mode) => startEditable && set("startMode", mode)}
              options={[
                { value: "now", label: "As soon as it is saved" },
                { value: "at", label: "At a time" },
              ]}
            />
          </Field>
          {draft.startMode === "at" && (
            <>
              <Field label="Date" className="w-[168px]">
                <DatePicker
                  value={draft.startDate}
                  min={today()}
                  clearable={false}
                  disabled={!startEditable}
                  ariaLabel="Start date"
                  onChange={(d) => d && set("startDate", d)}
                />
              </Field>
              <Field label="Time" className="w-[124px]">
                <Input
                  type="time"
                  value={draft.startTime}
                  disabled={!startEditable}
                  onChange={(e) => set("startTime", e.target.value)}
                  aria-label="Start time"
                />
              </Field>
            </>
          )}
          <Field label="Timezone" className="min-w-[220px] flex-1">
            <Select
              value={draft.timezone}
              onChange={(e) => set("timezone", e.target.value)}
              searchable
              aria-label="Timezone"
            >
              {timezones(draft.timezone).map((z) => (
                <option key={z} value={z}>
                  {z.replace(/_/g, " ")}
                </option>
              ))}
            </Select>
          </Field>
        </div>
        <p className="text-[13px] leading-5 text-muted">
          The batch keeps its own clock: its start time and calling hours are read on it, whatever
          the reader&rsquo;s.
          {!startEditable && " This batch has already started, so its start time is fixed."}
        </p>
      </div>

      <div className="grid gap-3">
        <div className="flex items-center gap-2.5">
          <BoxCheckbox
            checked={draft.windowOn}
            onChange={(on) => set("windowOn", on)}
            ariaLabel="Only call during these hours"
          />
          <button
            type="button"
            onClick={() => set("windowOn", !draft.windowOn)}
            className="text-[14px] font-semibold leading-5 text-ink"
          >
            Only call during these hours
          </button>
        </div>
        {draft.windowOn ? (
          <div className="grid gap-3 rounded-xl border border-line-2 bg-white p-3.5">
            <div className="flex flex-wrap items-center gap-2.5">
              <Input
                type="time"
                value={draft.windowStart}
                onChange={(e) => set("windowStart", e.target.value)}
                aria-label="Calling hours start"
                className="w-[130px]"
              />
              <span className="text-[13px] text-muted">to</span>
              <Input
                type="time"
                value={draft.windowEnd}
                onChange={(e) => set("windowEnd", e.target.value)}
                aria-label="Calling hours end"
                className="w-[130px]"
              />
              <div className="ml-auto flex gap-1">
                {ISO_WEEKDAYS.map((day) => {
                  const on = draft.windowDays.includes(day.value);
                  return (
                    <button
                      key={day.value}
                      type="button"
                      aria-pressed={on}
                      onClick={() => toggleDay(day.value)}
                      className={cn(
                        "min-h-8 rounded-lg border px-2 text-[12.5px] font-medium transition-colors",
                        on
                          ? "border-ink bg-ink text-white"
                          : "border-line-2 bg-white text-muted hover:text-ink",
                      )}
                    >
                      {day.short}
                    </button>
                  );
                })}
              </div>
            </div>
            <p className="text-[13px] leading-5 text-muted">
              {crossesMidnight ? (
                <>
                  This window runs overnight: it opens at {draft.windowStart} on the days you
                  picked and keeps going until {draft.windowEnd} the next morning.
                </>
              ) : (
                <>
                  Outside these hours the batch waits rather than stopping, and picks up again
                  when they next open.
                </>
              )}
            </p>
          </div>
        ) : (
          <p className="text-[13px] leading-5 text-muted">
            Calls go out at any hour of any day. Check who is on your list before leaving this
            off.
          </p>
        )}
      </div>

      <div className="grid gap-5 sm:grid-cols-3">
        <Field
          label="Calls at once"
          htmlFor="batch-concurrency"
          hint="1–10. Nothing else limits this."
        >
          <Input
            id="batch-concurrency"
            type="number"
            min={1}
            max={10}
            value={draft.maxConcurrency}
            onChange={(e) => set("maxConcurrency", clamp(e.target.value, 1, 10))}
          />
        </Field>
        <Field
          label={
            <Tooltip label="Someone who answered is never called again, whatever this says.">
              <span>Attempts each</span>
            </Tooltip>
          }
          htmlFor="batch-attempts"
          hint="1–5. Busy and unanswered numbers are tried again; a disconnected one never is."
        >
          <Input
            id="batch-attempts"
            type="number"
            min={1}
            max={5}
            value={draft.maxAttempts}
            onChange={(e) => set("maxAttempts", clamp(e.target.value, 1, 5))}
          />
        </Field>
        <Field
          label="Wait before retrying"
          htmlFor="batch-retry"
          hint="Minutes, 5–1440."
          className={draft.maxAttempts > 1 ? "" : "opacity-50"}
        >
          <Input
            id="batch-retry"
            type="number"
            min={5}
            max={1440}
            step={5}
            disabled={draft.maxAttempts <= 1}
            value={draft.retryAfterMinutes}
            onChange={(e) => set("retryAfterMinutes", clamp(e.target.value, 5, 1440))}
          />
        </Field>
      </div>

      <div className="grid gap-1.5">
        <Label>Pace</Label>
        <div className="overflow-hidden rounded-xl border border-line-2 bg-white">
          <TermsRow label="Between calls">
            <span className="text-[13.5px] text-ink-soft">Wait at least</span>
            <GapInput
              value={draft.dialGapSeconds}
              onChange={(gap) => set("dialGapSeconds", gap)}
              noun="call"
              min={0}
            />
            <span className="ml-auto text-[12.5px] tabular-nums text-faint">
              {draft.dialGapSeconds === 0
                ? "as soon as a line frees"
                : `at most ${Math.round(3600 / draft.dialGapSeconds).toLocaleString()} an hour`}
            </span>
            {draft.dialGapSeconds > 0 && (
              <p className="basis-full text-[12.5px] leading-5 text-faint">
                A call can start up to 30 seconds after its wait ends.
              </p>
            )}
          </TermsRow>
          <TermsRow label="Daily limit">
            <DailyLimit
              value={draft.dialDailyCap}
              onChange={(cap) => set("dialDailyCap", cap)}
              noun="call"
              defaultLimit={100}
              saved={savedCap}
            />
          </TermsRow>
        </div>
      </div>
    </div>
  );
}

/** What is missing before this can be saved, in the order a reader would fix it. */
export function policyProblems(draft: PolicyDraft, declared: VarDeclaration[]): string[] {
  const problems: string[] = [];
  if (!draft.name.trim()) problems.push("Give the batch a name.");
  if (!draft.agentId) problems.push("Choose an agent to make the calls.");
  if (!draft.fromPhoneNumberId) problems.push("Choose a number to call from.");
  // The API refuses this too — at create, and again on every dispatcher pass if
  // the agent gains a requirement mid-campaign. On an existing batch the bag is
  // not editable, so this is how "you have re-pointed it at an agent this
  // campaign cannot run" arrives before the click rather than after it.
  const missing = missingVars(declared, draft.vars);
  if (missing.length) problems.push(missingVarsSentence(missing));
  if (draft.windowOn && draft.windowStart === draft.windowEnd) {
    problems.push("Calling hours that start and end at the same time never open.");
  }
  if (rampNeverRises(draft.dialDailyCap)) problems.push("A ramp has to end higher than it starts.");
  if (draft.startMode === "at" && !draft.startDate) problems.push("Pick a start date.");
  return problems;
}
