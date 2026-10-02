"use client";

import { useState } from "react";
import { Button, Modal, useToast } from "@/app/components/ui";
import { apiErrorMessage } from "@/lib/apiError";

/** The confirmation before a call, email or WhatsApp batch is deleted for good.
 *  Says what goes and what stays, and offers the export while it still exists. */
export function DeleteBatchModal({
  children,
  run,
  onExport,
  onDeleted,
  onClose,
}: {
  /** What is deleted and what is kept, in a sentence or two. */
  children: React.ReactNode;
  run: () => Promise<unknown>;
  onExport?: () => void;
  onDeleted: () => void;
  onClose: () => void;
}) {
  const toast = useToast();
  const [busy, setBusy] = useState(false);

  async function remove() {
    setBusy(true);
    try {
      await run();
      toast({ msg: "Batch deleted.", kind: "ok" });
      onDeleted();
    } catch (e: unknown) {
      toast({ msg: apiErrorMessage(e, "Could not delete the batch."), kind: "err" });
      setBusy(false);
    }
  }

  return (
    <Modal
      title="Delete this batch?"
      onClose={() => !busy && onClose()}
      width="max-w-[480px]"
      footer={
        <>
          {onExport && (
            <Button variant="secondary" onClick={onExport} disabled={busy} className="mr-auto">
              Export CSV
            </Button>
          )}
          <Button variant="secondary" onClick={onClose} disabled={busy}>
            Keep it
          </Button>
          <Button variant="danger" onClick={remove} disabled={busy}>
            {busy ? "Deleting…" : "Delete batch"}
          </Button>
        </>
      }
    >
      <div className="grid gap-2.5 pb-1 text-[14px] leading-6 text-ink-soft">{children}</div>
    </Modal>
  );
}
