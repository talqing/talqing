import { TalqingClient } from "@talqing/sdk";
import type {
  AddEmailRecipientsRequest,
  AddRecipientsRequest,
  AddWhatsAppRecipientsRequest,
  AgentConfig,
  AssignPhoneNumberRequest,
  BackfillAnalysisRequest,
  CreateCallBatchRequest,
  CreateEmailBatchRequest,
  CreateEmailSendRequest,
  CreateIntegrationRequest,
  CreateIntegrationTriggerRequest,
  CreateStreamConnectionRequest,
  CreateTelephonyAccountRequest,
  CreateToolRequest,
  CreateWebhookRequest,
  CreditCheckoutRequest,
  FaqEntryInput,
  ImportPhoneNumbersRequest,
  OutboundCallRequest,
  PatchCallBatchRequest,
  PatchConversationRequest,
  PatchEmailBatchRequest,
  PatchEmailRecipientRequest,
  PatchEmailSendRequest,
  PatchIntegrationRequest,
  PatchIntegrationTriggerRequest,
  PatchPhoneNumberRequest,
  PatchStreamConnectionRequest,
  PatchTaskRequest,
  PatchTelephonyAccountRequest,
  PatchToolRequest,
  PatchWebhookRequest,
  PreviewAnalysisRequest,
  UpdateFaqEntryRequest,
  RedraftRequest,
  RunTaskRequest,
  RunToolRequest,
  SelectRecipientsRequest,
} from "@talqing/sdk";
import { localTimezone } from "@/lib/date";
import { control } from "@/lib/control";
import type { InviteRequest, PatchOrgRequest, RoleRequest } from "@/lib/control";
import { ACTIVE_REGION } from "@/lib/regions";

const LOGIN_PATH = "/login";

/** The SDK client, pointed at ONE REGION. New code should call this directly —
 *  `talqing.agents.get(…)` — rather than adding to the `api` facade below.
 *
 *  The base URL is a runtime value, not an inlined one: a single static build
 *  serves every region, and which region this tab is looking at comes from
 *  `lib/regions.ts`. Everything reachable here is that region's — its agents,
 *  its calls, its phone numbers, its credit balance — and switching region is a
 *  full navigation rather than a re-pointed client, because everything on screen
 *  belongs to the region being left.
 *
 *  Identity is not here. Sign-in, organizations, members, invites and tokens are
 *  the CONTROL plane's, and reach it through `lib/control` — two clients, not one
 *  client pointed at two hosts. */
export const talqing = new TalqingClient({
  baseUrl: ACTIVE_REGION.api_url,
  credentials: "include",
  // Session expiry is a property of the session, not of whichever call happened
  // to notice it first, so it is handled once here instead of in every page's
  // catch block. A full navigation (rather than router.replace) is deliberate:
  // it drops all in-memory state belonging to the signed-out user.
  onUnauthorized: () => {
    if (typeof window === "undefined") return;
    if (window.location.pathname === LOGIN_PATH) return;
    window.location.replace(LOGIN_PATH);
  },
});

/** Kept for the pages that still import it. */
export const dashboardClient = talqing;

/**
 * Read an SSE stream until it is closed, dispatching each frame.
 *
 * The SDK hands back an async iterator of a discriminated union; this is the
 * React-shaped wrapper around it — start it in an effect, call the returned
 * function to stop. Narrow with `switch (event.event)` inside `onEvent`.
 */
export function subscribe<T extends { event: string }>(
  open: (init: { signal: AbortSignal }) => Promise<{ stream: AsyncIterable<T> }>,
  onEvent: (event: T) => void,
): () => void {
  const controller = new AbortController();
  void (async () => {
    try {
      const { stream } = await open({ signal: controller.signal });
      for await (const event of stream) {
        if (controller.signal.aborted) return;
        onEvent(event);
      }
    } catch (error) {
      // The transport retries with backoff on its own; an error that escapes
      // here means the subscription is over, and the page re-reads on mount.
      // Aborting is us, so it is not worth a line — but everything else is,
      // including an opener that throws before any request goes out. Swallowed,
      // the two look identical from the outside: a panel that says "Loading…"
      // and never stops.
      if (!controller.signal.aborted) console.error("SSE subscription ended", error);
    }
  })();
  return () => controller.abort();
}

