import type {
  EmailBatchResponse,
  EmailBatchStatus,
  EmailRecipientStatus,
  EmailSendResponse,
  EmailSendStatus,
} from "@talqing/sdk";
import type { BadgeVariant } from "@/app/components/ui";
import type { DailyCap } from "@/app/components/Pacing";

/* One vocabulary for the whole feature.

   The most important word here is "Drafted". The API's batch status is about
   DRAFTING and nothing else, and a batch that reached the end of its list has
   not "completed" anything — nobody has been emailed until a person picks rows
   and creates a send. Every label below keeps that distinction visible, because
   it is the one place the two-object split could mislead an operator. */

export const BATCH_STATUS_LABEL: Record<EmailBatchStatus, string> = {
  scheduled: "Scheduled",
  drafting: "Drafting",
  paused: "Paused",
  drafted: "Ready to review",
  canceled: "Canceled",
  failed: "Stopped",
};

export const BATCH_STATUS_VARIANT: Record<EmailBatchStatus, BadgeVariant> = {
  scheduled: "info",
  drafting: "live",
  paused: "warn",
  drafted: "info",
  canceled: "default",
  failed: "danger",
};

export const SEND_STATUS_LABEL: Record<EmailSendStatus, string> = {
  scheduled: "Scheduled",
  sending: "Sending",
  paused: "Paused",
  sent: "Sent",
  canceled: "Canceled",
  failed: "Stopped",
};

export const SEND_STATUS_VARIANT: Record<EmailSendStatus, BadgeVariant> = {
  scheduled: "info",
  sending: "live",
  paused: "warn",
  sent: "live",
  canceled: "default",
  failed: "danger",
};

export const ROW_STATUS_LABEL: Record<EmailRecipientStatus, string> = {
  pending: "Not drafted",
  drafting: "Drafting",
  draft: "Draft",
  draft_failed: "Draft failed",
  skipped: "Skipped",
  queued: "Queued",
  sending: "Sending",
  sent: "Sent",
  send_failed: "Send failed",
  canceled: "Canceled",
};

export const ROW_STATUS_VARIANT: Record<EmailRecipientStatus, BadgeVariant> = {
  pending: "default",
  drafting: "live",
  draft: "info",
  draft_failed: "danger",
  skipped: "default",
  queued: "warn",
  sending: "live",
  sent: "live",
  send_failed: "danger",
  canceled: "default",
};

/** Statuses a human can still steer. `drafted` is not one: there is nothing
 *  left to pause, and cancelling would only take away drafts worth sending. */
export const isSteerable = (s: EmailBatchStatus) =>
  s === "scheduled" || s === "drafting" || s === "paused";

/* Why a batch or a send is waiting, in the operator's words rather than the
   API's. Rendered under the time itself: "00:00" is a fact and "00:00 — daily
   cap reached" is an explanation, and the page used to guess the second one by
   comparing two other numbers, which was wrong whenever a send had its own cap. */

export const DRAFT_WAIT_REASON: Record<
  NonNullable<EmailBatchResponse["next_draft_reason"]>,
  string
> = {
  start: "waiting to start",
  retry: "retrying failed rows",
};

export const SEND_WAIT_REASON: Record<
  NonNullable<EmailSendResponse["next_send_reason"]>,
  string
> = {
  start: "waiting to start",
  window: "outside sending hours",
  daily_cap: "daily cap reached",
  retry: "retrying failed rows",
  gap: "pacing",
  drafting: "waiting for drafting",
};

/** Did this stop itself, rather than a person stopping it?
 *
 *  A `paused` subject with a reason on it is the circuit breaker: ten failures
 *  in a row, so it held everything where it was and stopped. That reads exactly
 *  like a deliberate pause unless the page says otherwise, and the two want very
 *  different things from the person looking at them. */
export const pausedItself = (s: { status: string; failure_reason: string | null }) =>
  s.status === "paused" && !!s.failure_reason;

/** A send that is still going to do something. Decides which of a send's buttons show. */
export const isSendLive = (s: EmailSendStatus) => s === "scheduled" || s === "sending";

/** A send that still covers its rows — paused included, because a paused send
 *  has stopped rather than ended. The API refuses a second send beside a live
 *  send of every row, and this is the same set it counts. */
export const coversRows = (s: EmailSendStatus) => isSendLive(s) || s === "paused";

/** Is drafting still able to produce rows? Then a send of every row has
 *  something to wait for. */
export const isDraftingOn = (s: EmailBatchStatus) =>
  s === "scheduled" || s === "drafting" || s === "paused";

/** The two totals a progress figure can actually reach.
 *
 *  Measuring against `counts.total` put rows no email can ever reach inside the
 *  denominator — "Sent 79 / 1,499" on a batch where 739 rows had no address —
 *  so the bars never filled and the number read as a miscount. An `unfillable`
 *  row is never drafted; a skipped, failed or cancelled one is never sent. */
export function reachable(batch: EmailBatchResponse): { drafting: number; sending: number } {
  const { counts, skips } = batch;
  return {
    drafting: counts.total - skips.unfillable,
    sending: counts.total - counts.skipped - counts.draft_failed - counts.canceled,
  };
}

/** The mapped columns the task does not write — the only ones that can make a
 *  row `unfillable`, so the ones to name when saying why. */
export const unwrittenColumns = (batch: EmailBatchResponse): string[] =>
  [...new Set(Object.values(batch.field_map))].filter((c) => !batch.output_columns.includes(c));

