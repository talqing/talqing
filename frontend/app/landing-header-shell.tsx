"use client";

import { useEffect, useState, type ReactNode } from "react";

// The bar itself is a client component for two reasons only: it grows a
// hairline once the page scrolls off the top, and it owns the mobile menu's
// open state. Everything inside it — the logo, the nav links, the actions — is
// passed down as server-rendered children from marketing-chrome, so the
// navigation is defined in exactly one place.
//
// The header runs on a wider gutter than the page content below it, which is
// deliberate: the wordmark sits outside the text column the way it does on the
// reference comp.
export function LandingHeaderShell({
  children,
  mobileMenu,
}: {
  children: ReactNode;
  mobileMenu: ReactNode;
}) {
  const [isScrolled, setIsScrolled] = useState(false);
  const [isMenuOpen, setIsMenuOpen] = useState(false);

  useEffect(() => {
    const updateScrolled = () => setIsScrolled(window.scrollY > 4);
    updateScrolled();
    window.addEventListener("scroll", updateScrolled, { passive: true });
    return () => window.removeEventListener("scroll", updateScrolled);
  }, []);

  // A menu that stays open behind a closed overlay is the classic mobile-nav
  // bug: the user rotates to landscape, the panel is hidden at `md`, and the
  // page underneath is still locked. Close it when the breakpoint passes.
  useEffect(() => {
    if (!isMenuOpen) return;

    const wide = window.matchMedia("(min-width: 1100px)");
    const close = () => setIsMenuOpen(false);
    wide.addEventListener("change", close);

    const onEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setIsMenuOpen(false);
    };
    window.addEventListener("keydown", onEscape);

    document.body.style.overflow = "hidden";
    return () => {
      wide.removeEventListener("change", close);
      window.removeEventListener("keydown", onEscape);
      document.body.style.overflow = "";
    };
  }, [isMenuOpen]);

  return (
    <header className="fixed inset-x-0 top-0 z-50">
      <div
        className={`transition-[background-color,border-color] duration-300 ${
          isScrolled
            ? "border-b border-border bg-background/90 backdrop-blur-xl"
            : "border-b border-transparent bg-background"
        }`}
      >
        <div className="mx-auto grid h-[72px] max-w-[1800px] grid-cols-[auto_1fr_auto] items-center gap-6 px-6 sm:px-8 lg:px-10 min-[1100px]:h-20">
          {children}

          <button
            type="button"
            onClick={() => setIsMenuOpen((open) => !open)}
            aria-expanded={isMenuOpen}
            aria-label={isMenuOpen ? "Close menu" : "Open menu"}
            className="col-start-3 -mr-2 flex h-10 w-10 items-center justify-center rounded-lg text-foreground transition-colors hover:bg-secondary min-[1100px]:hidden"
          >
            <svg
              viewBox="0 0 24 24"
              className="h-5 w-5"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.6"
              strokeLinecap="round"
              aria-hidden
            >
              {isMenuOpen ? (
                <>
                  <path d="m6 6 12 12" />
                  <path d="M18 6 6 18" />
                </>
              ) : (
                <>
                  <path d="M3.5 8h17" />
                  <path d="M3.5 16h17" />
                </>
              )}
            </svg>
          </button>
        </div>
      </div>

      {/* A scrim under the panel: without it the page keeps showing through
          below a half-height drawer, which reads as a rendering fault rather
          than an open menu. Tapping it closes, same as the links do. */}
      <div
        onClick={() => setIsMenuOpen(false)}
        aria-hidden
        className={`fixed inset-0 top-[72px] bg-foreground/25 transition-opacity duration-200 min-[1100px]:hidden ${
          isMenuOpen ? "visible opacity-100" : "invisible opacity-0"
        }`}
      />

      <div
        className={`absolute inset-x-0 top-[72px] origin-top border-b border-border bg-background transition-all duration-200 min-[1100px]:hidden ${
          isMenuOpen
            ? "visible opacity-100"
            : "invisible -translate-y-2 opacity-0"
        }`}
      >
        <div onClick={() => setIsMenuOpen(false)}>{mobileMenu}</div>
      </div>
    </header>
  );
}
