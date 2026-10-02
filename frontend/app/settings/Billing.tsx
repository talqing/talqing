"use client";
import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import type {
  CreditBalanceResponse,
  CreditCheckoutRequest,
  CreditLedgerEntryResponse,
} from "@talqing/sdk";

/* The pack ids the API will accept. Taken from the request type rather than
   widened to `string`, so a pack the backend does not sell cannot reach the one
   call on this page that spends money. */
type PackId = CreditCheckoutRequest["pack"];
import { EmptyState, ListSkeleton, Panel } from "@/app/components/ui";
import { api } from "@/lib/api";
import { apiErrorMessage } from "@/lib/apiError";
import { cn } from "@/lib/cn";
import { useActiveRegion, useRegions } from "@/lib/regions";

/* Credits, in one panel each: what is left, what a top-up costs, and every
   movement that got the balance here.

   Prices are USD everywhere on this page, deliberately. The landing page quotes
   ₹ at a fixed rate for an Indian reader who has not signed up yet; post-login
   the platform bills in dollars and Dodo shows the buyer their own currency at
   the real rate at checkout, so a ₹ figure of ours here would be a number that
   does not match the one they are about to pay. */

const CoinIcon = (
  <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
    <ellipse cx="12" cy="6.5" rx="7.5" ry="3.5" /><path d="M4.5 6.5v11c0 1.9 3.4 3.5 7.5 3.5s7.5-1.6 7.5-3.5v-11M4.5 12c0 1.9 3.4 3.5 7.5 3.5s7.5-1.6 7.5-3.5" />
  </svg>
);

/* Two decimals, unless the figure is smaller than a cent — which one voice
   minute is, at $0.0035. Four decimals there rather than three, because three
   would print a one-minute call as "$0.004" and a column of those would not add
   up against the balance beside it. The whole-dollar top-up above still reads
   "$10.00", because it takes the other branch. */
function money(amount: number): string {
  const magnitude = Math.abs(amount);
  const digits = magnitude > 0 && magnitude < 0.01 ? 4 : 2;
  return `${amount < 0 ? "−" : ""}$${magnitude.toFixed(digits)}`;
}

function thousands(n: number): string {
  return n.toLocaleString("en-US");
}

/* One line per kind, written about the money rather than about our code. The
   ledger is the page a customer opens when they think they were overcharged, so
   every row has to explain itself without a legend. */
function describeEntry(entry: CreditLedgerEntryResponse): string {
  switch (entry.kind) {
    case "signup_grant":
      return "Welcome credit";
    case "purchase":
      return "Credits added";
    case "usage":
      return "Call";
    case "refund":
      return entry.note || "Refunded";
    case "adjustment":
      return entry.note || "Adjustment";
  }
}

/* What happened to the purchase this visit came back from.
   `waiting` while the poll runs; the rest are terminal. */
type Arrival = "waiting" | "landed" | "processing" | "landing" | "declined" | "unconfirmed";

/* The processor's own status for the top-up, turned into what to tell the buyer.
   Each answer sends them somewhere different — wait, try again, or write to us —
   so a status this does not know maps to the one that asks a human rather than
   to a reassurance we cannot stand behind. `null` is that case too: it means the
   control plane could not be asked. */
function arrivalFor(status: CreditBalanceResponse["topup_status"]): Arrival {
  switch (status) {
    case "pending":
      return "processing";
    case "paid":
      return "landing";
    case "failed":
    case "cancelled":
      return "declined";
    default:
      return "unconfirmed";
  }
}

const ARRIVAL_MESSAGE: Record<Exclude<Arrival, "waiting" | "landed">, string> = {
  processing:
    "We are still waiting on the payment processor. If you completed the payment, it usually clears within a minute — refresh then, and write to us if it does not.",
  landing:
    "Paid — your credits are landing now. Refresh in a moment; if they are still not here, write to us and we will sort it out.",
  declined:
    "That payment did not go through, and you have not been charged. Try again below, or write to us if your bank says otherwise.",
  unconfirmed:
    "We could not confirm this payment. If you completed it, nothing is lost — write to us and we will find it.",
};

