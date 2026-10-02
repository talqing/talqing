/* Which region this tab is looking at.
 *
 * One static build serves every region, so the API base URL is a RUNTIME value
 * rather than an inlined one. The list comes from the control plane
 * (`GET /v1/regions`) and is cached in `localStorage`; a build-time copy is the
 * fallback, so a control-plane blip cannot blank the app. Adding a region then
 * needs no dashboard deploy.
 *
 * **Region is a mode.** It silently changes what every list, every form and
 * every "create" button means, and creating a resource in the wrong region is
 * the single most common user error on consoles built this way. Two things
 * follow, and neither is polish: every empty state names its region (an unused
 * region is indistinguishable from a wiped one otherwise), and the preference is
 * keyed PER ORGANIZATION.
 *
 * The per-organization key is the subtle one. A member who works in `us` and
 * switches to an organization that has only ever used `in` would land, with a
 * global key, on an empty `us` — our own preference store manufacturing exactly
 * the "looks like data loss" failure the empty-state copy exists to prevent.
 */

import { useEffect, useState } from "react";

export interface Region {
  slug: string;
  /** The full name. `us` is not a label a person reads. */
  name: string;
  api_url: string;
  /** Exactly one region carries this. */
  default: boolean;
}

/* The fallback list, inlined at build time — the same shape `GET /v1/regions`
 * returns, so there is one shape and nothing to keep in sync. Required, and
 * checked here: a bundler inlines `NEXT_PUBLIC_*` at build time, so a missing
 * value cannot be caught at runtime, and a dashboard that quietly points at
 * nothing fails as a connection error three layers down. */
const BUILD_REGIONS: Region[] = parseBuildRegions(process.env.NEXT_PUBLIC_REGIONS);

function parseBuildRegions(raw: string | undefined): Region[] {
  if (!raw?.trim()) {
    throw new Error("NEXT_PUBLIC_REGIONS is not set (see frontend/README.md)");
  }
  const parsed: unknown = JSON.parse(raw);
  if (!Array.isArray(parsed) || parsed.length === 0) {
    throw new Error("NEXT_PUBLIC_REGIONS must be a non-empty JSON array of regions");
  }
  return parsed as Region[];
}

const LIST_KEY = "talqing.regions";
/* The organization the last load was for. Written when `me()` resolves, and
 * read at module init — which is the whole reason it exists: the base URL has to
 * be chosen synchronously, before any request, and the session cookie does not
 * tell the browser which organization it names. */
const ORG_KEY = "talqing.org";
const prefKey = (orgId: string) => `talqing.region.${orgId}`;

/* Every access is wrapped: a private window, cleared site data or a browser set
 * to block storage each make these throw rather than return null, and none of
 * that should be able to stop the dashboard rendering. */
function read(key: string): string | null {
  if (typeof window === "undefined") return null;
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

function write(key: string, value: string): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(key, value);
  } catch {
    /* A preference we cannot remember is a preference the next load re-derives
       from the organization's home region. Nothing here is load-bearing. */
  }
}

/** Every region, from the cache if there is one and from the build otherwise. */
export function regions(): Region[] {
  const cached = read(LIST_KEY);
  if (!cached) return BUILD_REGIONS;
  try {
    const parsed: unknown = JSON.parse(cached);
    if (Array.isArray(parsed) && parsed.length > 0) return parsed as Region[];
  } catch {
    /* fall through */
  }
  return BUILD_REGIONS;
}

/** Replace the cached list with what the control plane just served. */
export function cacheRegions(list: Region[]): void {
  if (list.length > 0) write(LIST_KEY, JSON.stringify(list));
}

export function regionBySlug(slug: string | null | undefined): Region | undefined {
  return slug ? regions().find((region) => region.slug === slug) : undefined;
}

function defaultRegion(): Region {
  const all = regions();
  return all.find((region) => region.default) ?? all[0];
}

/**
 * The region this tab is looking at, fixed for the life of the page.
 *
 * Resolved once, synchronously, at module init — because `lib/api.ts` builds its
 * client from it before any request goes out, and a `TalqingClient`'s `baseUrl`
 * is readonly by design. Changing region is therefore a full navigation, which
 * is what we want anyway: everything on screen belongs to the region being left.
 *
 * Order: the stored preference for the organization this browser was last in,
 * then the default. The organization's own `home_region` is the third fallback
 * and is applied by {@link adoptOrgRegion} once `me()` has resolved, because it
 * is not knowable before then.
 */
