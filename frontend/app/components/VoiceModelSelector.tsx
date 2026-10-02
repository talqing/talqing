"use client";
import type { CSSProperties } from "react";
import { Select, STAGE_GRID } from "./ui";
import { cn } from "@/lib/cn";
import { VoicePicker, type VoiceSettings } from "./VoicePicker";

// The voice-model widget: provider and model dropdowns stacked over the shared
// voice picker. Changing either re-points the picker, which self-fetches that
// provider/model's voices and filter metadata.
//
// Serves both kinds of model that own a voice — a text-to-speech model, and a
// speech-to-speech model that speaks for itself. `kind` is what tells the picker
// which roster to fetch; everything else on screen is identical.

export function VoiceModelSelector({
  provider,
  model,
  kind = "tts",
  value,
  valueName,
  onSelect,
  withVoiceSettings,
  language,
  speed,
  supportsSpeed,
  speedMin,
  speedMax,
  onSpeed,
  providerOptions,
  modelOptions,
  onProvider,
  onModel,
}: {
  provider: string;
  model: string;
  kind?: "tts" | "realtime";
  value: string | null | undefined;
  valueName?: string | null;
  onSelect: (
    id: string,
    name: string | null,
    language: string | null,
    settings: VoiceSettings | null,
  ) => void;
  /** Passed through — see VoicePicker. */
  withVoiceSettings?: boolean;
  /** The agent's language, in this model's own spelling. Seeds the voice
   *  gallery's filter so it opens on voices that can speak it. */
  language?: string | null;
  speed: number;
  supportsSpeed: boolean;
  speedMin: number;
  speedMax: number;
  onSpeed: (speed: number) => void;
  providerOptions: { value: string; label: string; logoUrl?: string }[];
  modelOptions: { value: string; label: string }[];
  onProvider: (provider: string) => void;
  onModel: (model: string) => void;
}) {
  const minSpeed = Number.isFinite(speedMin) ? speedMin : 0.5;
  const maxSpeed = Number.isFinite(speedMax) ? speedMax : 2;
  const rawSpeed = Number.isFinite(speed) ? speed : 1;
  const displaySpeed = Math.max(minSpeed, Math.min(maxSpeed, rawSpeed));
  const speedPct = ((displaySpeed - minSpeed) / Math.max(0.01, maxSpeed - minSpeed)) * 100;

  return (
    <div className="flex flex-col gap-3">
      <div className={STAGE_GRID}>
        <div className="flex min-w-0 flex-col gap-2">
          <label className="text-[14px] font-semibold leading-5 text-ink">Provider</label>
          <Select
            value={provider}
            onChange={(e) => onProvider(e.target.value)}
            className="[&>button]:min-h-10 [&>button]:rounded-[10px] [&>button]:border-line-2 [&>button]:bg-white [&>button]:text-[14px] [&>button]:shadow-none"
          >
            {providerOptions.map((o) => (
              <option key={o.value} value={o.value} data-logo-url={o.logoUrl || ""}>{o.label}</option>
            ))}
          </Select>
        </div>
        <div className="flex min-w-0 flex-col gap-2">
          <label className="text-[14px] font-semibold leading-5 text-ink">Model</label>
          <Select
            value={model}
            onChange={(e) => onModel(e.target.value)}
            className="[&>button]:min-h-10 [&>button]:rounded-[10px] [&>button]:border-line-2 [&>button]:bg-white [&>button]:text-[14px] [&>button]:shadow-none"
          >
            {modelOptions.map((o) => (
              <option key={o.value} value={o.value}>{o.label}</option>
            ))}
          </Select>
        </div>
      </div>
      {/* Inside the same grid, spanning Provider + Model: on its own it ran the
          full width of the card for a value as short as one name. */}
      <div className={cn(STAGE_GRID, "gap-y-0")}>
        <div className="flex min-w-0 flex-col gap-2 md:col-span-2">
          <label className="text-[14px] font-semibold leading-5 text-ink">Voice</label>
          <VoicePicker
            provider={provider}
            providerLabel={providerOptions.find((o) => o.value === provider)?.label}
            model={model}
            kind={kind}
            value={value}
            valueName={valueName}
            onSelect={onSelect}
            withVoiceSettings={withVoiceSettings}
            initialLanguage={language}
          />
        </div>
      </div>
      {supportsSpeed && (
        // Capped, not full-bleed: the track is a 0.5–2.0 range, and stretched
        // across a wide card it puts the readout an inch from the label it
        // belongs to.
        <div className="mt-3 flex max-w-[420px] flex-col gap-2">
          <div className="flex items-baseline justify-between gap-2.5">
            <label htmlFor="voice-speed" className="text-[14px] font-semibold leading-5 text-ink">Speed</label>
            <span className="font-mono text-[12px] tabular-nums text-muted">{displaySpeed.toFixed(1)}×</span>
          </div>
          <input
            id="voice-speed"
            className="range-neutral w-full"
            type="range"
            min={minSpeed}
            max={maxSpeed}
            step={0.1}
            value={displaySpeed}
            style={{ "--range-pct": `${speedPct}%` } as CSSProperties}
            onChange={(e) => onSpeed(Number(e.target.value))}
            aria-label="Speech speed"
          />
        </div>
      )}
    </div>
  );
}
