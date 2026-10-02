/* Calendar dates, as `YYYY-MM-DD` strings.

   A date in this app is a day on a wall calendar, not an instant: the range a
   user picks on /calls means "these days, where they are", and the API takes
   the day string. So the string is the representation everywhere, `Date` is
   only ever a scratch value inside these functions, and `toISOString()` never
   appears — it converts to UTC, which hands back yesterday for anyone east of
   Greenwich after midnight and tomorrow for anyone west of it before. */

const MONTHS = [
  "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December",
];

/** `YYYY-MM-DD` for a local date. */
export function toISODate(d: Date): string {
  const month = `${d.getMonth() + 1}`.padStart(2, "0");
  const day = `${d.getDate()}`.padStart(2, "0");
  return `${d.getFullYear()}-${month}-${day}`;
}

/** Parse `YYYY-MM-DD` as a local day, or null if it is not one. Rejects
 *  impossible days (2026-02-31) that the Date constructor would roll forward. */
export function fromISODate(value: string | null | undefined): Date | null {
  if (!value) return null;
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value.trim());
  if (!match) return null;
  const [, y, m, d] = match;
  const date = new Date(Number(y), Number(m) - 1, Number(d));
  return date.getMonth() === Number(m) - 1 && date.getDate() === Number(d) ? date : null;
}

/** Today, as a date string. */
export function today(): string {
  return toISODate(new Date());
}

/** `days` before today, counting today as day one — so `startForDays(7)` with
 *  today gives a seven-day window, not eight. */
export function startForDays(days: number): string {
  const d = new Date();
  d.setDate(d.getDate() - (days - 1));
  return toISODate(d);
}

/** Shift a date string by whole days. */
export function addDays(value: string, days: number): string {
  const date = fromISODate(value);
  if (!date) return value;
  date.setDate(date.getDate() + days);
  return toISODate(date);
}

/** The earlier of two date strings. Safe as a plain compare: `YYYY-MM-DD` sorts
 *  lexicographically in calendar order, which is the whole point of the format. */
export function earlierDate(first: string, second: string): string {
  return first < second ? first : second;
}

/** How a date reads in the UI: "31 Jan 2026". Unambiguous between en-GB and
 *  en-US readers, which 01/02/2026 is not. */
export function formatDate(value: string | null | undefined): string {
  const date = fromISODate(value);
  if (!date) return "";
  return `${date.getDate()} ${MONTHS[date.getMonth()].slice(0, 3)} ${date.getFullYear()}`;
}

/* ── Timezones ───────────────────────────────────────────────────────────────
   A zone here is a setting a user picks, not the reader's own clock: a batch
   dials on its own zone and an agent's `{{system_vars.time}}` resolves on the
   agent's. Both pickers are the same list, so it lives here rather than beside
   either of them. */

// Built once per tab: `Intl.supportedValuesOf` walks ~600 zones and the answer
// cannot change while the page is open. Callers pass a zone per render, so the
// walk has to stay out of the render path.
let zoneCache: string[] | null = null;

/** Every IANA zone the browser knows, with the operator's own first.
 *
 *  `ensure` is a zone that must be in the list whether the browser lists it or
 *  not — **a value the server gave us has to be selectable.** Browsers disagree
 *  with the API about aliases: Chrome reports `Asia/Calcutta` where everything
 *  server-side writes the canonical `Asia/Kolkata`. A picker with no option
 *  matching its own value renders as "Select…", so a batch created through the
 *  API looks unconfigured, and saving that form silently moves it to whatever
 *  the operator picks instead.
 */
export function timezones(ensure?: string | null): string[] {
  if (zoneCache === null) {
    const supported =
      typeof Intl.supportedValuesOf === "function" ? Intl.supportedValuesOf("timeZone") : [];
    const here = localTimezone();
    zoneCache = supported.length ? [here, ...supported.filter((z) => z !== here)] : [here];
  }
  return ensure && !zoneCache.includes(ensure) ? [ensure, ...zoneCache] : zoneCache;
}

/** The reader's own zone — the only sensible pre-fill for a new batch or agent. */
export function localTimezone(): string {
  return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
}

export { MONTHS };
