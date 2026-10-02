"use client";
import type {
  CostDetailResponse,
  PriceBreakdown,
  PriceLine,
} from "@talqing/sdk";
import { cn } from "@/lib/cn";
import { Disclosure } from "../components/ui";
import { fmtCost, fmtDuration } from "../components/SnapshotBand";
import { ProportionBar } from "../components/ProportionBar";

/* ── money ──────────────────────────────────────────────────────────────────
 *
 * Six decimals on every ledger figure, so the decimal points align down the
 * column and because a real line can cost $0.000762 — the post-call analysis
 * model on a short call — which anything shorter rounds away to nothing. */
const fmtMoney = (v: number) => `$${v.toFixed(6)}`;

/* Rates span five orders of magnitude, from $2.50 per million tokens down to
   $0.0000039 per character, so a fixed number of decimals either rounds the
   small ones to zero or pads the large ones with noise. Cents get two decimals;
   everything below gets three significant figures. */
function fmtRate(v: number): string {
  if (v === 0) return "$0";
  if (v >= 0.01) return `$${v.toFixed(2)}`;
  return `$${v.toPrecision(3).replace(/0+$/, "")}`;
}

const fmtQty = (v: number) => Math.round(v).toLocaleString();

/* ── one priced line ────────────────────────────────────────────────────── */

const KIND_LABEL: Record<PriceLine["kind"], string> = {
  llm: "LLM",
  stt: "STT",
  tts: "TTS",
  realtime: "Realtime",
  avatar: "Avatar",
};

/* The pipeline hues from tailwind.config.ts, plus bronze for post-call
   analysis — which is not a stage of the call and should not read as one. The
   whole set is validated all-pairs for colour blindness; see the config. */
function lineSwatch(line: PriceLine): string {
  if (line.kind === "llm" && line.purpose === "analysis") return "bg-chart-analysis";
  return {
    llm: "bg-chart-llm",
    stt: "bg-chart-stt",
    tts: "bg-chart-tts",
    realtime: "bg-chart-realtime",
    avatar: "bg-chart-avatar",
  }[line.kind];
}

/**
 * "What you were charged for, and at what rate" — one phrase per billed
 * quantity.
 *
 * Only quantities with a rate appear. A TTS model billed by the character also
 * reports its audio seconds, and printing those beside the charged characters
 * would read as a second thing being paid for.
 *
 * A provider-reported line is the exception: it has no rates because nothing
 * was multiplied — the gateway priced the requests itself — so its quantities
 * print bare. Dropping them instead would leave a cost with nothing behind it,
 * which is the one thing this panel exists not to do.
 */
