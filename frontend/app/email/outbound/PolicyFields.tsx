"use client";

import type {
  CreateEmailBatchRequest,
  EmailBatchResponse,
  IntegrationResponse,
  PatchEmailBatchRequest,
  TaskResponse,
} from "@talqing/sdk";
import { BoxCheckbox, Field, Input, Segment, Select, Tooltip } from "@/app/components/ui";
import { DatePicker } from "@/app/components/DatePicker";
import { cn } from "@/lib/cn";
import { localTimezone, timezones, today } from "@/lib/date";
import { ISO_WEEKDAYS, hhmm, instantToWallTime, wallTimeToInstant } from "@/app/telephony/outbound-calling/shared";
import {
  type CapProgress,
  type DailyCap,
  DailyLimit,
  clamp,
  rampNeverRises,
} from "@/app/components/Pacing";
import { paceSentence } from "./shared";

/* Everything about a batch except the list and the field map — the same fields
   at create time and when editing a live one, because they are the same fields.
   The state lives in the caller so both screens can validate and submit their
   own way; this only draws it. */

export type PolicyDraft = {
  name: string;
  taskId: string;
  integrationId: string;
  fromEmail: string;
  fromName: string;
  replyTo: string;
  bodyFormat: "text" | "html";
  startMode: "now" | "at";
  startDate: string;
  startTime: string;
  timezone: string;
  windowOn: boolean;
  windowStart: string;
  windowEnd: string;
  windowDays: number[];
  draftConcurrency: number;
  draftAttempts: number;
  draftRetryAfterMinutes: number;
  draftGapSeconds: number;
  sendGapSeconds: number;
  sendAttempts: number;
  sendRetryAfterMinutes: number;
  sendDailyCap: DailyCap | null;
};

export function emptyDraft(): PolicyDraft {
  return {
    name: "",
    taskId: "",
    integrationId: "",
    fromEmail: "",
    fromName: "",
    replyTo: "",
    bodyFormat: "text",
    startMode: "now",
    startDate: today(),
    startTime: "09:00",
    timezone: localTimezone(),
    windowOn: false,
    windowStart: "09:00",
    windowEnd: "18:00",
    windowDays: [1, 2, 3, 4, 5],
    draftConcurrency: 5,
    draftAttempts: 1,
    draftRetryAfterMinutes: 30,
    draftGapSeconds: 0,
    sendGapSeconds: 1,
    sendAttempts: 3,
    sendRetryAfterMinutes: 30,
    sendDailyCap: { kind: "fixed", limit: 200 },
  };
}

export function draftFromBatch(batch: EmailBatchResponse): PolicyDraft {
  const base = emptyDraft();
  const start = batch.start_at ? instantToWallTime(batch.start_at, batch.timezone) : null;
  return {
    ...base,
    name: batch.name,
    taskId: batch.task_id ?? "",
    integrationId: batch.integration_id ?? "",
    fromEmail: batch.from_email,
    fromName: batch.from_name ?? "",
    replyTo: batch.reply_to ?? "",
    bodyFormat: batch.body_format,
    startMode: start ? "at" : "now",
    startDate: start?.date ?? base.startDate,
    startTime: start?.time ?? base.startTime,
    timezone: batch.timezone,
    windowOn: !!batch.window,
    windowStart: batch.window ? hhmm(batch.window.start) : base.windowStart,
    windowEnd: batch.window ? hhmm(batch.window.end) : base.windowEnd,
    windowDays: batch.window?.days ?? base.windowDays,
    draftConcurrency: batch.draft_concurrency,
    draftAttempts: batch.draft_attempts,
    draftRetryAfterMinutes: batch.draft_retry_after_minutes,
    draftGapSeconds: batch.draft_gap_seconds,
    sendGapSeconds: batch.send_gap_seconds,
    sendAttempts: batch.send_attempts,
    sendRetryAfterMinutes: batch.send_retry_after_minutes,
    sendDailyCap: batch.send_daily_cap,
  };
}

