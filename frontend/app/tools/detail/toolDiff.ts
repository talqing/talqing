import type { JsonObject, JsonValue, OperationRequest, PublishFieldRequest, ToolVersionDetailResponse } from "@talqing/sdk";
import { same, stableKey, type FieldChange, type Status } from "@/app/components/diff";
import type { ToolDefinitionSnapshot } from "./definition";
import { inferredPublishKey } from "./definition";

/* ── comparing two tool definitions ─────────────────────────────────────────
   A tool is a tree, not a file, so this is a structural diff rather than a text
   one: it answers "which operation changed, and which field of it" instead of
   "which line moved". Both sides are `ToolDefinitionSnapshot` — the shape
   `definition.ts` produces for the draft and the API returns for a version — so
   the two are always compared like with like.

   Everything here is order-insensitive about object keys. It has to be: a
   published definition comes back out of JSONB, and Postgres reorders keys, so
   a string comparison would report every tool as changed. */

export type ArgChange = {
  name: string;
  status: Status;
  /** type / required / description / values, for a `changed` argument. */
  changes: FieldChange[];
  /** The argument as it reads now (or as it read, when removed). */
  type: string;
  required: boolean;
};

export type OperationDiff = {
  status: Status;
  /** The node as it is after the change — or, when removed, as it was. */
  op: OperationRequest;
  /** The node as it was, when it changed. Lets a renderer show both sides. */
  before?: OperationRequest;
  changes: FieldChange[];
  then?: OperationDiff[];
  else?: OperationDiff[];
  /** True when this node is unchanged but something inside its branches is not. */
  branchesChanged: boolean;
};

export type ToolDiff = {
  changed: boolean;
  counts: { added: number; removed: number; changed: number };
  behaviour: FieldChange[];
  args: ArgChange[];
  operations: OperationDiff[];
};

// ── behaviour ────────────────────────────────────────────────────────────────

const BEHAVIOUR_FIELDS = [
  "name",
  "description",
  "long_running_task",
  "silent",
  "disable_interruptions",
] as const;

function behaviourChanges(before: ToolDefinitionSnapshot, after: ToolDefinitionSnapshot): FieldChange[] {
  return BEHAVIOUR_FIELDS
    .filter((field) => !same(before[field], after[field]))
    .map((field) => ({ field, before: before[field], after: after[field] }));
}

// ── arguments ────────────────────────────────────────────────────────────────

type Arg = { name: string; type: string; required: boolean; description: string; values: JsonValue[] | null };

function argsOf(schema: JsonObject | undefined): Map<string, Arg> {
  const properties =
    schema?.properties && typeof schema.properties === "object" && !Array.isArray(schema.properties)
      ? schema.properties
      : {};
  const required = Array.isArray(schema?.required) ? schema.required : [];
  const out = new Map<string, Arg>();
  for (const [name, raw] of Object.entries(properties)) {
    const prop = raw && typeof raw === "object" && !Array.isArray(raw) ? raw : {};
    out.set(name, {
      name,
      type: typeof prop.type === "string" ? prop.type : "string",
      required: required.includes(name),
      description: typeof prop.description === "string" ? prop.description : "",
      values: Array.isArray(prop.enum) ? prop.enum : null,
    });
  }
  return out;
}

function argChanges(before: ToolDefinitionSnapshot, after: ToolDefinitionSnapshot): ArgChange[] {
  const a = argsOf(before.json_schema);
  const b = argsOf(after.json_schema);
  const names = Array.from(new Set([...b.keys(), ...a.keys()]));
  const out: ArgChange[] = [];
  for (const name of names) {
    const was = a.get(name);
    const now = b.get(name);
    if (!was && now) { out.push({ name, status: "added", changes: [], type: now.type, required: now.required }); continue; }
    if (was && !now) { out.push({ name, status: "removed", changes: [], type: was.type, required: was.required }); continue; }
    if (!was || !now) continue;
    const changes = (["type", "required", "description", "values"] as const)
      .filter((field) => !same(was[field], now[field]))
      .map((field) => ({ field, before: was[field], after: now[field] }));
    if (changes.length) out.push({ name, status: "changed", changes, type: now.type, required: now.required });
  }
  // Added and removed arguments break a caller; a reworded description does not.
  const rank: Record<Status, number> = { added: 0, removed: 1, changed: 2, unchanged: 3 };
  return out.sort((x, y) => rank[x.status] - rank[y.status] || (x.name < y.name ? -1 : 1));
}

