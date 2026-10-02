"use client";

import Link from "next/link";
import { Badge, CopyButton, Menu, Select } from "@/app/components/ui";
import { cn } from "@/lib/cn";
import type { AgentResponse, StreamConnectionResponse } from "@talqing/sdk";
import { DIALECTS, READINESS, agentLabel, type StreamDialect } from "./shared";

/* Rows and their header share one template so the columns line up without a
   <table> — a cell holds a live <Select>, and the whole row has to reflow on a
   narrow screen rather than scroll sideways. Same arrangement as the phone
   numbers page, because these are the same kind of row: a thing that answers,
   and what answers on it. */
export const ROW_GRID =
  "sm:grid-cols-[minmax(0,1.25fr)_minmax(150px,0.8fr)_minmax(140px,0.6fr)_68px] lg:grid-cols-[minmax(0,1.25fr)_minmax(160px,0.78fr)_minmax(96px,0.4fr)_minmax(140px,0.58fr)_68px]";

/** Everything a partner's platform cannot do, as three short facts.
 *
 *  Shown on the row rather than buried in the setup panel because these are
 *  what a reader is actually comparing when they look at two connections side
 *  by side — and because "the agent can't hang up" is the sort of thing that is
 *  discovered in UAT unless somebody wrote it where it would be seen. */
function Capabilities({ dialect }: { dialect: StreamDialect }) {
  const spec = DIALECTS[dialect];
  const gaps = [
    spec.canHangup ? null : "cannot hang up",
    spec.canSendDtmf ? null : "cannot press keys",
    spec.sendsCallerNumber ? null : "sends no caller number",
  ].filter(Boolean) as string[];
  if (!gaps.length) return null;
  return (
    <span className="text-[12.5px] leading-5 text-faint">{gaps.join(" · ")}</span>
  );
}

export function ConnectionRow({
  connection,
  agents,
  assignedAgent,
  callsThisWeek,
  busy,
  highlighted,
  expanded,
  onToggle,
  onAssign,
  onDisable,
  onEnable,
  onDelete,
}: {
  connection: StreamConnectionResponse;
  /** Voice agents — the only thing that can answer a stream. */
  agents: AgentResponse[];
  assignedAgent: AgentResponse | undefined;
  callsThisWeek: number;
  busy: boolean;
  /** Arrived here from a call's "Stream connection" link. */
  highlighted: boolean;
  expanded: boolean;
  onToggle: () => void;
  onAssign: (agentId: string) => void;
  onDisable: () => void;
  onEnable: () => void;
  onDelete: () => void;
}) {
  const readiness = READINESS[connection.readiness];
  const disabled = connection.status === "disabled";

  return (
    <div
      className={cn(
        "border-b border-line last:border-b-0",
        highlighted && "bg-info/[0.05]",
        busy && "opacity-60",
      )}
    >
      <div className={cn("grid items-center gap-3 px-4 py-3.5 sm:grid", ROW_GRID)}>
        <button
          type="button"
          onClick={onToggle}
          className="min-w-0 text-left"
          aria-expanded={expanded}
        >
          <span className="flex items-center gap-1.5">
            <svg
              className={cn(
                "h-3.5 w-3.5 flex-none text-faint transition-transform duration-150",
                expanded && "rotate-90",
              )}
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              strokeLinejoin="round"
              aria-hidden
            >
              <path d="m9 6 6 6-6 6" />
            </svg>
            <span
              className={cn(
                "truncate text-[14px] font-semibold leading-5",
                disabled ? "text-muted" : "text-ink",
              )}
            >
              {connection.name}
            </span>
          </span>
          <span className="mt-0.5 block pl-5">
            <Capabilities dialect={connection.dialect as StreamDialect} />
          </span>
        </button>

        <div className="min-w-0">
          <Select
            value={connection.agent_id}
            disabled={busy}
            onChange={(e) => onAssign(e.target.value)}
            aria-label={`Agent answering ${connection.name}`}
          >
            {/* The connection's own agent is always an option, even if it has
                since been deleted or changed channel: otherwise the select would
                silently show a different agent than the one it points at. */}
            {!agents.some((a) => a.id === connection.agent_id) && (
              <option value={connection.agent_id}>
                {connection.agent_name || connection.agent_id.slice(0, 8)}
              </option>
            )}
            {agents.map((agent) => (
              <option key={agent.id} value={agent.id}>
                {agentLabel(agent)}
              </option>
            ))}
          </Select>
        </div>

        <div className="hidden text-[13px] leading-5 text-muted lg:block">
          {callsThisWeek > 0 ? (
            <>
              <span className="font-medium text-ink">{callsThisWeek}</span> this week
            </>
          ) : (
            "—"
          )}
        </div>

        <div className="min-w-0">
          <Badge variant={readiness.variant} dot={readiness.variant === "live"}>
            {readiness.label}
          </Badge>
          <span className="mt-0.5 block truncate text-[12.5px] leading-5 text-faint">
            {readiness.hint}
          </span>
        </div>

        <div className="flex justify-end">
          <Menu
            label={`Actions for ${connection.name}`}
            items={[
              disabled
                ? { label: "Enable", onSelect: onEnable }
                : { label: "Disable", onSelect: onDisable },
              { label: "Delete", onSelect: onDelete, danger: true },
            ]}
          />
        </div>
      </div>

      {expanded && (
        <SetupPanel
          connection={connection}
          assignedAgent={assignedAgent}
        />
      )}
    </div>
  );
}

