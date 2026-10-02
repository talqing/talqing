import type {
  AddMessageConfig,
  CodeConfig,
  EndCallConfig,
  FrontendRpcConfig,
  GenerateReplyConfig,
  HandoffConfig,
  HttpConfig,
  IfConfig,
  OnError,
  PublishStore,
  SayConfig,
  SendDtmfConfig,
  SetVariableConfig,
  TransferConfig,
} from "@talqing/sdk";

export type { OnError };

export type ChainContainer = { parentId: string; branch: "then" | "else" } | null;

/* `enum` is a type in the picker but not a JSON Schema type — it is a constraint
   on one. The values still have to *be* something, and that something is what
   `enumValueType` holds: "string" for anything built in the editor, whatever the
   schema already said for a tool published before this editor existed, so
   opening a numeric enum and saving an unrelated field doesn't quietly restring
   it. */
export type ParamDraft = {
  name: string;
  type: string;
  description: string;
  required: boolean;
  enumValues: string[];
  enumValueType?: string;
};

export function paramValueType(param: ParamDraft): string {
  return param.type === "enum" ? param.enumValueType || "string" : param.type;
}

export type PublishFieldDraft = {
  _id: string;
  path: string;
  key: string;
  store: PublishStore;
};

/* ── the editor's own node ───────────────────────────────────────────────────
   A wire operation plus the state only the editor has: a React key, and the raw
   text of the two fields that are JSON on the wire and a textarea on screen.

   The same discriminated union as the SDK's `OperationRequest`, so
   `op.kind === "http"` narrows `op.config` to `HttpConfig` here exactly as it
   does there — and a node cannot be given a field its kind does not have. */

type Editable = { _id: string; on_error: OnError };

/* The three fields only a data-producing operation carries — there is nothing
   for the other eight kinds to hide, publish out of, or stop waiting for. */
type EditableData = Editable & {
  silent: boolean;
  publish_fields: PublishFieldDraft[];
  background_execution: boolean;
};

export type HttpDraft = EditableData & { kind: "http"; config: HttpConfig; bodyText: string };
export type CodeDraft = EditableData & { kind: "code"; config: CodeConfig };
export type FrontendRpcDraft = EditableData & {
  kind: "frontend_rpc";
  config: FrontendRpcConfig;
  payloadText: string;
};
export type IfDraft = Editable & {
  kind: "if";
  config: IfConfig;
  then: OperationDraft[];
  else: OperationDraft[];
};
export type SetVariableDraft = Editable & { kind: "set_variable"; config: SetVariableConfig };
export type SayDraft = Editable & { kind: "say"; config: SayConfig };
export type GenerateReplyDraft = Editable & { kind: "generate_reply"; config: GenerateReplyConfig };
export type AddMessageDraft = Editable & { kind: "add_message"; config: AddMessageConfig };
export type EndCallDraft = Editable & { kind: "end_call"; config: EndCallConfig };
export type HandoffDraft = Editable & { kind: "handoff"; config: HandoffConfig };
export type TransferDraft = Editable & { kind: "transfer"; config: TransferConfig };
export type SendDtmfDraft = Editable & { kind: "send_dtmf"; config: SendDtmfConfig };

/** The three kinds that produce a result. */
export type DataOperationDraft = HttpDraft | CodeDraft | FrontendRpcDraft;

/** The two that can hold back the next operation until their line has played. */
export type SpeechDraft = SayDraft | GenerateReplyDraft;

export type OperationDraft =
  | DataOperationDraft
  | IfDraft
  | SetVariableDraft
  | SayDraft
  | GenerateReplyDraft
  | AddMessageDraft
  | EndCallDraft
  | HandoffDraft
  | TransferDraft
  | SendDtmfDraft;

export type OperationKind = OperationDraft["kind"];

export function isDataOperation(op: OperationDraft): op is DataOperationDraft {
  return op.kind === "http" || op.kind === "code" || op.kind === "frontend_rpc";
}

export function isSpeech(op: OperationDraft): op is SpeechDraft {
  return op.kind === "say" || op.kind === "generate_reply";
}

/** A partial update to a node's own fields — never its config, which `setCfg`
    owns. Written flat rather than as a `Partial<OperationDraft>`: the union's
    members share no optional shape, and every caller here patches one field. */
export type OperationDraftPatch = {
  on_error?: OnError;
  silent?: boolean;
  publish_fields?: PublishFieldDraft[];
  background_execution?: boolean;
  bodyText?: string;
  payloadText?: string;
};

export type TreeEditorActions = {
  setCfg: (opId: string, patch: Record<string, unknown>) => void;
  setNode: (opId: string, patch: OperationDraftPatch) => void;
  addOp: (container: ChainContainer, kind: OperationKind) => void;
  delOp: (opId: string) => void;
};
