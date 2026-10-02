"use client";
import { useEffect, useRef, useState } from "react";
import { useRouter, usePathname } from "next/navigation";
import Link from "next/link";
import type { AuthContextResponse, OrgResponse } from "@/lib/control";
import { api, controlApi } from "@/lib/api";
import { apiErrorMessage } from "@/lib/apiError";
import { cn } from "@/lib/cn";
import {
  adoptOrgRegion,
  cacheRegions,
  switchRegion,
  useActiveRegion,
  useRegions,
} from "@/lib/regions";
import { FaqIcon } from "./FaqsSection";
import { Button, Field, Input, Modal } from "./ui";

/* An admin can add you to their organization without asking you or emailing
   you, so the switcher has to point at one you have not opened yet rather than
   leaving it to be noticed. That is what this remembers — and it seeds itself
   on first run, so a browser that has never stored it flags nothing rather
   than flagging everything. */
const SEEN_ORGS_KEY = "talqing.seenOrgs";

function readSeenOrgs(): Set<string> | null {
  const stored = window.localStorage.getItem(SEEN_ORGS_KEY);
  if (stored === null) return null;
  return new Set(JSON.parse(stored) as string[]);
}

function writeSeenOrgs(ids: Iterable<string>) {
  window.localStorage.setItem(SEEN_ORGS_KEY, JSON.stringify([...ids]));
}

/* tiny stroke icon set (currentColor). 20px on a 20px column so the icon, the
   organization avatar and the brand mark all sit on the same rail and every
   label in the sidebar starts at the same x. */
