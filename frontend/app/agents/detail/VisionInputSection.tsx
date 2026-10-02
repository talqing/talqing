"use client";

import { cn } from "@/lib/cn";
import { CardHead, LIFT_ON_HOVER, Panel } from "../../components/ui";
import { ToggleRow } from "./ToggleRow";

/** What the agent watches while a call is running — one switch per source.
 *
 *  Its own card rather than a switch inside the Models stack, because it is a
 *  different kind of decision from the ones there: those tune the model, this
 *  one decides what the model is pointed at. The camera, if it is ever built, is
 *  a second group here.
 *
 *  Each source is a group: its switches with no rule between them, closed by one
 *  rule marking where the next source starts. Recording what was shared belongs
 *  to screen share rather than being a second thing to weigh, so it appears only
 *  once screen share is on and sits inside the same group.
 *
 *  Directly after Models all the same, because the constraint is the model's:
 *  watching a screen is reading an image, and a model that cannot do that
 *  cannot do this. The row stays visible and disabled with its reason rather
 *  than disappearing — the same rule `ExpressiveToggle` follows, since the model
 *  is being chosen one card up and a control that vanishes makes the trade
 *  invisible at the moment it is being made.
 *
 *  Note the two senses of "vision", and that both are right: a catalog entry's
 *  `vision` is what the model *can* do, measured and not configurable; this is
 *  what the agent is *pointed at*, and it is a choice. The first is the
 *  precondition for the second, which is what the disabled reason says. */
export function VisionInputSection({
  screenshare,
  record,
  modelReadsImages,
  modelLabel,
  onScreenshare,
  onRecord,
}: {
  screenshare: boolean;
  record: boolean;
  /** Whether the selected language model can read an image at all. */
  modelReadsImages: boolean;
  modelLabel: string;
  onScreenshare: (enabled: boolean) => void;
  onRecord: (record: boolean) => void;
}) {
  return (
    <Panel className={cn(LIFT_ON_HOVER, "grid gap-3.5")}>
      <CardHead
        title="Vision input"
        desc="What the agent can see while the call is running"
        className="mb-0"
      />

      {/* Flush to the head's rule, like the past-conversations rows. */}
      <div className="-mt-3.5">
        {/* One group per source. The rule separates one source from the next —
            `last:` so it draws nothing today, with only screen share here: a
            line under the final row closes nothing and just floats above the
            card's own edge. The camera, if it is ever built, drops in
            underneath as a second group and the rule appears on its own. */}
        <div className="border-b border-line last:border-b-0">
          <ToggleRow
            checked={screenshare}
            onChange={onScreenshare}
            divider={false}
            label="Screen share"
            disabledReason={
              modelReadsImages
                ? null
                : `${modelLabel} cannot read images, so it cannot see a screen. Pick a model that can to turn this on.`
            }
            help={
              "The caller gets a Share screen button on a web call, and the agent sees what they " +
              "share. It is handed the single newest frame at the end of each of their turns, and " +
              "only that one - so an hour-long call costs the same per turn as the first minute, " +
              "and the agent is told when they stop sharing rather than guessing. Each frame is " +
              "billed as image input on your language model, which the cost estimate includes. " +
              "Phone calls are unaffected: there is nothing to share, and the agent is never told " +
              "it can see."
            }
          />
          {screenshare && (
            <ToggleRow
              checked={record}
              onChange={onRecord}
              divider={false}
              label="Record screen share video"
              help={
                "Keeps the screen as a second recording of the call - one frame a second, " +
                "alongside the audio and lined up with the transcript, so you can replay a call " +
                "and see what the caller was looking at. It covers the whole call: the " +
                "stretches where nobody was sharing are black. Stored and deleted exactly like " +
                "the call recording, under this workspace's retention policy."
              }
            />
          )}
        </div>
      </div>
    </Panel>
  );
}
