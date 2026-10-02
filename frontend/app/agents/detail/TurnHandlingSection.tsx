"use client";
import type { ReactNode } from "react";
import { BoxCheckbox, CardHead, HelpDot, Input, LIFT_ON_HOVER, Panel } from "@/app/components/ui";
import { cn } from "@/lib/cn";
import type { TurnDetection, TurnHandling } from "./agentConfig";

export const TURN_HELP = {
  endpointing_min_silence_duration:
    "How long the caller has to stay silent, in seconds, before the agent takes the turn. With a streaming speech-to-text model this is the window its own end-of-speech detector gets, so the transcription round-trip lands on top and the provider can hold the turn open longer when it hears the caller is not finished — and Deepgram Flux and Sarvam endpoint on their own schedule, treating it as a floor rather than a setting. With a batch model it is what the agent waits. Minimum 0.25, defaults to 0.5.",
  endpointing_max_silence_duration:
    "How long to wait instead when the turn detector judges the caller is mid-thought. Only a batch speech-to-text model in one of the detector's languages gets that judgement; every other pipeline ends turns on silence and ignores this. Defaults to 2.5.",
  interruption_enabled: "Whether interruptions are enabled. Defaults to true.",
  discard_audio_if_uninterruptible:
    "Drop buffered audio while the agent speaks and cannot be interrupted. Defaults to true.",
  interruption_min_speech_duration:
    "Minimum speech length in seconds to register as an interruption. Defaults to 0.5.",
  interruption_min_words:
    "Minimum word count to consider an interruption. Needs interim transcripts, so it applies to the speech-to-text pipeline only. Defaults to 0.",
  resume_false_interruption:
    "Resume the agent's speech after a false interruption. Defaults to true.",
  false_interruption_timeout:
    "Seconds of silence after an interruption before it is classified as false. Null disables false-interruption classification. Defaults to 2.0.",
  preemptive_enabled:
    "Whether preemptive generation is enabled. It runs off the live transcript, so it works the same whatever ends the turn. Defaults to true in LiveKit; this dashboard defaults it off to avoid surprise cost.",
  preemptive_tts:
    "Whether to also run TTS preemptively before the turn is confirmed. When false, only the LLM runs preemptively.",
  preemptive_max_speech_duration:
    "Maximum user speech duration in seconds for which preemptive generation is attempted. Defaults to 10.0.",
  preemptive_max_retries:
    "Maximum number of preemptive generation attempts per user turn. The counter resets when the turn completes. Defaults to 3.",
};

/** Checkbox whose label is also the hit target, plus a help tooltip. */
function Toggle({
  checked,
  onChange,
  label,
  help,
}: {
  checked: boolean;
  onChange: (value: boolean) => void;
  label: string;
  help: string;
}) {
  return (
    <div className="flex items-center gap-2 text-[13px] text-ink-soft">
      <BoxCheckbox checked={checked} onChange={onChange} ariaLabel={label} />
      <button type="button" className="text-left" onClick={() => onChange(!checked)}>
        {label}
      </button>
      <HelpDot label={help} />
    </div>
  );
}

/* A tuning value is two or three characters wide. Sized to what it holds rather
   than stretched across the card, with the unit set inside the field so the
   label can be the plain english name of the thing. */
function NumberField({
  label,
  unit,
  help,
  value,
  onChange,
  step,
  min = 0,
  disabled,
}: {
  label: string;
  unit?: string;
  help: string;
  value: number | "";
  onChange: (raw: string) => void;
  step: number;
  min?: number;
  disabled?: boolean;
}) {
  const id = `turn-${label.replace(/\W+/g, "-").toLowerCase()}`;
  return (
    <div className={cn("flex flex-none flex-col gap-2", disabled && "opacity-50")}>
      <span className="flex items-center gap-1.5 text-[13px] font-medium leading-4 text-ink-soft">
        <label htmlFor={id}>{label}</label>
        <HelpDot label={help} />
      </span>
      <span className="relative block w-[124px]">
        <Input
          id={id}
          type="number"
          step={step}
          min={min}
          disabled={disabled}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          className={cn("font-mono tabular-nums", unit && "pr-7")}
        />
        {unit && (
          <span aria-hidden className="pointer-events-none absolute right-3 top-1/2 -translate-y-1/2 font-mono text-[12px] text-faint">
            {unit}
          </span>
        )}
      </span>
    </div>
  );
}

/* A named band of the panel — the same full-bleed, hairline-separated shape the
   Models card gives a pipeline stage, so the two long cards on this page are
   read the same way. It used to be a small-caps eyebrow beside a rule that
   stopped short of the card edge, which was a third kind of sub-heading on a
   page that only needs one.

   A group's master switch is the first control in the group, left-aligned in
   the same column as everything it turns off — not parked on the heading row.
   It reads as a field because it is one. */