const iconCls = "h-5 w-5";
const I = {
  switcher: (
    <svg className="h-4 w-4" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <path d="M5 6.5 8 3.5l3 3M5 9.5 8 12.5l3-3" />
    </svg>
  ),
  // A globe: where this workspace's data lives.
  region: (
    <svg className={iconCls} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="12" cy="12" r="8.5" />
      <path d="M3.5 12h17M12 3.5a13 13 0 0 1 0 17 13 13 0 0 1 0-17Z" />
    </svg>
  ),
  agents: (
    <svg className={iconCls} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <path d="M12 3a4 4 0 0 1 4 4v3a4 4 0 0 1-8 0V7a4 4 0 0 1 4-4Z" />
      <path d="M5 11a7 7 0 0 0 14 0M12 18v3" />
    </svg>
  ),
  tasks: (
    <svg className={iconCls} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <path d="M8 4H6.5A2.5 2.5 0 0 0 4 6.5v11A2.5 2.5 0 0 0 6.5 20h11a2.5 2.5 0 0 0 2.5-2.5V16" />
      <path d="M8.5 3.5h7v2.2a1 1 0 0 1-1 1h-5a1 1 0 0 1-1-1V3.5Z" />
      <path d="m13.5 12.5 2 2 5-5" />
    </svg>
  ),
  // A speech bubble with a tail: a chat channel, as distinct from `conversations`.
  whatsapp: (
    <svg className={iconCls} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <path d="M4.5 19.5 5.6 16A8 8 0 1 1 8.4 18.6Z" />
      <path d="M9 9.5c.3 1.9 1.6 3.9 3.8 4.9l1.2-1.1 1.6.8" />
    </svg>
  ),
  email: (
    <svg className={iconCls} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <rect x="3" y="5.5" width="18" height="13" rx="2.5" />
      <path d="m3.8 7 7.1 5.3a2 2 0 0 0 2.2 0L20.2 7" />
    </svg>
  ),
  tools: (
    <svg className={iconCls} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <path d="M14.7 6.3a4 4 0 0 0-5.4 5.2L4 16.8 7.2 20l5.3-5.3a4 4 0 0 0 5.2-5.4l-2.6 2.6-2.2-.4-.4-2.2 2.6-2.6Z" />
    </svg>
  ),
  faqs: <FaqIcon className={iconCls} />,
  integrations: (
    <svg className={iconCls} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <path d="M8 6h8M8 12h8M8 18h8" />
      <rect x="3" y="3" width="4" height="6" rx="1.5" />
      <rect x="17" y="9" width="4" height="6" rx="1.5" />
      <rect x="3" y="15" width="4" height="6" rx="1.5" />
    </svg>
  ),
  secrets: (
    <svg className={iconCls} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <rect x="4" y="10" width="16" height="10" rx="2" />
      <path d="M8 10V7a4 4 0 0 1 8 0v3M12 14v2" />
    </svg>
  ),
  byok: (
    <svg className={iconCls} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="8" cy="14" r="4" />
      <path d="m11 11 8-8M17 5l2.5 2.5M14.5 7.5 17 10" />
    </svg>
  ),
  phone: (
    <svg className={iconCls} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <rect x="7" y="2.5" width="10" height="19" rx="2.5" />
      <path d="M10 5.5h4M11 17.5h2" />
    </svg>
  ),
  // A handset with an arrow leaving it: outbound calling, as distinct from the
  // `calls` handset that stands for the record of calls already made.
  outbound: (
    <svg className={iconCls} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <path d="M4 8.5C4 7 5 6 6.5 6H8l1.5 4L8 11.5a11 11 0 0 0 6.5 6.5L16 16.5 20 18v1.5c0 1.5-1 2.5-2.5 2.5A13.5 13.5 0 0 1 4 8.5Z" />
      <path d="M15 8.5h6m0 0-2.5-2.5M21 8.5 18.5 11" />
    </svg>
  ),
  calls: (
    <svg className={iconCls} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <path d="M4 6.5C4 5 5 4 6.5 4H8l1.5 4L8 9.5a11 11 0 0 0 6.5 6.5L16 14.5 20 16v1.5c0 1.5-1 2.5-2.5 2.5A13.5 13.5 0 0 1 4 6.5Z" />
    </svg>
  ),
  observability: (
    <svg className={iconCls} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <path d="M4 19V9M10 19V5M16 19v-7M22 19H2" />
      <path d="m4 8 6-4 6 7 4-5" />
    </svg>
  ),
  conversations: (
    <svg className={iconCls} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <path d="M5 6.5A3.5 3.5 0 0 1 8.5 3h7A3.5 3.5 0 0 1 19 6.5v5A3.5 3.5 0 0 1 15.5 15H12l-4.5 4v-4A3.5 3.5 0 0 1 5 11.5v-5Z" />
      <path d="M9 8h6M9 11h4" />
    </svg>
  ),
  webhooks: (
    <svg className={iconCls} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <path d="M9 9a3 3 0 1 1 4 2.8l2.5 4.2M15 14a3 3 0 1 1-2.6 4.5H8M9 12a3 3 0 1 1-2.4 4.8" />
    </svg>
  ),
  settings: (
    <svg className={iconCls} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="12" cy="12" r="3" />
      <path d="M12 2.5v2.2M12 19.3v2.2M21.5 12h-2.2M4.7 12H2.5M18.7 5.3l-1.5 1.5M6.8 17.2l-1.5 1.5M18.7 18.7l-1.5-1.5M6.8 6.8 5.3 5.3" />
    </svg>
  ),
  plus: (
    <svg className={iconCls} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <path d="M12 5v14M5 12h14" />
    </svg>
  ),
  tokens: (
    <svg className={iconCls} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="7.5" cy="15.5" r="3.5" />
      <path d="M10.2 12.8 20 3m-4.5 1.5 3 3M12.5 10.5l2.5 2.5" />
    </svg>
  ),
  stream: (
    <svg className={iconCls} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <path d="M3 12h2m14 0h2M8 8.5v7M12 6v12m4-9.5v7" />
    </svg>
  ),
  signout: (
    <svg className={iconCls} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <path d="M14 4h3a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2h-3M10 12H3m0 0 3-3m-3 3 3 3" />
    </svg>
  ),
};

