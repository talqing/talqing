import { ParticipantKind, type RemoteParticipant, type Room } from "livekit-client";
import type { JsonObject } from "./json.js";

/** The agent on this call, or undefined before it has joined.
 *
 *  On a video call the avatar worker is an agent participant too, but it joins
 *  on the agent's behalf and never answers RPCs, so the first one found is the
 *  one to talk to. */
export function talqingAgentParticipant(room: Room): RemoteParticipant | undefined {
  return [...room.remoteParticipants.values()].find(
    (participant: RemoteParticipant) => participant.kind === ParticipantKind.AGENT,
  );
}

/** Merge a patch into the live session's userdata, over RPC to the agent.
 *
 *  Resolves rather than throws when the agent is not there: a write that did
 *  not land is a state a page can render. */
export async function setTalqingUserdata(
  room: Room,
  patch: JsonObject,
  responseTimeout = 3,
): Promise<{ ok: boolean; error?: string }> {
  const agent = talqingAgentParticipant(room);
  if (!agent) return { ok: false, error: "agent participant is not connected" };
  const response = await room.localParticipant.performRpc({
    destinationIdentity: agent.identity,
    method: "talqing.client.userdata_set",
    payload: JSON.stringify(patch),
    responseTimeout,
  });
  return JSON.parse(response || "{}") as { ok: boolean; error?: string };
}

/** Read the live session's userdata, or only the keys named.
 *
 *  Throws when the agent is not there, unlike `setTalqingUserdata`: a read that
 *  returned nothing is not a state anything can render. */
export async function getTalqingUserdata(
  room: Room,
  keys?: string[],
  responseTimeout = 3,
): Promise<JsonObject> {
  const agent = talqingAgentParticipant(room);
  if (!agent) throw new Error("agent participant is not connected");
  const response = await room.localParticipant.performRpc({
    destinationIdentity: agent.identity,
    method: "talqing.client.userdata_get",
    payload: JSON.stringify(keys ? { keys } : {}),
    responseTimeout,
  });
  const parsed = JSON.parse(response || "{}") as JsonObject & { ok?: boolean; error?: string };
  if (parsed.ok === false) {
    throw new Error(String(parsed.error || "userdata RPC failed"));
  }
  return parsed;
}