function billedFor(line: PriceLine): string[] {
  const phrase = (
    qty: number | null | undefined,
    unit: string,
    rate: number | null | undefined,
    per: string,
  ) => (qty && rate != null ? `${fmtQty(qty)} ${unit} @ ${fmtRate(rate)}${per}` : null);
  const seconds = (
    qty: number | null | undefined,
    unit: string,
    rate: number | null | undefined,
    per: string,
  ) => (qty && rate != null ? `${qty.toFixed(1)}s ${unit} @ ${fmtRate(rate)}${per}` : null);
  const counted = (qty: number | null | undefined, unit: string) =>
    qty ? `${fmtQty(qty)} ${unit}` : null;

  /* `line.rates` is read inline in every branch rather than bound once above:
     the property is narrowed by `line.kind`, and a binding taken before the
     switch widens straight back to the union of all five rate shapes. */
  switch (line.kind) {
    case "llm":
      /* Cache WRITES are their own line, not part of "input": they are charged
         at a premium by the providers that charge for them at all, and
         `uncached_input_tokens` already excludes them — so leaving them out would
         both understate the input count and hide the dearest tokens on the
         call. */
      return line.provider_reported
        ? [
            counted(line.uncached_input_tokens, "input"),
            counted(line.input_cached_tokens, "cached"),
            counted(line.input_cache_write_tokens, "cache write"),
            counted(line.output_tokens, "output"),
          ].filter((v) => v !== null)
        : [
            phrase(line.uncached_input_tokens, "input", line.rates?.input_per_1m, "/1M"),
            phrase(line.input_cached_tokens, "cached", line.rates?.cached_input_per_1m, "/1M"),
            phrase(
              line.input_cache_write_tokens,
              "cache write",
              line.rates?.cache_write_per_1m,
              "/1M",
            ),
            phrase(line.output_tokens, "output", line.rates?.output_per_1m, "/1M"),
          ].filter((v) => v !== null);
    case "stt":
      if (line.provider_reported) {
        return [
          line.audio_duration ? `${line.audio_duration.toFixed(1)}s audio` : null,
          counted(line.input_tokens, "input"),
          counted(line.output_tokens, "output"),
        ].filter((v) => v !== null);
      }
      return [
        seconds(line.audio_duration, "audio", line.rates?.per_audio_second, "/s"),
        phrase(line.input_tokens, "input", line.rates?.input_per_1m, "/1M"),
        phrase(line.output_tokens, "output", line.rates?.output_per_1m, "/1M"),
      ].filter((v) => v !== null);
    case "tts":
      return [
        phrase(line.characters_count, "chars", line.rates?.per_character, "/char"),
        seconds(line.audio_duration, "audio", line.rates?.per_audio_second, "/s"),
        phrase(line.input_tokens, "input", line.rates?.input_per_1m, "/1M"),
        phrase(line.output_tokens, "output", line.rates?.output_per_1m, "/1M"),
      ].filter((v) => v !== null);
    case "realtime":
      return [
        phrase(line.uncached_input_text_tokens, "text in", line.rates?.text_input_per_1m, "/1M"),
        phrase(
          line.input_cached_text_tokens,
          "cached text",
          line.rates?.cached_text_input_per_1m,
          "/1M",
        ),
        phrase(line.uncached_input_audio_tokens, "audio in", line.rates?.audio_input_per_1m, "/1M"),
        phrase(
          line.input_cached_audio_tokens,
          "cached audio",
          line.rates?.cached_audio_input_per_1m,
          "/1M",
        ),
        phrase(line.output_text_tokens, "text out", line.rates?.text_output_per_1m, "/1M"),
        phrase(line.output_audio_tokens, "audio out", line.rates?.audio_output_per_1m, "/1M"),
        seconds(line.session_seconds, "connected", line.rates?.per_session_minute, "/min"),
      ].filter((v) => v !== null);
    case "avatar":
      return [seconds(line.seconds, "alive", line.rates?.per_minute, "/min")].filter(
        (v) => v !== null,
      );
  }
}

/* A qualifier on the rates beside it, not a status: `priority` says why an LLM
   line is priced at twice the standard rate, and `analysis` says this spend
   happened after everyone hung up. Both are answers to "why is this line here",
   which is why they sit on the line rather than in a legend. */
function RateTag({ children, title }: { children: string; title: string }) {
  return (
    <span
      title={title}
      className="rounded border border-line-2 bg-white px-1.5 py-px font-mono text-[10.5px] uppercase tracking-[0.06em] text-muted"
    >
      {children}
    </span>
  );
}

