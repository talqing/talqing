"use client";

import { useState } from "react";
import type { EmailBatchResponse, IntegrationResponse, TaskResponse } from "@talqing/sdk";
import { Button, Modal } from "@/app/components/ui";
import { api } from "@/lib/api";
import { apiErrorMessage } from "@/lib/apiError";
import {
  PolicyFields,
  type PolicyDraft,
  draftFromBatch,
  policyProblems,
  toPatchRequest,
} from "./PolicyFields";

/** The same policy form as create, minus the list and the field map.
 *
 *  The sending half here is the DEFAULT the next send inherits. A send already
 *  created carries its own copy of all four values and is re-steered from the
 *  Sends section — which the note below says, because "I changed the pace and
 *  nothing happened" is otherwise the obvious reading. */
export function EditPolicy({
  batch,
  tasks,
  accounts,
  identityEditable,
  onSaved,
  onClose,
}: {
  batch: EmailBatchResponse;
  tasks: TaskResponse[];
  accounts: IntegrationResponse[];
  identityEditable: boolean;
  onSaved: (batch: EmailBatchResponse) => void;
  onClose: () => void;
}) {
  const [draft, setDraft] = useState<PolicyDraft>(() => draftFromBatch(batch));
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState("");
  const startEditable = batch.status === "scheduled";
  const problems = policyProblems(draft);

  async function save() {
    setSaving(true);
    setErr("");
    try {
      onSaved(
        await api.patchEmailBatch(
          batch.id,
          toPatchRequest(draft, { startEditable, identityEditable }),
        ),
      );
    } catch (e: unknown) {
      setErr(apiErrorMessage(e));
    } finally {
      setSaving(false);
    }
  }

  return (
    <Modal
      title="Edit this batch"
      sub="Everything here is policy: the next drafting pass picks it up, and rows already drafted are left alone."
      onClose={() => !saving && onClose()}
      width="max-w-[760px]"
      footer={
        <>
          <Button variant="secondary" onClick={onClose} disabled={saving}>
            Cancel
          </Button>
          <Button onClick={save} disabled={saving || problems.length > 0}>
            {saving ? "Saving…" : "Save"}
          </Button>
        </>
      }
    >
      <div className="grid gap-4">
        {!identityEditable && (
          <p className="rounded-lg border border-line-2 bg-canvas px-3 py-2 text-[13px] leading-5 text-muted">
            This batch has sent emails, so its task, account, sender and body format are fixed —
            changing them would make the rows already delivered no longer describe what they were
            sent as. Pacing is still yours to change.
          </p>
        )}
        <p className="rounded-lg border border-line-2 bg-canvas px-3 py-2 text-[13px] leading-5 text-muted">
          The sending settings here are what the <strong className="font-semibold">next</strong>{" "}
          send starts from. A send already scheduled or going out carries its own copy — change
          that one from the Sends list.
        </p>
        <PolicyFields
          draft={draft}
          onChange={setDraft}
          tasks={tasks}
          accounts={accounts}
          startEditable={startEditable}
          identityEditable={identityEditable}
          creating={false}
          savedCap={{ cap: batch.send_daily_cap, today: batch.send_daily_cap_today }}
        />
        {(err || problems.length > 0) && (
          <p className="rounded-lg border border-danger/25 bg-white px-3 py-2 text-[13px] leading-5 text-danger">
            {err || problems[0]}
          </p>
        )}
      </div>
    </Modal>
  );
}
