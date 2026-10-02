"use client";

import Link from "next/link";
import type { AgentResponse, HandoffTarget } from "@talqing/sdk";
import { cn } from "@/lib/cn";
import {
  Badge,
  Button,
  CardHead,
  Field,
  Input,
  LIFT_ON_HOVER,
  Panel,
  Select,
  Textarea,
} from "../../components/ui";

/** Where this agent may hand the conversation next.
 *
 *  Before this existed a handoff lived inside a tool's operation tree, reachable
 *  only from the tool editor: a three-agent flow with mutual routing was three
 *  agents plus six published tools, and the graph was visible nowhere at all.
 *  Here it is a list of destinations on the agent that routes, which is where an
 *  author looks for it.
 *
 *  The `handoff` OPERATION is still there and is still the right tool for a
 *  conditional handoff — "look the account up, and *if* it is enterprise, hand
 *  to the enterprise desk". These entries are the routing case: the model picks,
 *  from the descriptions, which is why `description` is required.
 *
 *  The target's channel and pipeline are shown as facts rather than judged here.
 *  Whether an edge is legal is `services/agents/media.py`'s rule — an expressive
 *  voice has to match all the way down — and a second copy of it in TypeScript
 *  would be a copy that drifts. Publish states the verdict; this makes the
 *  mismatch visible while the choice is being made. */