/** The half of the draft both create and patch send verbatim. */
function paceOf(draft: PolicyDraft) {
  return {
    timezone: draft.timezone,
    window: draft.windowOn
      ? { start: draft.windowStart, end: draft.windowEnd, days: draft.windowDays }
      : null,
    draft_concurrency: draft.draftConcurrency,
    draft_attempts: draft.draftAttempts,
    draft_retry_after_minutes: draft.draftRetryAfterMinutes,
    draft_gap_seconds: draft.draftGapSeconds,
    send_gap_seconds: draft.sendGapSeconds,
    send_attempts: draft.sendAttempts,
    send_retry_after_minutes: draft.sendRetryAfterMinutes,
    send_daily_cap: draft.sendDailyCap,
  };
}

function startAtOf(draft: PolicyDraft) {
  return draft.startMode === "at"
    ? wallTimeToInstant(draft.startDate, draft.startTime, draft.timezone)
    : null;
}

export function toCreateRequest(
  draft: PolicyDraft,
  fieldMap: { to: string; subject: string; body: string },
  recipients: CreateEmailBatchRequest["recipients"],
): CreateEmailBatchRequest {
  return {
    name: draft.name.trim(),
    task_id: draft.taskId,
    integration_id: draft.integrationId,
    from_email: draft.fromEmail.trim(),
    from_name: draft.fromName.trim() || null,
    reply_to: draft.replyTo.trim() || null,
    body_format: draft.bodyFormat,
    field_map: fieldMap,
    recipients,
    start_at: startAtOf(draft),
    ...paceOf(draft),
  };
}

/** Sends every field, including explicit nulls — which is how the API is told
 *  to *clear* a window, a start time or the daily cap rather than leave it
 *  alone. Null on anything else is now a 400 rather than a silent no-op, so
 *  nothing here may send one.
 *
 *  The identity half is omitted once anything has been sent: the API refuses it
 *  then, and sending it unchanged would still be refused. */
export function toPatchRequest(
  draft: PolicyDraft,
  { startEditable, identityEditable }: { startEditable: boolean; identityEditable: boolean },
): PatchEmailBatchRequest {
  return {
    name: draft.name.trim(),
    ...paceOf(draft),
    ...(startEditable ? { start_at: startAtOf(draft) } : {}),
    ...(identityEditable
      ? {
          from_email: draft.fromEmail.trim(),
          from_name: draft.fromName.trim() || null,
          reply_to: draft.replyTo.trim() || null,
          body_format: draft.bodyFormat,
        }
      : {}),
  };
}

/** Connected accounts a batch can actually send through. */
export function emailAccounts(integrations: IntegrationResponse[]): IntegrationResponse[] {
  return integrations
    .filter((i) => i.provider === "resend" && i.status === "active")
    .sort((a, b) => a.display_name.localeCompare(b.display_name));
}

/** A field that is not a field: a value the form shows and cannot change.
 *  Sized and inset to sit in the same rhythm as the inputs beside it, so a row
 *  of settings does not visibly break where one of them is fixed. */
function Fixed({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex h-9 min-w-0 items-center truncate rounded-lg border border-line-2 bg-canvas px-3 text-[13.5px] leading-5 text-muted">
      {children}
    </div>
  );
}

