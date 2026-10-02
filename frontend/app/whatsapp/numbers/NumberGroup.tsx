"use client";

import type { AgentResponse, IntegrationResponse, IntegrationTriggerResponse } from "@talqing/sdk";
import { Badge, BoxCheckbox, Button, CopyButton, Menu, Select } from "@/app/components/ui";
import type { MenuItem } from "@/app/components/ui";
import { cn } from "@/lib/cn";
import { timeAgo } from "@/app/telephony/outbound-calling/shared";
import { agentLabel, plural } from "@/app/telephony/phone-numbers/shared";
import type { TriggerType } from "@/lib/triggers";
import { formatPhone } from "../outbound/shared";

const MESSAGE_TRIGGER: TriggerType = "whatsapp.message.inbound";
const CALL_TRIGGER: TriggerType = "whatsapp.call.inbound";

/* Rows and their header share one template so the columns line up without a
   <table>; each cell holds a live picker, and a phone stacks them instead. */
const ROW_GRID =
  "sm:grid-cols-[minmax(0,0.8fr)_minmax(0,1fr)_minmax(0,1fr)_84px_40px] lg:grid-cols-[minmax(150px,1fr)_minmax(0,300px)_minmax(0,300px)_112px_84px]";

/** One BSP account and the numbers on it: a Twilio account, or a Gupshup app. */
export type Account = {
  key: string;
  bsp: "twilio" | "gupshup";
  numbers: IntegrationResponse[];
};

export type CellState = { busy: boolean; error: string };

export function NumberGroup({
  account,
  triggers,
  textAgents,
  voiceAgents,
  cells,
  resuming,
  onSave,
  onResume,
  onUpdateCredential,
  onDisconnect,
}: {
  account: Account;
  triggers: Record<string, IntegrationTriggerResponse[]>;
  textAgents: AgentResponse[];
  voiceAgents: AgentResponse[];
  cells: Record<string, CellState>;
  resuming: string;
  onSave: (number: IntegrationResponse, triggerType: TriggerType, agentId: string, enabled: boolean) => void;
  onResume: (number: IntegrationResponse) => void;
  onUpdateCredential: (numbers: IntegrationResponse[]) => void;
  onDisconnect: (number: IntegrationResponse) => void;
}) {
  const info = account.numbers[0].provider_account_info ?? {};
  const twilio = account.bsp === "twilio";
  const identity = twilio ? String(info.account_sid) : String(info.app_id);

  return (
    <section className="overflow-hidden rounded-xl border border-line-2 bg-white">
      <header className="flex flex-wrap items-center gap-x-4 gap-y-2.5 border-b border-line bg-canvas px-4 py-3">
        <span className="grid h-9 w-9 flex-none place-items-center rounded-[10px] border border-line-2 bg-white">
          <img
            src={`/logos/${account.bsp}.png`}
            alt=""
            aria-hidden="true"
            width={20}
            height={20}
            className="block rounded-[4px] object-contain"
          />
        </span>
        <div className="min-w-0 flex-1">
          <div className="truncate font-display text-[14.5px] font-semibold leading-5 tracking-tight text-ink">
            {twilio ? "Twilio" : String(info.app_name)}
          </div>
          <div className="mt-0.5 flex min-w-0 items-center gap-1.5 text-[12px] leading-4 text-muted">
            {!twilio && (
              <>
                <span className="flex-none">Gupshup</span>
                <span aria-hidden className="flex-none text-line-strong">
                  ·
                </span>
              </>
            )}
            <span className="flex-none text-faint">{twilio ? "Account" : "App"}</span>
            <span className="min-w-0 truncate font-mono text-[11.5px] text-ink-soft" title={identity}>
              {identity.slice(0, 10)}…{identity.slice(-4)}
            </span>
            <CopyButton
              value={identity}
              ariaLabel={twilio ? "Copy account SID" : "Copy app ID"}
              className="flex-none"
            />
          </div>
        </div>
        <div className="flex flex-none items-center gap-3">
          <span className="text-[12.5px] leading-5 text-muted tabular-nums">
            {plural(account.numbers.length, "number")}
          </span>
          {twilio && (
            <Menu
              label="Actions for this Twilio account"
              items={[{ label: "Update auth token", onSelect: () => onUpdateCredential(account.numbers) }]}
            />
          )}
        </div>
      </header>

      <div
        className={cn(
          "hidden gap-x-4 border-b border-line px-4 py-2 text-[12px] font-medium leading-4 text-faint sm:grid",
          ROW_GRID,
        )}
      >
        <span>Number</span>
        <span>Messages</span>
        <span>Calls</span>
        <span>Last activity</span>
        <span className="sr-only">Actions</span>
      </div>
      {account.numbers.map((number) => (
        <NumberRow
          key={number.id}
          number={number}
          triggers={triggers[number.id] ?? []}
          textAgents={textAgents}
          voiceAgents={voiceAgents}
          cells={cells}
          resuming={resuming === number.id}
          onSave={(triggerType, agentId, enabled) => onSave(number, triggerType, agentId, enabled)}
          onResume={() => onResume(number)}
          menu={[
            ...(twilio ? [] : [{ label: "Update API key", onSelect: () => onUpdateCredential([number]) }]),
            { label: "Disconnect number", danger: true, onSelect: () => onDisconnect(number) },
          ]}
        />
      ))}
    </section>
  );
}

