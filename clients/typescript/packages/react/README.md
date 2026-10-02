# @talqing/react

React bindings for Talqing web calls: join a voice or video agent from a browser,
attach a photo mid-call, share a screen, read and write the session's userdata,
and answer the agent when a tool calls back into your page.

```bash
npm install @talqing/react
```

npm 7+ and pnpm install the peers for you — `@talqing/client`, `@talqing/sdk`,
`livekit-client`, `@livekit/components-react`, `react` and `react-dom`. Yarn only
warns, so on Yarn:

```bash
yarn add @talqing/react @talqing/client @talqing/sdk livekit-client @livekit/components-react
```

## Mint the token on your server

`POST /v1/calls/token` needs the **editor** role, and a Talqing credential that
can mint a call token can also create and delete agents, read every transcript
and spend the workspace's credits. **Never ship one to a browser.**

`contact_key` and `userdata` are the second reason. `contact_key` decides whose
history the call resumes and `userdata` is merged onto that person's record — so
on a server-minted call, the browser cannot choose either. They never leave your
backend.

```ts
// app/api/talqing-token/route.ts — your server, with @talqing/sdk
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

  if (call.warnings.length) console.warn(call.warnings); // stored nowhere else
  return Response.json({
    server_url: call.server_url,
    participant_token: call.participant_token,
  });
}
```

## Join from the browser

```tsx
"use client";
import { useEffect } from "react";
import {
  TalqingSessionProvider,
  useAgent,
  useSessionMessages,
  useTalqingConnection,
  useTalqingSession,
} from "@talqing/react";

export function CallSession({ onLeave }: { onLeave: () => void }) {
  const session = useTalqingSession({
    fetchToken: async () => {
      const response = await fetch("/api/talqing-token", { method: "POST" });
      return await response.json();
    },
  });

  useEffect(() => {
    // The token is minted inside start(), so an API refusal — an unpublished
    // agent, a workspace out of credits — arrives here.
    session.start().catch(onLeave);
    return () => void session.end();
    // eslint-disable-next-line react-hooks/exhaustive-deps -- mount and unmount only
  }, []);

  return (
    <TalqingSessionProvider session={session}>
      <CallUI onLeave={onLeave} />
    </TalqingSessionProvider>
  );
}

function CallUI({ onLeave }: { onLeave: () => void }) {
  const { isConnected, end } = useTalqingConnection();
  const agent = useAgent();
  const { messages } = useSessionMessages();

  return (
    <div>
      <p>{isConnected ? `agent: ${agent.state}` : "connecting…"}</p>
      {messages.map((message) => (
        <p key={message.id}>
          <b>{message.from?.isLocal ? "You" : "Agent"}</b> {message.message}
        </p>
      ))}
      <button onClick={() => { void end(); onLeave(); }}>End call</button>
    </div>
  );
}
```

**One session is one call.** Start it when the component mounts and end it on the
way out. Place a second call by mounting a second session — give the component a
`key` that changes per call. A finished session's token names the room that call
used, and the API will not let a second call rejoin it.

**The room's state is not the agent's state.** `isConnected` goes true partway
through the join, while the agent is still starting up. Wait for
`useAgent().state === "listening"` before telling the caller to speak.

## The three ways to authorize a call

Exactly one per call, and passing two is a type error rather than a precedence
rule.

| | When |
| --- | --- |
| `{ token }` | Your server already minted, and you forwarded `server_url` and `participant_token`. |
| `{ fetchToken }` | Your endpoint mints. Called once, when the call starts. |
| `{ client, agentId, callOptions }` | **Trusted first-party surfaces only.** Mints from the browser with a `TalqingClient` you already hold — an app same-origin with the API, signed in as a real editor. Not for a widget you embed for your customers. |

`callOptions` is on the third branch alone, because it carries `contact_key` and
`userdata`. `roomOptions` rides on all three.

The third branch is the only one where this browser is what minted, so it is the
only one where `session.warnings` fills in. On the other two your own server
received them.

## What else is here

| | |
| --- | --- |
| `useTalqingImages(room)` | Send a photo to the agent mid-call and follow what became of it — four states, because the agent can go away mid-upload. |
| `useTalqingScreenShare()` | Share a screen on the settings that keep it legible. `start()` must run inside a click. |
| `useTalqingUserdata()` | Read and write the live session's userdata over RPC. |
| `useTalqingRpcHandlers(handlers)` | Answer a tool's `frontend_rpc` calls into your page. `useTalqingFrontendRpcs()` is the declarative version. |
| `useTalqingAvatarTrack()` | The track to render for a video agent. |
| `TALQING_SCREEN_SHARE_CAPTURE` / `_PUBLISH` | The measured capture settings, exported so you can read them. |

## The escape hatch

**This layer only ever adds.** Anything LiveKit can do, you can do — take
`useTalqingConnection().room` and use `livekit-client` directly. We never remove
a capability to make our own surface look tidier, and we do not re-wrap LiveKit
to rename it: `useAgent`, `useSessionMessages` and `VideoTrack` are re-exported
here under their own names, and they are the same modules
`@livekit/components-react` exports.

That guarantee is why `livekit-client` and `@livekit/components-react` are peer
dependencies rather than dependencies: the `Room` we hand you must be the same
class your own imports and `instanceof` resolve to.

## Without React

[`@talqing/client`](https://www.npmjs.com/package/@talqing/client) is the same
call protocol as plain functions over a `Room` you manage yourself.
[`@talqing/sdk`](https://www.npmjs.com/package/@talqing/sdk) is the rest of the
API. Full reference: <https://docs.talqing.com/sdks/react>.
