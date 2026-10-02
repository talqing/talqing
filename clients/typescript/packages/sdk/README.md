# Talqing TypeScript SDK

The TypeScript client for the Talqing `/v1` API, generated from
`openapi/openapi.json`. It is the same set of operations, under the same names,
as the Python SDK — both come from the surface
`backend/api/dataplane/sdk_surface.py` declares.

```bash
npm install @talqing/sdk
```

Node 20 or newer. It ships ESM and CommonJS, so `import` and `require` both
work. Dependency-free and `fetch`-based: `npm install @talqing/sdk` installs one
package and nothing else.

Joining a call from a browser is a separate concern and separate packages —
`@talqing/client` and `@talqing/react` — so a server-side consumer never
installs the LiveKit browser stack. See [Web calls](#web-calls).

## Authentication

```ts
import { TalqingClient } from "@talqing/sdk";

const talqing = new TalqingClient({
  baseUrl: "https://api.in.talqing.com",
  token: process.env.TALQING_API_TOKEN,
});

const agents = await talqing.agents.list();
```

**One token reaches every region.** A token names your organization, not a
region, and every organization exists in every region — so working in a second
region is the same token and a different `baseUrl`. The resources are separate:
an agent you create in `https://api.in.talqing.com` is not in
`https://api.us.talqing.com`, and neither is its call history or its credit
balance.

`baseUrl` is required and has no default. Bundlers inline `NEXT_PUBLIC_*` at
build time, so a missing value cannot be caught at runtime — a localhost
fallback would silently ship a production bundle calling a server that is not
there.

In the dashboard, pass `credentials: "include"` to send the cookie session
instead of a token, and `onUnauthorized` to handle a dead session in one place:

```ts
const baseUrl = process.env.NEXT_PUBLIC_API_URL;
if (!baseUrl) throw new Error("NEXT_PUBLIC_API_URL is required");

const talqing = new TalqingClient({
  baseUrl,
  credentials: "include",
  onUnauthorized: () => router.push("/login"),
});
```

## The shape of it

Operations are reached by resource, exactly as the API names them. Path and
query parameters are one flat object; a request body is keyed by its schema
name.

```ts
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
```

The body key is the generator's one ergonomic cost — hey-api has no `body`
option — and it is uniform, so it is the same shape at every write call site.

## Errors

Every non-2xx throws `TalqingApiError`. There is one error shape, so there is
nothing to branch on:

```ts
import { TalqingApiError } from "@talqing/sdk";

try {
  await talqing.agents.publish({ agent_id });
} catch (error) {
  if (error instanceof TalqingApiError) {
    console.error(error.status, error.message); // 400 config is invalid
    error.errors.forEach(console.error);        // ["llm: unknown model gpt-4.9"]
  }
}
```

## Pagination

Every list endpoint pages the same way, so one helper covers all of them:

```ts
import { paginate } from "@talqing/sdk";

for await (const agent of paginate((q) => talqing.agents.list(q))) {
  console.log(agent.config.name);
}
```

## Credits

The platform fee — charged per minute of voice or video, and on nothing else —
comes out of a prepaid balance. Provider spend is yours, on your own keys, and
never passes through Talqing.

**A workspace has one balance per region**, so this reads the balance belonging
to whichever `baseUrl` the client holds. A call placed with nothing left in that
region never connects, and reads back with `close_reason: "insufficient_credits"`.

```ts
const credits = await talqing.billing.credits.get();
console.log(credits.balance, credits.currency, credits.voice_minutes_remaining);

for await (const entry of paginate((q) => talqing.billing.credits.ledger(q))) {
  console.log(entry.created_at, entry.kind, entry.amount, entry.balance_after);
}
```

Buying more is an admin's call, and returns a hosted checkout page to open in a
browser. The credit lands in the region you bought it from — balances do not
move between regions, so buy against the base URL you meant to top up.

```ts
const { checkout_url } = await talqing.billing.credits.checkout({
  creditCheckoutRequest: { pack: "usd_50" },
});
```

## Live streams

Six endpoints stay open and push events. Each frame is typed, and repeats its
own name in `event`, which is what narrows the union:

```ts
const { stream } = await talqing.conversations.events({ conversation_id });

for await (const event of stream) {
  if (event.event === "assistant.delta") process.stdout.write(event.text);
  if (event.event === "turn" && event.status === "done") break;
}
```

`talqing.copilot.agents.stream`, `copilot.tools.stream` and
`copilot.tasks.stream` work the same way.

## Chats

Text agents never create or join a LiveKit room. Start a chat, then send it
messages — the agent's reply comes back on the same call.

```ts
const chat = await talqing.chats.create({
  createChatRequest: { agent_id, contact_key: "user-42" },
});

const turn = await talqing.chats.messages.create({
  chat_id: chat.id,
  chatMessageRequest: { message: "Hello", client_message_id: crypto.randomUUID() },
});
for (const item of turn.items) console.log(item.text);

await talqing.chats.end({ chat_id: chat.id });
```

A chat stays open until its agent ends it or you call `chats.end`; that is when
its analysis runs and `session.completed` fires. To chat from a browser, mint a
token with `chats.tokens.create` and use `TalqingChat` from `@talqing/client`.

## Web calls

Voice and video agents take a LiveKit room token, and this package mints it —
but only ever on your **server**. `POST /v1/calls/token` needs the editor role,
and a token that can mint one can also create and delete agents, read every
transcript and spend the workspace's credits. It never belongs in a browser.

That is not only about the credential. `contact_key` decides whose history the
call resumes, and `userdata` is merged onto that person's record; on a
server-minted call a browser cannot choose either, because they never leave your
backend.

```ts
// app/api/talqing-token/route.ts — Next.js route handler, on your server.
import { TalqingClient } from "@talqing/sdk";

const talqing = new TalqingClient({
  baseUrl: "https://api.in.talqing.com",
  token: process.env.TALQING_API_TOKEN,
});

export async function POST(request: Request) {
  const user = await authenticate(request); // your sign-in, not ours
  if (!user) return new Response("unauthorized", { status: 401 });

  const call = await talqing.calls.token({
    tokenRequest: {
      agent_id: process.env.TALQING_AGENT_ID,
      contact_key: user.id,
      userdata: { name: user.name, plan: user.plan },
    },
  });

  // Two fields is the whole of what a browser needs to join.
  return Response.json({
    server_url: call.server_url,
    participant_token: call.participant_token,
  });
}
```

`call.session_id` is this call's id, and `GET /v1/calls/{session_id}` answers on
it immediately — keep it if you want the transcript, the cost or the analysis
afterwards. `call.warnings` is what resolving the plan complained about and is
stored nowhere, so log it here or it is gone.

The browser half is [`@talqing/react`](https://www.npmjs.com/package/@talqing/react),
which takes that response and joins:

```bash
npm install @talqing/react
```

```tsx
import { TalqingSessionProvider, useTalqingSession } from "@talqing/react";

function CallSession({ onLeave }: { onLeave: () => void }) {
  const session = useTalqingSession({
    fetchToken: async () => {
      const response = await fetch("/api/talqing-token", { method: "POST" });
      return await response.json();
    },
  });

  useEffect(() => {
    void session.start();
    return () => void session.end();
  }, []);

  return (
    <TalqingSessionProvider session={session}>
      <Call onLeave={onLeave} />
    </TalqingSessionProvider>
  );
}
```

One session is one call: start it when the component mounts and end it on the
way out. Place the second call by mounting a second session — render
`<CallSession>` while a call is up and drop it to hang up — rather than
`start()`ing a session that has already run. A finished session's token names
the room that call used, and the API will not let a second call rejoin it.

There is a third auth branch, `{ client, agentId, callOptions }`, which mints
from the browser using a `TalqingClient` you already hold. It is for **trusted
first-party surfaces only** — an app that is same-origin with the API and signed
in as a real editor, which is what our own dashboard is. If you are embedding a
call widget for your customers, it is not your branch. Passing two branches at
once is a type error rather than a precedence rule.

`@talqing/react` also carries `useTalqingImages` for attaching a photo mid-call,
`useTalqingScreenShare`, `useTalqingFrontendRpcs` for tools that call back into
your page, `useTalqingUserdata`, and `useTalqingAvatarTrack` for a video agent.
`@talqing/client` is the same protocol without React, over a `Room` you manage
yourself.

## Types

Every request and response shape is exported, named as the API names it:

```ts
import type { AgentConfig, AgentResponse, CallOutcome, OperationRequest } from "@talqing/sdk";
```

`Page<T>` is the shape every list endpoint returns. OpenAPI has no generics, so
the document spells each instantiation out (`PageAgentResponse`); TypeScript is
structural, so those satisfy `Page<AgentResponse>` and generic helpers work.

## Regenerating

`src/gen` is generated and checked in. After re-running `openapi/export.py`:

```bash
npm run generate         # rewrite it
npm run generate:check   # or just assert it is current
npm run build            # dist/, which the dashboard resolves through
```

Everything else in `src` is hand-written: the client, the error and the
pagination helper — what the document cannot say.
