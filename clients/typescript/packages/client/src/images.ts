import type { Room, RpcInvocationData } from "livekit-client";
import { talqingAgentParticipant } from "./userdata.js";

/* ── images ───────────────────────────────────────────────────────────────
 *
 * A caller attaches a photo on a web voice or video call and the agent sees
 * it. The bytes go over a LiveKit byte stream on the `talqing.images` topic,
 * addressed to the agent; the agent answers on an RPC back, and the id
 * `sendFile` returns is the id that ack carries, so there is nothing to
 * correlate by hand.
 *
 * The agent deliberately does NOT speak when a photo lands — the caller is
 * mid-conversation and about to say what it is for. The image is in the
 * model's view for their next turn.
 */

export const TALQING_IMAGE_TOPIC = "talqing.images";
/** Decoded size the server refuses above. The downscale below keeps a phone
 *  photo far under it; this is the belt for a file that is not a photo. */
export const TALQING_IMAGE_MAX_BYTES = 10 * 1024 * 1024;
/** Longest side the server stores. Downscaling to it before sending is a
 *  courtesy to the data channel, which is ordered and reliable and will
 *  happily spend a minute pushing 10 MB past a call's audio. */
export const TALQING_IMAGE_MAX_EDGE_PX = 1568;
const TALQING_IMAGE_TYPES = ["image/jpeg", "image/png", "image/webp"] as const;
/** How long to wait for the agent's ack before saying we do not know. Without
 *  it a thumbnail sits on "sending" for the rest of the call when the worker
 *  dies mid-upload. */
export const TALQING_IMAGE_ACK_TIMEOUT_MS = 30_000;

/** Refuse what the browser can see is wrong, instantly and with no round trip.
 *  Returns the sentence to show, or null when the file is fine. The server
 *  re-validates everything regardless — this exists so the common mistakes cost
 *  nothing to catch. */
export function talqingImageRejection(file: File): string | null {
  if (!(TALQING_IMAGE_TYPES as readonly string[]).includes(file.type)) {
    return "that file is not a JPEG, PNG or WebP";
  }
  if (file.size > TALQING_IMAGE_MAX_BYTES) return "images are 10 MB or smaller";
  return null;
}

/** Downscale to the longest edge the server keeps, re-encoding as JPEG or PNG.
 *
 *  Also what normalizes whatever the OS handed the file input — this is how a
 *  Safari HEIC becomes a JPEG. Throws if the image cannot be decoded, which is
 *  the third dropzone check. */
export async function talqingDownscaleImage(file: File): Promise<Blob> {
  const bitmap = await createImageBitmap(file);
  try {
    const longest = Math.max(bitmap.width, bitmap.height);
    const scale = longest > TALQING_IMAGE_MAX_EDGE_PX ? TALQING_IMAGE_MAX_EDGE_PX / longest : 1;
    const canvas = document.createElement("canvas");
    canvas.width = Math.max(1, Math.round(bitmap.width * scale));
    canvas.height = Math.max(1, Math.round(bitmap.height * scale));
    const context = canvas.getContext("2d");
    if (!context) throw new Error("this browser cannot resize images");
    context.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
    // PNG keeps transparency; everything else becomes a JPEG, including the
    // HEIC the OS decoded for us.
    const type = file.type === "image/png" ? "image/png" : "image/jpeg";
    const blob = await new Promise<Blob | null>((resolve) =>
      canvas.toBlob(resolve, type, 0.85),
    );
    if (!blob) throw new Error("that image could not be read");
    return blob;
  } finally {
    bitmap.close();
  }
}

/** Validate and downscale a file into the `data:` URL a text message carries.
 *
 *  `createTextMessage` takes `images: [{data_url, filename}]`; this produces the
 *  `data_url`. It doubles as the thumbnail's `src`, so there is no object URL to
 *  create and revoke. Throws the sentence to show if the file is refused. */
export async function talqingImageDataUrl(file: File): Promise<string> {
  const rejection = talqingImageRejection(file);
  if (rejection) throw new Error(rejection);
  const blob = await talqingDownscaleImage(file);
  return await new Promise<string>((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error("that image could not be read"));
    reader.onload = () => resolve(String(reader.result));
    reader.readAsDataURL(blob);
  });
}

export type TalqingImageStatus = "sending" | "sent" | "failed" | "unknown";

/** One image the browser sent on a call, and what became of it. */
export interface TalqingSentImage {
  /** The stream id `sendFile` returned, which the ack comes back under. */
  streamId: string;
  name: string;
  /** An object URL for the downscaled image, for rendering the thumbnail.
   *  Revoke it when you are done with it — `useTalqingImages` does. */
  previewUrl: string;
  /** When it was handed to the room, so a transcript can put it in the sequence
   *  it was sent in rather than at the end. */
  sentAt: number;
  status: TalqingImageStatus;
  /** Set when `status` is "failed" or "unknown" — a sentence, shown as-is. */
  error?: string;
}

/** Validate, downscale and send one image to the agent on this call.
 *
 *  Resolves once the bytes are on their way, with the entry to render. The
 *  outcome arrives later, on the ack — see `onTalqingImageResult`. Throws the
 *  sentence to show if the file is refused or the agent is not there yet. */
export async function sendTalqingImage(room: Room, file: File): Promise<TalqingSentImage> {
  const agent = talqingAgentParticipant(room);
  if (!agent) throw new Error("the agent is not on the call yet");
  const rejection = talqingImageRejection(file);
  if (rejection) throw new Error(rejection);

  const blob = await talqingDownscaleImage(file);
  // Addressed to the agent, never broadcast: on video the avatar worker is
  // a participant too, and a broadcast sends the bytes twice.
  const { id } = await room.localParticipant.sendFile(
    new File([blob], file.name, { type: blob.type }),
    {
      topic: TALQING_IMAGE_TOPIC,
      mimeType: blob.type,
      destinationIdentities: [agent.identity],
    },
  );
  return {
    streamId: id,
    name: file.name,
    previewUrl: URL.createObjectURL(blob),
    sentAt: Date.now(),
    status: "sending",
  };
}

/** Listen for the agent's verdict on each image sent. Returns the unregister.
 *
 *  There is one ack per image even on the happy path, because the agent stays
 *  silent when a photo lands — so silence cannot double as "it arrived". */
export function onTalqingImageResult(
  room: Room,
  onResult: (result: { streamId: string; ok: boolean; error?: string }) => void,
): () => void {
  room.registerRpcMethod("talqing.image_result", async (data: RpcInvocationData) => {
    const { stream_id, ok, error } = JSON.parse(data.payload || "{}") as {
      stream_id: string;
      ok: boolean;
      error?: string | null;
    };
    onResult({
      streamId: stream_id,
      ok,
      error: ok ? undefined : error || "that image was not accepted",
    });
    return "{}";
  });
  return () => room.unregisterRpcMethod("talqing.image_result");
}
