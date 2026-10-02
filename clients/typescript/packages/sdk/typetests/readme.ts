/* Every call the README shows, compiled.
 *
 * The README is what a tenant copies first, and the last one went three
 * releases describing `client.listAgents()` — a method the generated surface
 * has never had. Nothing catches that but this file. Keep the two in step: if a
 * snippet changes, change it here too.
 *
 * Nothing runs. The assertions are the type checker's.
 */
import { paginate, TalqingApiError, TalqingClient } from "../src/index.js";
import type { AgentConfig, AgentResponse, CallOutcome, OperationRequest, Page } from "../src/index.js";

// The README's snippets are a Node caller's. This package has no `@types/node`
// — it runs in a browser too — so the two globals they use are declared here
// rather than pulling Node's types into the SDK.
declare const process: {
  env: Record<string, string | undefined>;
  stdout: { write(text: string): void };
};

const talqing = new TalqingClient({
  baseUrl: "https://api.in.talqing.com",
  token: process.env.TALQING_API_TOKEN,
});

/** Authentication — the dashboard's cookie session, and a 401 handled once. */
export function dashboardClient(baseUrl: string, onDead: () => void) {
  return new TalqingClient({ baseUrl, credentials: "include", onUnauthorized: onDead });
}

/** The shape of it. */
export async function shape(agent_id: string, batch_id: string, number_id: string) {
  await talqing.agents.list({ limit: 50 });
  await talqing.agents.get({ agent_id });
  await talqing.agents.versions.rollback({ agent_id, version: 3 });
  await talqing.calls.batches.pause({ batch_id });
  await talqing.telephony.phoneNumbers.assign({
    number_id,
    assignPhoneNumberRequest: { agent_id },
  });

  const agent = await talqing.agents.create({
    createAgentRequest: { config: { name: "Support bot", channel: "text" } },
  });
  await talqing.agents.publish({ agent_id: agent.id });
}

/** Errors — one shape, nothing to guard. */
export async function errors(agent_id: string) {
  try {
    await talqing.agents.publish({ agent_id });
  } catch (error) {
    if (error instanceof TalqingApiError) {
      console.error(error.status, error.message);
      error.errors.forEach((problem) => console.error(problem));
    }
  }
}

/** Pagination. */
export async function everyAgent() {
  for await (const agent of paginate((q) => talqing.agents.list(q))) {
    console.log(agent.config.name);
  }
}

/** Credits — the balance, its ledger, and a checkout. */
export async function credits() {
  const balance = await talqing.billing.credits.get();
  console.log(balance.balance, balance.currency, balance.voice_minutes_remaining);

  for await (const entry of paginate((q) => talqing.billing.credits.ledger(q))) {
    console.log(entry.created_at, entry.kind, entry.amount, entry.balance_after);
  }

  const { checkout_url } = await talqing.billing.credits.checkout({
    creditCheckoutRequest: { pack: "usd_50" },
  });
  return checkout_url;
}

/** Live streams, narrowed by `event`. */
export async function watch(conversation_id: string) {
  const { stream } = await talqing.conversations.events({ conversation_id });
  for await (const event of stream) {
    if (event.event === "assistant.delta") process.stdout.write(event.text);
    if (event.event === "turn" && event.status === "done") break;
  }
}

/** Web calls — minted on the server, and only the two join fields forwarded. */
export async function callToken(agent_id: string, user: { id: string; name: string }) {
  const call = await talqing.calls.token({
    tokenRequest: {
      agent_id,
      contact_key: user.id,
      userdata: { name: user.name, plan: "gold" },
    },
  });
  console.log(call.session_id, call.warnings);
  return { server_url: call.server_url, participant_token: call.participant_token };
}

/** Chats: start, message, end. */
export async function talk(agent_id: string) {
  const chat = await talqing.chats.create({
    createChatRequest: { agent_id, contact_key: "user-42" },
  });
  const turn = await talqing.chats.messages.create({
    chat_id: chat.id,
    chatMessageRequest: { message: "Hello", client_message_id: crypto.randomUUID() },
  });
  for (const item of turn.items) console.log(item.text);
  await talqing.chats.end({ chat_id: chat.id });
}

/** The types the README names, and the `Page<T>` claim it makes. */
export function types(page: Page<AgentResponse>, config: AgentConfig) {
  return {
    page,
    config,
    outcome: "success" satisfies CallOutcome,
    operation: { kind: "say", config: { text: "hi" } } satisfies OperationRequest,
  };
}