export function TuningGroup({
  title,
  help,
  toggle,
  muted,
  children,
}: {
  title: string;
  /** Why this group exists, on the heading rather than in a paragraph below it. */
  help?: ReactNode;
  toggle?: ReactNode;
  /** The group's own switch is off: the controls stay readable but recede. */
  muted?: boolean;
  children: ReactNode;
}) {
  return (
    <section className="-mx-5 border-t border-line px-5 pb-4 pt-4 first:border-t-0 last:pb-0">
      <div className="mb-3 flex items-center gap-2">
        <h3 className="text-[15px] font-semibold leading-5 text-ink">{title}</h3>
        {help && <HelpDot label={help} className="flex-none" />}
      </div>
      {toggle && <div className="mb-3.5">{toggle}</div>}
      <div className={cn(muted && "opacity-55")}>{children}</div>
    </section>
  );
}

export function TurnHandlingSection({
  turnHandling,
  isVideo,
  isRealtime,
  turnDetection,
  onChange,
  children,
}: {
  turnHandling: TurnHandling;
  /** Video pins resume_false_interruption off — an avatar cannot resume
      mid-sentence — so that control is hidden rather than shown lying. */
  isVideo: boolean;
  /** A realtime model detects turns and interruptions on its own server side.
      The silence budget and the interruption switch still reach it; the knobs
      that only a transcript-driven pipeline can honour do not. */
  isRealtime: boolean;
  /** What will end the caller's turn, derived from the speech-to-text model and
      the agent's language rather than chosen. Stated rather than offered: it is
      not a preference, but it does change what the numbers below mean, so
      leaving it unsaid would make them look inconsistent. */
  turnDetection: TurnDetection;
  onChange: (patch: Partial<TurnHandling>) => void;
  /** Background audio controls, which belong to the config rather than turn handling. */
  children?: ReactNode;
}) {
  const { endpointing, interruption, preemptive_generation: preemptive } = turnHandling;
  // Only an end-of-turn model can say "the caller is not finished", so max
  // silence is the one number that is inert everywhere else. Realtime needs no
  // guard: it has no speech-to-text model, so it never resolves to the detector.
  const usesEou = turnDetection === "livekit-turn-detector-v1-mini";
  // What actually ends the caller's turn. Not a setting — it falls out of the
  // speech-to-text model and the agent's language — but it changes what the
  // silence numbers mean, so it rides along on the heading as help text.
  const detectionHelp = isRealtime
    ? "The realtime model detects turns itself, server-side. The silence below is sent to it as its own window."
    : turnDetection === "livekit-turn-detector-v1-mini"
      ? "This model transcribes each turn in one request, so LiveKit's end-of-turn model listens for you: it hears whether the sentence is finished and waits through a mid-thought pause instead of cutting in. Runs locally on the CPU at no extra cost."
      : turnDetection === "vad"
        ? "This model transcribes each turn in one request, so silence is what ends the turn. LiveKit's end-of-turn model would judge it better, but it is not trained on this agent's language — pick one it knows and the agent will use it."
        : "This model streams as the caller speaks, so its own end-of-speech signal ends the turn. It can keep listening when it hears the caller is not finished, and its transcription round-trip lands on top of the silence below.";

  const patchInterruption = (patch: Partial<TurnHandling["interruption"]>) =>
    onChange({ interruption: { ...interruption, ...patch } });
  const patchPreemptive = (patch: Partial<TurnHandling["preemptive_generation"]>) =>
    onChange({ preemptive_generation: { ...preemptive, ...patch } });

  return (
    <Panel className={LIFT_ON_HOVER}>
      <CardHead title="Turn handling" desc="How the agent decides when to speak" className="mb-0" />
      <div className="flex flex-col">
        <TuningGroup title="Endpointing" help={detectionHelp}>
          <div className="flex flex-wrap gap-x-7 gap-y-5">
            <NumberField
              label="Min silence"
              unit="s"
              help={TURN_HELP.endpointing_min_silence_duration}
              step={0.1}
              min={0.25}
              value={endpointing.min_silence_duration}
              onChange={(raw) =>
                onChange({ endpointing: { ...endpointing, min_silence_duration: Number(raw) } })
              }
            />
            {usesEou && (
              <NumberField
                label="Max silence"
                unit="s"
                help={TURN_HELP.endpointing_max_silence_duration}
                step={0.1}
                value={endpointing.max_silence_duration}
                onChange={(raw) =>
                  onChange({ endpointing: { ...endpointing, max_silence_duration: Number(raw) } })
                }
              />
            )}
          </div>
          {isRealtime && (
            <p className="mt-3 max-w-[70ch] text-[13px] leading-relaxed text-muted">
              Sent to the realtime model as its own silence window. There is no
              transcription round-trip after it, so the whole budget is the wait.
            </p>
          )}
          {usesEou && (
            <p className="mt-3 max-w-[70ch] text-[13px] leading-relaxed text-muted">
              Min silence is how long the agent waits once the model agrees the caller
              is done; max silence is how long it waits when the model thinks they are
              mid-thought.
            </p>
          )}
        </TuningGroup>

        <TuningGroup
          title="Interruption"
          muted={!interruption.enabled}
          toggle={
            <Toggle
              label="Enabled"
              help={TURN_HELP.interruption_enabled}
              checked={interruption.enabled}
              onChange={(enabled) => patchInterruption({ enabled })}
            />
          }
        >
          <div className="flex flex-col gap-5">
            <div className="flex flex-wrap gap-x-7 gap-y-5">
              <NumberField
                label="Min speech"
                unit="s"
                help={TURN_HELP.interruption_min_speech_duration}
                step={0.1}
                value={interruption.min_speech_duration}
                onChange={(raw) => patchInterruption({ min_speech_duration: Number(raw) })}
              />
              {!isRealtime && (
                <NumberField
                  label="Min words"
                  help={TURN_HELP.interruption_min_words}
                  step={1}
                  value={interruption.min_words}
                  onChange={(raw) => patchInterruption({ min_words: Number(raw) })}
                />
              )}
              <NumberField
                label="False timeout"
                unit="s"
                help={TURN_HELP.false_interruption_timeout}
                step={0.1}
                disabled={interruption.false_interruption_timeout === null}
                value={interruption.false_interruption_timeout ?? ""}
                onChange={(raw) =>
                  patchInterruption({ false_interruption_timeout: raw === "" ? null : Number(raw) })
                }
              />
            </div>
            <div className="flex flex-col gap-3">
              <Toggle
                label="Classify false interruptions"
                help={TURN_HELP.false_interruption_timeout}
                checked={interruption.false_interruption_timeout !== null}
                onChange={(on) => patchInterruption({ false_interruption_timeout: on ? 2.0 : null })}
              />
              <Toggle
                label="Discard audio while uninterruptible"
                help={TURN_HELP.discard_audio_if_uninterruptible}
                checked={interruption.discard_audio_if_uninterruptible}
                onChange={(discard_audio_if_uninterruptible) => patchInterruption({ discard_audio_if_uninterruptible })}
              />
              {!isVideo && !isRealtime && (
                <Toggle
                  label="Resume after false interruption"
                  help={TURN_HELP.resume_false_interruption}
                  checked={interruption.resume_false_interruption}
                  onChange={(resume_false_interruption) => patchInterruption({ resume_false_interruption })}
                />
              )}
            </div>
          </div>
        </TuningGroup>

        <TuningGroup
          title="Preemptive generation"
          muted={!isRealtime && !preemptive.enabled}
          toggle={
            !isRealtime ? (
              <Toggle
                label="Enabled"
                help={TURN_HELP.preemptive_enabled}
                checked={preemptive.enabled}
                onChange={(enabled) => patchPreemptive({ enabled })}
              />
            ) : undefined
          }
        >
          {isRealtime ? (
            <p className="max-w-[70ch] text-[13px] leading-relaxed text-muted">
              Off for realtime agents. The model answers the moment it decides the
              caller has stopped, so there is no gap left to speculate into.
            </p>
          ) : (
            <div className="flex flex-col gap-5">
              <div className="flex flex-wrap gap-x-7 gap-y-5">
                <NumberField
                  label="Max speech"
                  unit="s"
                  help={TURN_HELP.preemptive_max_speech_duration}
                  step={0.5}
                  value={preemptive.max_speech_duration}
                  onChange={(raw) => patchPreemptive({ max_speech_duration: Number(raw) })}
                />
                <NumberField
                  label="Max retries"
                  help={TURN_HELP.preemptive_max_retries}
                  step={1}
                  value={preemptive.max_retries}
                  onChange={(raw) => patchPreemptive({ max_retries: Number(raw) })}
                />
              </div>
              <div className="flex flex-col gap-3">
                <Toggle
                  label="Preemptive TTS"
                  help={TURN_HELP.preemptive_tts}
                  checked={preemptive.preemptive_tts}
                  onChange={(preemptive_tts) => patchPreemptive({ preemptive_tts })}
                />
              </div>
            </div>
          )}
        </TuningGroup>

        {children}
      </div>
    </Panel>
  );
}
