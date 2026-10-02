# @talqing/client

The Talqing call protocol, over a LiveKit `Room`. Framework-free.

```bash
npm install @talqing/client livekit-client
```

Everything here is something only Talqing can write: the mint-once token source,
the two userdata RPC methods, the `talqing.images` byte-stream topic and its
acknowledgement, the `talqing.frontend_rpc` envelope, and the capture settings
that decide whether the agent can read a shared screen. **Nothing here renames
LiveKit** — for a room, a track or a participant, use `livekit-client` directly.

**If you are writing React, install
[`@talqing/react`](https://www.npmjs.com/package/@talqing/react) instead.** It
re-exports all of this and adds the hooks.

## Joining a call

Your **server** mints the token with `POST /v1/calls/token` — see
[`@talqing/sdk`](https://www.npmjs.com/package/@talqing/sdk) — and the browser
receives two strings. A Talqing credential that can mint a call token can also
create and delete agents, read every transcript and spend the workspace's
credits, so it never belongs in browser code.

```ts
import { Room } from "livekit-client";
import { createTalqingTokenSource, TALQING_AUDIO_CAPTURE_DEFAULTS } from "@talqing/client";

const room = new Room({ audioCaptureDefaults: TALQING_AUDIO_CAPTURE_DEFAULTS });

const tokenSource = createTalqingTokenSource(async () => {
  const response = await fetch("/api/talqing-token", { method: "POST" });
  return await response.json(); // { server_url, participant_token }
});
```

The source mints **once**, however often it is asked. That is load-bearing:
minting files the session row the call is later read back from, and LiveKit asks
three times per call — to warm the connection, to connect, and once more as the
call ends. Freshness comes from a new source per call, never from re-minting
inside one.

Leave `TALQING_AUDIO_CAPTURE_DEFAULTS` on. Echo cancellation is what stops the
agent hearing its own speech through the microphone and interrupting itself,
which sounds exactly like a bad model.

## Userdata

What the agent knows about the person on the call, readable as
`{{userdata.field}}` from its prompt and its tools.

```ts
import { getTalqingUserdata, setTalqingUserdata } from "@talqing/client";

await setTalqingUserdata(room, { cart_total: 4200 }); // → { ok, error? }
const { tier } = await getTalqingUserdata(room, ["tier"]);
```

Both are RPCs to the worker running the call, with a 3-second timeout by
default. `set` resolves `{ ok: false }` when the agent has not joined — a write
that did not land is a state you can render — while `get` **throws**, because a
read that returned nothing is not.

## Images

```ts
import { onTalqingImageResult, sendTalqingImage } from "@talqing/client";

const stop = onTalqingImageResult(room, ({ streamId, ok, error }) => render(streamId, ok, error));
const image = await sendTalqingImage(room, file); // validates, downscales, sends
```

`sendTalqingImage` resolves once the bytes are on their way; the verdict arrives
on the acknowledgement. There is one **even on the happy path**, because the
agent deliberately stays silent when a photo lands — the caller is
mid-conversation and about to say what it is for — so silence cannot double as
"it arrived". Nothing acknowledging within 30 seconds means the agent went away
mid-upload, which is a third outcome and not a failure you can describe.

`talqingImageRejection(file)`, `talqingDownscaleImage(file)` and
`talqingImageDataUrl(file)` are the three steps on their own. The last produces
the `data:` URL a **text** message carries.

## Chats

The one thing here with no room behind it: a text chat, over HTTP. Your
**server** starts the chat with `POST /v1/chats`, mints a token for it with
`POST /v1/chats/{id}/token`, and hands the browser that response unchanged. The
token opens that one chat and nothing else.

```ts
import { TalqingChat } from "@talqing/client";

const chat = new TalqingChat({
  fetchToken: async () => {
    const response = await fetch("/api/talqing-chat-token", { method: "POST" });
    return await response.json(); // { chat_id, token, expires_at, api_url }
  },
});

const { events, result } = chat.send("Where is my order?");
for await (const event of events) {
  if (event.event === "assistant.delta") append(event.text);
}
const turn = await result; // { status, items, chat_status, … }
```

`events` and `result` are two views of one request; use either alone. A turn's
`status` is `canceled` when the person sent a newer message first — that
message's own `send` carries the reply — and `chat_status` is `ended` when the
agent closed the chat. `send` takes `{ images }` as `data:` URLs, which
`talqingImageDataUrl(file)` produces.

`chat.items()` is the chat so far, oldest first, and `chat.end()` ends it. A
browser only ever receives what the person can see — never the agent's tool
calls. A token lasts an hour, so `fetchToken` is called again shortly before
each one expires: have it mint a token for the **same** chat rather than start a
new one. A refused request throws `TalqingChatError`, with the HTTP `status`.

## Frontend RPC

A tool's `frontend_rpc` operation calls into your page. The agent sends one
envelope method and this dispatches on `envelope.method`, so you register once
rather than per tool.

```ts
import { registerTalqingRpcHandlers, TalqingRpcError } from "@talqing/client";

const stop = registerTalqingRpcHandlers(room, {
  open_page: async ({ payload }) => {
    const { href } = JSON.parse(payload) as { href: string };
    navigate(href);
    return JSON.stringify({ ok: true });
  },
  cancel_order: async () => {
    throw new TalqingRpcError(1403, "the customer declined");
  },
});
```

A `"*"` key catches everything unmatched and receives the whole envelope. An
unknown method with no wildcard answers RPC code `1404`; anything else you throw
becomes `1500` with its message.

## Screen share

`TALQING_SCREEN_SHARE_CAPTURE` and `TALQING_SCREEN_SHARE_PUBLISH` are the four
settings that decide whether the agent can read what is on screen — h264 at one
frame a second, 1920×1080, `contentHint: "text"`. Publishing on an SVC codec
costs you legibility, and it is the first thing to check if the agent starts
misreading identifiers. Pass them to `room.localParticipant.setScreenShareEnabled`,
and call it from a click: the browser's picker needs a user gesture, so an agent
can ask for a screen but can never take one.

Full reference: <https://docs.talqing.com/sdks/react>.
