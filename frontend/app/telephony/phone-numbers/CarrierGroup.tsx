"use client";

import { Badge, CopyButton, Menu } from "@/app/components/ui";
import { cn } from "@/lib/cn";
import type {
  AgentResponse,
  PhoneNumberResponse,
  TelephonyAccountResponse,
  TelephonyProviderSpec,
} from "@talqing/sdk";
import { CarrierLogo } from "./CarrierLogo";
import { CarrierSetupCard } from "./CarrierSetupCard";
import { NumberRow, ROW_GRID } from "./NumberRow";
import {
  ACCOUNT_STATUS,
  accountIdentity,
  needsAttention,
  plural,
  providerLabel,
  providerSpec,
} from "./shared";
import type { NumberTraffic } from "./traffic";

/**
 * One carrier account and the numbers that ride on it.
 *
 * The grouping is the page: a DID is only reachable through the trunk its
 * account owns, so an account that is failing, or one waiting on console work,
 * explains every row underneath it at once. It also retires the per-row
 * "Carrier" column, which used to repeat the same two words down the whole
 * table.
 */
export function CarrierGroup({
  account,
  owned,
  numbers,
  catalog,
  agents,
  agentsById,
  traffic,
  peakDay,
  windowDays,
  busyId,
  accountBusy,
  onAssign,
  onTestCall,
  onProvisionNumber,
  onDisableNumber,
  onEnableNumber,
  onProvisionAccount,
  onDeleteAccount,
  onSetupConfirmed,
  onError,
  onImport,
}: {
  account: TelephonyAccountResponse;
  /** Every number on this account. The header counts these, never the rows —
      a filter narrows what is listed, not what the carrier owns. */
  owned: PhoneNumberResponse[];
  /** The rows to list, already filtered and sorted. */
  numbers: PhoneNumberResponse[];
  catalog: TelephonyProviderSpec[];
  agents: AgentResponse[];
  agentsById: Map<string, AgentResponse>;
  traffic: Map<string, NumberTraffic>;
  peakDay: number;
  windowDays: number;
  busyId: string;
  accountBusy: boolean;
  onAssign: (number: PhoneNumberResponse, agentId: string) => void;
  onTestCall: (number: PhoneNumberResponse) => void;
  onProvisionNumber: (number: PhoneNumberResponse) => void;
  onDisableNumber: (number: PhoneNumberResponse) => void;
  onEnableNumber: (number: PhoneNumberResponse) => void;
  onProvisionAccount: () => void;
  onDeleteAccount: () => void;
  onSetupConfirmed: (message: string) => void;
  onError: (message: string) => void;
  onImport: () => void;
}) {
  const spec = providerSpec(account.provider, catalog);
  const status = ACCOUNT_STATUS[account.status];
  const identity = accountIdentity(account, spec);
  const carrier = providerLabel(account.provider, catalog);
  const attention = owned.filter(needsAttention).length;
  const hidden = owned.length - numbers.length;
  /* Every per-number write the rows offer — assign, provision, dial — is
     refused by the API unless the account is `ready`. The rows are told, so
     they can stop offering rather than fail on click. */
  const carrierDisabled = account.status === "disabled";
  const accountReady = account.status === "ready";

  return (
    <section className="overflow-hidden rounded-xl border border-line-2 bg-white">
      <header className="flex flex-wrap items-center gap-x-4 gap-y-2.5 border-b border-line bg-canvas px-4 py-3">
        <span className="grid h-9 w-9 flex-none place-items-center rounded-[10px] border border-line-2 bg-white text-ink">
          <CarrierLogo spec={spec} />
        </span>
        <div className="min-w-0 flex-1">
          <div className="truncate font-display text-[14.5px] font-semibold leading-5 tracking-tight text-ink">
            {account.display_name}
          </div>
          <div className="mt-0.5 flex min-w-0 items-center gap-1.5 text-[12px] leading-4 text-muted">
            {/* The carrier's name is only worth repeating when the account has
                been renamed to something that no longer says it. */}
            {account.display_name.toLowerCase().includes(carrier.toLowerCase()) ? null : (
              <>
                <span className="flex-none">{carrier}</span>
                <span aria-hidden className="flex-none text-line-strong">
                  ·
                </span>
              </>
            )}
            {identity && (
              <>
                <span className="flex-none text-faint">{identity.label}</span>
                <span className="min-w-0 truncate font-mono text-[11.5px] text-ink-soft">
                  {identity.value}
                </span>
                <CopyButton
                  value={identity.value}
                  ariaLabel={`Copy ${identity.label}`}
                  className="flex-none"
                />
              </>
            )}
          </div>
        </div>

        <div className="flex flex-none items-center gap-3">
          <span className="text-[12.5px] leading-5 text-muted tabular-nums">
            {plural(owned.length, "number")}
            {hidden > 0 && <span className="text-faint"> · {numbers.length} shown</span>}
          </span>
          {attention > 0 && (
            <span className="flex items-center gap-1.5 text-[12.5px] font-medium leading-5 text-warn">
              <span aria-hidden className="h-1.5 w-1.5 rounded-full bg-warn" />
              {attention} need{attention === 1 ? "s" : ""} attention
            </span>
          )}
          <Badge variant={status.variant} dot={account.status === "ready"}>
            {accountBusy ? "Working…" : status.label}
          </Badge>
          <Menu
            label={`Actions for ${account.display_name}`}
            items={[
              {
                label: account.status === "ready" ? "Run setup again" : "Finish setup",
                disabled: accountBusy || account.status === "disabled",
                onSelect: onProvisionAccount,
              },
              {
                label: "Import a number",
                // A disabled carrier is not offered in the wizard, so opening
                // it from here would lead to a list this account is missing
                // from.
                disabled: accountBusy || carrierDisabled,
                onSelect: onImport,
              },
              {
                label: "Disconnect carrier",
                danger: true,
                disabled: accountBusy,
                onSelect: onDeleteAccount,
              },
            ]}
          />
        </div>
      </header>

      {account.status_message && (
        <p className="border-b border-line bg-danger/[0.04] px-4 py-2.5 text-[12.5px] leading-5 text-danger [overflow-wrap:anywhere]">
          {account.status_message}
        </p>
      )}

      {/* Carrier-console work blocks inbound on every number below it, so the
          instructions sit between the account and the rows they explain. */}
      <CarrierSetupCard account={account} onConfirmed={onSetupConfirmed} onError={onError} />

      {owned.length === 0 ? (
        <p className="px-4 py-8 text-center text-[13px] leading-5 text-muted">
          No numbers on this carrier yet.{" "}
          <button
            type="button"
            onClick={onImport}
            className="font-medium text-ink underline underline-offset-2 hover:text-ink-hover"
          >
            Import one
          </button>{" "}
          to put an agent on the phone.
        </p>
      ) : (
        <>
          <div
            className={cn(
              "hidden gap-x-4 border-b border-line px-4 py-2 text-[12px] font-medium leading-4 text-faint sm:grid",
              ROW_GRID,
            )}
          >
            <span>Number</span>
            <span>Answered by</span>
            <span className="hidden lg:block">Last {windowDays} days</span>
            <span>Status</span>
            <span className="sr-only">Actions</span>
          </div>
          {numbers.map((number) => (
            <NumberRow
              key={number.id}
              number={number}
              agents={agents}
              assignedAgent={
                number.inbound_agent_id ? agentsById.get(number.inbound_agent_id) : undefined
              }
              traffic={traffic.get(number.id)}
              peakDay={peakDay}
              busy={busyId === number.id}
              carrierDisabled={carrierDisabled}
              accountReady={accountReady}
              onAssign={(agentId) => onAssign(number, agentId)}
              onTestCall={() => onTestCall(number)}
              onProvision={() => onProvisionNumber(number)}
              onDisable={() => onDisableNumber(number)}
              onEnable={() => onEnableNumber(number)}
            />
          ))}
        </>
      )}
    </section>
  );
}
