"use client";

import { useMemo, useRef, useState } from "react";
import { Button, Input } from "@/app/components/ui";
import { cn } from "@/lib/cn";
import {
  MAX_RECIPIENTS_PER_UPLOAD,
  PHONE_COLUMN,
  type RecipientRow,
  checkRow,
  keysOf,
  parseRecipientCsv,
  renumber,
} from "./recipients";

const PAGE_SIZE = 12;

/** What a working file looks like, for anyone who has never made one. */
const SAMPLE_CSV = `${PHONE_COLUMN},first_name,plan\n+919876543210,Asha,Gold\n+919812345678,Ravi,Silver\n`;

const Icon = {
  plus: (
    <svg className="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" aria-hidden>
      <path d="M12 5v14M5 12h14" />
    </svg>
  ),
  upload: (
    <svg className="h-[18px] w-[18px]" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="M12 16V4m0 0L7.5 8.5M12 4l4.5 4.5" />
      <path d="M4 15v2.5A2.5 2.5 0 0 0 6.5 20h11a2.5 2.5 0 0 0 2.5-2.5V15" />
    </svg>
  ),
};

/**
 * Where the recipient list comes from: typed in one person at a time, or a CSV.
 *
 * Both are on screen at once rather than behind a toggle — they are not two
 * modes of one thing, they are the two ways a list arrives, and a five-person
 * list should never require making a file.
 *
 * The whole list is rendered — paginated, one column per `userdata` key — before
 * anything is sent, because the thing an operator most needs to see is not that
 * the file parsed but *what each call will actually receive*. A batch that reads
 * "Hi {{userdata.first_name}}" is one empty column away from ringing four
 * thousand people and saying "Hi ,".
 */
