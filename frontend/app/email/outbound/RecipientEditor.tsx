"use client";

import { useMemo, useRef, useState } from "react";
import { Button, Input } from "@/app/components/ui";
import { cn } from "@/lib/cn";
import {
  EMPTY_LIST,
  MAX_RECIPIENTS_PER_UPLOAD,
  type ParsedList,
  columnNameProblem,
  parseRecipientCsv,
} from "./csv";

const PAGE_SIZE = 12;

/** What differs between the lists this editor builds: the columns an empty
 *  list starts with, a sample file, and what a column becomes downstream. */
export type ListCopy = {
  /** Where the cursor starts on an empty list. Only suggestions: nothing is
   *  committed until a row carries a value, and any of them can be removed. */
  starterColumns: string[];
  sample: { csv: string; fileName: string };
  /** Beside "Add rows": what a typed column becomes. */
  composerNote: React.ReactNode;
  /** Under "Upload a CSV", after "The first row is the column names, and". */
  uploadNote: React.ReactNode;
  /** In place of the table while the list is empty. */
  emptyNote: string;
};

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
 * Where the list comes from: typed in one row at a time, or a CSV.
 *
 * Both are on screen at once rather than behind a toggle — they are not two
 * modes of one thing, they are the two ways a list arrives, and a five-person
 * list should never require making a file. Batch calling settled this the same
 * way; this is the same shape over a different row.
 *
 * The list is rendered — paginated, one column per variable, every cell
 * editable — before anything is drafted, because what an operator needs to see
 * is not that the file parsed but *what each run will actually receive*. A task
 * that opens with "Hi {{vars.first_name}}" is one empty column away from
 * writing five thousand emails that open with "Hi ,".
 */
