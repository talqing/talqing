"use client";

import type { TTSCatalogEntryResponse } from "@talqing/sdk";

import { ToggleRow } from "./ToggleRow";

/** Whether the agent writes delivery tags into the words this voice speaks.
 *
 *  Which models understand them is the catalog's business: an entry carries an
 *  `expressive` block or it does not, and it is per model rather than per
 *  provider — ElevenLabs' v3 honours the tags while its Flash and Turbo models
 *  read them out to the caller as words.
 *
 *  Unlike the priority lane next to the language model, this one stays visible
 *  on a model that cannot do it. Expressive delivery is a reason to choose one
 *  voice over another, and the voice is being chosen directly above; a control
 *  that disappears would make the trade invisible at exactly the moment it is
 *  being made.
 *
 *  The fallback is kept in step here rather than reported at publish. The
 *  dialect is taught once, from the primary voice, and a failover happens
 *  mid-turn — so the two have to agree, and the editor is where that is cheap to
 *  arrange. */
export function ExpressiveToggle({
  entry,
  fallbackEntry,
  value,
  onChange,
}: {
  entry: TTSCatalogEntryResponse | undefined;
  /** The failover voice, when one is set. It has to speak tags too. */
  fallbackEntry: TTSCatalogEntryResponse | undefined;
  value: boolean | undefined;
  onChange: (expressive: boolean) => void;
}) {
  const model = entry ? entry.label || `${entry.provider}/${entry.model}` : "This voice";
  const disabledReason = !entry?.expressive
    ? `${model} speaks plain text only. Pick a voice that supports delivery tags to turn this on.`
    : fallbackEntry && !fallbackEntry.expressive
      ? `The fallback voice, ${fallbackEntry.label || fallbackEntry.model}, speaks plain text only. A failover takes over mid-call, so change or remove it first.`
      : null;

  return (
    // A group of one, so the row's own bottom rule collapses: whatever follows
    // it in the stage — the fallback picker, in either state — brings its own.
    <div>
      <ToggleRow
        checked={value === true}
        onChange={onChange}
        label="Expressive delivery"
        disabledReason={disabledReason}
        help={
          "Teaches the agent this voice's own delivery tags, so it can laugh, drop to a whisper, " +
          "or pause before the detail that matters instead of reading every line flat. The tags " +
          "appear in transcripts and in the call record, which is how you see what the agent " +
          "chose to do. It costs a little on both sides of a turn - the tag guidance adds to the " +
          "prompt, and every tag the agent writes is billed output - and both are passed to you."
        }
      />
    </div>
  );
}
