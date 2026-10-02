// Chrome shared by every public, pre-login page: the landing page and the two
// legal documents. It exists because those pages must be visibly one site —
// Google's OAuth review and Meta's business verification both check that the
// privacy policy is reachable from, and looks like it belongs to, the homepage.
//
// The brand theme here is deliberately NOT the dashboard's token palette. The
// `.landing-theme` class in globals.css swaps the whole CSS-variable set to the
// marketing ramp — white paper, near-black ink, a warm neutral shell — and
// switches the display face to Newsreader, which is why these pages are the
// documented exception to the token-only rule in the README.
import { Figtree, JetBrains_Mono, Newsreader } from "next/font/google";
import type { ReactNode } from "react";

import { LandingHeaderShell } from "./landing-header-shell";
import { DEMO_URL, DOCS_URL, GITHUB_URL } from "./site";

// The public pages run their own pair, unrelated to the dashboard's Inter:
// Figtree for everything set in sans, and Newsreader — a transitional serif with
// wide stroke contrast, ball terminals and a plain ampersand — for display lines.
//
// `axes: ["opsz"]` is load-bearing, not decoration. next/font ships only the
// weight axis of a variable font unless the others are named, and without opsz
// in the file the `font-optical-sizing: auto` in globals.css silently does
// nothing — every heading would render at Newsreader's default text cut and the
// hero would lose exactly the hairline contrast it was chosen for.
const figtree = Figtree({
  subsets: ["latin"],
  variable: "--font-landing-sans",
  display: "swap",
});

const newsreader = Newsreader({
  subsets: ["latin"],
  axes: ["opsz"],
  variable: "--font-landing-serif",
  display: "swap",
});

const mono = JetBrains_Mono({
  subsets: ["latin"],
  variable: "--font-jetbrains",
  display: "swap",
});

// The single destination for every call to action on the public pages. Nothing
// out here links into the dashboard directly: those routes are a static shell
// that would have to call the API from an origin the marketing domain is not
// allowed from, so the link would dead-end rather than sign anyone in. /login
// works from every hostname this build is served on, and the callback lands the
// user on whichever dashboard `app.dashboard_public_url` names.
export const loginHref = "/login";

export const SUPPORT_EMAIL = "hello@talqing.com";

// "This session is live." The public pages are otherwise a white sheet, so this
// is very nearly the only colour on them and it always means the same thing —
// a running session, or a published version of one. Never spend it on decoration.
export const SAGE = "#5b7d58";

// The operating company, as it appears in the footer, both legal pages and the
// governing-law clause. It is one object because those five places must never
// disagree — Meta's business verification compares the name and address on the
// site against the registration documents you upload, and a footer that says one
// thing while the terms say another is exactly what that check is looking for.
//
// `address` is the registered office as filed with the MCA. The district and the
// nearby-landmark line are left out — they are MCA form fields rather than parts
// of the address — so add them back if a verification check ever wants a literal
// match against the incorporation certificate.
// Name, registered office, CIN, phone and email are the set the Companies Act
// asks a company to carry on its official publications, so the footer prints all
// five together rather than scattering them.
export const LEGAL_ENTITY = {
  name: "Oggnai Technologies Private Limited",
  address: "293 S/F, Western Marg, Saidulajab, New Delhi, Delhi 110030, India",
  cin: "U62011DL2025PTC446040",
  phone: "+91 99101 80529",
  jurisdiction: "India",
  courts: "New Delhi",
};

// Rooted at "/", not bare fragments. The landing page is not the only page that
// renders this header any more, so "#capabilities" would scroll nowhere on
// /privacy.
const navItems = [
  { label: "Channels", href: "/#channels" },
  { label: "Capabilities", href: "/#capabilities" },
  { label: "Pricing", href: "/#pricing" },
  { label: "FAQ", href: "/#faq" },
  { label: "Docs", href: DOCS_URL },
  { label: "Star on GitHub", href: GITHUB_URL },
];