export const api = {
  catalog: () => talqing.catalog.get(),
  // The models `catalog()` deliberately leaves out: a provider with hundreds of
  // them is searched a page at a time instead of listed.
  searchModels: (query: Parameters<typeof talqing.catalog.models.search>[0] = {}) =>
    talqing.catalog.models.search(query),
  listModelHosts: (query: Parameters<typeof talqing.catalog.models.hosts>[0]) =>
    talqing.catalog.models.hosts(query),
  listAvatars: (query: Parameters<typeof talqing.catalog.avatars.list>[0] = {}) =>
    talqing.catalog.avatars.list(query),
  listVoices: (query: Parameters<typeof talqing.catalog.voices.list>[0]) =>
    talqing.catalog.voices.list(query),
  addSharedVoice: (b: Parameters<typeof talqing.catalog.voices.elevenlabs.add>[0]["addVoiceRequest"]) =>
    talqing.catalog.voices.elevenlabs.add({ addVoiceRequest: b }),
  elevenLabsVoiceSettings: (voiceId: string) =>
    talqing.catalog.voices.elevenlabs.settings({ voice_id: voiceId }),

  listAgents: (query: Parameters<typeof talqing.agents.list>[0] = {}) => talqing.agents.list(query),
  listAllAgents: async () => {
    const out = [] as Awaited<ReturnType<typeof talqing.agents.list>>["items"];
    for (let offset = 0; ; offset += 200) {
      const page = await talqing.agents.list({ limit: 200, offset });
      out.push(...page.items);
      if (!page.has_more) return out;
    }
  },
  /* The browser's own zone is the only sensible starting clock for an agent
     created here, and it is the one thing an API or MCP caller cannot supply —
     there is no browser to ask, so `timezone` stays null on those paths. */
  createAgent: (name: string) =>
    talqing.agents.create({
      createAgentRequest: { config: { name, timezone: localTimezone() } },
    }),
  /** Promote a whole config — a call's inline definition, say — into an agent.
   *  Any tool it defines inline is created and published with it. */
  createAgentFromConfig: (config: AgentConfig) =>
    talqing.agents.create({ createAgentRequest: { config } }),
  getAgent: (id: string) => talqing.agents.get({ agent_id: id }),
  updateAgent: (id: string, config: AgentConfig) =>
    talqing.agents.update({ agent_id: id, updateAgentRequest: { config } }),
  validateAgent: (id: string) => talqing.agents.validate({ agent_id: id }),
  publishAgent: (id: string) => talqing.agents.publish({ agent_id: id }),
  getAgentVersion: (id: string, version: number) =>
    talqing.agents.versions.get({ agent_id: id, version }),
  rollbackAgentVersion: (id: string, version: number) =>
    talqing.agents.versions.rollback({ agent_id: id, version }),
  deleteAgent: (id: string) => talqing.agents.delete({ agent_id: id }),

  // No callToken here: WebCall mints through the SDK's `useTalqingSession`,
  // which is the only place in the dashboard that starts a web call.
  listCalls: (query: Parameters<typeof talqing.calls.list>[0] = {}) => talqing.calls.list(query),
  callStats: (query: Parameters<typeof talqing.calls.stats>[0] = {}) => talqing.calls.stats(query),
  getCall: (id: string) => talqing.calls.get({ session_id: id }),
  // No getCallRecording here: the player takes `recording.url` off the call
  // detail and hands it to an <audio> element, so the bytes never pass through
  // the dashboard's JS at all.
  deleteCallRecording: (id: string) => talqing.calls.recording.delete({ session_id: id }),
  // No deleteCall either: the endpoint exists and the SDK exposes it, but the
  // dashboard has no control that erases a whole call's content, so a wrapper
  // here would be a method nothing can reach. Add it with the control.
  backfillCallAnalysis: (b: BackfillAnalysisRequest) =>
    talqing.calls.analysis.backfill({ backfillAnalysisRequest: b }),
  previewCallAnalysis: (id: string, b: PreviewAnalysisRequest) =>
    talqing.calls.analysis.preview({ session_id: id, previewAnalysisRequest: b }),
  observability: (query: Parameters<typeof talqing.observability.get>[0] = {}) =>
    talqing.observability.get(query),

  listConversations: (query: Parameters<typeof talqing.conversations.list>[0] = {}) =>
    talqing.conversations.list(query),
  getConversation: (id: string) => talqing.conversations.get({ conversation_id: id }),
  listConversationItems: (id: string, limit = 200, offset = 0, order: "asc" | "desc" = "asc") =>
    talqing.conversations.items.list({ conversation_id: id, limit, offset, order }),
  listConversationSessions: (id: string, limit = 100, offset = 0) =>
    talqing.conversations.sessions.list({ conversation_id: id, limit, offset }),
  getConversationSnapshot: (id: string) => talqing.conversations.snapshot({ conversation_id: id }),
  listConversationTrace: (id: string, window: { after?: string; before?: string } = {}) =>
    talqing.conversations.trace({ conversation_id: id, ...window }),
  getChatDetail: (id: string) => talqing.chats.detail({ chat_id: id }),
  rerunChatAnalysis: (id: string) => talqing.chats.analysis.rerun({ chat_id: id }),
  previewChatAnalysis: (id: string, b: PreviewAnalysisRequest) =>
    talqing.chats.analysis.preview({ chat_id: id, previewAnalysisRequest: b }),
  patchConversation: (id: string, b: PatchConversationRequest) =>
    talqing.conversations.update({ conversation_id: id, patchConversationRequest: b }),

  eventTypes: () => talqing.webhooks.eventTypes(),
  listWebhooks: () => talqing.webhooks.list(),
  createWebhook: (b: CreateWebhookRequest) => talqing.webhooks.create({ createWebhookRequest: b }),
  patchWebhook: (id: string, b: PatchWebhookRequest) =>
    talqing.webhooks.update({ webhook_id: id, patchWebhookRequest: b }),
  deleteWebhook: (id: string) => talqing.webhooks.delete({ webhook_id: id }),
  rotateWebhookSecret: (id: string) => talqing.webhooks.rotateSecret({ webhook_id: id }),
  testWebhook: (id: string) => talqing.webhooks.test({ webhook_id: id }),
  webhookDeliveries: (id: string, limit = 50, offset = 0) =>
    talqing.webhooks.deliveries({ webhook_id: id, limit, offset }),

  listTools: () => talqing.tools.list(),
  createTool: (b: CreateToolRequest) => talqing.tools.create({ createToolRequest: b }),
  getTool: (id: string) => talqing.tools.get({ tool_id: id }),
  patchTool: (id: string, b: PatchToolRequest) =>
    talqing.tools.update({ tool_id: id, patchToolRequest: b }),
  deleteTool: (id: string) => talqing.tools.delete({ tool_id: id }),
  validateTool: (id: string) => talqing.tools.validate({ tool_id: id }),
  runTool: (id: string, b: RunToolRequest) => talqing.tools.run({ tool_id: id, runToolRequest: b }),
  publishTool: (id: string, changelog?: string) =>
    talqing.tools.publish({ tool_id: id, publishToolRequest: { changelog } }),
  getToolVersion: (id: string, version: number) =>
    talqing.tools.versions.get({ tool_id: id, version }),
  rollbackToolVersion: (id: string, version: number) =>
    talqing.tools.versions.rollback({ tool_id: id, version }),

  listTasks: () => talqing.tasks.list(),
  /* Same argument as `createAgent`: the browser's zone is the only sensible
     starting clock, and it is the one thing an API or MCP caller cannot supply. */
  createTask: (name: string) =>
    talqing.tasks.create({ createTaskRequest: { config: { name, timezone: localTimezone() } } }),
  getTask: (id: string) => talqing.tasks.get({ task_id: id }),
  updateTask: (id: string, b: PatchTaskRequest) =>
    talqing.tasks.update({ task_id: id, patchTaskRequest: b }),
  deleteTask: (id: string) => talqing.tasks.delete({ task_id: id }),
  validateTask: (id: string) => talqing.tasks.validate({ task_id: id }),
  publishTask: (id: string) => talqing.tasks.publish({ task_id: id }),
  getTaskVersion: (id: string, version: number) =>
    talqing.tasks.versions.get({ task_id: id, version }),
  rollbackTaskVersion: (id: string, version: number) =>
    talqing.tasks.versions.rollback({ task_id: id, version }),
  /* Runs for real — the tenant's endpoints, credits and tokens. The Run panel's
     button says so; this is only the wire. */
  runTask: (id: string, b: RunTaskRequest) =>
    talqing.tasks.runs.create({ task_id: id, runTaskRequest: b }),
  listTaskRuns: (id: string, limit = 20) => talqing.tasks.runs.list({ task_id: id, limit }),
  getTaskRun: (taskId: string, runId: string) =>
    talqing.tasks.runs.get({ task_id: taskId, run_id: runId }),

  listIntegrations: () => talqing.integrations.list(),
  integrationCatalog: () => talqing.integrations.catalog(),
  createIntegration: (b: CreateIntegrationRequest) =>
    talqing.integrations.create({ createIntegrationRequest: b }),
  getIntegration: (id: string) => talqing.integrations.get({ integration_id: id }),
  listIntegrationMcpTools: (id: string) => talqing.integrations.mcpTools.list({ integration_id: id }),
  patchIntegration: (id: string, b: PatchIntegrationRequest) =>
    talqing.integrations.update({ integration_id: id, patchIntegrationRequest: b }),
  deleteIntegration: (id: string) => talqing.integrations.delete({ integration_id: id }),
  listIntegrationTriggers: (integrationId: string) =>
    talqing.integrations.triggers.list({ integration_id: integrationId }),
  createIntegrationTrigger: (integrationId: string, b: CreateIntegrationTriggerRequest) =>
    talqing.integrations.triggers.create({
      integration_id: integrationId,
      createIntegrationTriggerRequest: b,
    }),
  patchIntegrationTrigger: (
    integrationId: string,
    triggerId: string,
    b: PatchIntegrationTriggerRequest,
  ) =>
    talqing.integrations.triggers.update({
      integration_id: integrationId,
      trigger_id: triggerId,
      patchIntegrationTriggerRequest: b,
    }),
  deleteIntegrationTrigger: (integrationId: string, triggerId: string) =>
    talqing.integrations.triggers.delete({
      integration_id: integrationId,
      trigger_id: triggerId,
    }),
  oauthStartUrl: (provider: string, integrationId?: string) =>
    talqing.oauthStartUrl(provider, integrationId),

  credits: (topup?: string) => talqing.billing.credits.get(topup ? { topup } : {}),
  creditLedger: (limit = 50, offset = 0) => talqing.billing.credits.ledger({ limit, offset }),
  buyCredits: (pack: CreditCheckoutRequest["pack"]) =>
    talqing.billing.credits.checkout({ creditCheckoutRequest: { pack } }),

  listFaqs: () => talqing.faqs.list(),
  createFaq: (name: string) => talqing.faqs.create({ faqDefinition: { name } }),
  getFaq: (id: string) => talqing.faqs.get({ faq_id: id }),
  renameFaq: (id: string, name: string) =>
    talqing.faqs.update({ faq_id: id, updateFaqRequest: { name } }),
  deleteFaq: (id: string) => talqing.faqs.delete({ faq_id: id }),
  createFaqEntries: (id: string, entries: FaqEntryInput[]) =>
    talqing.faqs.entries.create({ faq_id: id, createFaqEntriesRequest: { entries } }),
  updateFaqEntry: (id: string, entryId: string, b: UpdateFaqEntryRequest) =>
    talqing.faqs.entries.update({ faq_id: id, entry_id: entryId, updateFaqEntryRequest: b }),
  deleteFaqEntry: (id: string, entryId: string) =>
    talqing.faqs.entries.delete({ faq_id: id, entry_id: entryId }),

  listSecrets: () => talqing.secrets.list(),
  createSecret: (b: { name: string; value: string }) =>
    talqing.secrets.create({ createSecretRequest: b }),
  deleteSecret: (id: string) => talqing.secrets.delete({ secret_id: id }),

  listProviderKeys: () => talqing.providerKeys.list(),
  setProviderKey: (provider: string, apiKey: string) =>
    talqing.providerKeys.set({ provider, setProviderKeyRequest: { api_key: apiKey } }),
  deleteProviderKey: (provider: string) => talqing.providerKeys.delete({ provider }),

  listTelephonyProviders: (query: Parameters<typeof talqing.telephony.providers.list>[0] = {}) =>
    talqing.telephony.providers.list(query),
  listTelephonyAccounts: (query: Parameters<typeof talqing.telephony.accounts.list>[0] = {}) =>
    talqing.telephony.accounts.list(query),
  createTelephonyAccount: (b: CreateTelephonyAccountRequest) =>
    talqing.telephony.accounts.create({ createTelephonyAccountRequest: b }),
  getTelephonyAccount: (id: string) => talqing.telephony.accounts.get({ account_id: id }),
  patchTelephonyAccount: (id: string, b: PatchTelephonyAccountRequest) =>
    talqing.telephony.accounts.update({ account_id: id, patchTelephonyAccountRequest: b }),
  deleteTelephonyAccount: (id: string) => talqing.telephony.accounts.delete({ account_id: id }),
  provisionTelephonyAccount: (id: string) =>
    talqing.telephony.accounts.provision({ account_id: id }),
  listRemoteNumbers: (
    accountId: string,
    query: Omit<Parameters<typeof talqing.telephony.accounts.remoteNumbers>[0], "account_id"> = {},
  ) => talqing.telephony.accounts.remoteNumbers({ account_id: accountId, ...query }),
  listPhoneNumbers: (query: Parameters<typeof talqing.telephony.phoneNumbers.list>[0] = {}) =>
    talqing.telephony.phoneNumbers.list(query),
  importPhoneNumbers: (b: ImportPhoneNumbersRequest) =>
    talqing.telephony.phoneNumbers.import({ importPhoneNumbersRequest: b }),
  getPhoneNumber: (id: string) => talqing.telephony.phoneNumbers.get({ number_id: id }),
  patchPhoneNumber: (id: string, b: PatchPhoneNumberRequest) =>
    talqing.telephony.phoneNumbers.update({ number_id: id, patchPhoneNumberRequest: b }),
  provisionPhoneNumber: (id: string) => talqing.telephony.phoneNumbers.provision({ number_id: id }),
  assignPhoneNumber: (id: string, b: AssignPhoneNumberRequest) =>
    talqing.telephony.phoneNumbers.assign({ number_id: id, assignPhoneNumberRequest: b }),
  unassignPhoneNumber: (id: string) => talqing.telephony.phoneNumbers.unassign({ number_id: id }),

  listStreamConnections: (query: Parameters<typeof talqing.telephony.streams.list>[0] = {}) =>
    talqing.telephony.streams.list(query),
  createStreamConnection: (b: CreateStreamConnectionRequest) =>
    talqing.telephony.streams.create({ createStreamConnectionRequest: b }),
  getStreamConnection: (id: string) => talqing.telephony.streams.get({ connection_id: id }),
  patchStreamConnection: (id: string, b: PatchStreamConnectionRequest) =>
    talqing.telephony.streams.update({ connection_id: id, patchStreamConnectionRequest: b }),
  deleteStreamConnection: (id: string) =>
    talqing.telephony.streams.delete({ connection_id: id }),

  createOutboundCall: (b: OutboundCallRequest) =>
    talqing.calls.outbound({ outboundCallRequest: b }),

  createCallBatch: (b: CreateCallBatchRequest) =>
    talqing.calls.batches.create({ createCallBatchRequest: b }),
  listCallBatches: (query: Parameters<typeof talqing.calls.batches.list>[0] = {}) =>
    talqing.calls.batches.list(query),
  getCallBatch: (id: string) => talqing.calls.batches.get({ batch_id: id }),
  patchCallBatch: (id: string, b: PatchCallBatchRequest) =>
    talqing.calls.batches.update({ batch_id: id, patchCallBatchRequest: b }),
  listCallBatchRecipients: (
    id: string,
    query: Omit<Parameters<typeof talqing.calls.batches.recipients.list>[0], "batch_id"> = {},
  ) => talqing.calls.batches.recipients.list({ batch_id: id, ...query }),
  addCallBatchRecipients: (id: string, b: AddRecipientsRequest) =>
    talqing.calls.batches.recipients.add({ batch_id: id, addRecipientsRequest: b }),
  pauseCallBatch: (id: string) => talqing.calls.batches.pause({ batch_id: id }),
  resumeCallBatch: (id: string) => talqing.calls.batches.resume({ batch_id: id }),
  cancelCallBatch: (id: string) => talqing.calls.batches.cancel({ batch_id: id }),
  deleteCallBatch: (id: string) => talqing.calls.batches.delete({ batch_id: id }),

  listEmailSenders: (integrationId: string) =>
    talqing.email.senders.list({ integration_id: integrationId }),
  createEmailBatch: (b: CreateEmailBatchRequest) =>
    talqing.email.batches.create({ createEmailBatchRequest: b }),
  listEmailBatches: (query: Parameters<typeof talqing.email.batches.list>[0] = {}) =>
    talqing.email.batches.list(query),
  getEmailBatch: (id: string) => talqing.email.batches.get({ batch_id: id }),
  patchEmailBatch: (id: string, b: PatchEmailBatchRequest) =>
    talqing.email.batches.update({ batch_id: id, patchEmailBatchRequest: b }),
  pauseEmailBatch: (id: string) => talqing.email.batches.pause({ batch_id: id }),
  resumeEmailBatch: (id: string) => talqing.email.batches.resume({ batch_id: id }),
  cancelEmailBatch: (id: string) => talqing.email.batches.cancel({ batch_id: id }),
  deleteEmailBatch: (id: string) => talqing.email.batches.delete({ batch_id: id }),
  listEmailBatchRecipients: (
    id: string,
    query: Omit<Parameters<typeof talqing.email.batches.recipients.list>[0], "batch_id"> = {},
  ) => talqing.email.batches.recipients.list({ batch_id: id, ...query }),
  addEmailBatchRecipients: (id: string, b: AddEmailRecipientsRequest) =>
    talqing.email.batches.recipients.add({ batch_id: id, addEmailRecipientsRequest: b }),
  addWhatsAppBatchRecipients: (id: string, b: AddWhatsAppRecipientsRequest) =>
    talqing.whatsapp.batches.recipients.add({ batch_id: id, addWhatsAppRecipientsRequest: b }),
  patchEmailBatchRecipient: (id: string, recipientId: string, b: PatchEmailRecipientRequest) =>
    talqing.email.batches.recipients.update({
      batch_id: id,
      recipient_id: recipientId,
      patchEmailRecipientRequest: b,
    }),
  skipEmailBatchRecipients: (id: string, b: SelectRecipientsRequest) =>
    talqing.email.batches.recipients.skip({ batch_id: id, selectRecipientsRequest: b }),
  restoreEmailBatchRecipients: (id: string, b: SelectRecipientsRequest) =>
    talqing.email.batches.recipients.restore({ batch_id: id, selectRecipientsRequest: b }),
  redraftEmailBatchRecipients: (id: string, b: RedraftRequest) =>
    talqing.email.batches.recipients.redraft({ batch_id: id, redraftRequest: b }),
  createEmailSend: (id: string, b: CreateEmailSendRequest) =>
    talqing.email.batches.sends.create({ batch_id: id, createEmailSendRequest: b }),
  listEmailSends: (
    id: string,
    query: Omit<Parameters<typeof talqing.email.batches.sends.list>[0], "batch_id"> = {},
  ) => talqing.email.batches.sends.list({ batch_id: id, ...query }),
  getEmailSend: (id: string, sendId: string) =>
    talqing.email.batches.sends.get({ batch_id: id, send_id: sendId }),
  patchEmailSend: (id: string, sendId: string, b: PatchEmailSendRequest) =>
    talqing.email.batches.sends.update({
      batch_id: id,
      send_id: sendId,
      patchEmailSendRequest: b,
    }),
  pauseEmailSend: (id: string, sendId: string) =>
    talqing.email.batches.sends.pause({ batch_id: id, send_id: sendId }),
  resumeEmailSend: (id: string, sendId: string) =>
    talqing.email.batches.sends.resume({ batch_id: id, send_id: sendId }),
  cancelEmailSend: (id: string, sendId: string) =>
    talqing.email.batches.sends.cancel({ batch_id: id, send_id: sendId }),
};

