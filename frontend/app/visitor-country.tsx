"use client";

import { useEffect, useState, type ReactNode } from "react";

/* The visitor's country, read off Cloudflare's own edge.
 *
 * This site is served from Cloudflare Pages, so `/cdn-cgi/trace` already
 * answers on its own origin with `loc=IN` — no proxying, no Pages Function, no
 * infrastructure at all. It decides two things: which region a brand-new
 * workspace is created in, and which currency the public pages quote the
 * platform fee in.
 *
 * Nothing may wait on it. A slow fetch, a failure, or a page served from
 * somewhere that is not behind Cloudflare — a `localhost:3000` dev server has no
 * `/cdn-cgi/trace` and simply 404s — all resolve to `null`, which means the
 * default region and dollar prices. There is no timeout to tune and no special
 * case for local.
 *
 * It is spoofable, and for both uses that is fine: every region serves every
 * organization, the home region grants no privilege, and a price quoted in the
 * wrong currency is still the right price. That would not be acceptable for
 * anything else on this API.
 *
 * One request per page load however many components ask, hence the module-level
 * promise — the privacy policy promises exactly one.
 */
let lookup: Promise<string | null> | undefined;

/** The ISO country code; `null` once the lookup found none, `undefined` while
 *  it is still running. */
export function useVisitorCountry(): string | null | undefined {
  const [country, setCountry] = useState<string | null>();
  useEffect(() => {
    let cancelled = false;
    lookup ??= fetch("/cdn-cgi/trace")
      .then((response) => (response.ok ? response.text() : ""))
      .then((body) => /^loc=([A-Z]{2})$/m.exec(body)?.[1] ?? null)
      .catch(() => null);
    lookup.then((code) => {
      if (!cancelled) setCountry(code);
    });
    return () => {
      cancelled = true;
    };
  }, []);
  return country;
}

/* A price in rupees for a visitor in India and in US dollars for everyone else.
 * The platform bills in USD everywhere, so a ₹ figure is a courtesy to the one
 * market that thinks in rupees, not a second price list. Dollars are also what
 * renders until the country arrives, and on localhost it never does. */
export function LocalPrice({ inr, usd }: { inr: ReactNode; usd: ReactNode }) {
  return <>{useVisitorCountry() === "IN" ? inr : usd}</>;
}
