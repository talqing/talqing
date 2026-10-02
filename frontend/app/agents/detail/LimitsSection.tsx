"use client";

import { useEffect, useState } from "react";
import { cn } from "@/lib/cn";
import { CardHead, Field, Input, LIFT_ON_HOVER, Label, Panel, Select } from "../../components/ui";
import { DEFAULT_MAX_STEPS, type Channel, type Silence } from "./agentConfig";
import { ToggleRow } from "./ToggleRow";

const MIN_STEPS = 1;
const MAX_STEPS = 50;

/* Selects rather than number fields, for the keypad timeout's reason: every
   value here is a judgement about a caller, not a measurement. The API takes
   any value in range, so one set elsewhere is shown as itself (`withCurrent`)
   rather than silently displayed as the nearest preset. */
const CALL_LENGTHS: readonly { value: number | null; label: string }[] = [
  { value: 300, label: "5 minutes" },
  { value: 600, label: "10 minutes" },
  { value: 900, label: "15 minutes" },
  { value: 1800, label: "30 minutes" },
  { value: 3600, label: "1 hour" },
  { value: 7200, label: "2 hours" },
  /* Null, not 10800: it is the platform's ceiling rather than a number the
     author chose, and the server treats the two the same. */
  { value: null, label: "3 hours (the maximum)" },
];

const SILENCE_TIMEOUTS: readonly { value: number; label: string }[] = [
  { value: 5, label: "5 seconds" },
  { value: 10, label: "10 seconds" },
  { value: 15, label: "15 seconds" },
  { value: 20, label: "20 seconds" },
  { value: 30, label: "30 seconds" },
  { value: 60, label: "1 minute" },
];

const CHECK_INS: readonly { value: number; label: string }[] = [
  { value: 0, label: "Hang up without checking in" },
  { value: 1, label: "Check in once, then hang up" },
  { value: 2, label: "Check in twice, then hang up" },
  { value: 3, label: "Check in 3 times, then hang up" },
];

function formatSeconds(seconds: number): string {
  if (seconds < 60) return `${seconds} seconds`;
  const minutes = seconds / 60;
  return Number.isInteger(minutes) ? `${minutes} minutes` : `${seconds} seconds`;
}

function withCurrent<T extends number | null>(
  options: readonly { value: T; label: string }[],
  current: T,
  label: (value: T) => string,
): readonly { value: T; label: string }[] {
  return options.some((o) => o.value === current) ? options : [...options, { value: current, label: label(current) }];
}

/** What bounds a turn, and on a call, what bounds the call.
 *
 *  Max steps: the field always shows the number the agent actually runs with,
 *  so nobody has to know that an empty box means "the channel's default". The
 *  default itself is stored as null, and typing it back is the same as
 *  resetting: a stored 4 would outlive a switch to text, whose default is 25.
 *  The typed text is local so a half-typed or cleared box is not snapped back
 *  to the default mid-keystroke; it settles on blur. Hidden on realtime, where
 *  LiveKit counts the rounds and never checks them.
 *
 *  Call length and silence: voice and video only — a chat window closes itself
 *  after a minute idle. Both are read off the agent that answers, so a handoff
 *  target's own values never apply; the help says so. */
