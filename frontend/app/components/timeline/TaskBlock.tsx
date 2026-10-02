"use client";
import { cn } from "@/lib/cn";
import { CopyButton } from "../ui";
import { fmtDuration } from "../SnapshotBand";
import { data, toolVerdict, type TimelineTask } from "./exchanges";
import { pretty } from "./ToolBeat";

/**
 * One task, as the bracket around everything that happened inside it.
 *
 * LiveKit writes a task as two activity switches — in, and back — with the
 * sub-conversation between them. Drawn as two loose pills that read as the
 * agent changing twice; drawn as a bracket it reads as what it is, a detour
 * that hands back a result. The call that entered the task is this header, and
 * what the task returned is the footer: at the moment it was returned, not at
 * the moment the task began.
 */
export function TaskBlock({
  task,
  clock,
  collapsed,
  onToggle,
  endedInside,
  children,
}: {
  task: TimelineTask;
  /* What to say of a task that never returned: the session ended inside it, or
     on a chat still open, it is simply where the conversation is. */
  endedInside: string;
  /* When it began, already formatted, and as a seek target when there is a
     recording to seek. */
  clock: React.ReactNode;
  collapsed: boolean;
  onToggle: () => void;
  children: React.ReactNode;
}) {
  const durationS = task.end
    ? Math.round((Date.parse(task.end.created_at) - Date.parse(task.start.created_at)) / 1000)
    : null;
  const verdict = toolVerdict(task.output, task.trace);
  /* The failure's own words: a timeout, or the reason the model gave to
     `finish_without_result`. LiveKit hands the model a fixed string for anything
     that is not a `ToolError`, so the trace is preferred. */
  const result = task.output ? pretty(data(task.output).output) : null;
  const failure = task.trace?.message ?? result;

  return (
    <div className="border-l-2 border-line-strong pl-4">
      <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1 py-0.5 text-[12.5px]">
        <span className="rounded-full border border-line-2 bg-subtle px-2 py-[1px] text-[11px] font-medium text-muted">
          Task
        </span>
        <span className="font-medium text-ink">{task.name}</span>
        <span className="text-[11.5px] tabular-nums text-faint">
          {clock}
          {durationS != null && ` · ${fmtDuration(durationS)}`}
          {` · ${task.messages} ${task.messages === 1 ? "message" : "messages"}`}
        </span>
        <button
          type="button"
          onClick={onToggle}
          aria-expanded={!collapsed}
          className="ml-auto text-[11.5px] text-muted underline decoration-line-strong underline-offset-2 hover:text-ink"
        >
          {collapsed ? "Show" : "Hide"}
        </button>
      </div>

      {!collapsed && <div className="grid gap-6 py-4">{children}</div>}

      <div className={cn("grid gap-1.5 text-[12.5px]", collapsed && "pt-2")}>
        <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1">
          <span className="font-medium text-ink-soft">
            {task.end ? (task.end.text ?? "Back") : endedInside}
          </span>
          {task.end && verdict === "ok" && (
            <span className="inline-flex items-center gap-1.5 text-live">
              <span className="h-1.5 w-1.5 rounded-full bg-current" aria-hidden />
              result submitted
            </span>
          )}
          {task.end && verdict === "failed" && (
            <span className="inline-flex items-center gap-1.5 text-danger">
              <span className="h-1.5 w-1.5 rounded-full bg-current" aria-hidden />
              no result
            </span>
          )}
        </div>
        {task.end && verdict === "failed" && failure && (
          <p className="rounded-md border border-danger/25 bg-danger/[0.05] px-2.5 py-1.5 leading-5 text-danger">
            {failure}
          </p>
        )}
        {task.end && verdict === "ok" && result && (
          <details>
            <summary className="cursor-pointer text-[11.5px] text-faint hover:text-muted">
              Result
            </summary>
            <div className="relative mt-1.5">
              <pre className="scroll-thin max-h-44 overflow-auto whitespace-pre-wrap break-words rounded-md bg-subtle px-2.5 py-2 font-mono text-[11.5px] leading-5 text-ink">
                {result}
              </pre>
              <CopyButton
                value={result}
                ariaLabel="Copy the task result"
                className="absolute right-1.5 top-1.5"
              />
            </div>
          </details>
        )}
      </div>
    </div>
  );
}
