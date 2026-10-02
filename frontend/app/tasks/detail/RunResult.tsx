"use client";

import { useState } from "react";
import Link from "next/link";
import type { CatalogResponse, TaskConfig, TaskRunResponse, TraceStepResponse } from "@talqing/sdk";
import { Badge, btn } from "@/app/components/ui";
import { missingKeyProvider } from "@/lib/byok";
import { cn } from "@/lib/cn";
import { formatCost, formatDuration, runOutcome } from "./outcome";

/* How one run reads: what came back, what the model did on the way, and what it
   cost.

   Shared by the task editor's Run panel and by an email batch's review table,
   which links every drafted row to the run that wrote it — "why did it write
   that" is the same question in both places, and two components answering it
   would drift the first time the trace shape changed. */

function Stat({ label, value, title }: { label: string; value: string; title?: string }) {
  return (
    <div className="min-w-0" title={title}>
      <div className="text-[11px] font-medium uppercase tracking-[0.06em] text-faint">{label}</div>
      <div className="mt-0.5 truncate text-[13.5px] font-semibold leading-5 text-ink tabular-nums">
        {value}
      </div>
    </div>
  );
}

/** A value the model produced. `null` is a real answer — "I looked and could not
 *  find it" — so it is rendered as itself rather than as a blank cell. */
function OutputValue({ value }: { value: unknown }) {
  if (value === null || value === undefined) {
    return <span className="text-[13px] italic leading-5 text-faint">null — not found</span>;
  }
  if (typeof value === "boolean") {
    return <span className="text-[13px] leading-5 text-ink">{value ? "Yes" : "No"}</span>;
  }
  if (typeof value === "number") {
    return <span className="text-[13px] leading-5 text-ink tabular-nums">{value}</span>;
  }
  return (
    <span className="whitespace-pre-wrap text-[13px] leading-5 text-ink [overflow-wrap:anywhere]">
      {String(value)}
    </span>
  );
}

