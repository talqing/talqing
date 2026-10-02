"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type {
  CatalogResponse,
  LLMCatalogEntryResponse,
  STTCatalogEntryResponse,
} from "@talqing/sdk";

import { api } from "@/lib/api";

import { Select } from "../../components/ui";

import { isSearchedProvider, type SearchedKind } from "./agentConfig";

/* One provider reaches hundreds of language models and a couple of dozen
   speech-to-text ones through a single key, so its models are not in
   `GET /catalog` at all — they are searched a page at a time through
   `GET /catalog/models?kind=`. Everything in this file exists to make that one
   difference invisible to the rest of the editor: the same `<Select>`, the same
   catalog entries, the same cost estimate and thinking control.

   The rest of the editor therefore keeps looking models up in `catalog.llm` and
   `catalog.stt`, and `useSearchedModels` is what makes that work — it merges the
   entries for the slugs this agent actually names back into the catalog it
   hands down. */

/** The entry a search of each kind returns. */
type SearchedEntry = { llm: LLMCatalogEntryResponse; stt: STTCatalogEntryResponse };

type Named = { provider?: string; model?: string } | null | undefined;

/** The model slots a config can name — an agent has all of them, a task only
 *  `llm`. Structural, so both configs fit without a cast. */
type ModelSlots = {
  llm?: (NonNullable<Named> & { fallback?: Named }) | null;
  analysis?: { model?: Named } | null;
  stt?: (NonNullable<Named> & { fallback?: Named }) | null;
};

/** The `kind/slug` keys one config names at a searched provider — the language
 *  model and its failover, the model post-call analysis runs on, and the
 *  speech-to-text model and its failover. */
function searchedKeys(catalog: CatalogResponse, config: ModelSlots): string[] {
  const specs: [SearchedKind, { provider?: string; model?: string } | null | undefined][] = [
    ["llm", config.llm],
    ["llm", config.llm?.fallback],
    ["llm", config.analysis?.model],
    ["stt", config.stt],
    ["stt", config.stt?.fallback],
  ];
  return specs
    .filter(([kind, spec]) => spec?.model && isSearchedProvider(catalog, spec.provider, kind))
    .map(([kind, spec]) => `${kind}/${spec!.model!}`);
}

/**
 * The catalog, with the searched models this agent names merged into `llm` and
 * `stt`.
 *
 * Without this every lookup in the editor — the label on the trigger, the
 * thinking control, the cost estimate, whether the model reads images, whether
 * speech-to-text is batch — would miss, and `normalizeCatalogSelections` would
 * re-point the agent at the first model it *does* know. So the entries are
 * fetched once by exact slug and put where the rest of the editor already looks.
 *
 * A slug that resolves to nothing is left out rather than faked: the model was
 * withdrawn, or this region's registry has not loaded yet, and either way the
 * editor should say so (publish validation does) instead of inventing an entry.
 */
export function useSearchedModels(catalog: CatalogResponse | null, config: ModelSlots | null) {
  const [found, setFound] = useState<{ llm: LLMCatalogEntryResponse[]; stt: STTCatalogEntryResponse[] }>(
    { llm: [], stt: [] },
  );
  // `kind/slug` keys already asked about, resolved or not, so a withdrawn model
  // is not re-fetched on every render.
  const asked = useRef(new Set<string>());

  const add = useCallback(<K extends SearchedKind>(kind: K, entry: SearchedEntry[K]) => {
    setFound((prev) =>
      prev[kind].some((e) => e.model === entry.model) ? prev : { ...prev, [kind]: [...prev[kind], entry] },
    );
  }, []);

  const remember = useCallback(
    <K extends SearchedKind>(kind: K, entry: SearchedEntry[K]) => {
      asked.current.add(`${kind}/${entry.model}`);
      add(kind, entry);
    },
    [add],
  );

  const wanted = catalog && config ? searchedKeys(catalog, config) : [];
  // Depend on the joined list rather than the array identity, which changes on
  // every render of a config object.
  const wantedKey = wanted.join("\n");
  useEffect(() => {
    const keys = wantedKey ? wantedKey.split("\n") : [];
    for (const key of keys) {
      if (asked.current.has(key)) continue;
      asked.current.add(key);
      const [kind, ...rest] = key.split("/");
      const slug = rest.join("/");
      void api
        .searchModels({ q: slug, limit: 1, kind: kind as SearchedKind })
        .then((page) => {
          // An exact match leads the results; anything else is a different model
          // that merely contains this slug, and adopting it would silently show
          // the agent running something it is not.
          if (page.kind === "llm") {
            const hit = page.models.find((entry) => entry.model === slug);
            if (hit) add("llm", hit);
          } else {
            const hit = page.models.find((entry) => entry.model === slug);
            if (hit) add("stt", hit);
          }
        })
        .catch(() => {
          /* Leave it unresolved. The editor renders the raw slug and publish
             validation is the thing that gets to call it invalid. */
        });
    }
  }, [wantedKey, add]);

  const merged = useMemo(() => {
    if (!catalog || (found.llm.length === 0 && found.stt.length === 0)) return catalog;
    const unknown = <T extends { provider: string; model: string }>(listed: T[], extra: T[]) => {
      const known = new Set(listed.map((entry) => `${entry.provider}/${entry.model}`));
      return extra.filter((e) => !known.has(`${e.provider}/${e.model}`));
    };
    return {
      ...catalog,
      llm: [...catalog.llm, ...unknown(catalog.llm, found.llm)],
      stt: [...catalog.stt, ...unknown(catalog.stt, found.stt)],
    };
  }, [catalog, found]);

  return { catalog: merged, remember };
}

