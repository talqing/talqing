import type {
  JsonObject,
  JsonValue,
  OperationRequest,
  PublishFieldRequest,
} from "@talqing/sdk";
import type {
  DataOperationDraft,
  OperationDraft,
  OperationKind,
  ParamDraft,
  PublishFieldDraft,
} from "./operationTypes";
import { isDataOperation, paramValueType } from "./operationTypes";
import { TRANSFER_RINGING_TIMEOUT_DEFAULT } from "./operationMetadata";

/* ── the one place a tool becomes JSON ──────────────────────────────────────
   The editor holds the WHOLE operation tree in local state and syncs it with the
   tool metadata on Save (PATCH /tools/:id). Add/remove/edit never round-trip, so
   nothing is ever discarded mid-edit. A chain is an array; an `if` node owns its
   own then/else child arrays.

   `draftDefinition` below is the shape the backend freezes into
   `tool_versions.definition`, byte for byte. Everything that compares a draft to
   a published version — the dirty check, the drift badge, the diff — goes
   through it, so a second normalizer can never drift from this one and make the
   diff lie. */

/** A tool at one moment: the draft, or a published version, in one shape. */
export type ToolDefinitionSnapshot = {
  name: string;
  description: string;
  json_schema: JsonObject;
  operations: OperationRequest[];
  long_running_task: boolean;
  silent: boolean;
  disable_interruptions: boolean;
};

export type DraftInput = {
  name: string;
  desc: string;
  longRunning: boolean;
  silent: boolean;
  disableInterruptions: boolean;
  params: ParamDraft[];
  ops: OperationDraft[];
};

export const newId = (): string =>
  (typeof crypto !== "undefined" && crypto.randomUUID
    ? crypto.randomUUID()
    : `tmp_${Math.random().toString(36).slice(2)}`);

// ── walking the tree ─────────────────────────────────────────────────────────

/** Every chain in `nodes` — the top-level list and each `if` branch. A chain is
    what "terminal" and "the next operation" are about, so anything positional
    reads them rather than single nodes. */
export function* chains(nodes: OperationDraft[]): Generator<OperationDraft[]> {
  yield nodes;
  for (const node of nodes) {
    if (node.kind === "if") {
      yield* chains(node.then);
      yield* chains(node.else);
    }
  }
}

/** Every operation in `nodes`, branches included, in document order. */
export function* walkTree(nodes: OperationDraft[]): Generator<OperationDraft> {
  for (const node of nodes) {
    yield node;
    if (node.kind === "if") {
      yield* walkTree(node.then);
      yield* walkTree(node.else);
    }
  }
}

// ── arguments ↔ JSON Schema ──────────────────────────────────────────────────

export function isValidEnumValueForType(raw: string, type: string): boolean {
  const value = raw.trim();
  if (!value) return false;
  if (type === "integer") return Number.isInteger(Number(value));
  if (type === "number") return Number.isFinite(Number(value));
  if (type === "boolean") return value.toLowerCase() === "true" || value.toLowerCase() === "false";
  return true;
}

function enumValueForSchema(raw: string, type: string): JsonValue {
  if (type === "number" || type === "integer") return Number(raw);
  if (type === "boolean") return raw.trim().toLowerCase() === "true";
  return raw;
}

export function paramsToJsonSchema(params: ParamDraft[]): JsonObject {
  const properties: Record<string, JsonObject> = {};
  const required: string[] = [];
  for (const p of params) {
    if (!p.name) continue;
    const valueType = paramValueType(p);
    properties[p.name] = { type: valueType, description: p.description };
    /* Values left over from a param that used to be an enum stay in the draft so
       switching back restores them, but they are not part of the contract. */
    if (p.type === "enum") {
      const enumVals = p.enumValues
        .map((s: string) => s.trim())
        .filter((s: string) => isValidEnumValueForType(s, valueType))
        .map((s: string) => enumValueForSchema(s, valueType));
      if (enumVals.length) properties[p.name].enum = enumVals;
    }
    if (p.required) required.push(p.name);
  }
  return { type: "object", properties, ...(required.length ? { required } : {}) };
}

export function paramsFromJsonSchema(schema: JsonObject | undefined): ParamDraft[] {
  const props =
    schema?.properties && typeof schema.properties === "object" && !Array.isArray(schema.properties)
      ? schema.properties
      : {};
  const req = Array.isArray(schema?.required)
    ? schema.required.filter((value): value is string => typeof value === "string")
    : [];
  return Object.entries(props).map(([name, value]) => {
    const prop = value && typeof value === "object" && !Array.isArray(value) ? value : {};
    const declaredType = typeof prop.type === "string" ? prop.type : "string";
    const enumValues = Array.isArray(prop.enum) ? prop.enum.map(String) : [];
    return {
      name,
      type: enumValues.length ? "enum" : declaredType,
      description: typeof prop.description === "string" ? prop.description : "",
      required: req.includes(name),
      enumValues,
      enumValueType: declaredType,
    };
  });
}

