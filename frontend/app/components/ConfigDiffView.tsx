"use client";

import { cn } from "@/lib/cn";
import { DiffEmpty, FieldRow, Section, StatusRow, STATUS_ROW } from "./DiffView";
import type { ConfigDiff, ToolChange } from "./diff";

/* ── reading a config diff ──────────────────────────────────────────────────
   One view for both an agent's versions and a task's: they produce the same
   `ConfigDiff` shape, and a second copy of this would drift.

   Sections in config order, then the tools. The tools section earns its place:
   two versions can hold an identical config and still behave differently,
   because a tool was republished between them, and the pinned version numbers
   are the only place that shows.

   It names the tool and the versions and stops there. What changed *inside*
   book_appointment between v1 and v2 is a question about the tool, and the tool
   editor's own history answers it properly. */

function ToolRow({ change }: { change: ToolChange }): JSX.Element {
  const versions =
    change.status === "changed"
      ? `v${change.before} → v${change.after}`
      : change.status === "added"
        ? change.after == null ? "attached" : `v${change.after}`
        : change.before == null ? "detached" : `v${change.before}`;

  return (
    <StatusRow
      status={change.status}
      head={
        <>
          <span className="min-w-0 truncate text-[13px] font-medium leading-5 text-ink">{change.name}</span>
          <span className="flex-none font-mono text-[12px] leading-5 text-muted">{versions}</span>
          {change.deleted && <span className="flex-none text-[11.5px] leading-5 text-danger">deleted</span>}
        </>
      }
    />
  );
}

export function ConfigDiffView({
  diff,
  emptyLabel,
  initial,
  className,
}: {
  diff: ConfigDiff;
  emptyLabel: string;
  /** Nothing came before — a first publish. Reads as a listing, not a change. */
  initial?: boolean;
  className?: string;
}): JSX.Element {
  if (!diff.changed) return <DiffEmpty label={emptyLabel} className={className} />;

  return (
    <div className={cn("flex min-w-0 flex-col gap-4", className)}>
      {diff.sections.map((section) => (
        <Section key={section.title} title={section.title} count={section.changes.length}>
          <div className={cn("rounded-lg border px-3 py-1.5", initial ? STATUS_ROW.added : STATUS_ROW.changed)}>
            {section.changes.map((change) => (
              <FieldRow key={change.field} change={change} initial={initial} />
            ))}
          </div>
        </Section>
      ))}

      <Section title={`Tools · ${diff.tools.length}`} count={diff.tools.length}>
        <div className="flex flex-col gap-1.5">
          {diff.tools.map((change) => (
            <ToolRow key={change.tool_id} change={change} />
          ))}
        </div>
      </Section>
    </div>
  );
}
