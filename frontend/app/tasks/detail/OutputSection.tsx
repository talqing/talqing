"use client";
import type { TaskOutputField } from "@talqing/sdk";
import { BoxCheckbox, Button, Input, Label, RowRemove, Select } from "@/app/components/ui";

/* The output field name is read back as a key in the run's result, and the API
   refuses anything outside `[a-z][a-z0-9_]*`. Normalising as the user types
   beats letting them find the rule as a save error on a field they finished
   with two minutes ago. */
function toFieldName(raw: string): string {
  return raw
    .toLowerCase()
    .replace(/[^a-z0-9_]+/g, "_")
    .replace(/^[^a-z]+/, "")
    .slice(0, 40);
}

const TYPES: NonNullable<TaskOutputField["type"]>[] = ["string", "boolean", "integer", "number"];

/** What the model must produce — the one thing an agent has no equivalent for.
 *
 *  Each field becomes one argument of the generated `submit_result` tool, and
 *  its description becomes that argument's description. So the description is
 *  not documentation: it is literally the only instruction the model gets about
 *  what belongs there, which is why the placeholder shows a whole sentence and
 *  an empty one is called out rather than left blank. */
export function OutputSection({
  output,
  onChange,
}: {
  output: TaskOutputField[];
  onChange: (next: TaskOutputField[]) => void;
}) {
  function patch(index: number, next: Partial<TaskOutputField>) {
    onChange(output.map((f, i) => (i === index ? { ...f, ...next } : f)));
  }

  return (
    <div className="flex flex-col gap-3">
      <div className="grid grid-cols-[190px_150px_minmax(0,1fr)_84px_36px] items-center gap-2 text-[12px] font-medium text-faint">
        <span>Name</span>
        <span>Type</span>
        <span>Description</span>
        <span>Required</span>
        <span />
      </div>

      {output.map((field, index) => (
        <div key={index} className="grid grid-cols-[190px_150px_minmax(0,1fr)_84px_36px] items-start gap-2">
          <Input
            aria-label="Field name"
            className="font-mono text-[12.5px]"
            placeholder="subject"
            value={field.name}
            onChange={(e) => patch(index, { name: toFieldName(e.target.value) })}
          />
          <Select
            aria-label="Field type"
            value={field.type ?? "string"}
            onChange={(e) => patch(index, { type: e.target.value as TaskOutputField["type"] })}
          >
            {TYPES.map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </Select>
          <div className="flex min-w-0 flex-col gap-1">
            <Input
              aria-label="What the model should put in this field"
              className="min-w-0"
              placeholder="A subject line under 60 characters that names the company."
              value={field.description}
              onChange={(e) => patch(index, { description: e.target.value })}
            />
            {/* Not a blank — a quality bug. The model is handed this sentence
                and nothing else about the field. */}
            {!field.description.trim() && (
              <span className="text-[11.5px] leading-4 text-warn">
                Say what belongs here — the model gets this sentence and the field name, nothing more.
              </span>
            )}
          </div>
          {/* Off means null is an accepted answer — "I could not find it".
              The field is sent either way; this only decides whether an
              empty answer passes. */}
          <div className="flex justify-start pl-1 pt-2">
            <BoxCheckbox
              checked={field.required !== false}
              onChange={(required) => patch(index, { required })}
              ariaLabel={`Require ${field.name || "this field"}`}
            />
          </div>
          {/* A task with no output is not a task, and the API refuses one,
              so the last field cannot be removed. */}
          <RowRemove
            onClick={() => onChange(output.filter((_, i) => i !== index))}
            ariaLabel={`Remove ${field.name || "field"}`}
            disabled={output.length <= 1}
            title={output.length <= 1 ? "A task has to produce at least one field" : undefined}
          />
        </div>
      ))}

      {/* `self-start` because the column stretches its children: standing alone
          now that the note beside it is gone, the button would run the width of
          the panel. Matches "Add an input" above it. */}
      <Button
        variant="secondary"
        size="sm"
        className="self-start"
        onClick={() => onChange([...output, { name: "", type: "string", description: "", required: true }])}
      >
        Add a field
      </Button>
    </div>
  );
}

/** The names this task uses, for the collision the API refuses at save: a name
 *  cannot be both an input and an output, because a run's inputs and its result
 *  are read side by side. Surfaced in the editor so the save is not the first
 *  place it is heard about. */
export function nameCollisions(varNames: string[], output: TaskOutputField[]): string[] {
  const inputs = new Set(varNames.filter(Boolean));
  return [...new Set(output.map((f) => f.name).filter((name) => name && inputs.has(name)))];
}

/** Duplicates within the output list itself — the later entry would silently win. */
export function duplicateFields(output: TaskOutputField[]): string[] {
  const seen = new Set<string>();
  const dupes = new Set<string>();
  for (const field of output) {
    if (!field.name) continue;
    if (seen.has(field.name)) dupes.add(field.name);
    seen.add(field.name);
  }
  return [...dupes];
}
