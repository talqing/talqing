import type { BadgeVariant } from "@/app/components/ui";
import type {
  AgentResponse,
  NumberReadiness,
  PhoneNumberResponse,
  TelephonyAccountResponse,
  TelephonyAccountStatus,
  TelephonyProvider,
  TelephonyProviderSpec,
} from "@talqing/sdk";

/* Presentation for the states the API computes. The mapping is the whole of
   the page's status logic: readiness arrives decided, so nothing here re-reads
   `status`, `can_inbound` or LiveKit ids to second-guess it.

   `hint` is what to do about it, in the interface's voice. It is the fallback
   for a row with no `status_message` — a badge that says "Setup needed" and
   nothing else leaves the reader to guess whose setup, and where. */
export const READINESS: Record<
  NumberReadiness,
  { label: string; variant: BadgeVariant; hint: string }
> = {
  live: { label: "Live", variant: "live", hint: "Answering incoming calls." },
  needs_agent: {
    label: "No agent",
    variant: "default",
    hint: "Pick an agent and this number starts answering.",
  },
  needs_carrier_setup: {
    label: "Setup needed",
    variant: "warn",
    hint: "Incoming calls are blocked until the carrier console work is finished.",
  },
  setting_up: { label: "Setting up", variant: "info", hint: "Wiring this number up — this takes a moment." },
  error: { label: "Error", variant: "danger", hint: "Run setup again to retry." },
  disabled: { label: "Disabled", variant: "default", hint: "Out of service. Enable it to take calls again." },
};

/** States a person has to act on. `setting_up` resolves itself; `disabled` was
    someone's decision. Neither belongs in a to-do count. */
const ATTENTION: ReadonlySet<NumberReadiness> = new Set<NumberReadiness>([
  "needs_agent",
  "needs_carrier_setup",
  "error",
]);

export function needsAttention(number: PhoneNumberResponse): boolean {
  return ATTENTION.has(number.readiness);
}

/** Numbers in dialling order, with the out-of-service ones after the rest —
    an E.164 sort groups a workspace by country and area code for free. */
export function byDiallingOrder(a: PhoneNumberResponse, b: PhoneNumberResponse): number {
  const off = (n: PhoneNumberResponse) => (n.readiness === "disabled" ? 1 : 0);
  return off(a) - off(b) || a.e164.localeCompare(b.e164);
}

export const ACCOUNT_STATUS: Record<
  TelephonyAccountStatus,
  { label: string; variant: BadgeVariant }
> = {
  ready: { label: "Ready", variant: "live" },
  connected: { label: "Setup incomplete", variant: "warn" },
  provisioning: { label: "Setting up", variant: "info" },
  pending: { label: "Pending", variant: "info" },
  error: { label: "Error", variant: "danger" },
  disabled: { label: "Disabled", variant: "default" },
};

export function providerSpec(
  provider: TelephonyProvider,
  catalog: TelephonyProviderSpec[],
): TelephonyProviderSpec | undefined {
  return catalog.find((spec) => spec.provider === provider);
}

export function providerLabel(
  provider: TelephonyProvider,
  catalog: TelephonyProviderSpec[],
): string {
  return providerSpec(provider, catalog)?.label ?? provider;
}

/** Only a published voice agent can be dispatched onto a SIP call. */
export function isPublishedVoiceAgent(agent: AgentResponse): boolean {
  return (
    agent.config?.channel === "voice" &&
    agent.published_version != null &&
    agent.published_version > 0
  );
}

export function agentLabel(agent: AgentResponse): string {
  return agent.config?.name || agent.id.slice(0, 8);
}

/**
 * The carrier's own identifier for an account, with the label the carrier uses
 * for it ("Auth ID", "Account SID").
 *
 * Read from the catalog rather than branched on provider: the first
 * `account_info_keys` entry is the identity key, and the matching setup field
 * already carries the human label. A new carrier needs no change here.
 */
export function accountIdentity(
  account: TelephonyAccountResponse,
  spec: TelephonyProviderSpec | undefined,
): { label: string; value: string } | null {
  const key = spec?.account_info_keys[0];
  if (!key) return null;
  const value = account.account_info[key];
  if (typeof value !== "string" || !value) return null;
  return { label: spec.setup_fields.find((f) => f.key === key)?.label ?? key, value };
}

/* ── formatting ─────────────────────────────────────────────────────────── */

export function money(amount: number, digits = 2): string {
  return `$${amount.toFixed(digits)}`;
}

export function plural(count: number, one: string, many = `${one}s`): string {
  return `${count} ${count === 1 ? one : many}`;
}

/** Talk time, at the coarsest unit that still says something. */
export function minutes(seconds: number): string {
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const mins = Math.round(seconds / 60);
  if (mins < 90) return `${mins}m`;
  const hours = mins / 60;
  return `${hours < 10 ? hours.toFixed(1) : Math.round(hours)}h`;
}
