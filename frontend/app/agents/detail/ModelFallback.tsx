"use client";

import type { CatalogResponse, CatalogEntry, LLMCatalogEntryResponse, STTCatalogEntryResponse } from "@talqing/sdk";

import { Label, MODEL_HOSTS_GRID, Select } from "../../components/ui";
import { catalogEntryLabel, isSearchedProvider, providerOptionsFor, type SearchedKind } from "./agentConfig";
import { SearchedModelSelect } from "./ModelSearch";

/** The optional failover model for one stage of the pipeline.
 *
 *  Collapsed to a single link until the user opts in, because most agents do not
 *  need one: a fallback costs a second provider key and only earns its keep
 *  during an outage. Once added it renders the same provider/model pair as the
 *  primary above it, plus whatever per-stage control `children` supplies (an STT
 *  language, a TTS voice). */
export function ModelFallback({
  catalog,
  kind,
  entries,
  value,
  onChange,
  children,
  footer,
  extraProviders = [],
  searchedEntry,
  onProvider,
}: {
  catalog: CatalogResponse;
  /** Which stage this backs. Decides whether a provider's models are searched
   *  here: OpenRouter searches language and speech-to-text models, not voices. */
  kind: SearchedKind | "tts";
  /** Everything eligible here — already filtered to the channel, and with the
   *  primary's own model removed so it cannot be chosen as its own fallback. */
  entries: CatalogEntry[];
  value: { provider?: string; model?: string } | null | undefined;
  /** Receives the chosen catalog entry, or null when the fallback is removed. */
  onChange: (entry: CatalogEntry | null) => void;
  /** Providers to offer that contribute no entries — see `providerOptionsFor`.
   *  A searched provider has none here until one of its models is picked. */
  extraProviders?: string[];
  /** The resolved entry for a fallback at a searched provider, once it is in
   *  hand. Its absence is why the model control below cannot be a `<Select>`
   *  over `entries`: this model is not in that list and never will be. */
  searchedEntry?: LLMCatalogEntryResponse | STTCatalogEntryResponse;
  /** Called instead of `onChange` when the chosen provider has no entries here,
   *  which is what picking a searched one looks like: the provider is settled
   *  and the model is the next thing to choose. */
  onProvider?: (provider: string) => void;
  /** A third control alongside provider/model, e.g. the STT language. */
  children?: React.ReactNode;
  /** A full-width control under the grid, e.g. the TTS voice — mirroring how
   *  the primary stacks its voice picker below the provider/model row. */
  footer?: React.ReactNode;
}) {
  if (!entries.length) return null;

  if (!value) {
    return (
      // An action, styled as one. As an underlined word in a run of muted prose
      // it read as part of the sentence rather than as the button it is.
      <div className="mt-4 flex flex-wrap items-center gap-x-3 gap-y-1.5 border-t border-line pt-3.5">
        <button
          type="button"
          className="inline-flex min-h-8 flex-none items-center gap-1.5 rounded-[10px] border border-line-2 bg-white px-2.5 text-[13px] font-medium text-ink transition-colors hover:bg-subtle focus:outline-none focus-visible:ring-2 focus-visible:ring-ink/10"
          onClick={() => onChange(entries[0])}
        >
          <svg className="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden>
            <path d="M12 5v14M5 12h14" />
          </svg>
          Add a fallback
        </button>
        <span className="text-[13px] leading-5 text-muted">
          {/* Not "mid-call": this same control configures an agent task, which
              has no call. True of both, and shorter. */}
          Used automatically if this provider starts failing.
        </span>
      </div>
    );
  }

  const providers = providerOptionsFor(catalog, entries, extraProviders);
  const models = entries.filter((entry) => entry.provider === value.provider);
  const searched = kind !== "tts" && isSearchedProvider(catalog, value.provider, kind);

  return (
    <div className="mt-4 rounded-lg border border-line bg-canvas p-3.5">
      <div className="mb-3 flex items-baseline justify-between gap-3">
        <div className="text-[13px] font-semibold leading-5 text-ink">
          Fallback
          <span className="ml-2 font-normal text-muted">
            Takes over when the model above starts failing
          </span>
        </div>
        <button
          type="button"
          className="shrink-0 rounded text-[13px] font-medium text-muted transition-colors hover:text-danger focus:outline-none focus-visible:ring-2 focus-visible:ring-ink/10"
          onClick={() => onChange(null)}
        >
          Remove
        </button>
      </div>
      {/* A searched language model brings a host picker among `children`. */}
      <div
        className={
          searched && kind === "llm"
            ? MODEL_HOSTS_GRID
            : "grid grid-cols-1 gap-3.5 md:grid-cols-[minmax(170px,220px)_minmax(220px,1fr)] lg:grid-cols-[minmax(170px,220px)_minmax(220px,1fr)_minmax(160px,190px)]"
        }
      >
        <div className="flex min-w-0 flex-col gap-2">
          <Label>Provider</Label>
          <Select
            value={value.provider || ""}
            onChange={(e) => {
              // A searched provider always goes to `onProvider`, even though
              // `entries` may happen to contain one of its models: what is in
              // there is whatever this agent already referred to, not a menu, so
              // picking from it would land the failover on an arbitrary model
              // nobody chose. The model control beside this is where the choice
              // belongs.
              if (kind !== "tts" && isSearchedProvider(catalog, e.target.value, kind)) {
                onProvider?.(e.target.value);
                return;
              }
              const entry = entries.find((item) => item.provider === e.target.value);
              if (entry) onChange(entry);
            }}
          >
            {providers.map((provider) => (
              <option key={provider.value} value={provider.value} data-logo-url={provider.logoUrl}>
                {provider.label}
              </option>
            ))}
          </Select>
        </div>
        <div className="flex min-w-0 flex-col gap-2">
          <Label>Model</Label>
          {searched ? (
            <SearchedModelSelect
              kind={kind}
              entry={searchedEntry}
              value={value.model}
              onChange={onChange}
              ariaLabel="Fallback model"
            />
          ) : (
            <Select
              value={value.model || ""}
              onChange={(e) => {
                const entry = models.find((item) => item.model === e.target.value);
                if (entry) onChange(entry);
              }}
            >
              {models.map((entry) => (
                <option key={entry.model} value={entry.model}>
                  {catalogEntryLabel(entry)}
                </option>
              ))}
            </Select>
          )}
        </div>
        {children}
      </div>
      {footer && <div className="mt-3.5 flex min-w-0 flex-col gap-2">{footer}</div>}
    </div>
  );
}
