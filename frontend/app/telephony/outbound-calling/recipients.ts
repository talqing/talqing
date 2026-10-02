/* Turning a spreadsheet into a recipient list, in the browser.
 *
 * The dashboard is a static export with no server of its own and the API takes
 * JSON, so the CSV never leaves the tab as a file — it is parsed here and posted
 * as `recipients[]`. That is also what lets the panel show the operator exactly
 * what it understood, row by row, before anything is sent.
 *
 * **On validation, and where the authority lives.** Only the server knows how a
 * number normalizes: it applies the workspace's default region, so `9876543210`
 * is a valid Indian mobile there and an unjudgeable fragment here. So this file
 * flags what is unambiguously wrong in any region — nothing typed, too few
 * digits to be a phone number anywhere, more than E.164 allows, characters that
 * are not part of one — and leaves the rest to the API, whose 400 names every
 * bad row and is rendered against these same rows. Two phone-number parsers that
 * agree today is exactly the kind of duplication that stops agreeing quietly.
 */

/** The one reserved CSV header. Every other column becomes a `userdata` key. */
export const PHONE_COLUMN = "phone_number";

/**
 * The most rows one upload may carry — `MAX_RECIPIENTS_PER_REQUEST` in
 * `backend/services/telephony/batch/models.py`. A batch itself has no ceiling:
 * the rest go in as further uploads.
 *
 * Mirrored here rather than discovered from a 400, because the 400 arrives
 * *after* the whole policy form has been filled in and Start pressed. A file
 * this size is refused where it is chosen, which is also where splitting it is
 * still an easy thing to do.
 */
export const MAX_RECIPIENTS_PER_UPLOAD = 10_000;

export type RecipientRow = {
  /** 1-based position in this upload, and the operator's line number: it is
   *  what an error names and what they will look for in their own spreadsheet. */
  rowNumber: number;
  to: string;
  userdata: Record<string, string>;
  /** Why this row cannot be sent. Empty means it is fine as far as we can tell. */
  problems: string[];
};

/** RFC 4180: quoted fields may hold commas, newlines and doubled quotes.
 *
 *  Shared with email outbound (`app/email/outbound/csv.ts`) rather than
 *  written twice — two CSV parsers that agree today is exactly the kind of
 *  duplication that stops agreeing quietly. */
export function parseCsv(text: string): string[][] {
  const rows: string[][] = [];
  let row: string[] = [];
  let field = "";
  let quoted = false;
  let i = 0;
  // A BOM on a file exported from Excel would otherwise become part of the
  // first header, so `phone_number` would not match and every row would fail.
  const src = text.charCodeAt(0) === 0xfeff ? text.slice(1) : text;
  const pushField = () => {
    row.push(field);
    field = "";
  };
  const pushRow = () => {
    pushField();
    if (row.some((cell) => cell.trim() !== "")) rows.push(row);
    row = [];
  };
  while (i < src.length) {
    const ch = src[i];
    if (quoted) {
      if (ch === '"') {
        if (src[i + 1] === '"') {
          field += '"';
          i += 2;
          continue;
        }
        quoted = false;
        i++;
        continue;
      }
      field += ch;
      i++;
      continue;
    }
    if (ch === '"') {
      quoted = true;
      i++;
      continue;
    }
    if (ch === ",") {
      pushField();
      i++;
      continue;
    }
    if (ch === "\r") {
      i++;
      continue;
    }
    if (ch === "\n") {
      pushRow();
      i++;
      continue;
    }
    field += ch;
    i++;
  }
  if (field !== "" || row.length) pushRow();
  return rows;
}

export type ParsedCsv = {
  rows: RecipientRow[];
  /** Every `userdata` key the file declared, in column order — the preview
   *  table renders one column per key so the operator sees what each call will
   *  actually receive. */
  keys: string[];
  /** A problem with the file itself rather than with a row. */
  error: string;
};

