"use client";

import { Suspense, useMemo } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { googleLoginUrl } from "@/lib/control";

import { AgentCluster } from "../agent-cluster";
import { Logo, MarketingShell, SUPPORT_EMAIL } from "../marketing-chrome";
import { LocalPrice, useVisitorCountry } from "../visitor-country";

/* -------------------------------------------------------------------------
   The last public page before the dashboard, so it wears the marketing brand
   rather than the dashboard instrument: MarketingShell supplies the same
   palette, Figtree and Newsreader that the landing page runs on. Someone who
   clicks "Start building" should not feel handed off to a different product.

   Two columns that carry different weight on purpose. The left is the reason
   to sign in — the landing page's own claims, in the landing page's own words,
   under the landing page's own agent cluster; a sign-in page is a bad place to
   introduce a new argument. The right is the whole job: one button.
   ------------------------------------------------------------------------- */

// The full set the backend can redirect with — see `_login_error_redirect` in
// backend/api/control/routes/auth.py. Anything else is a bug on our side, so
// the fallback says "try again" rather than inventing a cause.
const ERROR_MESSAGES: Record<string, string> = {
  oauth_denied: "Google sign-in was cancelled. Try again when you are ready.",
  oauth_failed: "Google sign-in did not complete. Please try again.",
};

function GoogleIcon() {
  return (
    <svg className="h-5 w-5 shrink-0" viewBox="0 0 24 24" aria-hidden>
      <path
        fill="#4285F4"
        d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92c-.26 1.37-1.04 2.53-2.21 3.31v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.09z"
      />
      <path
        fill="#34A853"
        d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z"
      />
      <path
        fill="#FBBC05"
        d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l2.85-2.22.81-.62z"
      />
      <path
        fill="#EA4335"
        d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z"
      />
    </svg>
  );
}

function Tick() {
  return (
    <svg
      viewBox="0 0 24 24"
      className="mt-[6px] h-3.5 w-3.5 shrink-0 text-foreground/35"
      fill="none"
      stroke="currentColor"
      strokeWidth="2.4"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
    >
      <path d="M20 6 9 17l-5-5" />
    </svg>
  );
}

/* ------------------------------ the argument ----------------------------- */

const promises = [
  "Voice, video and text agents from one definition",
  "Your own provider keys — we never resell tokens or minutes",
];

const promiseClass = "flex gap-3 text-[15.5px] leading-[1.6] text-foreground/80";

function ProductPanel() {
  const country = useVisitorCountry();
  return (
    <aside className="relative hidden border-r border-border bg-secondary lg:flex lg:flex-col">
      <div className="flex h-20 shrink-0 items-center px-12 xl:px-16">
        <Link href="/" aria-label="Talqing home" className="flex items-center">
          <Logo />
        </Link>
      </div>

      {/* Centred in the column, not pinned to the gutter. The columns have no
          max width — they are half a screen each — so on a 27" display a
          left-pinned block strands itself against the edge with a third of a
          metre of empty shell beside it, while the form sits centred in the
          other half. Both halves centre; the page stays symmetric at any size. */}
      <div className="flex flex-1 items-center justify-center px-12 pb-20 xl:px-16">
        <div className="w-full max-w-[34rem]">
          <h2 className="font-display text-[clamp(2rem,2.6vw,3.25rem)] font-normal leading-[1.1] tracking-[-0.016em] text-foreground">
            Your first agent
            <br /> is one prompt away.
          </h2>

          <ul className="mt-9 space-y-4 border-t border-border pt-8">
            {promises.map((promise) => (
              <li key={promise} className={promiseClass}>
                <Tick />
                {promise}
              </li>
            ))}
            {/* Held back until the country arrives. Unlike the landing page's
                pricing band, this panel is on screen from the first paint, so
                a visitor in India would otherwise watch dollars turn into
                rupees. */}
            <li
              className={`${promiseClass} transition-opacity duration-300 ${country === undefined ? "opacity-0" : ""}`}
            >
              <Tick />
              <LocalPrice
                inr="₹0.01 a message on text, ₹0.35 a minute on voice, ₹1 on video"
                usd="$0.0001 a message on text, $0.0035 a minute on voice, $0.01 on video"
              />
            </li>
          </ul>

          {/* The landing hero's own cluster, not a second illustration of the
              same idea — someone arriving here from "Start building" should be
              looking at the thing they just clicked past. The ramp is tuned to
              this panel's width, which is narrower than the hero's column. */}
          <AgentCluster className="mt-10 [--cluster-scale:0.52] xl:[--cluster-scale:0.62] 2xl:[--cluster-scale:0.7]" />
        </div>
      </div>
    </aside>
  );
}

