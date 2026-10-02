/* React bindings for the Talqing call protocol.
 *
 * Thin by design. Everything here is either the call lifecycle (`useTalqingSession`
 * and the auth union it takes) or a hook over something `@talqing/client` does to
 * a `Room`. Nothing here wraps LiveKit to rename it: the layer only ever ADDS, so
 * anything LiveKit can do you can do — take `useTalqingConnection().room` and use
 * `livekit-client` directly. We never remove a capability to make our own surface
 * look tidier, which is also why `livekit-client` and `@livekit/components-react`
 * are peer dependencies: the `Room` we hand you is the same class your own
 * imports and `instanceof` resolve to.
 */

export {
  TalqingSessionProvider,
  useTalqingConnection,
  useTalqingSession,
} from "./session.js";
export type {
  TalqingCallOptions,
  TalqingSession,
  TalqingSessionOptions,
} from "./session.js";

export { useTalqingUserdata } from "./userdata.js";
export { useTalqingImages } from "./images.js";
export { useTalqingFrontendRpcs, useTalqingRpcHandlers } from "./rpc.js";
export { useTalqingAvatarTrack, useTalqingScreenShare } from "./media.js";

/* All of `@talqing/client`, so one install and one import cover a web call.
 * Everything there works against any `Room`, with or without React — the token
 * source, the userdata and image and frontend-RPC calls, and the measured
 * capture constants. Re-exported wholesale rather than as a curated list
 * because the whole package is meant to be reachable from here, and a list
 * would quietly fall behind it. */
export * from "@talqing/client";

/* LiveKit's own, re-exported UNDER THEIR OWN NAMES so a tenant installs one
 * package and imports one name.
 *
 * Re-exporting is not wrapping: there is no surface of ours here to drift, and
 * nothing is subtracted — `useAgent` is the ten-state discriminated union
 * LiveKit ships, with every derived boolean, every action and
 * `failureReasons` intact. Keeping LiveKit's names is the point: a
 * `useTalqing`-prefixed alias would claim these as ours and hide where to read
 * their documentation. Import them from `@livekit/components-react` instead if
 * you prefer — it is the same module. */
export {
  useAgent,
  useSessionMessages,
  VideoTrack,
} from "@livekit/components-react";
export type {
  AgentState,
  ReceivedMessage,
  TrackReference,
  UseAgentReturn,
  UseSessionReturn,
} from "@livekit/components-react";
