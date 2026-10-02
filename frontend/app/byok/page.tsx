"use client";
import { Suspense, useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "next/navigation";
import type { CatalogResponse, ProviderKeyResponse } from "@talqing/sdk";
import { AppShell } from "@/app/components/AppShell";
import { Badge, Container, ListSkeleton, PageHead } from "@/app/components/ui";
import { IntegrationLogo } from "@/app/components/IntegrationLogo";
import { ProviderKeyModal } from "@/app/components/ProviderKeyModal";
import { api } from "@/lib/api";
import { apiErrorMessage } from "@/lib/apiError";
import { cn } from "@/lib/cn";

/** A provider row: what the catalog says it is, plus whether we hold a key. */
type Provider = {
  id: string;
  label: string;
  logoUrl: string;
  key: ProviderKeyResponse | null;
};

function ByokPageInner() {
  /* Where "add the key this agent is missing" lands. The blocked editor sends
     the provider it needs, and the box for it is open when the page arrives —
     otherwise the link has only saved the reader a click on the sidebar. */
  const requested = useSearchParams().get("provider");
  const opened = useRef(false);
  const [providers, setProviders] = useState<Provider[]>([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState("");
  const [editing, setEditing] = useState<Provider | null>(null);

  async function load() {
    try {
      // The catalog owns which providers exist and how they look; /byok owns
      // only which of them this workspace has a key for. Join on provider id.
      const [catalog, keys] = await Promise.all([
        api.catalog() as Promise<CatalogResponse>,
        api.listProviderKeys(),
      ]);
      const byProvider = new Map(keys.items.map((item) => [item.provider, item]));
      const rows = Object.entries(catalog.providers)
        .filter(([, meta]) => meta.enabled)
        .map(([id, meta]) => ({
          id,
          label: meta.label,
          logoUrl: meta.logo_url,
          key: byProvider.get(id) ?? null,
        }));
      setProviders(rows);
      // Once per visit: `load()` runs again after every save, and a dialog that
      // reopened itself on the way out could not be closed.
      if (requested && !opened.current) {
        opened.current = true;
        const match = rows.find((provider) => provider.id === requested);
        if (match) setEditing(match);
      }
      setErr("");
    } catch (error) {
      setErr(apiErrorMessage(error));
    } finally {
      setLoading(false);
    }
  }
  useEffect(() => {
    load();
  }, []); // eslint-disable-line react-hooks/exhaustive-deps -- one load per visit

  const configuredCount = useMemo(
    () => providers.filter((provider) => provider.key).length,
    [providers],
  );

  return (
    <AppShell>
      <Container>
        <PageHead
          title="BYOK"
          sub="Your agents run on your own provider accounts — add a key for every model they use."
        />

        {err && (
          <div className="mb-4 rounded-md border border-danger/30 bg-danger/10 px-3.5 py-2.5 text-[13.5px] text-danger">
            {err}
          </div>
        )}

        {loading ? (
          <ListSkeleton rows={3} />
        ) : (
          <>
            <div className="mb-4 text-[13px] leading-5 text-muted">
              {configuredCount} of {providers.length} providers connected.
            </div>
            {/* One panel of hairline-divided rows rather than ten floating
                cards: every provider carries the same four facts, and only a
                list gets them into columns that line up. Two columns on a wide
                screen — one full-width list would strand each key hint a
                thousand pixels from the name it belongs to, and one narrow list
                would leave half the page empty. */}
            <div className="grid grid-cols-1 overflow-hidden rounded-xl border border-line-2 bg-white xl:grid-cols-2">
              {providers.map((provider, index) => (
                <button
                  key={provider.id}
                  type="button"
                  onClick={() => setEditing(provider)}
                  className={cn(
                    "flex w-full items-stretch gap-4 p-4 text-left transition-colors",
                    "hover:bg-canvas focus:outline-none focus:ring-2 focus:ring-inset focus:ring-ink/10",
                    // Hairlines are drawn per cell rather than by a divider on
                    // the grid: a rule above every row but the first, and one
                    // down the left column. The grid fills row-major, so the
                    // odd children are the left column.
                    "border-line",
                    index > 0 && "border-t",
                    index === 1 && "xl:border-t-0",
                    "xl:[&:nth-child(odd)]:border-r",
                  )}
                >
                  <span
                    className={cn(
                      "grid aspect-square min-h-[52px] flex-none place-items-center self-stretch rounded-lg border bg-white",
                      provider.key ? "border-line-2" : "border-dashed border-line-strong",
                    )}
                  >
                    <IntegrationLogo kind={provider.id} logoUrl={provider.logoUrl} size={34} />
                  </span>

                  <span className="flex min-w-0 flex-1 flex-col justify-center gap-0.5">
                    <span className="truncate text-[16px] font-semibold leading-6 tracking-[-0.01em] text-ink">
                      {provider.label}
                    </span>
                    {/* The key belongs next to the name it authenticates. Only
                        four of the eleven providers name the account behind it,
                        so the hint is the one thing this line always carries. */}
                    {provider.key && (
                      <span className="truncate text-[13px] leading-5 text-muted">
                        {provider.key.account_label && (
                          <span className="text-ink-soft">{provider.key.account_label} · </span>
                        )}
                        <span className="font-mono">{provider.key.api_key_hint}</span>
                      </span>
                    )}
                  </span>

                  <span className="flex flex-none items-center gap-3 self-center pl-2">
                    {provider.key ? (
                      <Badge variant="live" dot>
                        connected
                      </Badge>
                    ) : (
                      <Badge>Add key</Badge>
                    )}
                    <svg
                      width="16"
                      height="16"
                      viewBox="0 0 24 24"
                      fill="none"
                      stroke="currentColor"
                      strokeWidth="2"
                      strokeLinecap="round"
                      strokeLinejoin="round"
                      className="flex-none text-placeholder"
                      aria-hidden
                    >
                      <path d="m9 18 6-6-6-6" />
                    </svg>
                  </span>
                </button>
              ))}
            </div>
          </>
        )}

        {editing && (
          <ProviderKeyModal
            provider={editing}
            storedKey={editing.key}
            onClose={() => setEditing(null)}
            onSaved={() => {
              setEditing(null);
              void load();
            }}
            onRemoved={() => {
              setEditing(null);
              void load();
            }}
          />
        )}
      </Container>
    </AppShell>
  );
}

// useSearchParams() suspends during prerender, so the exported page provides the
// boundary and ByokPageInner does the work.
export default function ByokPage() {
  return (
    <Suspense fallback={null}>
      <ByokPageInner />
    </Suspense>
  );
}