export function LimitsSection({
  channel,
  isRealtime,
  maxSteps,
  onMaxSteps,
  maxDurationSeconds,
  onMaxDurationSeconds,
  silence,
  onSilence,
}: {
  channel: Channel;
  isRealtime: boolean;
  maxSteps: number | null | undefined;
  onMaxSteps: (value: number | null) => void;
  maxDurationSeconds: number | null;
  onMaxDurationSeconds: (value: number | null) => void;
  silence: Silence;
  onSilence: (value: Silence) => void;
}) {
  const fallback = DEFAULT_MAX_STEPS[channel];
  const effective = maxSteps ?? fallback;
  const [text, setText] = useState(String(effective));

  // An edit from elsewhere (CoPilot, a channel switch, a discard) lands here.
  useEffect(() => setText(String(effective)), [effective]);

  const typed = Number(text);
  const outOfRange = text !== "" && (!Number.isInteger(typed) || typed < MIN_STEPS || typed > MAX_STEPS);

  function change(raw: string) {
    setText(raw);
    const n = Number(raw);
    if (raw === "" || !Number.isInteger(n)) return;
    onMaxSteps(n === fallback ? null : n);
  }

  const isCall = channel !== "text";
  const callLengths = withCurrent(CALL_LENGTHS, maxDurationSeconds, (v) => formatSeconds(v ?? 0));
  const silenceTimeouts = withCurrent(SILENCE_TIMEOUTS, silence.timeout, formatSeconds);
  const checkIns = withCurrent(CHECK_INS, silence.max_check_ins, (v) => `Check in ${v} times, then hang up`);

  return (
    <Panel className={LIFT_ON_HOVER}>
      <CardHead title="Limits" />
      <div className="flex flex-col divide-y divide-line">
        {!isRealtime && (
          <div className="flex flex-col gap-2 pb-4">
            <div>
              <Label htmlFor="agent-max-steps">Max steps per turn</Label>
              <p className="mt-0.5 text-[13px] leading-5 text-muted">
                A fresh human message starts a turn, and each subsequent agent reply (+ tool calls) is
                one step.
              </p>
            </div>
            <div className="flex items-center gap-3">
              <Input
                id="agent-max-steps"
                type="number"
                inputMode="numeric"
                min={MIN_STEPS}
                max={MAX_STEPS}
                aria-invalid={outOfRange}
                className={cn("w-[124px] font-mono tabular-nums", outOfRange && "border-danger")}
                value={text}
                onChange={(e) => change(e.target.value)}
                onBlur={() => {
                  if (text === "") setText(String(effective));
                }}
              />
              {outOfRange ? (
                <span className="text-[13px] leading-5 text-danger">
                  Between {MIN_STEPS} and {MAX_STEPS}
                </span>
              ) : maxSteps != null ? (
                <button
                  type="button"
                  onClick={() => onMaxSteps(null)}
                  className="text-[13px] leading-5 text-muted underline-offset-2 transition-colors hover:text-ink hover:underline"
                >
                  Reset to default ({fallback})
                </button>
              ) : null}
            </div>
          </div>
        )}

        {isCall && (
          <Field
            label="Max call length"
            hint="At the limit, the agent says goodbye and hangs up."
            className={cn("py-4", isRealtime && "pt-0")}
          >
            <Select
              aria-label="Max call length"
              className="sm:w-[260px]"
              value={String(maxDurationSeconds)}
              onChange={(e) => onMaxDurationSeconds(e.target.value === "null" ? null : Number(e.target.value))}
            >
              {callLengths.map((o) => (
                <option key={String(o.value)} value={String(o.value)}>
                  {o.label}
                </option>
              ))}
            </Select>
          </Field>
        )}

        {isCall && (
          <div className="-mb-1 pt-1">
            <ToggleRow
              checked={silence.enabled}
              onChange={(enabled) => onSilence({ ...silence, enabled })}
              divider={false}
              label="Check in when the caller goes quiet"
              help={
                "After the chosen stretch of silence the agent asks whether the caller is still " +
                "there, in its own words and language. If every check-in goes unanswered, it says " +
                "goodbye and hangs up. Set by the agent that answers the call; a handoff keeps it."
              }
            />
            {silence.enabled && (
              <div className="grid gap-3 pb-1 pl-8 sm:grid-cols-2">
                <Field label="After silence of">
                  <Select
                    aria-label="After silence of"
                    value={String(silence.timeout)}
                    onChange={(e) => onSilence({ ...silence, timeout: Number(e.target.value) })}
                  >
                    {silenceTimeouts.map((o) => (
                      <option key={o.value} value={o.value}>
                        {o.label}
                      </option>
                    ))}
                  </Select>
                </Field>
                <Field label="Then">
                  <Select
                    aria-label="What the agent does next"
                    value={String(silence.max_check_ins)}
                    onChange={(e) => onSilence({ ...silence, max_check_ins: Number(e.target.value) })}
                  >
                    {checkIns.map((o) => (
                      <option key={o.value} value={o.value}>
                        {o.label}
                      </option>
                    ))}
                  </Select>
                </Field>
              </div>
            )}
          </div>
        )}
      </div>
    </Panel>
  );
}