function GitHubMark() {
  return (
    <svg
      viewBox="0 0 16 16"
      className="h-[18px] w-[18px] shrink-0"
      fill="currentColor"
      aria-hidden
    >
      <path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82a7.42 7.42 0 0 1 4 0c1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.01 8.01 0 0 0 16 8c0-4.42-3.58-8-8-8Z" />
    </svg>
  );
}

// The repository is another site, so it opens beside this one.
function navLinkProps(href: string) {
  return href === GITHUB_URL
    ? { href, target: "_blank", rel: "noopener noreferrer" }
    : { href };
}

export function MarketingShell({ children }: { children: ReactNode }) {
  return (
    <main
      className={`${figtree.variable} ${newsreader.variable} ${mono.variable} landing-theme relative min-h-screen overflow-x-clip`}
    >
      {children}
    </main>
  );
}

export function ArrowRightIcon({
  className = "h-4 w-4",
}: {
  className?: string;
}) {
  return (
    <svg
      className={className}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
    >
      <path d="M5 12h13" />
      <path d="m12 5.5 6.5 6.5-6.5 6.5" />
    </svg>
  );
}

export function Logo({ large = false }: { large?: boolean }) {
  return (
    <span className="inline-flex items-center gap-[10px] text-foreground">
      {/* The app icon: the same file the browser puts in the tab and the
          dashboard puts in its sidebar, so the brand is one mark everywhere.
          It carries its own rounded corners as transparency, so no rounding
          here. */}
      <img
        src="/brand/icon-192.png"
        alt=""
        aria-hidden
        className={large ? "h-[38px] w-[38px]" : "h-[34px] w-[34px]"}
      />
      <span
        className={`${large ? "text-[27px]" : "text-[26px]"} font-semibold tracking-[-0.025em]`}
      >
        Talqing
      </span>
    </span>
  );
}

export function SiteHeader() {
  return (
    <LandingHeaderShell
      mobileMenu={
        <nav className="px-6 pb-6 pt-2 sm:px-8">
          <ul className="divide-y divide-border">
            {navItems.map((item) => (
              <li key={item.label}>
                <a
                  {...navLinkProps(item.href)}
                  className="flex items-center gap-2.5 py-3.5 text-[17px] text-foreground"
                >
                  {item.href === GITHUB_URL && <GitHubMark />}
                  {item.label}
                </a>
              </li>
            ))}
          </ul>
          <div className="mt-6 flex flex-col gap-3">
            <a
              href={loginHref}
              className="flex h-12 items-center justify-center rounded-lg bg-primary px-5 text-[15px] font-medium text-primary-foreground"
            >
              Start building for free
            </a>
            <a
              href={DEMO_URL}
              target="_blank"
              rel="noopener noreferrer"
              className="flex h-12 items-center justify-center rounded-lg border border-border px-5 text-[15px] font-medium text-foreground"
            >
              Book a demo
            </a>
            <a
              href={loginHref}
              className="flex h-12 items-center justify-center rounded-lg border border-border px-5 text-[15px] font-medium text-foreground"
            >
              Log in
            </a>
          </div>
        </nav>
      }
    >
      <a
        href="/"
        aria-label="Talqing home"
        className="flex shrink-0 items-center"
      >
        <Logo />
      </a>

      <nav className="hidden items-center justify-center gap-9 min-[1100px]:flex xl:gap-11">
        {navItems.map((item) => (
          <a
            key={item.label}
            {...navLinkProps(item.href)}
            className="flex items-center gap-2 whitespace-nowrap text-[18px] text-[#242426] transition-colors duration-200 hover:text-foreground"
          >
            {/* Between 1100 and 1440 the bar has no room for the full label. */}
            {item.href === GITHUB_URL ? (
              <>
                <GitHubMark />
                <span>
                  <span className="hidden min-[1440px]:inline">Star on </span>
                  GitHub
                </span>
              </>
            ) : (
              item.label
            )}
          </a>
        ))}
      </nav>

      <div className="hidden shrink-0 items-center gap-7 min-[1100px]:flex">
        <a
          href={DEMO_URL}
          target="_blank"
          rel="noopener noreferrer"
          className="hidden whitespace-nowrap text-[18px] text-[#242426] transition-colors hover:text-foreground xl:block"
        >
          Book a demo
        </a>
        <a
          href={loginHref}
          className="text-[18px] text-[#242426] transition-colors hover:text-foreground"
        >
          Log in
        </a>
        <a
          href={loginHref}
          className="inline-flex h-12 items-center rounded-lg border border-border px-6 text-[16px] font-medium text-foreground transition-colors hover:border-foreground/25 hover:bg-secondary"
        >
          Start building
        </a>
      </div>
    </LandingHeaderShell>
  );
}