// ── operations ↔ local editable nodes ────────────────────────────────────────

export function bodyToText(body: unknown): string {
  if (body == null) return "";
  return typeof body === "string" ? body : JSON.stringify(body, null, 2);
}

export function publishFieldToDraft(field: PublishFieldRequest): PublishFieldDraft {
  return { _id: newId(), path: field.path, key: field.key || "", store: field.store };
}

export function publishDraftToField(field: PublishFieldDraft): PublishFieldRequest | null {
  const path = field.path.trim();
  const key = field.key.trim();
  if (!path) return null;
  /* `key: null` rather than an absent key — an unnamed field is what the server
     stores and hands back, and a draft that serialized it differently would read
     as changed against the version it is identical to. */
  return { path, key: key || null, store: field.store };
}

export function inferredPublishKey(path: string): string {
  const parts = path.trim().split(".").map((part) => part.trim()).filter(Boolean);
  return parts[parts.length - 1] || "";
}

/** The three fields every data operation starts with, and nothing else has. */
const dataFields = () => ({ silent: false, publish_fields: [], background_execution: false });

/** A new node of `kind`, with the defaults that kind starts life with. */
export function makeNode(kind: OperationKind): OperationDraft {
  const base = { _id: newId(), on_error: "abort" as const };
  switch (kind) {
    case "http":
      return {
        ...base,
        ...dataFields(),
        kind,
        config: { method: "GET", url: "", headers: {}, query: {}, body: null, timeout: 20 },
        bodyText: "",
      };
    case "code":
      return {
        ...base,
        ...dataFields(),
        kind,
        config: {
          source_ts: [
            "// JSON in, JSON out — runs on an isolated code service, not in the agent.",
            "// input = { args, tooldata, userdata, system_vars, vars, secrets }; fields of the",
            "// returned object can be published to tooldata or userdata (pick them below).",
            "// Read them as properties — input.system_vars.now — never as {{…}} templates.",
            "// `fetch` is available (public URLs).",
            "export default async function handler(input: any) {",
            "  return {};",
            "}",
          ].join("\n"),
          timeout: 10,
        },
      };
    case "frontend_rpc":
      return {
        ...base,
        ...dataFields(),
        kind,
        config: { method: "", payload: null, timeout: 5 },
        payloadText: "",
      };
    case "if":
      return { ...base, kind, config: { left: "", op: "eq", right: "" }, then: [], else: [] };
    // tooldata by default: session userdata is shared with every later tool and
    // turn, so writing there should be something the author chooses.
    case "set_variable":
      return { ...base, kind, config: { key: "", value: "", store: "tooldata" } };
    // Off by default: the archetypal Say is a filler that exists to cover the
    // NEXT operation's latency, and waiting there would lengthen every call.
    case "say":
      return { ...base, kind, config: { text: "", wait_for_playback: false } };
    case "generate_reply":
      return { ...base, kind, config: { instructions: "", wait_for_playback: false } };
    case "add_message":
      return { ...base, kind, config: { text: "" } };
    case "end_call":
      return { ...base, kind, config: {} };
    /* `summary` is here so the field the `summary` policy requires exists the
       moment the policy is picked; `recent_turns` is deliberately absent, so an
       operation nobody touched keeps the policy's own default. */
    case "handoff":
      return {
        ...base,
        kind,
        config: { target_agent_id: "", context: "transcript", summary: "", message: "" },
      };
    /* `mode` is written but not offered: cold is the only one that exists, and a
       disabled "warm (coming soon)" radio is a promise in the UI with no date on
       it. The key is here so a tool published today stays valid when warm lands.
       `on_failure: continue` leaves the caller with the agent, which is the only
       recoverable answer and so the right default. */
    case "transfer":
      return {
        ...base,
        kind,
        config: {
          mode: "cold",
          destination: "",
          ringing_timeout: TRANSFER_RINGING_TIMEOUT_DEFAULT,
          on_failure: "continue",
        },
      };
    case "send_dtmf":
      return { ...base, kind, config: { digits: "" } };
  }
}

