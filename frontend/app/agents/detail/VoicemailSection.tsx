"use client";

import { cn } from "@/lib/cn";
import { CardHead, Field, LIFT_ON_HOVER, Panel, Textarea } from "../../components/ui";
import { ToggleRow } from "./ToggleRow";

/** What an outbound call does when a machine answers it.
 *
 *  Voice agents only, like the keypad beside it: only a voice agent places
 *  calls, and the server refuses the setting anywhere else.
 *
 *  The message is optional rather than a hang-up-or-message choice, which is
 *  ElevenLabs' shape and the honest one: an empty box already reads as "no
 *  message", and a two-way switch would need a third state for "chose to leave
 *  one, has not written it yet" that the server has no way to store. */
export function VoicemailSection({
  enabled,
  message,
  onEnabled,
  onMessage,
}: {
  enabled: boolean;
  message: string | null;
  onEnabled: (enabled: boolean) => void;
  onMessage: (message: string | null) => void;
}) {
  return (
    <Panel className={cn(LIFT_ON_HOVER, "grid gap-3.5")}>
      <CardHead
        title="Voicemail"
        desc="What the agent does when a call it places reaches an answering machine"
        className="mb-0"
      />

      <div className="-mt-3.5">
        <ToggleRow
          checked={enabled}
          onChange={onEnabled}
          divider={false}
          label="Hang up on voicemail"
          help="Outbound calls only. The call ends as voicemail, and a batch calls that person again later."
        />

        {enabled && (
          <Field
            label={
              <>
                Message to leave <span className="font-normal text-faint">(optional)</span>
              </>
            }
            hint="Spoken word for word after the voicemail greeting, then the agent hangs up."
            className="pb-1 pl-8 pt-1"
          >
            <Textarea
              aria-label="Voicemail message"
              className="min-h-[64px]"
              autoGrow
              maxLength={2000}
              placeholder="Hi, this is Maya from Brightside Dental about your appointment tomorrow. Please call us back when you can."
              value={message ?? ""}
              onChange={(e) => onMessage(e.target.value.trim() ? e.target.value : null)}
            />
          </Field>
        )}
      </div>
    </Panel>
  );
}