export function Billing({ isAdmin, topupId }: { isAdmin: boolean; topupId: string | null }) {
  const region = useActiveRegion();
  const allRegions = useRegions();
  const [credits, setCredits] = useState<CreditBalanceResponse | null>(null);
  const [ledger, setLedger] = useState<CreditLedgerEntryResponse[] | null>(null);
  const [err, setErr] = useState("");
  const [buying, setBuying] = useState<PackId | null>(null);
  /* Set only when we came back from a checkout: null means this was an ordinary
     visit and there is nothing to wait for.

     The four terminal states used to be one vague sentence. The balance read
     reports the payment processor's OWN status for this top-up, which is a
     different question from whether the credit has reached this region — so
     "the processor has told us nothing", "the processor paid and the credit is
     still landing" and "the payment did not complete" stop being the same
     message. They send a worried customer to three different places, and only
     one of them is a reason to write to us. */
  const [arrival, setArrival] = useState<Arrival | null>(topupId ? "waiting" : null);

  const load = useCallback(async () => {
    try {
      /* `topupId` goes to the balance read, which is also where credit the
         control plane could not deliver is reconciled. That is why this page is
         the backstop and there is no timer anywhere: the moment someone cares is
         the moment this page loads. */
      const [balance, entries] = await Promise.all([
        api.credits(topupId ?? undefined),
        api.creditLedger(),
      ]);
      setCredits(balance);
      setLedger(entries.items);
      return { entries: entries.items, status: balance.topup_status };
    } catch (error) {
      setErr(apiErrorMessage(error));
      return null;
    }
  }, [topupId]);

  useEffect(() => {
    load();
  }, [load]);

  /* The webhook usually lands before the browser finishes the redirect, but not
     always — so wait for the ledger row this purchase produces rather than
     showing a stale balance as if nothing had happened. The row is the exact
     signal: a payment that failed never writes one, which is why this gives up
     with a sentence rather than spinning for ever.

     The cleanup below is the ONLY thing that stops a run, and deliberately: a
     `useRef` guard beside it meant StrictMode's double-mount started a poll,
     cancelled it, and then skipped the restart — so in development the banner
     said "Adding your credits…" for ever and none of the four answers below was
     ever reachable. `load` only changes when `topupId` does, so this starts one
     poll per purchase either way. */
  useEffect(() => {
    if (!topupId) return;
    let cancelled = false;
    let lastStatus: CreditBalanceResponse["topup_status"] = null;
    (async () => {
      for (let attempt = 0; attempt < 6; attempt++) {
        const result = await load();
        if (cancelled) return;
        lastStatus = result?.status ?? null;
        if (result?.entries.some((e) => e.kind === "purchase" && e.topup_id === topupId)) {
          setArrival("landed");
          return;
        }
        await new Promise((resolve) => setTimeout(resolve, 2000));
      }
      /* Six attempts is unchanged, and so is what they are waiting on: the
         payment processor telling us the money arrived, which is a hop outside
         anything we run. What improved is the answer at the end. */
      if (!cancelled) setArrival(arrivalFor(lastStatus));
    })();
    return () => {
      cancelled = true;
    };
  }, [topupId, load]);

  async function buy(pack: PackId) {
    setErr("");
    setBuying(pack);
    try {
      const { checkout_url } = await api.buyCredits(pack);
      // Dodo's hosted page, not an overlay: an iframe on a payment page is a
      // support surface we have no reason to own.
      window.location.href = checkout_url;
    } catch (error) {
      setErr(apiErrorMessage(error));
      setBuying(null);
    }
  }

  if (!credits || !ledger) {
    return err ? <ErrorNote message={err} /> : <ListSkeleton rows={3} />;
  }

  const empty = credits.balance <= 0;
  const low = !empty && credits.balance < credits.low_balance_threshold;

  return (
    <div className="flex flex-col gap-5">
      {err && <ErrorNote message={err} />}

      {arrival && (
        <div
          className={cn(
            "rounded-lg border px-3.5 py-2.5 text-[13.5px]",
            arrival === "landed"
              ? "border-live/25 bg-live/[0.06] text-live"
              : arrival === "declined"
                ? "border-danger/25 bg-danger/[0.06] text-danger"
                : "border-line-2 bg-subtle text-ink-soft",
          )}
        >
          {arrival === "landed"
            ? `Payment received — your credits are in the ${region.name} balance below.`
            : arrival === "waiting"
              ? "Payment received. Adding your credits…"
              : ARRIVAL_MESSAGE[arrival]}
        </div>
      )}

      {/* Balance and packs are one panel, not two: "what is left" and "how to add
          more" are one question, and splitting them put a paragraph of air
          between the number and the thing you press about it. */}
      <Panel className="p-0">
        <div className="border-b border-line px-5 py-4">
          <div className="text-[16px] font-semibold leading-6 text-ink">
            Credits — {region.name}
          </div>
          <div className="mt-1 text-[13px] leading-5 text-muted">
            Talqing charges a per-minute platform fee on voice and video calls. What you pay your
            own AI providers is between you and them, and never comes out of this.
          </div>
          {/* One honest sentence beats a support ticket. Credits are held per
              region because the charge for a call is settled in the same
              transaction that prices it, in the region the call ran in — so a
              balance in one region is not spendable in another, and a customer
              who tops up in the wrong one needs to find that out here rather
              than from a refused call.

              Only with a second region to hold the other balance — the same
              condition the sidebar switcher uses, so this never tells someone to
              press a control that is not on their screen. */}
          {allRegions.length > 1 && (
            <div className="mt-1.5 text-[13px] leading-5 text-muted">
              Credits are held per region, so this balance pays for calls in{" "}
              {region.name} only. Switch region in the sidebar to see or top up another.
            </div>
          )}
        </div>

        <div className="flex flex-wrap items-end justify-between gap-x-8 gap-y-4 px-5 py-5">
          <div className="min-w-0">
            <div className="font-display text-[38px] font-semibold leading-none tracking-tight text-ink tabular-nums">
              {money(credits.balance)}
            </div>
            <p className="mt-2.5 text-[13.5px] leading-5 text-muted">
              About{" "}
              <strong className="font-semibold text-ink-soft tabular-nums">
                {thousands(credits.voice_minutes_remaining)}
              </strong>{" "}
              voice minutes, or{" "}
              <strong className="font-semibold text-ink-soft tabular-nums">
                {thousands(credits.video_minutes_remaining)}
              </strong>{" "}
              video minutes, at today&apos;s rates.
            </p>
          </div>
          {(empty || low) && (
            <div
              className={cn(
                "max-w-[400px] rounded-lg border px-3.5 py-2.5 text-[13px] leading-5",
                empty
                  ? "border-danger/25 bg-danger/[0.05] text-danger"
                  : "border-warn/25 bg-warn/[0.06] text-warn",
              )}
            >
              {empty
                ? "New calls will not start until you add credits. Calls already in progress are unaffected."
                : "Running low. Top up before your next batch so nothing stops mid-campaign."}
            </div>
          )}
        </div>

        <div className="border-t border-line px-5 py-4">
          <div className="mb-3 flex flex-wrap items-baseline gap-x-3 gap-y-1">
            <span className="text-[13.5px] font-semibold leading-5 text-ink">Add credits</span>
            <span className="text-[12.5px] leading-5 text-faint">
              {isAdmin
                ? "Paid once, in US dollars. Tax and your own currency are worked out at checkout, and the credit is always the full amount shown."
                : "Only an admin can buy credits for this organization."}
            </span>
          </div>
          {credits.packs.length === 0 ? (
            <p className="text-[13px] leading-5 text-muted">
              Credit packs are not on sale in this environment yet.
            </p>
          ) : (
            <div className="grid grid-cols-2 gap-2.5 sm:grid-cols-3 lg:grid-cols-6">
              {credits.packs.map((pack) => (
                <button
                  key={pack.id}
                  type="button"
                  disabled={!isAdmin || buying !== null}
                  onClick={() => buy(pack.id)}
                  className={cn(
                    "flex flex-col items-start rounded-[10px] border border-line-2 bg-white px-3 py-2.5 text-left",
                    "transition-[border-color,box-shadow,transform] duration-150 ease-out",
                    "hover:-translate-y-px hover:border-ink hover:shadow-soft",
                    "focus:outline-none focus-visible:ring-2 focus-visible:ring-ink/10",
                    "disabled:pointer-events-none disabled:opacity-45 motion-reduce:hover:transform-none",
                  )}
                >
                  <span className="font-display text-[19px] font-semibold leading-6 tracking-tight text-ink tabular-nums">
                    ${pack.amount}
                  </span>
                  <span className="mt-0.5 truncate text-[11.5px] leading-4 text-faint tabular-nums">
                    {buying === pack.id
                      ? "Opening checkout…"
                      : `${thousands(pack.voice_minutes)} voice min`}
                  </span>
                </button>
              ))}
            </div>
          )}
        </div>
      </Panel>

      <Panel className="p-0">
        <div className="flex items-start gap-3 border-b border-line px-5 py-4">
          <div className="min-w-0">
            <div className="text-[16px] font-semibold leading-6 text-ink">History</div>
            <div className="mt-1 text-[13px] leading-5 text-muted">
              Every movement of the balance, newest first, with what it left behind.
            </div>
          </div>
        </div>
        {ledger.length === 0 ? (
          <div className="px-5 py-4">
            <EmptyState
              icon={CoinIcon}
              title="Nothing has moved yet."
              body="Buy credits or run a call and it will show up here."
            />
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[520px] border-collapse">
              <thead>
                <tr className="border-b border-line text-[11.5px] font-medium uppercase tracking-[0.06em] text-faint">
                  <th className="px-5 py-2 text-left font-medium">When</th>
                  <th className="px-5 py-2 text-left font-medium">What</th>
                  <th className="px-5 py-2 text-right font-medium">Amount</th>
                  <th className="px-5 py-2 text-right font-medium">Balance</th>
                </tr>
              </thead>
              <tbody>
                {ledger.map((entry) => (
                  <tr key={entry.id} className="border-b border-line transition-colors last:border-b-0 hover:bg-hover">
                    <td className="whitespace-nowrap px-5 py-2.5 text-[13px] text-muted">
                      {new Date(entry.created_at).toLocaleString(undefined, {
                        dateStyle: "medium",
                        timeStyle: "short",
                      })}
                    </td>
                    <td className="px-5 py-2.5 text-[13.5px] text-ink">
                      {entry.session_id ? (
                        <Link
                          href={`/calls?id=${entry.session_id}`}
                          className="underline decoration-line-strong underline-offset-2 hover:decoration-ink"
                        >
                          {describeEntry(entry)}
                        </Link>
                      ) : (
                        describeEntry(entry)
                      )}
                    </td>
                    <td
                      className={cn(
                        "whitespace-nowrap px-5 py-2.5 text-right text-[13.5px] font-semibold tabular-nums",
                        entry.amount >= 0 ? "text-live" : "text-ink",
                      )}
                    >
                      {entry.amount >= 0 ? `+${money(entry.amount)}` : money(entry.amount)}
                    </td>
                    <td className="whitespace-nowrap px-5 py-2.5 text-right text-[13px] text-muted tabular-nums">
                      {money(entry.balance_after)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>

      {!isAdmin && (
        <p className="text-[13px] leading-5 text-muted">
          Everyone in the organization can see the balance, because everyone can start a test call
          and a refused one needs an explanation. Only an admin can buy more.
        </p>
      )}
    </div>
  );
}

function ErrorNote({ message }: { message: string }) {
  return (
    <div className="rounded-md border border-danger/30 bg-danger/10 px-3.5 py-2.5 text-[13.5px] text-danger">
      {message}
    </div>
  );
}