// ── operations ───────────────────────────────────────────────────────────────

/** The three fields only a data operation has, so the diff can read them off any
    node without asking which kind it is. */
function dataFields(op: OperationRequest): {
  silent: boolean;
  publish_fields: PublishFieldRequest[];
  background_execution: boolean;
} | null {
  if (op.kind !== "http" && op.kind !== "code" && op.kind !== "frontend_rpc") return null;
  return {
    silent: !!op.silent,
    publish_fields: op.publish_fields ?? [],
    background_execution: !!op.background_execution,
  };
}

/** An operation's config as a plain record. A structural diff lists the keys
    that differ and does not care which variant it is looking at, so this is the
    one place the union is flattened.

    `compiled_js` is excluded: publishing transpiles `source_ts` and stores the
    output in the version, so comparing it would make every `code` operation read
    as changed the moment it is published. */
function configOf(op: OperationRequest): JsonObject {
  const { compiled_js: _compiled, ...config } = (op.config ?? {}) as JsonObject;
  return config;
}

/** What makes two operations the same operation, ignoring their branches. */
function nodeIdentity(op: OperationRequest): string {
  return stableKey({
    kind: op.kind,
    config: configOf(op),
    on_error: op.on_error ?? "abort",
    ...(dataFields(op) ?? {}),
  });
}

function nodeChanges(before: OperationRequest, after: OperationRequest): FieldChange[] {
  const a = configOf(before);
  const b = configOf(after);
  const changes: FieldChange[] = Array.from(new Set([...Object.keys(b), ...Object.keys(a)]))
    .filter((field) => !same(a[field], b[field]))
    .map((field) => ({ field, before: a[field], after: b[field] }));
  if (!same(before.on_error ?? "abort", after.on_error ?? "abort")) {
    changes.push({ field: "on_error", before: before.on_error ?? "abort", after: after.on_error ?? "abort" });
  }
  const wasData = dataFields(before);
  const nowData = dataFields(after);
  for (const field of ["silent", "background_execution"] as const) {
    const was = wasData?.[field] ?? false;
    const now = nowData?.[field] ?? false;
    if (!same(was, now)) changes.push({ field, before: was, after: now });
  }
  const wasPublishing = publishedVariables(before);
  const nowPublishing = publishedVariables(after);
  if (!same(wasPublishing, nowPublishing)) {
    changes.push({ field: "publishes", before: wasPublishing, after: nowPublishing });
  }
  return changes;
}

/** `tooldata.slot`, `userdata.caller` — the tokens a later operation types, which
    is how the operation tree already names what a step saves. */
function publishedVariables(op: OperationRequest): string[] {
  return (dataFields(op)?.publish_fields ?? [])
    .map((field) => ({ key: field.key?.trim() || inferredPublishKey(field.path), store: field.store }))
    .filter(({ key }) => key.length > 0)
    .map(({ key, store }) => `${store}.${key}`);
}

/** An `if`'s branches, or nothing for the ten kinds that have none. */
function branchesOf(op: OperationRequest): { then: OperationRequest[]; else: OperationRequest[] } | null {
  return op.kind === "if" ? { then: op.then ?? [], else: op.else ?? [] } : null;
}

function leaf(op: OperationRequest, status: Status): OperationDiff {
  /* An added or removed `if` takes its whole subtree with it, and every node in
     that subtree carries the same status — otherwise deleting a branch would
     read as "the branch is gone" with unmarked children hanging under it. */
  const branches = branchesOf(op);
  return {
    status,
    op,
    changes: [],
    then: branches?.then.map((child) => leaf(child, status)),
    else: branches?.else.map((child) => leaf(child, status)),
    branchesChanged: false,
  };
}