function json(value: unknown): string {
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function TraceStep({ step }: { step: TraceStepResponse }) {
  const [open, setOpen] = useState(false);
  const isTool = step.kind === "tool";

  return (
    <div className="border-b border-line last:border-b-0">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2.5 px-3 py-2 text-left transition-colors hover:bg-subtle"
      >
        <span className="w-5 flex-none text-right text-[11.5px] leading-4 text-faint tabular-nums">
          {step.step}
        </span>
        <span
          aria-hidden
          className={cn(
            "grid h-5 w-5 flex-none place-items-center rounded-md border text-[10px] font-semibold",
            isTool ? "border-line-2 bg-white text-ink-soft" : "border-transparent bg-subtle text-faint",
          )}
        >
          {isTool ? "ƒ" : "¶"}
        </span>
        <span
          className={cn(
            "min-w-0 flex-1 truncate text-[12.5px] leading-4",
            isTool ? "font-mono font-medium text-ink" : "text-muted",
          )}
        >
          {isTool ? step.name : String(step.result ?? "")}
        </span>
        {step.truncated && (
          <span className="flex-none text-[11px] leading-4 text-warn" title="This step was too large to keep in full — it was cut.">
            cut
          </span>
        )}
        {/* Only when there is a duration worth reading. `submit_result` returns
            in under a millisecond, and "0ms" beside it is noise dressed as a
            measurement. */}
        {!!step.ms && (
          <span className="flex-none text-[11.5px] leading-4 text-faint tabular-nums">
            {formatDuration(step.ms)}
          </span>
        )}
        <svg
          className={cn("h-3.5 w-3.5 flex-none text-faint transition-transform", open && "rotate-90")}
          viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden
        >
          <path d="m9 6 6 6-6 6" />
        </svg>
      </button>
      {open && (
        <div className="grid gap-2 border-t border-line bg-canvas px-3 py-2.5">
          {isTool && (
            <div>
              <div className="mb-1 text-[11px] font-medium uppercase tracking-[0.06em] text-faint">
                Arguments
              </div>
              <pre className="scroll-thin max-h-[220px] overflow-auto whitespace-pre-wrap rounded-md border border-line bg-white px-2.5 py-2 font-mono text-[11.5px] leading-4 text-ink-soft [overflow-wrap:anywhere]">
                {json(step.args)}
              </pre>
            </div>
          )}
          <div>
            <div className="mb-1 text-[11px] font-medium uppercase tracking-[0.06em] text-faint">
              {isTool ? "Result" : "Message"}
            </div>
            <pre className="scroll-thin max-h-[320px] overflow-auto whitespace-pre-wrap rounded-md border border-line bg-white px-2.5 py-2 font-mono text-[11.5px] leading-4 text-ink-soft [overflow-wrap:anywhere]">
              {json(step.result)}
            </pre>
          </div>
        </div>
      )}
    </div>
  );
}


/** One finished run, rendered whole. */
export function RunResult({
  run,
  config,
  catalog,
}: {
  run: TaskRunResponse;
  config: TaskConfig;
  /** Only to name the provider behind a missing-key failure — that is the one
   *  failure here a reader can fix in a click rather than a hunt. */
  catalog: CatalogResponse;
}) {
  const outcome = runOutcome(run);
  const output = run.output;
  const missingKey = run.error ? missingKeyProvider(run.error.message, catalog) : null;
  return (
    <div className="flex flex-col gap-3.5 rounded-xl border border-line-2 bg-canvas p-4">
      <div className="flex flex-wrap items-center gap-x-6 gap-y-3">
        <Badge variant={outcome.badge} dot={run.status === "completed"}>
          {outcome.label}
        </Badge>
        {/* Which definition ran. Without it, two runs either side of a publish
            are indistinguishable — which is the whole reason it is recorded. */}
        <Stat
          label="Version"
          value={run.task_version == null ? "draft" : `v${run.task_version}`}
          title={
            run.task_version == null
              ? "This ran the unpublished draft."
              : `This ran published version ${run.task_version}.`
          }
        />
        <Stat label="Took" value={formatDuration(run.duration_ms)} />
        <Stat
          label="Steps"
          value={
            run.attempts > 1
              ? `${run.steps_used} of ${run.max_steps} ×${run.attempts}`
              : `${run.steps_used} of ${run.max_steps}`
          }
          title={
            run.attempts > 1
              ? `One step is one model → tools → model round, not one tool call. This run took ${run.attempts} attempts: an attempt ended without a result and was re-prompted, and each attempt gets the full step budget again — so it used up to ${run.steps_used * run.attempts} rounds in total. Raise the step limit if this keeps happening.`
              : "One step is one model → tools → model round, not one tool call."
          }
        />
        <Stat
          label="Cost"
          value={
            run.billing_status === "unpriceable"
              ? "unpriced"
              : formatCost(run.provider_cost)
          }
          title={
            run.billing_status === "unpriceable"
              ? "This run happened, but its model is missing from the catalog — the cost is quarantined rather than reported as zero."
              : run.usage.map((u) => `${u.provider}/${u.model}`).join(", ")
          }
        />
        <Stat
          label="Tokens"
          value={run.usage
            .reduce((total, u) => total + u.input_tokens + u.output_tokens, 0)
            .toLocaleString()}
        />
      </div>

      {run.error && (
        <div className="rounded-lg border border-danger/25 bg-danger/[0.05] px-3.5 py-2.5">
          <div className="text-[11px] font-semibold uppercase tracking-[0.08em] text-danger">
            {run.error.type}
          </div>
          <div className="mt-1 text-[13px] leading-5 text-danger [overflow-wrap:anywhere]">
            {run.error.message}
          </div>
          {/* `configuration` covers a missing key, a deleted tool and a stale
              credential, so its help line lists all three. When the message has
              already said which one it is, the link is the more useful half. */}
          {missingKey ? (
            <Link
              href={`/byok?provider=${encodeURIComponent(missingKey.id)}`}
              className={cn(btn("secondary", "sm"), "mt-2.5")}
            >
              Add {missingKey.label} key
            </Link>
          ) : (
            outcome.help && (
              <div className="mt-1.5 text-[12.5px] leading-5 text-ink-soft">{outcome.help}</div>
            )
          )}
        </div>
      )}

      {output && (
        <div className="overflow-hidden rounded-lg border border-line-2 bg-white">
          <div className="border-b border-line px-3 py-2 text-[11px] font-medium uppercase tracking-[0.06em] text-faint">
            Result
          </div>
          <dl className="divide-y divide-line">
            {(config.output ?? []).map((field) => (
              <div key={field.name} className="grid grid-cols-[minmax(120px,180px)_minmax(0,1fr)] gap-3 px-3 py-2">
                <dt className="truncate font-mono text-[12.5px] leading-5 text-muted">
                  {field.name}
                </dt>
                <dd className="min-w-0">
                  <OutputValue value={output[field.name]} />
                </dd>
              </div>
            ))}
          </dl>
        </div>
      )}

      {run.trace.length > 0 && (
        <div className="overflow-hidden rounded-lg border border-line-2 bg-white">
          <div className="border-b border-line px-3 py-2 text-[11px] font-medium uppercase tracking-[0.06em] text-faint">
            What it did · {run.trace.length} {run.trace.length === 1 ? "step" : "steps"}
          </div>
          {/* Said once, always, rather than only when the task has a provider
              tool switched on: this list is also what an author reads to work
              out why a run took forty seconds and shows two steps, and the model
              provider does not tell us how many times it searched. */}
          <div className="border-b border-line px-3 py-2 text-[12px] leading-5 text-muted">
            Steps are what Talqing ran. Tools the model runs itself — a search, a
            code run — happen <em>inside</em> a step, on the provider&rsquo;s servers, and do
            not appear here.
          </div>
          {run.trace.map((step) => (
            <TraceStep key={step.step} step={step} />
          ))}
        </div>
      )}
    </div>
  );
}
