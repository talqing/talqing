"use client";

import { useEffect, useRef, useState } from "react";
import type { EmailBatchResponse, EmailRecipientResponse } from "@talqing/sdk";
import { Badge, BoxCheckbox, SHEET, Tooltip, btn } from "@/app/components/ui";
import { cn } from "@/lib/cn";
import {
  ROW_STATUS_LABEL,
  ROW_STATUS_VARIANT,
  isEditableRow,
  isSendableRow,
  rowError,
} from "./shared";

/* The spreadsheet. This screen is the product.
 *
 * One column per name in the merged space — the file's columns and the task's
 * output fields, side by side, because after drafting nothing distinguishes
 * them and the operator should not have to care. The three mapped ones are
 * pinned left and marked, since they are the only ones that decide whether a
 * row can go.
 *
 * Every cell is editable in place. An edit is stored apart from what the model
 * wrote, so an edited cell is marked and its original is one hover away — the
 * point of the review gate is being able to act in it, not just look. */

type Props = {
  batch: EmailBatchResponse;
  rows: EmailRecipientResponse[];
  selected: Set<string>;
  onToggle: (id: string) => void;
  onToggleAll: () => void;
  onEdit: (row: EmailRecipientResponse, column: string, value: string) => Promise<void>;
  onTrace: (row: EmailRecipientResponse) => void;
  onPreview: (row: EmailRecipientResponse) => void;
  busy: boolean;
};

