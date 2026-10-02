"use client";

import { useMemo } from "react";
import { ParticipantKind, Track, type RemoteParticipant } from "livekit-client";
import {
  useParticipantTracks,
  useRemoteParticipants,
  useTrackToggle,
  useVoiceAssistant,
  type TrackReference,
} from "@livekit/components-react";
import {
  TALQING_SCREEN_SHARE_CAPTURE,
  TALQING_SCREEN_SHARE_PUBLISH,
} from "@talqing/client";

/** Share the caller's screen with the agent, on the settings that keep it
 *  legible — see `TALQING_SCREEN_SHARE_CAPTURE` for why each one is what it is. */
export function useTalqingScreenShare() {
  const { toggle, enabled, pending } = useTrackToggle({
    source: Track.Source.ScreenShare,
    captureOptions: TALQING_SCREEN_SHARE_CAPTURE,
    publishOptions: TALQING_SCREEN_SHARE_PUBLISH,
  });
  return useMemo(
    () => ({
      /** Whether a screen is being shared right now. */
      enabled,
      /** True while the browser picker is open or the track is publishing. */
      pending,
      /** Opens the browser's share picker. Must be called from a user gesture. */
      start: () => toggle(true),
      /** Stops sharing. The agent is told it can no longer see on the next turn. */
      stop: () => toggle(false),
    }),
    [enabled, pending, toggle],
  );
}

/** The video track to render for a video agent.
 *
 *  Prefers the avatar worker's camera — the worker publishes on the agent's
 *  behalf, which is how it is identified — and falls back through the worker's
 *  screen share and then the agent's own tracks. */
export function useTalqingAvatarTrack(): TrackReference | undefined {
  const { agent } = useVoiceAssistant();
  const remoteParticipants = useRemoteParticipants();
  const worker = remoteParticipants.find(
    (participant: RemoteParticipant) =>
      participant.kind === ParticipantKind.AGENT &&
      participant.attributes["lk.publish_on_behalf"] === agent?.identity,
  );

  const agentTracks = useParticipantTracks(
    [Track.Source.Camera, Track.Source.ScreenShare],
    agent?.identity,
  );
  const workerTracks = useParticipantTracks(
    [Track.Source.Camera, Track.Source.ScreenShare],
    worker?.identity,
  );

  return (
    workerTracks.find(
      (track: TrackReference) => track.source === Track.Source.Camera,
    ) ??
    workerTracks.find(
      (track: TrackReference) => track.source === Track.Source.ScreenShare,
    ) ??
    agentTracks.find(
      (track: TrackReference) => track.source === Track.Source.Camera,
    ) ??
    agentTracks.find(
      (track: TrackReference) => track.source === Track.Source.ScreenShare,
    )
  );
}