/** wire operations → local editable nodes */
export function toLocal(ops: OperationRequest[]): OperationDraft[] {
  return (ops || []).map((op): OperationDraft => {
    const base = { _id: newId(), on_error: op.on_error ?? ("abort" as const) };
    switch (op.kind) {
      case "http":
        return {
          ...base,
          ...dataFieldsFrom(op),
          kind: op.kind,
          config: op.config,
          bodyText: bodyToText(op.config.body),
        };
      case "code":
        return { ...base, ...dataFieldsFrom(op), kind: op.kind, config: op.config };
      case "frontend_rpc":
        return {
          ...base,
          ...dataFieldsFrom(op),
          kind: op.kind,
          config: op.config,
          payloadText: bodyToText(op.config.payload),
        };
      case "if":
        return {
          ...base,
          kind: op.kind,
          config: op.config,
          then: toLocal(op.then || []),
          else: toLocal(op.else || []),
        };
      case "end_call":
        return { ...base, kind: op.kind, config: op.config ?? {} };
      /* Spelled out rather than defaulted: a `default` branch widens `kind` and
         `config` independently, and the pair is the whole point of the union. */
      case "set_variable":
        return { ...base, kind: op.kind, config: op.config };
      case "say":
        return { ...base, kind: op.kind, config: op.config };
      case "generate_reply":
        return { ...base, kind: op.kind, config: op.config };
      case "add_message":
        return { ...base, kind: op.kind, config: op.config };
      case "handoff":
        return { ...base, kind: op.kind, config: op.config };
      case "transfer":
        return { ...base, kind: op.kind, config: op.config };
      case "send_dtmf":
        return { ...base, kind: op.kind, config: op.config };
    }
  });
}

function dataFieldsFrom(op: { silent?: boolean; publish_fields?: PublishFieldRequest[]; background_execution?: boolean }) {
  return {
    silent: !!op.silent,
    publish_fields: (op.publish_fields || []).map(publishFieldToDraft),
    background_execution: !!op.background_execution,
  };
}

/** local nodes → wire operations (merges body/payload text back into config) */
export function toServer(nodes: OperationDraft[]): OperationRequest[] {
  return nodes.map((node): OperationRequest => {
    const base = { on_error: node.on_error };
    switch (node.kind) {
      case "http": {
        const text = node.bodyText.trim();
        let body: JsonValue = null;
        if (text) { try { body = JSON.parse(text); } catch { body = text; } }
        return { ...base, ...dataFieldsTo(node), kind: node.kind, config: { ...node.config, body } };
      }
      case "code":
        return { ...base, ...dataFieldsTo(node), kind: node.kind, config: node.config };
      case "frontend_rpc": {
        const text = node.payloadText.trim();
        /* Null, not `{}`: that is the config's own default, and the runtime
           already hands the client an empty object for it. Serializing `{}` here
           instead would make every untouched payload read as an edit. */
        let payload: JsonValue = null;
        if (text) { try { payload = JSON.parse(text); } catch { payload = text; } }
        return { ...base, ...dataFieldsTo(node), kind: node.kind, config: { ...node.config, payload } };
      }
      case "if":
        return {
          ...base,
          kind: node.kind,
          config: node.config,
          then: toServer(node.then),
          else: toServer(node.else),
        };
      /* Spelled out rather than defaulted — see `toLocal`. */
      case "set_variable":
        return { ...base, kind: node.kind, config: node.config };
      case "say":
        return { ...base, kind: node.kind, config: node.config };
      case "generate_reply":
        return { ...base, kind: node.kind, config: node.config };
      case "add_message":
        return { ...base, kind: node.kind, config: node.config };
      case "end_call":
        return { ...base, kind: node.kind, config: node.config };
      case "handoff":
        return { ...base, kind: node.kind, config: node.config };
      case "transfer":
        return { ...base, kind: node.kind, config: node.config };
      case "send_dtmf":
        return { ...base, kind: node.kind, config: node.config };
    }
  });
}

function dataFieldsTo(node: DataOperationDraft) {
  return {
    silent: node.silent,
    publish_fields: node.publish_fields
      .map(publishDraftToField)
      .filter((field): field is PublishFieldRequest => !!field),
    background_execution: node.background_execution,
  };
}

// ── what an operation still needs ────────────────────────────────────────────

/* The API rejects an incomplete operation at SAVE — each kind is a typed variant
   with required fields — so the editor has to name the gaps before the request
   does. These mirror the required fields of the operation configs in
   `@talqing/sdk`; the server is still the authority, and anything it
   catches that this misses arrives as an ordinary error. */

export type OperationProblem = { field: string; message: string };

/** Loose next to the server's check, which requires canonical E.164: reddening
    "+1415" on the way to a valid number is noise, and the server's message is
    the one that has to be exact. */
const E164 = /^\+[1-9]\d{7,14}$/;

/** Every key a telephone keypad has, plus the two pause characters. Mirrors
    `DTMF_KEYS` in backend/services/tools/defs.py. */