function NumberRow({
  number,
  triggers,
  textAgents,
  voiceAgents,
  cells,
  resuming,
  onSave,
  onResume,
  menu,
}: {
  number: IntegrationResponse;
  triggers: IntegrationTriggerResponse[];
  textAgents: AgentResponse[];
  voiceAgents: AgentResponse[];
  cells: Record<string, CellState>;
  resuming: boolean;
  onSave: (triggerType: TriggerType, agentId: string, enabled: boolean) => void;
  onResume: () => void;
  menu: MenuItem[];
}) {
  const e164 = String(number.provider_account_info?.sender_e164);
  const paused = number.status !== "active";
  const lastActivity = number.webhook_setup?.last_received_at;
  // Named by default after the number, which the row already says.
  const named = number.display_name !== `WhatsApp ${e164}`;
  const cellFor = (triggerType: TriggerType) => ({
    trigger: triggers.find((t) => t.trigger_type === triggerType),
    state: cells[`${number.id}:${triggerType}`] ?? { busy: false, error: "" },
    onSave: (agentId: string, enabled: boolean) => onSave(triggerType, agentId, enabled),
    paused,
    e164: formatPhone(e164),
  });

  return (
    <div
      className={cn(
        "grid min-h-[64px] items-start gap-x-4 gap-y-3 border-b border-line px-4 py-3 transition-colors last:border-b-0 hover:bg-canvas/60",
        ROW_GRID,
      )}
    >
      <div className="flex min-w-0 items-start justify-between gap-3 sm:block sm:pt-2">
        <div className="min-w-0">
          <div className="truncate font-mono text-[13.5px] font-semibold leading-5 text-ink tabular-nums">
            {formatPhone(e164)}
          </div>
          {(named || paused) && (
            <div className="mt-0.5 flex min-w-0 items-center gap-2 text-[12px] leading-4">
              {paused && <Badge variant="warn">Paused</Badge>}
              {named && <span className="truncate text-muted">{number.display_name}</span>}
            </div>
          )}
        </div>
        {/* On a phone the actions ride on the number's line; from sm up they
            have a column of their own. */}
        <div className="flex flex-none items-center gap-1 sm:hidden">
          {paused && (
            <Button size="sm" variant="secondary" onClick={onResume} disabled={resuming}>
              Resume
            </Button>
          )}
          <Menu label={`Actions for ${e164}`} items={menu} />
        </div>
      </div>

      <Assignment label="Messages" agents={textAgents} channel="text" {...cellFor(MESSAGE_TRIGGER)} />

      {number.provider_account_info?.bsp === "twilio" ? (
        <Assignment label="Calls" agents={voiceAgents} channel="voice" {...cellFor(CALL_TRIGGER)} />
      ) : (
        <div className="min-w-0">
          <CellCaption>Calls</CellCaption>
          <p className="text-[13px] leading-5 text-faint sm:pt-2.5">Not available on Gupshup yet</p>
        </div>
      )}

      <div className="hidden text-[13px] leading-5 sm:block sm:pt-2.5">
        {lastActivity ? (
          <span className="text-ink-soft" title={new Date(lastActivity).toLocaleString()}>
            {timeAgo(lastActivity)}
          </span>
        ) : (
          <span className="text-placeholder">—</span>
        )}
      </div>

      <div className="hidden items-center justify-end gap-1 sm:flex sm:pt-1">
        {paused && (
          <Button size="sm" variant="secondary" onClick={onResume} disabled={resuming}>
            {resuming ? "Resuming…" : "Resume"}
          </Button>
        )}
        <Menu label={`Actions for ${e164}`} items={menu} />
      </div>
    </div>
  );
}

