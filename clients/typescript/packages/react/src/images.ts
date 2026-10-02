"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import type { Room } from "livekit-client";
import {
  onTalqingImageResult,
  sendTalqingImage,
  TALQING_IMAGE_ACK_TIMEOUT_MS,
  type TalqingSentImage,
} from "@talqing/client";

/** Send images to the agent on this call, and follow what became of each.
 *
 *  `send` resolves once the bytes are on their way; the outcome arrives on the
 *  returned list, which is what a thumbnail should render.
 *
 *  Takes the `Room` rather than reading it from the session context: a byte
 *  stream and an RPC handler are room-level things, and passing the room keeps
 *  this usable from a surface that manages its own connection —
 *  `useTalqingConnection().room` inside a provider, or the Room you built. */
export function useTalqingImages(room: Room | null | undefined) {
  const [sent, setSent] = useState<TalqingSentImage[]>([]);
  const timers = useRef(new Map<string, ReturnType<typeof setTimeout>>());

  const settle = useCallback((streamId: string, patch: Partial<TalqingSentImage>) => {
    const timer = timers.current.get(streamId);
    if (timer) {
      clearTimeout(timer);
      timers.current.delete(streamId);
    }
    setSent((images) =>
      images.map((image) => (image.streamId === streamId ? { ...image, ...patch } : image)),
    );
  }, []);

  useEffect(() => {
    if (!room) return;
    return onTalqingImageResult(room, ({ streamId, ok, error }) =>
      settle(streamId, { status: ok ? "sent" : "failed", error }),
    );
  }, [room, settle]);

  const pending = timers.current;
  useEffect(
    () => () => {
      pending.forEach(clearTimeout);
      pending.clear();
    },
    [pending],
  );

  /* Each thumbnail is an object URL over the downscaled blob, and each pins that
     blob in memory until it is revoked. This hook is mounted per call, so
     without this every call's images would be held for the life of the tab. */
  const previews = useRef<string[]>([]);
  useEffect(
    () => () => {
      previews.current.forEach(URL.revokeObjectURL);
      previews.current = [];
    },
    [],
  );

  const send = useCallback(
    async (file: File): Promise<TalqingSentImage> => {
      if (!room) throw new Error("the call is not connected");
      const image = await sendTalqingImage(room, file);
      previews.current.push(image.previewUrl);
      setSent((images) => [...images, image]);
      timers.current.set(
        image.streamId,
        setTimeout(
          () =>
            settle(image.streamId, {
              status: "unknown",
              // Four states, not two: the one people forget is the agent going
              // away mid-upload, where no ack is ever coming.
              error: "we did not hear back — the agent may not have received it",
            }),
          TALQING_IMAGE_ACK_TIMEOUT_MS,
        ),
      );
      return image;
    },
    [room, settle],
  );

  const clear = useCallback(() => {
    setSent((images) => {
      images.forEach((image) => URL.revokeObjectURL(image.previewUrl));
      previews.current = [];
      return [];
    });
  }, []);

  return { send, sent, clear };
}