const GROUPS: { label: string; items: { href: string; label: string; icon: keyof typeof I }[] }[] = [
  {
    label: "Build",
    items: [
      { href: "/agents", label: "Agents", icon: "agents" },
      // Directly under Agents: a task is the same thing with nobody on the
      // other end, and it is built out of the same tools listed below it.
      { href: "/tasks", label: "Tasks", icon: "tasks" },
      { href: "/tools", label: "Tools", icon: "tools" },
      // What an agent knows, after what it can do.
      { href: "/faqs", label: "FAQs", icon: "faqs" },
      { href: "/integrations", label: "Integrations", icon: "integrations" },
      { href: "/secrets", label: "Secrets", icon: "secrets" },
      { href: "/byok", label: "BYOK", icon: "byok" },
    ],
  },
  {
    // Phone numbers left Build for this group: a number is not something you
    // build, it is the line an agent answers on — and it belongs beside the
    // thing that dials out of it.
    label: "Telephony",
    items: [
      { href: "/telephony/phone-numbers", label: "Phone Numbers", icon: "phone" },
      { href: "/telephony/outbound-calling", label: "Outbound Calling", icon: "outbound" },
      // The third way a call reaches an agent, beside a browser room and a
      // trunk: somebody else's contact centre, streaming us the audio. Under
      // Telephony because that is what it is to a tenant — a phone call — even
      // though not one line of SIP is involved.
      { href: "/telephony/streams", label: "Media Streams", icon: "stream" },
    ],
  },
  {
    // Named for the channel, the way Telephony above is. Outbound is the batch
    // side of it; anything else email grows — a shared inbox, a template
    // library — belongs beside it rather than under a verb.
    label: "Email",
    items: [{ href: "/email/outbound", label: "Outbound", icon: "email" }],
  },
  {
    // Shaped like Telephony: the numbers agents answer, then the batch side
    // that sends from them.
    label: "WhatsApp",
    items: [
      { href: "/whatsapp/numbers", label: "Numbers", icon: "phone" },
      { href: "/whatsapp/outbound", label: "Outbound", icon: "whatsapp" },
    ],
  },
  {
    label: "Monitor",
    items: [
      { href: "/observability", label: "Observability", icon: "observability" },
      { href: "/conversations", label: "Conversations", icon: "conversations" },
      { href: "/calls", label: "Calls", icon: "calls" },
      { href: "/webhooks", label: "Webhooks", icon: "webhooks" },
    ],
  },
  {
    label: "Organization",
    items: [
      { href: "/settings", label: "Settings", icon: "settings" },
      { href: "/tokens", label: "API Tokens", icon: "tokens" },
    ],
  },
];

/* The region picker, directly under the organization switcher — which is the
   real hierarchy said without a word of explanation: an organization contains
   regions, and a region does not contain organizations.

   Region is a MODE. It silently changes what every list, every form and every
   "create" button means, so three things here are load-bearing rather than
   decorative: the full name (`us` is not a label a person reads), the balance
   beside each name (so the other region's money is visible without switching to
   go and find it), and the fact that every region is always selectable — there
   is no enablement, and a region an organization has never used is a correct
   empty list rather than something to unlock. */
