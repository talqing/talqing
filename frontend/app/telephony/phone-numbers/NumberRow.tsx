"use client";

import { useRouter } from "next/navigation";
import { Badge, Menu, Select } from "@/app/components/ui";
import { cn } from "@/lib/cn";
import type { AgentResponse, PhoneNumberResponse } from "@talqing/sdk";
import { READINESS, agentLabel, money } from "./shared";
import type { NumberTraffic } from "./traffic";

/* Rows and their header share one template, so the columns line up without a
   <table> — which matters here because a cell holds a live <Select>, and the
   whole row has to reflow on a narrow screen rather than scroll sideways.
   Traffic is the first column to go: it is context, not control. */
export const ROW_GRID =
  "sm:grid-cols-[minmax(0,1.2fr)_minmax(150px,0.8fr)_minmax(140px,0.62fr)_68px] lg:grid-cols-[minmax(0,1.2fr)_minmax(160px,0.78fr)_minmax(104px,0.42fr)_minmax(148px,0.6fr)_68px]";

const PhoneOutIcon = (
  <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
    <path d="M5 5.5C5 4.4 5.9 3.5 7 3.5h1.2l1.3 3.4-1.6 1.2a10.4 10.4 0 0 0 5 5l1.2-1.6 3.4 1.3V14c0 1.1-.9 2-2 2A12.9 12.9 0 0 1 5 5.5Z" />
    <path d="M15.5 8.5 21 3m0 0h-4m4 0v4" />
  </svg>
);

const BAR_MAX_PX = 20;

/**
 * Daily call volume, on a scale shared with every other row on the page.
 *
 * The shared scale is the whole point: given its own maximum, a number that
 * took two calls draws exactly the same silhouette as one that took sixty, and
 * the column stops meaning anything. A day with no calls keeps a hairline so
 * the baseline — and therefore the gap — stays readable.
 */
function Sparkline({ days, peak, label }: { days: number[]; peak: number; label: string }) {
  return (
    <span
      className="mt-1 flex items-end gap-[3px]"
      role="img"
      aria-label={label}
      /* Runtime-computed geometry stays an inline style: a Tailwind class
         cannot carry a value derived from the data. */
      style={{ height: BAR_MAX_PX }}
    >
      {days.map((count, i) => (
        <span
          key={i}
          className={cn("w-[5px] flex-none rounded-[1.5px]", count > 0 ? "bg-ink/30" : "bg-line-2")}
          style={{ height: count > 0 ? Math.max(3, (count / peak) * BAR_MAX_PX) : 2 }}
        />
      ))}
    </span>
  );
}