/** What the partner has to do, and what this connection can and cannot do once
 *  they have done it.
 *
 *  Inline rather than a separate page: there are three or four facts per
 *  connection and none of them is worth a navigation. */
function SetupPanel({
  connection,
  assignedAgent,
}: {
  connection: StreamConnectionResponse;
  assignedAgent: AgentResponse | undefined;
}) {
  const spec = DIALECTS[connection.dialect as StreamDialect];
  const records = assignedAgent?.config?.recording?.enabled === true;
  /* Only worth a row when the agent asks for a bed, because that is the only
     case where the answer is a surprise. */
  const wantsBackgroundAudio = assignedAgent?.config?.background_audio?.enabled === true;

  return (
    <div className="border-t border-line bg-canvas px-4 py-4">
      <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_320px]">
        <div className="min-w-0">
          <div className="flex items-center gap-2 rounded-lg border border-line-2 bg-white px-3 py-2.5">
            <code className="min-w-0 flex-1 truncate font-mono text-[12.5px] leading-5 text-ink">
              {connection.url}
            </code>
            <CopyButton value={connection.url} label="Copy" />
          </div>
          <p className="mt-1.5 text-[12.5px] leading-5 text-faint">
            {/* Said here rather than left to be discovered: the URL names the
                agent, so it is an address and not a credential, and the only
                thing that stops it answering is this connection's own switch. */}
            The URL carries no secret — it names the platform, the workspace and the agent. To stop
            it answering, disable this connection or point it at another agent; deleting and
            recreating it gives the same URL back.
          </p>

          <ol className="mt-4 grid gap-2">
            {spec.steps.map((step, index) => (
              <li key={index} className="flex gap-2.5 text-[13px] leading-5 text-ink-soft">
                <span className="mt-px flex h-[18px] w-[18px] flex-none items-center justify-center rounded-full border border-line-2 bg-white text-[11px] font-semibold text-muted">
                  {index + 1}
                </span>
                <span className="min-w-0">{step}</span>
              </li>
            ))}
          </ol>

          {spec.snippet && (
            <div className="mt-3">
              <div className="flex items-center justify-between gap-2 pb-1.5">
                <span className="text-[12px] font-semibold uppercase tracking-[0.06em] text-faint">
                  {spec.snippet.caption}
                </span>
                <CopyButton
                  value={() => spec.snippet!.body(connection.url)}
                  label="Copy"
                  ariaLabel="Copy the snippet"
                />
              </div>
              <pre className="overflow-x-auto rounded-lg border border-line-2 bg-white px-3 py-2.5 font-mono text-[12px] leading-5 text-ink-soft">
                {spec.snippet.body(connection.url)}
              </pre>
            </div>
          )}
        </div>

        <dl className="grid h-fit gap-0 divide-y divide-line overflow-hidden rounded-lg border border-line-2 bg-white text-[12.5px] leading-5">
          <Fact term="Audio" value={spec.audio} />
          <Fact
            term="Ending a call"
            value={
              spec.canHangup
                ? "The agent hangs up, and the caller's call ends."
                : "The agent finishing closes the stream, and the caller goes back to the partner's own flow. It does not hang up."
            }
          />
          <Fact
            term="Keypad"
            value={
              spec.canSendDtmf
                ? "The caller can type, and the agent can press keys back."
                : "The caller can type. The agent cannot press keys — this protocol has no command for it, and a send_dtmf operation refuses itself by name."
            }
          />
          <Fact
            term="Escalation"
            value="A streamed agent cannot transfer to a human — the partner owns the phone line. Hand the caller back to their flow instead."
          />
          {wantsBackgroundAudio && (
            <Fact
              term="Background audio"
              value="Off on streamed calls. The ambient and thinking sounds are a second audio track, and a partner's socket carries one — a phone call hears them only because the carrier side mixes. The agent's own voice is unaffected."
            />
          )}
          <Fact
            term="Recording"
            value={
              records
                ? `On, because ${assignedAgent ? agentLabel(assignedAgent) : "this agent"} records. Digits typed on a keypad are never in it.`
                : "Off, because the agent it runs does not record."
            }
          >
            {assignedAgent && (
              <Link
                href={`/agents/detail?id=${assignedAgent.id}`}
                className="font-medium text-ink underline underline-offset-2"
              >
                Change it on the agent
              </Link>
            )}
          </Fact>
        </dl>
      </div>
    </div>
  );
}

function Fact({
  term,
  value,
  children,
}: {
  term: string;
  value: string;
  children?: React.ReactNode;
}) {
  return (
    <div className="grid gap-0.5 px-3 py-2.5 sm:grid-cols-[86px_minmax(0,1fr)] sm:gap-3">
      <dt className="font-medium text-ink">{term}</dt>
      <dd className="text-muted">
        {value}
        {children && <span className="block pt-1">{children}</span>}
      </dd>
    </div>
  );
}