export function parseRecipientCsv(text: string): ParsedCsv {
  const grid = parseCsv(text);
  if (!grid.length) return { rows: [], keys: [], error: "That file has no rows in it." };
  const headers = grid[0].map((h) => h.trim());
  const phoneAt = headers.findIndex((h) => h.toLowerCase() === PHONE_COLUMN);
  if (phoneAt === -1) {
    return {
      rows: [],
      keys: [],
      error: `No ${PHONE_COLUMN} column. Add one — every other column becomes data the agent can read.`,
    };
  }
  if (grid.length - 1 > MAX_RECIPIENTS_PER_UPLOAD) {
    return {
      rows: [],
      keys: [],
      error: `That file has ${(grid.length - 1).toLocaleString()} rows. One upload takes at most ${MAX_RECIPIENTS_PER_UPLOAD.toLocaleString()} — split it, and add the rest to the batch once it exists.`,
    };
  }
  const keys = headers.filter((h, index) => index !== phoneAt && h !== "");
  const rows = grid.slice(1).map((cells, index) => {
    const userdata: Record<string, string> = {};
    headers.forEach((header, column) => {
      if (column === phoneAt || header === "") return;
      const value = (cells[column] ?? "").trim();
      // Empty cells are omitted rather than stored as "", so
      // `{{userdata.name}}` resolves to nothing and the greeting still reads as
      // written instead of "Hi , how are you".
      if (value) userdata[header] = value;
    });
    return checkRow({
      rowNumber: index + 1,
      to: (cells[phoneAt] ?? "").trim(),
      userdata,
      problems: [],
    });
  });
  return { rows: withDuplicateFlags(rows), keys, error: "" };
}

/** Row-level checks that hold in every region. See the note at the top. */
export function checkRow(row: RecipientRow): RecipientRow {
  const problems: string[] = [];
  const digits = row.to.replace(/\D/g, "");
  if (!row.to) problems.push("No phone number");
  else if (/[^0-9+()\-.\s]/.test(row.to)) problems.push("Not a phone number");
  else if (digits.length < 8) problems.push("Too short to be a phone number");
  else if (digits.length > 15) problems.push("Too long to be a phone number");
  for (const key of Object.keys(row.userdata)) {
    if (key.startsWith("_talqing")) problems.push(`“${key}” is a reserved name`);
  }
  return { ...row, problems };
}

/**
 * Flag repeats within one list.
 *
 * Compared on digits alone, which catches the case that actually happens — the
 * same number twice in an exported list — and misses the one where the same
 * person appears once in national and once in international form. The API
 * normalizes both and refuses the batch naming both rows, and that 400 is
 * rendered against these rows, so nothing gets through either way.
 */
export function withDuplicateFlags(rows: RecipientRow[]): RecipientRow[] {
  const firstAt = new Map<string, number>();
  return rows.map((row) => {
    const digits = row.to.replace(/\D/g, "");
    if (!digits) return row;
    const seen = firstAt.get(digits);
    if (seen === undefined) {
      firstAt.set(digits, row.rowNumber);
      return row;
    }
    return { ...row, problems: [...row.problems, `Already on row ${seen}`] };
  });
}

/** Renumber from 1 after an insert or a delete, then re-check for duplicates. */
export function renumber(rows: RecipientRow[]): RecipientRow[] {
  return withDuplicateFlags(
    rows.map((row, index) => checkRow({ ...row, rowNumber: index + 1, problems: [] })),
  );
}

/** Every `userdata` key present across a list, in first-seen order. */
export function keysOf(rows: RecipientRow[]): string[] {
  const keys: string[] = [];
  for (const row of rows) {
    for (const key of Object.keys(row.userdata)) if (!keys.includes(key)) keys.push(key);
  }
  return keys;
}

/**
 * Map the API's row-numbered errors back onto the rows they name.
 *
 * The server answers a bad list with one 400 listing every bad row — "row 47:
 * '98765' is not a valid phone number" — which is the right shape for a client
 * that has the rows in front of it. Anything that does not name a row (a
 * problem with the agent, the number, the schedule) is handed back to be shown
 * as a form-level error instead.
 */
export function applyServerErrors(
  rows: RecipientRow[],
  errors: string[],
): { rows: RecipientRow[]; unattached: string[] } {
  const byRow = new Map<number, string[]>();
  const unattached: string[] = [];
  for (const message of errors) {
    const match = /^row (\d+):\s*(.*)$/.exec(message);
    if (!match) {
      unattached.push(message);
      continue;
    }
    const at = Number(match[1]);
    byRow.set(at, [...(byRow.get(at) ?? []), match[2]]);
  }
  return {
    rows: rows.map((row) => {
      const extra = byRow.get(row.rowNumber);
      return extra ? { ...row, problems: [...row.problems, ...extra] } : row;
    }),
    unattached,
  };
}
