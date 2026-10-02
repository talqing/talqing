/* A compile-time test of the auth union. Nothing here runs; the assertions are
 * the type checker's, and `npm run typecheck` is what makes them.
 *
 * The union's whole job is to make "two ways to authorize one call" a compile
 * error rather than a runtime precedence rule, so the negative cases below are
 * the point of the file.
 */
import { TalqingClient } from "@talqing/sdk";
import {
  TalqingSessionProvider,
  useTalqingConnection,
  useTalqingImages,
  useTalqingSession,
  useAgent,
  useSessionMessages,
  type TalqingCallCredentials,
  type TalqingSessionOptions,
} from "../src/index.js";

const client = new TalqingClient({ baseUrl: "https://api.example.com", token: "tq_x" });
const credentials: TalqingCallCredentials = { server_url: "wss://x", participant_token: "jwt" };

/** Branch 1 — your server minted and forwarded the two fields. */
export function preMinted() {
  return useTalqingSession({ token: credentials });
}

/** Branch 2 — your endpoint mints, once, when the call starts. */
export function serverMinted() {
  return useTalqingSession({
    fetchToken: async () => {
      const response = await fetch("/api/talqing-token", { method: "POST" });
      return (await response.json()) as TalqingCallCredentials;
    },
  });
}

/** Branch 3 — trusted first-party only, and the only one that carries
 *  `callOptions`, because those decide whose history the call resumes. */
export function browserMinted(agentId: string) {
  const session = useTalqingSession({
    client,
    agentId,
    callOptions: { contact_key: "user-42", userdata: { plan: "gold" } },
  });
  void (session.warnings satisfies string[]);
  return session;
}

/** Two branches at once do not compile. Each line is an error the union exists
 *  to produce, so removing any `@ts-expect-error` should fail the build. */
export function mutuallyExclusive(agentId: string) {
  // @ts-expect-error — a pre-minted token and an endpoint to mint one
  useTalqingSession({ token: credentials, fetchToken: async () => credentials });
  // @ts-expect-error — a pre-minted token and a client to mint with
  useTalqingSession({ token: credentials, client, agentId });
  // @ts-expect-error — an endpoint and a client
  useTalqingSession({ fetchToken: async () => credentials, client, agentId });
  // @ts-expect-error — `callOptions` belongs to the client branch alone: a
  // browser must not choose `contact_key` on a server-minted call.
  useTalqingSession({ token: credentials, callOptions: { contact_key: "user-42" } });
  // @ts-expect-error — no branch at all
  useTalqingSession({});
  // @ts-expect-error — the client branch needs an agent (or null for a team)
  useTalqingSession({ client });
}

/** `roomOptions` rides on every branch. */
export const withRoomOptions: TalqingSessionOptions = {
  token: credentials,
  roomOptions: { adaptiveStream: true },
};

/** The hooks a call surface reaches for, including LiveKit's own under their
 *  own names — proof they are re-exported rather than re-wrapped. */
export function CallSurface() {
  const { room, isConnected } = useTalqingConnection();
  const images = useTalqingImages(room);
  const agent = useAgent();
  const { messages } = useSessionMessages();
  // The discriminated union our old wrapper collapsed: our version returned
  // `{ state, isFinished }` and nothing else, so `canListen`, `failureReasons`
  // and the per-state narrowing were all unreachable. They are reachable here
  // only because nothing renames `useAgent`.
  void (agent.isFinished satisfies boolean);
  void agent.canListen;
  if (agent.state === "failed") void (agent.failureReasons satisfies string[]);
  return { images: images.sent, isConnected, messages };
}

export { TalqingSessionProvider };