function LineRow({ line, share }: { line: PriceLine; share: number }) {
  const billed = billedFor(line);
  return (
    <div className="grid grid-cols-[minmax(0,1fr)_auto] items-baseline gap-x-4 px-3 py-2.5">
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-x-1.5 gap-y-1">
          <span className={cn("h-2 w-2 flex-none rounded-[2px]", lineSwatch(line))} aria-hidden />
          <span className="font-mono text-[11px] font-medium uppercase tracking-[0.06em] text-muted">
            {KIND_LABEL[line.kind]}
          </span>
          <span className="text-[13px] text-muted">{line.provider}</span>
          <strong className="truncate font-mono text-[13px] font-medium text-ink">
            {line.model}
          </strong>
          {line.kind === "llm" && line.priority && (
            <RateTag title="Ran in the provider's priority lane, which prices at a higher rate">
              priority
            </RateTag>
          )}
          {line.kind === "llm" && line.purpose === "analysis" && (
            <RateTag title="Post-call analysis — read the finished transcript after the call ended">
              analysis
            </RateTag>
          )}
          {(line.kind === "llm" || line.kind === "stt") && line.provider_reported && (
            // The one line on this panel that is not our arithmetic. A gateway
            // forwards each request to a different host at that host's price,
            // so there is no rate card to multiply by — it reports the charge
            // instead, and this is the figure the tenant can reconcile against
            // their own account with that provider.
            <RateTag title="Charged by the provider per request rather than priced from a rate card — reconcilable against your own account with them">
              reported
            </RateTag>
          )}
        </div>
        {billed.length > 0 && (
          <div className="mt-1 font-mono text-[11.5px] leading-5 tabular-nums text-faint">
            {billed.join(" · ")}
          </div>
        )}
      </div>
      <div className="text-right">
        <div className="font-mono text-[13px] tabular-nums text-ink">{fmtMoney(line.cost)}</div>
        <div className="text-[11px] tabular-nums text-faint">{Math.round(share * 100)}%</div>
      </div>
    </div>
  );
}

/* ── the panel ──────────────────────────────────────────────────────────── */

function TotalRow({
  label,
  note,
  amount,
  strong,
}: {
  label: string;
  note?: string;
  amount: number;
  strong?: boolean;
}) {
  return (
    <div className="grid grid-cols-[minmax(0,1fr)_auto] items-baseline gap-x-4 px-3 py-1.5">
      <dt className={cn("text-[13px]", strong ? "font-medium text-ink" : "text-muted")}>
        {label}
        {note && <span className="ml-2 font-mono text-[11.5px] tabular-nums text-faint">{note}</span>}
      </dt>
      <dd
        className={cn(
          "text-right font-mono tabular-nums",
          strong ? "text-[14px] font-semibold text-ink" : "text-[13px] text-ink-soft",
        )}
      >
        {fmtMoney(amount)}
      </dd>
    </div>
  );
}

