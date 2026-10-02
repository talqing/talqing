import type {
  CallBatchRecipientStatus,
  CallBatchResponse,
  CallBatchStatus,
} from "@talqing/sdk";
import type { BadgeVariant } from "@/app/components/ui";

/* One vocabulary for the whole feature. The API's status strings are precise
   and the labels below are what an operator reads — "Waiting" rather than
   "scheduled", because a batch that has not started is waiting for its clock and
   nothing is wrong with it. */

export const BATCH_STATUS_LABEL: Record<CallBatchStatus, string> = {
  scheduled: "Scheduled",
  running: "Running",
  paused: "Paused",
  completed: "Completed",
  canceled: "Canceled",
  failed: "Stopped",
};

export const BATCH_STATUS_VARIANT: Record<CallBatchStatus, BadgeVariant> = {
  scheduled: "info",
  running: "live",
  paused: "warn",
  completed: "default",
  canceled: "default",
  failed: "danger",
};

export const RECIPIENT_STATUS_LABEL: Record<CallBatchRecipientStatus, string> = {
  pending: "Not called",
  dialing: "Calling",
  completed: "Reached",
  failed: "Failed",
  canceled: "Canceled",
};

export const RECIPIENT_STATUS_VARIANT: Record<CallBatchRecipientStatus, BadgeVariant> = {
  pending: "default",
  dialing: "live",
  completed: "info",
  failed: "danger",
  canceled: "default",
};

/** `failure_reason` as a sentence. Setup failures arrive as the API's own error
 *  fragments ("this batch's agent … has been deleted"); others are already
 *  sentences ("This workspace is out of credits. …"). */
export function asSentence(reason: string): string {
  const text = reason.trim();
  return text.charAt(0).toUpperCase() + text.slice(1) + (/[.!?]$/.test(text) ? "" : ".");
}

/** Statuses a batch can still be edited, paused and added to in, which is also
 *  what the API will accept. A completed batch is idle, not over. */
export const isSteerable = (s: CallBatchStatus) =>
  s === "scheduled" || s === "running" || s === "paused" || s === "completed";

/** Statuses a batch can still be canceled in; the rest can be deleted. */
export const isCancelable = (s: CallBatchStatus) =>
  s === "scheduled" || s === "running" || s === "paused";

export const ISO_WEEKDAYS = [
  { value: 1, short: "Mon" },
  { value: 2, short: "Tue" },
  { value: 3, short: "Wed" },
  { value: 4, short: "Thu" },
  { value: 5, short: "Fri" },
  { value: 6, short: "Sat" },
  { value: 7, short: "Sun" },
] as const;

/* A time this batch is WAITING for is rendered on the batch's own clock, not the
   reader's: a dispatcher in Delhi watching a New York campaign wants to know it
   opens at 10:00 there, and 20:30 would be technically true and useless. The
   zone is always printed alongside, so nobody has to guess which clock it is.

   A time that has already PASSED gets `timeAgo` instead — "ended 4m ago" is what
   a reader wants from a finished row, and no zone can make that ambiguous. */
export function inZone(iso: string | null | undefined, timeZone: string): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString(undefined, {
    timeZone,
    weekday: "short",
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  });
}

/** Weekday and clock only — for a row cell, where the date is either today or
 *  obvious from the weekday and the full string does not fit. */
export function inZoneShort(iso: string | null | undefined, timeZone: string): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString(undefined, {
    timeZone,
    weekday: "short",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function timeAgo(iso: string | null | undefined): string {
  const t = iso ? new Date(iso).getTime() : 0;
  if (!t) return "";
  const s = Math.max(1, Math.floor((Date.now() - t) / 1000));
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.floor(h / 24)}d ago`;
}

export function zoneAbbreviation(iso: string | null | undefined, timeZone: string): string {
  const at = iso ? new Date(iso) : new Date();
  const part = new Intl.DateTimeFormat("en-US", { timeZone, timeZoneName: "short" })
    .formatToParts(at)
    .find((p) => p.type === "timeZoneName");
  return part?.value ?? timeZone;
}

/** `10:00:00` → `10:00`. The API stores seconds; nobody schedules by them. */
export const hhmm = (t: string) => t.slice(0, 5);

/**
 * A local wall time in some zone, as the absolute instant the API wants.
 *
 * Two passes rather than one: the first guesses the zone's offset by asking
 * what the naive instant looks like there, the second re-asks at the corrected
 * instant, which is what makes a time on the far side of a DST change land on
 * the right hour. A wall time inside a spring-forward gap does not exist and
 * resolves to the hour after it — the same thing every calendar app does.
 */
export function wallTimeToInstant(date: string, time: string, timeZone: string): string {
  const naive = Date.parse(`${date}T${time}:00Z`);
  let guess = naive;
  for (let pass = 0; pass < 2; pass++) {
    guess = naive + (guess - zoneWallClock(guess, timeZone));
  }
  return new Date(guess).toISOString();
}

/** What `at` reads as on `timeZone`'s wall clock, expressed as a UTC epoch. */
function zoneWallClock(at: number, timeZone: string): number {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone,
    hour12: false,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).formatToParts(new Date(at));
  const get = (type: string) => Number(parts.find((p) => p.type === type)?.value ?? 0);
  // `hour12: false` renders midnight as 24 in some engines.
  return Date.UTC(
    get("year"),
    get("month") - 1,
    get("day"),
    get("hour") % 24,
    get("minute"),
    get("second"),
  );
}

/** The instant, split back into the date and time fields of a zone. */
export function instantToWallTime(iso: string, timeZone: string): { date: string; time: string } {
  const wall = new Date(zoneWallClock(Date.parse(iso), timeZone));
  return { date: wall.toISOString().slice(0, 10), time: wall.toISOString().slice(11, 16) };
}

/** How far along a batch is: everyone whose row has stopped moving. */
export function settledShare(batch: CallBatchResponse): number {
  const { total, completed, failed, canceled } = batch.counts;
  if (!total) return 0;
  return Math.min(1, (completed + failed + canceled) / total);
}
