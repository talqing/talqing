"use client";

import { useState } from "react";
import type { EmailBatchResponse } from "@talqing/sdk";
import { BoxCheckbox, Button, Modal } from "@/app/components/ui";
import { api } from "@/lib/api";
import { apiErrorMessage } from "@/lib/apiError";

/* Redrafting discards a draft the tenant has already paid for and runs the task
 * again, so this dialog exists to make the three forms of it look different from
 * each other rather than like three items on one dropdown.
 *
 * `failed` on twelve rows and `all` on five thousand are one click apart in the
 * API. Here they say what each one discards, and `all` is the red button. */

export type RedraftKind = "failed" | "stale_version" | "all";

export function RedraftDialog({
  batch,
  kind,
  staleRows,
  onDone,
  onClose,
}: {
  batch: EmailBatchResponse;
  kind: RedraftKind;
  staleRows: number;
  onDone: (affected: number) => void;
  onClose: () => void;
}) {
  const [clearOverrides, setClearOverrides] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  // Everything but what has left or is leaving — and the rows no draft could
  // ever send, which a redraft refuses rather than pays for.
  const count =
    kind === "failed"
      ? batch.counts.draft_failed
      : kind === "stale_version"
        ? staleRows
        : batch.counts.total -
          batch.counts.sent -
          batch.counts.sending -
          batch.counts.drafting -
          batch.skips.unfillable;

  const copy = {
    failed: {
      title: `Draft ${count.toLocaleString()} failed ${count === 1 ? "row" : "rows"} again?`,
      lead: "Nothing sendable is discarded — not one of these rows has a draft that could go out. Each runs the task again, and is billed again. A row the task declined on purpose will most likely decline again.",
    },
    stale_version: {
      title: `Redraft ${count.toLocaleString()} ${count === 1 ? "row" : "rows"} on the old version?`,
      lead: `Every row an older version of ${batch.task_name ?? "the task"} wrote and has not sent is drafted again with v${batch.published_task_version ?? "?"}. What they say now is discarded — and a live send takes each new draft as it lands.`,
    },
    all: {
      title: `Redraft everything — ${count.toLocaleString()} ${count === 1 ? "row" : "rows"}?`,
      lead: "Every draft in this batch is discarded and written again from scratch — including drafts a live send is holding, which then go out as their new version. Rows already sent are left alone.",
    },
  }[kind];

  async function submit() {
    setBusy(true);
    setErr("");
    try {
      const res = await api.redraftEmailBatchRecipients(batch.id, {
        selection: kind,
        clear_overrides: clearOverrides,
      });
      onDone(res.affected);
    } catch (e: unknown) {
      setErr(apiErrorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      title={copy.title}
      onClose={() => !busy && onClose()}
      width="max-w-[540px]"
      footer={
        <>
          <Button variant="secondary" onClick={onClose} disabled={busy}>
            Back
          </Button>
          <Button
            variant={kind === "all" ? "danger" : "primary"}
            onClick={submit}
            disabled={busy || count === 0}
          >
            {busy ? "Queuing…" : `Redraft ${count.toLocaleString()}`}
          </Button>
        </>
      }
    >
      <div className="grid gap-3.5 text-[14px] leading-6 text-ink-soft">
        <p>{copy.lead}</p>
        {kind === "failed" && batch.draft_failures.length > 0 && (
          <ul className="grid gap-1 rounded-lg border border-line-2 bg-canvas px-3 py-2 text-[13px] leading-5">
            {batch.draft_failures.map((f) => (
              <li key={f.reason} className="flex gap-3">
                <span className="min-w-0 flex-1 truncate" title={f.reason}>
                  {f.reason}
                </span>
                <span className="font-mono tabular-nums text-ink">{f.count.toLocaleString()}</span>
              </li>
            ))}
          </ul>
        )}
        <p>
          <strong className="font-semibold text-ink">
            {count.toLocaleString()} more task {count === 1 ? "run" : "runs"}, billed to your
            provider keys.
          </strong>{" "}
          {batch.provider_cost != null && batch.counts.total > 0 && (
            <>
              This batch has cost ${batch.provider_cost.toFixed(4)} so far, so roughly $
              {((batch.provider_cost / Math.max(batch.counts.total, 1)) * count).toFixed(4)} more.
            </>
          )}
        </p>
        <label className="flex items-start gap-2.5">
          <BoxCheckbox
            checked={clearOverrides}
            onChange={setClearOverrides}
            ariaLabel="Also discard cells edited by hand"
          />
          <span className="text-[13px] leading-5">
            Also discard the cells someone edited by hand.{" "}
            <span className="text-muted">
              Off by default — a person&rsquo;s edit sits on top of the model&rsquo;s output and
              survives a redraft.
            </span>
          </span>
        </label>
        {err && (
          <p className="rounded-lg border border-danger/25 bg-white px-3 py-2 text-[13px] leading-5 text-danger">
            {err}
          </p>
        )}
      </div>
    </Modal>
  );
}
