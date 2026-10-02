"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";
import React, { useEffect, useState } from "react";
import { Nav } from "./Nav";
import { api } from "@/lib/api";
import { cn } from "@/lib/cn";
import { useActiveRegion, useRegions } from "@/lib/regions";

/* Authed-page shell: a fixed sidebar (Nav) on md+ and a sticky top bar on
   mobile, with the content padded to clear the sidebar. Replaces the old
   body { padding-left } + :has(.auth/.lp) hack. */
export function AppShell({ children }: { children: React.ReactNode }) {
  useRegionInTitle();
  return (
    <>
      <Nav />
      <main className="min-h-screen bg-surface md:pl-[var(--side-w)]">
        <CreditBanner />
        {children}
      </main>
    </>
  );
}

/* The region in the browser tab.
 *
 * Region is a mode, and the tab title is the one piece of chrome that survives a
 * screenshot, a bookmark and a second tab — which is exactly the situation where
 * two windows on two regions look identical and someone creates an agent in the
 * wrong one. Set from an effect rather than from `metadata`, because the region
 * is a runtime value and every dashboard page is a client component.
 *
 * Only when there is more than one region: on a single-region deployment it
 * would be noise on every tab, saying nothing. */
function useRegionInTitle() {
  const all = useRegions();
  const region = useActiveRegion();
  useEffect(() => {
    // `document.title` is not markup React owns, so this never mismatches —
    // but it still waits for the hooks, because before they settle the answer
    // is the build default rather than this browser's region.
    if (all.length < 2) return;
    const base = document.title.split(" · ")[0];
    document.title = `${base} · ${region.name}`;
  }, [all, region]);
}

/* A workspace out of credit stops answering calls, and the page it is on is
   rarely the billing page — so the whole app says so rather than the one screen
   nobody was looking at.

   Read once per page load, and deliberately NOT taken off /me: that response is
   the signed-in identity, is served by a different host, and a number that moves
   with every call does not belong on it. A failed read shows nothing at all: a
   banner that appears because a request wobbled would be worse than no banner.

   It NAMES ITS REGION, and that is not decoration. Credits are held per region,
   so a customer with $50 in India and $0 in the US has calls refused in the US —
   and "Out of credits" over a workspace they know they topped up is a support
   ticket with a frightened person on the end of it. Saying which region is out
   costs one clause and removes the whole class. */
function CreditBanner() {
  const region = useActiveRegion();
  const pathname = usePathname();
  const [state, setState] = useState<{ balance: number; threshold: number } | null>(null);

  useEffect(() => {
    let cancelled = false;
    api
      .credits()
      .then((credits) => {
        if (!cancelled) {
          setState({ balance: credits.balance, threshold: credits.low_balance_threshold });
        }
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, []);

  // Not on Settings: the balance, the packs and this same sentence are already
  // on the page, and saying it twice reads as a system that is not listening.
  if (!state || pathname.startsWith("/settings")) return null;
  const empty = state.balance <= 0;
  if (!empty && state.balance >= state.threshold) return null;

  return (
    <div
      className={cn(
        "flex flex-wrap items-center gap-x-2 gap-y-1 border-b px-5 py-2.5 text-[13px] leading-5 sm:px-6",
        empty ? "border-danger/25 bg-danger/[0.06] text-danger" : "border-warn/25 bg-warn/[0.07] text-warn",
      )}
    >
      <strong className="font-semibold">
        {empty
          ? `Out of credits in ${region.name}.`
          : `Low on credits in ${region.name} — $${state.balance.toFixed(2)} left.`}
      </strong>
      <span>
        {empty
          ? "New calls will not start there. Calls in progress are unaffected."
          : "Top up before your next batch so nothing stops mid-campaign."}
      </span>
      <Link
        href="/settings?tab=billing"
        className="font-semibold underline decoration-current/40 underline-offset-2 hover:decoration-current"
      >
        Add credits
      </Link>
    </div>
  );
}