export function NumberRow({
  number,
  agents,
  assignedAgent,
  traffic,
  peakDay,
  busy,
  carrierDisabled,
  accountReady,
  onAssign,
  onTestCall,
  onProvision,
  onDisable,
  onEnable,
}: {
  number: PhoneNumberResponse;
  /** Published voice agents — the only thing that can answer a SIP call. */
  agents: AgentResponse[];
  assignedAgent: AgentResponse | undefined;
  traffic: NumberTraffic | undefined;
  peakDay: number;
  busy: boolean;
  /** The carrier account is disabled, so this number is off the air whatever
      its own readiness says — and every write below it would be refused. */
  carrierDisabled: boolean;
  /** The account is provisioned. Assign, provision and dial all require it. */
  accountReady: boolean;
  onAssign: (agentId: string) => void;
  onTestCall: () => void;
  onProvision: () => void;
  onDisable: () => void;
  onEnable: () => void;
}) {
  const router = useRouter();
  // A disabled carrier outranks the number's own readiness: the row is off the
  // air for a reason that has nothing to do with this number, and "No agent"
  // next to an agent's name — inviting a pick the API would refuse — is the
  // wrong thing to say about it. The hint states the situation rather than
  // offering a way out: only the API can re-enable an account, so there is no
  // control on this page to point at.
  const readiness = carrierDisabled
    ? {
        label: "Carrier disabled",
        variant: "default" as const,
        hint: "This number cannot answer while its carrier is disabled.",
      }
    : READINESS[number.readiness];
  // assign_number requires an active, inbound-capable number on a provisioned
  // account; anything else is refused by the API, so it is not offered here.
  const assignable =
    accountReady && (number.readiness === "live" || number.readiness === "needs_agent");
  const canDial = accountReady && number.can_outbound && number.status === "active";
  const answering = !carrierDisabled && number.readiness === "live";
  // Both directions is the norm and needs no ink. Say so only when it is not.
  const oneWay =
    number.status !== "disabled" && number.can_inbound !== number.can_outbound
      ? number.can_outbound
        ? "Outbound only"
        : "Inbound only"
      : "";

  return (
    <div
      className={cn(
        // A fixed floor rather than natural height: the agent picker is 40px
        // and a bare "—" is 20, so without it the rows with something to do
        // would be the tall ones and the list would read as ragged.
        "group grid min-h-[64px] items-center gap-x-4 gap-y-3 border-b border-line px-4 py-2.5 transition-colors last:border-b-0 hover:bg-canvas",
        ROW_GRID,
        busy && "opacity-60",
      )}
    >
      <div className="min-w-0">
        <div className="truncate font-mono text-[13.5px] font-semibold leading-5 text-ink tabular-nums">
          {number.e164}
        </div>
        <div className="mt-0.5 flex min-w-0 items-center gap-2 text-[12px] leading-4">
          {number.label && <span className="truncate text-muted">{number.label}</span>}
          {oneWay && (
            <span className="flex-none rounded border border-line-2 bg-subtle px-1 py-px text-[10.5px] font-medium leading-4 text-muted">
              {oneWay}
            </span>
          )}
          {!number.label && !oneWay && <span className="text-placeholder">No label</span>}
        </div>
      </div>

      {/* Stacked on a phone, an empty cell is a blank line rather than a
          column, so a placeholder dash that reads fine in a table becomes a
          stray mark. Keep the column, drop the mark. */}
      <div className={cn("min-w-0", !assignable && !assignedAgent && "hidden sm:block")}>
        {assignable ? (
          <Select
            value={number.inbound_agent_id || ""}
            disabled={busy || agents.length === 0}
            aria-label={`Agent answering ${number.e164}`}
            onChange={(e) => onAssign(e.target.value)}
          >
            <option value="">
              {agents.length === 0 ? "No published voice agents" : "Not assigned"}
            </option>
            {agents.map((agent) => (
              <option key={agent.id} value={agent.id}>
                {agentLabel(agent)}
              </option>
            ))}
          </Select>
        ) : assignedAgent ? (
          // Still shown, because the number is out of service, not unassigned —
          // it goes back to this agent the moment it is working again.
          <span className="block truncate text-[13px] leading-5 text-muted">
            {agentLabel(assignedAgent)}
          </span>
        ) : (
          <span className="text-[13px] leading-5 text-placeholder">—</span>
        )}
      </div>

      <div className="hidden min-w-0 lg:block">
        {traffic ? (
          <>
            <div className="flex items-baseline gap-1.5">
              <span className="text-[13px] font-medium leading-5 text-ink tabular-nums">
                {traffic.calls}
              </span>
              <span className="truncate text-[11.5px] leading-4 text-faint tabular-nums">
                {money(traffic.spend, 2)}
              </span>
            </div>
            {traffic.days.length > 0 && (
              <Sparkline
                days={traffic.days}
                peak={peakDay || 1}
                label={`${traffic.calls} calls on ${number.e164} over the last ${traffic.days.length} days`}
              />
            )}
          </>
        ) : (
          <span className="text-[13px] leading-5 text-placeholder">No calls</span>
        )}
      </div>

      <div className="min-w-0">
        <Badge variant={readiness.variant} dot={answering}>
          {busy ? "Working…" : readiness.label}
        </Badge>
        {!answering && (
          <p
            className={cn(
              "mt-1 line-clamp-2 text-[11.5px] leading-4 [overflow-wrap:anywhere]",
              number.readiness === "error" ? "text-danger" : "text-faint",
            )}
          >
            {number.status_message || readiness.hint}
          </p>
        )}
      </div>

      <div className="flex items-center justify-end gap-0.5">
        {canDial && (
          <button
            type="button"
            aria-label={`Place a test call from ${number.e164}`}
            title="Place a test call"
            disabled={busy}
            onClick={onTestCall}
            className="flex h-8 w-8 items-center justify-center rounded-[10px] border border-transparent text-muted transition-colors hover:border-line-2 hover:bg-subtle hover:text-ink focus:outline-none focus-visible:ring-2 focus-visible:ring-ink/10 disabled:pointer-events-none disabled:opacity-40"
          >
            {PhoneOutIcon}
          </button>
        )}
        <Menu
          label={`More actions for ${number.e164}`}
          items={[
            {
              label: "Copy number",
              onSelect: () => void navigator.clipboard.writeText(number.e164),
            },
            // The picker names the agent but cannot link to it, and "what does
            // this number actually say when it answers?" is the next question
            // after seeing the name.
            {
              label: "Open the agent",
              disabled: !assignedAgent,
              onSelect: () => {
                if (assignedAgent) router.push(`/agents/detail?id=${assignedAgent.id}`);
              },
            },
            // Both go through provision_number, which the API refuses unless
            // the account is ready — so a paused carrier greys them out here
            // rather than answering a click with an error toast.
            {
              label: "Run setup again",
              disabled: busy || !accountReady || number.status === "disabled",
              onSelect: onProvision,
            },
            number.status === "disabled"
              ? { label: "Enable number", disabled: busy || !accountReady, onSelect: onEnable }
              : { label: "Disable number", disabled: busy, danger: true, onSelect: onDisable },
          ]}
        />
      </div>
    </div>
  );
}
