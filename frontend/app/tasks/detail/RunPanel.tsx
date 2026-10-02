"use client";
import { useState } from "react";
import type { CatalogResponse, TaskConfig, TaskRunResponse } from "@talqing/sdk";
import { api } from "@/lib/api";
import { apiErrorList, apiErrorMessage } from "@/lib/apiError";
import { Button } from "@/app/components/ui";
import {
  SessionVarFields,
  missingVars,
  missingVarsSentence,
  suppliedVars,
} from "@/app/components/SessionVars";
import { RunResult } from "./RunResult";

/* The Run panel.
   This is where a tenant finds out their prompt does not reliably finish, so it
   has to answer three questions without a second click: what came back, what the
   model did on the way, and what it cost.

   It runs the DRAFT, saved first, exactly as the agent editor's test panel does
   — so `config` is the definition on screen, and which variables the run will
   be refused without is its answer. */

export function RunPanel({
  taskId,
  config,
  catalog,
  publishedVersion,
  beforeRun,
  onRan,
}: {
  taskId: string;
  /** The draft on screen. Its `vars` are the fields. */
  config: TaskConfig;
  catalog: CatalogResponse;
  /** What callers run meanwhile, named under the button; null when unpublished. */
  publishedVersion: number | null;
  /** Saves the draft; the run goes no further when this resolves false. */
  beforeRun: () => Promise<boolean>;
  onRan?: () => void;
}) {
  const vars = config.vars ?? [];
  const [values, setValues] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  // With the definition it ran, which is what its output is read against — the
  // draft may have moved on since.
  const [run, setRun] = useState<{ result: TaskRunResponse; config: TaskConfig } | null>(null);

  /* A required variable with no default has to be supplied, and the API refuses
     the run before it spends anything — so the button says so rather than
     letting the request find out. */
  const missing = missingVars(vars, values);

  async function go() {
    setBusy(true);
    setErr("");
    try {
      if (!(await beforeRun())) return;
      const result = await api.runTask(taskId, {
        vars: suppliedVars(vars, values),
        version: "draft",
      });
      setRun({ result, config });
      onRan?.();
    } catch (error) {
      // With every reason under the sentence: a draft that cannot run is
      // refused with a list.
      setErr([apiErrorMessage(error, "Could not run the task."), ...apiErrorList(error)].join("\n"));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex flex-col gap-4">
      {vars.length === 0 ? (
        <p className="text-[13px] leading-5 text-muted">
          This task takes no inputs, so every run is the same run. Add one under Inputs to give it
          something to work on.
        </p>
      ) : (
        <SessionVarFields
          declared={vars}
          values={values}
          onChange={setValues}
          idPrefix="run-var"
          className="sm:grid-cols-2"
        />
      )}

      <div className="flex flex-wrap items-center gap-x-3 gap-y-2 border-t border-line pt-3.5">
        <Button
          onClick={go}
          disabled={busy || missing.length > 0}
        >
          {busy ? "Running…" : "Run task"}
        </Button>
        {missing.length > 0 ? (
          <span className="text-[12.5px] leading-5 text-warn">{missingVarsSentence(missing)}</span>
        ) : (
          <span className="max-w-[70ch] text-[12.5px] leading-5 text-muted">
            {/* The two sentences this button owes the reader. */}
            Runs this draft, saving your edits first.{" "}
            {publishedVersion
              ? `Everything else stays on v${publishedVersion} until you publish.`
              : "Nothing else runs it until you publish."}{" "}
            It runs for real: your tools call your endpoints with your secrets, your MCP servers
            spend your credits, and the model spends your tokens.
          </span>
        )}
      </div>

      {err && (
        <div role="alert" className="whitespace-pre-line rounded-lg border border-danger/25 bg-danger/[0.05] px-3.5 py-2.5 text-[13px] leading-5 text-danger [overflow-wrap:anywhere]">
          {err}
        </div>
      )}

      {run && (
        <RunResult run={run.result} config={run.config} catalog={catalog} />
      )}
    </div>
  );
}
