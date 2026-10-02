import type {
  AgentConfig,
  FaqSelection,
  HandoffTarget,
  JsonValue,
  McpSelection,
  TaskSelection,
  ToolSelection,
} from "@talqing/sdk";
import {
  same,
  type ConfigDiff,
  type ConfigSection,
  type FieldChange,
  type Status,
  type ToolChange,
} from "@/app/components/diff";

/* ── comparing two agent definitions ────────────────────────────────────────
   An agent is one `AgentConfig` object, so this walks the two configs to their
   leaves rather than hand-listing fields. That is deliberate: `AgentConfig`
   gains fields steadily — recording, analysis and avatar all arrived recently —
   and a hand-written table would quietly omit whatever it had not caught up
   with, which is the one failure a diff must not have.

   Both sides come through `AgentConfig`, so the same validator has normalized
   them and a difference here is a real difference, not one the channel
   normalizer invented. */

/** Which section a top-level config key belongs to, in reading order. The keys
    a section claims are listed rather than inferred, but a key that belongs to
    no section still shows up — see `SECTION_OF` below. */
const SECTIONS: { title: string; keys: string[] }[] = [
  { title: "Identity & prompt", keys: ["name", "channel", "prompt", "greeting", "greeting_interruptible", "language", "timezone", "vars"] },
  { title: "Models", keys: ["stt", "llm", "tts", "realtime"] },
  { title: "Vision input", keys: ["vision_input"] },
  { title: "Keypad input", keys: ["keypad_input"] },
  { title: "Voicemail", keys: ["voicemail_detection"] },
  { title: "Turn handling", keys: ["turn_handling", "noise_cancellation"] },
  { title: "Tools & integrations", keys: ["tools", "tasks", "mcps", "faqs", "on_enter", "on_exit", "on_user_turn_completed"] },
  { title: "Limits", keys: ["max_steps", "max_duration_seconds", "silence"] },
  { title: "Session behaviour", keys: ["background_audio", "recording", "analysis", "conversation", "avatar"] },
];

const SECTION_OF: Record<string, string> = Object.fromEntries(
  SECTIONS.flatMap((s) => s.keys.map((k) => [k, s.title])),
);

/** Anything a future field lands in until it is given a home. Better a change
    shown under a vague heading than a change not shown. */
const OTHER = "Other";

/** Names for the paths worth naming well. Anything missing falls back to a
    humanized path, so a new field reads acceptably rather than wrongly. */
const LABELS: Record<string, string> = {
  name: "Name",
  channel: "Channel",
  prompt: "System prompt",
  greeting: "Greeting",
  greeting_interruptible: "Caller can interrupt the greeting",
  language: "Language",
  timezone: "Timezone",
  vars: "Variables",
  mcps: "MCP servers",
  faqs: "FAQs",
  handoffs: "Handoffs",
  tasks: "Tasks",
  max_steps: "Maximum steps per turn",
  max_duration_seconds: "Max call length",
  "silence.enabled": "Check in when the caller goes quiet",
  "silence.timeout": "Silence · Check in after",
  "silence.max_check_ins": "Silence · Check-ins before hanging up",
  on_enter: "On enter",
  on_exit: "On exit",
  on_user_turn_completed: "After each user turn",
  "turn_handling.endpointing.min_silence_duration": "Endpointing · Min silence",
  "turn_handling.endpointing.max_silence_duration": "Endpointing · Max silence",
  "llm.builtin_tools": "LLM · Tools the model runs itself",
  "llm.fallback.builtin_tools": "LLM · Fallback · Tools the model runs itself",
  "llm.hosts": "LLM · Hosts",
  "llm.fallback.hosts": "LLM · Fallback · Hosts",
  "analysis.model.hosts": "Analysis · Model · Hosts",
  "noise_cancellation.enabled": "Noise cancellation",
  "noise_cancellation.enhancement_level": "Noise cancellation · Strength",
  "vision_input.screenshare.enabled": "Screen share",
  "vision_input.screenshare.record": "Screen share · Record video",
  "keypad_input.enabled": "Keypad input",
  "keypad_input.timeout": "Keypad input · Quiet timeout",
  "keypad_input.terminator": "Keypad input · Terminator key",
  "voicemail_detection.enabled": "Hang up on voicemail",
  "voicemail_detection.message": "Voicemail · Message to leave",
  "conversation.context": "Past conversations",
  "conversation.summary_limit": "Past conversations · Calls to summarize",
  "conversation.initialize_userdata": "Past conversations · Initialize userdata",
};