export function RecipientEditor({
  rows,
  onChange,
}: {
  rows: RecipientRow[];
  onChange: (rows: RecipientRow[]) => void;
}) {
  const [page, setPage] = useState(0);
  const [fileError, setFileError] = useState("");
  const [fileName, setFileName] = useState("");
  const [dropping, setDropping] = useState(false);
  /* Fields the operator named here and no row has filled in yet. Once someone on
     the list carries one it is a column of the list itself, and comes back from
     `keysOf` instead — which is why it can then no longer be removed from here:
     a × that quietly stripped a column off ten thousand rows is not a × anyone
     should be able to press by accident. */
  const [newFields, setNewFields] = useState<string[]>([]);
  const fileInput = useRef<HTMLInputElement>(null);

  const keys = useMemo(() => keysOf(rows), [rows]);
  const columns = useMemo(
    () => [...keys, ...newFields.filter((f) => !keys.includes(f))],
    [keys, newFields],
  );
  const badRows = useMemo(() => rows.filter((r) => r.problems.length), [rows]);
  const pages = Math.max(1, Math.ceil(rows.length / PAGE_SIZE));
  const current = Math.min(page, pages - 1);
  const visible = rows.slice(current * PAGE_SIZE, current * PAGE_SIZE + PAGE_SIZE);

  async function onFile(file: File | undefined) {
    if (!file) return;
    setFileError("");
    const parsed = parseRecipientCsv(await file.text());
    if (parsed.error) {
      setFileError(parsed.error);
      setFileName("");
      return;
    }
    setFileName(file.name);
    // Replace rather than append: uploading a second file almost always means
    // "I picked the wrong one", and a silent merge of two lists is the kind of
    // mistake that only shows up as a phone bill.
    onChange(renumber(parsed.rows));
    setPage(0);
  }

  function updateRow(index: number, to: string) {
    const next = rows.map((row, i) => (i === index ? checkRow({ ...row, to, problems: [] }) : row));
    onChange(renumber(next));
  }

  return (
    /* flex-col rather than grid: a grid track is min-content-sized, so one wide
       table would push the whole panel past the dialog instead of scrolling
       inside it. */
    <div className="flex flex-col gap-3">
      <div className="rounded-xl border border-line-2 bg-white p-4">
        <Composer
          columns={columns}
          full={rows.length >= MAX_RECIPIENTS_PER_UPLOAD}
          canRemoveField={(field) => !keys.includes(field)}
          onRemoveField={(field) => setNewFields((f) => f.filter((name) => name !== field))}
          onAddField={(field) => setNewFields((f) => [...f, field])}
          onAdd={(row) => {
            onChange(renumber([...rows, row]));
            setPage(Math.floor(rows.length / PAGE_SIZE));
          }}
        />

        <div className="my-4 flex items-center gap-3" aria-hidden>
          <span className="h-px flex-1 bg-line" />
          <span className="text-[11.5px] font-medium uppercase tracking-[0.08em] text-faint">or</span>
          <span className="h-px flex-1 bg-line" />
        </div>

        <button
          type="button"
          onClick={() => fileInput.current?.click()}
          onDragOver={(e) => e.preventDefault()}
          onDragEnter={() => setDropping(true)}
          // Dragging across the icon or the caption fires dragleave on the
          // button too, so only a pointer that has actually left it counts.
          onDragLeave={(e) => {
            if (!e.currentTarget.contains(e.relatedTarget as Node | null)) setDropping(false);
          }}
          onDrop={(e) => {
            e.preventDefault();
            setDropping(false);
            void onFile(e.dataTransfer.files?.[0]);
          }}
          className={cn(
            "flex w-full items-center gap-3.5 rounded-xl border border-dashed bg-white px-4 py-3.5 text-left transition-colors",
            dropping ? "border-ink bg-ink/[0.03]" : "border-line-strong hover:border-ink/35 hover:bg-canvas",
          )}
        >
          <span className="grid h-9 w-9 flex-none place-items-center rounded-[10px] border border-line-2 bg-white text-ink-soft">
            {Icon.upload}
          </span>
          <span className="min-w-0">
            <span className="block text-[14px] font-semibold leading-5 text-ink">
              {fileName ? `${fileName} — choose another` : "Upload a CSV"}
            </span>
            <span className="block text-[12.5px] leading-[18px] text-muted">
              {/* A file replaces the list rather than joining it, which is right
                  — a second upload almost always means "I picked the wrong
                  one" — but now that both ways of building a list are on screen
                  together, it has to say so before it happens. */}
              {rows.length > 0
                ? `Replaces the ${rows.length.toLocaleString()} already on the list. `
                : "Drop it here, or click to pick one. "}
              One <code className="font-mono text-[12px] text-ink-soft">{PHONE_COLUMN}</code>{" "}
              column, plus any columns the agent should know about.
            </span>
          </span>
        </button>
        <input
          ref={fileInput}
          type="file"
          // Spreadsheet apps save CSVs under several types; with only `text/csv`
          // the picker greys out a perfectly good file.
          accept=".csv,.txt,text/csv,text/plain,application/vnd.ms-excel"
          className="sr-only"
          onChange={(e) => {
            void onFile(e.target.files?.[0]);
            e.target.value = "";
          }}
        />
        <button
          type="button"
          onClick={() => {
            const link = document.createElement("a");
            link.href = `data:text/csv;charset=utf-8,${encodeURIComponent(SAMPLE_CSV)}`;
            link.download = "recipients-sample.csv";
            link.click();
          }}
          className="mt-2 text-[12.5px] font-medium text-muted underline-offset-2 hover:text-ink hover:underline"
        >
          Download a sample file
        </button>
      </div>

      {fileError && (
        <p className="rounded-lg border border-danger/25 bg-white px-3 py-2 text-[13px] leading-5 text-danger">
          {fileError}
        </p>
      )}

      {rows.length === 0 ? (
        <p className="text-[13px] leading-5 text-muted">
          Nobody on the list yet. Everyone you add shows up below, exactly as their call will
          receive it, before anything is dialled.
        </p>
      ) : (
        <div className="flex flex-col gap-2.5">
          {badRows.length > 0 && (
            <div className="flex flex-wrap items-center gap-2 rounded-lg border border-danger/25 bg-white px-3 py-2 text-[13px] leading-5 text-danger">
              <span>
                {badRows.length} row{badRows.length === 1 ? "" : "s"} cannot be called. Fix or
                remove them.
              </span>
              <button
                type="button"
                onClick={() => {
                  const at = rows.findIndex((r) => r.problems.length);
                  if (at >= 0) setPage(Math.floor(at / PAGE_SIZE));
                }}
                className="font-medium underline underline-offset-2"
              >
                Go to row {badRows[0].rowNumber}
              </button>
            </div>
          )}

          <div className="overflow-hidden rounded-xl border border-line-2 bg-white">
            <div className="flex flex-wrap items-center gap-x-2 gap-y-1 border-b border-line px-3.5 py-2.5">
              <span className="text-[13px] font-semibold leading-5 text-ink tabular-nums">
                {rows.length.toLocaleString()} {rows.length === 1 ? "person" : "people"}
              </span>
              {columns.length > 0 && (
                <span className="text-[12.5px] leading-5 text-faint">
                  · {columns.length} field{columns.length === 1 ? "" : "s"} per call
                </span>
              )}
              <button
                type="button"
                onClick={() => onChange([])}
                className="ml-auto text-[12.5px] font-medium text-muted underline-offset-2 hover:text-danger hover:underline"
              >
                Clear the list
              </button>
            </div>

            <div className="overflow-x-auto">
              <table className="w-full min-w-[520px] border-collapse text-[13px]">
                <thead>
                  <tr className="border-b border-line text-left text-[12px] font-medium text-muted">
                    <th className="w-11 px-3 py-2 font-medium">#</th>
                    <th className="w-[190px] px-3 py-2 font-medium">Phone number</th>
                    {columns.map((key) => (
                      <th key={key} className="min-w-[120px] px-3 py-2 font-mono font-medium text-ink-soft">
                        {key}
                      </th>
                    ))}
                    <th className="w-10 px-3 py-2" />
                  </tr>
                </thead>
                <tbody>
                  {visible.map((row) => {
                    const index = rows.indexOf(row);
                    const bad = row.problems.length > 0;
                    return (
                      <tr
                        key={row.rowNumber}
                        className={cn(
                          "group border-b border-line last:border-0",
                          bad ? "bg-danger/[0.03]" : "hover:bg-canvas",
                        )}
                      >
                        <td className="px-3 py-1.5 font-mono text-[12px] text-faint tabular-nums">
                          {row.rowNumber}
                        </td>
                        <td className="px-3 py-1.5">
                          <input
                            value={row.to}
                            onChange={(e) => updateRow(index, e.target.value)}
                            aria-label={`Phone number on row ${row.rowNumber}`}
                            className={cn(
                              "w-full rounded-md border border-transparent bg-transparent px-1.5 py-1 font-mono text-[12.5px] text-ink outline-none transition-colors hover:border-line-2 focus:border-ink focus:bg-white",
                              bad && "text-danger",
                            )}
                          />
                          {bad && (
                            <span className="block px-1.5 pt-0.5 text-[12px] leading-4 text-danger">
                              {row.problems.join(" · ")}
                            </span>
                          )}
                        </td>
                        {columns.map((key) => (
                          <td
                            key={key}
                            className="max-w-[220px] truncate px-3 py-1.5 text-ink-soft"
                            title={row.userdata[key] ?? ""}
                          >
                            {row.userdata[key] ?? <span className="text-placeholder">—</span>}
                          </td>
                        ))}
                        <td className="px-3 py-1.5 text-right">
                          <button
                            type="button"
                            onClick={() =>
                              onChange(renumber(rows.filter((_, i) => i !== index)))
                            }
                            aria-label={`Remove row ${row.rowNumber}`}
                            className="grid h-6 w-6 place-items-center rounded-md text-placeholder opacity-0 transition-colors hover:bg-danger/[0.06] hover:text-danger focus-visible:opacity-100 group-hover:opacity-100"
                          >
                            <svg className="h-3.5 w-3.5" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" aria-hidden>
                              <path d="m3 3 6 6M9 3 3 9" />
                            </svg>
                          </button>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>

            {pages > 1 && (
              <div className="flex items-center justify-between border-t border-line px-3.5 py-2 text-[12.5px] text-muted">
                <span className="tabular-nums">
                  Rows {current * PAGE_SIZE + 1}–{Math.min(rows.length, (current + 1) * PAGE_SIZE)}{" "}
                  of {rows.length.toLocaleString()}
                </span>
                <div className="flex items-center gap-1.5">
                  <Button
                    variant="secondary"
                    size="sm"
                    disabled={current === 0}
                    onClick={() => setPage(current - 1)}
                  >
                    Previous
                  </Button>
                  <span className="px-1 tabular-nums">
                    {current + 1} / {pages}
                  </span>
                  <Button
                    variant="secondary"
                    size="sm"
                    disabled={current >= pages - 1}
                    onClick={() => setPage(current + 1)}
                  >
                    Next
                  </Button>
                </div>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

/**
 * One person, typed.
 *
 * The fields are the list's own columns, so the sixth person added to a
 * five-person batch is filled in under the same headings rather than by naming
 * them again — and a field named here before anyone has it becomes a column of
 * the list the moment the first person carries a value for it.
 */
function Composer({
  columns,
  full,
  canRemoveField,
  onAddField,
  onRemoveField,
  onAdd,
}: {
  columns: string[];
  /** The list has as many people as one upload may carry. */
  full: boolean;
  canRemoveField: (field: string) => boolean;
  onAddField: (field: string) => void;
  onRemoveField: (field: string) => void;
  onAdd: (row: RecipientRow) => void;
}) {
  const [to, setTo] = useState("");
  const [values, setValues] = useState<Record<string, string>>({});
  const [naming, setNaming] = useState("");
  const [addingField, setAddingField] = useState(false);

  function add() {
    const userdata = Object.fromEntries(
      columns
        .map((key) => [key, (values[key] ?? "").trim()] as const)
        .filter(([, value]) => value !== ""),
    );
    // rowNumber is provisional — the caller renumbers the whole list.
    onAdd(checkRow({ rowNumber: 0, to: to.trim(), userdata, problems: [] }));
    setTo("");
    setValues({});
  }

  function commitField() {
    const name = naming.trim();
    if (name && !columns.includes(name)) onAddField(name);
    setNaming("");
    setAddingField(false);
  }

  const onEnter = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && to.trim() && !full) {
      e.preventDefault();
      add();
    }
  };

  return (
    <div>
      <div className="mb-2.5 flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <h3 className="text-[14px] font-semibold leading-5 text-ink">Add recipients</h3>
        <p className="text-[12.5px] leading-5 text-muted">
          {full ? (
            <span className="text-danger">
              That&apos;s {MAX_RECIPIENTS_PER_UPLOAD.toLocaleString()} people, as many as one upload
              carries. Add the rest once these are in.
            </span>
          ) : (
            <>
              Anything you fill in here reaches the call as{" "}
              <code className="font-mono text-[12px] text-ink-soft">{"{{userdata.field}}"}</code>.
            </>
          )}
        </p>
      </div>

      {/* The number and the two buttons stay put; only the fields scroll, so the
          action that commits the row is never off the right edge. */}
      <div className="flex items-end gap-2">
        <label className="grid flex-none gap-1">
          <span className="text-[11.5px] font-medium leading-4 text-faint">Phone number</span>
          <Input
            value={to}
            onChange={(e) => setTo(e.target.value)}
            onKeyDown={onEnter}
            placeholder="+91 98765 43210"
            className="min-h-9 w-[190px] py-1.5 font-mono text-[13px]"
          />
        </label>

        {/* The negative margin cancels the room left for a scrollbar, so these
            fields still sit on the same baseline as the number beside them. */}
        <div className="-mb-1 flex min-w-0 shrink items-end gap-2 overflow-x-auto pb-1">
          {columns.map((field) => (
            <label key={field} className="group/field grid flex-none gap-1">
              <span className="flex items-center gap-1 text-[11.5px] font-medium leading-4 text-faint">
                <span className="max-w-[130px] truncate font-mono">{field}</span>
                {canRemoveField(field) && (
                  <button
                    type="button"
                    onClick={() => onRemoveField(field)}
                    aria-label={`Remove the ${field} field`}
                    className="opacity-0 transition-opacity hover:text-danger focus-visible:opacity-100 group-hover/field:opacity-100"
                  >
                    <svg className="h-3 w-3" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" aria-hidden>
                      <path d="m3 3 6 6M9 3 3 9" />
                    </svg>
                  </button>
                )}
              </span>
              <Input
                value={values[field] ?? ""}
                onChange={(e) => setValues({ ...values, [field]: e.target.value })}
                onKeyDown={onEnter}
                aria-label={field}
                className="min-h-9 w-[140px] py-1.5 text-[13px]"
              />
            </label>
          ))}

          {addingField && (
            <label className="grid flex-none gap-1">
              <span className="text-[11.5px] font-medium leading-4 text-faint">New field</span>
              <Input
                autoFocus
                value={naming}
                onChange={(e) => setNaming(e.target.value)}
                onBlur={commitField}
                onKeyDown={(e) => {
                  if (e.key === "Enter") {
                    e.preventDefault();
                    commitField();
                  }
                  if (e.key === "Escape") {
                    setNaming("");
                    setAddingField(false);
                  }
                }}
                placeholder="first_name"
                aria-label="Name of the new field"
                className="min-h-9 w-[140px] py-1.5 font-mono text-[13px]"
              />
            </label>
          )}
        </div>

        {!addingField && (
          <Button
            variant="secondary"
            size="sm"
            onClick={() => setAddingField(true)}
            className="min-h-9 flex-none"
          >
            {Icon.plus}
            Field
          </Button>
        )}
        <Button size="sm" onClick={add} disabled={!to.trim() || full} className="min-h-9 flex-none">
          Add
        </Button>
      </div>
    </div>
  );
}
