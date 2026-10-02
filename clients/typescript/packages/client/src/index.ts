/* The Talqing call protocol, over a LiveKit `Room`. Framework-free.
 *
 * Everything here is something only Talqing can write: the mint-once token
 * source, the two userdata RPC methods, the `talqing.images` byte-stream topic
 * and its ack, the `talqing.frontend_rpc` envelope, and the measured capture
 * settings. Nothing here renames LiveKit — for a room, a track or a
 * participant, use `livekit-client` directly.
 *
 * React bindings for all of it are `@talqing/react`.
 *
 * `TalqingChat` is the one thing here with no room behind it: a text chat, over
 * HTTP, on a chat token.
 */

export { createTalqingTokenSource } from "./token.js";
export type { TalqingCallCredentials } from "./token.js";

export {
  getTalqingUserdata,
  setTalqingUserdata,
  talqingAgentParticipant,
} from "./userdata.js";

export {
  onTalqingImageResult,
  sendTalqingImage,
  talqingDownscaleImage,
  talqingImageDataUrl,
  talqingImageRejection,
  TALQING_IMAGE_ACK_TIMEOUT_MS,
  TALQING_IMAGE_MAX_BYTES,
  TALQING_IMAGE_MAX_EDGE_PX,
  TALQING_IMAGE_TOPIC,
} from "./images.js";
export type { TalqingImageStatus, TalqingSentImage } from "./images.js";

export { registerTalqingRpcHandlers, TalqingRpcError } from "./rpc.js";
export type {
  TalqingFrontendRpcEnvelope,
  TalqingRpcHandler,
  TalqingRpcHandlers,
  TalqingRpcInvocation,
} from "./rpc.js";

export {
  TALQING_AUDIO_CAPTURE_DEFAULTS,
  TALQING_SCREEN_SHARE_CAPTURE,
  TALQING_SCREEN_SHARE_PUBLISH,
} from "./media.js";

export { TalqingChat, TalqingChatError } from "./chat.js";
export type {
  TalqingChatAttachment,
  TalqingChatCredentials,
  TalqingChatEvent,
  TalqingChatImage,
  TalqingChatItem,
  TalqingChatTurn,
} from "./chat.js";

export type { JsonObject, JsonPrimitive, JsonValue } from "./json.js";