const HUMAN: Record<string, string> = {
  stt: "Speech to text",
  tts: "Text to speech",
  llm: "LLM",
  realtime: "Realtime",
  turn_handling: "Turn handling",
  background_audio: "Background audio",
  noise_cancellation: "Noise cancellation",
  tts_provider: "Provider",
};

function label(path: string): string {
  if (LABELS[path]) return LABELS[path];
  return path
    .split(".")
    .map((part) => HUMAN[part] ?? part.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase()))
    .join(" · ");
}

/** Every leaf of a config, as `path -> value`. Arrays are leaves: their order
    can carry meaning, and the ones that do not (`tools`, `mcps`) are pulled
    out and compared as sets before this runs. */
function leaves(value: unknown, prefix: string, out: Map<string, JsonValue>): void {
  if (value !== null && typeof value === "object" && !Array.isArray(value)) {
    for (const [key, child] of Object.entries(value as Record<string, unknown>)) {
      leaves(child, prefix ? `${prefix}.${key}` : key, out);
    }
    return;
  }
  out.set(prefix, value as JsonValue);
}

/** A leaf as the diff shows it. A host set reads as one line of tags, and its
    absence as what it means — any host — rather than as an empty value.
    Exported for `taskDiff`, whose LLM carries the same field. */
export function shownLeaf(path: string, value: JsonValue | undefined): JsonValue | undefined {
  if (!path.endsWith(".hosts")) return value;
  return Array.isArray(value) ? value.join(", ") : "Automatic";
}

/** An unset thinking level against a set one: not a change. Publishing pins an
    unset level to the model's fastest, so the draft carries null where every
    published version carries a value — the same shape as `tool_version`, and
    compared under the same rule. A model switch still shows, on its own line.
    Exported for `taskDiff`, whose LLM pins the same way. */
export function unpinnedEffort(path: string, before: JsonValue | undefined, after: JsonValue | undefined): boolean {
  return path.endsWith(".reasoning_effort") && (before == null || after == null);
}

const HOOK_KEYS = ["on_enter", "on_exit", "on_user_turn_completed"] as const;

/** Keys handled by name rather than walked to their leaves — walking them would
    print `on_enter.tool_id: 3f8a1c2e-… → 91dcfead-…`, which names nothing, and
    would treat a reordered list as a rewrite. */
const SET_KEYS = new Set<string>(["tools", "mcps", "faqs", "handoffs", "tasks", ...HOOK_KEYS]);

/** A hook, as one readable value: the tool's name, plus the version once the
    agent is published, so a tool republish between two versions is visible. A
    tool defined inline on the call carries its own name and has no version. */
function hookText(
  sel: ToolSelection | null | undefined,
  nameById: (id: string) => string | null,
): JsonValue {
  if (!sel) return null;
  if (!sel.tool_id) return sel.tool ? `${sel.tool.name} (inline)` : null;
  const name = nameById(sel.tool_id);
  const shown = name ?? `${sel.tool_id} (deleted)`;
  return sel.tool_version == null ? shown : `${shown} (v${sel.tool_version})`;
}

/** The outgoing handoff edges, as one readable line each: where it goes, what
    the model routes on, and what crosses the boundary. Compared as a set, keyed
    by the destination's name — which is the stable thing about an edge, and the
    one the model sees.

    The context policy is on the line because without it a destination moving
    from `transcript` to `summary` — a change to how much of the conversation the
    next agent can see — showed up as no change at all. The summary prompt is on
    it for the same reason: it is what the model is told to write. */
