/* The palette, for the one place a Tailwind class cannot reach.
 *
 * An SVG `fill` or `stroke` takes a colour, not a class name, so a chart is the
 * single exception to the token-only rule the rest of the app follows. Reading
 * the values off `tailwind.config.ts` rather than re-typing them is what keeps
 * that exception from becoming a second palette: the raw `#8a8a93` that used to
 * sit at the top of `ObservabilityCharts.tsx` was already a shade the design
 * system no longer used.
 */
import config from "@/tailwind.config";

const palette = (config.theme?.extend?.colors ?? {}) as Record<
  string,
  string & Record<string, string>
>;

export const CHART_COLORS = {
  /** The dominant, meaning-free band: "this is the part that went fine". */
  ink: palette.ink,
  danger: palette.danger,
  info: palette.info,
  /** Recessive furniture. */
  grid: palette.line,
  axis: palette.faint,
  /** Neutrals, darkest to lightest, for a band that is deliberately not a
   *  colour: Other, Canceled, Skipped, Not recorded. */
  muted: palette.placeholder,
  faint: palette["line-strong"],
  faintest: palette["line-2"],
} as const;

/* The eight categorical slots, in the order the palette was validated in.
 *
 * Assigned by index and never cycled: a ninth series folds into `OTHER_COLOR`
 * (the backend rolls it up as one), so two bands on one chart can never share a
 * colour. Colour follows the entity — the backend ranks a dimension's series
 * once and the client paints slot N to series N, so a filter that removes a
 * series does repaint the rest, and that is the one place this rule bends: the
 * ranking IS the identity for an unbounded dimension like "which DID".
 */
export const SERIES_COLORS: readonly string[] = [
  palette.series[1],
  palette.series[2],
  palette.series[3],
  palette.series[4],
  palette.series[5],
  palette.series[6],
  palette.series[7],
  palette.series[8],
];

export const OTHER_COLOR = CHART_COLORS.muted;
export const OTHER_KEY = "__other__";

/* Fixed hues for the dimensions whose values MEAN something.
 *
 * These are not decoration and are not interchangeable with the categorical
 * slots: "failed" is red on every chart on the page, and the neutral ink band is
 * always the outcome nobody needs to act on. Every map below was validated in
 * its own stacking order against the white panel surface — the order is the
 * safety mechanism, so re-validate the whole map if you insert a value.
 */
export const STATUS_COLORS: Record<string, string> = {
  completed: CHART_COLORS.ink,
  failed: palette.series[8],
  canceled: CHART_COLORS.faint,
  running: palette.series[1],
  queued: palette.series[3],
};

export const TYPE_COLORS: Record<string, string> = {
  SIP_INBOUND: palette.series[1],
  SIP_OUTBOUND: palette.series[2],
  /* Violet, not the free green: green sits beside orange here and fails CVD
     (protan ΔE 3.2); violet passes against both neighbours. */
  WHATSAPP_INBOUND: palette.series[7],
  /* A streamed call stacks between the phone kinds and the web/text pair — that
     is the backend's `_TYPE_ORDER` — but takes the next FREE slot rather than
     slot 3, so adding it left the four colours that were already validated
     exactly where they were. Only the adjacency is new. */
  STREAM: palette.series[5],
  WEB: palette.series[3],
  TEXT: palette.series[4],
};

/* Endings nobody needs to act on are ink. The four with an owner outside this
   building take the validated slots in the order the backend stacks them;
   `platform` takes the same red that means "failed" everywhere else on the page,
   because it is the one band that is our fault; and `other` is grey, because "a
   reason this version has never been taught" is not a severity, it is a gap. */
export const CLOSE_REASON_COLORS: Record<string, string> = {
  normal: CHART_COLORS.ink,
  transferred: palette.series[1],
  caller_unreachable: palette.series[2],
  carrier_fault: palette.series[3],
  configuration: palette.series[4],
  platform: palette.series[8],
  other: CHART_COLORS.muted,
};

/* "Not recorded" is the lightest band on the chart, not a mid-grey: it is the
   remainder — text sessions and failed calls, which were never going to have
   audio — and a heavy grey cap on every bar reads as a second problem sitting on
   top of the recordings. */
export const RECORDING_COLORS: Record<string, string> = {
  stored: CHART_COLORS.ink,
  pending: palette.series[1],
  failed: palette.series[8],
  consent_withdrawn: palette.series[4],
  deleted: CHART_COLORS.muted,
  none: CHART_COLORS.faintest,
};

/* Skipped is grey rather than amber for the same reason: the gates declining to
   spend an LLM call on a nine-second wrong number is a choice working, and only
   `failed` is a thing to go and look at. */
export const ANALYSIS_COLORS: Record<string, string> = {
  completed: CHART_COLORS.ink,
  failed: palette.series[8],
  skipped: CHART_COLORS.faint,
  pending: palette.series[1],
  none: CHART_COLORS.faintest,
};

export const TRANSFER_COLORS: Record<string, string> = {
  connected: CHART_COLORS.ink,
  failed: palette.series[8],
};

/** Paint by slot order — for a dimension with no meaning of its own. */
export function categorical(key: string, index: number): string {
  if (key === OTHER_KEY) return OTHER_COLOR;
  return SERIES_COLORS[index % SERIES_COLORS.length];
}

/** Paint by value — for a dimension whose values mean something. */
export function semantic(map: Record<string, string>) {
  return (key: string): string => {
    if (key === OTHER_KEY) return OTHER_COLOR;
    return map[key] ?? OTHER_COLOR;
  };
}
