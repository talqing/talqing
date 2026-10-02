"use client";

import { cn } from "@/lib/cn";
import {
  DiffEmpty,
  FieldRow,
  Section,
  STATUS_MARK,
  STATUS_ROW,
  STATUS_TEXT,
  StatusRow,
} from "@/app/components/DiffView";
import { toLocal } from "./definition";
import { KIND_LABEL } from "./operationMetadata";
import { OperationIcon, operationSummary } from "./OperationFlowchart";
import type { ArgChange, OperationDiff, ToolDiff } from "./toolDiff";

/* ── reading a diff ─────────────────────────────────────────────────────────
   Rendered as an indented tree rather than as the flowchart the editor shows on
   the right: a diff is read top to bottom, and the flowchart's branch split
   spends its width on structure that a reader scanning for "what changed"
   does not need. Unchanged operations still appear, greyed — otherwise a change
   loses the one thing that says where in the tool it happened.

   Colour is the token ramp with opacity washes, never a tint of its own:
   `live` = added, `danger` = removed, `warn` = changed. */

function ArgRow({ arg }: { arg: ArgChange }): JSX.Element {
  return (
    <StatusRow
      status={arg.status}
      head={
        <>
          <span className="min-w-0 truncate font-mono text-[13px] font-medium leading-5 text-ink">{arg.name}</span>
          <span className="flex-none text-[12px] leading-5 text-muted">{arg.type}</span>
          {arg.required && <span className="flex-none text-[11.5px] leading-5 text-warn">required</span>}
        </>
      }
    >
      {arg.changes.length > 0 && (
        <div className="mt-0.5 pl-5">
          {arg.changes.map((change) => (
            <FieldRow key={change.field} change={change} />
          ))}
        </div>
      )}
    </StatusRow>
  );
}

function OperationRow({
  node,
  agentNameById,
}: {
  node: OperationDiff;
  agentNameById: Record<string, string>;
}): JSX.Element {
  const summary = operationSummary(toLocal([node.op])[0], agentNameById);
  const branchy = node.op.kind === "if";

  return (
    <div className="flex min-w-0 flex-col">
      <div className={cn("rounded-lg border px-3 py-2", STATUS_ROW[node.status])}>
        <div className="flex min-w-0 items-center gap-2">
          <span className={cn("w-3 flex-none text-center font-mono text-[12px] font-semibold leading-5", STATUS_TEXT[node.status])}>
            {STATUS_MARK[node.status]}
          </span>
          <OperationIcon kind={node.op.kind} className={cn("h-4 w-4", node.status === "unchanged" ? "text-muted" : "text-ink")} />
          <span className={cn("flex-none text-[13px] font-medium leading-5", node.status === "unchanged" ? "text-muted" : "text-ink")}>
            {KIND_LABEL[node.op.kind] ?? node.op.kind}
          </span>
          {summary.primary && (
            <span className="min-w-0 truncate text-[12.5px] leading-5 text-muted" title={summary.primary}>
              {summary.primary}
            </span>
          )}
          {node.status === "unchanged" && node.branchesChanged && (
            <span className="ml-auto flex-none text-[11.5px] leading-5 text-faint">changed inside</span>
          )}
        </div>
        {node.changes.length > 0 && (
          <div className="mt-0.5 pl-5">
            {node.changes.map((change) => (
              <FieldRow key={change.field} change={change} />
            ))}
          </div>
        )}
      </div>
      {branchy && (
        <div className="mt-1.5 flex flex-col gap-1.5 border-l border-line-2 pl-3">
          <Branch label="then" nodes={node.then ?? []} agentNameById={agentNameById} />
          <Branch label="else" nodes={node.else ?? []} agentNameById={agentNameById} />
        </div>
      )}
    </div>
  );
}

function Branch({
  label,
  nodes,
  agentNameById,
}: {
  label: "then" | "else";
  nodes: OperationDiff[];
  agentNameById: Record<string, string>;
}): JSX.Element {
  return (
    <div className="flex min-w-0 flex-col gap-1.5">
      <span className="text-[10.5px] font-semibold uppercase leading-4 tracking-[0.06em] text-faint">{label}</span>
      {nodes.length ? (
        nodes.map((child, i) => <OperationRow key={i} node={child} agentNameById={agentNameById} />)
      ) : (
        <div className="text-[12px] leading-5 text-faint">empty</div>
      )}
    </div>
  );
}

export function ToolDiffView({
  diff,
  agentNameById,
  emptyLabel,
  initial,
  className,
}: {
  diff: ToolDiff;
  agentNameById: Record<string, string>;
  emptyLabel: string;
  /** Nothing came before — a first publish. Reads as a listing, not a change. */
  initial?: boolean;
  className?: string;
}): JSX.Element {
  if (!diff.changed) return <DiffEmpty label={emptyLabel} className={className} />;

  const tally = [
    diff.counts.added && `${diff.counts.added} added`,
    diff.counts.removed && `${diff.counts.removed} removed`,
    diff.counts.changed && `${diff.counts.changed} changed`,
  ].filter(Boolean) as string[];

  return (
    <div className={cn("flex min-w-0 flex-col gap-4", className)}>
      <Section title="Behaviour" count={diff.behaviour.length}>
        <div className={cn("rounded-lg border px-3 py-1.5", initial ? STATUS_ROW.added : STATUS_ROW.changed)}>
          {diff.behaviour.map((change) => (
            <FieldRow key={change.field} change={change} initial={initial} />
          ))}
        </div>
      </Section>

      <Section title="Arguments" count={diff.args.length}>
        <div className="flex flex-col gap-1.5">
          {diff.args.map((arg) => (
            <ArgRow key={arg.name} arg={arg} />
          ))}
        </div>
      </Section>

      {/* The whole tree is listed so a change keeps its place in it — but only
          when something in it actually changed. A description-only edit should
          not print every operation greyed out. */}
      <Section title={`Operations · ${tally.join(", ")}`} count={tally.length ? diff.operations.length : 0}>
        <div className="flex flex-col gap-1.5">
          {diff.operations.map((node, i) => (
            <OperationRow key={i} node={node} agentNameById={agentNameById} />
          ))}
        </div>
      </Section>
    </div>
  );
}