/**
 * The control plane's half: who you are, which organizations you belong to, who
 * is in them, and the access tokens you hold.
 *
 * Separate from `api` above because it goes to a different host. Nothing here is
 * regional — one identity, one member list, one token that reaches every region
 * — and nothing above it is global.
 */
export const controlApi = {
  logout: () => control.auth.logout(),
  me: () => control.auth.me(),

  listRegions: () => control.regions.list(),

  listOrgs: () => control.orgs.list(),
  createOrg: (name: string) => control.orgs.create({ orgNameRequest: { name } }),
  switchOrg: (orgId: string) => control.orgs.switch({ org_id: orgId }),
  patchOrg: (b: PatchOrgRequest) => control.orgs.update({ patchOrgRequest: b }),
  orgMembers: () => control.orgs.members.list(),
  setMemberRole: (userId: string, role: RoleRequest["role"]) =>
    control.orgs.members.setRole({ user_id: userId, roleRequest: { role } }),
  removeMember: (userId: string) => control.orgs.members.remove({ user_id: userId }),
  leaveOrg: () => control.orgs.leave(),
  listInvites: () => control.orgs.invites.list(),
  createInvite: (email: string, role: InviteRequest["role"]) =>
    control.orgs.invites.create({ inviteRequest: { email, role } }),
  revokeInvite: (id: string) => control.orgs.invites.revoke({ invite_id: id }),

  listTokens: () => control.tokens.list(),
  createToken: (name: string) => control.tokens.create({ createTokenRequest: { name } }),
  deleteToken: (id: string) => control.tokens.delete({ token_id: id }),
  mcpToken: () => control.tokens.mcp(),
};