export function PolicyFields({
  draft,
  onChange,
  tasks,
  accounts,
  startEditable = true,
  identityEditable = true,
  creating = true,
  savedCap,
  className,
}: {
  draft: PolicyDraft;
  onChange: (next: PolicyDraft) => void;
  tasks: TaskResponse[];
  accounts: IntegrationResponse[];
  /** False once drafting has begun: moving the start time of something already
   *  running means nothing, and the API refuses it. */
  startEditable?: boolean;
  /** False once anything has been sent: the rows already delivered would no
   *  longer describe what they were sent as. */
  identityEditable?: boolean;
  /** False on the edit form. The task and the account are fixed from the moment
   *  a batch exists — the API does not accept either on a PATCH and never has —
   *  so they are shown rather than offered. They used to render as live
   *  dropdowns that moved, saved, said "Saved", and changed nothing. */
  creating?: boolean;
  /** The limit as saved and today's value under it, on the edit form. */
  savedCap?: CapProgress;
  className?: string;
}) {
  const set = <K extends keyof PolicyDraft>(key: K, value: PolicyDraft[K]) =>
    onChange({ ...draft, [key]: value });


  const toggleDay = (day: number) => {
    const next = draft.windowDays.includes(day)
      ? draft.windowDays.filter((d) => d !== day)
      : [...draft.windowDays, day].sort((a, b) => a - b);
    if (next.length) set("windowDays", next);
  };

  const crossesMidnight = draft.windowStart > draft.windowEnd;

  return (
    <div className={cn("grid gap-5", className)}>
      <Field label="Name" htmlFor="email-batch-name" hint="What this list is for — only you see it.">
        <Input
          id="email-batch-name"
          value={draft.name}
          onChange={(e) => set("name", e.target.value)}
          placeholder="Indian D2C brands, week 34"
          maxLength={200}
        />
      </Field>

      <div className="grid gap-5 sm:grid-cols-2">
        <Field
          label="Task"
          hint={
            creating
              ? "Runs once per row, as currently published. Its output columns sit beside your own, and either can be mapped."
              : "Fixed when the batch was created. Drafting follows whatever version of it is published."
          }
        >
          {creating ? (
            <Select
              value={draft.taskId}
              onChange={(e) => set("taskId", e.target.value)}
              searchable={tasks.length > 20}
              aria-label="Task"
            >
              <option value="">Choose a task</option>
              {tasks.map((t) => (
                <option key={t.id} value={t.id}>
                  {t.config.name}
                </option>
              ))}
            </Select>
          ) : (
            <Fixed>
              {tasks.find((t) => t.id === draft.taskId)?.config.name ?? "Task deleted"}
            </Fixed>
          )}
        </Field>
        <Field
          label="Send through"
          hint={
            creating
              ? "A connected Resend account. One per batch."
              : "Fixed when the batch was created — a different account is a different batch."
          }
        >
          {creating ? (
            <Select
              value={draft.integrationId}
              onChange={(e) => set("integrationId", e.target.value)}
              aria-label="Email account"
            >
              <option value="">Choose an account</option>
              {accounts.map((a) => (
                <option key={a.id} value={a.id}>
                  {a.display_name}
                </option>
              ))}
            </Select>
          ) : (
            <Fixed>
              {accounts.find((a) => a.id === draft.integrationId)?.display_name ??
                "Account disconnected"}
            </Fixed>
          )}
        </Field>
      </div>

      <div className="grid gap-5 sm:grid-cols-2">
        <Field
          label="From"
          htmlFor="email-from"
          hint="Its domain has to be verified on that account. A single send can use a different one."
        >
          <Input
            id="email-from"
            value={draft.fromEmail}
            onChange={(e) => set("fromEmail", e.target.value)}
            disabled={!identityEditable}
            placeholder="hello@yourdomain.com"
            autoComplete="off"
          />
        </Field>
        <Field label="From name" htmlFor="email-from-name" hint="Optional. Shown beside the address.">
          <Input
            id="email-from-name"
            value={draft.fromName}
            onChange={(e) => set("fromName", e.target.value)}
            disabled={!identityEditable}
            placeholder="Aniket at Talqing"
            maxLength={200}
          />
        </Field>
      </div>

      <div className="grid gap-5 sm:grid-cols-2">
        <Field
          label="Reply to"
          htmlFor="email-reply-to"
          hint="Optional, and where replies land — Talqing does not read them."
        >
          <Input
            id="email-reply-to"
            value={draft.replyTo}
            onChange={(e) => set("replyTo", e.target.value)}
            disabled={!identityEditable}
            placeholder="you@yourdomain.com"
            autoComplete="off"
          />
        </Field>
        <Field
          label="Body"
          hint="Plain text lands in more inboxes. Pick HTML only if the task writes HTML."
        >
          <Segment
            value={draft.bodyFormat}
            onChange={(v) => identityEditable && set("bodyFormat", v)}
            options={[
              { value: "text", label: "Plain text" },
              { value: "html", label: "HTML" },
            ]}
          />
        </Field>
      </div>

      <div className="grid gap-2">
        <div className="flex flex-wrap items-start gap-x-4 gap-y-3">
          <Field label="Start drafting">
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
          The batch keeps its own clock, and this is about <strong>drafting</strong> only. Nothing
          is emailed until you review the drafts and create a send yourself.
          {!startEditable && " This batch has already started, so its start time is fixed."}
        </p>
      </div>

      <Section
        title="Drafting pace"
        hint="Each row is a model run on your own provider keys, and every row is billed whether or not it is ever sent."
      >
        <div className="grid gap-5 sm:grid-cols-2 lg:grid-cols-4">
          <Field
            label="Rows drafting at once"
            htmlFor="email-concurrency"
            hint="1–10. Nothing else limits this."
          >
            <Input
              id="email-concurrency"
              type="number"
              min={1}
              max={10}
              value={draft.draftConcurrency}
              onChange={(e) => set("draftConcurrency", clamp(e.target.value, 1, 10))}
            />
          </Field>
          <Field
            label="Wait between rows"
            htmlFor="email-draft-gap"
            hint="Seconds. 0 starts each row as a slot frees — raise it only if the task calls something that rate-limits."
          >
            <Input
              id="email-draft-gap"
              type="number"
              min={0}
              max={3600}
              value={draft.draftGapSeconds}
              onChange={(e) => set("draftGapSeconds", clamp(e.target.value, 0, 3600))}
            />
          </Field>
          <Field
            label={
              <Tooltip label="A row whose task returned nothing useful is not retried — that produces the same non-answer.">
                <span>Attempts each</span>
              </Tooltip>
            }
            htmlFor="email-attempts"
            hint="1–5. Only a timeout or a provider failure is tried again."
          >
            <Input
              id="email-attempts"
              type="number"
              min={1}
              max={5}
              value={draft.draftAttempts}
              onChange={(e) => set("draftAttempts", clamp(e.target.value, 1, 5))}
            />
          </Field>
          <Field
            label="Wait before retrying"
            htmlFor="email-retry"
            hint="Minutes, 5–1440."
            className={draft.draftAttempts > 1 ? "" : "opacity-50"}
          >
            <Input
              id="email-retry"
              type="number"
              min={5}
              max={1440}
              step={5}
              disabled={draft.draftAttempts <= 1}
              value={draft.draftRetryAfterMinutes}
              onChange={(e) => set("draftRetryAfterMinutes", clamp(e.target.value, 5, 1440))}
            />
          </Field>
        </div>
      </Section>

      <Section
        title="Sending"
        hint="Every send this batch creates starts from these, and can override any of them."
      >
        <div className="grid gap-4">
          <div className="flex items-center gap-2.5">
            <BoxCheckbox
              checked={draft.windowOn}
              onChange={(on) => set("windowOn", on)}
              ariaLabel="Only send during these hours"
            />
            <button
              type="button"
              onClick={() => set("windowOn", !draft.windowOn)}
              className="text-[14px] font-semibold leading-5 text-ink"
            >
              Only send during these hours
            </button>
          </div>
          {draft.windowOn ? (
            <div className="grid gap-3 rounded-xl border border-line-2 bg-white p-3.5">
              <div className="flex flex-wrap items-center gap-2.5">
                <Input
                  type="time"
                  value={draft.windowStart}
                  onChange={(e) => set("windowStart", e.target.value)}
                  aria-label="Sending hours start"
                  className="w-[130px]"
                />
                <span className="text-[13px] text-muted">to</span>
                <Input
                  type="time"
                  value={draft.windowEnd}
                  onChange={(e) => set("windowEnd", e.target.value)}
                  aria-label="Sending hours end"
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
                    Outside these hours a send waits rather than stopping, and picks up again when
                    they next open. Drafting is not affected — nobody receives a draft.
                  </>
                )}
              </p>
            </div>
          ) : (
            <p className="text-[13px] leading-5 text-muted">
              Email may leave at any hour. Business hours are usually worth setting: a message that
              arrives at 03:00 reads as automated before it is read at all.
            </p>
          )}

          <div className="grid gap-5 sm:grid-cols-3">
            <Field label="Wait between emails" htmlFor="email-pace" hint="Seconds, 1–3600.">
              <Input
                id="email-pace"
                type="number"
                min={1}
                max={3600}
                value={draft.sendGapSeconds}
                onChange={(e) => set("sendGapSeconds", clamp(e.target.value, 1, 3600))}
              />
            </Field>
            <Field
              label="Attempts each"
              htmlFor="email-send-attempts"
              hint={
                draft.sendAttempts === 1
                  ? "At 1, any transient provider failure is permanent — only a paid redraft recovers the row."
                  : "1–5. A rate limit is retried; a refused address is not."
              }
            >
              <Input
                id="email-send-attempts"
                type="number"
                min={1}
                max={5}
                value={draft.sendAttempts}
                onChange={(e) => set("sendAttempts", clamp(e.target.value, 1, 5))}
              />
            </Field>
            <Field
              label="Wait before retrying"
              htmlFor="email-send-retry"
              hint="Minutes, 5–1440."
              className={draft.sendAttempts > 1 ? "" : "opacity-50"}
            >
              <Input
                id="email-send-retry"
                type="number"
                min={5}
                max={1440}
                step={5}
                disabled={draft.sendAttempts <= 1}
                value={draft.sendRetryAfterMinutes}
                onChange={(e) => set("sendRetryAfterMinutes", clamp(e.target.value, 5, 1440))}
              />
            </Field>
          </div>
          <Field label="Daily limit">
            <div className="flex flex-wrap items-center gap-2">
              <DailyLimit
                value={draft.sendDailyCap}
                onChange={(cap) => set("sendDailyCap", cap)}
                noun="email"
                defaultLimit={200}
                saved={savedCap}
              />
            </div>
          </Field>
          <p className="text-[13px] leading-5 text-muted">
            {paceSentence(draft.sendGapSeconds, 200)}{" "}
            {draft.sendDailyCap ? (
              <>
                Sending stops at the daily limit, across every send of this batch, and resumes at
                midnight.
              </>
            ) : (
              <strong className="font-semibold text-warn-ink">
                No daily ceiling — a new sending domain that emits thousands in an afternoon is one
                nobody&rsquo;s inbox trusts afterwards.
              </strong>
            )}
          </p>
        </div>
      </Section>
    </div>
  );
}

