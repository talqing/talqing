"use client";
import type { AnalysisSpec, ConversationSpec } from "@talqing/sdk";
import { cn } from "@/lib/cn";
import { BoxCheckbox, CardHead, Input, Label, LIFT_ON_HOVER, Panel } from "../../components/ui";

type Context = NonNullable<ConversationSpec["context"]>;

/** What one session of this agent is called: a voice or video agent takes
    calls, a text agent holds chats. The setting means the same for both. */
type Session = "call" | "chat";

/* Named for what the agent STARTS WITH, which is the thing being chosen. The
   same three words name the handoff operation's context policy, so the tool
   editor and this section say one thing for one idea. */
const modes = (session: Session): { value: Context; label: string; desc: string }[] => [
  {
    value: "none",
    label: "Don't include past conversation context",
    desc: `Every ${session} starts clean. Earlier conversations are still saved against this person.`,
  },
  {
    value: "summary",
    label: "Include summaries of past conversations",
    desc: "The agent is told what happened on recent conversations, as background.",
  },
  {
    value: "transcript",
    label: `Continue the same conversation on every ${session}`,
    desc: "One long conversation. The agent sees everything said before.",
  },
];

/** One radio row: the whole row is the hit target, the way ToggleRow works. */
function ModeRow({
  selected,
  onSelect,
  label,
  desc,
}: {
  selected: boolean;
  onSelect: () => void;
  label: string;
  desc: string;
}) {
  return (
    <button
      type="button"
      role="radio"
      aria-checked={selected}
      onClick={onSelect}
      className="flex w-full items-start gap-3 border-b border-line py-3 text-left last:border-b-0"
    >
      <span
        className={cn(
          "mt-0.5 grid h-5 w-5 flex-none place-items-center rounded-full border border-line-2 bg-surface shadow-[0_1px_3px_rgba(0,0,0,0.10),0_1px_2px_-1px_rgba(0,0,0,0.10)] transition-colors",
          selected && "border-ink bg-ink",
        )}
        aria-hidden
      >
        {selected && <span className="h-1.5 w-1.5 rounded-full bg-white" />}
      </span>
      <span className="min-w-0 flex-1">
        <span className="block text-[14px] font-semibold leading-5 text-ink">{label}</span>
        <span className="mt-0.5 block text-[12.5px] leading-5 text-muted">{desc}</span>
      </span>
    </button>
  );
}

export function ConversationSection({
  session,
  conversation,
  analysis,
  onChange,
  onAnalysisChange,
}: {
  session: Session;
  conversation: ConversationSpec;
  /* Read AND written: `summary` mode has nothing to read without call-analysis
     summaries, so picking it switches them on rather than saving a config that
     publish will refuse. */
  analysis: AnalysisSpec;
  onChange: (patch: Partial<ConversationSpec>) => void;
  onAnalysisChange: (patch: Partial<AnalysisSpec>) => void;
}) {
  const context: Context = conversation.context ?? "none";
  const initializeUserdata = conversation.initialize_userdata ?? true;
  const limit = conversation.summary_limit;
  const analysisEnabled = analysis.enabled ?? true;
  const analysisSummary = analysis.summary ?? true;

  function selectMode(next: Context) {
    onChange({ context: next });
    // `summary` reads what call analysis writes, so picking it switches analysis
    // and its summary on rather than saving a config publish will refuse. No
    // note here: the Analysis card directly below is where those two toggles
    // live and they visibly flip, and the cost rail already itemises Analysis.
    if (next === "summary" && (!analysisEnabled || !analysisSummary)) {
      onAnalysisChange({ enabled: true, summary: true });
    }
  }

  return (
    <Panel className={cn(LIFT_ON_HOVER, "grid gap-3.5")}>
      <CardHead
        title="Past conversations"
        desc={`What a new ${session} knows about earlier ones with the same person`}
        className="mb-0"
      />

      {/* Flush to the head's rule and closed by its own, like the recording
          rows: equal bands, each label optically centred in its row. */}
      <div className="-mt-3.5 flex flex-col border-b border-line" role="radiogroup">
        {modes(session).map((mode) => (
          <ModeRow
            key={mode.value}
            selected={context === mode.value}
            onSelect={() => selectMode(mode.value)}
            label={mode.label}
            desc={mode.desc}
          />
        ))}
      </div>
      {session === "call" && (
        <p className="-mt-1 text-[12px] leading-relaxed text-muted">
          WhatsApp calls always continue the chat.
        </p>
      )}

      {context === "summary" && (
        <div className="flex flex-col gap-2">
          <Label htmlFor="conversation-summary-limit">Conversations to summarize</Label>
          <Input
            id="conversation-summary-limit"
            type="number"
            min={1}
            className="w-[160px]"
            placeholder="All"
            value={limit ?? ""}
            /* Empty must send null, not 0: the API rejects 0 (ge=1) and it would
               read as "none", the opposite of what an empty box means here. */
            onChange={(e) =>
              onChange({
                summary_limit: e.target.value.trim() === "" ? null : Number(e.target.value),
              })
            }
          />
          <p className="text-[12px] leading-relaxed text-muted">
            Leave empty to include every past one.
          </p>
        </div>
      )}

      {context !== "none" && (
        <div className="flex items-start gap-3 border-t border-line pt-3.5">
          <BoxCheckbox
            checked={initializeUserdata}
            onChange={(next) => onChange({ initialize_userdata: next })}
            ariaLabel="Initialize with conversation userdata"
            className="mt-0.5"
          />
          <button
            type="button"
            className="min-w-0 flex-1 text-left"
            onClick={() => onChange({ initialize_userdata: !initializeUserdata })}
          >
            <span className="block text-[14px] font-semibold leading-5 text-ink">
              Initialize with conversation userdata
            </span>
            <span className="mt-0.5 block text-[12.5px] leading-5 text-muted">
              Start the {session} already knowing what the agent learned about this person before.
            </span>
          </button>
        </div>
      )}

    </Panel>
  );
}
