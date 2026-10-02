"use client";
import type { VarDeclaration } from "@talqing/sdk";
import { BoxCheckbox, Button, HelpDot, Input, Label, RowRemove } from "../../components/ui";
import { useState } from "react";
import { cn } from "@/lib/cn";

/* A variable is read back as `{{vars.name}}`, so the API refuses anything the
   token pattern could not match. Normalising as the user types beats letting
   them find the rule as a save error on a field they finished with two minutes
   ago — the same trade `AnalysisSection.toIdentifier` makes. */
function toVarName(raw: string): string {
  return raw
    .replace(/[^A-Za-z0-9_]+/g, "_")
    .replace(/^[^A-Za-z_]+/, "")
    .slice(0, 60);
}

const BLANK: VarDeclaration = { name: "", description: "", default: null, required: false };

/** The variables this agent or task reads, and what each falls back to.
 *
 *  Three columns rather than a name/value pair, because a declaration is not a
 *  value: the description is what a teammate and the CoPilot read to know what
 *  to send, and the default is what resolves on an inbound call nobody made a
 *  request for. Values arrive per session, on the request that starts it.
 *
 *  One component for both editors, deliberately. After the decision that a
 *  task's inputs simply ARE an agent's variables — same model, same meaning,
 *  same `{{vars.name}}` — a task-shaped copy of this file would be two
 *  components editing one shape, which is how they drift. */
export function VariablesSection({
  vars,
  channel,
  subject = "agent",
  withRequired = false,
  onChange,
}: {
  vars: VarDeclaration[];
  /** Only a voice agent can be reached by an inbound call, which is the one
   *  session that has no request to carry values — so the sentence about
   *  defaults mattering is only true, and only shown, there. Unused when
   *  `subject` is "task": a task run always has a request behind it. */
  channel?: string;
  /** Which editor this is in. Decides the prose only. */
  subject?: "agent" | "task";
  /** Offer the Required flag. On both editors now: a required variable refuses
   *  the request that starts a session — a run, a call, a dial, a campaign or a
   *  text thread — before anything is compiled or any provider is called. */
  withRequired?: boolean;
  onChange: (next: VarDeclaration[]) => void;
}) {
  /* The empty state offers a row to type into rather than a button to press
     first. It is NOT in `vars` until something is typed into it — an editor
     nobody touched still saves zero variables, and a nameless declaration never
     reaches the API on its own — and its cross takes it away, since an agent
     with no variables should be able to look like one. */
  const [showBlank, setShowBlank] = useState(true);
  const rows = vars.length ? vars : showBlank ? [BLANK] : [];

  function patch(index: number, next: Partial<VarDeclaration>) {
    if (!vars.length) return onChange([{ ...BLANK, ...next }]);
    onChange(vars.map((v, i) => (i === index ? { ...v, ...next } : v)));
  }

  function removeRow(index: number) {
    if (!vars.length) return setShowBlank(false);
    onChange(vars.filter((_, i) => i !== index));
  }

  const isTask = subject === "task";
  const grid = withRequired
    ? "grid-cols-[190px_minmax(0,1fr)_200px_84px_36px]"
    : "grid-cols-[190px_minmax(0,1fr)_260px_36px]";

  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-center gap-2">
        <Label>{isTask ? "Inputs" : "Variables"}</Label>
        {/* Everything about a variable that is not the one-line rule under the
            button lives here. It used to be a second paragraph beside the
            button, which said much of this again at four lines. */}
        <HelpDot
          label={
            isTask
              ? "The values a run is given, read as {{vars.name}} in the prompt and in this task's tools. Whoever starts a run supplies them by name. A required input with no default refuses the run before it starts. The model can read them, so keep credentials in a workspace secret."
              : "Values the request that starts a session supplies — an API host, a reseller code — read as {{vars.name}} in the prompt, the greeting and this agent's tools. They reach every agent on the session, including one it hands off to. Unlike userdata they are read-only and are never kept on the caller's record, so they are the place for deployment settings rather than for facts about a person. A required one with no default refuses the session before it starts." +
                (channel === "voice"
                  ? " An inbound call carries no request at all, so anything needed on one must have a default — a phone number cannot be pointed at an agent that requires something without."
                  : "") +
                " The model can read them, so keep credentials in a workspace secret."
          }
        />
      </div>

      <div className="flex flex-col gap-2">
        {/* One header row rather than three labels per variable: with the
            default column empty on most rows, unlabelled inputs read as a
            key/value pair with a stray third box. */}
        <div
          className={cn(
            "grid items-center gap-2 text-[12px] font-medium text-faint",
            grid,
          )}
        >
          <span>Name</span>
          <span>Description</span>
          <span>Default</span>
          {withRequired && <span>Required</span>}
          <span />
        </div>
        {rows.map((declared, index) => (
          <div key={index} className={cn("grid items-center gap-2", grid)}>
            <Input
              aria-label="Variable name"
              className="font-mono text-[12.5px]"
              placeholder={isTask ? "email" : "api_domain"}
              value={declared.name}
              onChange={(e) => patch(index, { name: toVarName(e.target.value) })}
            />
            <Input
              aria-label="Variable description"
              className="min-w-0"
              placeholder={
                isTask
                  ? "The work email address to research."
                  : "Base URL of the customer's booking API."
              }
              value={declared.description ?? ""}
              onChange={(e) => patch(index, { description: e.target.value })}
            />
            <Input
              aria-label="Default value"
              className="font-mono text-[12.5px]"
              // Never "Needed", whatever Required says. Required with no default
              // is the strict case, not an unfinished one: `missing_required_vars`
              // refuses the session at the door when the request omits the value,
              // which is the entire point of the flag. A default only satisfies it
              // early. (The one place the pair is actually refused — assigning a
              // voice agent to a phone number — says so there, where it is true.)
              placeholder="Empty"
              value={declared.default ?? ""}
              // "" is not a default: a blank box means the author set none,
              // and null is what says so. A caller who wants a deliberate
              // blank sends "" in the request, which does override.
              onChange={(e) => patch(index, { default: e.target.value || null })}
            />
            {withRequired && (
              <div className="flex justify-start pl-1">
                <BoxCheckbox
                  checked={declared.required === true}
                  onChange={(required) => patch(index, { required })}
                  ariaLabel={`Require ${declared.name || "this variable"}`}
                />
              </div>
            )}
            <RowRemove
              onClick={() => removeRow(index)}
              ariaLabel={`Remove ${declared.name || "variable"}`}
            />
          </div>
        ))}
      </div>

      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-2">
        <Button
          variant="secondary"
          size="sm"
          // `rows`, not `vars`: with the blank row showing, appending to `vars`
          // would turn it real and add nothing visible — a button that looks
          // broken. This always puts one more row on screen.
          onClick={() => onChange([...rows, BLANK])}
        >
          {isTask ? "Add an input" : "Add a variable"}
        </Button>
        {/* Agent editor only. On the task editor this sits beside an Output
            section whose own Add button carries no such line, and the help dot
            above already says who supplies a value and what a missing one falls
            back to — so there it is the asymmetry, not the explanation. */}
        {!isTask && (
          <p className="text-[13px] leading-5 text-muted">
            Supplied by the request that starts a session; one left out falls back to its default.
          </p>
        )}
      </div>
    </div>
  );
}
