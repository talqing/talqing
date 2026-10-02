/* A compile-time test of the public surface. It is never bundled — `tsc -p
 * tsconfig.json` only reads `src` — but `npm run typecheck` compiles it, so a
 * regeneration that moves a resource, drops a method or changes a parameter
 * shape fails here instead of in a tenant's editor.
 *
 * Nothing in here runs. The assertions are the type checker's.
 */
import { paginate, TalqingApiError, TalqingClient } from "../src/index.js";
import type { AgentConfig, AgentResponse, Page } from "../src/index.js";

const talqing = new TalqingClient({ baseUrl: "https://api.example.com", token: "tq_x" });

export async function surface(id: string, config: AgentConfig) {
  // Resource nesting, and parameters flat rather than wrapped in `path`/`body`.
  const page = await talqing.agents.list({ limit: 50 });
  const everything = await talqing.agents.list(); //  optional arity preserved
  const agent = await talqing.agents.get({ agent_id: id });
  await talqing.agents.update({ agent_id: id, updateAgentRequest: { config } });
  await talqing.agents.publish({ agent_id: id });
  await talqing.agents.versions.rollback({ agent_id: id, version: 3 });
  await talqing.calls.batches.recipients.add({ batch_id: id, addRecipientsRequest: { recipients: [] } });
  await talqing.telephony.phoneNumbers.assign({ number_id: id, assignPhoneNumberRequest: { agent_id: id } });
  await talqing.integrations.triggers.update({ integration_id: id, trigger_id: id, patchIntegrationTriggerRequest: {} });
  await talqing.copilot.agents.send({ agent_id: id, sendMessageRequest: { text: "hi" } });

  // 302 + binary: the followed result, not `unknown`.
  const audio: Blob | File = await talqing.calls.recording.get({ session_id: id });

  // A generic helper still accepts a concrete page — OpenAPI has no generics,
  // TypeScript is structural.
  const typed: Page<AgentResponse> = page;
  for await (const a of paginate((q) => talqing.agents.list(q))) void (a satisfies AgentResponse);

  return { everything, agent, audio, typed };
}

/** The streams are a discriminated union, narrowed by `event`. */
export async function readConversation(conversationId: string) {
  const { stream } = await talqing.conversations.events({ conversation_id: conversationId });
  let reply = "";
  for await (const frame of stream) {
    switch (frame.event) {
      case "conversation.snapshot":
        reply = "";
        void frame.items.length; //  snapshot carries items
        break;
      case "assistant.delta":
        reply += frame.text; //  delta carries text
        break;
      case "assistant.completed":
        reply = frame.text ?? reply;
        break;
      case "item.created":
        void frame.delivery_status; //  a whole ConversationItemResponse
        break;
      case "turn":
        void (frame.status satisfies "running" | "done" | "error" | "canceled");
        break;
      default:
        break;
    }
  }
  return reply;
}

export async function readCopilot(agentId: string) {
  const { stream } = await talqing.copilot.agents.stream({ agent_id: agentId });
  for await (const frame of stream) {
    if (frame.event === "message") void frame.content;
    if (frame.event === "snapshot") void frame.busy;
  }
}

/** The error contract: one shape, no optional fields to guard. */
export function readError(e: unknown) {
  if (e instanceof TalqingApiError) {
    return `${e.status}: ${e.message} (${e.errors.length} field errors)`;
  }
  throw e;
}