function handoffChange(
  before: AgentConfig,
  after: AgentConfig,
  nameById: (id: string) => string,
): FieldChange | null {
  const line = (t: HandoffTarget) => {
    const context = t.context ?? "transcript";
    const carried =
      context === "summary"
        ? `a summary + ${t.recent_turns ?? 2} recent turns`
        : context === "none"
          ? t.recent_turns
            ? `${t.recent_turns} recent turns only`
            : "nothing"
          : "the conversation so far";
    const prompt = t.summary_prompt ? `, asked to write: ${t.summary_prompt}` : "";
    return (
      `${t.name} → ${t.agent_id ? nameById(t.agent_id) : "a team member"}: ${t.description}` +
      ` (starts from ${carried}${prompt})`
    );
  };
  const a = (before.handoffs ?? []).map(line);
  const b = (after.handoffs ?? []).map(line);
  if (same([...a].sort(), [...b].sort())) return null;
  return { field: label("handoffs"), before: a, after: b };
}

/** The task attachments, as one readable line each, compared as a set keyed by
    the tool name — which is the stable thing about an attachment and the one the
    model sees. The pinned version is on the line for the same reason a hook's
    is: a task republished between two agent versions is a real change to what
    the model is shown, since the task's variables ARE the tool's schema. */
function taskChange(
  before: AgentConfig,
  after: AgentConfig,
  nameById: (id: string) => string,
): FieldChange | null {
  const line = (t: TaskSelection) => {
    const target = t.task
      ? `${t.task.name} (inline)`
      : nameById(t.task_id ?? "") + (t.task_version == null ? "" : ` (v${t.task_version})`);
    const said = t.message ? `, says: ${t.message}` : "";
    return `${t.name} → ${target}: ${t.description}${said}`;
  };
  const a = (before.tasks ?? []).map(line);
  const b = (after.tasks ?? []).map(line);
  if (same([...a].sort(), [...b].sort())) return null;
  return { field: label("tasks"), before: a, after: b };
}

function fieldChanges(before: AgentConfig, after: AgentConfig): Map<string, FieldChange[]> {
  const a = new Map<string, JsonValue>();
  const b = new Map<string, JsonValue>();
  for (const [key, value] of Object.entries(before)) {
    if (!SET_KEYS.has(key)) leaves(value, key, a);
  }
  for (const [key, value] of Object.entries(after)) {
    if (!SET_KEYS.has(key)) leaves(value, key, b);
  }

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
  return bySection;
}

/** MCP servers, compared as a set and rendered by the integration's name — or,
    for a server defined on the call, by its own. Keyed on that name rather than
    on the whole selection, so a selection that grows a second field later does
    not read as every attachment being rewritten. */
function mcpChange(
  before: AgentConfig,
  after: AgentConfig,
  nameById: (id: string) => string,
): FieldChange | null {
  const key = (sel: McpSelection) =>
    sel.integration_id ? nameById(sel.integration_id) : `${sel.mcp?.name ?? "?"} (inline)`;
  const a = (before.mcps ?? []).map(key);
  const b = (after.mcps ?? []).map(key);
  if (same([...a].sort(), [...b].sort())) return null;
  return { field: label("mcps"), before: a, after: b };
}

/** FAQs, compared as a set and rendered by name — the FAQ's own, or the inline
    one's on a call that carried it. Only WHICH are attached: their content is
    live and is not part of any version. Shared with the task diff. */
export function faqChange(
  before: { faqs?: FaqSelection[] },
  after: { faqs?: FaqSelection[] },
  nameById: (id: string) => string,
): FieldChange | null {
  const key = (sel: FaqSelection) =>
    sel.faq_id ? nameById(sel.faq_id) : `${sel.faq?.name ?? "?"} (inline)`;
  const a = (before.faqs ?? []).map(key);
  const b = (after.faqs ?? []).map(key);
  if (same([...a].sort(), [...b].sort())) return null;
  return { field: "FAQs", before: a, after: b };
}

/** The attached tools, keyed by tool id so a reorder is not a rewrite.
    `tool_version` is null on a draft and set on a published version, so a
    comparison involving the draft is about attachment only — never render
    `v3 → —`, which would read as a downgrade when it only means "the draft
    tracks latest".

    Exported because a task's tools pin under exactly the same rule; it takes
    anything with a `tools` list rather than an `AgentConfig`, so `taskDiff`
    calls it rather than keeping a second copy of that rule to get wrong. */