/** "one email a minute — 1,240 rows take about 21 hours".
 *
 *  The arithmetic is the whole reason the field exists and nobody does it in
 *  their head, so the consequence is rendered rather than the number left to
 *  speak for itself. A gap may now be an hour and a send is no longer capped at
 *  fifty rows, which is exactly when the second half stops being obvious. */
export function paceSentence(seconds: number, rows: number): string {
  const each =
    seconds === 1
      ? "One email a second"
      : seconds < 60
        ? `One email every ${seconds}s`
        : seconds === 60
          ? "One email a minute"
          : seconds % 3600 === 0
            ? `One email every ${seconds / 3600} h`
            : `One email every ${Math.round(seconds / 60)} min`;
  const perHour = Math.round(3600 / seconds);
  const rate = perHour >= 1 ? `${perHour.toLocaleString()}/hour` : "under one an hour";
  if (rows <= 1) return `${each} — ${rate}.`;
  return `${each} — ${rate}, so ${rows.toLocaleString()} rows take ${duration(seconds * (rows - 1))}.`;
}

/** Seconds as the coarsest unit that still says something useful. */
export function duration(seconds: number): string {
  // A send of one email has no gap to wait out at all.
  if (seconds < 1) return "under a second";
  const about = (n: number, unit: string) => `about ${n} ${unit}${n === 1 ? "" : "s"}`;
  if (seconds < 90) return about(Math.round(seconds), "second");
  const minutes = seconds / 60;
  if (minutes < 90) return about(Math.round(minutes), "minute");
  const hours = minutes / 60;
  if (hours < 36) return about(Math.round(hours), "hour");
  return about(Math.round(hours / 24), "day");
}

/** How long a send takes, and what bounds it when it is not the pace alone.
 *
 *  Three things decide how many go out in a day: the gap, the sending hours the
 *  gap has to fit in, and the daily limit. 1,142 rows at one every 5 minutes is
 *  four days of clock time, but 10:30–17:00 holds 78 of them, so it is fifteen
 *  sending days — and a cap of 150 never comes into it. A ramp raises the limit
 *  as the days go, so the days are walked rather than divided. `why` names the
 *  bound that made it days, and is empty when the send fits in one.
 *
 *  `today` is the limit in force today (null for none) under `cap`. */
export function sendSpan(
  rows: number,
  gapSeconds: number,
  cap: DailyCap | null,
  today: number | null,
  window: { start: string; end: string; days?: number[] } | null,
): { span: string; why: string } {
  const seconds = (hhmm: string) => Number(hhmm.slice(0, 2)) * 3600 + Number(hhmm.slice(3, 5)) * 60;
  // An end before the start is a window that runs past midnight.
  const open = window ? (seconds(window.end) - seconds(window.start) + 86400) % 86400 : 86400;
  const fits = Math.max(Math.floor(open / gapSeconds), 1);
  const first = Math.min(fits, today ?? Infinity);
  if (rows <= first) return { span: duration(gapSeconds * Math.max(rows - 1, 0)), why: "" };

  let days = 0;
  for (let left = rows; left > 0; days += 1) {
    const limit =
      cap?.kind === "ramp" && today !== null
        ? Math.min(cap.end, today + cap.step * Math.floor(days / cap.interval_days))
        : (today ?? Infinity);
    left -= Math.min(fits, limit);
  }
  const weekdays = window?.days?.length ?? 7;
  const weeks = Math.ceil(days / weekdays);
  return {
    span:
      weekdays < 7 && days > weekdays
        ? `${days} sending days, about ${weeks} week${weeks === 1 ? "" : "s"}`
        : `${days} ${weekdays < 7 ? "sending " : ""}day${days === 1 ? "" : "s"}`,
    why:
      today === null || today > fits
        ? `${window ? "the sending hours fit" : "the pace allows"} ${fits.toLocaleString()} a day`
        : cap?.kind === "ramp" && today < cap.end
          ? `ramping from ${today.toLocaleString()} to ${cap.end.toLocaleString()} a day`
          : `capped at ${today.toLocaleString()} a day`,
  };
}

/** Statuses whose cells a person may still change. A `queued` row is a draft a
 *  send has not taken yet, and goes out as edited. Past these the row describes
 *  an email that exists, and editing it would describe one that does not. The
 *  API refuses the rest with a 409, so this is the same list as `patch_recipient`. */
export const isEditableRow = (s: EmailRecipientStatus) =>
  s === "pending" || s === "draft" || s === "queued" || s === "draft_failed" || s === "skipped";

/** Can this row go? A reviewed draft whose three mapped fields actually resolve
 *  — both halves, in one place, because `status === "draft"` on its own is the
 *  reading that puts an unsendable row in a selection. */
export const isSendableRow = (row: {
  status: EmailRecipientStatus;
  not_ready_reason: string | null;
}) => row.status === "draft" && !row.not_ready_reason;

/** What is wrong with this row, as the sentence under its status badge — and
 *  the `error` column of an export, which is why it lives here.
 *
 *  `not_ready_reason` is set on any row whose three mapped fields do not
 *  resolve, which on a row that has not been drafted yet is simply true and not
 *  worth saying. Only a DRAFT that cannot go is news. */
export const rowError = (row: {
  status: EmailRecipientStatus;
  last_error: string | null;
  not_ready_reason: string | null;
}): string | null =>
  row.last_error || (row.status === "draft" ? row.not_ready_reason : null);
