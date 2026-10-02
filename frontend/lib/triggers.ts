import type { CreateIntegrationTriggerRequest, IntegrationTriggerResponse } from "@talqing/sdk";
import { api } from "./api";

export type TriggerType = CreateIntegrationTriggerRequest["trigger_type"];

/** Put an agent on a channel trigger, or take it off: creates the trigger on
 *  first use, and patches it after. Turning off keeps the agent, so turning
 *  back on needs no pick. */
export function saveTrigger(
  integrationId: string,
  existing: IntegrationTriggerResponse | undefined,
  { triggerType, agentId, enabled }: { triggerType: TriggerType; agentId: string | null; enabled: boolean },
): Promise<IntegrationTriggerResponse> {
  const body = {
    trigger_type: triggerType,
    agent_id: agentId,
    enabled,
    reply_mode: "public_reply" as const,
  };
  return existing
    ? api.patchIntegrationTrigger(integrationId, existing.id, body)
    : api.createIntegrationTrigger(integrationId, body);
}
