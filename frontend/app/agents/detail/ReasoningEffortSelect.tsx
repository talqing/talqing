"use client";

import type { LLMCatalogEntryResponse, ReasoningEffort } from "@talqing/sdk";

import { Label, Select } from "../../components/ui";

/** How hard one language model may think before it answers.
 *
 *  Which values exist is the model's business, not ours: the catalog entry lists
 *  what that exact model accepts, in fastest-first order, and the first one is
 *  what an agent runs at until its author picks another. A model offering one
 *  value (or none) has nothing to ask about, so the control disappears rather
 *  than showing a select that cannot be changed. */
export function ReasoningEffortSelect({
  entry,
  value,
  onChange,
}: {
  entry: LLMCatalogEntryResponse | undefined;
  value: ReasoningEffort | null | undefined;
  onChange: (effort: ReasoningEffort) => void;
}) {
  const efforts = entry?.reasoning_efforts ?? [];
  if (efforts.length < 2) return null;

  return (
    <div className="flex min-w-0 flex-col gap-2">
      <Label>Thinking</Label>
      <Select
        value={value ?? efforts[0]}
        onChange={(e) => onChange(e.target.value as ReasoningEffort)}
      >
        {efforts.map((effort) => (
          <option key={effort} value={effort}>
            {EFFORT_LABELS[effort]}
          </option>
        ))}
      </Select>
    </div>
  );
}

/** Sentence case, and deliberately the provider's own words: an author reading
 *  a model's documentation should find the same four names there. */
const EFFORT_LABELS: Record<ReasoningEffort, string> = {
  none: "None",
  minimal: "Minimal",
  low: "Low",
  medium: "Medium",
  high: "High",
};
