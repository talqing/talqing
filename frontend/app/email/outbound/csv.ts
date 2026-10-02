/* A batch's list as a spreadsheet, both ways, in the browser.
 *
 * In: the dashboard is a static export with no server of its own and the API
 * takes JSON, so an uploaded CSV is parsed here and posted as `recipients[]`.
 * That is also what lets the create panel show exactly what it understood,
 * column by column, before anything is sent.
 *
 * Out: an export is built here too, from the same recipients list an API caller
 * pages through. The writer is the parser's inverse, so an exported file can be
 * uploaded again as a new batch.
 *
 * The RFC 4180 parser itself is shared with batch calling rather than written
 * twice: two CSV parsers that agree today is exactly the kind of duplication
 * that stops agreeing quietly.
 */

import type { EmailBatchResponse, EmailRecipientResponse } from "@talqing/sdk";
import { parseCsv } from "@/app/telephony/outbound-calling/recipients";
import { ROW_STATUS_LABEL } from "./shared";

/** `MAX_RECIPIENTS_PER_REQUEST` in `backend/services/email/batch/models.py` and
 *  `backend/services/whatsapp/models.py`: rows per upload, not per batch.
 *
 *  Mirrored here rather than discovered from a 400, because the 400 arrives
 *  after the whole form has been filled in — a file this size is refused where
 *  it is chosen, which is also where splitting it is still easy. */
export const MAX_RECIPIENTS_PER_UPLOAD = 10_000;

/** Headers become the task's `{{vars.*}}` names, so they have to be readable
 *  back as one. Same rule the API applies, checked here so it is a red column
 *  header rather than a 400 after the form is filled in. */
const VAR_NAME = /^[A-Za-z_][A-Za-z0-9_]*$/;

/** Why ``name`` cannot be a column, or "" if it can.
 *
 *  The same rule ``parseRecipientCsv`` applies to a file's headers, exported so
 *  a column somebody types by hand is held to exactly one standard rather than
 *  a second one that agrees today. */
export function columnNameProblem(name: string, existing: string[]): string {
  if (!name) return "Give the column a name.";
  if (existing.includes(name)) return "There is already a column with that name.";
  if (!VAR_NAME.test(name)) {
    return "Use letters, digits and underscores, starting with a letter.";
  }
  return "";
}

export type ParsedList = {
  /** In the operator's own column order. Kept even when a column is blank on
   *  every row: the field map may still point at it. */
  columns: string[];
  /** One per data row, keyed by header. Blank cells included — the server drops
   *  them, and the preview needs to show which ones are blank. */
  rows: Record<string, string>[];
  /** Columns whose names cannot be variables, with why. */
  badColumns: { name: string; reason: string }[];
  /** A problem with the file itself rather than with a column. */
  error: string;
};

export const EMPTY_LIST: ParsedList = { columns: [], rows: [], badColumns: [], error: "" };

export function parseRecipientCsv(text: string): ParsedList {
  const grid = parseCsv(text);
  if (!grid.length) return { ...EMPTY_LIST, error: "That file has no rows in it." };

  const headers = grid[0].map((h) => h.trim());
  const columns: string[] = [];
  const badColumns: { name: string; reason: string }[] = [];
  headers.forEach((header, index) => {
    if (!header) return;
    if (columns.includes(header)) {
      badColumns.push({ name: header, reason: `Column ${index + 1} repeats this name` });
      return;
    }
    if (!VAR_NAME.test(header)) {
      badColumns.push({
        name: header,
        reason: "Use letters, digits and underscores, starting with a letter",
      });
    }
    columns.push(header);
  });
  if (!columns.length) {
    return { ...EMPTY_LIST, error: "That file's first row has no column names in it." };
  }
  if (grid.length - 1 > MAX_RECIPIENTS_PER_UPLOAD) {
    return {
      ...EMPTY_LIST,
      error: `That file has ${(grid.length - 1).toLocaleString()} rows. One upload takes at most ${MAX_RECIPIENTS_PER_UPLOAD.toLocaleString()} — split it, and add the rest to the batch once it exists.`,
    };
  }

  const rows = grid.slice(1).map((cells) => {
    const row: Record<string, string> = {};
    headers.forEach((header, column) => {
      if (!header || !columns.includes(header)) return;
      row[header] = (cells[column] ?? "").trim();
    });
    return row;
  });
  if (!rows.length) return { ...EMPTY_LIST, error: "That file has column names but no rows." };
  return { columns, rows, badColumns, error: "" };
}

/** How many rows have nothing in this column.
 *
 *  The header check the API makes is not the whole story, and the UI has to say
 *  so: a required column that exists but is blank on ten rows passes create and
 *  fails those ten rows at draft time. Better to see it now. */
export function blankCount(rows: Record<string, string>[], column: string): number {
  return rows.reduce((n, row) => (row[column]?.trim() ? n : n + 1), 0);
}

/** RFC 4180, the inverse of `parseCsv`. A field is quoted only when it has to
 *  be, and whitespace is kept: the file holds what the batch holds. */
export function toCsv(grid: string[][]): string {
  return grid
    .map((row) =>
      row
        .map((field) => (/[",\r\n]/.test(field) ? `"${field.replace(/"/g, '""')}"` : field))
        .join(","),
    )
    .join("\r\n");
}

/** One row of an export, cut down to what the file needs: a recipient from the
 *  API carries each of its values three times. */
export type ExportRow = {
  columns: EmailRecipientResponse["columns"];
  status: EmailRecipientResponse["status"];
  error: string | null;
};

/** The whole batch: the uploaded columns, the task's, then how far each row got.
 *
 *  A plain list of headers, never a set. A batch that already has a `status` or
 *  `error` column — a task output field, or an export uploaded again — keeps it,
 *  and the name appears twice. */
export function batchCsv(batch: EmailBatchResponse, rows: ExportRow[]): string {
  const columns = [...batch.input_columns, ...batch.output_columns];
  return toCsv([
    [...columns, "status", "error"],
    ...rows.map((row) => [
      ...columns.map((column) => {
        const value = row.columns[column];
        return value == null ? "" : String(value);
      }),
      ROW_STATUS_LABEL[row.status],
      row.error ?? "",
    ]),
  ]);
}
