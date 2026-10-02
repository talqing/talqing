/** The named vocabularies the document spells inline.
 *
 * Pydantic renders a `Literal[...]` field as an inline enum rather than a named
 * schema, so a generator has nowhere to hang a name — the values arrive typed,
 * but only as part of whichever field carries them. These give each one back
 * its name by reading it off that field, so they cannot drift from the
 * contract: change the backend's Literal and the alias changes with it, or
 * stops compiling.
 *
 */

import type {
  AvatarCatalogEntryResponse,
  AvatarPriceLine,
  AvatarPricing,
  CallBatchRecipientResponse,
  CallSummaryResponse,
  CallBatchResponse,
  ConversationItemResponse,
  EmailBatchResponse,
  EmailRecipientResponse,
  EmailSendResponse,
  ObservabilityGetData,
  HttpOperation,
  LLMModelSpec,
  ObservabilityEndingResponse,
  ObservabilityLatencyMetricResponse,
  ObservabilityRangeResponse,
  LLMCatalogEntryResponse,
  LLMPriceLine,
  LLMPricing,
  PhoneNumberResponse,
  PublicMessage,
  NoiseCancellationCatalogEntryResponse,
  RealtimeCatalogEntryResponse,
  RealtimePriceLine,
  RealtimePricing,
  STTCatalogEntryResponse,
  STTPriceLine,
  STTPricing,
  StreamConnectionResponse,
  TelephonyAccountResponse,
  TTSCatalogEntryResponse,
  TTSPriceLine,
  TTSPricing,
  SetVariableConfig,
  TaskRunError,
  ToolResponse,
} from "./gen/types.gen.js";

/** Whether a call achieved what it was for. Three-valued on purpose: an
 *  evaluator that cannot say "I don't know" invents a verdict for wrong
 *  numbers and hangups. */
export type CallOutcome = NonNullable<CallSummaryResponse["outcome"]>;

/** How long a model may think before it answers. */
export type ReasoningEffort = NonNullable<LLMModelSpec["reasoning_effort"]>;

/** How a call reached the agent. */
export type CallType = CallSummaryResponse["type"];

export type CallBatchStatus = CallBatchResponse["status"];
export type CallBatchRecipientStatus = CallBatchRecipientResponse["status"];
export type EmailBatchStatus = EmailBatchResponse["status"];
export type EmailSendStatus = EmailSendResponse["status"];
export type EmailRecipientStatus = EmailRecipientResponse["status"];
export type NumberReadiness = PhoneNumberResponse["readiness"];
/** What a partner media-stream connection can do right now, as one value. */
export type StreamReadiness = StreamConnectionResponse["readiness"];
/** Which platform's WebSocket protocol a connection speaks. */
export type StreamDialect = StreamConnectionResponse["dialect"];
export type TaskErrorType = TaskRunError["type"];
export type PublishStore = SetVariableConfig["store"];
export type ObservabilityGranularity = ObservabilityRangeResponse["granularity"];
export type ObservabilityChannel = NonNullable<ObservabilityGetData["query"]>["channel"];

/** What an operation does when it fails: stop the tree, or carry on. */
export type OnError = NonNullable<HttpOperation["on_error"]>;

/** One node of a tool's operation tree — the 11-variant discriminated union. */
export type OperationRequest = NonNullable<ToolResponse["operations"]>[number];

/** The kinds of row a conversation transcript holds. */
export type ConversationItemType = ConversationItemResponse["type"];
export type DeliveryStatus = ConversationItemResponse["delivery_status"];
/** Who wrote the row: `livekit.agents.llm.ChatRole`, plus the `tool` we stamp on
 *  a function call and its output, which LiveKit gives no role of its own. */
export type ConversationItemRole = NonNullable<ConversationItemResponse["role"]>;
/** Which way the row travelled, and whether the end customer saw it. */
export type ConversationItemDirection = ConversationItemResponse["direction"];
export type ConversationItemVisibility = ConversationItemResponse["visibility"];

/** One item on a CoPilot rail: a message, a tool call, or its output. A union
 *  alias has no schema of its own, so this is the only one of the three that
 *  the generator cannot name — `TurnMetrics` and `StoredAgentPlan` come
 *  straight out of `./gen`. */
export type CopilotItem = PublicMessage["item"];

/** One priced line on a call's bill. The document has one shape per stage —
 *  they share `kind`, which is what a reader switches on. */
export type PriceLine =
  | LLMPriceLine
  | STTPriceLine
  | TTSPriceLine
  | RealtimePriceLine
  | AvatarPriceLine;

export type TelephonyAccountStatus = TelephonyAccountResponse["status"];
export type TelephonyProvider = TelephonyAccountResponse["provider"];

/** Which latency the chart is showing. */
export type ObservabilityLatencyMetric = ObservabilityLatencyMetricResponse["metric"];

/** How a call ended, bucketed. */
export type ObservabilityCloseReasonBucket = ObservabilityEndingResponse["bucket"];

/** One entry in the model catalog, whichever stage it belongs to. The document
 *  has a type per stage and no common base — Pydantic inlines an inherited base
 *  into each concrete schema rather than pointing at it — so the shared fields
 *  (`provider`, `model`, `label`, `channel`, `pricing`) are reached through the
 *  union. */
export type CatalogEntry =
  | LLMCatalogEntryResponse
  | STTCatalogEntryResponse
  | TTSCatalogEntryResponse
  | RealtimeCatalogEntryResponse
  | AvatarCatalogEntryResponse
  | NoiseCancellationCatalogEntryResponse;

/** What one catalog entry costs, whichever stage it is priced by. */
export type CatalogPricing =
  | LLMPricing
  | STTPricing
  | TTSPricing
  | RealtimePricing
  | AvatarPricing;
