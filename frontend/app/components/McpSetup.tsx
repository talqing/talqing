"use client";

/* "Build agents from your terminal" — the workspace's MCP token plus ready-made
   setup for the coding agents people actually use.

   Everything the platform can do is reachable over MCP, so this is the shortest
   path from signing up to building an agent from Claude Code. The token is the
   one issued at signup (GET /v1/mcp-token), which is why it can be shown here
   at all: it is re-derived from its row rather than stored, so it can be read
   as often as needed instead of once at create.

   It is masked until asked for, including inside the setup snippet, so the
   panel is safe to leave open while screen-sharing. Copy always copies the real
   value. */

import { useEffect, useState } from "react";
import type { McpTokenResponse } from "@/lib/control";
import { api, controlApi } from "@/lib/api";
import { apiErrorMessage } from "@/lib/apiError";
import { useActiveRegion } from "@/lib/regions";
import { cn } from "@/lib/cn";
import { Button } from "./ui";

const OPEN_KEY = "talqing.mcp-setup.open";
const MASK = "•".repeat(40);

type ClientKey = "claude" | "codex" | "grok";

const CLIENTS: {
  key: ClientKey;
  label: string;
  where: string;
  snippet: (apiUrl: string, token: string) => string;
}[] = [
  {
    key: "claude",
    label: "Claude Code",
    where: "Run this once in your terminal.",
    snippet: (apiUrl, token) =>
      [
        "claude mcp add talqing \\",
        `  -e TALQING_BASE_URL=${apiUrl} \\`,
        `  -e TALQING_API_KEY=${token} \\`,
        "  -- npx -y @talqing/mcp",
      ].join("\n"),
  },
  {
    key: "codex",
    label: "Codex",
    where: "Run this once in your terminal.",
    snippet: (apiUrl, token) =>
      [
        "codex mcp add talqing \\",
        `  --env TALQING_BASE_URL=${apiUrl} \\`,
        `  --env TALQING_API_KEY=${token} \\`,
        "  -- npx -y @talqing/mcp",
      ].join("\n"),
  },
  {
    // Grok's `mcp add` takes no --env, so the server's credentials can only be
    // set in the config file.
    key: "grok",
    label: "Grok",
    where: "Add this to ~/.grok/config.toml.",
    snippet: (apiUrl, token) =>
      [
        "[mcp_servers.talqing]",
        'command = "npx"',
        'args = ["-y", "@talqing/mcp"]',
        `env = { TALQING_BASE_URL = "${apiUrl}", TALQING_API_KEY = "${token}" }`,
      ].join("\n"),
  },
];

const Icon = {
  terminal: (
    <svg className="h-[17px] w-[17px]" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <rect x="3" y="4" width="18" height="16" rx="2.5" />
      <path d="m7.5 9.5 3 2.5-3 2.5M13 15h4" />
    </svg>
  ),
  chevron: (
    <svg className="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="m6 9 6 6 6-6" />
    </svg>
  ),
  eye: (
    <svg className="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12Z" />
      <circle cx="12" cy="12" r="3" />
    </svg>
  ),
  eyeOff: (
    <svg className="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="M4 4l16 16M9.9 5.9A9.6 9.6 0 0 1 12 5.5c6 0 9.5 6.5 9.5 6.5a17 17 0 0 1-3.3 4M6.3 8.1A17 17 0 0 0 2.5 12S6 18.5 12 18.5c.9 0 1.7-.1 2.5-.4M9.9 9.9a3 3 0 0 0 4.2 4.2" />
    </svg>
  ),
};

/** One copy button's worth of state: copy, then say so for a moment. */
function useCopy() {
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    if (!copied) return;
    const timer = setTimeout(() => setCopied(false), 1600);
    return () => clearTimeout(timer);
  }, [copied]);
  return {
    copied,
    async copy(text: string) {
      await navigator.clipboard.writeText(text);
      setCopied(true);
    },
  };
}