function RegionSwitcher({ orgId }: { orgId: string | null }) {
  const [open, setOpen] = useState(false);
  const [balances, setBalances] = useState<Record<string, number>>({});
  const boxRef = useRef<HTMLDivElement>(null);
  const all = useRegions();
  const active = useActiveRegion();

  useEffect(() => {
    if (!open) return;
    function onDoc(e: MouseEvent) {
      if (!boxRef.current?.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, [open]);

  /* Read on open rather than on mount: it is one request per OTHER region, and
     a sidebar should not make them on every page load for a menu nobody
     touched. This region's own balance is already on screen in the credit
     banner. A read that fails shows no number rather than a wrong one. */
  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    for (const region of all) {
      if (region.slug === active.slug) continue;
      fetch(`${region.api_url}/v1/billing/credits`, { credentials: "include" })
        .then((response) => (response.ok ? response.json() : null))
        .then((body: { balance?: number } | null) => {
          if (!cancelled && typeof body?.balance === "number") {
            setBalances((current) => ({ ...current, [region.slug]: body.balance as number }));
          }
        })
        .catch(() => {});
    }
    return () => {
      cancelled = true;
    };
  }, [open, all, active]);

  // One region is not a choice, and a picker offering it would be chrome tax on
  // every page. It reappears the day a second region is stood up, with no
  // dashboard deploy — the list comes from the control plane at runtime.
  if (all.length < 2) return null;

  return (
    <div className="relative mb-2 max-md:mb-0" ref={boxRef}>
      <button
        type="button"
        className="flex h-8 w-full items-center gap-2 rounded-[10px] px-2 text-left text-[13px] font-medium leading-5 text-muted transition-colors hover:bg-hover hover:text-ink-hover max-md:w-auto"
        aria-haspopup="menu"
        aria-expanded={open}
        // A region's full name does not always fit the sidebar, and which region
        // you are in is the one thing on this screen you must never have to
        // guess. The menu shows it whole; this is for the hover before that.
        title={active.name}
        onClick={() => setOpen((o) => !o)}
      >
        <span className="flex w-5 flex-none justify-center">{I.region}</span>
        <span className="min-w-0 flex-1 truncate max-md:hidden">{active.name}</span>
        <span className="flex-none text-muted max-md:hidden">{I.switcher}</span>
      </button>
      {/* Wider than the sidebar it hangs off: a region's full name is the whole
          point of showing one, and "United States (San Fr…" is the failure this
          exists to prevent. `min-w` rather than a fixed width, so a short list
          still reads as a menu and not a panel. */}
      {open && (
        <div
          className="absolute left-0 top-[calc(100%+6px)] z-[60] min-w-[268px] rounded-[10px] border border-line-2 bg-white p-1.5 shadow-[0_12px_28px_rgba(0,0,0,0.12),0_0_1px_rgba(0,0,0,0.22)] max-md:left-auto max-md:right-0"
          role="menu"
        >
          {/* A complete sentence, because it is the only place the product ever
              explains what a region IS. Everything else — the empty states, the
              credit banner, the webhooks page — assumes the reader has met this
              idea already, and this menu is where they meet it. */}
          <div className="mb-1 px-2 pb-0.5 pt-1 text-[12px] font-medium leading-4 text-muted">
            Each region has its own agents, calls and credits.
          </div>
          {all.map((region) => {
            const current = region.slug === active.slug;
            const balance = current ? undefined : balances[region.slug];
            return (
              <button
                key={region.slug}
                type="button"
                role="menuitem"
                disabled={current || !orgId}
                onClick={() => orgId && switchRegion(orgId, region.slug)}
                className={cn(
                  "flex h-9 w-full items-center gap-2 rounded-[10px] px-2 text-left text-[14px] font-medium leading-5 transition-colors",
                  current
                    ? "bg-hover text-ink-hover"
                    : "text-muted hover:bg-hover hover:text-ink-hover disabled:opacity-50",
                )}
              >
                <span className="min-w-0 flex-1 truncate">{region.name}</span>
                {/* The other region's balance, beside its name — so money that
                    is not spendable here is visible without switching to go and
                    find it. Credits are held per region, and a customer with $50
                    in one and $0 in the other finds that out at the door
                    otherwise. */}
                {typeof balance === "number" && (
                  <span className="flex-none tabular-nums text-[11.5px] font-normal text-placeholder">
                    ${balance.toFixed(2)}
                  </span>
                )}
                {current && (
                  <span className="flex-none text-[11.5px] font-normal text-placeholder">
                    current
                  </span>
                )}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}

export function Nav() {
  const router = useRouter();
  const path = usePathname();
  const [me, setMe] = useState<AuthContextResponse | null>(null);
  const [orgs, setOrgs] = useState<OrgResponse[]>([]);
  const [unseen, setUnseen] = useState<Set<string>>(new Set());
  const [menuOpen, setMenuOpen] = useState(false);
  const [creating, setCreating] = useState(false);
  const [newOrgName, setNewOrgName] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const switchRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    // A 401 here already sends the browser to /login (see lib/api.ts). Anything
    // else means the organization name is missing while the rest of the page
    // works, so log it rather than letting the sidebar quietly read
    // "Organization".
    controlApi.me().then(setMe).catch((error) => {
      console.error("Could not load the signed-in organization", error);
    });
    /* One static build serves every region, so the list is a runtime value. It
       is refreshed here for the NEXT load — the client in lib/api.ts was already
       built from the cached copy — which is what lets a region be added with no
       dashboard deploy. */
    controlApi.listRegions().then((body) => cacheRegions(body.regions)).catch(() => {});
    controlApi.listOrgs().then((page) => {
      setOrgs(page.items);
      const ids = page.items.map((o) => o.id);
      const seen = readSeenOrgs();
      setUnseen(seen === null ? new Set() : new Set(ids.filter((id) => !seen.has(id))));
      if (seen === null) writeSeenOrgs(ids);
    }).catch((error) => {
      console.error("Could not load your organizations", error);
    });
  }, []);

  useEffect(() => {
    if (!menuOpen) return;
    function onDoc(e: MouseEvent) {
      if (!switchRef.current?.contains(e.target as Node)) setMenuOpen(false);
    }
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") setMenuOpen(false);
    }
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onKey);
    };
  }, [menuOpen]);

  useEffect(() => setMenuOpen(false), [path]);

  /* Which organization the session names is only knowable once `me()` answers,
     and the region preference is keyed per organization — so this is where the
     two meet. It normally does nothing; it reloads when the session turns out to
     be for an organization that belongs in another region, which is what stops a
     member who works in `us` from landing on an empty `in`. */
  useEffect(() => {
    if (!me?.tenant?.id) return;
    adoptOrgRegion(me.tenant.id, me.tenant.home_region);
  }, [me]);

  const isActive = (p: string) => path === p || path.startsWith(p + "/");

  const orgName = me?.tenant?.name || "Organization";
  const orgInitial = orgName.trim().charAt(0).toUpperCase();
  const email = me?.user?.email || "";
  const isAdmin = me?.user?.role === "ADMIN";
  const others = orgs.filter((o) => o.id !== me?.tenant?.id);
  const unseenElsewhere = others.filter((o) => unseen.has(o.id)).length;

  async function signout(e: React.MouseEvent) {
    e.preventDefault();
    await controlApi.logout();
    router.push("/login");
  }

  /* Everything on screen belongs to the organization we are leaving, so this is
     a full navigation rather than a router push: it drops all in-memory state
     with the old cookie, the same way sign-out does. */
  async function switchTo(orgId: string) {
    if (busy) return;
    setBusy(true);
    setErr("");
    try {
      await controlApi.switchOrg(orgId);
      writeSeenOrgs(orgs.map((o) => o.id));
      window.location.assign("/agents");
    } catch (error) {
      setErr(apiErrorMessage(error));
      setBusy(false);
    }
  }

  async function createOrg(e: React.FormEvent) {
    e.preventDefault();
    if (busy) return;
    setBusy(true);
    setErr("");
    try {
      const org = await controlApi.createOrg(newOrgName.trim());
      await controlApi.switchOrg(org.id);
      writeSeenOrgs([...orgs.map((o) => o.id), org.id]);
      window.location.assign("/agents");
    } catch (error) {
      setErr(apiErrorMessage(error));
      setBusy(false);
    }
  }

  return (
    <aside
      className={cn(
        "fixed inset-y-0 left-0 z-40 flex w-[var(--side-w)] flex-col gap-1 border-r border-line bg-canvas p-3",
        "max-md:sticky max-md:inset-x-0 max-md:h-auto max-md:w-full max-md:flex-row max-md:flex-wrap max-md:items-center max-md:gap-1.5 max-md:border-b max-md:border-r-0 max-md:p-2.5",
      )}
    >
      <Link
        href="/agents"
        className="mb-2 flex h-[34px] items-center gap-2 rounded-md px-2 text-[16px] font-semibold tracking-normal text-ink transition-colors hover:bg-hover max-md:mb-0"
      >
        {/* The app icon, the same file the browser puts in the tab. It carries
            its own rounded corners as transparency, so no rounding here. */}
        <img src="/brand/icon-192.png" alt="" aria-hidden className="h-5 w-5 flex-none" />
        Talqing
      </Link>

      {/* organization switcher */}
      <div className="relative mb-2 max-md:ml-auto max-md:mb-0" ref={switchRef}>
        <button
          type="button"
          className="flex h-9 w-full items-center gap-2 rounded-[10px] bg-white px-2 text-left text-ink shadow-[0_2px_4px_rgba(0,0,0,0.04),0_0_1.07px_rgba(0,0,0,0.4)] transition-colors hover:bg-subtle max-md:w-auto"
          aria-haspopup="menu"
          aria-expanded={menuOpen}
          onClick={() => setMenuOpen((o) => !o)}
        >
          <span className="relative grid h-5 w-5 flex-none place-items-center rounded-md bg-ink text-[10px] font-semibold text-white">
            {orgInitial}
            {unseenElsewhere > 0 && (
              <span className="absolute -right-1 -top-1 h-2 w-2 rounded-full bg-accent ring-2 ring-white" aria-hidden />
            )}
          </span>
          <span className="flex-1 truncate text-[14px] font-medium leading-5 text-ink max-md:hidden">{orgName}</span>
          <span className="text-muted max-md:hidden">{I.switcher}</span>
        </button>
        {menuOpen && (
          <div
            className="absolute left-0 right-0 top-[calc(100%+6px)] z-[60] rounded-[10px] border border-line-2 bg-white p-1.5 shadow-[0_12px_28px_rgba(0,0,0,0.12),0_0_1px_rgba(0,0,0,0.22)] max-md:left-auto max-md:right-0 max-md:w-72"
            role="menu"
          >
            <div className="flex items-center gap-2.5 px-2 py-2">
              <div className="flex min-w-0 flex-col">
                <b className="truncate text-[14px] font-medium leading-5 text-ink">{orgName}</b>
                {email && <span className="truncate text-[12px] font-normal leading-4 text-muted">{email}</span>}
              </div>
            </div>

            {others.length > 0 && (
              <>
                <div className="my-1 h-px bg-line" />
                <div className="mb-0.5 px-2 pt-1 text-[12px] font-medium leading-4 text-muted">Switch organization</div>
                <div className="scroll-thin max-h-[220px] overflow-y-auto">
                  {others.map((org) => (
                    <button
                      key={org.id}
                      type="button"
                      role="menuitem"
                      disabled={busy}
                      onClick={() => switchTo(org.id)}
                      className="flex h-9 w-full items-center gap-2 rounded-[10px] px-2 text-left text-[14px] font-medium leading-5 text-muted transition-colors hover:bg-hover hover:text-ink-hover disabled:opacity-50"
                    >
                      <span className="grid h-5 w-5 flex-none place-items-center rounded-md bg-subtle text-[10px] font-semibold text-ink-soft">
                        {org.name.trim().charAt(0).toUpperCase()}
                      </span>
                      <span className="min-w-0 flex-1 truncate">{org.name}</span>
                      {unseen.has(org.id) && (
                        <span className="flex-none rounded-full bg-accent/12 px-1.5 py-0.5 text-[11px] font-semibold text-accent">New</span>
                      )}
                      <span className="flex-none text-[11.5px] font-normal text-placeholder">{org.role.toLowerCase()}</span>
                    </button>
                  ))}
                </div>
              </>
            )}

            <div className="my-1 h-px bg-line" />
            <button
              type="button"
              role="menuitem"
              className="flex h-8 w-full items-center gap-2 rounded-[10px] px-2 text-[14px] font-medium leading-5 text-muted transition-colors hover:bg-hover hover:text-ink-hover"
              onClick={() => { setMenuOpen(false); setNewOrgName(""); setErr(""); setCreating(true); }}
            >
              <span className="flex w-5 flex-none justify-center">{I.plus}</span>
              <span>Create organization</span>
            </button>
            {isAdmin && (
              <Link
                href="/settings"
                role="menuitem"
                className="flex h-8 w-full items-center gap-2 rounded-[10px] px-2 text-[14px] font-medium leading-5 text-muted transition-colors hover:bg-hover hover:text-ink-hover"
              >
                <span className="flex w-5 flex-none justify-center">{I.settings}</span>
                <span>Organization settings</span>
              </Link>
            )}
            <a
              className="flex h-8 w-full items-center gap-2 rounded-[10px] px-2 text-[14px] font-medium leading-5 text-muted transition-colors hover:bg-hover hover:text-ink-hover"
              href="#"
              role="menuitem"
              onClick={signout}
            >
              <span className="flex w-5 flex-none justify-center">{I.signout}</span>
              <span>Sign out</span>
            </a>
            {err && <p className="px-2 py-1.5 text-[12px] leading-4 text-danger">{err}</p>}
          </div>
        )}
      </div>

      {/* Directly under the organization switcher, which is the real hierarchy:
          an organization contains regions, a region does not contain
          organizations. No new chrome for it — there is no desktop top bar, and
          a bar introduced to hold one widget is a tax on every page. */}
      <RegionSwitcher orgId={me?.tenant?.id ?? null} />

      {creating && (
        <Modal
          title="Create an organization"
          sub="A fresh, empty organization with you as its admin. Nothing is copied over from this one."
          onClose={() => setCreating(false)}
          width="max-w-[440px]"
          footer={
            <>
              <Button variant="secondary" onClick={() => setCreating(false)}>Cancel</Button>
              <Button form="create-org" disabled={!newOrgName.trim() || busy}>
                {busy ? "Creating…" : "Create and switch"}
              </Button>
            </>
          }
        >
          <form id="create-org" onSubmit={createOrg} className="pb-2">
            <Field label="Name" htmlFor="new-org-name">
              <Input
                id="new-org-name"
                value={newOrgName}
                onChange={(e) => setNewOrgName(e.target.value)}
                placeholder="Acme"
                maxLength={100}
              />
            </Field>
            {err && <p className="mt-2 text-[13px] leading-5 text-danger">{err}</p>}
          </form>
        </Modal>
      )}

      <nav className="scroll-thin flex flex-1 flex-col gap-5 overflow-y-auto max-md:flex-row max-md:gap-1.5">
        {GROUPS.map((g) => (
          <div key={g.label} className="flex flex-col gap-1 max-md:flex-row max-md:gap-0.5">
            {/* mb-0.5 + the list's gap-1 puts exactly 6px under the label */}
            <div className="mb-0.5 ml-1.5 text-[14px] font-medium leading-5 tracking-normal text-muted max-md:hidden">{g.label}</div>
            {g.items.map((it) => {
              const active = isActive(it.href);
              return (
                <Link
                  key={it.href}
                  href={it.href}
                  className={cn(
                    "flex h-8 items-center gap-2 rounded-[10px] px-2 text-[14px] font-medium leading-5 transition-colors max-md:px-2.5",
                    active
                      ? "bg-hover text-ink-hover shadow-none"
                      : "text-muted hover:bg-hover hover:text-ink-hover",
                  )}
                >
                  <span className="flex w-5 flex-none justify-center">{I[it.icon]}</span>
                  <span className="truncate max-md:hidden">{it.label}</span>
                </Link>
              );
            })}
          </div>
        ))}
      </nav>
    </aside>
  );
}