/** Stacked on a phone the column heads are gone, so each cell names itself. */
function CellCaption({ children }: { children: React.ReactNode }) {
  return <div className="mb-1.5 text-[12px] font-medium leading-4 text-faint sm:hidden">{children}</div>;
}

/**
 * Who answers one kind of traffic on a number: an agent, and whether it is on.
 *
 * Turning it off keeps the agent, so turning it back on needs no pick, and a
 * new pick while it is on takes over at once. Both are one trigger underneath.
 */
function Assignment({
  label,
  agents,
  channel,
  trigger,
  state,
  onSave,
  paused,
  e164,
}: {
  label: string;
  agents: AgentResponse[];
  channel: "text" | "voice";
  trigger: IntegrationTriggerResponse | undefined;
  state: CellState;
  onSave: (agentId: string, enabled: boolean) => void;
  paused: boolean;
  /** Every row has the same two cells, so each control names its number. */
  e164: string;
}) {
  const agentId = trigger?.agent_id ?? "";
  const on = Boolean(trigger?.enabled && trigger.status === "active");
  // The provider refused the last turn-on, which left it off.
  const failed = trigger?.status === "error";
  const assigned = agents.find((a) => a.id === agentId);
  const hint = state.error || (failed ? "Didn't turn on. Try again." : "");

  if (paused) {
    return (
      <div className="min-w-0">
        <CellCaption>{label}</CellCaption>
        <p className="truncate text-[13px] leading-5 text-muted sm:pt-2.5">
          {/* What it goes back to on Resume: only traffic that was switched on. */}
          {on && assigned ? agentLabel(assigned) : <span className="text-placeholder">Off</span>}
        </p>
      </div>
    );
  }

  return (
    <div className={cn("min-w-0", state.busy && "opacity-60")}>
      <CellCaption>{label}</CellCaption>
      <div className="flex min-w-0 items-center gap-2.5">
        <BoxCheckbox
          checked={on}
          disabled={state.busy || !agentId}
          onChange={(next) => onSave(agentId, next)}
          ariaLabel={`${on ? "Stop answering" : "Answer"} ${label.toLowerCase()} on ${e164}`}
        />
        <Select
          value={agentId}
          disabled={state.busy || agents.length === 0}
          aria-label={`Agent answering ${label.toLowerCase()} on ${e164}`}
          // The first pick puts the agent on, as it does for a phone number;
          // after that the switch alone decides.
          onChange={(e) => onSave(e.target.value, on || !trigger)}
          className="min-w-0 flex-1"
        >
          <option value="" disabled>
            {agents.length === 0 ? `No published ${channel} agents` : "Choose an agent"}
          </option>
          {agents.map((agent) => (
            <option key={agent.id} value={agent.id}>
              {agentLabel(agent)}
            </option>
          ))}
        </Select>
      </div>
      {hint && (
        <p className="mt-1.5 line-clamp-3 text-[12px] leading-4 text-danger [overflow-wrap:anywhere]" title={hint}>
          {hint}
        </p>
      )}
    </div>
  );
}