export function ReviewTable({
  batch,
  rows,
  selected,
  onToggle,
  onToggleAll,
  onEdit,
  onTrace,
  onPreview,
  busy,
}: Props) {
  const mapped = batch.field_map;
  const mappedNames = [mapped.to, mapped.subject, mapped.body];
  const rest = [...batch.input_columns, ...batch.output_columns].filter(
    (c) => !mappedNames.includes(c),
  );
  const anySent = rows.some((r) => r.sent_from);
  const anyVersion = rows.some((r) => r.task_version != null);
  // Named apart from the per-row `selectable` below on purpose: this is the set
  // the header checkbox acts on, and one shadowing the other is how a
  // select-all quietly starts meaning something else.
  const sendableHere = rows.filter(isSendableRow);
  const allSelected = sendableHere.length > 0 && sendableHere.every((r) => selected.has(r.id));

  return (
    /* **The header is deliberately not sticky, and a reader who reaches for it
       should know why.** `overflow-x` forces `overflow-y: auto` in CSS, so this
       wrapper is the sticky containing block whether we want it or not: an
       unbounded `top` pins the heading partway DOWN the table and hides a row
       under it, and bounding the height gives a nested scroll that the PAGE
       scrolls off the screen before the inner one ever engages. Both are worse
       than a heading that scrolls. What is sticky instead is the toolbar above
       — the tabs, the search and the bulk actions — which is the part you
       actually reach for with a long list on screen. */
    <div className={SHEET.wrap}>
      <table className={SHEET.table}>
        <thead className={SHEET.thead}>
          <tr className={SHEET.headRow}>
            <th className="w-9 py-2 pl-4 pr-0">
              <BoxCheckbox
                checked={allSelected}
                onChange={onToggleAll}
                ariaLabel={
                  allSelected
                    ? "Clear the selection"
                    : `Select the ${sendableHere.length} sendable drafts on this page`
                }
              />
            </th>
            <th className={SHEET.thNumber}>#</th>
            {mappedNames.map((column, i) => (
              <th
                key={`${column}-${i}`}
                className={SHEET.thPinned}
              >
                <span className="flex items-center gap-1.5">
                  {["To", "Subject", "Body"][i]}
                  <span className={SHEET.thName}>{column}</span>
                </span>
              </th>
            ))}
            <th className={cn(SHEET.th, "w-[110px]")}>Status</th>
            {/* Preview and Trace get a column each rather than sharing one.
                Sharing meant a flex row whose first item moved whenever the
                second was "Trace" on one row and "—" on the next, so the word
                "Preview" sat at a different x on every line. A table already
                aligns columns; this is just letting it. */}
            <th className="w-[84px]" />
            <th className="w-[64px]" />
            {anyVersion && <th className="w-[62px] px-3 py-2 font-medium">Wrote</th>}
            {rest.map((column) => (
              <th key={column} className={SHEET.thData}>
                {column}
              </th>
            ))}
            {anySent && <th className="min-w-[150px] px-3 py-2 font-medium">Sent from</th>}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const selectable = isSendableRow(row);
            const editable = isEditableRow(row.status);
            const error = rowError(row);
            return (
              <tr
                key={row.id}
                className={cn(
                  SHEET.row,
                  selected.has(row.id) && "bg-info/[0.05]",
                )}
              >
                <td className="py-1.5 pl-4 pr-0">
                  {selectable ? (
                    <BoxCheckbox
                      checked={selected.has(row.id)}
                      onChange={() => onToggle(row.id)}
                      ariaLabel={`Select row ${row.row_number}`}
                    />
                  ) : row.not_ready_reason && row.status === "draft" ? (
                    <Tooltip label={row.not_ready_reason}>
                      <span className="grid h-4 w-4 place-items-center rounded border border-line-strong text-[10px] text-faint">
                        !
                      </span>
                    </Tooltip>
                  ) : null}
                </td>
                <td className={SHEET.tdNumber}>
                  {row.row_number}
                </td>
                {mappedNames.map((column, i) => (
                  <Cell
                    key={`${column}-${i}`}
                    row={row}
                    column={column}
                    editable={editable}
                    busy={busy}
                    onEdit={onEdit}
                    emphasis
                  />
                ))}
                <td className="px-3 py-1.5">
                  {/* The reason is under the pointer, not under the badge: a
                      second line made every skipped row taller than the rest. */}
                  {error ? (
                    <Tooltip label={error}>
                      <Badge variant={ROW_STATUS_VARIANT[row.status]}>
                        {ROW_STATUS_LABEL[row.status]}
                      </Badge>
                    </Tooltip>
                  ) : (
                    <Badge variant={ROW_STATUS_VARIANT[row.status]}>
                      {ROW_STATUS_LABEL[row.status]}
                    </Badge>
                  )}
                </td>
                <td className="py-1.5 pl-1">
                  <button
                    type="button"
                    onClick={() => onPreview(row)}
                    className={btn("ghost", "sm")}
                    title="The email as it will actually be sent"
                  >
                    Preview
                  </button>
                </td>
                <td className="py-1.5 pr-1">
                  {row.task_run_id && batch.task_id ? (
                    <button
                      type="button"
                      onClick={() => onTrace(row)}
                      className={cn(btn("ghost", "sm"), "text-muted")}
                      title="What the task did on this row"
                    >
                      Trace
                    </button>
                  ) : (
                    <span className="px-2.5 text-placeholder">—</span>
                  )}
                </td>
                {anyVersion && (
                  <td className="px-3 py-1.5">
                    {row.task_version == null ? (
                      <span className="text-[12px] text-placeholder">—</span>
                    ) : (
                      <span
                        className={cn(
                          "font-mono text-[12px] tabular-nums",
                          batch.published_task_version != null &&
                            row.task_version < batch.published_task_version
                            ? "text-warn-ink"
                            : "text-muted",
                        )}
                        title={
                          batch.published_task_version != null &&
                          row.task_version < batch.published_task_version
                            ? `An older version wrote this. v${batch.published_task_version} is published now.`
                            : "Written by the version that is published now"
                        }
                      >
                        v{row.task_version}
                      </span>
                    )}
                  </td>
                )}
                {rest.map((column) => (
                  <Cell
                    key={column}
                    row={row}
                    column={column}
                    editable={editable}
                    busy={busy}
                    onEdit={onEdit}
                  />
                ))}
                {anySent && (
                  <td className="px-3 py-1.5 font-mono text-[12px] text-ink-soft">
                    {row.sent_from ?? <span className="text-placeholder">—</span>}
                  </td>
                )}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

/** One cell: a value from anywhere in the merged space, edited in place.
 *
 * Committing on blur rather than on every keystroke, because each save is a
 * request. Escape restores what was there, which is the only way out of a
 * half-typed edit that does not write it. */
function Cell({
  row,
  column,
  editable,
  busy,
  onEdit,
  emphasis,
}: {
  row: EmailRecipientResponse;
  column: string;
  editable: boolean;
  busy: boolean;
  onEdit: (row: EmailRecipientResponse, column: string, value: string) => Promise<void>;
  emphasis?: boolean;
}) {
  const stored = row.columns[column];
  const text = stored == null ? "" : String(stored);
  const edited = column in row.overrides;
  const original = row.output?.[column] ?? row.input[column];
  const [draft, setDraft] = useState(text);
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLTextAreaElement>(null);

  useEffect(() => setDraft(text), [text]);
  useEffect(() => {
    if (open) ref.current?.focus();
  }, [open]);

  if (!editable) {
    return (
      <td
        className={emphasis ? SHEET.tdPinned : SHEET.tdData}
        title={text}
      >
        {text || <span className="text-placeholder">—</span>}
      </td>
    );
  }

  return (
    <td
      className={cn("px-1.5 py-1", edited && "relative")}
    >
      {open ? (
        <textarea
          ref={ref}
          value={draft}
          rows={4}
          disabled={busy}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Escape") {
              setDraft(text);
              setOpen(false);
            }
          }}
          onBlur={async () => {
            setOpen(false);
            if (draft === text) return;
            await onEdit(row, column, draft);
          }}
          className="min-h-[80px] w-full min-w-[220px] resize-y rounded-md border border-ink/40 bg-white px-2 py-1.5 text-[12.5px] leading-5 text-ink outline-none"
          aria-label={`${column} on row ${row.row_number}`}
        />
      ) : (
        <button
          type="button"
          onClick={() => setOpen(true)}
          title={
            edited && original != null
              ? `Edited. The task wrote: ${String(original)}`
              : text || "Empty — click to type one in"
          }
          className={cn(
            "block w-full max-w-[240px] truncate rounded-md px-1.5 py-1 text-left transition-colors hover:bg-subtle",
            emphasis ? "font-medium text-ink" : "text-ink-soft",
            !text && "font-normal italic text-placeholder",
          )}
        >
          {edited && (
            <span
              aria-hidden
              className="mr-1 inline-block h-1.5 w-1.5 rounded-full bg-warn align-middle"
            />
          )}
          {text || "empty"}
        </button>
      )}
    </td>
  );
}