/* ── the picker ─────────────────────────────────────────────────────────── */

/** A context window, rounded DOWN.
 *
 *  Never up: this is a capacity someone sizes a prompt against, and a window
 *  reported as bigger than it is invites a request that will not fit. Compact
 *  `Intl` formatting rounds to nearest, which turns OpenRouter's 1,050,000 for
 *  `gpt-5.6-luna` into "1.1M" — a number that model does not have. */
function contextLabel(tokens: number): string {
  if (tokens < 1000) return String(tokens);
  if (tokens < 1_000_000) return `${Math.floor(tokens / 1000)}K`;
  return `${Math.floor(tokens / 100_000) / 10}M`;
}

function money(value: unknown): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  return value < 1 ? `$${value.toFixed(3)}` : `$${value.toFixed(2)}`;
}

/** "$0.150 in · $0.600 out per 1M" — a language model's rates, and one host's. */
export function priceLine(pricing: { input_per_1m?: number | null; output_per_1m?: number | null }): string {
  return `${money(pricing.input_per_1m)} in · ${money(pricing.output_per_1m)} out per 1M`;
}

/** The line under a model's name: what it costs and, for a language model, how
 *  much it can hold — the facts that decide between 260 of them. A speech model
 *  priced per second reads per minute, the unit the cost estimate uses; the two
 *  priced per token read like a language model. */
function detail(entry: LLMCatalogEntryResponse | STTCatalogEntryResponse): string {
  const pricing = entry.pricing as Record<string, unknown>;
  if (typeof pricing.per_audio_second === "number") {
    return `${money(pricing.per_audio_second * 60)} per minute of speech`;
  }
  const price = priceLine(pricing);
  // Only a language model publishes a context window.
  const context = entry.context_length;
  return typeof context === "number" && context > 0 ? `${contextLabel(context)} context · ${price}` : price;
}

const PAGE = 30;

/**
 * One model from a searched provider, chosen by typing or by scrolling.
 *
 * Opens on the popularity-ranked first page — OpenRouter ranks its own list by
 * tokens actually processed, which beats anything we would curate — narrows as
 * the user types, and pages in the rest on scroll. Paging is not a nicety here:
 * one page is 30 of about 260, so without it the other 230 would be reachable
 * only by already knowing what to type.
 *
 * The query is debounced rather than fired per keystroke, and a slower answer
 * that arrives after a newer one is dropped — which is also what keeps a page
 * that was requested for an older query from being appended to a newer one.
 */
