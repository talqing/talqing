"use client";

import { useState } from "react";
import { Button, Modal, Pager, UnsavedChangesModal, useToast } from "@/app/components/ui";
import { apiErrorList, apiErrorMessage } from "@/lib/apiError";
import { cn } from "@/lib/cn";

/* Appending to an outbound batch — calls, email or WhatsApp — around each
 * channel's own list editor.
 *
 * What it adds is the two things the editor cannot know: what will happen to
 * these rows once they are in (one line, decided by the page from the batch's
 * state), and what the API said about them. A duplicate is the normal case and
 * only counted in the toast; a row that went in but cannot go out — a bad phone
 * cell, a blank address — needs the operator, so the dialog stays open and
 * names it. */

export type AddOutcome = {
  added: number;
  /** `row_number` is the row's position in this upload, 1-based. A row the API
   *  stored all the same carries its `recipient_id`; a dropped duplicate does not. */
  skipped: { row_number: number; reason: string; recipient_id?: string | null }[];
};

const RESULT_PAGE_SIZE = 8;

export function AddRecipientsModal({
  batchName,
  count,
  blocked,
  note,
  fixHint,
  submit,
  onRejected,
  onAdded,
  onClose,
  children,
}: {
  batchName: string;
  /** Rows ready to go. */
  count: number;
  /** The editor is showing a problem that has to be fixed first. */
  blocked: boolean;
  /** What happens to the rows once they are in. */
  note: { text: string; tone?: "warn" };
  /** Under the rows that went in but cannot go out: where to fix them. */
  fixHint?: string;
  submit: () => Promise<AddOutcome>;
  /** The API's per-row errors, for an editor that can pin them on its rows.
   *  Returns the ones it could not place. */
  onRejected?: (errors: string[]) => string[];
  onAdded: () => void;
  onClose: () => void;
  children: React.ReactNode;
}) {
  const toast = useToast();
  const [saving, setSaving] = useState(false);
  const [errors, setErrors] = useState<string[]>([]);
  const [discarding, setDiscarding] = useState(false);
  const [result, setResult] = useState<{ added: number; duplicates: number; held: AddOutcome["skipped"] } | null>(null);
  const [page, setPage] = useState(0);

  async function add() {
    setSaving(true);
    setErrors([]);
    try {
      const outcome = await submit();
      const held = outcome.skipped.filter((s) => s.recipient_id);
      const duplicates = outcome.skipped.length - held.length;
      onAdded();
      if (held.length) {
        setResult({ added: outcome.added, duplicates, held });
        return;
      }
      toast({
        msg: `Added ${outcome.added.toLocaleString()}${
          duplicates ? ` · ${duplicates.toLocaleString()} already in this batch` : ""
        }`,
        kind: "ok",
      });
      onClose();
    } catch (e: unknown) {
      const listed = apiErrorList(e);
      const left = listed.length && onRejected ? onRejected(listed) : listed;
      setErrors(left.length ? left : listed.length ? [] : [apiErrorMessage(e)]);
    } finally {
      setSaving(false);
    }
  }

  if (result) {
    const shown = result.held.slice(page * RESULT_PAGE_SIZE, (page + 1) * RESULT_PAGE_SIZE);
    return (
      <Modal
        title={`Added ${result.added.toLocaleString()}`}
        sub={[
          `${result.held.length.toLocaleString()} of them ${result.held.length === 1 ? "needs" : "need"} a fix before ${result.held.length === 1 ? "it goes" : "they go"} out.`,
          result.duplicates
            ? `${result.duplicates.toLocaleString()} ${result.duplicates === 1 ? "was" : "were"} already in this batch.`
            : "",
        ]
          .filter(Boolean)
          .join(" ")}
        onClose={onClose}
        width="max-w-[560px]"
        footer={
          <Button onClick={onClose} data-dialog-autofocus>
            Done
          </Button>
        }
      >
        <ul className="divide-y divide-line overflow-hidden rounded-xl border border-line-2 bg-white">
          {shown.map((s) => (
            <li key={s.row_number} className="flex items-baseline gap-3 px-3.5 py-2.5 text-[13px] leading-5">
              <span className="w-14 flex-none font-mono text-[12px] text-faint tabular-nums">
                Row {s.row_number}
              </span>
              <span className="min-w-0 text-ink-soft">{s.reason}</span>
            </li>
          ))}
        </ul>
        {result.held.length > RESULT_PAGE_SIZE && (
          <Pager
            page={page}
            pageSize={RESULT_PAGE_SIZE}
            total={result.held.length}
            onPage={setPage}
            className="mt-2.5"
          />
        )}
        {fixHint && <p className="mt-3 text-[13px] leading-5 text-muted">{fixHint}</p>}
      </Modal>
    );
  }

  return (
    <>
      <Modal
        title={`Add to ${batchName}`}
        onClose={() => (saving ? undefined : count > 0 ? setDiscarding(true) : onClose())}
        width="max-w-[900px]"
        footer={
          <>
            <Button variant="secondary" onClick={() => (count > 0 ? setDiscarding(true) : onClose())} disabled={saving}>
              Cancel
            </Button>
            <Button onClick={add} disabled={saving || count === 0 || blocked}>
              {saving
                ? "Adding…"
                : count === 0
                  ? "Add recipients"
                  : `Add ${count.toLocaleString()} ${count === 1 ? "recipient" : "recipients"}`}
            </Button>
          </>
        }
      >
        {children}
        <p
          className={cn(
            "mt-4 flex items-start gap-2 rounded-lg border px-3 py-2 text-[13px] leading-5",
            note.tone === "warn"
              ? "border-warn/30 bg-warn/[0.05] text-warn"
              : "border-line-2 bg-canvas text-ink-soft",
          )}
        >
          <svg className="mt-[3px] h-3.5 w-3.5 flex-none" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
            <circle cx="8" cy="8" r="6.25" />
            <path d="M8 7.25v4M8 4.9v.1" />
          </svg>
          {note.text}
        </p>
        {errors.length > 0 && (
          <ul className="mt-2.5 grid gap-1 rounded-lg border border-danger/25 bg-white px-3 py-2 text-[13px] leading-5 text-danger">
            {errors.map((message) => (
              <li key={message}>{message}</li>
            ))}
          </ul>
        )}
      </Modal>
      {discarding && (
        <UnsavedChangesModal
          sub="The rows you've added here aren't in the batch yet."
          onKeepEditing={() => setDiscarding(false)}
          onDiscard={onClose}
        />
      )}
    </>
  );
}
