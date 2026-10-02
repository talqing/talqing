"use client";
import type { AgentConfig } from "@talqing/sdk";
import { cn } from "@/lib/cn";
import { BoxCheckbox, HelpDot } from "../../components/ui";
import { CONSENT_TOKEN } from "./PrivacySection";

/** Mirrors `CompiledAgent._greeting_interruptible`: the greeting carries the
    recording notice, so the caller cannot cut it off whatever the setting says. */
export function greetingSpeaksNotice(config: AgentConfig): boolean {
  const recording = config.recording ?? {};
  return (
    Boolean(recording.enabled) &&
    recording.consent === "disclosure" &&
    Boolean(recording.consent_notice?.trim()) &&
    (config.greeting ?? "").includes(CONSENT_TOKEN)
  );
}

/** The one switch on the greeting. Shown locked while the greeting speaks the
    recording notice, rather than hidden: the Consent row right below is what
    locked it, and the author should see the two are connected. */
export function GreetingInterruptible({
  checked,
  speaksNotice,
  onChange,
}: {
  checked: boolean;
  speaksNotice: boolean;
  onChange: (next: boolean) => void;
}) {
  const label = "Caller can interrupt the greeting";
  return (
    <div className="flex flex-col gap-1 pt-1">
      <div className="flex items-center gap-2.5">
        <BoxCheckbox
          checked={checked && !speaksNotice}
          onChange={onChange}
          disabled={speaksNotice}
          ariaLabel={label}
        />
        <button
          type="button"
          disabled={speaksNotice}
          onClick={() => onChange(!checked)}
          className={cn(
            "text-left text-[13px] font-medium leading-5",
            speaksNotice ? "cursor-not-allowed text-muted" : "text-ink",
          )}
        >
          {label}
        </button>
        <HelpDot label="Off, the agent finishes its opening line even if the caller talks over it." />
      </div>
      {speaksNotice && (
        <p className="pl-[30px] text-[13px] leading-5 text-muted">
          It carries the recording notice, so it always plays in full.
        </p>
      )}
    </div>
  );
}
