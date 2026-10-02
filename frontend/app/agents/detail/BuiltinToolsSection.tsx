"use client";

import { useState } from "react";
import type {
  BuiltinTool,
  BuiltinToolOption,
  BuiltinToolSpec,
  JsonObject,
  JsonValue,
  LLMCatalogEntryResponse,
} from "@talqing/sdk";
import { BoxCheckbox, Button, HelpDot, Input, Label, Select } from "@/app/components/ui";
import { cn } from "@/lib/cn";
import { DatePicker } from "@/app/components/DatePicker";
import { ToggleRow } from "./ToggleRow";

/* The tools the model provider runs itself, mid-turn, on its own servers.
   They belong to the model rather than to the agent, which is why they live in
   the Models card next to the model that offers them and not beside the tenant's
   own tools — there is nothing to author, publish or version, only a switch.

   Every row here is rendered from the catalog entry, options included, so
   offering a new tool (or a new provider) is a catalog edit. Nothing in this
   file knows what web search is — and, since the task editor renders the same
   section, nothing in it knows what a channel or a step is either: what running
   one costs the caller is the one sentence its owner passes in. */

function optionValue(spec: BuiltinToolSpec, key: string): unknown {
  return (spec.config as JsonObject | undefined)?.[key];
}

function withOption(spec: BuiltinToolSpec, key: string, value: JsonValue): BuiltinToolSpec {
  return { ...spec, config: { ...(spec.config as JsonObject | undefined), [key]: value } };
}

/** The conflicting option that is already set, if any — an allow-list next to a
 *  block-list of the same thing, which the provider refuses in one request. */
function blockedBy(
  tool: BuiltinTool,
  spec: BuiltinToolSpec,
  option: BuiltinToolOption,
): BuiltinToolOption | null {
  const conflicting = (option.conflicts_with ?? []).find(
    (key) => !isBlank(optionValue(spec, key)),
  );
  return (tool.options ?? []).find((o) => o.key === conflicting) ?? null;
}

/** Whether an option is unset. A switch turned off is a real value, not blank. */
function isBlank(value: unknown): boolean {
  return value === undefined || value === null || value === "" || (Array.isArray(value) && !value.length);
}

/** A list of short strings — domains, X handles, store ids — added one at a
 *  time and shown as removable chips.
 *
 *  Deliberately the same interaction as the enum-values editor in the tool
 *  builder: both are "a set of literals the author types out", so an author who
 *  has built a tool already knows this field. It also sidesteps what a single
 *  comma-separated box does to a controlled input, where the separator you have
 *  just typed parses to an empty entry and disappears from under the cursor. */
