import type { OperationRequest, TransferConfig } from "@talqing/sdk";

/* ── what the editor calls each kind, and how each one behaves ───────────────
   The *shape* of an operation is the SDK's discriminated union — which kinds
   exist, and which fields each one has. This file is what cannot be read off a
   type: the words in the picker, and the two rules that are behaviour rather
   than shape (what may end a chain, and what a transfer mode costs the caller).

   Which kinds carry `silent` / `publish_fields` / `background_execution` used to
   live here as three hand-copied arrays. It is now `isDataOperation` in
   `operationTypes.ts`, narrowing the same union the API validates against. */

export type OperationKind = OperationRequest["kind"];

/** Fails to compile when `T` is anything but `never` — used below to prove a
    hand-written list has not fallen behind the SDK's union. */
type AssertNever<T extends never> = T;

export const KINDS = [
  "http",
  "code",
  "if",
  "set_variable",
  "say",
  "generate_reply",
  "add_message",
  "end_call",
  "handoff",
  "transfer",
  "send_dtmf",
  "frontend_rpc",
] as const satisfies readonly OperationKind[];

/* `satisfies` above rejects a kind that is not in the SDK union; this rejects
   one the SDK has and this list does not. Between them the picker cannot drift
   from the API — the compile-time twin of the runtime throw below. */
type AllKindsListed = AssertNever<Exclude<OperationKind, (typeof KINDS)[number]>>;
export type { AllKindsListed };

export const KIND_LABEL: Record<OperationKind, string> = {
  http: "HTTP request",
  code: "Code (TypeScript)",
  if: "If / branch",
  set_variable: "Set variable",
  say: "Say",
  generate_reply: "Generate reply",
  add_message: "Add message",
  end_call: "End call",
  handoff: "Hand off to agent",
  transfer: "Transfer to a human",
  send_dtmf: "Press keypad keys",
  frontend_rpc: "Frontend RPC",
};

/* What each operation does, in one line, for the picker. The label alone stopped
   being enough at eleven kinds — "Add message" and "Say" are both plausible
   readings of "make the agent talk", and only one of them speaks. */
export const KIND_DESCRIPTION: Record<OperationKind, string> = {
  http: "Call an external API and read the response.",
  code: "Run TypeScript in a sandbox — reshape data, do maths, chain calls.",
  frontend_rpc: "Ask the connected web app to run one of its own handlers.",
  say: "Speak a fixed line, with variables filled in.",
  generate_reply: "Let the model phrase the line from your instructions.",
  add_message: "Add a note to the chat context. Nothing is spoken.",
  set_variable: "Store a value for later operations, or for the rest of the call.",
  if: "Compare two values and run one of two branches.",
  handoff: "Move the conversation to another published agent.",
  transfer: "Put the caller through to a phone number.",
  send_dtmf: "Dial digits into the call, for an IVR on the other end.",
  end_call: "Hang up. Anything else the tool must do goes before it.",
};

/** The label for a kind that arrives as an untyped string — a trace step's
    `kind` comes off the wire, and a build that predates a kind should print it
    rather than nothing. */
export function kindLabel(kind: string): string {
  return KIND_LABEL[kind as OperationKind] ?? kind;
}

/* The picker's shape. Grouped by what the author came to do, because a flat list
   of eleven ordered the labels and ordered the intent not at all. */
export const OPERATION_GROUPS: readonly { title: string; kinds: readonly OperationKind[] }[] = [
  { title: "Fetch and compute", kinds: ["http", "code", "frontend_rpc"] },
  { title: "Speak", kinds: ["say", "generate_reply", "add_message"] },
  { title: "Remember and branch", kinds: ["set_variable", "if"] },
  /* Its own group of one rather than under "Speak": pressing keys is not
     talking, and it is the only operation whose availability depends on which
     platform is carrying the call. */
  { title: "Use the keypad", kinds: ["send_dtmf"] },
  { title: "Hand over or end", kinds: ["handoff", "transfer", "end_call"] },
];

/* The picker renders the groups, not KINDS, so a kind added to one and not the
   other would simply not be offerable — a missing card is much harder to notice
   than a crash on the page that owns it. */
const GROUPED_KINDS = OPERATION_GROUPS.flatMap((group) => group.kinds);
const UNGROUPED = KINDS.filter((kind) => !GROUPED_KINDS.includes(kind));
if (UNGROUPED.length || GROUPED_KINDS.length !== KINDS.length)
  throw new Error(`OPERATION_GROUPS is out of sync with KINDS (ungrouped: ${UNGROUPED.join(", ") || "none"})`);

/* Kinds nothing can follow in the same chain: `if` branches out and branches do
   not rejoin, `handoff` gives the conversation to another agent, `end_call`
   closes the session — anything after it would run against a call that is already
   draining — and `transfer` hands the caller to a person and ends our session
   behind them. Behaviour rather than shape: nothing in the type of a `handoff`
   says what may follow it. Mirrors `TERMINAL_KINDS` in
   backend/services/tools/tree.py, which rejects the shape on save. */
export const TERMINAL_KINDS: readonly OperationKind[] = ["if", "handoff", "end_call", "transfer"];

/* Seconds a transfer destination may ring. Mirrors the bounds enforced at
   save in backend/services/tools/defs.py. */
export const TRANSFER_RINGING_TIMEOUT_DEFAULT = 30;
export const TRANSFER_RINGING_TIMEOUT_MIN = 5;
export const TRANSFER_RINGING_TIMEOUT_MAX = 120;

/* The two transfers, described by what the caller lives through rather than by
   which SIP verb runs — which is the platform's choice and never the builder's.
   Mirrors `TransferMode` in backend/services/tools/defs.py. */
export const TRANSFER_MODES: readonly {
  value: NonNullable<TransferConfig["mode"]>;
  label: string;
  caller: string;
}[] = [
  {
    value: "cold",
    label: "Hand over straight away",
    caller: "The agent finishes its line and a person answers. No introduction.",
  },
  {
    value: "warm",
    label: "Brief the person first",
    caller:
      "The caller waits on hold music while the agent rings the person, explains who is calling and why, and only then puts the two together. That person can also decline, and the caller comes back to the agent.",
  },
];
