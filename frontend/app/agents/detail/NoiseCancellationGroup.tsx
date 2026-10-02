"use client";
import type { CSSProperties } from "react";
import type { CatalogResponse } from "@talqing/sdk";
import { BoxCheckbox, Select } from "@/app/components/ui";
import {
  ENHANCEMENT_LEVEL_MARKS,
  entriesForChannel,
  providerDisplayLabel,
  providerOptionsFor,
  type Channel,
  type NoiseCancellation,
} from "./agentConfig";
import { TuningGroup } from "./TurnHandlingSection";

const HELP =
  "Cleans up the caller's audio before anything else hears it — on web calls and on the phone " +
  "alike. The speech-to-text model, the turn detection and the call recording all get the " +
  "cleaned audio, so the recording is what the agent actually heard. Runs on the provider's " +
  "own key: add one under BYOK before publishing.";

/* This group sits among the turn-handling groups in the same card, so its field
   labels are the ones those groups use rather than the heavier form label the
   Models card wants. */
const FIELD_LABEL = "text-[13px] font-medium leading-4 text-ink-soft";

/** The nearest documented level to a slider position, so the readout can say
    what the number means instead of only what it is. */
function nearestMark(level: number) {
  return ENHANCEMENT_LEVEL_MARKS.reduce((best, mark) =>
    Math.abs(mark.value - level) < Math.abs(best.value - level) ? mark : best,
  );
}

export function NoiseCancellationGroup({
  catalog,
  channel,
  value,
  onChange,
}: {
  catalog: CatalogResponse;
  channel: Channel;
  value: NoiseCancellation;
  onChange: (patch: Partial<NoiseCancellation>) => void;
}) {
  const entries = entriesForChannel(catalog.noise_cancellation ?? [], channel);
  // No enhancement models for this channel means nothing to offer and nothing
  // to explain — better an absent group than a switch that cannot be honoured.
  if (!entries.length) return null;

  const selected =
    entries.find((entry) => entry.provider === value.provider && entry.model === value.model) ??
    entries.find((entry) => entry.provider === value.provider) ??
    entries[0];
  const providers = providerOptionsFor(catalog, entries);
  const models = entries.filter((entry) => entry.provider === selected.provider);
  const mark = nearestMark(value.enhancement_level);

  // A provider's models are its own, so switching provider has to land on one of
  // them rather than keep a model id the new provider has never heard of.
  function pickProvider(provider: string) {
    const first = entries.find((entry) => entry.provider === provider);
    if (first) onChange({ provider: first.provider, model: first.model });
  }

  return (
    <TuningGroup
      title="Noise cancellation"
      help={HELP}
      muted={!value.enabled}
      toggle={
        <div className="flex items-center gap-2 text-[13px] text-ink-soft">
          <BoxCheckbox
            checked={value.enabled}
            onChange={(enabled) => onChange({ enabled })}
            ariaLabel="Noise cancellation enabled"
          />
          <button type="button" className="text-left" onClick={() => onChange({ enabled: !value.enabled })}>
            Enabled
          </button>
        </div>
      }
    >
      <div className="flex flex-wrap items-start gap-x-7 gap-y-5">
        <div className="flex min-w-0 flex-col gap-2">
          <label className={FIELD_LABEL}>Provider</label>
          <Select
            className="max-w-[200px]"
            aria-label="Noise cancellation provider"
            value={selected.provider}
            onChange={(e) => pickProvider(e.target.value)}
          >
            {providers.map((provider) => (
              <option key={provider.value} value={provider.value} data-logo-url={provider.logoUrl}>
                {provider.label}
              </option>
            ))}
          </Select>
        </div>

        <div className="flex min-w-0 flex-col gap-2">
          <label className={FIELD_LABEL}>Model</label>
          <Select
            className="max-w-[260px]"
            aria-label="Noise cancellation model"
            value={selected.model}
            onChange={(e) => onChange({ model: e.target.value })}
          >
            {models.map((entry) => (
              <option key={entry.model} value={entry.model} title={entry.description}>
                {entry.label || entry.model}
              </option>
            ))}
          </Select>
          <p className="max-w-[260px] text-[12.5px] leading-5 text-muted">{selected.description}</p>
        </div>

        {/* Capped rather than full-bleed, for the reason the speech-rate slider
            is: stretched across the card the readout drifts away from its label. */}
        <div className="flex w-full max-w-[340px] flex-col gap-2">
          <div className="flex items-baseline justify-between gap-2.5">
            <label htmlFor="noise-cancellation-level" className={FIELD_LABEL}>Strength</label>
            <span className="font-mono text-[12px] tabular-nums text-muted">
              {value.enhancement_level.toFixed(2)} · {mark.label}
            </span>
          </div>
          {/* The track is a few pixels tall next to a 40px select. Centring it in
              a select-sized slot is what puts the two help lines below on one
              baseline instead of a step. */}
          <span className="flex h-10 items-center">
            <input
              id="noise-cancellation-level"
              className="range-neutral w-full"
              type="range"
              min={0}
              max={1}
              step={0.05}
              value={value.enhancement_level}
              style={{ "--range-pct": `${value.enhancement_level * 100}%` } as CSSProperties}
              onChange={(e) => onChange({ enhancement_level: Number(e.target.value) })}
              aria-label="Enhancement strength"
            />
          </span>
          <p className="text-[12.5px] leading-5 text-muted">{mark.help}</p>
        </div>
      </div>

      <p className="mt-3.5 text-[12.5px] leading-5 text-muted">
        {providerDisplayLabel(catalog, selected.provider)} bills this on your own key, not through
        Talqing. Enhancement is lossy: turn it up for a noisy line, leave it low for a quiet one.
      </p>
    </TuningGroup>
  );
}