export const ACTIVE_REGION: Region = resolveActive();

/* What the build-time prerender resolves to, computed from BUILD_REGIONS alone
   so it is identical in every environment that runs this build. It is what the
   hooks above start from. */
const BUILD_ACTIVE: Region = BUILD_REGIONS.find((r) => r.default) ?? BUILD_REGIONS[0];

function resolveActive(): Region {
  const orgId = read(ORG_KEY);
  const preferred = orgId ? regionBySlug(read(prefKey(orgId))) : undefined;
  return preferred ?? defaultRegion();
}

/**
 * The active region, for anything that RENDERS it.
 *
 * Not `ACTIVE_REGION` directly, and the difference matters: this dashboard is a
 * static export, so every page is prerendered at build time against the
 * build-time region list — while the real answer comes from `localStorage` and
 * is only knowable in the browser. Interpolating `ACTIVE_REGION` straight into
 * markup is therefore a hydration mismatch whenever the two differ, and React
 * answers a mismatch by throwing away the server HTML and re-rendering the
 * entire root on the client.
 *
 * So: the prerender and the first client render both see the build default, and
 * the effect swaps in the real one. One extra render, only on pages that show a
 * region, and no mismatch.
 *
 * `ACTIVE_REGION` stays the value everything NON-rendering uses — the API
 * client's base URL above all, which has to be resolved before any request and
 * cannot wait for an effect.
 */
export function useActiveRegion(): Region {
  const [region, setRegion] = useState<Region>(BUILD_ACTIVE);
  useEffect(() => setRegion(ACTIVE_REGION), []);
  return region;
}

/**
 * The regions to OFFER, for anything that renders the list. Same reasoning as
 * {@link useActiveRegion}: the cached list and the build-time list can differ,
 * and a switcher that exists in one and not the other is a mismatch.
 */
export function useRegions(): Region[] {
  const [all, setAll] = useState<Region[]>(BUILD_REGIONS);
  useEffect(() => setAll(regions()), []);
  return all;
}

/**
 * Switch region. A full navigation, exactly like the organization switcher and
 * for the same reason: everything on screen belongs to the region being left.
 */
export function switchRegion(orgId: string, slug: string): void {
  write(prefKey(orgId), slug);
  write(ORG_KEY, orgId);
  window.location.assign("/agents");
}

/**
 * Reload into `slug` for `orgId`, remembering it first. Returns true when the
 * page is reloading, so a caller can stop rendering data that belongs elsewhere.
 *
 * **Writing the preference before reloading is what makes the reload
 * terminate.** `ACTIVE_REGION` is resolved at module init from that preference,
 * so a reload with nothing stored resolves to the default all over again and
 * asks for the same reload — for ever. And the write is READ BACK rather than
 * trusted: a private window or a browser set to block site data swallows it, and
 * a dashboard that reload-loops is far worse than one sitting in the default
 * region with every empty state naming which region that is.
 */
function reloadInto(orgId: string, slug: string): boolean {
  write(ORG_KEY, orgId);
  // Pinned even when no reload follows, so the answer is stable: an
  // organization keeps opening where it opened the first time, and the day the
  // configured default changes it does not move.
  write(prefKey(orgId), slug);
  if (slug === ACTIVE_REGION.slug) return false;
  if (read(prefKey(orgId)) !== slug) return false;
  window.location.reload();
  return true;
}

/**
 * Reconcile the region this page is pointed at with the organization it turns
 * out to be for. Returns true when the page is reloading.
 *
 * Called once `me()` resolves. It normally does nothing: the common case is
 * returning to the organization this browser was already in. It reloads when the
 * session names a different organization — a switch, another tab, a first visit
 * — whose region is not the one already in use, which is what stops a member who
 * works in `us` from landing on an empty `in`.
 */
export function adoptOrgRegion(orgId: string, homeRegion: string): boolean {
  const wanted =
    regionBySlug(read(prefKey(orgId))) ?? regionBySlug(homeRegion) ?? defaultRegion();
  return reloadInto(orgId, wanted.slug);
}

/**
 * Point this browser at one region for one organization, without navigating.
 *
 * For the one caller that has already been sent somewhere by a third party: the
 * payment processor's return URL carries `?region=`, and a buyer who finished
 * checkout in a different browser has no stored preference to honour. Returns
 * true when the page is reloading into that region.
 */
export function adoptRegionFromUrl(orgId: string, slug: string): boolean {
  if (!regionBySlug(slug)) return false;
  return reloadInto(orgId, slug);
}
