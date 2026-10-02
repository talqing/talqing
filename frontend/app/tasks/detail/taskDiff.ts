import type { JsonValue, McpSelection, TaskConfig, ToolSelection } from "@talqing/sdk";
import {
  same,
  type ConfigDiff,
  type ConfigSection,
  type FieldChange,
} from "@/app/components/diff";
import { faqChange, shownLeaf, toolChanges, unpinnedEffort } from "@/app/agents/detail/agentDiff";

/* ── comparing two task definitions ──────────────────────────────────────────
   `agentDiff.ts`'s smaller sibling, and the same argument for walking the config
   to its leaves rather than hand-listing fields: a hand-written table would
   quietly omit whatever field `TaskConfig` grows next.

   There is deliberately no config sanitizer: both sides arrive through the same
   pydantic model with every default filled in, and `stableKey` already sorts
   keys so JSONB's reordering is not a diff. A spurious change showing up here
   means a missing default on `TaskConfig` — fix it there. */

const SECTIONS: { title: string; keys: string[] }[] = [
  { title: "Identity & prompt", keys: ["name", "prompt", "timezone", "vars"] },
  { title: "Model", keys: ["llm"] },
  {
    title: "Output",
    keys: ["output", "submit_result_description", "finish_without_result_description"],
  },
  { title: "Tools & integrations", keys: ["tools", "mcps", "faqs", "on_enter", "on_exit", "on_user_turn_completed"] },
  { title: "Limits", keys: ["max_steps", "timeout_seconds"] },
];

const SECTION_OF: Record<string, string> = Object.fromEntries(
  SECTIONS.flatMap((s) => s.keys.map((k) => [k, s.title])),
);

/** Anything a future field lands in until it is given a home. Better a change
    shown under a vague heading than a change not shown. */
const OTHER = "Other";

const LABELS: Record<string, string> = {
  name: "Name",
  prompt: "System prompt",
  timezone: "Timezone",
  vars: "Inputs",
  output: "Output fields",
  submit_result_description: "submit_result description",
  finish_without_result_description: "finish_without_result description",
  mcps: "MCP servers",
  on_enter: "On enter",
  on_exit: "On exit",
  on_user_turn_completed: "After each user turn",
  max_steps: "Steps",
  timeout_seconds: "Timeout",
  "llm.builtin_tools": "LLM · Tools the model runs itself",
  "llm.fallback.builtin_tools": "LLM · Fallback · Tools the model runs itself",
  "llm.hosts": "LLM · Hosts",
  "llm.fallback.hosts": "LLM · Fallback · Hosts",
};

const HUMAN: Record<string, string> = { llm: "LLM" };

function label(path: string): string {
  if (LABELS[path]) return LABELS[path];
  return path
    .split(".")
    .map((part) => HUMAN[part] ?? part.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase()))
    .join(" · ");
}

/** Every leaf of a config, as `path -> value`. Arrays are leaves: `vars` and
    `output` are ordered lists whose order the author chose, and the two that
    compare as sets (`tools`, `mcps`) are pulled out before this runs. */
function leaves(value: unknown, prefix: string, out: Map<string, JsonValue>): void {
  if (value !== null && typeof value === "object" && !Array.isArray(value)) {
    for (const [key, child] of Object.entries(value as Record<string, unknown>)) {
      leaves(child, prefix ? `${prefix}.${key}` : key, out);
    }
    return;
  }
  out.set(prefix, value as JsonValue);
}

/** Handled by name rather than walked to their leaves — walking them would print
    `tools.0.tool_id: 3f8a1c2e-… → 91dcfead-…`, which names nothing, and would
    treat a reordered list as a rewrite. */
const HOOK_KEYS = ["on_enter", "on_exit", "on_user_turn_completed"] as const;

const SET_KEYS = new Set<string>(["tools", "mcps", "faqs", ...HOOK_KEYS]);

/** A hook, as one readable value: the tool's name, plus the version once the
    task is published, so a tool republish between two versions is visible. */
function hookText(
  sel: ToolSelection | null | undefined,
  nameById: (id: string) => string | null,
): JsonValue {
  if (!sel) return null;
  if (!sel.tool_id) return sel.tool ? `${sel.tool.name} (inline)` : null;
  const shown = nameById(sel.tool_id) ?? `${sel.tool_id} (deleted)`;
  return sel.tool_version == null ? shown : `${shown} (v${sel.tool_version})`;
}

/** MCP servers, compared as a set and rendered by the integration's name — or,
    for a server defined inline, by its own. */
function mcpChange(
  before: TaskConfig,
  after: TaskConfig,
  nameById: (id: string) => string,
): FieldChange | null {
  const key = (sel: McpSelection) =>
    sel.integration_id ? nameById(sel.integration_id) : `${sel.mcp?.name ?? "?"} (inline)`;
  const a = (before.mcps ?? []).map(key);
  const b = (after.mcps ?? []).map(key);
  if (same([...a].sort(), [...b].sort())) return null;
  return { field: label("mcps"), before: a, after: b };
}

export function diffTaskConfigs(
  before: TaskConfig,
  after: TaskConfig,
  names: {
    tool: (id: string) => string | null;
    integration: (id: string) => string;
    faq: (id: string) => string;
  },
): ConfigDiff {
  const a = new Map<string, JsonValue>();
  const b = new Map<string, JsonValue>();
  for (const [key, value] of Object.entries(before)) if (!SET_KEYS.has(key)) leaves(value, key, a);
  for (const [key, value] of Object.entries(after)) if (!SET_KEYS.has(key)) leaves(value, key, b);

  const bySection = new Map<string, FieldChange[]>();
  for (const path of new Set([...a.keys(), ...b.keys()])) {
    const before = shownLeaf(path, a.get(path));
    const after = shownLeaf(path, b.get(path));
    if (same(before, after) || unpinnedEffort(path, before, after)) continue;
    const section = SECTION_OF[path.split(".")[0]] ?? OTHER;
    const list = bySection.get(section) ?? [];
    list.push({ field: label(path), before, after });
    bySection.set(section, list);
  }
  for (const list of bySection.values()) list.sort((x, y) => (x.field < y.field ? -1 : 1));

  const attachments: FieldChange[] = [];
  const mcp = mcpChange(before, after, names.integration);
  if (mcp) attachments.push(mcp);
  const faqs = faqChange(before, after, names.faq);
  if (faqs) attachments.push(faqs);
  for (const hook of HOOK_KEYS) {
    const x = hookText(before[hook], names.tool);
    const y = hookText(after[hook], names.tool);
    if (!same(x, y)) attachments.push({ field: label(hook), before: x, after: y });
  }
  if (attachments.length) {
    const title = SECTION_OF["mcps"];
    bySection.set(title, [...(bySection.get(title) ?? []), ...attachments]);
  }

  const tools = toolChanges(before, after, names.tool);
  const sections: ConfigSection[] = SECTIONS.map((s) => ({
    title: s.title,
    changes: bySection.get(s.title) ?? [],
  }))
    .concat({ title: OTHER, changes: bySection.get(OTHER) ?? [] })
    .filter((s) => s.changes.length > 0);
  return { changed: sections.length > 0 || tools.length > 0, sections, tools };
}