// Deep dashboard links (Agents, Calls, Tokens…) are deliberately absent. Every
// one of them is a dead end for the only kind of visitor a marketing page has —
// a logged-out one — and linking them from here is what invites a crawler into
// the authenticated shell, which app/robots.ts then has to shut back out.
export function Footer() {
  const footerGroups: [string, [string, string][]][] = [
    [
      "Product",
      [
        ["Channels", "/#channels"],
        ["Capabilities", "/#capabilities"],
        ["Pricing", "/#pricing"],
        ["FAQ", "/#faq"],
        ["Documentation", DOCS_URL],
        ["GitHub", GITHUB_URL],
      ],
    ],
    [
      "Legal",
      [
        ["Privacy policy", "/privacy"],
        ["Terms of service", "/terms"],
      ],
    ],
    [
      "Company",
      [
        ["Book a demo", DEMO_URL],
        ["Contact", `mailto:${SUPPORT_EMAIL}`],
        ["Log in", loginHref],
      ],
    ],
  ];

  return (
    <footer className="border-t border-border bg-background">
      <div className="mx-auto w-full max-w-[1800px] px-6 sm:px-10 lg:px-16 xl:px-20 2xl:px-[104px]">
        <div className="grid gap-12 py-16 md:grid-cols-[1.4fr_repeat(3,1fr)] lg:py-20">
          <div>
            <a
              href="/"
              aria-label="Talqing home"
              className="inline-flex items-center gap-2"
            >
              <Logo large />
            </a>
            <p className="mt-5 max-w-xs text-[15px] leading-relaxed text-muted-foreground">
              The no-code studio for voice, video and text agents. You write the
              behaviour; we run the realtime.
            </p>
          </div>
          {footerGroups.map(([heading, links]) => (
            <div key={heading}>
              <h3 className="text-[13px] font-semibold uppercase tracking-[0.1em] text-foreground">
                {heading}
              </h3>
              <ul className="mt-5 space-y-3">
                {links.map(([item, href]) => (
                  <li key={item}>
                    <a
                      href={href}
                      {...((href === DEMO_URL || href === GITHUB_URL) && {
                        target: "_blank",
                        rel: "noopener noreferrer",
                      })}
                      className="text-[15px] text-muted-foreground transition-colors hover:text-foreground"
                    >
                      {item}
                    </a>
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </div>

        <div className="flex flex-col gap-4 border-t border-border py-8 text-[13px] text-muted-foreground sm:flex-row sm:items-end sm:justify-between">
          <div className="space-y-1.5">
            <p>{LEGAL_ENTITY.address}</p>
            <p className="flex flex-wrap items-center gap-x-2 gap-y-1">
              <span>CIN {LEGAL_ENTITY.cin}</span>
              <span aria-hidden>·</span>
              <a
                className="transition-colors hover:text-foreground"
                href={`tel:${LEGAL_ENTITY.phone.replace(/\s/g, "")}`}
              >
                {LEGAL_ENTITY.phone}
              </a>
              <span aria-hidden>·</span>
              <a
                className="transition-colors hover:text-foreground"
                href={`mailto:${SUPPORT_EMAIL}`}
              >
                {SUPPORT_EMAIL}
              </a>
            </p>
          </div>
          <p className="shrink-0">© 2026 {LEGAL_ENTITY.name}</p>
        </div>
      </div>
    </footer>
  );
}
