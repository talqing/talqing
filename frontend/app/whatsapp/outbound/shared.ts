import type {
  IntegrationResponse,
  WhatsAppBatchResponse,
  WhatsAppRecipientResponse,
} from "@talqing/sdk";
import type { BadgeVariant } from "@/app/components/ui";

type WhatsAppBatchStatus = WhatsAppBatchResponse["status"];
type WhatsAppRecipientStatus = WhatsAppRecipientResponse["status"];
type WhatsAppRecipientFilter = WhatsAppRecipientStatus | "replied";

export const BATCH_STATUS_LABEL: Record<WhatsAppBatchStatus, string> = {
  draft: "Draft",
  scheduled: "Scheduled",
  sending: "Sending",
  paused: "Paused",
  completed: "Completed",
  canceled: "Canceled",
  failed: "Failed",
};

export const BATCH_STATUS_VARIANT: Record<WhatsAppBatchStatus, BadgeVariant> = {
  draft: "default",
  scheduled: "info",
  sending: "live",
  paused: "warn",
  completed: "default",
  canceled: "default",
  failed: "danger",
};

export const ROW_STATUS_LABEL: Record<WhatsAppRecipientStatus, string> = {
  ready: "Ready",
  skipped: "Skipped",
  sending: "Sending",
  queued: "Sent",
  sent: "Sent",
  delivered: "Delivered",
  read: "Read",
  undelivered: "Undelivered",
  failed: "Failed",
};

export const ROW_STATUS_VARIANT: Record<WhatsAppRecipientStatus, BadgeVariant> = {
  ready: "default",
  skipped: "default",
  sending: "info",
  queued: "info",
  sent: "info",
  delivered: "live",
  read: "live",
  undelivered: "danger",
  failed: "danger",
};

export const WAIT_REASON: Record<NonNullable<WhatsAppBatchResponse["next_send_reason"]>, string> = {
  start: "Scheduled start",
  window: "Outside sending hours",
  daily_cap: "Daily cap reached",
  retry: "Waiting to send again",
  gap: "Pacing",
};

/** The review tabs, each a set of stored statuses. They count what the figures
 *  above them count: a read row was sent and delivered first, so it is in all
 *  three. */
export type Tab = "all" | "ready" | "skipped" | "sent" | "delivered" | "read" | "replied" | "failed";
export const TAB_FILTER: Record<Tab, WhatsAppRecipientFilter[] | undefined> = {
  all: undefined,
  ready: ["ready"],
  skipped: ["skipped"],
  sent: ["queued", "sent", "delivered", "read", "undelivered", "failed"],
  delivered: ["delivered", "read"],
  read: ["read"],
  replied: ["replied"],
  failed: ["failed", "undelivered"],
};

/** Rows that left, whatever happened to them after. */
export const sentCount = (b: WhatsAppBatchResponse) =>
  b.counts.queued + b.counts.sent + b.counts.delivered + b.counts.read + b.counts.undelivered + b.counts.failed;

/** Delivered or better — a read message was delivered first. */
export const deliveredCount = (b: WhatsAppBatchResponse) => b.counts.delivered + b.counts.read;

export function tabCount(batch: WhatsAppBatchResponse, tab: Tab): number {
  const c = batch.counts;
  return {
    all: c.total,
    ready: c.ready,
    skipped: c.skipped,
    sent: sentCount(batch),
    delivered: deliveredCount(batch),
    read: c.read,
    replied: c.replied,
    failed: c.failed + c.undelivered,
  }[tab];
}

export const isLive = (s: WhatsAppBatchStatus) => s === "scheduled" || s === "sending";

/** A pause the batch took itself (a breaker, a credential, a limit) says why. */
export const pausedItself = (b: { status: string; failure_reason: string | null }) =>
  b.status === "paused" && !!b.failure_reason;

/** Why a row failed, in the words a person would use. Falls back to the BSP's. */
const PLAIN_ERRORS: Record<string, string> = {
  "63024": "Not on WhatsApp",
  "1002": "Not on WhatsApp",
  "63003": "Not a valid WhatsApp number",
  "63049": "Meta's marketing limit for this person",
  "131049": "Meta's marketing limit for this person",
  "63016": "Outside the 24-hour reply window",
  "470": "Outside the 24-hour reply window",
  "63013": "WhatsApp refused a variable (blank, line break or extra spaces)",
  "63033": "Opted out of your messages",
  "63050": "Opted out of marketing messages",
  "63058": "Your business can't message this country",
  "63018": "WhatsApp's messaging limit was reached",
  "63038": "The account's daily message limit was reached",
  "63020": "Meta Business Manager has not accepted Twilio",
  "63040": "WhatsApp rejected the template",
  "63041": "WhatsApp paused the template",
  "63042": "WhatsApp disabled the template",
};

export function plainError(row: WhatsAppRecipientResponse): string | null {
  // A person's own skip needs no reason; one the list caused says which cell.
  if (row.status === "skipped") return row.skip_reason === "operator" ? null : row.last_error;
  return (row.error_code && PLAIN_ERRORS[row.error_code]) || row.last_error;
}

/** When a row Meta held back is sent again; null for any other row, and once the
 *  batch has stopped for good. */
export const resendsAt = (row: WhatsAppRecipientResponse, batch: WhatsAppBatchResponse): string | null =>
  row.status === "undelivered" && (isLive(batch.status) || batch.status === "paused") ? row.next_attempt_at : null;

export const whatsAppSenders = (items: IntegrationResponse[]) =>
  items.filter((i) => i.provider === "whatsapp");

/** The row's cells as they will be sent: the CSV's, with a person's edits on top. */
export const rowCells = (row: WhatsAppRecipientResponse): Record<string, string> => ({
  ...row.input,
  ...row.overrides,
});

/** `+14155550100` → `+1 415 555 0100`-ish grouping, for reading only. */
export function formatPhone(e164: string): string {
  const digits = e164.replace(/^\+/, "");
  if (digits.startsWith("1") && digits.length === 11)
    return `+1 ${digits.slice(1, 4)} ${digits.slice(4, 7)} ${digits.slice(7)}`;
  if (digits.startsWith("91") && digits.length === 12)
    return `+91 ${digits.slice(2, 7)} ${digits.slice(7)}`;
  return e164;
}
