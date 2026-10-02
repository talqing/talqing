import type { BadgeVariant } from "@/app/components/ui";
import type { TaskErrorType, TaskRunResponse, TaskRunSummary } from "@talqing/sdk";

/* What a failed run means, in one place.

   The eight error types are a closed vocabulary and the whole point of them is
   that each sends the reader somewhere different: `step_limit` to the Limits
   section, `no_output` to the prompt, `configuration` to BYOK or a deleted
   tool. Two screens spelling that out by hand is how one of them ends up
   telling somebody to rewrite a prompt that was never the problem. */
export const RUN_OUTCOME: Record<TaskErrorType, { label: string; help: string }> = {
  missing_vars: {
    label: "Missing input",
    help: "A required input had no value and no default. Caught before anything ran, so it cost nothing.",
  },
  no_output: {
    label: "No result",
    help: "The model finished without calling submit_result, twice — it was already re-prompted once. Say in the prompt that calling it is how the task finishes.",
  },
  step_limit: {
    label: "Out of steps",
    help: "Every round was used and no result came back. Raise the step limit in Limits, or narrow what the task does.",
  },
  timeout: {
    label: "Timed out",
    help: "The run passed its time budget. Raise the timeout in Limits, or cut the work.",
  },
  provider_error: {
    label: "Provider failed",
    help: "The model or an MCP server failed. Not something this task's config can fix — try it again.",
  },
  configuration: {
    label: "Not configured",
    help: "Something this task needs is missing: a provider key under BYOK, a tool that was deleted or unpublished, an integration whose credential no longer resolves.",
  },
  platform: {
    label: "Platform error",
    help: "Ours. The run failed for a reason that is not about this task.",
  },
  canceled: {
    label: "Interrupted",
    help: "A deploy or restart stopped this run before it finished. Nothing is wrong with the task — a batch row is drafted again automatically, and a run you started by hand can be run again.",
  },
};

export type Outcome = { label: string; help: string; badge: BadgeVariant };

/** How one run went, as a badge. Shared by the list page and the editor. */
export function runOutcome(run: TaskRunResponse | TaskRunSummary | null | undefined): Outcome {
  if (!run) return { label: "Never run", help: "", badge: "default" };
  if (run.status === "running") {
    return { label: "Running", help: "This run is still going.", badge: "info" };
  }
  if (run.status === "completed") {
    return { label: "Completed", help: "The task produced its result.", badge: "live" };
  }
  const type = run.error?.type;
  if (!type) return { label: "Failed", help: "", badge: "danger" };
  const outcome = RUN_OUTCOME[type];
  return { label: outcome.label, help: outcome.help, badge: "danger" };
}

/** `$0.0043` — a task run costs cents, so the usual two decimals would read
 *  `$0.00` for every run anyone actually does. */
export function formatCost(cost: number | null | undefined): string {
  if (cost === null || cost === undefined) return "—";
  if (cost === 0) return "$0";
  return cost < 0.01 ? `$${cost.toFixed(4)}` : `$${cost.toFixed(2)}`;
}

/** `1.4s` / `320ms` — a run is seconds, not minutes. */
export function formatDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return "—";
  return ms < 1000 ? `${ms}ms` : `${(ms / 1000).toFixed(1)}s`;
}
