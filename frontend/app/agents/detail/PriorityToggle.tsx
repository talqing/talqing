"use client";

import type { CatalogResponse, LLMCatalogEntryResponse } from "@talqing/sdk";

import { providerDisplayLabel } from "./agentConfig";
import { ToggleRow } from "./ToggleRow";

/** Whether one language model runs in its provider's priority lane.
 *
 *  Which models sell one is the catalog's business, not ours — and support is
 *  not even uniform inside a vendor (OpenAI sells no lane on gpt-5.4-nano), so
 *  a model without one shows no control rather than a switch that cannot be
 *  used. Same rule as the thinking select next to it.
 *
 *  The help text quotes this exact model's premium rather than a range: the
 *  multiplier runs from 1.75x to 2.5x across the catalog, so a generic "costs
 *  more" would be the one thing an author actually needs to know, left vague. */
export function PriorityToggle({
  catalog,
  entry,
  value,
  onChange,
}: {
  catalog: CatalogResponse;
  entry: LLMCatalogEntryResponse | undefined;
  value: boolean | undefined;
  onChange: (priority: boolean) => void;
}) {
  if (!entry?.priority) return null;
  const provider = providerDisplayLabel(catalog, entry.provider);

  return (
    <ToggleRow
      checked={value === true}
      onChange={onChange}
      label="Priority processing"
      help={`Asks ${provider} to schedule this model's turns ahead of standard traffic. It mostly steadies the worst case rather than speeding up the average - it cuts the occasional long pause before the agent speaks, which on a phone call is silence the caller hears.${premiumNote(entry)} The lane is best-effort: under load a provider may serve a turn at standard speed anyway.`}
    />
  );
}

/** "Tokens cost 2x while it is on." — computed from the two rate blocks so it
 *  cannot drift from what billing actually charges. Falls back to naming no
 *  figure at all rather than guessing one, if either rate is missing. */
function premiumNote(entry: LLMCatalogEntryResponse): string {
  const standard = entry.pricing?.output_per_1m;
  const priority = entry.priority?.pricing?.output_per_1m;
  if (typeof standard !== "number" || typeof priority !== "number" || !standard) {
    return " Tokens cost more while it is on, billed through to you at the provider's rate.";
  }
  const multiple = priority / standard;
  // 2 -> "2x", 1.75 -> "1.75x"
  const shown = Number.isInteger(multiple) ? String(multiple) : multiple.toFixed(2).replace(/0$/, "");
  return ` Tokens cost ${shown}x while it is on, billed through to you.`;
}
