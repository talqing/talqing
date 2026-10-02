import "./globals.css";
import type { Metadata } from "next";
import { Inter, Inter_Tight, JetBrains_Mono } from "next/font/google";
import { Toaster } from "./components/ui";
import { SITE_URL } from "./site";

// Display: Inter Tight — a tighter cut of Inter for confident headings.
const display = Inter_Tight({
  subsets: ["latin"],
  variable: "--font-display",
  display: "swap",
});

// Body / UI: Inter throughout the app shell.
const sans = Inter({
  subsets: ["latin"],
  variable: "--font-sans",
  display: "swap",
});

// Identifiers, metrics, inline code.
const mono = JetBrains_Mono({
  subsets: ["latin"],
  variable: "--font-mono",
  display: "swap",
});

// Inherited by every page that does not set its own — which is all of the
// dashboard, since a client component cannot export metadata. Keep it free of
// claims that need to stay true (prices, limits): a page can override the title
// and description, but nothing overrides a wrong number that got indexed.
//
// metadataBase is what makes `alternates.canonical: "/privacy"` resolve to an
// absolute URL. Without it Next emits a relative canonical, which is legal but
// useless here — the whole point is to name one host out of the four that serve
// this build. See app/site.ts.
export const metadata: Metadata = {
  metadataBase: new URL(SITE_URL),
  title: "talqing — voice agents, no code",
  description:
    "The no-code studio for voice, video and text AI agents. Build, test and publish without writing LiveKit code or running realtime infrastructure.",
  // Not indexable unless a page says so. The dashboard is in this same export
  // and on this same host, its routes render an empty shell that redirects to
  // /login, and none of them can export metadata to opt out. The public pages
  // (and /login) opt in instead, so a new dashboard route is out of the index
  // by default.
  robots: { index: false, follow: false },
  openGraph: { type: "website", siteName: "Talqing" },
  twitter: { card: "summary_large_image" },
  // The app icon, declared rather than dropped into app/ as a metadata file, so
  // the same PNG the browser puts in the tab is the one the UI renders as the
  // brand mark. public/favicon.ico is deliberately not listed: it exists for
  // the clients that ask for /favicon.ico without reading these tags at all.
  icons: {
    icon: [
      { url: "/brand/icon-32.png", type: "image/png", sizes: "32x32" },
      { url: "/brand/icon-192.png", type: "image/png", sizes: "192x192" },
    ],
    apple: { url: "/brand/icon-180.png", sizes: "180x180" },
  },
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={`${display.variable} ${sans.variable} ${mono.variable}`}>
      <body>
        <Toaster>{children}</Toaster>
      </body>
    </html>
  );
}
