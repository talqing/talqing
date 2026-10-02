"use client";

import React, { useCallback, useMemo, useState } from "react";
import { Room, type RoomOptions } from "livekit-client";
import {
  RoomAudioRenderer,
  SessionProvider,
  useSession,
  useSessionContext,
  type UseSessionReturn,
} from "@livekit/components-react";
import {
  createTalqingTokenSource,
  TALQING_AUDIO_CAPTURE_DEFAULTS,
  type TalqingCallCredentials,
} from "@talqing/client";
import type { TalqingClient, TokenRequest } from "@talqing/sdk";

/** Everything about one web call except which agent runs it. Wire-shaped, like
 *  every other request type in this SDK. */
export type TalqingCallOptions = Omit<TokenRequest, "agent_id">;

/* Three ways to authorize one call, and exactly one of them per call.
 *
 * The branches are mutually exclusive by construction rather than by a runtime
 * precedence rule: every branch declares every other branch's fields as
 * `?: never`, so passing two is a type error at the call site.
 *
 * The first two are what a tenant ships. `POST /v1/calls/token` needs the EDITOR
 * role, and a Talqing credential that can mint a call token can also create and
 * delete agents, read every transcript and spend the workspace's credits — so it
 * belongs on your server and nowhere near a browser. Your server mints; the
 * browser receives two strings and joins.
 *
 * That is not only about the credential. `contact_key` decides WHOSE history the
 * call resumes, and `userdata` is merged onto that person's record. A browser
 * that chooses its own would be choosing whose past conversation to continue.
 * On these two branches it never can: those fields never leave your backend.
 */
type TalqingCallAuth =
  /** Your server called `POST /v1/calls/token` and forwarded these two fields. */
  | {
      token: TalqingCallCredentials;
      fetchToken?: never;
      client?: never;
      agentId?: never;
      callOptions?: never;
    }
  /** Your endpoint mints one. Called once, when the call starts. */
  | {
      fetchToken: () => Promise<TalqingCallCredentials>;
      token?: never;
      client?: never;
      agentId?: never;
      callOptions?: never;
    }
  /** Mint from the browser with a `TalqingClient` that already holds a
   *  credential. **Trusted first-party surfaces only** — an app that is
   *  same-origin with the API and signed in as a real editor, which is what our
   *  own dashboard is. If you are embedding a call widget for your customers,
   *  this is not your branch. */
  | {
      client: TalqingClient;
      agentId: string | null;
      /** `contact_key`, `userdata`, `vars` and the per-call agent selection.
       *  Only on this branch: on the other two the server passes them when it
       *  mints, which is what keeps a browser from choosing whose history to
       *  resume. */
      callOptions?: TalqingCallOptions;
      token?: never;
      fetchToken?: never;
    };

export type TalqingSessionOptions = TalqingCallAuth & { roomOptions?: RoomOptions };

/** LiveKit's session, plus what resolving this call's plan complained about. */
export type TalqingSession = UseSessionReturn & {
  /** An unreachable team member, a handoff into an agent that records when this
   *  one does not — resolved against state at CALL time, so it is not the list
   *  publishing returned. Empty until the token is minted.
   *
   *  Populated on the `client` branch, which is the one where this browser is
   *  what minted. On the two server-minted branches your own server received
   *  them in the token response and this stays empty. */
  warnings: string[];
};

/** One session, one call — `start()` it when this mounts and `end()` it on the
 *  way out, the lifecycle LiveKit's own session docs describe. A second call is
 *  a second mount, so give the component holding this a `key` that changes per
 *  call. Starting a finished session again would rejoin the room the last call
 *  used, which the API refuses. */
export function useTalqingSession(options: TalqingSessionOptions): TalqingSession {
  const { token, fetchToken, client, agentId, callOptions, roomOptions } = options;
  const [warnings, setWarnings] = useState<string[]>([]);

  // Reads every branch's fields so the dependency list is the same length on
  // each; all but one are `undefined` on any given call.
  const mint = useCallback(async (): Promise<TalqingCallCredentials> => {
    if (token) return token;
    if (fetchToken) return await fetchToken();
    if (!client) throw new Error("useTalqingSession needs one of token, fetchToken or client");
    const minted = await client.calls.token({
      tokenRequest: { ...callOptions, agent_id: agentId ?? null },
    });
    setWarnings(minted.warnings);
    return { server_url: minted.server_url, participant_token: minted.participant_token };
  }, [token, fetchToken, client, agentId, callOptions]);

  const tokenSource = useMemo(() => createTalqingTokenSource(mint), [mint]);
  const room = useMemo(
    () =>
      new Room({ audioCaptureDefaults: TALQING_AUDIO_CAPTURE_DEFAULTS, ...roomOptions }),
    [roomOptions],
  );
  const session = useSession(tokenSource, { room });
  return useMemo(() => ({ ...session, warnings }), [session, warnings]);
}

export function TalqingSessionProvider({
  session,
  children,
  audio = true,
}: {
  session: TalqingSession;
  children: React.ReactNode;
  audio?: boolean;
}) {
  return (
    <SessionProvider session={session}>
      {children}
      {audio ? <RoomAudioRenderer /> : null}
    </SessionProvider>
  );
}

/** The call itself, read from context — and `room`, which is the supported way
 *  down to raw `livekit-client` for anything this layer does not cover. */
export function useTalqingConnection() {
  const ctx = useSessionContext();
  return {
    isConnected: ctx.isConnected,
    connectionState: ctx.connectionState,
    room: ctx.room,
    start: ctx.start,
    end: ctx.end,
  };
}