export function SearchedModelSelect<K extends SearchedKind>({
  kind,
  entry,
  value,
  onChange,
  ariaLabel,
}: {
  kind: K;
  /** The chosen model's entry, once it has been resolved. Null while it is
   *  still being fetched, or if the provider no longer lists it. */
  entry: SearchedEntry[K] | undefined;
  value: string | undefined;
  onChange: (entry: SearchedEntry[K]) => void;
  ariaLabel?: string;
}) {
  const [results, setResults] = useState<SearchedEntry[K][]>([]);
  const [busy, setBusy] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const latest = useRef(0);
  // What the pages on screen were fetched for, and where the next one starts.
  // Null means there is no next one; both live in a ref because `loadMore` is
  // called from a scroll handler that must not re-subscribe on every page.
  const paging = useRef<{ query: string; cursor: string | null }>({ query: "", cursor: null });

  useEffect(() => () => clearTimeout(timer.current), []);

  const search = useCallback((query: string) => {
    clearTimeout(timer.current);
    setBusy(true);
    timer.current = setTimeout(() => {
      const seq = ++latest.current;
      void api
        .searchModels({ q: query, limit: PAGE, kind })
        .then((page) => {
          if (seq !== latest.current) return;
          paging.current = { query, cursor: page.next_cursor ?? null };
          // The page is the kind that was asked for; the union type cannot know
          // that the `kind` argument decided which member came back.
          setResults(page.models as SearchedEntry[K][]);
          setBusy(false);
        })
        .catch(() => {
          if (seq !== latest.current) return;
          paging.current = { query, cursor: null };
          setResults([]);
          setBusy(false);
        });
    }, 180);
  }, [kind]);

  const loadMore = useCallback(() => {
    const { query, cursor } = paging.current;
    if (cursor === null) return;
    // Claimed up front, so a second scroll event before this page lands does not
    // ask for the same one twice.
    paging.current = { query, cursor: null };
    const seq = ++latest.current;
    setBusy(true);
    void api
      .searchModels({ q: query, limit: PAGE, cursor, kind })
      .then((page) => {
        if (seq !== latest.current) return;
        paging.current = { query, cursor: page.next_cursor ?? null };
        setResults((prev) => [...prev, ...(page.models as SearchedEntry[K][])]);
        setBusy(false);
      })
      .catch(() => {
        if (seq !== latest.current) return;
        setBusy(false);
      });
  }, [kind]);

  // The chosen model always appears in the list, even when it does not match
  // what is typed — a control that cannot show its own value reads as broken.
  // When it is out-of-band it gets a heading of its own, so a row that plainly
  // does not match the query is not read as a result that does.
  const pinned = entry && !results.some((r) => r.model === entry.model) ? entry : null;

  return (
    <Select
      value={value ?? ""}
      valueLabel={value}
      onSearch={search}
      busy={busy}
      onScrollEnd={loadMore}
      aria-label={ariaLabel}
      popupClassName="max-w-[440px]"
      onChange={(e) => {
        const picked = [pinned, ...results].find((o) => o?.model === e.target.value);
        if (picked) onChange(picked);
      }}
    >
      {pinned && (
        <optgroup label="Selected">
          <option value={pinned.model} data-detail={detail(pinned)} data-logo-url={pinned.logo_url ?? undefined}>
            {pinned.label || pinned.model}
          </option>
        </optgroup>
      )}
      {results.map((option) => (
        <option
          key={option.model}
          value={option.model}
          data-detail={detail(option)}
          data-logo-url={option.logo_url ?? undefined}
        >
          {option.label || option.model}
        </option>
      ))}
    </Select>
  );
}

/** What every speech model at a searched provider makes the author know. */
export function SearchedProviderNote({
  catalog,
  provider,
}: {
  catalog: CatalogResponse;
  provider?: string | null;
}) {
  if (!isSearchedProvider(catalog, provider, "stt")) return null;
  const label = catalog.providers?.[provider ?? ""]?.label ?? provider;
  /* `/audio/transcriptions` ignores the routing block that carries
     `data_collection: "deny"`, so the only thing that keeps caller audio off a
     host that trains on it is the workspace's own OpenRouter setting. */
  return (
    <>
      <p className="mt-3 text-[13px] leading-5 text-muted">
        All STT models exposed through {label} are batch STT and would have higher latency
        compared to streaming STT models.
      </p>
      <p className="mt-2 text-[13px] leading-5 text-muted">
        Caller audio may reach hosts that train on it &mdash; turn training off in your{" "}
        <a
          href="https://openrouter.ai/settings/privacy"
          target="_blank"
          rel="noreferrer"
          className="font-medium text-ink underline underline-offset-2"
        >
          {label} privacy settings
        </a>
        .
      </p>
    </>
  );
}