export function RecipientEditor({
  list,
  onChange,
  copy,
}: {
  list: ParsedList;
  onChange: (list: ParsedList) => void;
  copy: ListCopy;
}) {
  const [page, setPage] = useState(0);
  const [fileError, setFileError] = useState("");
  const [fileName, setFileName] = useState("");
  const [dropping, setDropping] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);

  /* An empty list still shows one column so there is somewhere to type. It is
     only a suggestion until a row carries it — nothing is committed to the
     list until Add is pressed. */
  const columns = list.columns.length ? list.columns : copy.starterColumns;
  const pages = Math.max(1, Math.ceil(list.rows.length / PAGE_SIZE));
  const current = Math.min(page, pages - 1);
  const visible = useMemo(
    () => list.rows.slice(current * PAGE_SIZE, current * PAGE_SIZE + PAGE_SIZE),
    [list.rows, current],
  );

  async function onFile(file: File | undefined) {
    if (!file) return;
    setFileError("");
    const parsed = parseRecipientCsv(await file.text());
    if (parsed.error) {
      /* Keep what is already on the list. A bad second upload almost always
         means the wrong file was picked, and losing an afternoon's typing to
         that is not a thing this dialog gets to do. */
      setFileError(parsed.error);
      return;
    }
    setFileName(file.name);
    // Replace rather than append, for the same reason.
    onChange(parsed);
    setPage(0);
  }

  function updateCell(rowIndex: number, column: string, value: string) {
    onChange({
      ...list,
      rows: list.rows.map((row, i) => (i === rowIndex ? { ...row, [column]: value } : row)),
    });
  }

  return (
    /* flex-col rather than grid: a grid track is min-content-sized, so one wide
       table would push the whole panel past the dialog instead of scrolling
       inside it. */
    <div className="flex flex-col gap-3">
      <div className="rounded-xl border border-line-2 bg-white p-4">
        <Composer
          columns={columns}
          full={list.rows.length >= MAX_RECIPIENTS_PER_UPLOAD}
          note={copy.composerNote}
          /* A column every row is blank on is one nobody has committed to yet,
             so it can go. One that carries values cannot: a × that quietly
             stripped a column off five thousand rows is not a × anyone should
             be able to press by accident. */
          canRemoveColumn={(column) =>
            columns.length > 1 && list.rows.every((row) => !row[column]?.trim())
          }
          onRemoveColumn={(column) =>
            onChange({
              ...list,
              columns: columns.filter((c) => c !== column),
              rows: list.rows.map(({ [column]: _dropped, ...rest }) => rest),
            })
          }
          onAddColumn={(column) => onChange({ ...list, columns: [...columns, column] })}
          onAdd={(row) => {
            onChange({ ...list, columns, rows: [...list.rows, row] });
            setPage(Math.floor(list.rows.length / PAGE_SIZE));
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
              {list.rows.length > 0
                ? `Replaces the ${list.rows.length.toLocaleString()} already on the list. `
                : "Drop it here, or click to pick one. "}
              The first row is the column names, and {copy.uploadNote} Up to{" "}
              {MAX_RECIPIENTS_PER_UPLOAD.toLocaleString()} rows.
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
            link.href = `data:text/csv;charset=utf-8,${encodeURIComponent(copy.sample.csv)}`;
            link.download = copy.sample.fileName;
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

      {list.badColumns.length > 0 && (
        <ul className="grid gap-1 rounded-lg border border-danger/25 bg-white px-3 py-2 text-[13px] leading-5 text-danger">
          {list.badColumns.map((c) => (
            <li key={c.name}>
              <span className="font-mono">{c.name || "(blank)"}</span> — {c.reason}
            </li>
          ))}
        </ul>
      )}

      {list.rows.length === 0 ? (
        <p className="text-[13px] leading-5 text-muted">
          {copy.emptyNote}
        </p>
      ) : (
        <div className="overflow-hidden rounded-xl border border-line-2 bg-white">
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1 border-b border-line px-3.5 py-2.5">
            <span className="text-[13px] font-semibold leading-5 text-ink tabular-nums">
              {list.rows.length.toLocaleString()} {list.rows.length === 1 ? "row" : "rows"}
            </span>
            <span className="text-[12.5px] leading-5 text-faint">
              · {columns.length} {columns.length === 1 ? "column" : "columns"}
            </span>
            <button
              type="button"
              onClick={() => {
                onChange(EMPTY_LIST);
                setFileName("");
              }}
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
                  {columns.map((column) => (
                    <th
                      key={column}
                      className="min-w-[150px] px-3 py-2 font-mono font-medium text-ink-soft"
                    >
                      {column}
                    </th>
                  ))}
                  <th className="w-10 px-3 py-2" />
                </tr>
              </thead>
              <tbody>
                {visible.map((row, i) => {
                  const index = current * PAGE_SIZE + i;
                  return (
                    <tr key={index} className="group border-b border-line last:border-0 hover:bg-canvas">
                      <td className="px-3 py-1.5 font-mono text-[12px] text-faint tabular-nums">
                        {index + 1}
                      </td>
                      {columns.map((column) => (
                        <td key={column} className="px-3 py-1.5">
                          <input
                            value={row[column] ?? ""}
                            onChange={(e) => updateCell(index, column, e.target.value)}
                            aria-label={`${column} on row ${index + 1}`}
                            className="w-full rounded-md border border-transparent bg-transparent px-1.5 py-1 text-[12.5px] text-ink outline-none transition-colors hover:border-line-2 focus:border-ink focus:bg-white"
                          />
                        </td>
                      ))}
                      <td className="px-3 py-1.5 text-right">
                        <button
                          type="button"
                          onClick={() =>
                            onChange({ ...list, rows: list.rows.filter((_, r) => r !== index) })
                          }
                          aria-label={`Remove row ${index + 1}`}
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
                Rows {current * PAGE_SIZE + 1}–
                {Math.min(list.rows.length, (current + 1) * PAGE_SIZE)} of{" "}
                {list.rows.length.toLocaleString()}
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
      )}
    </div>
  );
}

/**
 * One row, typed.
 *
 * The fields are the list's own columns, so the sixth row added to a five-row
 * list is filled in under the same headings rather than by naming them again —
 * and a column named here becomes a column of the list the moment the first row
 * carries a value for it.
 */
function Composer({
  columns,
  full,
  note,
  canRemoveColumn,
  onAddColumn,
  onRemoveColumn,
  onAdd,
}: {
  columns: string[];
  /** The list holds as many rows as one upload may carry. */
  full: boolean;
  note: React.ReactNode;
  canRemoveColumn: (column: string) => boolean;
  onAddColumn: (column: string) => void;
  onRemoveColumn: (column: string) => void;
  onAdd: (row: Record<string, string>) => void;
}) {
  const [values, setValues] = useState<Record<string, string>>({});
  const [naming, setNaming] = useState("");
  const [addingColumn, setAddingColumn] = useState(false);
  const [nameError, setNameError] = useState("");

  const filled = columns.some((column) => (values[column] ?? "").trim() !== "");

  function add() {
    onAdd(
      Object.fromEntries(columns.map((column) => [column, (values[column] ?? "").trim()])),
    );
    setValues({});
  }

  function commitColumn() {
    const name = naming.trim();
    if (!name) {
      setNaming("");
      setNameError("");
      setAddingColumn(false);
      return;
    }
    const problem = columnNameProblem(name, columns);
    if (problem) {
      setNameError(problem);
      return;
    }
    onAddColumn(name);
    setNaming("");
    setNameError("");
    setAddingColumn(false);
  }

  const onEnter = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && filled && !full) {
      e.preventDefault();
      add();
    }
  };

  return (
    <div>
      <div className="mb-2.5 flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <h3 className="text-[14px] font-semibold leading-5 text-ink">Add rows</h3>
        <p className="text-[12.5px] leading-5 text-muted">
          {full ? (
            <span className="text-danger">
              That&apos;s {MAX_RECIPIENTS_PER_UPLOAD.toLocaleString()} rows, as many as one upload
              carries. Add the rest once these are in.
            </span>
          ) : (
            note
          )}
        </p>
      </div>

      {/* Only the fields scroll, so Add — the thing that commits the row — is
          never off the right edge however many columns the list has. */}
      <div className="flex items-end gap-2">
        {/* The negative margin cancels the room left for a scrollbar, so these
            fields still sit on the same baseline as the buttons beside them. */}
        <div className="-mb-1 flex min-w-0 flex-1 items-end gap-2 overflow-x-auto pb-1">
          {columns.map((column) => (
            <label key={column} className="group/field grid flex-none gap-1">
              <span className="flex items-center gap-1 text-[11.5px] font-medium leading-4 text-faint">
                <span className="max-w-[130px] truncate font-mono">{column}</span>
                {canRemoveColumn(column) && (
                  <button
                    type="button"
                    onClick={() => onRemoveColumn(column)}
                    aria-label={`Remove the ${column} column`}
                    className="opacity-0 transition-opacity hover:text-danger focus-visible:opacity-100 group-hover/field:opacity-100"
                  >
                    <svg className="h-3 w-3" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" aria-hidden>
                      <path d="m3 3 6 6M9 3 3 9" />
                    </svg>
                  </button>
                )}
              </span>
              <Input
                value={values[column] ?? ""}
                onChange={(e) => setValues({ ...values, [column]: e.target.value })}
                onKeyDown={onEnter}
                aria-label={column}
                className="min-h-9 w-[170px] py-1.5 text-[13px]"
              />
            </label>
          ))}

          {addingColumn && (
            <label className="grid flex-none gap-1">
              <span className="text-[11.5px] font-medium leading-4 text-faint">New column</span>
              <Input
                autoFocus
                value={naming}
                onChange={(e) => {
                  setNaming(e.target.value);
                  setNameError("");
                }}
                onBlur={commitColumn}
                onKeyDown={(e) => {
                  if (e.key === "Enter") {
                    e.preventDefault();
                    commitColumn();
                  }
                  if (e.key === "Escape") {
                    setNaming("");
                    setNameError("");
                    setAddingColumn(false);
                  }
                }}
                aria-invalid={Boolean(nameError) || undefined}
                placeholder="first_name"
                aria-label="Name of the new column"
                className="min-h-9 w-[170px] py-1.5 font-mono text-[13px]"
              />
            </label>
          )}
        </div>

        {!addingColumn && (
          <Button
            variant="secondary"
            size="sm"
            onClick={() => setAddingColumn(true)}
            className="min-h-9 flex-none"
          >
            {Icon.plus}
            Column
          </Button>
        )}
        <Button size="sm" onClick={add} disabled={!filled || full} className="min-h-9 flex-none">
          Add
        </Button>
      </div>

      {nameError && <p className="mt-1.5 text-[12.5px] leading-5 text-danger">{nameError}</p>}
    </div>
  );
}