/** A labelled group. The form has three distinct subjects now — what this batch
 *  is, how it drafts, how it sends — and a flat stack of fourteen inputs makes
 *  the reader work out which knob belongs to which half. */
function Section({
  title,
  hint,
  children,
}: {
  title: string;
  hint: string;
  children: React.ReactNode;
}) {
  return (
    <section className="grid gap-3 border-t border-line pt-5">
      <div>
        <h3 className="font-display text-[14.5px] font-semibold leading-5 tracking-tight text-ink">
          {title}
        </h3>
        <p className="mt-0.5 text-[12.5px] leading-5 text-muted">{hint}</p>
      </div>
      {children}
    </section>
  );
}

const EMAIL = /^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$/;

/** What is missing before this can be saved, in the order a reader would fix it. */
export function policyProblems(draft: PolicyDraft): string[] {
  const problems: string[] = [];
  if (!draft.name.trim()) problems.push("Give the batch a name.");
  if (!draft.taskId) problems.push("Choose the task that drafts each row.");
  if (!draft.integrationId) problems.push("Choose an account to send through.");
  if (!draft.fromEmail.trim()) problems.push("Say which address these go from.");
  else if (!EMAIL.test(draft.fromEmail.trim())) {
    problems.push(`“${draft.fromEmail.trim()}” is not an email address.`);
  }
  if (draft.replyTo.trim() && !EMAIL.test(draft.replyTo.trim())) {
    problems.push(`“${draft.replyTo.trim()}” is not an email address.`);
  }
  if (draft.windowOn && draft.windowStart === draft.windowEnd) {
    problems.push("Sending hours that start and end at the same time never open.");
  }
  if (rampNeverRises(draft.sendDailyCap)) problems.push("A ramp has to end higher than it starts.");
  if (draft.startMode === "at" && !draft.startDate) problems.push("Pick a start date.");
  return problems;
}
