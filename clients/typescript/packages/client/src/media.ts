import type { ScreenShareCaptureOptions, TrackPublishOptions } from "livekit-client";

/* Echo cancellation is what stops the agent hearing its own speech through the
 * caller's microphone and interrupting itself. It is a default rather than
 * something each app remembers, because a call without it is broken in a way
 * that sounds like a bad model. Pass `roomOptions.audioCaptureDefaults` to
 * override. */
export const TALQING_AUDIO_CAPTURE_DEFAULTS = {
  echoCancellation: true,
  noiseSuppression: true,
  autoGainControl: true,
} as const;

/* Screen share, with the four settings that decide whether the agent can read
 * what is on it.
 *
 * Only the first is obvious, and every one of them is upstream of anything we
 * could do on our side:
 *
 *  - `contentHint: "text"` tells the encoder to preserve spatial detail. The
 *    default for a screen share on an SVC codec is `motion`, which trades
 *    exactly that away — the wrong trade for reading code, and no JPEG quality
 *    setting downstream recovers it.
 *  - `videoCodec: "h264"` is what keeps that hint. `livekit-client` forces
 *    `contentHint = "motion"` on screen shares published as vp9 or av1,
 *    together with `scalabilityMode: "L1T3"`. **Publishing on an SVC codec will
 *    cost you legibility**, and it is the first thing to check if the agent
 *    starts misreading identifiers.
 *  - `screenShareEncoding.maxFramerate: 1` is the frame-rate cap that actually
 *    binds. `resolution.frameRate` reaches `getDisplayMedia` as a bare number,
 *    which WebRTC reads as `ideal` — a wish, not a limit. The default publish
 *    ceiling is 15 fps, and every delivered frame is copied into the agent's
 *    process before it can be dropped, so this is the single biggest lever on
 *    what a call costs the worker. One frame a second is ample: the agent is
 *    handed the newest frame at the end of each turn, and turns are seconds
 *    apart.
 *  - `resolution` is set explicitly rather than inherited. The default is 1080p
 *    by `ideal`, which is a strong hint and not a guarantee.
 *
 * The picker cannot be opened for the person — `getDisplayMedia` requires a
 * transient user activation — so sharing must start from a click. The agent
 * asking out loud is what prompts it; there is no version of this where the
 * agent turns sharing on.
 */
export const TALQING_SCREEN_SHARE_CAPTURE: ScreenShareCaptureOptions = {
  resolution: { width: 1920, height: 1080, frameRate: 1 },
  contentHint: "text",
};

export const TALQING_SCREEN_SHARE_PUBLISH: TrackPublishOptions = {
  videoCodec: "h264",
  screenShareEncoding: { maxBitrate: 1_000_000, maxFramerate: 1 },
};