/* -------------------------------- the form ------------------------------- */

function LoginForm() {
  const searchParams = useSearchParams();
  const country = useVisitorCountry();
  const error = useMemo(() => {
    const code = searchParams.get("error");
    if (!code) return "";
    return ERROR_MESSAGES[code] || ERROR_MESSAGES.oauth_failed;
  }, [searchParams]);

  return (
    <div className="w-full max-w-[26rem]">
      <h1 className="font-display text-[clamp(2.1rem,3vw,3rem)] font-normal leading-[1.1] tracking-[-0.016em] text-foreground">
        Sign in to Talqing.
      </h1>
      {error && (
        <div
          role="alert"
          className="mt-10 flex gap-3 rounded-[10px] border border-[#df5248]/25 bg-[#df5248]/[0.07] px-4 py-3.5 text-[14.5px] leading-[1.6] text-[#8f2f27]"
        >
          <svg
            viewBox="0 0 24 24"
            className="mt-[3px] h-4 w-4 shrink-0"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            aria-hidden
          >
            <circle cx="12" cy="12" r="9" />
            <path d="M12 7.5v5.5" />
            <path d="M12 16.5h.01" />
          </svg>
          {error}
        </div>
      )}

      <a
        href={googleLoginUrl(country)}
        className="mt-10 flex h-[58px] w-full items-center justify-center gap-3 rounded-lg border border-[#dcdcdc] bg-background px-6 text-[16.5px] font-medium text-foreground transition-colors hover:border-foreground/25 hover:bg-secondary"
      >
        <GoogleIcon />
        Continue with Google
      </a>

      <p className="mt-8 text-[14px] leading-[1.7] text-muted-foreground">
        By continuing you agree to our{" "}
        <Link
          href="/terms"
          className="text-foreground underline decoration-[#c9c9c9] underline-offset-[5px] transition-colors hover:decoration-foreground"
        >
          terms of service
        </Link>{" "}
        and{" "}
        <Link
          href="/privacy"
          className="text-foreground underline decoration-[#c9c9c9] underline-offset-[5px] transition-colors hover:decoration-foreground"
        >
          privacy policy
        </Link>
        .
      </p>
    </div>
  );
}

export default function LoginPage() {
  return (
    <MarketingShell>
      <div className="grid min-h-screen lg:grid-cols-[minmax(0,0.92fr)_minmax(0,1fr)]">
        <ProductPanel />

        <div className="flex min-h-screen min-w-0 flex-col">
          <header className="flex h-20 shrink-0 items-center gap-6 px-6 sm:px-10 lg:px-14">
            <Link
              href="/"
              aria-label="Talqing home"
              className="flex items-center lg:hidden"
            >
              <Logo />
            </Link>
            <Link
              href="/"
              className="group ml-auto inline-flex items-center gap-2 text-[15.5px] text-[#242426] transition-colors hover:text-foreground"
            >
              <svg
                viewBox="0 0 24 24"
                className="h-4 w-4 transition-transform duration-300 ease-out group-hover:-translate-x-0.5"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.8"
                strokeLinecap="round"
                strokeLinejoin="round"
                aria-hidden
              >
                <path d="M19 12H6" />
                <path d="m12 5.5-6.5 6.5 6.5 6.5" />
              </svg>
              Back to site
            </Link>
          </header>

          {/* Centred rather than flush left with the wordmark: the form is
              narrow by design, and pinning it to the gutter leaves a column of
              dead paper to its right that reads as a layout fault. */}
          <div className="flex flex-1 items-center px-6 py-14 sm:px-10 lg:justify-center lg:px-14">
            <Suspense
              fallback={
                <div className="w-full max-w-[26rem] text-[15px] text-muted-foreground">
                  Loading…
                </div>
              }
            >
              <LoginForm />
            </Suspense>
          </div>

          {/* Centred on the column rather than set to the form's left edge:
              it is a footnote for the page, not a field of the form. */}
          <footer className="shrink-0 px-6 pb-8 sm:px-10 lg:px-14">
            <p className="text-center text-[14px] text-muted-foreground">
              Trouble signing in?{" "}
              <a
                href={`mailto:${SUPPORT_EMAIL}`}
                className="text-foreground underline decoration-[#c9c9c9] underline-offset-[5px] transition-colors hover:decoration-foreground"
              >
                {SUPPORT_EMAIL}
              </a>
            </p>
          </footer>
        </div>
      </div>
    </MarketingShell>
  );
}
