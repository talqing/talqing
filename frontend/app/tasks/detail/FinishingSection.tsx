"use client";
import type { TaskConfig } from "@talqing/sdk";
import { Label, Textarea } from "@/app/components/ui";

/* One field. There is no placeholder and no reset, because there is nothing to
   fall back to: a new task is created carrying a one-line stub, and from then on
   whatever stands here is the whole of what the model is told about finishing. */
function ToolDescription({
  id,
  tool,
  title,
  hint,
  value,
  onChange,
}: {
  id: string;
  tool: string;
  title: string;
  hint: React.ReactNode;
  value: string;
  onChange: (next: string) => void;
}) {
  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-baseline gap-2">
        <Label htmlFor={id}>{title}</Label>
        <code className="font-mono text-[12px] text-faint">{tool}</code>
      </div>
      <p className="text-[12px] leading-5 text-muted">{hint}</p>
      <Textarea
        id={id}
        className="min-h-[96px] text-[13px]"
        value={value}
        aria-invalid={!value.trim()}
        onChange={(e) => onChange(e.target.value)}
      />
      {/* Not a blank — a task that cannot be published. The save refuses it too,
          but the save is the wrong place to hear it first. */}
      {!value.trim() && (
        <span className="text-[11.5px] leading-4 text-warn">
          Required. The model is handed this sentence and the tool&rsquo;s name, nothing else.
        </span>
      )}
    </div>
  );
}

/** The two tools every task is given, and the whole of what the model is told
 *  about how a run ends.
 *
 *  Nothing is appended to the prompt — no generated paragraph about calling
 *  `submit_result`, no generated sentence about when giving up is allowed. Both
 *  used to exist and both landed after the author's own text, where they won on
 *  recency and could not be argued with. Each tool now says its piece once, in
 *  the description the author owns.
 *
 *  Which is why the hints below read as writing instructions rather than field
 *  labels: a new task arrives with a one-line stub in each box, and replacing it
 *  is part of building the task. */
export function FinishingSection({
  cfg,
  onChange,
}: {
  cfg: TaskConfig;
  onChange: (next: Partial<TaskConfig>) => void;
}) {
  return (
    /* A prose column rather than the panel's full width. These two are
       sentences, written and re-read as sentences — set across 1300px both the
       hint above and the text inside run as single unreadable lines. */
    <div className="flex max-w-[880px] flex-col gap-5">
      <ToolDescription
        id="task-submit-description"
        tool="submit_result"
        title="Finishing with a result"
        hint={
          <>
            The only thing telling the model that calling this tool is how a run ends — nothing
            in the prompt says so. Worth saying here: when the job is done enough to call it, and
            that the call <em>is</em> the result and a written answer is not.
          </>
        }
        value={cfg.submit_result_description ?? ""}
        onChange={(submit_result_description) => onChange({ submit_result_description })}
      />
      <ToolDescription
        id="task-abandon-description"
        tool="finish_without_result"
        title="Finishing without one"
        hint={
          <>
            Only exists when an agent enters this task — a run started on its own has no
            caller to change their mind. Narrow this if the task gives up on jobs it should
            have finished: with Required off, a field accepts <code className="font-mono">null</code>,
            so &ldquo;I could not find it&rdquo; is a result, not a reason to abandon.
          </>
        }
        value={cfg.finish_without_result_description ?? ""}
        onChange={(finish_without_result_description) =>
          onChange({ finish_without_result_description })
        }
      />
    </div>
  );
}