export function toolChanges(
  before: { tools?: ToolSelection[] | null },
  after: { tools?: ToolSelection[] | null },
  nameById: (id: string) => string | null,
): ToolChange[] {
  /* A tool defined on the call has no row and no id, so its name IS its
     identity — and it carries its own name, which is why it never falls to the
     `nameById` lookup below and never reads as deleted. */
  const keyed = (sels: ToolSelection[]) =>
    new Map(
      sels.map((sel): [string, { version: number | null; inlineName: string | null }] => [
        sel.tool_id ?? `inline:${sel.tool?.name}`,
        { version: sel.tool_version ?? null, inlineName: sel.tool_id ? null : (sel.tool?.name ?? null) },
      ]),
    );
  const a = keyed(before.tools ?? []);
  const b = keyed(after.tools ?? []);
  const out: ToolChange[] = [];
  for (const tool_id of new Set([...a.keys(), ...b.keys()])) {
    const inA = a.has(tool_id);
    const inB = b.has(tool_id);
    const beforeVersion = a.get(tool_id)?.version ?? null;
    const afterVersion = b.get(tool_id)?.version ?? null;
    /* A null version is the draft saying "whatever is published now", which
       cannot be said to differ from a pin — so only two *pinned* sides can
       report a version change. Without this every published agent would read as
       differing from its own live version, since the draft is never pinned. */
    const bumped = beforeVersion != null && afterVersion != null && beforeVersion !== afterVersion;
    const status: Status = !inA ? "added" : !inB ? "removed" : bumped ? "changed" : "unchanged";
    if (status === "unchanged") continue;
    const inlineName = (b.get(tool_id) ?? a.get(tool_id))?.inlineName ?? null;
    const name = inlineName ?? nameById(tool_id);
    out.push({
      status,
      tool_id,
      name: name ?? tool_id,
      deleted: inlineName === null && name === null,
      before: beforeVersion,
      after: afterVersion,
    });
  }
  const rank: Record<string, number> = { added: 0, removed: 1, changed: 2, unchanged: 3 };
  // Attaching or detaching a tool changes what the model can do; a version bump
  // changes only how one of them behaves.
  return out.sort((x, y) => rank[x.status] - rank[y.status] || (x.name < y.name ? -1 : 1));
}

export function diffAgentConfigs(
  before: AgentConfig,
  after: AgentConfig,
  names: {
    tool: (id: string) => string | null;
    integration: (id: string) => string;
    faq: (id: string) => string;
  },
): ConfigDiff {
  const bySection = fieldChanges(before, after);
  const attachments: FieldChange[] = [];
  for (const hook of HOOK_KEYS) {
    const a = hookText(before[hook], names.tool);
    const b = hookText(after[hook], names.tool);
    if (!same(a, b)) attachments.push({ field: label(hook), before: a, after: b });
  }
  const mcp = mcpChange(before, after, names.integration);
  if (mcp) attachments.push(mcp);
  const faqs = faqChange(before, after, names.faq);
  if (faqs) attachments.push(faqs);
  const handoffs = handoffChange(before, after, (id) => names.tool(id) ?? id);
  if (handoffs) attachments.push(handoffs);
  // Named through the same resolver the rest use, which knows agents and tools
  // and not tasks — so a task shows as its id. Better an id than a wrong name:
  // the line's job here is to say THAT the attachment changed.
  const tasks = taskChange(before, after, (id) => id);
  if (tasks) attachments.push(tasks);
  if (attachments.length) {
    const title = SECTION_OF["tools"];
    bySection.set(title, [...(bySection.get(title) ?? []), ...attachments]);
  }
  const tools = toolChanges(before, after, names.tool);
  const sections: ConfigSection[] = SECTIONS.map((s) => ({ title: s.title, changes: bySection.get(s.title) ?? [] }))
    .concat({ title: OTHER, changes: bySection.get(OTHER) ?? [] })
    .filter((s) => s.changes.length > 0);
  return {
    changed: sections.length > 0 || tools.length > 0,
    sections,
    tools,
  };
}
