"use client";
import type { RecordingSpec } from "@talqing/sdk";
import { cn } from "@/lib/cn";
import { BoxCheckbox, Input, Label } from "../../components/ui";
import { ToggleRow } from "./ToggleRow";

/* The token that says *where* in the greeting the notice is spoken. Publish
   rejects a disclosing agent whose greeting does not contain it, so this section
   inserts it rather than letting the user meet that rule as an error — a setting
   that silently invalidates a field two sections away is the worst version of
   this feature. Which is also why the consent controls live under the greeting
   itself rather than down in Privacy: they edit that field. */
export const CONSENT_TOKEN = "{{consent.notice}}";

/* Mirrors compiler.compile.personalize_greeting: substitute, then collapse the
   whitespace the empty token leaves behind. Kept in step so the preview is what
   the caller actually hears, not an approximation of it. */
function previewGreeting(greeting: string, recording: RecordingSpec): string {
  const notice = recording.consent === "disclosure" ? (recording.consent_notice ?? "") : "";
  return greeting.replaceAll(CONSENT_TOKEN, notice).split(/\s+/).filter(Boolean).join(" ");
}

/* Put the token after the first sentence — where a disclosure reads naturally,
   between the hello and the offer of help — or at the end when there is no
   sentence break to find. */
function withConsentToken(greeting: string): string {
  if (greeting.includes(CONSENT_TOKEN)) return greeting;
  if (!greeting.trim()) return CONSENT_TOKEN;
  const match = greeting.match(/^.*?[.!?]\s+/);
  if (!match) return `${greeting.trimEnd()} ${CONSENT_TOKEN}`;
  return `${match[0]}${CONSENT_TOKEN} ${greeting.slice(match[0].length)}`;
}

/** Strip the token and tidy the space it leaves. Called when recording goes off:
    an orphaned {{consent.notice}} would sit in the greeting resolving to nothing
    for the rest of the agent's life. */
function withoutConsentToken(greeting: string): string {
  return greeting.replaceAll(CONSENT_TOKEN, " ").split(/\s+/).filter(Boolean).join(" ");
}

/** Recording and its notice, shown under the greeting they edit. */
export function RecordingFields({
  recording,
  greeting,
  onChange,
  onGreetingChange,
}: {
  recording: RecordingSpec;
  greeting: string;
  onChange: (patch: Partial<RecordingSpec>) => void;
  onGreetingChange: (next: string) => void;
}) {
  const enabled = recording.enabled ?? true;
  const disclosing = enabled && recording.consent === "disclosure";
  const tokenMissing = disclosing && !greeting.includes(CONSENT_TOKEN);

  function toggleConsent(next: boolean) {
    if (next) {
      onChange({ consent: "disclosure" });
      onGreetingChange(withConsentToken(greeting));
    } else {
      onChange({ consent: "off" });
      onGreetingChange(withoutConsentToken(greeting));
    }
  }

  function toggleRecording(next: boolean) {
    // Nothing is being recorded, so the notice would announce a recording that
    // is not happening — take it out of the greeting along with it.
    const dropping = !next && recording.consent === "disclosure";
    onChange(dropping ? { enabled: next, consent: "off" } : { enabled: next });
    if (dropping) onGreetingChange(withoutConsentToken(greeting));
  }

  return (
    /* The card's own bottom border closes the last row, so the rows are equal
       bands: 12px of padding either side of every label. Left with the card's
       padding below it, the last row would carry 28px under its label and 12
       above, and sit visibly high. The padding comes back when the notice
       fields render underneath and need it. (-mb-4, not -mb-5: SectionCard's
       body pads 16px, where the old Panel padded 20.) */
    <div className={cn("flex flex-col border-t border-line", !disclosing && "-mb-4")}>
      <ToggleRow
        checked={enabled}
        onChange={toggleRecording}
        label="Record calls"
        help="Saves the audio of every call so you can play it back later. Caller and agent land on separate channels. It is what the agent heard — after noise cancellation, and without the background audio bed. How long recordings are kept is set for the whole organization, under Settings."
      />

      {enabled && (
        <ToggleRow
          checked={disclosing}
          onChange={toggleConsent}
          label="Consent"
          help="Tells the caller they are being recorded, as part of the greeting. Several countries require this. The agent can also stop recording if the caller objects."
        />
      )}

      {disclosing && (
        <div className="flex flex-col gap-2.5 pt-3.5">
          <div className="flex flex-col gap-2">
            <Label htmlFor="consent-notice">Notice</Label>
            <Input
              id="consent-notice"
              value={recording.consent_notice ?? ""}
              onChange={(e) => onChange({ consent_notice: e.target.value })}
            />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label>What the caller hears</Label>
            <p className="rounded-lg border border-line bg-subtle px-3 py-2 text-[13px] leading-5 text-ink">
              {previewGreeting(greeting, recording) || "(no greeting)"}
            </p>
            <p className="text-[13px] leading-5 text-muted">
              Move{" "}
              <code className="rounded bg-subtle px-1 py-0.5 font-mono text-[11px]">{CONSENT_TOKEN}</code>{" "}
              inside the greeting to change where the notice lands.
            </p>
          </div>
          {tokenMissing && (
            <p className="rounded-lg border border-danger/25 bg-danger/[0.05] px-3 py-2 text-[12.5px] leading-5 text-danger">
              The greeting no longer contains {CONSENT_TOKEN}, so the caller would never hear the
              notice. Publishing is blocked until it is back.
            </p>
          )}
        </div>
      )}
    </div>
  );
}
