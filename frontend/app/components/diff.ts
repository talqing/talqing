
/* ── the parts of a diff that are not about tools, agents or tasks ───────────
   Shared by `tools/detail/toolDiff.ts` (an operation tree),
   `agents/detail/agentDiff.ts` and `tasks/detail/taskDiff.ts` (config objects).
   All answer "which field changed, and how", so all need the same equality, the
   same line diff, and the same vocabulary of statuses — and none should own it. */

export type Status = "unchanged" | "added" | "removed" | "changed";

/** One field that differs. `before`/`after` are undefined when the field only
    exists on one side. */
export type FieldChange = {
  field: string;
  before?: unknown;
  after?: unknown;
};

/** One attached tool that differs between two configs. `before`/`after` are the
    pinned versions: null on a draft, which tracks whatever is published now. */
export type ToolChange = {
  status: Status;
  tool_id: string;
  /** The tool's current name, or its id when the tool has been deleted. */
  name: string;
  deleted: boolean;
  before?: number | null;
  after?: number | null;
};

export type ConfigSection = { title: string; changes: FieldChange[] };

/** Two versions of an agent or a task config, compared. Sections in config
    order, then the tools — which earn their own list because two versions can
    hold an identical config and still behave differently, a tool having been
    republished between them. `ConfigDiffView` renders exactly this. */
export type ConfigDiff = {
  changed: boolean;
  sections: ConfigSection[];
  tools: ToolChange[];
};

/** JSON with object keys sorted, so two values that differ only in key order
    compare equal. Non-negotiable here: a published definition comes back out of
    JSONB, and Postgres reorders keys, so a string comparison would report
    everything as changed. */
export function stableKey(value: unknown): string {
  if (value === null || typeof value !== "object") return JSON.stringify(value) ?? "null";
  if (Array.isArray(value)) return `[${value.map(stableKey).join(",")}]`;
  const entries = Object.entries(value as Record<string, unknown>)
    .filter(([, v]) => v !== undefined)
    .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));
  return `{${entries.map(([k, v]) => `${JSON.stringify(k)}:${stableKey(v)}`).join(",")}}`;
}

export function same(a: unknown, b: unknown): boolean {
  return stableKey(a) === stableKey(b);
}

// ── line diff, for the values that are prose or source ───────────────────────

export type LineChange = { status: "unchanged" | "added" | "removed"; text: string };

/** A classic LCS line diff, for values where a field-level "before → after"
    would be unreadable — a system prompt, a `code` operation's TypeScript, an
    HTTP body. */
export function diffLines(before: string, after: string): LineChange[] {
  const a = before.split("\n");
  const b = after.split("\n");
  const table: number[][] = Array.from({ length: a.length + 1 }, () => new Array(b.length + 1).fill(0));
  for (let i = a.length - 1; i >= 0; i--) {
    for (let j = b.length - 1; j >= 0; j--) {
      table[i][j] = a[i] === b[j] ? table[i + 1][j + 1] + 1 : Math.max(table[i + 1][j], table[i][j + 1]);
    }
  }
  const out: LineChange[] = [];
  let i = 0;
  let j = 0;
  while (i < a.length && j < b.length) {
    if (a[i] === b[j]) { out.push({ status: "unchanged", text: a[i] }); i++; j++; }
    else if (table[i + 1][j] >= table[i][j + 1]) { out.push({ status: "removed", text: a[i] }); i++; }
    else { out.push({ status: "added", text: b[j] }); j++; }
  }
  while (i < a.length) out.push({ status: "removed", text: a[i++] });
  while (j < b.length) out.push({ status: "added", text: b[j++] });
  return out;
}

/** Whether a change reads better as a line diff than as `before → after`.
    Decided from the values, not from a list of field names: an allowlist would
    have to name every long field a tool or an agent can hold, and would be wrong
    the moment one is added. */
export function isMultiline(change: FieldChange): boolean {
  if (typeof change.before !== "string" && change.before !== undefined) return false;
  if (typeof change.after !== "string" && change.after !== undefined) return false;
  return `${change.before ?? ""}${change.after ?? ""}`.includes("\n");
}

/** How long ago, in the shortest form that is still exact enough to pick a
    version by. Past a month the date itself is more use than the interval. */
export function timeAgo(iso: string): string {
  const seconds = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (seconds < 60) return `${Math.floor(seconds)}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  if (seconds < 86400 * 30) return `${Math.floor(seconds / 86400)}d ago`;
  return new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}
