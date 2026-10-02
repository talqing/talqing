import type { CallSummaryResponse, PhoneNumberResponse } from "@talqing/sdk";

/** What one number carried over the window, ready to render. */
export type NumberTraffic = {
  calls: number;
  spend: number;
  talkSeconds: number;
  /** One count per day, oldest first, `windowDays` long — or empty when the
      call list was capped. Calls arrive newest first, so a capped list is
      missing whole days off the far end: drawn as a sparkline it would show a
      number that surged this week when it only ever ran out of page. Totals
      survive being a floor; a shape does not. */
  days: number[];
};

export type TrafficReport = {
  byNumber: Map<string, NumberTraffic>;
  totals: NumberTraffic;
  /** The tallest single day across every number — the shared scale that makes
      two sparklines comparable. Without it a number with two calls draws the
      same silhouette as one with sixty. */
  peakDay: number;
  /** True when the call page was full, so every figure here is a floor. */
  capped: boolean;
};

const DAY_MS = 86_400_000;

/** Digits only: a carrier may report `+14155550142`, `14155550142` or
    `0014155550142` for the same DID, and none of those should split a number's
    traffic in two. */
function digits(value: string | null | undefined): string {
  return (value || "").replace(/\D/g, "");
}

function empty(windowDays: number): NumberTraffic {
  return { calls: 0, spend: 0, talkSeconds: 0, days: new Array(windowDays).fill(0) };
}

/**
 * Attribute calls to the workspace's own numbers.
 *
 * Calls are matched on either leg rather than on `type`, so an inbound call
 * (our number is the callee) and an outbound one (our number is the caller)
 * both land on the same row without this having to know how the SIP direction
 * was labelled.
 */
export function bucketTraffic(
  calls: CallSummaryResponse[],
  numbers: PhoneNumberResponse[],
  windowDays: number,
  capped: boolean,
): TrafficReport {
  const idByDigits = new Map<string, string>();
  for (const number of numbers) idByDigits.set(digits(number.e164), number.id);

  const byNumber = new Map<string, NumberTraffic>();
  const totals = empty(windowDays);

  // Bucket against local midnight so "today" is the reader's today, and the
  // rightmost bar is the day they are looking at.
  const midnight = new Date();
  midnight.setHours(0, 0, 0, 0);
  const todayIndex = windowDays - 1;

  for (const call of calls) {
    const id = idByDigits.get(digits(call.to_e164)) ?? idByDigits.get(digits(call.from_e164));
    if (!id) continue;

    const started = new Date(call.started_at).getTime();
    if (!started) continue;
    const daysAgo = started >= midnight.getTime()
      ? 0
      : Math.ceil((midnight.getTime() - started) / DAY_MS);
    const index = todayIndex - daysAgo;
    const bucket = byNumber.get(id) ?? empty(windowDays);

    bucket.calls += 1;
    bucket.spend += call.cost?.total_charge ?? 0;
    bucket.talkSeconds += call.duration_s ?? 0;
    totals.calls += 1;
    totals.spend += call.cost?.total_charge ?? 0;
    totals.talkSeconds += call.duration_s ?? 0;
    if (index >= 0 && index < windowDays) {
      bucket.days[index] += 1;
      totals.days[index] += 1;
    }
    byNumber.set(id, bucket);
  }

  if (capped) {
    for (const bucket of byNumber.values()) bucket.days = [];
    totals.days = [];
    return { byNumber, totals, peakDay: 0, capped };
  }

  let peakDay = 0;
  for (const bucket of byNumber.values()) {
    for (const count of bucket.days) if (count > peakDay) peakDay = count;
  }

  return { byNumber, totals, peakDay, capped };
}
