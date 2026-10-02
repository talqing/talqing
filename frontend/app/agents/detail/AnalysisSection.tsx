"use client";
import type { AgentConfig, AnalysisField, AnalysisSpec, CatalogResponse, LLMCatalogEntryResponse, LLMModelSpec, CatalogEntry } from "@talqing/sdk";
import { cn } from "@/lib/cn";
import { BoxCheckbox, Button, CardHead, HelpDot, Input, Label, LIFT_ON_HOVER, MODEL_HOSTS_GRID, Panel, Select, Textarea } from "../../components/ui";
import { ToggleRow } from "./ToggleRow";
import { ReasoningEffortSelect } from "./ReasoningEffortSelect";
import {
  catalogEntryLabel,
  isSearchedProvider,
  providerOptionsFor,
  searchedProviders,
} from "./agentConfig";
import { HostNote, HostSelect, useModelHosts } from "./HostSelect";
import { SearchedModelSelect } from "./ModelSearch";

/* Only provider/model reach the API — a catalog entry carries display fields
   (label, logo) that are not part of the agent definition. */
function specOf(entry: CatalogEntry): LLMModelSpec {
  return { provider: entry.provider, model: entry.model };
}

const FIELD_TYPES: AnalysisField["type"][] = ["string", "boolean", "integer", "number"];

/* Field names address a value in the API, the webhook and the extracted-data
   table, so the API rejects anything else. Normalising as the user types beats
   letting them discover the rule as a save error on a field they finished with
   two minutes ago. */
function toIdentifier(raw: string): string {
  return raw
    .toLowerCase()
    .replace(/[^a-z0-9_]+/g, "_")
    .replace(/^[^a-z]+/, "")
    .slice(0, 40);
}

const MAX_FIELDS = 25;