const KEYPAD_KEYS = "0123456789*#ABCDwW ";
const RPC_METHOD = /^[A-Za-z_][A-Za-z0-9_.-]*$/;

export function operationProblems(op: OperationDraft): OperationProblem[] {
  const out: OperationProblem[] = [];
  const need = (field: string, value: string | undefined | null, message: string) => {
    if (!String(value ?? "").trim()) out.push({ field, message });
  };
  switch (op.kind) {
    case "http":
      need("url", op.config.url, "A URL is required.");
      break;
    case "code":
      need("source_ts", op.config.source_ts, "The TypeScript source is required.");
      break;
    case "frontend_rpc":
      if (!String(op.config.method ?? "").trim()) {
        out.push({ field: "method", message: "A client method name is required." });
      } else if (!RPC_METHOD.test(op.config.method)) {
        out.push({
          field: "method",
          message: "A method name is letters, digits, _ . or -, and cannot start with a digit.",
        });
      }
      break;
    case "set_variable":
      need("key", op.config.key, "A variable name is required.");
      break;
    case "say":
      need("text", op.config.text, "The line to speak is required.");
      break;
    case "generate_reply":
      need("instructions", op.config.instructions, "Reply instructions are required.");
      break;
    case "add_message":
      need("text", op.config.text, "The message to add is required.");
      break;
    case "handoff":
      /* `agent_name` present (even empty) is the team-member form — the API
         takes exactly one of the two targets. */
      if (op.config.agent_name != null) {
        need("agent_name", op.config.agent_name, "Name the team member to hand off to.");
      } else {
        need("target_agent_id", op.config.target_agent_id, "Pick the agent to hand off to.");
      }
      if (op.config.context === "summary") {
        need("summary", op.config.summary, "Context 'a summary of it' needs a summary to hand over.");
      }
      break;
    case "transfer":
      if (!String(op.config.destination ?? "").trim()) {
        out.push({ field: "destination", message: "A phone number is required." });
      } else if (!E164.test(op.config.destination.trim())) {
        out.push({
          field: "destination",
          message: "Not a valid phone number — it needs the country code, e.g. +14155550101.",
        });
      }
      break;
    case "send_dtmf": {
      const digits = String(op.config.digits ?? "").trim();
      if (!digits) {
        out.push({ field: "digits", message: "Keys to press are required." });
        break;
      }
      /* A value carrying a template is checked at runtime against what it
         resolved to — the same rule the API applies at save, because
         `{{args.pin}}` is a legitimate value and no keypad pattern accepts one. */
      const unknown = digits.includes("{{")
        ? []
        : [...new Set([...digits].filter((key) => !KEYPAD_KEYS.includes(key)))];
      if (unknown.length) {
        out.push({
          field: "digits",
          message: `A keypad has no ${unknown.join("")} — use 0-9, *, #, A-D, or w/W to pause.`,
        });
      }
      break;
    }
  }
  return out;
}

/** Every incomplete operation in the tree, by node id. Empty when the draft can
    be saved. */
export function treeProblems(ops: OperationDraft[]): Map<string, OperationProblem[]> {
  const out = new Map<string, OperationProblem[]>();
  for (const op of walkTree(ops)) {
    const problems = operationProblems(op);
    if (problems.length) out.set(op._id, problems);
  }
  return out;
}

// ── the draft as a definition ────────────────────────────────────────────────

/** True when no operation in the tree can contribute a response to the LLM, so
 *  the tool is silent whatever the box says.
 *
 *  Mirrors `tree_is_silent` in backend/services/tools/tree.py, which is what
 *  publishing actually freezes into the version. Walks `if` branches: a tree
 *  whose only contributing operation sits in an `else` is not a silent tree.
 *  A `background_execution` operation counts as silent whatever its own box
 *  says — it runs against a result that is discarded. */
export function treeIsSilent(ops: OperationDraft[]): boolean {
  for (const op of walkTree(ops)) {
    if (isDataOperation(op) && !op.silent && !op.background_execution) return false;
  }
  return true;
}

export function draftDefinition(d: DraftInput): ToolDefinitionSnapshot {
  return {
    name: d.name,
    description: d.desc,
    json_schema: paramsToJsonSchema(d.params),
    long_running_task: d.longRunning,
    /* The value publishing will freeze, not the one the checkbox holds — so the
       diff against the live version compares like with like. The author's own
       choice stays underneath and comes back the moment the tree stops being
       all-silent. */
    silent: d.silent || treeIsSilent(d.ops),
    disable_interruptions: d.disableInterruptions,
    operations: toServer(d.ops),
  };
}

/** The dirty/stale check: two drafts are the same iff they serialize the same. */
export function draftSignature(d: DraftInput): string {
  return JSON.stringify(draftDefinition(d));
}