export function McpSetup() {
  const region = useActiveRegion();
  const [mcp, setMcp] = useState<McpTokenResponse | null>(null);
  const [err, setErr] = useState("");
  const [open, setOpen] = useState(true);
  const [revealed, setRevealed] = useState(false);
  const [client, setClient] = useState<ClientKey>("claude");
  const snippet = useCopy();

  useEffect(() => {
    // Read after mount: the dashboard is statically exported, so localStorage
    // is not available while rendering.
    setOpen(window.localStorage.getItem(OPEN_KEY) !== "false");
    controlApi.mcpToken().then(setMcp).catch((error) => setErr(apiErrorMessage(error)));
  }, []);

  function toggle() {
    setOpen((wasOpen) => {
      window.localStorage.setItem(OPEN_KEY, String(!wasOpen));
      return !wasOpen;
    });
  }

  const active = CLIENTS.find((entry) => entry.key === client)!;
  /* The base URL is THIS REGION's, and the token is region-agnostic: one token
     reaches every region, so pointing a coding agent at another one is the same
     snippet with a different URL. That is also why the URL comes from here
     rather than from the token response — only the dashboard knows which region
     the person is looking at. */
  // Shown masked, copied real — so the panel stays safe on a shared screen.
  const shownSnippet = mcp
    ? active.snippet(region.api_url, revealed ? mcp.token : MASK)
    : "";

  return (
    <section className="mb-6 overflow-hidden rounded-xl border border-line-2 bg-white shadow-[0_1px_2px_rgba(15,15,16,0.025)]">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2 px-4 py-3.5">
        <span className="grid h-9 w-9 flex-none place-items-center rounded-[10px] border border-line bg-canvas text-ink-soft">
          {Icon.terminal}
        </span>
        <div className="min-w-0 flex-1">
          <h2 className="text-[14px] font-semibold leading-5 text-ink">Build agents from your terminal</h2>
          <p className="mt-0.5 text-[13px] leading-5 text-muted">
            Connect Claude Code, Codex or Grok to this organization — they can do everything the
            dashboard can.
          </p>
        </div>

        <button
          type="button"
          onClick={toggle}
          aria-expanded={open}
          className="inline-flex min-h-8 items-center gap-1 rounded-lg px-2 text-[13px] font-medium text-ink-soft transition-colors hover:bg-hover hover:text-ink"
        >
          {open ? "Hide" : "Set up"}
          <span className={cn("transition-transform", open && "rotate-180")}>{Icon.chevron}</span>
        </button>
      </div>

      {open && (
        <div className="border-t border-line px-4 py-4">
          {err ? (
            <p className="text-[13px] leading-5 text-danger">{err}</p>
          ) : !mcp ? (
            <div className="h-[132px] animate-shimmer rounded-lg bg-[length:200%_100%] bg-gradient-to-r from-subtle via-line to-subtle" />
          ) : (
            <>
              <div className="mb-4 flex flex-wrap items-center gap-2">
                <div className="flex gap-1 rounded-[10px] border border-line-2 bg-subtle p-1">
                  {CLIENTS.map((entry) => (
                    <button
                      key={entry.key}
                      type="button"
                      onClick={() => setClient(entry.key)}
                      className={cn(
                        "min-h-7 rounded-md px-2.5 text-[13px] font-medium transition-colors",
                        entry.key === client
                          ? "bg-white text-ink shadow-[0_1px_2px_rgba(15,15,16,0.06)]"
                          : "text-muted hover:text-ink",
                      )}
                    >
                      {entry.label}
                    </button>
                  ))}
                </div>
                <span className="text-[13px] leading-5 text-muted">{active.where}</span>
                <div className="ml-auto flex items-center gap-2">
                  <button
                    type="button"
                    onClick={() => setRevealed((shown) => !shown)}
                    className="inline-flex min-h-8 items-center gap-1.5 rounded-lg px-2 text-[13px] font-medium text-ink-soft transition-colors hover:bg-hover hover:text-ink"
                  >
                    {revealed ? Icon.eyeOff : Icon.eye}
                    {revealed ? "Hide token" : "Reveal token"}
                  </button>
                  <Button variant="secondary" size="sm" onClick={() => snippet.copy(active.snippet(region.api_url, mcp.token))}>
                    {snippet.copied ? "Copied ✓" : "Copy"}
                  </Button>
                </div>
              </div>

              <pre className="overflow-x-auto rounded-lg border border-line-2 bg-subtle px-3.5 py-3 font-mono text-[12.5px] leading-[1.7] text-ink-soft">
                {shownSnippet}
              </pre>
            </>
          )}
        </div>
      )}
    </section>
  );
}
