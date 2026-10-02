"use client";

import { useEffect, useState } from "react";
import type { CatalogResponse, TaskConfig, TaskRunResponse } from "@talqing/sdk";
import { ListSkeleton, Modal } from "@/app/components/ui";
import { api } from "@/lib/api";
import { apiErrorMessage } from "@/lib/apiError";
import { RunResult } from "@/app/tasks/detail/RunResult";

/* Why this row reads the way it does.
 *
 * The same view the task editor's Run panel shows, because it is the same
 * question — and an operator looking at a draft that is subtly wrong wants the
 * tool calls behind it, not a second, thinner rendering of them. */

export function TraceModal({
  taskId,
  runId,
  rowNumber,
  onClose,
}: {
  taskId: string;
  runId: string;
  rowNumber: number;
  onClose: () => void;
}) {
  const [run, setRun] = useState<TaskRunResponse | null>(null);
  const [config, setConfig] = useState<TaskConfig | null>(null);
  /* Only so a row that failed for want of a provider key can offer to add it —
     the catalog is what turns the provider the error names into a link. */
  const [catalog, setCatalog] = useState<CatalogResponse | null>(null);
  const [err, setErr] = useState("");

  /* One flow, because the second fetch depends on the first: the field list has
     to come from the definition this run actually used, which is what
     `task_version` names. Reading the draft instead would render a column for
     an output field that was added after the row was drafted, and show it
     empty — a value the model was never asked for, presented as one it failed
     to find. A draft run has no version, and then the draft IS what ran. */
  useEffect(() => {
    let stale = false;
    Promise.all([api.getTaskRun(taskId, runId), api.catalog()])
      .then(async ([r, c]) => {
        if (stale) return;
        setRun(r);
        setCatalog(c);
        const cfg =
          r.task_version == null
            ? (await api.getTask(taskId)).config
            : (await api.getTaskVersion(taskId, r.task_version)).config;
        if (!stale) setConfig(cfg);
      })
      .catch((e) => !stale && setErr(apiErrorMessage(e, "Could not load that run.")));
    return () => {
      stale = true;
    };
  }, [taskId, runId]);

  return (
    <Modal title={`Row ${rowNumber}`} sub="What the task did on this row." onClose={onClose} width="max-w-[720px]">
      {err ? (
        <p className="rounded-lg border border-danger/25 bg-danger/[0.05] px-3 py-2 text-[13px] leading-5 text-danger">
          {err}
        </p>
      ) : run && config && catalog ? (
        <RunResult run={run} config={config} catalog={catalog} />
      ) : (
        <ListSkeleton rows={4} />
      )}
    </Modal>
  );
}
