import type { Metadata } from "next";

// The page itself is a client component and cannot export metadata.
export const metadata: Metadata = {
  title: "Log in — Talqing",
  description:
    "Sign in to Talqing with Google to build, test and publish AI voice, video and text agents.",
  alternates: { canonical: "/login" },
  robots: { index: true, follow: true },
};

export default function LoginLayout({ children }: { children: React.ReactNode }) {
  return children;
}