function StringListInput({
  value,
  maxItems,
  disabled,
  onChange,
}: {
  value: string[];
  maxItems?: number | null;
  disabled?: boolean;
  onChange: (value: string[]) => void;
}) {
  const [draft, setDraft] = useState("");
  const full = maxItems != null && value.length >= maxItems;

  function add(): void {
    const next = draft.trim();
    if (!next || full) return;
    if (!value.includes(next)) onChange([...value, next]);
    setDraft("");
  }

  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-center gap-2">
        <Input
          value={draft}
          disabled={disabled || full}
          placeholder={full ? `${maxItems} is the limit` : "one at a time"}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key !== "Enter") return;
            // This sits inside the agent form: Enter adds a value here rather
            // than saving the whole draft.
            e.preventDefault();
            add();
          }}
          className="min-h-9 max-w-[260px] py-1.5 font-mono text-[13px]"
        />
        <Button
          type="button"
          variant="secondary"
          size="sm"
          onClick={add}
          disabled={disabled || full || !draft.trim()}
        >
          Add
        </Button>
      </div>
      {value.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {value.map((item) => (
            <span
              key={item}
              className="inline-flex max-w-full items-center gap-1.5 rounded-md border border-line-2 bg-white py-1 pl-2 pr-1 font-mono text-[12.5px] leading-4 text-ink"
            >
              <span className="min-w-0 truncate">{item}</span>
              <button
                type="button"
                disabled={disabled}
                className="grid h-4 w-4 flex-none place-items-center rounded text-muted transition-colors hover:bg-subtle hover:text-danger"
                onClick={() => onChange(value.filter((v) => v !== item))}
                aria-label={`Remove ${item}`}
              >
                <svg
                  className="h-2.5 w-2.5"
                  viewBox="0 0 12 12"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="1.8"
                  strokeLinecap="round"
                  aria-hidden
                >
                  <path d="m3 3 6 6M9 3 3 9" />
                </svg>
              </button>
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

/** How wide a field needs to be is a property of what it holds, not of the card
 *  it sits in — the same rule the turn-handling tuners follow. A date is eight
 *  characters; a domain list is a phrase; a count is three digits. Sizing them
 *  individually is what lets several share a row. */
// Options whose value is an ISO 8601 date. The catalog says `string` because
// that is what goes on the wire; a calendar is what stops an author typing
// "31/01/2026" and learning it was wrong from a provider error mid-call.
const DATE_KEYS = new Set(["from_date", "to_date"]);

const OPTION_WIDTH: Record<BuiltinToolOption["type"], string> = {
  string_list: "min-w-[264px]",
  enum: "min-w-[172px]",
  string: "min-w-[170px]",
  integer: "min-w-[104px]",
  boolean: "",
};

/** One option, rendered from the shape its catalog entry declares.
 *
 *  `blockedBy` is the conflicting option that is already filled in — an
 *  allow-list next to a block-list of the same thing, which the provider
 *  refuses in one request. The field is disabled rather than left to fail on
 *  save, so the exclusivity is visible at the moment it starts applying. */
function OptionField({
  option,
  value,
  blockedBy,
  onChange,
}: {
  option: BuiltinToolOption;
  value: unknown;
  blockedBy: BuiltinToolOption | null;
  onChange: (value: JsonValue) => void;
}) {
  const disabled = blockedBy !== null;

  /* Whether the field is needed leads its tooltip, ahead of what it does — it
     is the first thing an author has to know and the shortest thing to say. It
     lives here rather than on the label so a row of eight fields reads as eight
     names and not eight names plus eight qualifiers; the trade is that it takes
     a hover. Every option carries a dot, so there is nowhere it goes missing. */
  const help = [
    option.required ? "Required." : "Optional.",
    blockedBy ? `Not available while “${blockedBy.label}” is set.` : option.description,
  ]
    .filter(Boolean)
    .join(" ");

  if (option.type === "boolean") {
    // The compact checkbox, not the full-width ToggleRow: that one is for the
    // tools themselves, and an option is a detail of one. Wrapped in the same
    // column shape as a labelled field, with an empty line where their label
    // sits, so the switch lands on the control line of the fields beside it.
    return (
      <div className="flex flex-none flex-col gap-2">
        <span aria-hidden className="h-4" />
        <div className="flex h-9 items-center gap-2 whitespace-nowrap text-[13px] text-ink-soft">
          <BoxCheckbox checked={value === true} onChange={onChange} ariaLabel={option.label} />
          <button type="button" className="text-left" onClick={() => onChange(value !== true)}>
            {option.label}
          </button>
          <HelpDot label={help} />
        </div>
      </div>
    );
  }

  return (
    <div className={cn("flex flex-none flex-col gap-2", OPTION_WIDTH[option.type], disabled && "opacity-50")}>
      {/* The label never wraps: a field is at least as wide as its own name, so
          a two-word label cannot fold and knock the row out of alignment. */}
      <span className="flex items-center gap-1.5 whitespace-nowrap">
        <Label className="text-[13px] font-medium leading-4 text-ink-soft">{option.label}</Label>
        <HelpDot label={help} className="flex-none" />
      </span>
      {option.type === "enum" ? (
        <Select
          value={typeof value === "string" ? value : ""}
          disabled={disabled}
          onChange={(e) => onChange(e.target.value)}
        >
          {/* The provider has its own default, and leaving this blank is how the
              author says "use it" rather than picking on the provider's behalf. */}
          <option value="">Default</option>
          {(option.choices ?? []).map((choice) => (
            <option key={choice} value={choice}>
              {choice}
            </option>
          ))}
        </Select>
      ) : option.type === "string_list" ? (
        <StringListInput
          value={Array.isArray(value) ? (value as string[]) : []}
          maxItems={option.max_items}
          disabled={disabled}
          onChange={onChange}
        />
      ) : DATE_KEYS.has(option.key) ? (
        <DatePicker
          value={typeof value === "string" ? value : null}
          disabled={disabled}
          ariaLabel={option.label}
          onChange={(next) => onChange(next ?? "")}
        />
      ) : (
        <Input
          type={option.type === "integer" ? "number" : "text"}
          disabled={disabled}
          value={typeof value === "string" || typeof value === "number" ? value : ""}
          onChange={(e) => {
            if (option.type !== "integer") return onChange(e.target.value);
            // An emptied number box means "unset", not zero.
            const next = e.target.value.trim();
            onChange(next === "" ? "" : Number(next));
          }}
        />
      )}
    </div>
  );
}

function ToolRow({
  tool,
  spec,
  runtimeNote,
  onChange,
}: {
  tool: BuiltinTool;
  spec: BuiltinToolSpec | undefined;
  runtimeNote?: string;
  onChange: (spec: BuiltinToolSpec | null) => void;
}) {
  const on = spec !== undefined;
  const options = tool.options ?? [];

  // Everything this row has to say lives on its help dot, which is where the
  // rest of the editor keeps its explanations. That includes what running one
  // costs where this section is being rendered — dead air on a call, seconds off
  // a task's clock. It is a warning rather than a block: the author may want it
  // anyway.
  const help = [tool.description, tool.price_note, runtimeNote].filter(Boolean).join(" ");

  return (
    <div className="border-b border-line last:border-b-0">
      {/* The row boundary is this wrapper's job, so the switch does not draw a
          second one. It was drawing one anyway — and `last:` was zeroing it on
          some rows and not others, which is why the list had dividers of two
          different weights. */}
      <ToggleRow
        checked={on}
        onChange={(next) => onChange(next ? { type: tool.type, config: {} } : null)}
        label={tool.label}
        help={help}
        divider={false}
      />
      {on && options.length > 0 && (
        // A recessed band, bled to the card edges. On the white surface these
        // fields floated: nothing said they belonged to the switch above rather
        // than to the list, and an indent alone is a weak claim. It is the same
        // treatment the tool editor gives an argument's enum values. A tool with
        // nothing to configure (xAI's code interpreter) shows the switch alone —
        // an empty band would read as a loading state.
        <div className="-mx-5 border-t border-line bg-canvas px-5 py-3.5">
          {/* One flowing row for everything a tool takes, in catalog order.
              Each field is only as wide as what it holds, so four options are
              one line rather than four. Indented past the checkbox, so the band
              still points back at the switch that opened it. */}
          <div className="flex flex-wrap items-start gap-x-5 gap-y-3.5 pl-8">
            {options.map((option) => (
              <OptionField
                key={option.key}
                option={option}
                value={optionValue(spec, option.key)}
                blockedBy={blockedBy(tool, spec, option)}
                onChange={(value) => onChange(withOption(spec, option.key, value))}
              />
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

/** The built-in tools of the selected model, or nothing at all when it has none.
 *
 *  `runtimeNote` is appended to every row's help: the same switch costs dead air
 *  on a call and seconds of a task's timeout, and only the caller knows which. */
export function BuiltinToolsSection({
  entry,
  value,
  runtimeNote,
  onChange,
}: {
  entry: LLMCatalogEntryResponse | undefined;
  value: BuiltinToolSpec[];
  runtimeNote?: string;
  onChange: (next: BuiltinToolSpec[]) => void;
}) {
  const tools = entry?.builtin_tools ?? [];
  if (!tools.length) return null;

  /* Rebuilt in catalog order rather than appended to, so switching a tool off
     and back on produces the same list it started with — otherwise the publish
     diff would report a reorder as a change. */
  function set(type: string, spec: BuiltinToolSpec | null) {
    const next = new Map(value.map((t) => [t.type, t]));
    if (spec) next.set(type, spec);
    else next.delete(type);
    onChange(tools.flatMap((tool) => next.get(tool.type) ?? []));
  }

  return (
    <div className="mt-3.5">
      <span className="flex items-center gap-1.5">
        <Label>Provider tools</Label>
        <HelpDot label="Run by the model provider during the reply, not by Talqing. Each one is billed by the provider per call, on top of tokens — Talqing's figures cover tokens only." />
      </span>
      <div className="mt-1.5">
        {tools.map((tool) => (
          <ToolRow
            key={tool.type}
            tool={tool}
            spec={value.find((t) => t.type === tool.type)}
            runtimeNote={runtimeNote}
            onChange={(spec) => set(tool.type, spec)}
          />
        ))}
      </div>
    </div>
  );
}