export function HandoffsSection({
  handoffs,
  agents,
  selfId,
  channel,
  realtime,
  onChange,
}: {
  handoffs: HandoffTarget[];
  /** Every agent in the workspace, so a target can be picked by name. */
  agents: AgentResponse[];
  selfId: string | null;
  channel: string;
  realtime: boolean;
  onChange: (next: HandoffTarget[]) => void;
}) {
  const targets = agents.filter((a) => a.id !== selfId && a.published_version);

  function edit(index: number, patch: Partial<HandoffTarget>) {
    onChange(handoffs.map((t, i) => (i === index ? { ...t, ...patch } : t)));
  }

  /** Changing the policy clears the fields the new one does not accept, because
   *  the API rejects them rather than ignoring them — `transcript` already
   *  carries every turn, and only `summary` has a summary to prompt for. */
  function setContext(index: number, context: HandoffTarget["context"]) {
    edit(index, {
      context,
      recent_turns: context === "transcript" ? null : handoffs[index].recent_turns,
      summary_prompt: context === "summary" ? handoffs[index].summary_prompt : null,
    });
  }

  return (
    <Panel className={cn(LIFT_ON_HOVER, "grid gap-3.5")}>
      <CardHead
        title="Handoffs"
        desc="Where this agent can pass the conversation next"
        className="mb-0"
      >
        {handoffs.length > 0 && (
          <Badge>
            {handoffs.length} {handoffs.length === 1 ? "destination" : "destinations"}
          </Badge>
        )}
        <Button
          variant="secondary"
          size="sm"
          onClick={() =>
            onChange([...handoffs, { name: "", agent_id: null, description: "", context: "transcript" }])
          }
        >
          Add a destination
        </Button>
      </CardHead>

      {handoffs.length === 0 ? (
        <p className="-mt-1 text-[13px] leading-5 text-muted">
          Each destination becomes one tool the model can call. It routes on the description you
          write, so say plainly what belongs there — “invoices, refunds, payment questions” — and
          the caller is handed over mid-call, with as much of the conversation as you choose to
          carry across.
        </p>
      ) : (
        <div className="-mt-1 flex flex-col divide-y divide-line">
          {handoffs.map((target, index) => {
            const picked = targets.find((a) => a.id === target.agent_id);
            return (
              <div key={index} className="grid gap-2.5 py-3 first:pt-0 last:pb-0">
                <div className="flex flex-wrap items-end gap-3">
                  <Field label="Name" className="min-w-[9rem] flex-1">
                    <Input
                      value={target.name}
                      placeholder="Billing"
                      onChange={(e) => edit(index, { name: e.target.value })}
                    />
                  </Field>
                  <Field label="Goes to" className="min-w-[14rem] flex-1">
                    <Select
                      value={target.agent_id ?? ""}
                      onChange={(e) => edit(index, { agent_id: e.target.value || null })}
                      searchable={targets.length > 12}
                      aria-label="Handoff target"
                    >
                      <option value="">A team member, named at call time</option>
                      {targets.map((a) => (
                        <option key={a.id} value={a.id}>
                          {a.config.name}
                        </option>
                      ))}
                    </Select>
                  </Field>
                  <Field label="Starts from" className="w-[13rem] flex-none">
                    <Select
                      value={target.context ?? "transcript"}
                      onChange={(e) => setContext(index, e.target.value as HandoffTarget["context"])}
                      aria-label="Context passed to the target"
                    >
                      <option value="transcript">Conversation so far</option>
                      {/* “you” because the agent reading this card is the one
                          that writes it, as an argument on the tool it calls —
                          unlike the call-level summary, which we write. */}
                      <option value="summary">A summary you write</option>
                      {/* A policy called “Nothing” that carries three turns is a
                          label contradicting the field beside it, so the label
                          follows the field. The cost of letting `none` take a
                          tail, paid here rather than in the schema. */}
                      <option value="none">
                        {target.recent_turns
                          ? target.recent_turns === 1
                            ? "Just the last turn"
                            : `Just the last ${target.recent_turns} turns`
                          : "Nothing"}
                      </option>
                    </Select>
                  </Field>
                  {target.context !== "transcript" && (
                    <Field label="Recent turns" className="w-[7.5rem] flex-none">
                      <Input
                        type="number"
                        min={1}
                        max={10}
                        value={target.recent_turns ?? ""}
                        /* Under `summary` the tail is sized, never switched off,
                           so an empty field means the default of 2 rather than
                           none — a summary-only handoff leaves the target
                           knowing the history and not the question. Under
                           `none` it is genuinely optional. */
                        placeholder={target.context === "summary" ? "2" : "none"}
                        onChange={(e) =>
                          edit(index, {
                            recent_turns: e.target.value ? Number(e.target.value) : null,
                          })
                        }
                        aria-label="Recent turns carried over verbatim"
                      />
                    </Field>
                  )}
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => onChange(handoffs.filter((_, i) => i !== index))}
                    aria-label={`Remove ${target.name || "destination"}`}
                  >
                    Remove
                  </Button>
                </div>

                {/* Side by side: both are one short line, and stacked they made
                    one destination as tall as the whole Models card. */}
                <div className="grid gap-3 md:grid-cols-2">
                  <Field label="What the model routes on">
                    <Input
                      value={target.description}
                      placeholder="invoices, refunds, payment questions"
                      onChange={(e) => edit(index, { description: e.target.value })}
                    />
                  </Field>
                  <Field label="Said while the target loads" className="min-w-0">
                    <Input
                      value={target.message ?? ""}
                      placeholder="Let me put you through to billing…"
                      onChange={(e) => edit(index, { message: e.target.value || null })}
                    />
                  </Field>
                </div>

                {/* Full width rather than a third column: it is the longest
                    prose of the three and the only one the model is asked to
                    answer rather than read. */}
                {target.context === "summary" && (
                  <Field
                    label="What this agent writes as it hands over"
                    hint="Longer prompts mean a longer pause before the handoff — the agent writes this before the target starts loading."
                  >
                    <Textarea
                      className="min-h-[68px]"
                      value={target.summary_prompt ?? ""}
                      placeholder={`A short summary of this conversation for ${target.name || "…"}: what the caller wants, what you have already done, and what is still open.`}
                      onChange={(e) => edit(index, { summary_prompt: e.target.value || null })}
                    />
                  </Field>
                )}

                <div className="flex flex-wrap items-center gap-2 text-[12.5px] text-muted">
                  <span className="font-mono text-[12px] text-ink">
                    handoff_to_{slug(target.name)}
                  </span>
                  {picked ? (
                    <>
                      <Badge variant={picked.config.channel === channel ? "default" : "warn"}>
                        {picked.config.channel}
                      </Badge>
                      <Badge
                        variant={(picked.config.realtime != null) === realtime ? "default" : "warn"}
                      >
                        {picked.config.realtime ? "realtime" : "cascade"}
                      </Badge>
                      <Link
                        href={`/agents/detail?id=${picked.id}`}
                        className="text-muted underline-offset-2 hover:text-ink hover:underline"
                      >
                        Open {picked.config.name} ↗
                      </Link>
                    </>
                  ) : target.agent_id ? (
                    <Badge variant="warn">this agent is gone or unpublished</Badge>
                  ) : (
                    <span>
                      resolves to the member named “{target.name || "…"}” on a call that defines
                      one
                    </span>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </Panel>
  );
}

/** The identifier the destination's name becomes — the tool the model sees.
 *  Mirrors `services/agents/models.py::handoff_tool_name`; shown here only so an
 *  author can see the name their prompt will be written against. */
function slug(name: string): string {
  return name
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "");
}
