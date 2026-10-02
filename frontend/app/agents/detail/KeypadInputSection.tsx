"use client";

import { cn } from "@/lib/cn";
import { CardHead, Field, Input, LIFT_ON_HOVER, Panel, Select } from "../../components/ui";
import { ToggleRow } from "./ToggleRow";

/** How much quiet ends a keypad entry, and what the choices actually mean.
 *
 *  A select rather than a number field: the useful range is small, every value
 *  in it is a judgement about a caller rather than a measurement, and "0" needs
 *  a sentence that a spinner has nowhere to put. */
const TIMEOUTS: readonly { value: number; label: string }[] = [
  { value: 1, label: "1 second — quick menu choices" },
  { value: 2, label: "2 seconds — the usual" },
  { value: 4, label: "4 seconds — long numbers, unhurried callers" },
  { value: 0, label: "Never — wait for the terminator key" },
];

const TERMINATORS: readonly { value: "#" | "*" | ""; label: string }[] = [
  { value: "#", label: "# (hash)" },
  { value: "*", label: "* (star)" },
  { value: "", label: "None — only the quiet timer ends an entry" },
];

/** Digits the caller types, delivered as a turn the agent can answer.
 *
 *  Voice agents only — the server clears the setting on every other channel, so
 *  this card is not rendered there. A browser has no keypad, and only a voice
 *  agent can hold a phone number or answer a media stream.
 *
 *  Beside Vision input rather than inside Tuning, and for the same reason those
 *  two are siblings on the server: both are "a second way input reaches the
 *  agent", where Tuning is about how the agent reads the input it already has.
 *
 *  The one thing worth saying out loud in the UI is what keypad DTMF is not: it
 *  is an event from the carrier, never a tone found in the audio. "I pressed 1
 *  and nothing happened" on a carrier that only sends in-band tones is
 *  otherwise unexplainable, and the person who needs that sentence is reading
 *  this card. */
export function KeypadInputSection({
  enabled,
  timeout,
  terminator,
  onEnabled,
  onTimeout,
  onTerminator,
}: {
  enabled: boolean;
  timeout: number;
  terminator: "#" | "*" | "";
  onEnabled: (enabled: boolean) => void;
  onTimeout: (timeout: number) => void;
  onTerminator: (terminator: "#" | "*" | "") => void;
}) {
  /* The combination the server refuses at publish, stated here at the moment it
     is created rather than at the moment it is saved: with no timer and no
     terminator, every digit a caller types disappears for ever. */
  const cannotFlush = enabled && timeout === 0 && terminator === "";

  return (
    <Panel className={cn(LIFT_ON_HOVER, "grid gap-3.5")}>
      <CardHead
        title="Keypad input"
        desc="Digits the caller types, answered like anything else they say"
        className="mb-0"
      />

      <div className="-mt-3.5">
        <ToggleRow
          checked={enabled}
          onChange={onEnabled}
          divider={false}
          label="Let the caller answer with the keypad"
          help={
            "Keypresses arrive as one labelled turn - 'Keypad entry: 1234' - so the agent answers " +
            "them the way it answers speech, and the transcript keeps the two apart. The first " +
            "keypress interrupts whatever the agent is saying, because a caller who has started " +
            "typing has stopped listening. Phone calls and partner media streams only: a browser " +
            "has no keypad, and the digits are read from the carrier's own signalling rather than " +
            "detected in the audio, so a caller on a line that sends tones in-band cannot be heard."
          }
        />

        {enabled && (
          <div className="grid gap-3 pb-1 pl-8 pt-1 sm:grid-cols-2">
            <Field label="An entry is finished after">
              <Select
                value={String(timeout)}
                onChange={(e) => onTimeout(Number(e.target.value))}
              >
                {TIMEOUTS.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </Select>
            </Field>
            <Field label="Or when the caller presses">
              <Select
                value={terminator}
                onChange={(e) => onTerminator(e.target.value as "#" | "*" | "")}
              >
                {TERMINATORS.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </Select>
            </Field>
          </div>
        )}

        {cannotFlush && (
          <p className="pb-2 pl-8 text-[13px] leading-5 text-danger">
            Nothing would ever finish an entry, so the agent would never hear a single keypress.
            Pick a timeout, or a terminator key.
          </p>
        )}

        {enabled && !cannotFlush && (
          <p className="pb-2 pl-8 text-[13px] leading-5 text-muted">
            {terminator
              ? `${terminator} ends an entry and is not part of it. `
              : "Only the quiet timer ends an entry. "}
            Out-of-band digits never reach the recording, so a PIN typed on a keypad is absent from
            the stored audio.
          </p>
        )}
      </div>
    </Panel>
  );
}