export function AnalysisSection({
  session,
  analysis,
  config,
  catalog,
  onChange,
  onRemember,
}: {
  /** What one session of this agent is called: analysis runs when it ends. */
  session: "call" | "chat";
  analysis: AnalysisSpec;
  /** The whole draft: the default analysis model is the agent's own LLM. */
  config: AgentConfig;
  catalog: CatalogResponse;
  onChange: (patch: Partial<AnalysisSpec>) => void;
  /** Keeps a model picked from a searched provider in the catalog the editor
   *  hands down, so the thinking control beside it has an entry to read. */
  onRemember: (entry: LLMCatalogEntryResponse) => void;
}) {
  const enabled = analysis.enabled ?? true;
  const summary = analysis.summary ?? true;
  const fields = analysis.fields ?? [];
  const judging = analysis.outcome != null;
  const model = analysis.model ?? null;

  // Every LLM in the catalog is eligible — unlike the pipeline slots there is no
  // channel to filter on, because this reads a finished transcript.
  const modelEntries = catalog.llm ?? [];
  const providers = providerOptionsFor(catalog, modelEntries, searchedProviders(catalog, "llm"));

  /* Mirrors services.analysis.resolve_model: the agent's own LLM, or — for a
     realtime agent, which has none — the first catalog LLM from that same
     provider. Never a platform-wide default, so turning analysis on can never
     demand a provider key the agent did not already need. */
  const inherited =
    modelEntries.find(
      (entry) => entry.provider === config.llm?.provider && entry.model === config.llm?.model,
    ) ?? modelEntries.find((entry) => entry.provider === config.realtime?.provider);
  const effective = model
    ? (modelEntries.find(
        (entry) => entry.provider === model.provider && entry.model === model.model,
      ) ?? model)
    : inherited;
  const providerModels = modelEntries.filter((entry) => entry.provider === effective?.provider);
  // `effective` falls back to the pinned spec itself when that names a model the
  // catalog no longer has; only a real entry can say what efforts it accepts.
  const effectiveEntry = modelEntries.find(
    (entry) => entry.provider === effective?.provider && entry.model === effective?.model,
  );
  /* The spec analysis runs on today. Following the agent's own LLM, that
     includes its hosts — analysis inherits them, since a region restriction is
     exactly what it must not escape — so a pin made from here starts from them
     rather than quietly widening to any host. */
  const effectiveSpec: LLMModelSpec | null =
    model ??
    (inherited
      ? {
          provider: inherited.provider,
          model: inherited.model,
          hosts: inherited.provider === config.llm?.provider ? config.llm?.hosts : null,
        }
      : null);
  const hosts = useModelHosts(catalog, effectiveSpec);

  /* Every output switched off is a save error, not a saved state — the agent
     would pay for an LLM call that produces nothing. Shown here rather than
     waiting for the publish check, because the toggle that caused it is
     right there. */
  const producesNothing = enabled && !summary && !judging && fields.length === 0;

  function patchField(index: number, patch: Partial<AnalysisField>) {
    onChange({ fields: fields.map((f, i) => (i === index ? { ...f, ...patch } : f)) });
  }

  return (
    <Panel className={cn(LIFT_ON_HOVER, "grid gap-3.5")}>
      {/* The description carries what a help dot used to: every other card on
          this page states its job under its title, and this one read a line
          shorter than all of them. */}
      <CardHead
        title={session === "chat" ? "Chat analysis" : "Call analysis"}
        desc={`What a model works out from the transcript once the ${session} ends`}
        className="mb-0"
      >
        <div className="flex items-center gap-2 text-[13px] text-ink-soft">
          <BoxCheckbox
            checked={enabled}
            onChange={(next) => onChange({ enabled: next })}
            ariaLabel={`Analyse ${session}s after they end`}
          />
          <button type="button" onClick={() => onChange({ enabled: !enabled })}>
            Enabled
          </button>
        </div>
      </CardHead>

      {enabled && (
        <>
          {/* Flush to the head's rule and closed by its own, the way the
              recording rows are. Left to the panel's gap, the band above the
              first row and below the last would be 14px wider than the rule
              between them, and each label would sit off-centre in its row. */}
          <div className="-mt-3.5 flex flex-col border-b border-line">
            <ToggleRow
              checked={summary}
              onChange={(next) => onChange({ summary: next })}
              label={`Summarize ${session}`}
              help={`Adds a two-line summary to each ${session}, so you can scan the list without opening anything.`}
            />
            <ToggleRow
              checked={judging}
              onChange={(next) => onChange({ outcome: next ? { prompt: "" } : null })}
              label="Judge the outcome"
              help={
                session === "chat"
                  ? "Marks each chat success, failure or unknown against the definition you write."
                  : "Marks each call success, failure or unknown against the definition you write, and lets the calls list be filtered by it."
              }
            />
          </div>

          {judging && (
            <Textarea
              id="analysis-outcome"
              aria-label={`What a successful ${session} looks like`}
              rows={2}
              placeholder="Success is: the person ended up with a confirmed appointment slot."
              value={analysis.outcome?.prompt ?? ""}
              onChange={(e) => onChange({ outcome: { prompt: e.target.value } })}
            />
          )}

          <div className="flex flex-col gap-2">
            <div className="flex items-center gap-2">
              <Label>Structured output</Label>
              <HelpDot
                label={`Values to pull out of each ${session} and send to your systems. These are worked out from the transcript afterwards, so they never overwrite what a tool confirmed during the ${session}.`}
              />
              <span className="ml-auto text-[12px] tabular-nums text-faint">
                {fields.length}/{MAX_FIELDS}
              </span>
            </div>

            {fields.length > 0 && (
              <div className="flex flex-col gap-2">
                {fields.map((field, index) => (
                  // One row per field: name, type, what to look for, remove. The
                  // description was a full-width textarea under each row, which
                  // made five fields taller than the rest of the section.
                  <div key={index} className="flex items-center gap-2">
                    <Input
                      aria-label="Field name"
                      className="w-[184px] flex-none font-mono text-[12.5px]"
                      placeholder="appointment_date"
                      value={field.name}
                      onChange={(e) => patchField(index, { name: toIdentifier(e.target.value) })}
                    />
                    <Select
                      aria-label="Field type"
                      className="w-[112px] flex-none"
                      value={field.type ?? "string"}
                      onChange={(e) =>
                        patchField(index, { type: e.target.value as AnalysisField["type"] })
                      }
                    >
                      {FIELD_TYPES.map((t) => (
                        <option key={t} value={t}>
                          {t}
                        </option>
                      ))}
                    </Select>
                    <Input
                      aria-label="How to find this value"
                      className="min-w-0 flex-1"
                      placeholder="The booked date as YYYY-MM-DD, or nothing if no appointment was made."
                      value={field.description}
                      onChange={(e) => patchField(index, { description: e.target.value })}
                    />
                    <Button
                      variant="ghost"
                      size="sm"
                      className="flex-none"
                      onClick={() => onChange({ fields: fields.filter((_, i) => i !== index) })}
                      aria-label={`Remove ${field.name || "field"}`}
                    >
                      Remove
                    </Button>
                  </div>
                ))}
              </div>
            )}

            <div>
              <Button
                variant="secondary"
                size="sm"
                disabled={fields.length >= MAX_FIELDS}
                onClick={() =>
                  onChange({ fields: [...fields, { name: "", type: "string", description: "" }] })
                }
              >
                Add a field
              </Button>
            </div>
          </div>

          <div className="flex flex-col gap-2 border-t border-line pt-3.5">
            <div className="flex items-baseline justify-between gap-3">
              {/* Not "Model": it sits directly above a field also called Model,
                  at the same weight, and the two read as a stutter. */}
              <Label>Analysis model</Label>
              {/* Only once the author has pinned one — otherwise this row looks
                  exactly like the pipeline pickers above, which is the point.
                  Without it, choosing a model would be a one-way door: there
                  would be no way back to following the agent. */}
              {model && (
                <button
                  type="button"
                  className="shrink-0 text-[13px] font-semibold text-muted underline underline-offset-2 hover:text-ink"
                  onClick={() => onChange({ model: null })}
                >
                  Match the agent&rsquo;s model
                </button>
              )}
            </div>

            {/* Shows the model that will actually run — the agent's own until
                the author picks another. Touching either select pins the choice;
                left alone, `model` stays null and analysis keeps following the
                agent's LLM, so the two can never drift apart by accident. */}
            <div
              className={
                isSearchedProvider(catalog, effective?.provider, "llm")
                  ? MODEL_HOSTS_GRID
                  : "grid grid-cols-1 gap-3.5 md:grid-cols-[minmax(170px,220px)_minmax(220px,1fr)] lg:grid-cols-[minmax(170px,220px)_minmax(220px,1fr)_minmax(150px,180px)]"
              }
            >
              <div className="flex min-w-0 flex-col gap-2">
                <Label>Provider</Label>
                <Select
                  value={effective?.provider ?? ""}
                  onChange={(e) => {
                    const entry = modelEntries.find((item) => item.provider === e.target.value);
                    if (entry) onChange({ model: specOf(entry) });
                    // A searched provider contributes no entries, so there is
                    // nothing to preselect and the model picker beside this is
                    // where the choice gets made.
                    else if (isSearchedProvider(catalog, e.target.value, "llm"))
                      onChange({ model: { provider: e.target.value, model: "", hosts: null } });
                  }}
                >
                  {providers.map((provider) => (
                    <option
                      key={provider.value}
                      value={provider.value}
                      data-logo-url={provider.logoUrl}
                    >
                      {provider.label}
                    </option>
                  ))}
                </Select>
              </div>
              <div className="flex min-w-0 flex-col gap-2">
                <Label>Model</Label>
                {isSearchedProvider(catalog, effective?.provider, "llm") ? (
                  <SearchedModelSelect
                    kind="llm"
                    entry={effectiveEntry}
                    value={effective?.model}
                    onChange={(entry) => {
                      onRemember(entry);
                      onChange({ model: specOf(entry) });
                    }}
                    ariaLabel="Analysis model"
                  />
                ) : (
                  <Select
                    value={effective?.model ?? ""}
                    onChange={(e) => {
                      const entry = providerModels.find((item) => item.model === e.target.value);
                      if (entry) onChange({ model: specOf(entry) });
                    }}
                  >
                    {providerModels.map((entry) => (
                      <option key={entry.model} value={entry.model}>
                        {catalogEntryLabel(entry)}
                      </option>
                    ))}
                  </Select>
                )}
              </div>
              {/* Analysis is the one model call nobody waits on, so thinking
                  here costs tokens and no latency at all. Choosing it pins the
                  model, like the two selects beside it. */}
              <ReasoningEffortSelect
                entry={effectiveEntry}
                value={model?.reasoning_effort}
                onChange={(reasoning_effort) =>
                  effectiveSpec && onChange({ model: { ...effectiveSpec, reasoning_effort } })
                }
              />
              <HostSelect
                catalog={catalog}
                spec={effectiveSpec}
                hosts={hosts}
                ariaLabel="Analysis hosts"
                onChange={(next) => effectiveSpec && onChange({ model: { ...effectiveSpec, hosts: next } })}
              />
            </div>
            <HostNote
              value={effectiveSpec?.hosts}
              hosts={hosts}
              subject="the analysis"
              failover="impossible"
            />
          </div>

          {producesNothing && (
            <p className="rounded-lg border border-danger/25 bg-danger/[0.05] px-3 py-2 text-[12.5px] leading-5 text-danger">
              Analysis is on but nothing is switched on to produce, so every {session} would pay for
              a model call that returns nothing. Turn on the summary, say what a successful {session}{" "}
              is, or add a field.
            </p>
          )}
        </>
      )}
    </Panel>
  );
}