function Ledger({ snapshot }: { snapshot: PriceBreakdown }) {
  /* Ranked across all five kinds rather than grouped by kind: the ranking IS
     the finding. "STT is 57% of your provider cost" is not visible in a list
     that always puts LLM first. */
  const lines: PriceLine[] = [
    ...(snapshot.llm ?? []),
    ...(snapshot.stt ?? []),
    ...(snapshot.tts ?? []),
    ...(snapshot.realtime ?? []),
    ...(snapshot.avatar ?? []),
  ].sort((a, b) => b.cost - a.cost);

  const providerCost = snapshot.provider_cost;
  /* A call that ended on an explicit failure keeps the provider passthrough and
     is not charged our margin (`services/billing/pricing.py`). The snapshot
     still carries the catalog rate, so printing the formula unconditionally put
     "$0.01/min × 2h 0m" against "$0.000000" and read as broken arithmetic on
     every failed call. All three inputs are in this one frozen object, so
     deriving the waiver here cannot drift from the biller. */
  const perMinute = snapshot.platform_fee_per_minute;
  const chargeable = perMinute !== null && perMinute > 0 && (snapshot.duration_s ?? 0) > 0;
  const waived = chargeable && snapshot.platform_fee === 0;
  /* Explaining the platform fee beats stating it: it is roughly half of what a
     short call is charged, and a tenant reading one number cannot tell whether
     it scales with the call or with the usage. A chat is charged per answered
     message instead, and its snapshot carries that rate in place of the
     per-minute one. */
  const feeFormula = waived
    ? "waived — the call failed"
    : chargeable
      ? `${fmtRate(perMinute)}/min × ${fmtDuration(snapshot.duration_s)}`
      : snapshot.platform_fee_per_message !== null
        ? `${fmtRate(snapshot.platform_fee_per_message)}/message × ${snapshot.answered_messages}`
        : undefined;
  /* Priced, but with nothing to price. The biller writes totals and a breakdown
     in one statement, so an empty ledger on a `computed` call means the usage
     rows never landed — a worker that died before finalize. Saying "$0" without
     saying that claims a two-hour call was free. */
  const nothingMetered =
    lines.length === 0 && providerCost === 0 && (snapshot.duration_s ?? 0) > 0;

  return (
    <div className="grid gap-3">
      {nothingMetered && (
        <p className="rounded-lg border border-warn/25 bg-warn/[0.06] px-3 py-2 text-[12.5px] leading-5 text-warn">
          No provider usage was recorded for this call, so this is not what it cost us — it is what
          we were able to measure. The run ended without sealing its meters.
        </p>
      )}
      {/* The bar is the shape; the ledger below is its legend — same order,
          same swatches, one row per segment. */}
      <ProportionBar
        label="Where the provider cost went"
        ariaLabel="Share of provider cost by model"
        legend={false}
        segments={lines.map((line, i) => ({
          key: `${line.kind}-${line.provider}-${line.model}-${i}`,
          label: line.model,
          value: line.cost,
          swatch: lineSwatch(line),
          display: fmtMoney(line.cost),
        }))}
      />
      <div className="rounded-lg border border-line-2">
        {lines.length > 0 && (
          <div className="divide-y divide-line">
            {lines.map((line, i) => (
              <LineRow
                key={`${line.kind}-${line.provider}-${line.model}-${i}`}
                line={line}
                share={providerCost > 0 ? line.cost / providerCost : 0}
              />
            ))}
          </div>
        )}
        <dl className={cn("grid py-1.5", lines.length > 0 && "border-t border-line bg-subtle")}>
          <TotalRow label="Provider cost" amount={providerCost} />
          <TotalRow label="Platform fee" note={feeFormula} amount={snapshot.platform_fee} />
          <TotalRow label="Total" amount={snapshot.total_charge} strong />
        </dl>
      </div>
    </div>
  );
}

/**
 * What this call cost, and why.
 *
 * The breakdown is not computed here — it is the exact `PriceBreakdown` the
 * biller froze at seal, the same object we already post to a tenant's
 * `session.completed` webhook. Until now a tenant with a webhook receiver got a
 * richer account of their own spend than their own dashboard showed them.
 *
 * This replaces the pair of disclosures — "Cost breakdown" (three numbers) and
 * "Provider usage" (the same quantities with no money) — that a reader had to
 * open BOTH of to answer "why did this call cost six cents", and still could
 * not. One section, open by default, and no raw-usage appendix under it: every
 * quantity a priced line bills on is already ON that line, next to the rate it
 * was charged at. `UsageDisclosure` in CallDetail covers the only case this
 * does not — a call with no priced breakdown to read them off.
 *
 * Every call that priced cleanly has a snapshot — the biller writes the totals
 * and the breakdown in the same statement — so the second branch below should
 * not be reachable. It says what is missing rather than quietly showing three
 * numbers, because a breakdown that silently shrank to a total is the one thing
 * a reader would not notice.
 */
export function CostBreakdown({
  cost,
  defaultOpen,
}: {
  cost: CostDetailResponse;
  defaultOpen?: boolean;
}) {
  return (
    <Disclosure summary="Cost" meta={fmtCost(cost.total_charge)} defaultOpen={defaultOpen}>
      {cost.pricing_snapshot ? (
        <Ledger snapshot={cost.pricing_snapshot} />
      ) : (
        <div className="grid gap-2">
          <p className="text-[12px] leading-4 text-faint">
            No line-by-line pricing was recorded for this call, so only the totals are known.
          </p>
          <dl className="grid rounded-lg border border-line-2 py-1.5">
            <TotalRow label="Provider cost" amount={cost.provider_cost} />
            <TotalRow label="Platform fee" amount={cost.platform_fee} />
            <TotalRow label="Total" amount={cost.total_charge} strong />
          </dl>
        </div>
      )}
    </Disclosure>
  );
}

