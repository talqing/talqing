import { TokenSource, type TokenSourceFixed } from "livekit-client";

/** The two fields a browser needs to join a room, and nothing else.
 *
 *  Named as `POST /v1/calls/token` names them, so a server that mints one can
 *  forward the response through to the browser without renaming anything. */
export interface TalqingCallCredentials {
  server_url: string;
  participant_token: string;
}

/** Turn a one-shot mint into the token source LiveKit's session takes.
 *
 *  `fetchToken` is called at most once per source, no matter how often LiveKit
 *  asks. That is load-bearing rather than an optimization: minting is not free,
 *  because `POST /v1/calls/token` files the session row the call is later read
 *  back from. The framework asks three times per call — to warm the connection,
 *  to connect, and once more as the call ends to drop what it assumes is a
 *  cached token — and only the middle one is a call anybody placed. Minting for
 *  the third filed a session nobody ever joined, which sat in the workspace's
 *  call list as queued for good.
 *
 *  `TokenSource.literal` is the LiveKit variant that leaves caching to us, and
 *  this is us doing it.
 *
 *  Freshness comes from a new source per call, never from re-minting inside
 *  one: the token names the room, and this call has exactly one. Mount the
 *  session per call and end it on unmount, as LiveKit's own session lifecycle
 *  does, and that holds by construction. */
export function createTalqingTokenSource(
  fetchToken: () => Promise<TalqingCallCredentials>,
): TokenSourceFixed {
  let minted: Promise<{ serverUrl: string; participantToken: string }> | null = null;

  return TokenSource.literal(() => {
    // A mint that failed is not this call's token, and the surface will offer
    // the call again — so keep the rejection out of the cache.
    minted ??= fetchToken()
      .then((credentials) => ({
        serverUrl: credentials.server_url,
        participantToken: credentials.participant_token,
      }))
      .catch((error: unknown) => {
        minted = null;
        throw error;
      });
    return minted;
  });
}
