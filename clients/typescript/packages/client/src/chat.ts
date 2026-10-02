/* ── chats ────────────────────────────────────────────────────────────────
 *
 * A text chat with an agent, from a browser. Your SERVER starts the chat
 * (`POST /v1/chats`) and mints a token for it (`POST /v1/chats/{id}/token`);
 * the browser holds only that token, which opens that one chat and nothing
 * else.
 */

/** What `POST /v1/chats/{id}/token` returns, named as the API names it — so a
 *  server that mints one can forward the response through unchanged. */
export interface TalqingChatCredentials {
  chat_id: string;
  token: string;
  /** ISO 8601. A token lasts an hour. */
  expires_at: string;
  /** The API this chat lives on. */
  api_url: string;
}

export interface TalqingChatAttachment {
  id: string;
  kind: "image";
  mime_type: string;
  bytes: number;
  width: number;
  height: number;
  filename: string | null;
  /** Signed, and short-lived: valid until `url_expires_at`. */
  url: string;
  url_expires_at: string;
}

/** One message in the chat. A browser only ever receives what the person
 *  chatting can see — never the agent's tool calls. */
export interface TalqingChatItem {
  id: string;
  /** The message this one answers; null on the person's own messages. */
  trigger_item_id: string | null;
  direction: "inbound" | "outbound" | "internal";
  type: string;
  role: "user" | "assistant" | "system" | "developer" | "tool" | null;
  /** Null on a message that is only images. */
  text: string | null;
  attachments: TalqingChatAttachment[];
  created_at: string;
}

/** What one message led to. */
export interface TalqingChatTurn {
  input: TalqingChatItem;
  /** `canceled`: a newer message arrived first, named by `superseded_by_item_id`. */
  status: "done" | "error" | "canceled" | "running";
  error: string | null;
  superseded_by_item_id: string | null;
  /** Everything the agent said in reply, oldest first. */
  items: TalqingChatItem[];
  /** `ended` when the agent ended the chat on this turn. */
  chat_status: "open" | "ended";
}

/** A frame of a reply as it is generated. `assistant.delta` chunks append. */
export type TalqingChatEvent =
  | { event: "assistant.started"; trigger_item_id: string }
  | { event: "assistant.delta"; trigger_item_id: string; text: string }
  | { event: "assistant.completed"; trigger_item_id: string; text: string | null }
  | ({ event: "item.created" } & TalqingChatItem)
  | ({ event: "turn.result" } & TalqingChatTurn);

/** An image to send with a message, as a `data:` URL. */
export interface TalqingChatImage {
  data_url: string;
  filename?: string;
}

/** An error response from the API. */
export class TalqingChatError extends Error {
  readonly name = "TalqingChatError";
  readonly status: number;
  /** Per-field problems, when the failure had more than one. */
  readonly errors: string[];

  constructor(status: number, message: string, errors: string[]) {
    super(message);
    this.status = status;
    this.errors = errors;
  }
}

/** How long before `expires_at` a new token is fetched. */
const REFRESH_BEFORE_EXPIRY_MS = 60_000;

/** One chat, driven from a browser.
 *
 *  `fetchToken` asks YOUR server for the chat's token, and is called again
 *  shortly before each one expires. */
export class TalqingChat {
  private readonly fetchToken: () => Promise<TalqingChatCredentials>;
  private minted: Promise<TalqingChatCredentials> | null = null;

  constructor(options: { fetchToken: () => Promise<TalqingChatCredentials> }) {
    this.fetchToken = options.fetchToken;
  }

  /** Send a message. `events` yields the reply as it is generated; `result`
   *  resolves with the whole turn. Either can be used without the other. */
  send(
    text: string,
    options: { images?: TalqingChatImage[] } = {},
  ): { events: AsyncIterable<TalqingChatEvent>; result: Promise<TalqingChatTurn> } {
    const queue: TalqingChatEvent[] = [];
    let finished = false;
    let failure: unknown;
    let wake: (() => void) | null = null;
    const notify = () => {
      wake?.();
      wake = null;
    };

    const result = (async () => {
      const response = await this.request("/messages", {
        method: "POST",
        body: JSON.stringify({
          message: text,
          images: options.images ?? [],
          client_message_id: crypto.randomUUID(),
          stream: true,
        }),
      });
      for await (const event of readEvents(response)) {
        queue.push(event);
        notify();
        if (event.event === "turn.result") {
          const { event: _name, ...turn } = event;
          return turn;
        }
      }
      throw new TalqingChatError(response.status, "the reply ended before it finished", []);
    })();
    result
      .catch((error: unknown) => {
        failure = error;
      })
      .finally(() => {
        finished = true;
        notify();
      });

    const events: AsyncIterable<TalqingChatEvent> = {
      [Symbol.asyncIterator]: async function* () {
        for (;;) {
          while (queue.length > 0) yield queue.shift() as TalqingChatEvent;
          if (finished) {
            if (failure !== undefined) throw failure;
            return;
          }
          await new Promise<void>((resolve) => {
            wake = resolve;
          });
        }
      },
    };
    return { events, result };
  }

  /** The chat so far, oldest first. */
  async items(): Promise<TalqingChatItem[]> {
    const items: TalqingChatItem[] = [];
    for (;;) {
      const response = await this.request(`/items?limit=500&offset=${items.length}`, {
        method: "GET",
      });
      const page = (await response.json()) as { items: TalqingChatItem[]; has_more: boolean };
      items.push(...page.items);
      if (!page.has_more) return items;
    }
  }

  /** End the chat. Further messages are refused. */
  async end(): Promise<void> {
    await this.request("/end", { method: "POST" });
  }

  private credentials(): Promise<TalqingChatCredentials> {
    const current = this.minted;
    this.minted = (async () => {
      const held = await current?.catch(() => null);
      if (held && Date.parse(held.expires_at) - Date.now() > REFRESH_BEFORE_EXPIRY_MS) {
        return held;
      }
      return this.fetchToken();
    })();
    return this.minted;
  }

  private async request(path: string, init: RequestInit): Promise<Response> {
    const { api_url, chat_id, token } = await this.credentials();
    const response = await fetch(`${api_url}/v1/chats/${chat_id}${path}`, {
      ...init,
      headers: { authorization: `Bearer ${token}`, "content-type": "application/json" },
    });
    if (!response.ok) {
      const body = (await response.json().catch(() => null)) as {
        detail?: { message?: string; errors?: string[] };
      } | null;
      throw new TalqingChatError(
        response.status,
        body?.detail?.message ?? response.statusText,
        body?.detail?.errors ?? [],
      );
    }
    return response;
  }
}

/** Parse a `text/event-stream` body into its frames. Keep-alive comments are dropped. */
async function* readEvents(response: Response): AsyncGenerator<TalqingChatEvent> {
  if (!response.body) return;
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) return;
    buffer += decoder.decode(value, { stream: true });
    let boundary = buffer.indexOf("\n\n");
    while (boundary !== -1) {
      const frame = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);
      const data = frame
        .split("\n")
        .filter((line) => line.startsWith("data:"))
        .map((line) => line.slice(5).trimStart())
        .join("\n");
      if (data) yield JSON.parse(data) as TalqingChatEvent;
      boundary = buffer.indexOf("\n\n");
    }
  }
}