function matched(before: OperationRequest, after: OperationRequest): OperationDiff {
  const changes = nodeChanges(before, after);
  const wasBranches = branchesOf(before);
  const nowBranches = branchesOf(after);
  const then = wasBranches || nowBranches
    ? diffChain(wasBranches?.then ?? [], nowBranches?.then ?? [])
    : undefined;
  const els = wasBranches || nowBranches
    ? diffChain(wasBranches?.else ?? [], nowBranches?.else ?? [])
    : undefined;
  const branchesChanged = [...(then ?? []), ...(els ?? [])].some(
    (node) => node.status !== "unchanged" || node.branchesChanged,
  );
  return {
    status: changes.length ? "changed" : "unchanged",
    op: after,
    before: changes.length ? before : undefined,
    changes,
    then,
    else: els,
    branchesChanged,
  };
}

/** Align one chain of siblings against another.

    Identical nodes anchor the alignment (an LCS over their canonical form), and
    what is left between two anchors is paired up by kind — so editing an HTTP
    url reads as one changed operation rather than one removed and one added. */
function diffChain(before: OperationRequest[], after: OperationRequest[]): OperationDiff[] {
  const a = before.map(nodeIdentity);
  const b = after.map(nodeIdentity);
  const table: number[][] = Array.from({ length: a.length + 1 }, () => new Array(b.length + 1).fill(0));
  for (let i = a.length - 1; i >= 0; i--) {
    for (let j = b.length - 1; j >= 0; j--) {
      table[i][j] = a[i] === b[j] ? table[i + 1][j + 1] + 1 : Math.max(table[i + 1][j], table[i][j + 1]);
    }
  }

  const out: OperationDiff[] = [];
  let gapBefore: OperationRequest[] = [];
  let gapAfter: OperationRequest[] = [];

  function flushGap(): void {
    // Pair leftovers of the same kind, in order; anything unpaired is a real
    // addition or deletion.
    const removed = [...gapBefore];
    for (const node of gapAfter) {
      const index = removed.findIndex((candidate) => candidate.kind === node.kind);
      if (index >= 0) out.push(matched(removed.splice(index, 1)[0], node));
      else out.push(leaf(node, "added"));
    }
    for (const node of removed) out.push(leaf(node, "removed"));
    gapBefore = [];
    gapAfter = [];
  }

  let i = 0;
  let j = 0;
  while (i < a.length && j < b.length) {
    if (a[i] === b[j]) { flushGap(); out.push(matched(before[i], after[j])); i++; j++; }
    else if (table[i + 1][j] >= table[i][j + 1]) { gapBefore.push(before[i]); i++; }
    else { gapAfter.push(after[j]); j++; }
  }
  while (i < a.length) gapBefore.push(before[i++]);
  while (j < b.length) gapAfter.push(after[j++]);
  flushGap();
  return out;
}

function countOperations(nodes: OperationDiff[], counts: ToolDiff["counts"]): void {
  for (const node of nodes) {
    if (node.status === "added") counts.added++;
    else if (node.status === "removed") counts.removed++;
    else if (node.status === "changed") counts.changed++;
    // An added/removed subtree is one change, not one per descendant.
    if (node.status === "added" || node.status === "removed") continue;
    countOperations(node.then ?? [], counts);
    countOperations(node.else ?? [], counts);
  }
}

/** The version fields, as the snapshot shape the diff compares. */
export function versionSnapshot(version: ToolVersionDetailResponse): ToolDefinitionSnapshot {
  return {
    name: version.name,
    description: version.description,
    json_schema: version.json_schema,
    operations: version.operations,
    long_running_task: version.long_running_task,
    silent: version.silent,
    disable_interruptions: version.disable_interruptions,
  };
}

/** An empty definition, so a first publish diffs as "everything is new". */
export const EMPTY_DEFINITION: ToolDefinitionSnapshot = {
  name: "",
  description: "",
  json_schema: {},
  operations: [],
  long_running_task: false,
  silent: false,
  disable_interruptions: false,
};

export function diffToolDefinitions(
  before: ToolDefinitionSnapshot,
  after: ToolDefinitionSnapshot,
): ToolDiff {
  const behaviour = behaviourChanges(before, after);
  const args = argChanges(before, after);
  const operations = diffChain(before.operations ?? [], after.operations ?? []);
  const counts = { added: 0, removed: 0, changed: 0 };
  countOperations(operations, counts);
  return {
    changed: behaviour.length > 0 || args.length > 0 || counts.added + counts.removed + counts.changed > 0,
    counts,
    behaviour,
    args,
    operations,
  };
}
